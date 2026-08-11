"""Answer one question: are the captions themselves costing us posts?

The suspicion is that a caption -- text typed into the composer, often with a
call to action -- is what makes some accounts fail to post. That is testable,
but only if the test is *narrow*. This module runs it:

    three caption-bearing failures in a row on one account
    -> the next two attempts go out with no caption at all
    -> then captions come back

If the two bare attempts succeed where the three captioned ones failed, the
caption is implicated. If they fail the same way, it is not, and the search
moves on. Either answer is worth having; today there is no way to get either.

**What counts as a failure here is the whole design.** Most posting failures on
this fleet have nothing to do with text: a profile that never launched, ADB that
would not connect, a phone that stopped being ours mid-run. Counting those would
fire the probe on MultiLogin flakiness and strip captions off accounts that were
never having a caption problem -- the experiment would run constantly and prove
nothing. So only the two statuses where Instagram itself saw the post and it did
not land are counted (`CAPTION_RELEVANT_FAILURES`):

  - `failed`       -- the flow ran on the phone and the post did not go through
  - `action_block` -- Instagram refused the action, the single most likely way a
                      caption gets a post rejected

`done` clears the streak. Everything else -- infrastructure failures,
`already_shared`, and `uncertain` (share tapped, outcome not yet known) -- is
deliberately *neutral*: it neither counts against the caption nor forgives it,
because it is not evidence either way.

State is a small JSON file per target, written the same way the post ledger
writes: best-effort, never allowed to abort a post that is already in flight. A
lost state file costs the experiment a few attempts, not a post.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, asdict, field
from pathlib import Path

from adb_bot.config.settings import get_app_data_dir

STATE_FILENAME = "caption_probe.json"

# Three strikes, then two bare attempts -- the shape the experiment was asked
# for. Both are knobs so a run can widen the sample without a code change.
DEFAULT_FAILURES_BEFORE_PROBE = 3
DEFAULT_PROBE_ATTEMPTS = 2

# The only terminal statuses that count against the caption. See the module
# docstring: everything absent from this set is neutral, not forgiven.
CAPTION_RELEVANT_FAILURES = frozenset({"failed", "action_block"})

# The status that clears a streak.
SUCCESS_STATUS = "done"


def target_key(account_id: str = "", target_handle: str = "",
               launch_id: str = "") -> str:
    """Which Instagram account this post speaks as.

    Keyed on the account, never the phone. Two-account phones share one profile
    and one launch id, so keying on the phone would blend two accounts' results
    into one streak and the probe would strip captions off an account that never
    failed. The handle is preferred precisely because it is the one identifier
    that is distinct per account on such a phone.
    """
    handle = (target_handle or "").strip().lstrip("@").lower()
    if handle:
        return f"handle:{handle}"
    if account_id:
        return f"account:{account_id}"
    if launch_id:
        return f"profile:{launch_id}"
    return ""


@dataclass
class ProbeState:
    """What we know about one account's run of caption-bearing failures."""

    # Consecutive caption-relevant failures on posts that carried a caption.
    streak: int = 0
    # Attempts still owed with the caption deliberately withheld.
    probes_left: int = 0
    # Outcomes of the bare attempts, so the comparison survives a restart and
    # can be read back without trawling the Run Log.
    probe_results: list = field(default_factory=list)
    # The streak that opened the current (or last) probe, kept for the report.
    probe_opened_at_streak: int = 0

    @property
    def probing(self) -> bool:
        return self.probes_left > 0


class CaptionProbe:
    """Per-account caption suppression state.

    Two calls make up the contract:

      - `should_drop_caption(key)` before posting -- True while the account owes
        bare attempts.
      - `record(key, status, had_caption)` after -- advances the state machine.
    """

    def __init__(self, path=None,
                 failures_before_probe: int = DEFAULT_FAILURES_BEFORE_PROBE,
                 probe_attempts: int = DEFAULT_PROBE_ATTEMPTS):
        self.path = Path(path) if path else (get_app_data_dir() / STATE_FILENAME)
        self.failures_before_probe = max(1, int(failures_before_probe))
        self.probe_attempts = max(1, int(probe_attempts))

    # -- reading ---------------------------------------------------------

    def load(self) -> dict:
        """key -> ProbeState. An unreadable file yields no opinion at all."""
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except Exception:
            # Corrupt or hand-edited: start clean rather than crash the run. The
            # cost is a reset experiment, which is strictly better than a loop
            # that will not post.
            return {}
        states = {}
        for key, value in (raw or {}).items():
            try:
                states[key] = ProbeState(**value)
            except Exception:
                continue
        return states

    def state_for(self, key: str) -> ProbeState:
        return self.load().get(key, ProbeState())

    def should_drop_caption(self, key: str) -> bool:
        """True when this account owes a caption-free attempt."""
        if not key:
            return False
        return self.state_for(key).probing

    # -- writing ---------------------------------------------------------

    def _save(self, states: dict) -> bool:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            payload = json.dumps(
                {k: asdict(v) for k, v in states.items()}, ensure_ascii=False, indent=1)
            # Write-then-rename: a torn write would otherwise lose every
            # account's state at once, since this file is rewritten whole.
            tmp = self.path.with_suffix(".tmp")
            with tmp.open("w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, self.path)
            return True
        except Exception:
            # Same rule as the post ledger: this is instrumentation, never a
            # dependency. A failed write must not take a post down with it.
            return False

    def record(self, key: str, status: str, had_caption: bool) -> ProbeState:
        """Fold one post's outcome into this account's state and persist it.

        Returns the state *after* the update, so the caller can say in the Run
        Log what the probe just decided.
        """
        if not key:
            return ProbeState()

        states = self.load()
        state = states.get(key, ProbeState())

        if not had_caption and state.probing:
            # A bare attempt we asked for: spend it and keep the result.
            state.probes_left -= 1
            state.probe_results.append(status)
            if state.probes_left <= 0:
                # Experiment over. Clear the streak so captions get a fair fresh
                # run -- without this the very next captioned failure would trip
                # the probe again on a streak that was already spent.
                state.streak = 0
                state.probes_left = 0
        elif status == SUCCESS_STATUS and had_caption:
            # Only a captioned success forgives a captioned failure. A post that
            # went out bare proves nothing about the text, so it leaves the
            # streak where it is instead of quietly resetting the experiment.
            state.streak = 0
        elif status in CAPTION_RELEVANT_FAILURES and had_caption:
            state.streak += 1
            if state.streak >= self.failures_before_probe:
                state.probes_left = self.probe_attempts
                state.probe_opened_at_streak = state.streak
                state.probe_results = []
        # Anything else is neutral by design -- see the module docstring.

        states[key] = state
        self._save(states)
        return state

    # -- reporting -------------------------------------------------------

    def summary(self) -> dict:
        """What the experiment currently says, for the daily report."""
        states = self.load()
        probing = {k: v for k, v in states.items() if v.probing}
        finished = {k: v for k, v in states.items()
                    if v.probe_results and not v.probing}
        return {
            "tracked": len(states),
            "probing_now": len(probing),
            "probing_keys": sorted(probing),
            "finished": {
                k: {
                    "failed_with_caption": v.probe_opened_at_streak,
                    "bare_attempts": list(v.probe_results),
                    "bare_succeeded": sum(1 for r in v.probe_results
                                          if r == SUCCESS_STATUS),
                }
                for k, v in sorted(finished.items())
            },
        }
