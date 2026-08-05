from __future__ import annotations

import logging
import re
import requests
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, scrolledtext, ttk
from typing import Any


if __package__ in {None, ""}:
    repo_root = Path(__file__).resolve().parents[2]
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))

from adb_bot.automation import AutomationRunner
from adb_bot.automation.workflow import run_profile_workflow
from adb_bot.config import get_bearer_token
from adb_bot.config.settings import load_settings, save_settings, get_saved_bearer_token, get_saved_batch_launch_delay, get_saved_readiness_wait, get_saved_readiness_attempts, get_app_data_dir, get_scheduler_config, save_scheduler_config, get_saved_flow_speed, get_folder_media_paths, save_folder_media_paths
from adb_bot.automation import scheduling as scheduler_admin
from adb_bot.core.batching import LaunchGate, resolve_concurrency, run_rolling
from adb_bot.core.locks import live_profile_count, live_profile_slot
from adb_bot.core.logger import get_logger
from adb_bot.core.models import Profile
from adb_bot.clients.api import MultiloginApiClient
from adb_bot.clients.multilogin import (
    MultiloginAdbEnableClient,
    MultiloginFolderClient,
    MultiloginLauncherClient,
    MultiloginMobileListClient,
    MultiloginShutdownClient,
)
from adb_bot.automation.flows import InstagramLikeFeedFlow, InstagramNotificationsFlow, InstagramScrollFlow, InstagramStoryUploadFlow, InstagramReelUploadFlow, InstagramReelUploadU2Flow, InstagramReelIntentProbeFlow, InstagramUpdateBioFlow, InstagramUpdateBioU2Flow, InstagramUpdateProfilePictureU2Flow, InstagramWarmUpDay1Flow, PushMediaTestFlow
from adb_bot.ui.helpers import (
    REEL_FLOWS,
    build_foldered_profile_groups,
    build_profile_options,
    folders_missing_media,
    get_available_flows,
    resolve_folder_media_path,
)
from adb_bot.automation.flows.story_media import discover_story_media_files
from adb_bot.clients.airtable import AirtableClient
from adb_bot.automation.airtable_runner import run_airtable_queue

# How long a hand-started profile waits for a place under the global live-phone
# ceiling before it is given up on. The scheduled loops skip immediately (the
# next tick retries), but nothing retries for a person, so this path waits --
# bounded, because a UI that sits there forever is indistinguishable from a hang.
# 90s is about one posting profile's turnaround, so a run started while a loop is
# finishing usually just proceeds.
UI_SLOT_WAIT_SECONDS = 90.0


class TextLogHandler(logging.Handler):
    def __init__(self, widget: scrolledtext.ScrolledText) -> None:
        super().__init__()
        self.widget = widget

    def emit(self, record: logging.LogRecord) -> None:
        message = self.format(record)
        self.widget.after(0, self._append, message)

    def _append(self, message: str) -> None:
        self.widget.configure(state="normal")
        try:
            # Insert the message in chunks so only the log-level word gets tagged.
            for level, tag in (("INFO", "info"), ("WARNING", "warning"), ("ERROR", "error")):
                token = f" | {level} |"
                pos = message.find(token)
                if pos != -1:
                    prefix = message[: pos + 3]
                    suffix = message[pos + 3 + len(level) :]
                    self.widget.insert("end", prefix)
                    self.widget.insert("end", level, tag)
                    self.widget.insert("end", suffix + "\n")
                    break
            else:
                self.widget.insert("end", message + "\n")
        finally:
            self.widget.see("end")
            self.widget.configure(state="disabled")


def parse_profiles_from_response(api_client: MultiloginApiClient, api_response: dict):
    if hasattr(api_client, "parse_profiles") and callable(api_client.parse_profiles):
        try:
            parsed_profiles = api_client.parse_profiles(api_response)
        except TypeError:
            parsed_profiles = None
        if isinstance(parsed_profiles, list):
            return parsed_profiles
        if isinstance(parsed_profiles, tuple):
            return list(parsed_profiles)
        if parsed_profiles is not None and not isinstance(parsed_profiles, (str, bytes, dict)):
            try:
                return list(parsed_profiles)
            except TypeError:
                pass

    items = api_response.get("data", {}).get("items", []) or []
    return list(items)


def profile_matches_id(profile: Any, profile_id: str) -> bool:
    profile_id_value = getattr(profile, "id", None)
    if profile_id_value is None and isinstance(profile, dict):
        profile_id_value = profile.get("id")
    return profile_id_value == profile_id


def profile_is_ready(profile: Any) -> bool:
    explicit_ready = getattr(profile, "is_ready", None)
    if explicit_ready is not None:
        return bool(explicit_ready)

    if isinstance(profile, dict):
        status = profile.get("status")
        return str(status).lower() in {"active", "ready"}

    status = getattr(profile, "status", None)
    return str(status).lower() in {"active", "ready"}


def get_profile_id(profile: Any) -> str | None:
    if isinstance(profile, dict):
        return profile.get("id")
    return getattr(profile, "id", None)


def coerce_profile(profile: Any, profile_id: str) -> Profile:
    if isinstance(profile, Profile):
        return profile

    if isinstance(profile, dict):
        return Profile(
            id=profile.get("id", profile_id),
            status=profile.get("status", ""),
            ip=profile.get("ip"),
            port=profile.get("port"),
            pwd=profile.get("pwd"),
        )

    return Profile(
        id=get_profile_id(profile) or profile_id,
        status=getattr(profile, "status", ""),
        ip=getattr(profile, "ip", None),
        port=getattr(profile, "port", None),
        pwd=getattr(profile, "pwd", None),
    )


def prepare_profile_for_adb(
    profile_id: str,
    api_client: MultiloginApiClient,
    adb_enable_client: MultiloginAdbEnableClient,
    logger: logging.Logger,
    max_attempts: int = 2,
    wait_seconds: int = 10,
):
    profile = None

    for attempt in range(1, max_attempts + 1):
        logger.info("Waiting for profile %s readiness, attempt %s/%s", profile_id, attempt, max_attempts)
        logger.info("Sleeping %s seconds before checking again", wait_seconds)
        time.sleep(wait_seconds)

        logger.info("Enabling ADB for profile %s before checking credentials", profile_id)
        adb_enable_response = adb_enable_client.enable_adb([profile_id], enabled=True)
        logger.info("ADB enable response for %s: %s", profile_id, adb_enable_response)

        logger.info("Checking ADB credentials for %s", profile_id)
        api_response = api_client.fetch_adb_credentials([profile_id])
        profiles = parse_profiles_from_response(api_client, api_response)
        profile = next((item for item in profiles if profile_matches_id(item, profile_id) and profile_is_ready(item)), None)
        if profile:
            profile = coerce_profile(profile, profile_id)
            logger.info("Profile %s is ready after ADB enable attempt %s", profile_id, attempt)
            return profile

        if attempt < max_attempts:
            logger.info("Profile %s still not ready after attempt %s; retrying enable and check", profile_id, attempt)

    logger.warning("Profile %s is not ready for ADB automation", profile_id)
    return None


class WorkflowUI:
    def __init__(self) -> None:
        self.root = tk.Tk()
        self.root.title("ADB Bot Workflow UI")
        self.root.geometry("1100x700")
        self.root.minsize(900, 600)

        self.profile_vars: dict[str, tk.BooleanVar] = {}
        self.profile_labels: dict[str, str] = {}
        self.profile_status_vars: dict[str, tk.StringVar] = {}
        self.profile_last_status: dict[str, str] = {}  # profile_id -> normalized terminal status, for the run summary
        # One row per profile from the last run: (category, name, detail).
        self._run_summary_rows: list[tuple[str, str, str]] = []
        self._run_summary_sort: tuple[str, bool] | None = None  # (column, descending)
        self.profile_progress_vars: dict[str, tk.StringVar] = {}
        self.profile_manual_continue_events: dict[str, threading.Event] = {}
        self.profile_manual_continue_buttons: dict[str, ttk.Button] = {}
        self.folder_sections: dict[str, ttk.Frame] = {}
        self.folder_toggles: dict[str, ttk.Button] = {}
        self.folder_expanded: dict[str, bool] = {}
        self.folder_names: dict[str, str] = {}  # folder_key -> display name
        self.folder_media_vars: dict[str, tk.StringVar] = {}  # folder_key -> label text
        # folder_key -> local media folder, persisted so folders are mapped once
        # and every later run just picks profiles.
        self.folder_media_paths: dict[str, str] = get_folder_media_paths()
        self._run_folder_media: dict[str, str] = {}  # snapshot taken at run start
        self.profile_search_var: tk.StringVar | None = None
        self.profile_to_folder: dict[str, str] = {}  # Maps profile_id to folder_id
        self.profile_rows: dict[str, ttk.Frame] = {}  # Maps profile_id to row frame
        self.flow_options = get_available_flows()
        self.abort_requested = False
        self._active_run = None
        self._run_in_progress = False
        self._airtable_selected_count = 0
        self._airtable_override_flow = None
        self._airtable_override_bio = None
        self._airtable_override_caption = None
        self._airtable_override_picture = None
        self._run_token = 0
        self._shutdown_on_abort = True
        self._shutdown_on_success = False
        self._current_profile_ids: list[str] = []
        
        # Load settings from file or use defaults
        saved_settings = load_settings()
        self._batch_launch_delay_seconds = saved_settings.get("batch_launch_delay_seconds", 1)
        self._readiness_wait_seconds = saved_settings.get("readiness_wait_seconds", 10)
        self._readiness_max_attempts = saved_settings.get("readiness_max_attempts", 2)
        self._shutdown_on_success = bool(saved_settings.get("shutdown_on_success", False))
        
        saved_bearer_token = saved_settings.get("bearer_token", "").strip()
        if not saved_bearer_token:
            saved_bearer_token = get_bearer_token()
        
        self.bearer_token_var = tk.StringVar(value=saved_bearer_token)
        self.batch_launch_delay_var = tk.StringVar(value=str(self._batch_launch_delay_seconds))
        self.readiness_wait_var = tk.StringVar(value=str(self._readiness_wait_seconds))
        self.readiness_attempts_var = tk.StringVar(value=str(self._readiness_max_attempts))
        self.story_media_path_var = tk.StringVar(value=str(Path(saved_settings.get("story_media_path", "") or "").expanduser()) if (saved_settings.get("story_media_path", "") or "") else "")
        self.shutdown_on_success_var = tk.BooleanVar(value=bool(saved_settings.get("shutdown_on_success", False)))
        self.airtable_token_var = tk.StringVar(value=(saved_settings.get("airtable_token", "") or "").strip())
        self.airtable_base_id_var = tk.StringVar(value=(saved_settings.get("airtable_base_id", "") or "").strip())
        self.airtable_table_name_var = tk.StringVar(value=(saved_settings.get("airtable_table_name", "") or "").strip() or "Profiles")
        self.logger = self._build_logger()
        self._build_ui()
        self.load_profiles()

    def _build_logger(self) -> logging.Logger:
        logger = get_logger("adb_bot_ui", log_file=str(get_app_data_dir() / "logs" / "ui.log"))
        logger.propagate = False
        return logger

    def _on_flow_selection_changed(self, event: tk.Event | None = None) -> None:
        self._update_reel_caption_visibility()

    def _update_reel_caption_visibility(self) -> None:
        selected_flow = self._get_selected_flow_value()
        if selected_flow in ("instagram_reel_upload", "instagram_reel_upload_u2"):
            self.reel_caption_frame.grid()
        else:
            self.reel_caption_frame.grid_remove()
        if selected_flow in ("update_bio", "update_bio_u2"):
            self.bio_frame.grid()
        else:
            self.bio_frame.grid_remove()
        if selected_flow == "update_profile_picture":
            self.picture_frame.grid()
        else:
            self.picture_frame.grid_remove()

    def _select_profile_picture(self) -> None:
        path = filedialog.askopenfilename(
            title="Select profile picture",
            filetypes=[
                ("Images", "*.jpg *.jpeg *.png *.webp *.heic *.bmp"),
                ("All files", "*.*"),
            ],
        )
        if path:
            self.picture_var.set(path)

    def _build_ui(self) -> None:
        self.root.columnconfigure(0, weight=2, minsize=650)
        self.root.columnconfigure(1, weight=1, minsize=360)
        self.root.rowconfigure(0, weight=1)

        main = ttk.Frame(self.root, padding=16)
        main.grid(row=0, column=0, columnspan=2, sticky="nsew")
        main.columnconfigure(0, weight=2, minsize=650)
        main.columnconfigure(1, weight=1, minsize=360)
        main.rowconfigure(1, weight=1)

        top_bar = ttk.Frame(main)
        top_bar.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 12))
        top_bar.columnconfigure(0, weight=1)
        ttk.Button(top_bar, text="Dev controls", command=self.open_dev_controls_window).grid(row=0, column=0, sticky="w")
        ttk.Button(top_bar, text="Scheduler", command=self.open_scheduler_window).grid(row=0, column=1, sticky="w", padx=(8, 0))
        ttk.Button(top_bar, text="Pipeline", command=self.open_pipeline_window).grid(row=0, column=2, sticky="w", padx=(8, 0))

        left_panel = ttk.LabelFrame(main, text="Profiles", padding=12)
        left_panel.grid(row=1, column=0, rowspan=1, sticky="nsew", padx=(0, 12))
        left_panel.columnconfigure(0, weight=1)
        left_panel.columnconfigure(1, weight=0)
        left_panel.rowconfigure(2, weight=1)

        right_panel = ttk.LabelFrame(main, text="Workflow", padding=12)
        right_panel.grid(row=1, column=1, sticky="nsew")
        right_panel.columnconfigure(0, weight=1)
        right_panel.rowconfigure(5, weight=1)  # the summary/logs paned window takes the slack

        ttk.Label(left_panel, text="Select the profiles to run:").grid(row=0, column=0, sticky="w")
        
        search_frame = ttk.Frame(left_panel)
        search_frame.grid(row=1, column=0, sticky="ew", pady=(6, 6))
        search_frame.columnconfigure(1, weight=1)
        ttk.Label(search_frame, text="Search:").grid(row=0, column=0, sticky="w", padx=(0, 4))
        self.profile_search_var = tk.StringVar()
        search_entry = ttk.Entry(search_frame, textvariable=self.profile_search_var, width=30)
        search_entry.grid(row=0, column=1, sticky="ew")
        self.profile_search_var.trace_add("write", self._on_profile_search_changed)
        
        self.profile_canvas = tk.Canvas(left_panel, borderwidth=0, highlightthickness=0, takefocus=True)
        self.profile_scrollbar = ttk.Scrollbar(left_panel, orient="vertical", command=self.profile_canvas.yview)
        self.profile_canvas.configure(yscrollcommand=self.profile_scrollbar.set)
        self.profile_canvas.grid(row=2, column=0, sticky="nsew", pady=(0, 0))
        self.profile_scrollbar.grid(row=2, column=1, sticky="ns", padx=(4, 0))

        self.profile_container = ttk.Frame(self.profile_canvas)
        self.profile_container_id = self.profile_canvas.create_window((0, 0), window=self.profile_container, anchor="nw")
        self.profile_container.columnconfigure(0, weight=1)
        self.profile_container.bind("<Configure>", self._on_profile_container_configure)
        self.profile_canvas.bind("<Configure>", self._on_profile_canvas_configure)
        self.profile_canvas.bind("<Enter>", lambda event: self.profile_canvas.focus_set())
        self.profile_container.bind("<Enter>", lambda event: self.profile_canvas.focus_set())
        self.root.bind_all("<MouseWheel>", self._on_profile_mousewheel)
        self.root.bind_all("<Button-4>", self._on_profile_mousewheel)
        self.root.bind_all("<Button-5>", self._on_profile_mousewheel)

        ttk.Label(right_panel, text="Flow:").grid(row=0, column=0, sticky="w")
        self.flow_var = tk.StringVar(value=self.flow_options[0]["label"] if self.flow_options else "")
        self.reel_caption_var = tk.StringVar(value="")
        self.bio_var = tk.StringVar(value="")
        self.picture_var = tk.StringVar(value="")
        self.flow_combo = ttk.Combobox(
            right_panel,
            textvariable=self.flow_var,
            state="readonly",
            width=40,
        )
        self.flow_combo["values"] = [flow["label"] for flow in self.flow_options]
        self.flow_combo.grid(row=1, column=0, sticky="ew", pady=(6, 12))
        self.flow_combo.bind("<<ComboboxSelected>>", self._on_flow_selection_changed)

        self.reel_caption_frame = ttk.Frame(right_panel)
        self.reel_caption_frame.grid(row=2, column=0, sticky="ew", pady=(0, 12))
        self.reel_caption_frame.columnconfigure(1, weight=1)
        ttk.Label(self.reel_caption_frame, text="Reel caption:").grid(row=0, column=0, sticky="w")
        ttk.Entry(self.reel_caption_frame, textvariable=self.reel_caption_var, width=40).grid(row=0, column=1, sticky="ew", padx=(8, 0))

        self.bio_frame = ttk.Frame(right_panel)
        self.bio_frame.grid(row=2, column=0, sticky="ew", pady=(0, 12))
        self.bio_frame.columnconfigure(1, weight=1)
        ttk.Label(self.bio_frame, text="Profile bio:").grid(row=0, column=0, sticky="w")
        ttk.Entry(self.bio_frame, textvariable=self.bio_var, width=40).grid(row=0, column=1, sticky="ew", padx=(8, 0))

        # Profile-picture picker -- occupies the same slot as the caption/bio
        # inputs, shown only for the Update Profile Picture flow. The chosen file
        # is pushed to the device (and media-scanned) like the reel/story media.
        self.picture_frame = ttk.Frame(right_panel)
        self.picture_frame.grid(row=2, column=0, sticky="ew", pady=(0, 12))
        self.picture_frame.columnconfigure(1, weight=1)
        ttk.Label(self.picture_frame, text="Profile picture:").grid(row=0, column=0, sticky="w")
        ttk.Entry(self.picture_frame, textvariable=self.picture_var, width=30, state="readonly").grid(row=0, column=1, sticky="ew", padx=(8, 4))
        ttk.Button(self.picture_frame, text="📷 Select photo", command=self._select_profile_picture).grid(row=0, column=2, sticky="e")

        buttons = ttk.Frame(right_panel)
        buttons.grid(row=3, column=0, sticky="ew")
        ttk.Button(buttons, text="Refresh profiles", command=self.load_profiles).pack(side="left")
        self.run_button = ttk.Button(buttons, text="Run selected", command=self.run_selected)
        self.run_button.pack(side="left", padx=(8, 0))
        self.airtable_button = ttk.Button(buttons, text="Run from Airtable", command=self.run_from_airtable)
        self.airtable_button.pack(side="left", padx=(8, 0))
        self.abort_button = ttk.Button(buttons, text="Abort", command=self.abort_run)
        self.abort_button.pack(side="left", padx=(8, 0))
        self.abort_button.state(["disabled"])

        # When ticked, "Run from Airtable" runs the UI-selected flow on the
        # in-scope Airtable profiles instead of each profile's lifecycle flow.
        self.airtable_use_ui_flow_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            buttons,
            text="Use UI flow",
            variable=self.airtable_use_ui_flow_var,
        ).pack(side="left", padx=(12, 0))

        self.shutdown_on_success_checkbox = ttk.Checkbutton(
            right_panel,
            # Success only, on purpose: a profile whose flow failed stays open so
            # it can be inspected (and, for a verification prompt, solved by hand).
            text="Close profile when the flow succeeds",
            variable=self.shutdown_on_success_var,
        )
        self.shutdown_on_success_checkbox.grid(row=4, column=0, sticky="w", pady=(8, 0))

        # Summary and logs split the rest of the panel through a draggable sash:
        # a 58-profile run needs a tall sheet, a 3-profile run needs none of it.
        summary_logs = ttk.PanedWindow(right_panel, orient="vertical")
        summary_logs.grid(row=5, column=0, sticky="nsew", pady=(10, 0))
        try:
            # The native sash is a 1px hairline nobody notices; widen it so the
            # drag handle is findable.
            ttk.Style(self.root).configure("Sash", sashthickness=8, gripcount=12)
        except tk.TclError:
            pass

        summary_frame = ttk.LabelFrame(summary_logs, text="Last run summary", padding=(8, 4))
        summary_frame.columnconfigure(0, weight=1)
        summary_frame.rowconfigure(1, weight=1)
        # weight=0: the sheet keeps its requested height and the logs absorb the
        # window's slack, so growing the window never shrinks the sheet.
        summary_logs.add(summary_frame, weight=0)

        # Headline counts stay a label -- one glance, no scrolling.
        self.run_summary_var = tk.StringVar(value="")
        headline = ttk.Label(
            summary_frame,
            textvariable=self.run_summary_var,
            justify="left",
            anchor="w",
        )
        headline.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 4))
        # Wrap against the frame, not the label: measuring the label itself would
        # feed its own width back in and oscillate.
        summary_frame.bind(
            "<Configure>",
            lambda event: headline.configure(wraplength=max(event.width - 24, 160)),
        )

        self.summary_tree = ttk.Treeview(
            summary_frame,
            columns=("profile", "outcome", "detail"),
            show="headings",
            height=6,
            selectmode="extended",
        )
        for key, title, width, minwidth, stretch in (
            ("profile", "Profile", 130, 80, False),
            ("outcome", "Outcome", 155, 90, False),  # fits "⚠ Needs attention"
            ("detail", "Detail", 240, 80, True),
        ):
            self.summary_tree.heading(
                key,
                text=title,
                anchor="w",
                command=lambda column=key: self._sort_run_summary(column),
            )
            self.summary_tree.column(key, width=width, minwidth=minwidth, stretch=stretch, anchor="w")
        summary_scroll = ttk.Scrollbar(summary_frame, orient="vertical", command=self.summary_tree.yview)
        self.summary_tree.configure(yscrollcommand=summary_scroll.set)
        self.summary_tree.grid(row=1, column=0, sticky="nsew")
        summary_scroll.grid(row=1, column=1, sticky="ns", padx=(4, 0))
        # Same palette as the log levels, so a red row here means what a red word
        # means down there.
        self.summary_tree.tag_configure("success", foreground="#16a34a")
        self.summary_tree.tag_configure("failed", foreground="#ef4444")
        self.summary_tree.tag_configure("attention", foreground="#f59e0b")
        self.summary_tree.tag_configure("skipped", foreground="#6b7280")
        self.summary_tree.tag_configure("incomplete", foreground="#6b7280")
        self.summary_tree.tag_configure("stripe", background="#f3f4f6")

        logs_frame = ttk.Frame(summary_logs)
        logs_frame.columnconfigure(0, weight=1)
        logs_frame.rowconfigure(2, weight=1)
        summary_logs.add(logs_frame, weight=1)

        log_buttons = ttk.Frame(logs_frame)
        log_buttons.grid(row=0, column=0, sticky="ew", pady=(10, 6))
        ttk.Button(log_buttons, text="Clear logs", command=self.clear_logs).pack(side="left")
        ttk.Button(log_buttons, text="Save logs", command=self.save_logs).pack(side="left", padx=(8, 0))

        ttk.Label(logs_frame, text="Live logs:").grid(row=1, column=0, sticky="w", pady=(0, 6))
        self.log_output = scrolledtext.ScrolledText(logs_frame, height=14, state="disabled")
        self.log_output.grid(row=2, column=0, sticky="nsew")
        self.log_output.bind("<MouseWheel>", self._on_log_mousewheel)
        self.log_output.bind("<Button-4>", self._on_log_mousewheel)
        self.log_output.bind("<Button-5>", self._on_log_mousewheel)
        # Color only the level word: INFO green, WARNING yellow, ERROR red
        self.log_output.tag_configure("info", foreground="#16a34a")
        self.log_output.tag_configure("warning", foreground="#f59e0b")
        self.log_output.tag_configure("error", foreground="#ef4444")

        self._attach_log_handler()
        self._update_reel_caption_visibility()
        self.logger.info("UI ready. Loading profiles...")

    def open_dev_controls_window(self) -> None:
        settings_window = tk.Toplevel(self.root)
        settings_window.title("Dev controls")
        # No fixed geometry on purpose: this grid asks for ~600px and a hard-coded
        # 560x380 clipped the right-hand column clean off the window (that is what
        # made "Flow speed" unreachable). Letting Tk size the window to its content
        # also keeps it correct on macOS, where the font metrics differ.
        settings_window.minsize(560, 380)
        settings_window.transient(self.root)
        settings_window.grab_set()

        frame = ttk.Frame(settings_window, padding=12)
        frame.pack(fill="both", expand=True)
        frame.columnconfigure(1, weight=1)

        ttk.Label(frame, text="Bearer token:").grid(row=0, column=0, sticky="w", padx=(0, 8), pady=(0, 6))
        bearer_var = tk.StringVar(value=self._get_active_bearer_token())
        ttk.Entry(frame, textvariable=bearer_var, width=60).grid(row=0, column=1, sticky="ew", pady=(0, 6))

        ttk.Label(frame, text="Launch delay (batch):").grid(row=1, column=0, sticky="w", padx=(0, 8), pady=(0, 6))
        launch_delay_var = tk.StringVar(value=str(self._batch_launch_delay_seconds))
        ttk.Entry(frame, textvariable=launch_delay_var, width=12).grid(row=1, column=1, sticky="w", pady=(0, 6))

        ttk.Label(frame, text="Readiness wait (s):").grid(row=2, column=0, sticky="w", padx=(0, 8), pady=(0, 6))
        readiness_wait_var = tk.StringVar(value=str(self._readiness_wait_seconds))
        ttk.Entry(frame, textvariable=readiness_wait_var, width=12).grid(row=2, column=1, sticky="w", pady=(0, 6))

        ttk.Label(frame, text="Readiness attempts:").grid(row=3, column=0, sticky="w", padx=(0, 8), pady=(0, 6))
        readiness_attempts_var = tk.StringVar(value=str(self._readiness_max_attempts))
        ttk.Entry(frame, textvariable=readiness_attempts_var, width=12).grid(row=3, column=1, sticky="w", pady=(0, 6))

        ttk.Label(frame, text="Story media file/folder:").grid(row=4, column=0, sticky="w", padx=(0, 8), pady=(0, 6))
        story_path_var = tk.StringVar(value=self.story_media_path_var.get() if getattr(self, 'story_media_path_var', None) is not None else "")
        ttk.Entry(frame, textvariable=story_path_var, width=40).grid(row=4, column=1, sticky="ew", pady=(0, 6))
        ttk.Button(frame, text="Choose...", command=lambda: self._choose_story_media(story_path_var)).grid(row=4, column=2, sticky="w", padx=(8,0))

        # Own row, in the same two columns as every other field -- it used to sit
        # in columns 3/4 of the story-media row, i.e. off the right edge.
        ttk.Label(frame, text="Flow speed:").grid(row=5, column=0, sticky="w", padx=(0, 8), pady=(0, 6))
        flow_speed_var = tk.StringVar(value=get_saved_flow_speed())
        ttk.Combobox(frame, textvariable=flow_speed_var, width=10, state="readonly",
                     values=("fast", "normal", "slow")).grid(row=5, column=1, sticky="w", pady=(0, 6))
        self._dev_flow_speed_var = flow_speed_var

        ttk.Label(frame, text="Airtable token:").grid(row=6, column=0, sticky="w", padx=(0, 8), pady=(0, 6))
        airtable_token_var = tk.StringVar(value=self.airtable_token_var.get())
        ttk.Entry(frame, textvariable=airtable_token_var, width=60, show="*").grid(row=6, column=1, columnspan=2, sticky="ew", pady=(0, 6))

        ttk.Label(frame, text="Airtable base ID:").grid(row=7, column=0, sticky="w", padx=(0, 8), pady=(0, 6))
        airtable_base_var = tk.StringVar(value=self.airtable_base_id_var.get())
        ttk.Entry(frame, textvariable=airtable_base_var, width=40).grid(row=7, column=1, columnspan=2, sticky="ew", pady=(0, 6))

        ttk.Label(frame, text="Airtable table name:").grid(row=8, column=0, sticky="w", padx=(0, 8), pady=(0, 6))
        airtable_table_var = tk.StringVar(value=self.airtable_table_name_var.get())
        ttk.Entry(frame, textvariable=airtable_table_var, width=40).grid(row=8, column=1, columnspan=2, sticky="ew", pady=(0, 6))

        buttons = ttk.Frame(frame)
        buttons.grid(row=9, column=0, columnspan=3, sticky="e", pady=(12, 0))
        ttk.Button(buttons, text="Apply", command=lambda: self._apply_dev_controls(
            bearer_var.get(),
            launch_delay_var.get(),
            readiness_wait_var.get(),
            readiness_attempts_var.get(),
            story_path_var.get(),
            settings_window,
            airtable_token_var.get(),
            airtable_base_var.get(),
            airtable_table_var.get(),
        )).pack(side="left")
        ttk.Button(buttons, text="Cancel", command=settings_window.destroy).pack(side="left", padx=(8, 0))

    # ------------------------------------------------------------------
    # Pipeline window: where raw videos come from (local folder or Google
    # Drive), where spoofed variants are written, and which spoofer to run.
    # Saved to settings so the scheduled loops pick them up.
    # ------------------------------------------------------------------
    _PIPELINE_FIELDS = (
        # (settings key, label, chooser: 'dir' | 'file' | None)
        ("raw_videos_dir", "Raw videos folder (local):", "dir"),
        ("spoofed_videos_dir", "Spoofed output folder:", "dir"),
        ("drive_raw_folder_id", "Google Drive folder id:", None),
        ("google_service_account_json", "Drive service-account JSON:", "file"),
        ("spoofer_python", "Spoofer interpreter:", "file"),
        ("spoofer_root", "Spoofer project folder:", "dir"),
    )

    def open_pipeline_window(self) -> None:
        window = tk.Toplevel(self.root)
        window.title("Pipeline & storage")
        window.geometry("720x330")
        window.transient(self.root)
        window.grab_set()

        frame = ttk.Frame(window, padding=12)
        frame.pack(fill="both", expand=True)
        frame.columnconfigure(1, weight=1)

        ttk.Label(frame, text="Where the spoofing pipeline reads raw videos and writes variants. "
                              "Drive is used when a folder id + service-account key are both set; "
                              "otherwise the local raw folder is used.",
                  wraplength=690).grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 10))

        saved = load_settings()
        self._pipeline_vars = {}
        for i, (key, label, chooser) in enumerate(self._PIPELINE_FIELDS, start=1):
            ttk.Label(frame, text=label).grid(row=i, column=0, sticky="w", padx=(0, 8), pady=3)
            var = tk.StringVar(value=str(saved.get(key, "") or ""))
            ttk.Entry(frame, textvariable=var, width=52).grid(row=i, column=1, sticky="ew", pady=3)
            if chooser:
                ttk.Button(frame, text="Choose...",
                           command=lambda v=var, c=chooser: self._choose_path(v, c)
                           ).grid(row=i, column=2, sticky="w", padx=(8, 0))
            self._pipeline_vars[key] = var

        buttons = ttk.Frame(frame)
        buttons.grid(row=20, column=0, columnspan=3, sticky="e", pady=(14, 0))
        ttk.Button(buttons, text="Save", command=lambda: self._save_pipeline_settings(window)).pack(side="left")
        ttk.Button(buttons, text="Check", command=self._check_pipeline_settings).pack(side="left", padx=(8, 0))
        ttk.Button(buttons, text="Cancel", command=window.destroy).pack(side="left", padx=(8, 0))

    def _choose_path(self, var: tk.StringVar, kind: str) -> None:
        chosen = filedialog.askdirectory() if kind == "dir" else filedialog.askopenfilename()
        if chosen:
            var.set(str(Path(chosen)))

    def _save_pipeline_settings(self, window: tk.Toplevel | None = None) -> None:
        settings = load_settings()
        for key, var in getattr(self, "_pipeline_vars", {}).items():
            settings[key] = var.get().strip()
        if save_settings(settings):
            self.logger.info("Pipeline settings saved")
        else:
            self.logger.warning("Pipeline settings updated but failed to save")
        if window is not None:
            window.destroy()

    def _check_pipeline_settings(self) -> None:
        """Run the pipeline-related preflight checks against what's typed in,
        so problems are caught here instead of mid-run on the server."""
        from adb_bot.automation import doctor

        values = {key: var.get().strip() for key, var in getattr(self, "_pipeline_vars", {}).items()}
        results = doctor.check_paths(values.get("raw_videos_dir", ""),
                                     values.get("spoofed_videos_dir", ""),
                                     values.get("drive_raw_folder_id", ""))
        results.append(doctor.check_drive(values.get("google_service_account_json", ""),
                                          values.get("drive_raw_folder_id", "")))
        results.append(doctor.check_spoofer(values.get("spoofer_python", ""),
                                            values.get("spoofer_root", "")))
        results.append(doctor.check_ffmpeg())
        report = doctor.format_report(results)
        for line in report.splitlines():
            self.logger.info("%s", line)
        messagebox.showinfo("Pipeline check", report)

    # ------------------------------------------------------------------
    # Scheduler window: turn each background loop on/off and set its interval,
    # backed by Windows Task Scheduler (adb_bot.automation.scheduler_admin).
    # ------------------------------------------------------------------
    _SCHEDULER_LOOP_LABELS = {
        "posting": "Posting (main engine)",
        "warmup": "Warmup (Day 1–4)",
        "pipeline": "Spoofing pipeline",
        "mlx-sync": "MultiLogin → Airtable sync",
        "cleanup": "Cleanup (old used media)",
    }

    def open_scheduler_window(self) -> None:
        window = tk.Toplevel(self.root)
        window.title("Scheduler")
        window.geometry("640x360")
        window.transient(self.root)
        window.grab_set()

        frame = ttk.Frame(window, padding=12)
        frame.pack(fill="both", expand=True)
        frame.columnconfigure(0, weight=1)

        if not scheduler_admin.is_supported():
            ttk.Label(frame, text=scheduler_admin.unavailable_reason(),
                      wraplength=560).grid(row=0, column=0, sticky="w")
            ttk.Button(frame, text="Close", command=window.destroy).grid(row=1, column=0, sticky="e", pady=(12, 0))
            return

        backend = scheduler_admin.backend_name()
        unit_word = "Scheduled Task (ADBBot-*)" if backend == scheduler_admin.WINDOWS else "systemd timer (adbbot-*)"
        ttk.Label(frame, text=f"Each loop is a {unit_word}, via {backend}. "
                              "Apply installs/updates enabled loops and removes disabled ones.",
                  wraplength=600).grid(row=0, column=0, columnspan=5, sticky="w", pady=(0, 10))

        header = ttk.Frame(frame)
        header.grid(row=1, column=0, columnspan=5, sticky="ew")
        for col, (text, w) in enumerate((("On", 4), ("Loop", 26), ("Every (min)", 12), ("Status", 14), ("", 8))):
            ttk.Label(header, text=text, width=w).grid(row=0, column=col, sticky="w", padx=4)

        saved = get_scheduler_config()
        self._scheduler_rows = {}
        for i, loop in enumerate(scheduler_admin.LOOPS, start=2):
            cfg = saved.get(loop, {})
            enabled_var = tk.BooleanVar(value=bool(cfg.get("enabled", False)))
            interval_var = tk.StringVar(value=str(cfg.get("interval_min", scheduler_admin.DEFAULT_INTERVALS[loop])))
            status_var = tk.StringVar(value=self._scheduler_status_text(loop))

            row = ttk.Frame(frame)
            row.grid(row=i, column=0, columnspan=5, sticky="ew", pady=2)
            ttk.Checkbutton(row, variable=enabled_var).grid(row=0, column=0, sticky="w", padx=4)
            ttk.Label(row, text=self._SCHEDULER_LOOP_LABELS.get(loop, loop), width=26).grid(row=0, column=1, sticky="w", padx=4)
            ttk.Entry(row, textvariable=interval_var, width=10).grid(row=0, column=2, sticky="w", padx=4)
            ttk.Label(row, textvariable=status_var, width=14).grid(row=0, column=3, sticky="w", padx=4)
            ttk.Button(row, text="Run now", command=lambda l=loop: self._scheduler_run_now(l)).grid(row=0, column=4, sticky="w", padx=4)
            self._scheduler_rows[loop] = {"enabled": enabled_var, "interval": interval_var, "status": status_var}

        dry_run_var = tk.BooleanVar(value=bool(saved.get("dry_run", False)))
        ttk.Checkbutton(frame, text="Install loops in dry-run mode (plan only, no device/writes)",
                        variable=dry_run_var).grid(row=20, column=0, columnspan=5, sticky="w", pady=(10, 0))
        self._scheduler_dry_run_var = dry_run_var

        logged_off_var = tk.BooleanVar(value=bool(saved.get("run_when_logged_off", False)))
        if backend == scheduler_admin.WINDOWS:
            logged_off_text = "Run even when nobody is logged on (as SYSTEM — needs the app started as administrator)"
            logged_off_state = "normal"
        else:
            # systemd system units always run with nobody logged in, so there is
            # nothing to toggle -- say so rather than showing a dead checkbox.
            logged_off_text = "Runs when nobody is logged on (always true for systemd system units)"
            logged_off_var.set(True)
            logged_off_state = "disabled"
        ttk.Checkbutton(frame, text=logged_off_text, variable=logged_off_var,
                        state=logged_off_state).grid(row=21, column=0, columnspan=5, sticky="w")
        self._scheduler_logged_off_var = logged_off_var

        buttons = ttk.Frame(frame)
        buttons.grid(row=22, column=0, columnspan=5, sticky="e", pady=(12, 0))
        ttk.Button(buttons, text="Apply", command=self._apply_scheduler).pack(side="left")
        ttk.Button(buttons, text="Refresh", command=self._refresh_scheduler_status).pack(side="left", padx=(8, 0))
        ttk.Button(buttons, text="Close", command=window.destroy).pack(side="left", padx=(8, 0))

    def _scheduler_status_text(self, loop: str) -> str:
        state = scheduler_admin.query_state(loop)
        return "not installed" if state is None else str(state)

    def _refresh_scheduler_status(self) -> None:
        for loop, row in getattr(self, "_scheduler_rows", {}).items():
            row["status"].set(self._scheduler_status_text(loop))

    def _scheduler_run_now(self, loop: str) -> None:
        ok, msg = scheduler_admin.run_now(loop)
        if ok:
            self.logger.info("Triggered scheduled loop '%s' now", loop)
        else:
            self.logger.warning("Could not run loop '%s': %s", loop, msg)
            messagebox.showwarning("Scheduler", f"Could not run '{loop}':\n{msg}\n\nInstall it first with Apply.")

    def _apply_scheduler(self) -> None:
        rows = getattr(self, "_scheduler_rows", {})
        dry_run = bool(getattr(self, "_scheduler_dry_run_var", tk.BooleanVar()).get())
        logged_off = bool(getattr(self, "_scheduler_logged_off_var", tk.BooleanVar()).get())
        config: dict = {"dry_run": dry_run, "run_when_logged_off": logged_off}
        results = []
        for loop, row in rows.items():
            enabled = bool(row["enabled"].get())
            try:
                interval = max(1, int(row["interval"].get()))
            except (ValueError, TypeError):
                interval = scheduler_admin.DEFAULT_INTERVALS[loop]
                row["interval"].set(str(interval))
            config[loop] = {"enabled": enabled, "interval_min": interval}

            if enabled:
                ok, msg = scheduler_admin.install(loop, interval, apply=not dry_run,
                                                  run_when_logged_off=logged_off)
                results.append(f"{loop}: {'installed' if ok else 'FAILED: ' + msg}")
            else:
                ok, msg = scheduler_admin.remove(loop)
                results.append(f"{loop}: {'removed' if ok else 'remove failed: ' + msg}")

        save_scheduler_config(config)
        self._refresh_scheduler_status()
        self.logger.info("Scheduler applied (%s): %s", "dry-run" if dry_run else "apply", "; ".join(results))
        messagebox.showinfo("Scheduler", "\n".join(results))

    def _apply_dev_controls(self, bearer_token: str, launch_delay: str, readiness_wait: str, readiness_attempts: str, story_media_path: str, window: tk.Toplevel, airtable_token: str = "", airtable_base_id: str = "", airtable_table_name: str = "") -> None:
        self.bearer_token_var = getattr(self, "bearer_token_var", None)
        if self.bearer_token_var is None:
            self.bearer_token_var = tk.StringVar(value=get_bearer_token())
        self.bearer_token_var.set(bearer_token.strip())

        self.batch_launch_delay_var = getattr(self, "batch_launch_delay_var", None)
        if self.batch_launch_delay_var is None:
            self.batch_launch_delay_var = tk.StringVar(value=str(self._batch_launch_delay_seconds))
        self.batch_launch_delay_var.set(launch_delay.strip())

        self.readiness_wait_var = getattr(self, "readiness_wait_var", None)
        if self.readiness_wait_var is None:
            self.readiness_wait_var = tk.StringVar(value=str(self._readiness_wait_seconds))
        self.readiness_wait_var.set(readiness_wait.strip())

        self.readiness_attempts_var = getattr(self, "readiness_attempts_var", None)
        if self.readiness_attempts_var is None:
            self.readiness_attempts_var = tk.StringVar(value=str(self._readiness_max_attempts))
        self.readiness_attempts_var.set(readiness_attempts.strip())

        self._batch_launch_delay_seconds = self._get_launch_delay_seconds()
        self._readiness_wait_seconds = self._get_readiness_wait_seconds()
        self._readiness_max_attempts = self._get_readiness_max_attempts()
        # Persist story media path
        self.story_media_path_var = getattr(self, "story_media_path_var", None)
        if self.story_media_path_var is None:
            self.story_media_path_var = tk.StringVar(value="")
        # Normalize and expand user home (~) for cross-platform compatibility (mac, linux, windows)
        normalized_story_path = (story_media_path or "").strip()
        if normalized_story_path:
            try:
                normalized_story_path = str(Path(normalized_story_path).expanduser())
            except Exception:
                pass
        self.story_media_path_var.set(normalized_story_path)

        # Persist Airtable config.
        self.airtable_token_var.set(airtable_token.strip())
        self.airtable_base_id_var.set(airtable_base_id.strip())
        self.airtable_table_name_var.set(airtable_table_name.strip() or "Profiles")

        # Merge into the saved settings rather than replacing them -- the file
        # also holds the Pipeline and Scheduler config, which a blind overwrite
        # would silently wipe.
        settings = load_settings()
        settings.update({
            "bearer_token": bearer_token.strip(),
            "batch_launch_delay_seconds": self._batch_launch_delay_seconds,
            "readiness_wait_seconds": self._readiness_wait_seconds,
            "readiness_max_attempts": self._readiness_max_attempts,
            "story_media_path": self.story_media_path_var.get(),
            "shutdown_on_success": self._shutdown_on_success,
            "airtable_token": self.airtable_token_var.get(),
            "airtable_base_id": self.airtable_base_id_var.get(),
            "airtable_table_name": self.airtable_table_name_var.get(),
        })
        flow_speed_var = getattr(self, "_dev_flow_speed_var", None)
        if flow_speed_var is not None:
            settings["flow_speed"] = (flow_speed_var.get() or "normal").strip().lower()
        if save_settings(settings):
            self.logger.info("Dev controls updated and saved")
        else:
            self.logger.warning("Dev controls updated but failed to save")
        window.destroy()

    def _choose_story_media(self, var: tk.StringVar) -> None:
        selected_path = filedialog.askdirectory(title="Select story media folder")
        if selected_path:
            try:
                var.set(str(Path(selected_path).expanduser()))
            except Exception:
                var.set(selected_path)
            return

        file_path = filedialog.askopenfilename(
            title="Select story media file",
            filetypes=[
                ("Image files", ("*.png", "*.jpg", "*.jpeg", "*.webp")),
                ("Video files", ("*.mp4", "*.mov", "*.mkv", "*.webm")),
                ("All files", ("*.*",)),
            ],
        )
        if file_path:
            try:
                var.set(str(Path(file_path).expanduser()))
            except Exception:
                var.set(file_path)

    def _attach_log_handler(self) -> None:
        if any(isinstance(handler, TextLogHandler) for handler in self.logger.handlers):
            return
        handler = TextLogHandler(self.log_output)
        handler.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | %(message)s"))
        self.logger.addHandler(handler)

    def load_profiles(self) -> None:
        self.clear_profile_options()
        self.logger.info("Fetching profiles from Multilogin...")
        try:
            bearer_token = self._get_active_bearer_token()
            mobile_client = MultiloginMobileListClient(bearer_token)
            folder_client = MultiloginFolderClient(bearer_token)
            items = mobile_client.list_mobile_profiles()
            folders = folder_client.list_mobile_folders()
            groups = build_foldered_profile_groups(items, folders)
            if not groups:
                self.logger.warning("No profiles were returned by the Multilogin API.")
                return
            for group in groups:
                self._add_folder_group(group)
            self.logger.info("Loaded %s profile(s) across %s folder(s)", sum(len(group["profiles"]) for group in groups), len(groups))
        except requests.exceptions.HTTPError as exc:
            status_code = exc.response.status_code if exc.response is not None else None
            if status_code == 401:
                self.logger.error("Unable to load profiles: HTTP 401 Unauthorized")
                messagebox.showerror(
                    "Multilogin Authentication Failed",
                    "Multilogin returned 401 Unauthorized. Check your bearer token in Dev controls and try again.",
                )
            else:
                self.logger.exception("Unable to load profiles: %s", exc)
        except Exception as exc:  # pragma: no cover - UI protection path
            self.logger.exception("Unable to load profiles: %s", exc)

    def clear_profile_options(self) -> None:
        for widget in self.profile_container.winfo_children():
            widget.destroy()
        self.profile_vars.clear()
        self.profile_labels.clear()
        self.profile_status_vars.clear()
        self.profile_last_status.clear()
        self.folder_sections.clear()
        self.folder_toggles.clear()
        self.folder_expanded.clear()
        self.folder_names.clear()
        # Only the widget-bound vars go: the folder -> media map is persisted
        # config, so a Refresh must not wipe the mappings already made.
        self.folder_media_vars.clear()
        self.profile_to_folder.clear()
        self.profile_rows.clear()
        if self.profile_search_var is not None:
            self.profile_search_var.set("")
        self.profile_canvas.yview_moveto(0.0)

    def _on_profile_container_configure(self, event: tk.Event) -> None:
        self.profile_canvas.configure(scrollregion=self.profile_canvas.bbox("all"))

    def _on_profile_canvas_configure(self, event: tk.Event) -> None:
        if self.profile_container_id is not None:
            self.profile_canvas.itemconfigure(self.profile_container_id, width=event.width)

    def _is_profile_widget(self, widget: tk.Misc | None) -> bool:
        while widget is not None:
            if widget is self.profile_container or widget is self.profile_canvas:
                return True
            try:
                widget = widget.nametowidget(widget.winfo_parent()) if widget.winfo_parent() else None
            except tk.TclError:
                return False
        return False

    def _on_profile_mousewheel(self, event: tk.Event) -> str:
        if not self._is_profile_widget(event.widget if hasattr(event, "widget") else None):
            return ""

        delta = 0
        if getattr(event, "num", None) == 4:
            delta = -1
        elif getattr(event, "num", None) == 5:
            delta = 1
        elif hasattr(event, "delta"):
            delta = -int(event.delta / 120)
        if delta:
            self.profile_canvas.yview_scroll(delta, "units")
            return "break"
        return ""

    def _on_log_mousewheel(self, event: tk.Event) -> str:
        delta = 0
        if getattr(event, "num", None) == 4:
            delta = -1
        elif getattr(event, "num", None) == 5:
            delta = 1
        elif hasattr(event, "delta"):
            delta = -int(event.delta / 120)
        if delta:
            self.log_output.yview_scroll(delta, "units")
        return "break"

    def _rebuild_profile_rows_for_search(self, search_query: str) -> None:
        folder_has_visible_profiles: dict[str, bool] = {}
        for folder_key in self.folder_sections.keys():
            folder_has_visible_profiles[folder_key] = False

        sorted_profile_ids = sorted(
            self.profile_rows.keys(),
            key=lambda profile_id: (
                self.profile_labels.get(profile_id, "").lower(),
                str(profile_id).lower(),
            ),
        )

        visible_profile_ids: list[str] = []
        for profile_id in sorted_profile_ids:
            row = self.profile_rows.get(profile_id)
            if row is None:
                continue

            profile_name = self.profile_labels.get(profile_id, "")
            matches = search_query in profile_name.lower() or search_query in str(profile_id).lower()

            if not search_query or matches:
                visible_profile_ids.append(profile_id)
                folder_key = self.profile_to_folder.get(profile_id)
                if folder_key is not None:
                    folder_has_visible_profiles[folder_key] = True

        for row in self.profile_rows.values():
            try:
                row.pack_forget()
            except Exception:
                continue

        for profile_id in visible_profile_ids:
            row = self.profile_rows.get(profile_id)
            if row is not None:
                row.pack(fill="x", pady=1)

        for folder_key, folder_section in self.folder_sections.items():
            has_visible = folder_has_visible_profiles.get(folder_key, False)
            if search_query:
                if has_visible:
                    folder_section.pack(fill="x", padx=(0, 4), pady=(4, 0))
                else:
                    folder_section.pack_forget()
            else:
                folder_section.pack(fill="x", padx=(0, 4), pady=(4, 0))

        if getattr(self, "profile_container", None) is not None:
            try:
                self.profile_container.update_idletasks()
                self.profile_canvas.update_idletasks()
            except Exception:
                pass

    def _on_profile_search_changed(self, *args) -> None:
        """Filter profiles based on search query (name or ID)."""
        if self.profile_search_var is None:
            return

        search_query = self.profile_search_var.get().strip().lower()
        self._rebuild_profile_rows_for_search(search_query)

    def _add_folder_group(self, group: dict[str, Any]) -> None:
        folder_id = str(group.get("folder_id") or "")
        folder_name = str(group.get("name") or "Unassigned")
        folder_key = folder_id or folder_name
        section = ttk.Frame(self.profile_container)
        section.pack(fill="x", padx=(0, 4), pady=(4, 0))
        self.folder_sections[folder_key] = section
        self.folder_expanded[folder_key] = True
        self.folder_names[folder_key] = folder_name

        # Folder title + its media mapping share one bar, which lives outside the
        # collapsible part on purpose: `toggle_folder` hides children[1:], so the
        # mapping stays readable even with the folder collapsed.
        title_bar = ttk.Frame(section)
        title_bar.pack(fill="x", pady=(0, 4))
        toggle = ttk.Button(title_bar, text=f"▾ {folder_name}", command=lambda key=folder_key: self.toggle_folder(key))
        toggle.pack(side="left")
        self.folder_toggles[folder_key] = toggle

        media_var = tk.StringVar(value=self._folder_media_label(self.folder_media_paths.get(folder_key, "")))
        self.folder_media_vars[folder_key] = media_var
        ttk.Button(title_bar, text="Clear", width=6, command=lambda key=folder_key: self._clear_folder_media(key)).pack(side="right", padx=(6, 0))
        ttk.Button(title_bar, text="📁 Reels media", command=lambda key=folder_key, name=folder_name: self._choose_folder_media(key, name)).pack(side="right", padx=(6, 0))
        ttk.Label(title_bar, textvariable=media_var, anchor="e", foreground="#6b7280").pack(side="right", padx=(8, 0))

        header = ttk.Frame(section)
        header.pack(fill="x", pady=(0, 4))
        header.columnconfigure(0, weight=0)
        header.columnconfigure(1, weight=1)
        header.columnconfigure(2, weight=0, minsize=70)
        header.columnconfigure(3, weight=0, minsize=110)
        header.columnconfigure(4, weight=0, minsize=100)
        ttk.Label(header, text="", anchor="w").grid(row=0, column=0, sticky="w")
        ttk.Label(header, text="Profile", anchor="w").grid(row=0, column=1, sticky="w")
        ttk.Label(header, text="Step", anchor="e", width=16).grid(row=0, column=2, sticky="e", padx=(0, 8))
        ttk.Label(header, text="Status", anchor="e", width=12).grid(row=0, column=3, sticky="e", padx=(0, 8))
        ttk.Label(header, text="Action", anchor="e").grid(row=0, column=4, sticky="e")

        body = ttk.Frame(section)
        body.pack(fill="x")
        body.columnconfigure(0, weight=0)
        body.columnconfigure(1, weight=1)
        body.columnconfigure(2, weight=0)
        body.columnconfigure(3, weight=0)
        body.columnconfigure(4, weight=0)
        profiles = list(group.get("profiles", []))
        profiles.sort(
            key=lambda profile: (
                str(profile.get("name") if isinstance(profile, dict) else getattr(profile, "name", "") or "").lower(),
                str(profile.get("id") if isinstance(profile, dict) else getattr(profile, "id", "") or "").lower(),
            )
        )
        for profile in profiles:
            profile_id = str(profile.get("id") if isinstance(profile, dict) else getattr(profile, "id", ""))
            profile_name = str(profile.get("name") if isinstance(profile, dict) else getattr(profile, "name", ""))
            self.profile_labels[profile_id] = profile_name
            var = tk.BooleanVar(value=False)
            self.profile_vars[profile_id] = var
            status_var = tk.StringVar(value="")
            self.profile_status_vars[profile_id] = status_var
            self.profile_manual_continue_events[profile_id] = threading.Event()

            row = ttk.Frame(body)
            row.pack(fill="x", pady=1)
            row.columnconfigure(0, weight=0)
            row.columnconfigure(1, weight=1)
            row.columnconfigure(2, weight=0, minsize=110)
            row.columnconfigure(3, weight=0, minsize=205)
            self.profile_rows[profile_id] = row
            self.profile_to_folder[profile_id] = folder_key

            checkbox = ttk.Checkbutton(row, variable=var)
            checkbox.grid(row=0, column=0, sticky="w")
            ttk.Label(
                row,
                text=f"{profile_name} ({profile_id})",
                wraplength=520,
                justify="left",
                anchor="w",
            ).grid(row=0, column=1, sticky="w")
            step_var = tk.StringVar(value="0/0")
            self.profile_progress_vars[profile_id] = step_var
            ttk.Label(row, textvariable=step_var, width=10, anchor="center").grid(row=0, column=2, sticky="ew", padx=(0, 8))

            ttk.Label(row, textvariable=status_var, width=28, anchor="e").grid(row=0, column=3, sticky="e", padx=(0, 8))
            continue_button = ttk.Button(
                row,
                text="Continue",
                command=lambda pid=profile_id: self._manual_continue(pid),
                state="disabled",
                width=10,
            )
            continue_button.grid(row=0, column=4, sticky="e")
            self.profile_manual_continue_buttons[profile_id] = continue_button

    def _folder_media_label(self, media_path: str, max_chars: int = 42) -> str:
        """Short, right-aligned rendering of a folder's mapped media path."""
        media_path = (media_path or "").strip()
        if not media_path:
            return "reels media: not set"
        display = str(Path(media_path))
        if len(display) <= max_chars:
            return display
        parts = Path(media_path).parts
        tail = str(Path(*parts[-2:])) if len(parts) >= 2 else display
        return f"…{tail}" if len(tail) <= max_chars else f"…{tail[-max_chars:]}"

    def _choose_folder_media(self, folder_key: str, folder_name: str) -> None:
        """Map a local media folder to one Multilogin folder, saved right away so
        the choice survives restarts and only has to be made once."""
        selected_path = filedialog.askdirectory(title=f"Select reels media folder for '{folder_name}'")
        if not selected_path:
            return
        try:
            normalized = str(Path(selected_path).expanduser())
        except Exception:
            normalized = selected_path

        clash = next(
            (
                self.folder_names.get(key, key)
                for key, path in self.folder_media_paths.items()
                if key != folder_key and self._same_media_folder(path, normalized)
            ),
            None,
        )
        if clash is not None and not messagebox.askokcancel(
            "Folder already in use",
            f"'{clash}' is already using this media folder.\n\n{normalized}\n\n"
            f"Both folders would draw from the same queue, so their profiles would "
            f"share (and consume) the same clips.\n\nMap it to '{folder_name}' anyway?",
        ):
            return

        pending = discover_story_media_files(normalized)
        if not pending:
            messagebox.showwarning(
                "No media found",
                f"No supported media files were found in:\n{normalized}\n\n"
                "The folder is saved anyway -- drop clips in before running, or the "
                "run will stop with 'no pending reel media'.",
            )

        self.folder_media_paths[folder_key] = normalized
        var = self.folder_media_vars.get(folder_key)
        if var is not None:
            var.set(self._folder_media_label(normalized))
        if save_folder_media_paths(self.folder_media_paths):
            self.logger.info(
                "Mapped reels media folder for '%s': %s (%s file(s) pending)",
                folder_name, normalized, len(pending),
            )
        else:
            self.logger.warning("Mapped reels media folder for '%s' but failed to save it", folder_name)

    def _clear_folder_media(self, folder_key: str) -> None:
        if folder_key not in self.folder_media_paths:
            return
        removed = self.folder_media_paths.pop(folder_key)
        var = self.folder_media_vars.get(folder_key)
        if var is not None:
            var.set(self._folder_media_label(""))
        save_folder_media_paths(self.folder_media_paths)
        self.logger.info(
            "Cleared reels media folder for '%s' (was %s); its profiles fall back to the global media setting",
            self.folder_names.get(folder_key, folder_key), removed,
        )

    @staticmethod
    def _same_media_folder(left: str, right: str) -> bool:
        try:
            return Path(left).expanduser().resolve() == Path(right).expanduser().resolve()
        except Exception:
            return str(left).strip() == str(right).strip()

    def toggle_folder(self, folder_key: str) -> None:
        section = self.folder_sections.get(folder_key)
        if not section:
            return
        button = self.folder_toggles.get(folder_key)
        if not button:
            return

        if self.folder_expanded.get(folder_key, True):
            self.folder_expanded[folder_key] = False
            for child in section.winfo_children()[1:]:
                child.pack_forget()
            current_text = button.cget("text")
            if current_text.startswith("▾"):
                button.config(text="▸ " + current_text[2:])
        else:
            self.folder_expanded[folder_key] = True
            for child in section.winfo_children()[1:]:
                child.pack(fill="x")
            current_text = button.cget("text")
            if current_text.startswith("▸"):
                button.config(text="▾ " + current_text[2:])

    def _manual_continue(self, profile_id: str) -> None:
        event = self.profile_manual_continue_events.get(profile_id)
        button = self.profile_manual_continue_buttons.get(profile_id)
        if event is None:
            return

        self.logger.info("Manual continue requested for profile %s", profile_id)
        event.set()
        if button is not None:
            button.config(state="disabled")

    def _enable_manual_continue_button(self, profile_id: str) -> None:
        def _apply() -> None:
            button = self.profile_manual_continue_buttons.get(profile_id)
            event = self.profile_manual_continue_events.get(profile_id)
            if event is not None:
                event.clear()
            if button is not None:
                button.config(state="normal")

        if getattr(self, "root", None) is not None and self.root.winfo_exists():
            self.root.after(0, _apply)
        else:
            _apply()

    def _disable_manual_continue_button(self, profile_id: str) -> None:
        # Must be marshaled onto the Tk main thread: this is called from the
        # per-profile worker threads, and touching a widget off-thread is what
        # made the Continue button behave erratically.
        def _apply() -> None:
            button = self.profile_manual_continue_buttons.get(profile_id)
            if button is not None:
                button.config(state="disabled")

        if getattr(self, "root", None) is not None and self.root.winfo_exists():
            self.root.after(0, _apply)
        else:
            _apply()

    _STATUS_DISPLAY = {
        "starting": "starting",
        "connecting": "connecting",
        "running": "running",
        "manual_continue": "manual continue",
        "failed": "failed",
        "adb_connect_failed": "failed to connect ADB",
        "already_had_bio": "already had a bio",
        "human_verification": "human verification requested",
        "banned": "banned / suspended",
        "action_block": "action blocked (temporary)",
        # Share was tapped but the post could not be proven. Deliberately worded
        # as "may have posted": the wrong move here is to re-post it, which puts
        # the same reel on the account twice.
        "uncertain": "uncertain — may have posted, check first",
        "done": "done",
    }

    def _normalize_profile_status(self, status: str | None) -> str:
        normalized = (status or "").strip().lower()
        if normalized in self._STATUS_DISPLAY:
            return normalized
        return ""

    def _set_profile_status(self, profile_id: str, status: str | None, detail: str = "") -> None:
        normalized_status = self._normalize_profile_status(status)
        display_status = self._STATUS_DISPLAY.get(normalized_status, normalized_status)

        # How the flow decided, e.g. "via post_count [strong]" or
        # "via timeout: no positive signal". Logged rather than squeezed into the
        # status column, which has to stay short.
        if detail and normalized_status in ("done", "uncertain", "failed"):
            self.logger.info("Profile %s -> %s (%s)",
                             self.profile_labels.get(profile_id, profile_id),
                             display_status, detail)

        def _apply() -> None:
            if normalized_status:
                self.profile_last_status[profile_id] = normalized_status
            status_var = self.profile_status_vars.get(profile_id)
            if status_var is None:
                status_var = tk.StringVar(value=display_status)
                self.profile_status_vars[profile_id] = status_var
            else:
                status_var.set(display_status)

        if getattr(self, "root", None) is not None and self.root.winfo_exists():
            self.root.after(0, _apply)
        else:
            _apply()

    def _set_profile_progress(self, profile_id: str, completed: int | float, total: int | None = None) -> None:
        class _FallbackStringVar:
            def __init__(self, value: str = "") -> None:
                self._value = value

            def get(self) -> str:
                return self._value

            def set(self, value: str) -> None:
                self._value = str(value)

        def _apply() -> None:
            step_var = self.profile_progress_vars.get(profile_id)
            if step_var is None:
                if getattr(self, "root", None) is not None and self.root.winfo_exists():
                    step_var = tk.StringVar(value="0/0")
                else:
                    step_var = _FallbackStringVar("0/0")
                self.profile_progress_vars[profile_id] = step_var
            try:
                completed_value = int(float(completed))
                total_value = int(total) if total is not None else 0
                if total_value > 0:
                    step_var.set(f"{completed_value}/{total_value}")
                else:
                    step_var.set(str(completed_value))
            except Exception:
                step_var.set("0/0")

        if getattr(self, "root", None) is not None and self.root.winfo_exists():
            self.root.after(0, _apply)
        else:
            _apply()

    def _get_active_bearer_token(self) -> str:
        if hasattr(self, "bearer_token_var") and self.bearer_token_var is not None:
            return (self.bearer_token_var.get() or "").strip() or get_bearer_token()
        return get_bearer_token()

    def _get_launch_delay_seconds(self) -> int:
        if hasattr(self, "batch_launch_delay_var") and self.batch_launch_delay_var is not None:
            try:
                return max(0, int((self.batch_launch_delay_var.get() or "0").strip()))
            except ValueError:
                pass
        return self._batch_launch_delay_seconds

    def _get_readiness_wait_seconds(self) -> int:
        if hasattr(self, "readiness_wait_var") and self.readiness_wait_var is not None:
            try:
                return max(0, int((self.readiness_wait_var.get() or "0").strip()))
            except ValueError:
                pass
        return self._readiness_wait_seconds

    def _get_readiness_max_attempts(self) -> int:
        if hasattr(self, "readiness_attempts_var") and self.readiness_attempts_var is not None:
            try:
                return max(1, int((self.readiness_attempts_var.get() or "1").strip()))
            except ValueError:
                pass
        return self._readiness_max_attempts

    def _media_path_for_profile(self, profile_id: str) -> str | None:
        """The media folder this profile is allowed to use: only the one mapped
        to its own Multilogin folder, or None to keep the old global behaviour."""
        return resolve_folder_media_path(profile_id, self.profile_to_folder, self._run_folder_media)

    def _confirm_folder_media(self, selected_ids: list[str]) -> bool:
        """Before a reels run, flag selected folders that have no media mapping.

        Only asks once at least one folder is mapped -- with no mappings at all
        the run behaves exactly as it did before this feature existed, so nothing
        new gets in the way."""
        if not self.folder_media_paths:
            return True
        unmapped = folders_missing_media(
            selected_ids, self.profile_to_folder, self.folder_media_paths, self.folder_names
        )
        if not unmapped:
            return True
        listed = "\n".join(f"  • {name}" for name in unmapped[:10])
        if len(unmapped) > 10:
            listed += f"\n  • ... and {len(unmapped) - 10} more"
        return bool(messagebox.askokcancel(
            "Folders without a media folder",
            f"These selected folders have no reels media folder mapped:\n\n{listed}\n\n"
            "Their profiles will fall back to the global story media setting in Dev "
            "controls (and fail if that is unset).\n\nRun anyway?",
        ))

    def _log_folder_media_plan(self, selected_ids: list[str]) -> None:
        """Write out which folder each profile draws media from, so a wrong
        mapping is visible in the log before anything is pushed to a phone."""
        for folder_key in dict.fromkeys(self.profile_to_folder.get(pid, "") for pid in selected_ids):
            folder_name = self.folder_names.get(folder_key, folder_key or "Unassigned")
            media_path = (self._run_folder_media or {}).get(folder_key, "")
            count = sum(1 for pid in selected_ids if self.profile_to_folder.get(pid, "") == folder_key)
            if media_path and not Path(media_path).is_dir():
                self.logger.warning(
                    "Reels media folder for '%s' (%s profile(s)) is missing: %s -- those profiles will fail; re-map it",
                    folder_name, count, media_path,
                )
            elif media_path:
                pending = len(discover_story_media_files(media_path))
                log = self.logger.info if pending >= count else self.logger.warning
                log(
                    "Reels media for folder '%s' (%s profile(s)): %s [%s file(s) pending]",
                    folder_name, count, media_path, pending,
                )
            else:
                self.logger.warning(
                    "Folder '%s' (%s profile(s)) has no reels media folder; falling back to the global media setting",
                    folder_name, count,
                )

    def run_selected(self) -> None:
        if self._run_in_progress:
            messagebox.showinfo("Run in progress", "A workflow run is already in progress.")
            return

        selected_ids = [profile_id for profile_id, var in self.profile_vars.items() if var.get()]
        if not selected_ids:
            messagebox.showinfo("Nothing selected", "Please select at least one profile first.")
            return

        flow_name = self._get_selected_flow_value()
        if flow_name in REEL_FLOWS and not self._confirm_folder_media(selected_ids):
            return

        self.abort_requested = False
        self._run_in_progress = True
        self._reset_run_statuses()
        self._run_token += 1
        self._current_profile_ids = list(selected_ids)
        # Snapshot the mapping on the UI thread: the per-profile workers read it
        # while the user could still be re-mapping folders in the window.
        self._run_folder_media = dict(self.folder_media_paths)
        self._batch_launch_delay_seconds = self._get_launch_delay_seconds()
        self._readiness_wait_seconds = self._get_readiness_wait_seconds()
        self._readiness_max_attempts = self._get_readiness_max_attempts()
        self._shutdown_on_success = bool(self.shutdown_on_success_var.get()) if hasattr(self, "shutdown_on_success_var") and self.shutdown_on_success_var is not None else False
        # Merge, don't replace -- the file also holds the folder media map, the
        # Airtable/Pipeline/Scheduler config, and flow speed.
        settings = load_settings()
        settings.update({
            "bearer_token": self._get_active_bearer_token(),
            "batch_launch_delay_seconds": self._batch_launch_delay_seconds,
            "readiness_wait_seconds": self._readiness_wait_seconds,
            "readiness_max_attempts": self._readiness_max_attempts,
            "story_media_path": self.story_media_path_var.get() if hasattr(self, "story_media_path_var") and self.story_media_path_var is not None else "",
            "shutdown_on_success": self._shutdown_on_success,
        })
        save_settings(settings)
        if flow_name in REEL_FLOWS:
            self._log_folder_media_plan(selected_ids)
        run_token = self._run_token
        self.run_button.config(state="disabled")
        self.abort_button.state(["!disabled"])
        for profile_id in selected_ids:
            self._set_profile_status(profile_id, "starting")
        self.logger.info("Starting workflow '%s' for %s profile(s)", flow_name, len(selected_ids))
        thread = threading.Thread(target=self._run_flows, args=(selected_ids, flow_name, run_token), daemon=True)
        self._active_run = thread
        thread.start()

    def run_from_airtable(self) -> None:
        if self._run_in_progress:
            messagebox.showinfo("Run in progress", "A workflow run is already in progress.")
            return

        token = (self.airtable_token_var.get() or "").strip()
        base_id = (self.airtable_base_id_var.get() or "").strip()
        table_name = (self.airtable_table_name_var.get() or "").strip() or "Profiles"
        if not token or not base_id:
            messagebox.showinfo(
                "Airtable not configured",
                "Set the Airtable token and base ID in Dev controls first.",
            )
            return

        self.abort_requested = False
        self._run_in_progress = True
        self._reset_run_statuses()
        self._run_token += 1
        self._run_folder_media = dict(self.folder_media_paths)
        self._batch_launch_delay_seconds = self._get_launch_delay_seconds()
        self._readiness_wait_seconds = self._get_readiness_wait_seconds()
        self._readiness_max_attempts = self._get_readiness_max_attempts()
        run_token = self._run_token
        self.run_button.config(state="disabled")
        self.airtable_button.config(state="disabled")
        self.abort_button.state(["!disabled"])
        # Selected profiles restrict the run; none selected = run all.
        selected_ids = [profile_id for profile_id, var in self.profile_vars.items() if var.get()]
        self._airtable_selected_count = len(selected_ids)
        scope = f"{len(selected_ids)} selected profile(s)" if selected_ids else "all profiles"

        # Optional override: run the UI-selected flow (+ its UI inputs) instead
        # of each profile's lifecycle flow.
        use_ui_flow = bool(getattr(self, "airtable_use_ui_flow_var", None) and self.airtable_use_ui_flow_var.get())
        override_flow = self._get_selected_flow_value() if use_ui_flow else None
        self._airtable_override_flow = override_flow
        self._airtable_override_caption = (self.reel_caption_var.get().strip() or None) if override_flow in REEL_FLOWS else None
        self._airtable_override_bio = (self.bio_var.get().strip()[:150] or None) if override_flow in ("update_bio", "update_bio_u2") else None
        self._airtable_override_picture = (self.picture_var.get().strip() or None) if override_flow == "update_profile_picture" else None

        flow_scope = f"UI flow '{override_flow}'" if override_flow else "each profile's lifecycle flow"
        self.logger.info("Starting Airtable run (base %s, scope: %s, running %s)", base_id, scope, flow_scope)
        thread = threading.Thread(
            target=self._run_airtable_thread,
            args=(token, base_id, table_name, run_token, selected_ids),
            daemon=True,
        )
        self._active_run = thread
        thread.start()

    def _confirm_airtable_run(self, num_to_run: int, num_skipped: int, run_token: int) -> bool:
        """Modal (shown on the UI thread) that tells the user how many profiles
        will run and asks them to proceed. Returns True to continue. Called from
        the worker thread, so it marshals to the main thread and blocks for the
        answer."""
        if run_token != self._run_token or self.abort_requested:
            return False

        result = {"ok": False}
        done = threading.Event()

        def _ask() -> None:
            try:
                selected = self._airtable_selected_count
                scope = f"{selected} selected profile(s)" if selected else "all profiles"
                override_flow = self._airtable_override_flow
                flow_line = f"Flow: UI-selected '{override_flow}'" if override_flow else "Flow: each profile's due lifecycle flow"
                if num_to_run <= 0:
                    detail = (
                        f"No in-scope profiles are runnable right now.\n\nScope: {scope}\nSkipped: {num_skipped}"
                        if override_flow
                        else f"No profiles have a flow due right now.\n\nScope: {scope}\nSkipped: {num_skipped}"
                    )
                    messagebox.showinfo("Nothing to run", detail)
                    result["ok"] = False
                else:
                    verb = f"run {override_flow}" if override_flow else "run their due flow(s)"
                    result["ok"] = bool(messagebox.askokcancel(
                        "Run from Airtable",
                        f"{num_to_run} profile(s) will {verb}.\n\n"
                        f"Scope: {scope}\n{flow_line}\nSkipped: {num_skipped}\n\nProceed?",
                    ))
            finally:
                done.set()

        if getattr(self, "root", None) is not None and self.root.winfo_exists():
            self.root.after(0, _ask)
            done.wait(timeout=300)
        return bool(result["ok"])

    def _run_airtable_thread(self, token: str, base_id: str, table_name: str, run_token: int, selected_ids: list | None = None) -> None:
        try:
            if self.abort_requested or run_token != self._run_token:
                return
            bearer_token = self._get_active_bearer_token()
            launcher_client = MultiloginLauncherClient(bearer_token)
            shutdown_client = MultiloginShutdownClient(bearer_token)
            adb_enable_client = MultiloginAdbEnableClient(bearer_token)
            api_client = MultiloginApiClient(bearer_token)
            automation = AutomationRunner()
            automation.register_flow(InstagramScrollFlow())
            automation.register_flow(InstagramLikeFeedFlow())
            automation.register_flow(InstagramNotificationsFlow())
            automation.register_flow(InstagramStoryUploadFlow())
            automation.register_flow(InstagramReelUploadFlow())
            automation.register_flow(InstagramReelUploadU2Flow())
            automation.register_flow(InstagramUpdateBioFlow())
            automation.register_flow(InstagramUpdateBioU2Flow())
            automation.register_flow(InstagramUpdateProfilePictureU2Flow())
            automation.register_flow(InstagramWarmUpDay1Flow())
            automation.register_flow(PushMediaTestFlow())
            automation.register_flow(InstagramReelIntentProbeFlow())

            airtable = AirtableClient(token, base_id, table_name)
            run_airtable_queue(
                airtable,
                launcher_client,
                shutdown_client,
                adb_enable_client,
                api_client,
                automation,
                self.logger,
                readiness_wait_seconds=self._readiness_wait_seconds,
                readiness_max_attempts=self._readiness_max_attempts,
                batch_launch_delay_seconds=self._batch_launch_delay_seconds,
                should_stop=lambda: self.abort_requested,
                status_callback=self._set_profile_status,
                progress_callback=self._set_profile_progress,
                selected_launch_ids=(set(selected_ids) if selected_ids else None),
                confirm_callback=lambda n, s: self._confirm_airtable_run(n, s, run_token),
                override_flow=self._airtable_override_flow,
                override_bio=self._airtable_override_bio,
                override_caption=self._airtable_override_caption,
                override_picture=self._airtable_override_picture,
                media_path_resolver=self._media_path_for_profile,
            )
        except Exception as exc:  # pragma: no cover - UI protection path
            self.logger.exception("Airtable queue run failed: %s", exc)
        finally:
            self.root.after(0, self._finish_run, run_token)

    def abort_run(self) -> None:
        if not self._run_in_progress and self._active_run is None:
            self.run_button.config(state="normal")
            self.abort_button.state(["disabled"])
            return

        self.abort_requested = True
        self._shutdown_on_abort = True
        self._run_in_progress = False
        self._active_run = None
        self.run_button.config(state="normal")
        if getattr(self, "airtable_button", None) is not None:
            self.airtable_button.config(state="normal")
        self.abort_button.state(["disabled"])
        self.logger.info("Abort requested by user")

    def _get_selected_flow_value(self) -> str:
        selected_label = self.flow_var.get()
        for flow in self.flow_options:
            if flow["label"] == selected_label:
                return flow["value"]
        return selected_label

    def _run_flows(self, selected_ids: list[str], flow_name: str, run_token: int) -> None:
        try:
            if self.abort_requested or run_token != self._run_token:
                return

            bearer_token = self._get_active_bearer_token()
            launcher_client = MultiloginLauncherClient(bearer_token)
            shutdown_client = MultiloginShutdownClient(bearer_token)
            adb_enable_client = MultiloginAdbEnableClient(bearer_token)
            api_client = MultiloginApiClient(bearer_token)
            automation = AutomationRunner()
            automation.register_flow(InstagramScrollFlow())
            automation.register_flow(InstagramLikeFeedFlow())
            automation.register_flow(InstagramNotificationsFlow())
            automation.register_flow(InstagramStoryUploadFlow())
            automation.register_flow(InstagramReelUploadFlow())
            automation.register_flow(InstagramReelUploadU2Flow())
            automation.register_flow(InstagramUpdateBioFlow())
            automation.register_flow(InstagramUpdateBioU2Flow())
            automation.register_flow(InstagramUpdateProfilePictureU2Flow())
            automation.register_flow(InstagramWarmUpDay1Flow())
            automation.register_flow(PushMediaTestFlow())
            automation.register_flow(InstagramReelIntentProbeFlow())

            # A rolling window: at most `concurrency` phones live at once, and the
            # next selected profile starts the moment one finishes. This path used
            # to launch every selected profile up front and size the pool to match,
            # so picking 20 booted 20 phones and ran 20 flows at once -- which does
            # not fail outright, it just makes every flow slow together. The
            # headless runners were capped long ago; this one was missed.
            concurrency = resolve_concurrency(None)
            gate = LaunchGate(self._batch_launch_delay_seconds)

            def launch_and_run(profile_id: str) -> None:
                if self.abort_requested or run_token != self._run_token:
                    self.logger.info("Abort requested before launching profile %s", profile_id)
                    return
                # The cross-loop ceiling on live phones. The scheduled loops may
                # already be driving phones on this box, and `concurrency` above
                # knows nothing about them. Unlike the loops this path waits
                # first -- a person picked these profiles by hand and a silent
                # skip would look like a bug -- but the wait is bounded, so a
                # busy box costs a minute, not a hung UI.
                with live_profile_slot(owner="ui", wait_seconds=UI_SLOT_WAIT_SECONDS) as slot:
                    if slot is None:
                        self.logger.error(
                            "Not launching profile %s: %s phone(s) are already open across every "
                            "loop (global ceiling). Try again once the running loops finish.",
                            profile_id, live_profile_count())
                        return
                    launch_response = gate.launch(
                        lambda: launcher_client.start_profiles([profile_id]))
                    if isinstance(launch_response, dict) and launch_response.get("status") == "error":
                        self.logger.error(
                            "Failed to launch profile %s on Multilogin. Response: %s",
                            profile_id,
                            launch_response,
                        )
                        return
                    self.logger.info("Launched profile %s successfully", profile_id)
                    # _run_single_profile waits for *this* profile to become ready,
                    # so the old global readiness sleep is no longer needed.
                    self._run_single_profile(
                        profile_id,
                        bearer_token,
                        api_client,
                        adb_enable_client,
                        shutdown_client,
                        automation,
                        run_token,
                        readiness_wait_seconds=self._readiness_wait_seconds,
                        readiness_max_attempts=self._readiness_max_attempts,
                        should_stop=lambda: self.abort_requested,
                        manual_continue_event=self.profile_manual_continue_events.get(profile_id),
                        manual_continue_callback=self._enable_manual_continue_button,
                        shutdown_on_success=self._shutdown_on_success,
                        launcher_client=launcher_client,
                    )

            self.logger.info("Running %s profile(s), up to %s at a time (rolling)",
                             len(selected_ids), concurrency)
            run_rolling(selected_ids, launch_and_run, concurrency=concurrency,
                        should_stop=lambda: self.abort_requested or run_token != self._run_token,
                        logger=self.logger)
        finally:
            self.root.after(0, self._finish_run, run_token)

    def clear_logs(self) -> None:
        self.log_output.configure(state="normal")
        self.log_output.delete("1.0", "end")
        self.log_output.configure(state="disabled")
        self.logger.info("Logs cleared from UI")

    def save_logs(self) -> None:
        try:
            file_path = filedialog.asksaveasfilename(
                defaultextension=".txt",
                filetypes=[("Text files", "*.txt"), ("All files", "*.*")],
                title="Save logs",
            )
            if not file_path:
                return
            content = self.log_output.get("1.0", "end")
            with open(file_path, "w", encoding="utf-8") as handle:
                handle.write(content)
            messagebox.showinfo("Logs saved", f"Logs were saved to {file_path}")
        except Exception as exc:  # pragma: no cover - UI protection path
            messagebox.showerror("Save failed", f"Unable to save logs: {exc}")

    def _run_single_profile(self, profile_id: str, bearer_token: str, api_client: MultiloginApiClient, adb_enable_client: MultiloginAdbEnableClient, shutdown_client: MultiloginShutdownClient, automation: AutomationRunner, run_token: int, readiness_wait_seconds: int = 10, readiness_max_attempts: int = 2, should_stop=None, manual_continue_event=None, manual_continue_callback=None, shutdown_on_success: bool = False, launcher_client=None) -> None:
        if self.abort_requested or run_token != self._run_token:
            self.logger.info("Abort requested, skipping profile %s", profile_id)
            return

        if manual_continue_event is not None:
            manual_continue_event.clear()
        self._disable_manual_continue_button(profile_id)

        self._set_profile_status(profile_id, "starting")
        selected_flow = self._get_selected_flow_value()
        # Reels only: the folder mapping feeds the reel flows' media_path. Other
        # flows keep resolving their media exactly as before.
        folder_media_path = self._media_path_for_profile(profile_id) if selected_flow in REEL_FLOWS else None
        if folder_media_path:
            self.logger.info("Profile %s will take its reel media from %s", profile_id, folder_media_path)
        run_profile_workflow(
            profile_id,
            bearer_token,
            api_client,
            adb_enable_client,
            shutdown_client,
            automation,
            self.logger,
            readiness_wait_seconds=readiness_wait_seconds,
            readiness_max_attempts=readiness_max_attempts,
            flow_name=selected_flow,
            should_stop=lambda: self.abort_requested,
            status_callback=self._set_profile_status,
            manual_continue_event=manual_continue_event,
            manual_continue_callback=manual_continue_callback,
            progress_callback=self._set_profile_progress,
            shutdown_on_abort=True,
            shutdown_on_success=shutdown_on_success,
            connect_max_attempts=5,
            connect_retry_delay_seconds=5,
            caption=(self.reel_caption_var.get().strip() or None) if selected_flow in REEL_FLOWS else None,
            bio=(self.bio_var.get().strip()[:150] or None) if selected_flow in ("update_bio", "update_bio_u2") else None,
            picture=(self.picture_var.get().strip() or None) if selected_flow == "update_profile_picture" else None,
            media_path=folder_media_path,
            # Lets readiness relaunch a profile whose launch didn't take,
            # instead of re-enabling ADB on something that isn't running.
            launcher_client=launcher_client,
        )

        self._disable_manual_continue_button(profile_id)

    def _reset_run_statuses(self) -> None:
        """Clear the previous run's per-profile statuses + summary so the report
        reflects only the run about to start."""
        self.profile_last_status.clear()
        for status_var in self.profile_status_vars.values():
            try:
                status_var.set("")
            except Exception:
                pass
        self._clear_run_summary()

    # Sheet order and labels. Problems first: with 58 rows the whole point is
    # not having to hunt for the ones that need a human.
    _SUMMARY_CATEGORIES = (
        ("failed", "✗ Failed"),
        ("attention", "⚠ Needs attention"),
        ("incomplete", "… Incomplete"),
        ("skipped", "– Skipped"),
        ("success", "✓ Success"),
    )

    @staticmethod
    def _natural_key(text: str) -> list:
        """Sort "Jasmin 2" before "Jasmin 10" instead of after it."""
        return [
            int(part) if part.isdigit() else part.lower()
            for part in re.split(r"(\d+)", text)
        ]

    def _sort_run_summary(self, column: str) -> None:
        """Header click: sort by that column, second click reverses it."""
        previous_column, descending = self._run_summary_sort or (None, False)
        descending = (column == previous_column) and not descending
        self._run_summary_sort = (column, descending)
        self._render_run_summary()

    def _render_run_summary(self) -> None:
        """Redraw the sheet from self._run_summary_rows in the current sort."""
        tree = getattr(self, "summary_tree", None)
        if tree is None or not tree.winfo_exists():
            return

        labels = dict(self._SUMMARY_CATEGORIES)
        severity = {key: index for index, (key, _label) in enumerate(self._SUMMARY_CATEGORIES)}
        rows = list(self._run_summary_rows)
        column, descending = self._run_summary_sort or ("outcome", False)
        if column == "profile":
            rows.sort(key=lambda row: self._natural_key(row[1]), reverse=descending)
        elif column == "detail":
            rows.sort(key=lambda row: (row[2].lower(), self._natural_key(row[1])), reverse=descending)
        else:
            rows.sort(key=lambda row: (severity.get(row[0], 99), self._natural_key(row[1])), reverse=descending)

        tree.delete(*tree.get_children())
        for index, (category, name, detail) in enumerate(rows):
            tags = [category] + (["stripe"] if index % 2 else [])
            tree.insert("", "end", values=(name, labels.get(category, category), detail), tags=tuple(tags))

    def _clear_run_summary(self) -> None:
        self._run_summary_rows = []
        if getattr(self, "run_summary_var", None) is not None:
            try:
                self.run_summary_var.set("")
            except Exception:
                pass
        self._render_run_summary()

    def _build_run_summary(self) -> tuple[list[tuple[str, str, str]], str, bool]:
        """Classify each profile's final status from the run that just finished
        into success / failed / needs-verification / skipped / incomplete and
        return (sheet_rows, headline, had_failures)."""
        success_keys = {"done"}
        skipped_keys = {"already_had_bio"}
        failed_keys = {"failed", "adb_connect_failed"}
        # Uncertain sits with the attention group, not with failures: these are
        # the ones a human should look at, and specifically the ones that must
        # NOT be blindly re-run.
        attention_keys = {"human_verification", "banned", "action_block", "uncertain"}
        incomplete_keys = {"starting", "connecting", "running", "manual_continue"}

        rows: list[tuple[str, str, str]] = []
        for profile_id, norm in self.profile_last_status.items():
            if not norm:
                continue
            name = self.profile_labels.get(profile_id, profile_id)
            display = self._STATUS_DISPLAY.get(norm, norm)
            if norm in success_keys:
                # No detail on a plain success: 43 rows of "done" is noise.
                rows.append(("success", name, ""))
            elif norm in skipped_keys:
                rows.append(("skipped", name, display))
            elif norm in failed_keys:
                # Plain "failed" is already the Outcome cell; only the specific
                # failures (ADB connect) earn a detail.
                rows.append(("failed", name, "" if norm == "failed" else display))
            elif norm in attention_keys:
                rows.append(("attention", name, display))
            elif norm in incomplete_keys:
                rows.append(("incomplete", name, display))

        if not rows:
            return ([], "No profiles ran.", False)

        counts = {key: 0 for key, _label in self._SUMMARY_CATEGORIES}
        for category, _name, _detail in rows:
            counts[category] = counts.get(category, 0) + 1

        parts = [f"Last run: {len(rows)} profile(s)", f"✓ Success {counts['success']}"]
        parts.extend(
            f"{label} {counts[key]}"
            for key, label in self._SUMMARY_CATEGORIES
            if key != "success" and counts[key]
        )
        return (rows, "  ·  ".join(parts), bool(counts["failed"]))

    def _format_run_summary_log(self, rows: list[tuple[str, str, str]]) -> str:
        """The same report as one log line, so saved logs still name the profiles
        the sheet lists."""
        if not rows:
            return "No profiles ran."

        def _join(names: list[str], cap: int = 25) -> str:
            if len(names) <= cap:
                return ", ".join(names)
            return ", ".join(names[:cap]) + f", +{len(names) - cap} more"

        chunks = [f"Last run: {len(rows)} profile(s)"]
        for key, label in self._SUMMARY_CATEGORIES:
            names = [
                name if not detail else f"{name} — {detail}"
                for category, name, detail in rows
                if category == key
            ]
            if names or key == "success":
                chunks.append(f"{label} ({len(names)}): {_join(names)}" if names else f"{label} (0)")
        return " | ".join(chunks)

    def _finish_run(self, run_token: int | None = None) -> None:
        if run_token is not None and run_token != self._run_token:
            return

        self._run_in_progress = False
        self._active_run = None
        self.run_button.config(state="normal")
        if getattr(self, "airtable_button", None) is not None:
            self.airtable_button.config(state="normal")
        self.abort_button.state(["disabled"])
        if self.abort_requested:
            self.logger.info("Workflow aborted")
        else:
            self.logger.info("Workflow run complete")

        rows, headline, _had_failures = self._build_run_summary()
        self._run_summary_rows = rows
        if getattr(self, "run_summary_var", None) is not None:
            self.run_summary_var.set(headline)
        self._render_run_summary()
        self.logger.info("Run summary — %s", self._format_run_summary_log(rows))

    def start(self) -> None:
        self.root.mainloop()


def main() -> None:
    app = WorkflowUI()
    app.start()


if __name__ == "__main__":
    main()
