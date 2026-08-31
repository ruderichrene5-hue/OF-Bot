"""Render a `report.collect()` snapshot as one self-contained HTML page.

Self-contained on purpose: no CDN, no fonts, no build step. The page has to open
from a `file://` URL on a headless server, over an SSH tunnel, and as a shared
snapshot, and the only thing that survives all three is one file with its CSS
inline.

Written for the question an operator actually asks at 21:00 -- "is it running,
and if not, what broke?" -- so the live state is at the top, the failures are
listed by name, and everything that is merely interesting is below the fold.
"""

from __future__ import annotations

import html
import time
from collections import Counter
from datetime import datetime

from adb_bot.automation import geelark_migration as _gm
from adb_bot.automation import schedule_spec
from adb_bot.automation.retry_runner import DEFAULT_MAX_RETRIES

REFRESH_SECONDS = 30

_CSS = """
:root {
  --bg: #ffffff; --fg: #1a1a1a; --muted: #6b7280; --line: #e5e7eb;
  --card: #f9fafb; --ok: #15803d; --warn: #b45309; --bad: #b91c1c;
  --ok-bg: #dcfce7; --warn-bg: #fef3c7; --bad-bg: #fee2e2; --accent: #1d4ed8;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #0f1115; --fg: #e6e6e6; --muted: #9ca3af; --line: #262b33;
    --card: #171a21; --ok: #4ade80; --warn: #fbbf24; --bad: #f87171;
    --ok-bg: #052e16; --warn-bg: #3b2f0b; --bad-bg: #3f1113; --accent: #60a5fa;
  }
}
:root[data-theme="dark"] {
  --bg: #0f1115; --fg: #e6e6e6; --muted: #9ca3af; --line: #262b33;
  --card: #171a21; --ok: #4ade80; --warn: #fbbf24; --bad: #f87171;
  --ok-bg: #052e16; --warn-bg: #3b2f0b; --bad-bg: #3f1113; --accent: #60a5fa;
}
:root[data-theme="light"] {
  --bg: #ffffff; --fg: #1a1a1a; --muted: #6b7280; --line: #e5e7eb;
  --card: #f9fafb; --ok: #15803d; --warn: #b45309; --bad: #b91c1c;
  --ok-bg: #dcfce7; --warn-bg: #fef3c7; --bad-bg: #fee2e2; --accent: #1d4ed8;
}
* { box-sizing: border-box; }
body {
  margin: 0; padding: 1.5rem; background: var(--bg); color: var(--fg);
  font: 15px/1.5 ui-sans-serif, system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
}
.wrap { max-width: 1100px; margin: 0 auto; }
h1 { font-size: 1.4rem; margin: 0 0 .2rem; }
h2 { font-size: 1.05rem; margin: 2rem 0 .6rem; padding-bottom: .3rem;
     border-bottom: 1px solid var(--line); }
h3 { font-size: .9rem; margin: 1.2rem 0 .4rem; color: var(--muted);
     text-transform: uppercase; letter-spacing: .04em; }
.sub { color: var(--muted); font-size: .85rem; margin-bottom: 1.2rem; }
/* The inline half of .sub: same grey, no block margins, for a quiet aside that
   sits inside a table cell rather than under a heading. */
.dim { color: var(--muted); font-size: .85rem; }
.grid { display: grid; gap: .75rem; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); }
.tile { background: var(--card); border: 1px solid var(--line); border-radius: 10px; padding: .8rem .9rem; }
.tile .label { color: var(--muted); font-size: .75rem; text-transform: uppercase;
               letter-spacing: .04em; }
.tile .value { font-size: 1.6rem; font-weight: 600; font-variant-numeric: tabular-nums;
               margin-top: .15rem; }
.tile .note { color: var(--muted); font-size: .78rem; margin-top: .1rem; }
.pill { display: inline-block; padding: .1rem .5rem; border-radius: 999px;
        font-size: .75rem; font-weight: 600; }
.pill.ok { background: var(--ok-bg); color: var(--ok); }
.pill.warn { background: var(--warn-bg); color: var(--warn); }
.pill.bad { background: var(--bad-bg); color: var(--bad); }
.scroll { overflow-x: auto; }
table { border-collapse: collapse; width: 100%; font-size: .88rem; }
th, td { text-align: left; padding: .4rem .6rem; border-bottom: 1px solid var(--line);
         white-space: nowrap; }
th { color: var(--muted); font-weight: 600; font-size: .75rem; text-transform: uppercase;
     letter-spacing: .04em; }
td.num, th.num { text-align: right; font-variant-numeric: tabular-nums; }
td.wrap-cell { white-space: normal; max-width: 420px; color: var(--muted); }
/* A hint that opens on hover and on tap, without a line of JavaScript. The
   text reveals *inline*, below whatever it explains, rather than floating over
   it: every table on this page sits in a horizontally scrolling box, and a
   floating bubble would be clipped by that box on the narrow screen this page
   is most often read on. tabindex is what makes a tap work -- touch has no
   hover, and :focus is the only thing it leaves behind. */
.hint { display: inline-block; width: 1.15em; height: 1.15em; line-height: 1.15em;
        margin-left: .35rem; border-radius: 999px; border: 1px solid var(--line);
        color: var(--muted); background: var(--card); font-size: .72rem;
        font-weight: 700; text-align: center; cursor: help; vertical-align: middle;
        font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }
.hint:hover, .hint:focus { color: var(--accent); border-color: var(--accent);
                           outline: none; }
.hint-text { display: none; white-space: normal; max-width: 44ch; margin-top: .3rem;
             color: var(--muted); font-size: .8rem; font-weight: 400;
             font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }
.hint:hover + .hint-text, .hint:focus + .hint-text { display: block; }
/* Keep the name at the top of its row while the hint pushes the row taller. */
td.hint-cell { vertical-align: top; }
/* Nothing to reveal on a printout, so show it all. */
@media print { .hint { display: none; } .hint-text { display: block; } }
code, .mono { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
              font-size: .85em; }
.empty { color: var(--muted); font-style: italic; }
.alerts { background: var(--card); border: 1px solid var(--line); border-radius: 10px;
          padding: .7rem .9rem; }
.alerts div { font-family: ui-monospace, Menlo, Consolas, monospace; font-size: .8rem;
              padding: .15rem 0; overflow-wrap: anywhere; }
footer { margin-top: 2.5rem; color: var(--muted); font-size: .78rem;
         border-top: 1px solid var(--line); padding-top: .8rem; }

/* Tabs without JavaScript: one radio per panel drives which is displayed. The
   page has to work from a file:// URL and inside a strict-CSP host, and this
   survives both. Radios stay in the DOM (not display:none) so they keep
   keyboard focus and screen-reader semantics. */
.tabnav input { position: absolute; width: 1px; height: 1px; opacity: 0; }
.tabs { display: flex; flex-wrap: wrap; gap: .25rem; margin: 1.2rem 0 1.6rem;
        border-bottom: 1px solid var(--line); }
.tabs label { padding: .55rem 1rem; cursor: pointer; font-weight: 600; font-size: .92rem;
              color: var(--muted); border-bottom: 2px solid transparent;
              margin-bottom: -1px; white-space: nowrap; }
.tabs label:hover { color: var(--fg); }
.panel { display: none; }
#tab-server:checked ~ #panel-server,
#tab-human:checked ~ #panel-human,
#tab-posts:checked ~ #panel-posts,
#tab-schedules:checked ~ #panel-schedules,
#tab-warmup:checked ~ #panel-warmup,
#tab-profiles:checked ~ #panel-profiles,
#tab-geelark:checked ~ #panel-geelark,
#tab-technical:checked ~ #panel-technical { display: block; }
#tab-server:checked ~ .tabs label[for="tab-server"],
#tab-schedules:checked ~ .tabs label[for="tab-schedules"],
#tab-warmup:checked ~ .tabs label[for="tab-warmup"],
#tab-profiles:checked ~ .tabs label[for="tab-profiles"],
#tab-geelark:checked ~ .tabs label[for="tab-geelark"],
#tab-technical:checked ~ .tabs label[for="tab-technical"] {
  color: var(--fg); border-bottom-color: var(--accent); }
#tab-server:focus-visible ~ .tabs label[for="tab-server"],
#tab-schedules:focus-visible ~ .tabs label[for="tab-schedules"],
#tab-warmup:focus-visible ~ .tabs label[for="tab-warmup"],
#tab-profiles:focus-visible ~ .tabs label[for="tab-profiles"],
#tab-geelark:focus-visible ~ .tabs label[for="tab-geelark"],
#tab-technical:focus-visible ~ .tabs label[for="tab-technical"] {
  outline: 2px solid var(--accent); outline-offset: 2px; border-radius: 4px; }
.tabs .count { display: inline-block; margin-left: .4rem; padding: .05rem .4rem;
               border-radius: 999px; font-size: .72rem; background: var(--bad-bg);
               color: var(--bad); }
.howto { background: var(--card); border: 1px solid var(--line); border-radius: 10px;
         padding: .9rem 1rem; margin: .8rem 0 1.4rem; }
.howto dt { font-weight: 600; margin-top: .7rem; }
.howto dt:first-child { margin-top: 0; }
.howto dd { margin: .15rem 0 0; color: var(--muted); font-size: .9rem; }
.lead { font-size: .95rem; margin: 0 0 1rem; }

/* One collapsible card per posting run. <details> is the only disclosure a
   strict-CSP page can have without script, and it keeps a 46-profile run from
   burying the four runs under it. Runs that lost a profile open by default --
   the ones worth reading are the ones with something red in them. */
details.run { background: var(--card); border: 1px solid var(--line);
              border-radius: 10px; padding: .55rem .8rem; margin: .55rem 0; }
details.run > summary { cursor: pointer; font-weight: 600; font-size: .92rem;
                        list-style: none; display: flex; flex-wrap: wrap;
                        align-items: center; gap: .4rem; }
details.run > summary::-webkit-details-marker { display: none; }
details.run > summary::before { content: "▸"; color: var(--muted); font-weight: 400; }
details.run[open] > summary::before { content: "▾"; }
details.run > summary:focus-visible { outline: 2px solid var(--accent);
                                      outline-offset: 2px; border-radius: 4px; }
details.run .when { color: var(--muted); font-weight: 400;
                    font-variant-numeric: tabular-nums; }
details.run .scroll { margin-top: .6rem; }
"""


def _e(value) -> str:
    return html.escape(str(value if value is not None else ""))


def _days_since(day: str) -> int:
    """Whole days from a ``YYYY-MM-DD`` to today, or 0 if it will not parse."""
    try:
        then = datetime.strptime(str(day)[:10], "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return 0
    return max(0, (datetime.now().date() - then).days)


def _fmt_seconds(seconds: float) -> str:
    seconds = float(seconds or 0)
    if seconds <= 0:
        return "-"
    if seconds < 60:
        return f"{seconds:.0f}s"
    minutes, rest = divmod(int(seconds), 60)
    if minutes < 60:
        return f"{minutes}m {rest:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes:02d}m"


def _tile(label: str, value, note: str = "", tone: str = "") -> str:
    colour = {"ok": "var(--ok)", "warn": "var(--warn)", "bad": "var(--bad)"}.get(tone, "")
    style = f' style="color:{colour}"' if colour else ""
    note_html = f'<div class="note">{_e(note)}</div>' if note else ""
    return (f'<div class="tile"><div class="label">{_e(label)}</div>'
            f'<div class="value"{style}>{_e(value)}</div>{note_html}</div>')


def _status_pill(status: str) -> str:
    tone = {"Posted": "ok", "Verifying": "warn", "Pending": "warn", "Failed": "bad"}.get(status, "")
    return f'<span class="pill {tone}">{_e(status)}</span>' if tone else _e(status)


def _section_now(now: dict) -> str:
    phones, slots, ceiling = now["phones"], now["slots_held"], now["slot_ceiling"]
    # Judge the cap on the thing it actually governs: slots in use. Raw phone
    # count includes orphans from runs that predate the cap, which the ceiling
    # cannot know about -- comparing those to it cries wolf on every healthy run.
    if ceiling and slots > ceiling:
        phone_tone = "bad"
    elif phones > slots:
        phone_tone = "warn"
    else:
        phone_tone = "ok"

    active = now["active_loops"]
    tiles = [
        _tile("Loops running", ", ".join(active) if active else "idle",
              "systemd ActiveState", "ok" if active else ""),
        _tile("Profiles locked", len(now.get("profiles") or []),
              "being worked on right now"),
        _tile("Live phones", phones,
              f"{slots} of {ceiling} tracked by the cap", phone_tone),
        _tile("MultiLogin agent", "up" if now["agent_up"] else "DOWN",
              "listening on :45001", "ok" if now["agent_up"] else "bad"),
    ]
    if now.get("stale_locks"):
        tiles.insert(2, _tile("Abandoned locks", len(now["stale_locks"]),
                              "owner died; see below", "warn"))
    body = f'<div class="grid">{"".join(tiles)}</div>'
    if slots > ceiling and ceiling:
        body += (f'<p class="sub" style="margin-top:.7rem">'
                 f'{slots} slots in use against a ceiling of {ceiling} — the cross-loop '
                 f'cap is not holding. Check the slot directory in ~/.adb_bot/locks.</p>')
    elif phones > slots:
        body += (f'<p class="sub" style="margin-top:.7rem">'
                 f'{phones - slots} phone(s) open that no slot accounts for — phones still '
                 f'starting, or orphans left by a run that was killed before the close '
                 f'guarantee existed. Nothing reaps those.</p>')
    def lock_table(entries, heading):
        rows = "".join(
            f"<tr><td class='mono'>{_e(entry['name'] or entry['profile_id'])}</td>"
            f"<td class='mono'>{_e(entry['owner'])}</td>"
            f"<td class='num'>{_e(_fmt_seconds(entry['age_seconds']))}</td>"
            f"<td class='num mono'>{_e(entry['pid'])}</td></tr>" for entry in entries)
        return (f"<h3>{_e(heading)}</h3><div class='scroll'><table>"
                "<tr><th>Profile</th><th>Held by</th><th class='num'>For</th>"
                "<th class='num'>PID</th></tr>" + rows + "</table></div>")

    if now.get("profiles"):
        body += lock_table(now["profiles"], "Profiles being worked on")
    if now.get("stale_locks"):
        body += lock_table(now["stale_locks"], "Abandoned locks")
        body += ('<p class="sub">These locks outlived their 45-minute TTL, so whatever held '
                 'them is gone. They no longer block anything — the next loop that wants one '
                 'takes it over — but they are the signature of a run that was killed rather '
                 'than finishing.</p>')
    return body


def _section_server(server: dict, procs=None) -> str:
    """The box itself. Memory leads because this machine has been OOM-killed."""
    mem_pct = server.get("mem_percent", 0.0)
    swap_total = server.get("swap_total_mb", 0)
    swap_used = server.get("swap_used_mb", 0)
    swap_pct = (100.0 * swap_used / swap_total) if swap_total else 0.0
    cores = server.get("cores", 0) or 1
    load = server.get("load1", 0.0)
    cpu_pct = server.get("cpu_percent", 0.0)

    # Name the process the number is about. "96%" invites the question; the
    # answer is one line away and is nearly always a single process.
    busiest = (procs or [None])[0]
    cpu_note = (f'{busiest["role"] or busiest["name"]} — {busiest["cpu_percent"]:,.0f}%'
                if busiest else f"load {load:.2f} over {cores} core(s)")

    tiles = [
        _tile("Processes", server.get("processes", 0), "running on the box"),
        # Judged on the CPU sample, not on load average: load counts processes
        # *waiting*, and this box spends its day blocked on phones rather than
        # computing, so it can sit under 1.0 while an encode holds every core.
        _tile("CPU", f'{cpu_pct:.0f}%', cpu_note,
              "bad" if cpu_pct >= 90 else "warn" if cpu_pct >= 70 else "ok"),
        _tile("Memory", f"{mem_pct:.0f}%",
              f'{server.get("mem_used_mb", 0):,} of {server.get("mem_total_mb", 0):,} MB used',
              "bad" if mem_pct >= 90 else "warn" if mem_pct >= 75 else "ok"),
        # Swap filling is the early warning the OOM kills of 2026-08-04 gave and
        # nobody was watching: RAM looks survivable right up until swap is gone.
        _tile("Swap", f"{swap_pct:.0f}%", f"{swap_used:,} of {swap_total:,} MB used",
              "bad" if swap_pct >= 90 else "warn" if swap_pct >= 50 else "ok"),
    ]
    return f'<div class="grid">{"".join(tiles)}</div>'


def _section_disks_and_uptime(data: dict) -> str:
    disks, uptime = data.get("disks") or [], data.get("uptime") or 0.0
    tiles = [_tile("Uptime", _fmt_seconds(uptime).replace("m ", "m "), "since last boot")]
    for disk in disks:
        pct = disk["percent"]
        tiles.append(_tile(f'Disk {disk["path"]}', f'{pct:.0f}%',
                           f'{disk["free_gb"]:.0f} GB free of {disk["total_gb"]:.0f} GB',
                           "bad" if pct >= 90 else "warn" if pct >= 80 else "ok"))
    return f'<div class="grid">{"".join(tiles)}</div>'


def _section_top_processes(procs) -> str:
    if not procs:
        return '<p class="empty">Could not read the process table.</p>'
    head = "<tr><th>Process</th><th class='num'>PID</th><th class='num'>Memory</th></tr>"
    rows = "".join(
        f"<tr><td class='mono'>{_e(proc['name'])}</td>"
        f"<td class='num mono'>{proc['pid']}</td>"
        f"<td class='num'>{proc['rss_mb']:,.0f} MB</td></tr>" for proc in procs)
    return (f'<div class="scroll"><table>{head}{rows}</table></div>'
            '<p class="sub">Biggest memory consumers. A single process reaching several '
            'GB here is what precedes an out-of-memory kill.</p>')


def _section_cpu_processes(procs, server: dict) -> str:
    """What is actually burning the CPU, named in the operator's terms.

    The box percentage alone raises the question rather than answering it, and
    the honest answer is almost always one process: an encode holds every core
    it can get, which looks identical to a runaway loop from the outside.
    """
    if not procs:
        return '<p class="empty">Nothing is using measurable CPU right now.</p>'
    cores = server.get("cores", 0) or 1
    head = ("<tr><th>Process</th><th>What it is</th><th class='num'>PID</th>"
            "<th class='num'>CPU</th></tr>")
    rows = []
    for proc in procs:
        percent = proc["cpu_percent"]
        # One process holding more than half the box is worth the eye going to
        # it first, whether that is expected (an encode) or not.
        tone = ("bad" if percent >= 100 * cores * 0.75
                else "warn" if percent >= 100 * cores * 0.4 else "")
        style = f' style="color:var(--{tone})"' if tone else ""
        rows.append(
            f"<tr><td class='mono'>{_e(proc['name'])}</td>"
            f"<td>{_e(proc['role'])}</td>"
            f"<td class='num mono'>{proc['pid']}</td>"
            f"<td class='num'{style}>{percent:,.0f}%</td></tr>")
    return (f'<div class="scroll"><table>{head}{"".join(rows)}</table></div>'
            f'<p class="sub">Per-core, the way <span class="mono">top</span> counts it: '
            f'100% is one core busy, and this box has {cores}. A video encode takes every '
            f'core it can and will sit near {cores * 100}% for the length of a clip — that '
            f'is the spoofer working, not a fault.</p>')


def _section_spoof(spoof: dict) -> str:
    """Is the spoofer working, what on, and how much is behind it."""
    now = spoof.get("now") or {}
    models = spoof.get("models") or []
    running, encoding = now.get("running"), now.get("encoding")

    if running or encoding:
        where = " · ".join(filter(None, [now.get("model"), now.get("run")]))
        state, tone = (f"encoding — {where}" if where else "encoding"), "ok"
    else:
        state, tone = "idle", ""

    tiles = [
        _tile("Spoofer", state, _e(now.get("clip")) or "no clip on the encoder", tone),
        _tile("Clips waiting", spoof.get("clips", 0), "raw videos not yet spoofed"),
        # The number that predicts the wait: encodes are serial, one per profile.
        _tile("Variants to encode", spoof.get("variants", 0),
              "one per profile, one at a time",
              "warn" if spoof.get("variants", 0) >= 40 else ""),
    ]
    if (running or encoding) and now.get("done") is not None and now.get("run"):
        tiles.append(_tile("This clip", f'{len(now["done"])} done',
                           "profiles finished in this run folder"))
    if now.get("seconds"):
        tiles.append(_tile("Encoding for", _fmt_seconds(now["seconds"]),
                           "this clip, all profiles",
                           "warn" if now["seconds"] > 3600 else ""))
    body = f'<div class="grid">{"".join(tiles)}</div>'

    if (running or encoding) and now.get("done"):
        body += ('<p class="sub" style="margin-top:.7rem">Already built for: '
                 + ", ".join(f'<span class="mono">{_e(h)}</span>' for h in now["done"])
                 + '.</p>')

    if spoof.get("error"):
        body += (f'<p class="sub" style="margin-top:.7rem">The waiting list could not be '
                 f'read ({_e(spoof["error"])}), so the two counts above are not to be '
                 f'trusted. What is on the encoder is read from this box and still is.</p>')
    elif not models and not spoof.get("unroutable"):
        body += ('<p class="sub" style="margin-top:.7rem">Every raw clip in Drive has been '
                 'through the pipeline. New content shows up here within five minutes of '
                 'landing in its model folder.</p>')
    elif models:
        head = ("<tr><th>Model</th><th class='num'>Clips</th><th class='num'>Profiles</th>"
                "<th class='num'>Variants</th><th>Waiting</th></tr>")
        rows = []
        for entry in models:
            folder = (f' <span class="sub">(folder {_e(entry["folder"])})</span>'
                      if entry["folder"] != entry["model"] else "")
            clips = "<br>".join(_e(name) for name in entry["clips"])
            rows.append(
                f"<tr><td class='mono'>{_e(entry['model'])}{folder}</td>"
                f"<td class='num'>{len(entry['clips'])}</td>"
                f"<td class='num'>{len(entry['handles'])}</td>"
                f"<td class='num'>{entry['variants']}</td>"
                f"<td class='mono sub'>{clips}</td></tr>")
        body += (f'<div class="scroll"><table>{head}{"".join(rows)}</table></div>'
                 '<p class="sub">"Profiles" is how many active profiles that model has, and '
                 'each one needs its own encode — the same clip cannot be sent to two '
                 'accounts. Parking a profile in Airtable removes it from this count.</p>')

    for entry in spoof.get("unroutable") or []:
        body += (f'<p class="sub" style="margin-top:.7rem">'
                 f'{entry["clips"]} clip(s) under <span class="mono">{_e(entry["folder"])}</span> '
                 f'have no active profile to be spoofed for, so nothing will pick them up. '
                 f'Either the folder is named for a model that has no profiles, or every '
                 f'profile under {_e(entry["model"])} is parked.</p>')

    for entry in spoof.get("unclaimed") or []:
        # An empty raw folder with no profiles behind it: a model somebody has
        # started onboarding and not finished. Shown because the alternative is
        # that it appears nowhere at all until a person notices the silence.
        body += (f'<p class="sub" style="margin-top:.7rem">'
                 f'Raw folder <span class="mono">{_e(entry["folder"])}</span> is empty and '
                 f'{_e(entry["model"])} has no active profile — a model part-way through '
                 f'onboarding. Nothing dropped in that folder will ever be spoofed until a '
                 f'profile is named <span class="mono">{_e(entry["model"])} 1</span> (in '
                 f'MultiLogin <em>and</em> in Profiles (Cloning)) with Status Active.</p>')
    return body


def _section_minutes(minutes) -> str:
    """MultiLogin phone-minutes: what the period has spent, and what is left.

    Counted from MultiLogin's own launcher log because there is no API for the
    balance (`mlx_minutes`). Shown even with no allowance configured -- the
    consumption is the useful half, and a fleet that does not know its burn rate
    is the one that gets surprised by it.
    """
    if minutes is None:
        # Deliberately not `_hint`: that "?" belongs to the loop table, and a
        # panel that borrowed it would put one on a page that has no loops.
        return ('<p class="dim">MultiLogin\u2019s launcher logs could not be '
                'read, so minute usage is unknown.</p>')
    used = f"{minutes.period_minutes:,.0f}"
    tiles = [_tile("used this period", used,
                   f"since {minutes.period_start}"),
             _tile("sessions", f"{minutes.period_sessions:,}",
                   "phone launches billed"),
             _tile("today", f"{minutes.today_minutes:,.0f}", "minutes")]
    if minutes.allowance:
        tone = "bad" if minutes.low else ""
        tiles.insert(0, _tile("minutes left", f"{minutes.remaining:,.0f}",
                              f"of {minutes.allowance:,} "
                              f"({minutes.used_pct:.0f}% used)", tone))
    else:
        tiles.append(_tile("allowance", "not set",
                           "set MLX_MINUTES_ALLOWANCE for a balance"))
    out = ['<div class="tiles">' + "".join(tiles) + "</div>"]

    recent = list(minutes.by_day)[-7:]
    if recent:
        peak = max(d.minutes for d in recent) or 1.0
        rows = []
        for day in reversed(recent):
            # A bar rather than a number alone: the thing worth seeing is that
            # some days cost three times others, which a column of figures hides.
            width = max(1, round(100 * day.minutes / peak))
            rows.append(
                f"<tr><td>{_e(day.day)}</td>"
                f"<td class=\"num\">{day.sessions:,}</td>"
                f"<td class=\"num\">{day.minutes:,.0f}</td>"
                f"<td><div style=\"background:#4b6bfb;height:.6rem;"
                f"width:{width}%;border-radius:3px\"></div></td></tr>")
        out.append(
            "<table><thead><tr><th>day</th><th class=\"num\">sessions</th>"
            "<th class=\"num\">minutes</th><th>&nbsp;</th></tr></thead>"
            f"<tbody>{''.join(rows)}</tbody></table>")
    if minutes.unclosed:
        out.append(f'<p class="dim">{minutes.unclosed} session(s) this period '
                   f'were never closed in the log and are not counted, so the '
                   f'real usage is a little higher.</p>')
    return "\n".join(out)


def _section_phones(phones) -> str:
    if not phones:
        return '<p class="empty">No phones are open right now.</p>'
    head = ("<tr><th>Profile</th><th>Phone</th><th class='num'>Open for</th>"
            "<th class='num'>Memory</th><th>State</th></tr>")
    rows = []
    for phone in phones:
        state = ('<span class="pill bad">orphan</span>' if phone["orphan"]
                 else '<span class="pill ok">in use</span>')
        rows.append(
            f"<tr><td class='mono'>{_e(phone['name'])}</td>"
            f"<td class='num mono'>{_e(phone['profile_id'] or phone['pid'])}</td>"
            f"<td class='num'>{_e(_fmt_seconds(phone['age_seconds']))}</td>"
            f"<td class='num'>{phone['rss_mb']:,.0f} MB</td><td>{state}</td></tr>")
    orphans = sum(1 for p in phones if p["orphan"])
    note = ('<p class="sub">An orphan is a phone older than 45 minutes that no loop holds a '
            'lock for. The reaper closes those every 20 minutes; nothing else would.</p>'
            if orphans else
            '<p class="sub">Every open phone belongs to a running job.</p>')
    return f'<div class="scroll"><table>{head}{"".join(rows)}</table></div>{note}'


def _hint(text: str) -> str:
    """A "?" that opens `text` in place, on hover or on tap.

    The label carries the whole sentence so a screen reader gets it without
    revealing anything -- the visible copy is `display:none` until asked for,
    and hidden text is not announced.
    """
    if not text:
        return ""
    return (f'<span class="hint" tabindex="0" role="note" aria-label="{_e(text)}">?</span>'
            f'<span class="hint-text" aria-hidden="true">{_e(text)}</span>')


def _section_live_work(rows, refresh_seconds: int = REFRESH_SECONDS) -> str:
    """Every profile being worked on: which loop has it, what it is sending,
    and since when.

    The "Reel" column is only ever filled for a profile the *posting* loop
    holds, and the note below says so -- see `report.annotate_live_reels` for
    why a warm-up profile's queue row is not its reel.
    """
    if not rows:
        return ('<p class="empty">No profile is being worked on right now — '
                'no loop holds a lock and no phone is open.</p>')

    head = ("<tr><th>Profile</th><th>Doing</th><th>Reel</th>"
            "<th class='num'>Started</th><th class='num'>Running for</th>"
            "<th class='num'>Phone</th></tr>")
    body = []
    for row in rows:
        if row["orphan"]:
            state = '<span class="pill bad">orphan</span>'
        elif not row["has_phone"]:
            state = '<span class="pill warn">launching</span>'
        else:
            state = f'<span class="mono">{_e(row["pid"])}</span>'
        # The full path in the title: the basename is what identifies the clip
        # to a person, but the run folder is what they need to go and find it.
        reel = (f'<span class="mono" title="{_e(row["reel_path"])}">{_e(row["reel"])}</span>'
                if row["reel"] else '<span class="sub">—</span>')
        # Marked, not silently blended in: this row's loop was worked out from
        # the slot it holds rather than read off a lock naming the profile.
        doing = _e(row["doing"]) + (_hint(
            "Attributed by the slot this loop holds, not by a profile lock — the recheck "
            "probe drives one phone and takes no lock. It is the right loop; on a box "
            "running several lockless loops at once it need not be the right phone."
        ) if row.get("by_slot") else "")
        body.append(
            f"<tr><td class='mono'>{_e(row['name'])}</td>"
            f"<td>{doing}</td><td>{reel}</td>"
            f"<td class='num mono'>{_e(row['started'])}</td>"
            f"<td class='num'>{_e(_fmt_seconds(row['for_seconds']))}</td>"
            f"<td class='num'>{state}</td></tr>")

    posting = sum(1 for r in rows if r["loop"] == "posting")
    named = sum(1 for r in rows if r["reel"])
    # The reconciliation, above the table rather than below it. This count and
    # the Server tab's "Live phones" tile are different quantities -- a profile
    # is being worked on from the moment its loop takes the lock, and its phone
    # comes up seconds to minutes later -- and two numbers that look like the
    # same thing must be shown to disagree on purpose, or the page just looks
    # wrong. Asked live 2026-08-07.
    with_phone = sum(1 for r in rows if r["has_phone"])
    launching = sum(1 for r in rows if not r["has_phone"] and r["profile_id"])
    slot_only = sum(1 for r in rows if not r["has_phone"] and not r["profile_id"])
    parts = [f'<strong>{with_phone}</strong> with a phone open']
    if launching:
        parts.append(f"{launching} still launching")
    if slot_only:
        parts.append(f"{slot_only} holding a slot with no phone yet")
    lead = (f'<p class="sub">{len(rows)} profile(s) being worked on — {", ".join(parts)}. '
            f'The <strong>{with_phone}</strong> with a phone open is the Server tab\'s '
            f'"Live phones"; the rest have been claimed by a loop but have no phone yet, '
            f'which is why this table is usually the longer of the two.</p>')
    notes = (f'<p class="sub">Started and "running for" are the phone process\'s own clock, '
             f'not the lock\'s — locks are taken for a whole batch before any phone comes up. '
             f'This is a snapshot, rebuilt every {_refresh_words(refresh_seconds)}; a post takes '
             f'minutes, so a profile listed here may have finished since.</p>')
    if posting and named < posting:
        notes += (f'<p class="sub">{posting - named} posting profile(s) have no reel named. '
                  f'The clip is read from the oldest still-Pending queue row for that profile, '
                  f'so a blank means Airtable was unreachable, or the row\'s result was written '
                  f'between the post finishing and this snapshot.</p>')
    if any(r["loop"] and r["loop"] != "posting" for r in rows):
        notes += ('<p class="sub">Only the posting loop names a reel. Warm-up and recheck open '
                  'the same phones for other work, and their profiles\' queue rows are not what '
                  'they are sending.</p>')
    return f'{lead}<div class="scroll"><table>{head}{"".join(body)}</table></div>{notes}'


def _section_timers(timers) -> str:
    if not timers:
        return '<p class="empty">No scheduled loops found.</p>'
    head = ("<tr><th>Loop</th><th>State</th><th>Runs</th>"
            "<th>Last run</th><th>Next run</th><th class='num'>In</th></tr>")
    rows = []
    for timer in timers:
        tone = "bad" if timer["stopped"] else "ok"
        if not timer["interval_min"]:
            every = "—"
        elif timer["interval_min"] < 1440:
            every = f'every {timer["interval_min"]} min'
        else:
            # "daily" alone is the one cadence nobody can act on.
            every = f'daily at {timer["at"]}' if timer.get("at") else "daily"
        left = _fmt_seconds(timer.get("seconds_until") or 0)
        hint = _hint(schedule_spec.WHAT_IT_DOES.get(timer["loop"], ""))
        rows.append(
            f"<tr><td class='mono hint-cell'>{_e(timer['loop'])}{hint}</td>"
            f"<td><span class='pill {tone}'>{_e(timer['state'])}</span></td>"
            f"<td>{_e(every)}</td>"
            f"<td class='mono'>{_e(timer['last'] or '-')}</td>"
            f"<td class='mono'>{_e(timer['next'] or 'running now')}</td>"
            f"<td class='num'>{_e(left if timer['next'] else '—')}</td></tr>")
    stopped = [t["loop"] for t in timers if t["stopped"]]
    state_note = (f'<span class="pill bad">{len(stopped)} stopped</span> '
                  f'{_e(", ".join(stopped))} — a stopped loop produces nothing and raises no '
                  f'alert, because alerts are only recorded when a loop actually runs.'
                  if stopped else
                  'All loops are scheduled. An empty "next run" means that loop is '
                  'executing right now.')
    note = (f'<p class="sub">{state_note} Tap the <span class="mono">?</span> beside a loop '
            f'to read what it does. These are the server\'s own clock — the same clock '
            f'"Last run" and "Next run" are in, and not necessarily the one the posting '
            f'times below use.</p>')
    return f'<div class="scroll"><table>{head}{"".join(rows)}</table></div>{note}'


def _section_today(data: dict) -> str:
    totals, ledger = data["totals"], data["ledger"]
    phone = data.get("phone") or {}
    queue = data["queue"]
    posted = queue["by_status"].get("Posted", 0)
    verifying = queue["by_status"].get("Verifying", 0)
    failed = queue["by_status"].get("Failed", 0)

    rate = totals["mlx_rate"]
    tiles = [
        # Not "Posts today": this is what the posting loop *sent*, counted off
        # its own run lines, and a run takes every row that is due -- including
        # rows dated days ago that a flag or a retry held back. The Posts tab
        # counts the rows *scheduled* for today. Two honest numbers, and while
        # both were called "Posts today" the page read as though one of them
        # was wrong: 200 here against 160 there on 2026-08-16.
        _tile("Sent by the loop", totals["posts"],
              f'{totals["runs"]} run(s) · any row that came due'),
        _tile("Avg per phone", _fmt_seconds(phone.get("average", 0.0)),
              (f'median {_fmt_seconds(phone.get("median", 0.0))}, '
               f'from {phone.get("samples", 0)} measured post(s)')
              if phone.get("samples") else "no completed phone yet"),
        _tile("Confirmed", posted, "Post Status = Posted", "ok" if posted else ""),
        _tile("Verifying", verifying, "recheck will resolve", "warn" if verifying else ""),
        _tile("Failed", failed, "see below", "bad" if failed else "ok"),
        _tile("MLX 500 rate", f"{rate:.1f}%",
              f'{totals["mlx_500"]}/{totals["attempts"]} launches',
              "bad" if rate >= 25 else "warn" if rate >= 10 else "ok"),
        _tile("Retries burned", totals["retries_burned"], "by MLX-side 500s",
              "warn" if totals["retries_burned"] else ""),
        _tile("Verify latency", _fmt_seconds(ledger["verify_seconds"]),
              "share → resolved"),
    ]
    grid = f'<div class="grid">{"".join(tiles)}</div>'
    grid += ('<p class="sub" style="margin-top:.7rem">"Sent by the loop" is counted from the '
             'posting log and covers every row the loop took today, whatever day that row was '
             'scheduled for — so it runs ahead of the <strong>Posts</strong> tab, which counts '
             'the rows dated today. A backlog draining is the usual gap between them. '
             '"Confirmed", "Verifying" and "Failed" beside it are today\'s rows, like that tab.'
             '</p>')
    if phone.get("samples"):
        note = (f'<p class="sub" style="margin-top:.7rem">"Avg per phone" is how long one '
                f'phone is actually held — from launching the profile to closing it — '
                f'measured on {phone["samples"]} post(s) that completed. Posts run up to 10 '
                f'at a time, so dividing a run\'s wall clock by its post count would report '
                f'roughly a tenth of the real figure. Longest so far '
                f'{_fmt_seconds(phone.get("longest", 0.0))}.')
        if phone.get("unclosed"):
            note += (f' {phone["unclosed"]} launch(es) have no close recorded and are excluded '
                     f'— still running, or leaked before the close guarantee existed.')
        grid += note + "</p>"
    return grid


def _section_runs(runs) -> str:
    if not runs:
        return '<p class="empty">No posting run has started today.</p>'
    head = ("<tr><th>Started</th><th>Finished</th><th class='num'>Planned</th>"
            "<th class='num'>Posts</th><th class='num'>Wall clock</th>"
            "<th class='num'>Per post</th><th class='num'>Launches</th>"
            "<th class='num'>MLX 500</th><th class='num'>Ours</th></tr>")
    rows = []
    for run in runs:
        finished = run.finished or '<span class="pill warn">running</span>'
        rows.append(
            f"<tr><td class='mono'>{_e(run.started[11:])}</td>"
            f"<td class='mono'>{finished if run.finished == '' else _e(run.finished[11:])}</td>"
            f"<td class='num'>{run.planned}</td><td class='num'>{run.posts}</td>"
            f"<td class='num'>{_e(_fmt_seconds(run.seconds))}</td>"
            f"<td class='num'>{_e(_fmt_seconds(run.seconds_per_post))}</td>"
            f"<td class='num'>{run.attempts}</td>"
            f"<td class='num'>{run.mlx_500} ({run.mlx_rate:.0f}%)</td>"
            f"<td class='num'>{run.other_failures}</td></tr>")
    return f'<div class="scroll"><table>{head}{"".join(rows)}</table></div>'


_OUTCOME_PILL = {
    "posted": ("ok", "posted"),
    "verifying": ("warn", "verifying"),
    "failed": ("bad", "did not post"),
    "pending": ("warn", "not sent yet"),
    "skipped": ("warn", "skipped"),
    "unknown": ("warn", "no result"),
}
_PILL_ORDER = ("posted", "verifying", "failed", "pending", "skipped", "unknown")


def _profile_table(profiles, unknown_label: str = "") -> str:
    """Who got the clip and who did not, worst first."""
    head = ("<tr><th>Profile</th><th>Result</th><th>What happened</th>"
            "<th>What happens next</th></tr>")
    rows = []
    for entry in profiles:
        tone, label = _OUTCOME_PILL.get(entry.outcome, ("warn", entry.outcome))
        if entry.outcome == "unknown" and unknown_label:
            label = unknown_label
        next_step = ""
        if entry.next_step:
            next_tone = entry.next_tone or "warn"
            next_step = f'<span class="pill {next_tone}">{_e(entry.next_step)}</span>'
        rows.append(
            f"<tr><td class='mono'>{_e(entry.label)}</td>"
            f"<td><span class='pill {tone}'>{_e(label)}</span></td>"
            f"<td class='wrap-cell'>{_e(entry.detail)}</td>"
            f"<td class='wrap-cell'>{next_step}</td></tr>")
    return f'<div class="scroll"><table>{head}{"".join(rows)}</table></div>'


def _count_pills(counts: dict, unknown_label: str = "no result") -> str:
    pills = []
    for outcome in _PILL_ORDER:
        if counts.get(outcome):
            tone, label = _OUTCOME_PILL[outcome]
            if outcome == "unknown":
                label = unknown_label
            pills.append(f'<span class="pill {tone}">{counts[outcome]} {label}</span>')
    return "".join(pills)


def _video_card(video) -> str:
    """One clip: which profiles it was made for, and where each one got to."""
    counts = video.counts()
    # The folder name is kept in the meta line: the card counts from 1 for the
    # day, and this is what you `cd` into when you want the files themselves.
    summary = (f'<summary>{_e(video.title)} '
               f'<span class="when">{_e(video.source)} · built '
               f'{_e(video.built[11:16])} · {_e(video.run)}</span> '
               f'{_count_pills(counts)}</summary>')
    body = _profile_table(video.sorted_profiles())
    # Open a clip that has not reached everybody; a fully delivered one is a line.
    unresolved = counts.get("failed", 0) + counts.get("pending", 0) + counts.get("unknown", 0)
    return f'<details class="run"{" open" if unresolved else ""}>{summary}{body}</details>'


def _section_videos(videos, day: str = "") -> str:
    if not videos:
        return ('<p class="empty">No spoofed clip has been built for today yet — '
                'the pipeline makes one folder per video.</p>')
    totals = Counter()
    for video in videos:
        totals.update(video.counts())
    planned = sum(totals[key] for key in _PILL_ORDER)
    # Before today's first clip exists the collector hands back the last day
    # that has any. Say so, or the counts read as this morning's.
    shown = videos[0].built[:10]
    stale = (f'<p class="sub">Nothing has been built yet today. Showing '
             f'<strong>{_e(shown)}</strong>, the last day the pipeline built '
             f'clips.</p>') if day and shown != day else ""
    lead = (f'{stale}<p class="sub">{len(videos)} clip(s), {planned} profile-copies in all · '
            f'{totals["posted"]} posted · {totals["verifying"]} verifying · '
            f'{totals["failed"]} did not post · {totals["pending"]} not sent yet. '
            'One card per video: the pipeline spoofs a clip once per profile of '
            'that model, and these are all of them — including the copies nobody '
            'has tried yet. Clips that have not reached everybody are open. '
            '"What happens next" is the retry pass\'s own verdict, so a red row '
            'with a green pill needs nobody.</p>')
    return lead + "".join(_video_card(video) for video in videos)


def _run_detail(run, index: int) -> str:
    """One posting tick as a collapsible card, with every profile it touched."""
    counts = run.counts()
    # A run still in flight has profiles with no result *yet*. Calling those
    # "no result" would read as a fault; they are simply mid-post.
    running = not run.finished
    unknown_label = "still going" if running else "no result"
    pills = _count_pills(counts, unknown_label) or (
        '<span class="pill warn">no profile reached a phone</span>')

    finished = _e(run.finished[11:16]) if run.finished else "running"
    summary = (f'<summary>Tick {index} '
               f'<span class="when">{_e(run.started[11:16])} → {finished}</span> '
               f'{pills}</summary>')

    if not run.profiles:
        body = ('<p class="empty">No profile is named in this run\'s log — it '
                'planned work but nothing reached a phone.</p>')
    else:
        body = _profile_table(run.sorted_profiles(), unknown_label)
    return f'<details class="run">{summary}{body}</details>'


def _section_run_detail(runs) -> str:
    if not runs:
        return '<p class="empty">No posting run has started today.</p>'
    totals = Counter()
    in_flight = 0
    for run in runs:
        counts = run.counts()
        totals.update(counts)
        if not run.finished:
            in_flight += counts.get("unknown", 0)
    missed = totals["failed"] + totals["unknown"] - in_flight
    still = f' · {in_flight} still going' if in_flight else ""
    lead = (f'<p class="sub">The same day seen the other way round: one card per '
            f'posting tick (the loop runs every 5 minutes and takes whatever is '
            f'due that minute), which is where the timing and the MultiLogin '
            f'failures live. {len(runs)} tick(s) · {totals["posted"]} posted · '
            f'{totals["verifying"]} verifying · {missed} did not post{still}. '
            'A profile that failed twice today appears twice.</p>')
    cards = [_run_detail(run, index) for index, run in enumerate(runs, start=1)]
    return lead + "".join(cards)


def _rate_tone(rate) -> str:
    """Colour for a success rate. Deliberately strict.

    A posting fleet that lands nine posts in ten is not "fine" -- the tenth is a
    profile someone has to open by hand -- so 90% is the floor for green rather
    than the ceiling. Below three quarters the day is red: that is a rate at
    which the failures, not the posts, are the day's output.
    """
    if rate is None:
        return ""
    if rate >= 90:
        return "ok"
    return "warn" if rate >= 75 else "bad"


def _section_daily(daily: dict) -> str:
    days = (daily or {}).get("days") or []
    if not days:
        return '<p class="empty">No posts have been scheduled yet, so there is no rate to show.</p>'

    totals = daily.get("totals") or {}
    lead = ('<p class="sub">One <strong>row</strong> per scheduled post, counted on the day it '
            'was <em>due</em>. A post that failed and was retried until it went out counts once, '
            'as confirmed — only a post that ran out of retries counts as failed. So this is the '
            'share of each day’s planned posts that actually reached Instagram, not the '
            'share of attempts that worked.</p>'
            '<p class="sub"><strong>Will post</strong> is the valid remainder: rows whose phone '
            'is healthy, so they go out on their own as the fleet works through them. '
            '<strong>Parked rows</strong> are waiting on a <em>person</em> — the phone is flagged, '
            'parked, or still needs its bio, picture and first post — and they will never post '
            'on their own however long they are left. A day whose remainder is nearly all parked '
            'has not had a slow evening; it has a backlog nobody is working.</p>'
            # The two tabs are read side by side and their two "will post"
            # figures never match, because this one folds in the Verifying
            # rows and the Posts tab shows those as their own tile. Said here
            # so nobody has to reconcile it a second time.
            '<p class="sub">Today’s <strong>Will post</strong> reads higher than the same '
            'figure on the Posts tab: this column also counts the <em>Verifying</em> rows — '
            'already on Instagram, waiting to be proved — while the Posts tab keeps them in '
            'their own tile and splits only the pending ones. The two differ by exactly the '
            'Verifying count.</p>')

    head = ("<tr><th>Day</th><th class='num'>Confirmed</th><th class='num'>Failed</th>"
            "<th class='num'>Success rate</th><th class='num'>Will post</th>"
            "<th class='num'>Parked rows</th></tr>")

    body = []
    for entry in days:
        rate, unsettled = entry["rate"], entry["unsettled"]
        parked, to_post = entry.get("parked"), entry.get("to_post")
        if rate is None:
            cell = '<span class="dim">—</span>'
        else:
            cell = f'<span class="pill {_rate_tone(rate)}">{rate:.0f}%</span>'
        # Two columns, because the old single "still to settle" answered the
        # wrong question. A parked row is waiting on a person and will not go
        # out on its own however long anyone leaves it; a will-post row is just
        # waiting its turn. Read together they said "busy evening" when the
        # truth was a backlog nobody was working.
        if parked is None:
            # No profile map this render -- show the old single figure rather
            # than two columns that would both be guesses.
            to_post_cell = ""
            parked_cell = (f"<span class='dim'>{unsettled} unsettled</span>"
                           if unsettled else "")
        else:
            to_post_cell = (f"<span class='ok'>{to_post}</span>" if to_post else "")
            parked_cell = (f"<span class='warn'>{parked} parked</span>"
                           if parked else "")
        body.append(
            f"<tr><td class='mono'>{_e(entry['day'])}</td>"
            f"<td class='num'>{entry['posted']}</td>"
            f"<td class='num'>{entry['failed'] or ''}</td>"
            f"<td class='num'>{cell}</td>"
            f"<td class='num'>{to_post_cell}</td>"
            f"<td class='num'>{parked_cell}</td></tr>")

    overall = totals.get("rate")
    total_cell = ('<span class="dim">—</span>' if overall is None
                  else f'<span class="pill {_rate_tone(overall)}">{overall:.0f}%</span>')
    total_parked, total_to_post = totals.get("parked"), totals.get("to_post")
    body.append(
        f"<tr><td><strong>All {len(days)} day(s)</strong></td>"
        f"<td class='num'><strong>{totals.get('posted', 0)}</strong></td>"
        f"<td class='num'><strong>{totals.get('failed', 0) or ''}</strong></td>"
        f"<td class='num'>{total_cell}</td>"
        f"<td class='num'><strong>{total_to_post if total_to_post else ''}</strong></td>"
        f"<td class='num'><strong>{total_parked if total_parked else ''}</strong></td></tr>")

    notes = ""
    if daily.get("omitted"):
        notes += (f'<p class="sub">{daily["omitted"]} older day(s) are not shown — the table '
                  f'keeps the most recent {len(days)}.</p>')
    if daily.get("undated"):
        notes += (f'<p class="sub">{daily["undated"]} row(s) carry no scheduled time and belong '
                  f'to no day, so they are in no rate above.</p>')
    return f'{lead}<div class="scroll"><table>{head}{"".join(body)}</table></div>{notes}'


def _section_failures(failures) -> str:
    if not failures:
        return '<p class="empty">Nothing failed today.</p>'
    head = ("<tr><th>Profile</th><th>Slot</th><th>Issue</th>"
            "<th class='num'>Retries</th><th>Notes</th></tr>")
    rows = "".join(
        f"<tr><td class='mono'>{_e(f['name'])}</td><td class='mono'>{_e(f['slot'])}</td>"
        f"<td>{_e(f['issue'])}</td><td class='num'>{_e(f['retries'])}</td>"
        f"<td class='wrap-cell'>{_e(f['notes'])}</td></tr>" for f in failures)
    return f'<div class="scroll"><table>{head}{rows}</table></div>'


# What each flag means and what the person handling it should actually do.
# Written for somebody with the MultiLogin app open and no interest in the code.
ISSUE_GUIDE = {
    "Human Verification Required": (
        "Instagram stopped the account and is asking a human to prove it is real — "
        "a code by email or SMS, a photo, or a \u201cwas this you?\u201d prompt.",
        "Open this profile\u2019s phone in MultiLogin, open Instagram, and complete "
        "whatever it asks. When the feed loads normally again, the account is fixed."),
    "Banned / Blocked": (
        "Instagram has banned the account, or blocked it from posting. The bot "
        "cannot undo either, and trying again can make it worse.",
        "Open the phone and read what Instagram says. If it offers an appeal, use it. "
        "If the account is gone for good, set the profile\u2019s Status to Inactive so "
        "the bot stops choosing it."),
    "Retries Exhausted": (
        f"The retry counter reached {DEFAULT_MAX_RETRIES} and the bot stopped trying, so it "
        "does not keep hammering the account. This one is a count, not a diagnosis: the same "
        "label has covered a phone sitting on an SMS checkpoint and a profile MultiLogin no "
        "longer has. Whatever is actually wrong, the notes below say it and the phone shows it.",
        "Open the phone and see what state Instagram is in — logged out, an update "
        "prompt, a checkpoint, a frozen screen. Fix that, then untag it."),
    "Repeated Failures": (
        "This profile keeps failing across different runs, so something about it is "
        "consistently wrong rather than unlucky.",
        "Open the phone and post one reel by hand. Whatever stops you is what stops "
        "the bot."),
    "No Recent Success": (
        "Nothing has actually landed on this account for over a day, while the bot kept "
        "trying. Each attempt failed or could not be confirmed, so no single run looks "
        "broken — it is only visible across a day of them.",
        "Post one reel from this phone by hand and watch it through: if it appears on the "
        "profile, untag it and the bot picks the account back up. If it does not, whatever "
        "stopped you is what has been stopping the bot, and no amount of retrying will get "
        "past it."),
    "Device Unreachable": (
        "The phone itself did not answer. Instagram may be perfectly fine — the "
        "device never came up.",
        "Check the phone in MultiLogin. Start it manually and see whether it boots; "
        "if it never does, the phone needs recreating."),
    # Both of these were reaching the page under the generic wording below,
    # which told somebody to go and look at a screen when the answer was already
    # written down -- and, for the held ones, to undo a hold put on deliberately.
    "Account Not On Phone": (
        "Airtable says this phone carries a handle that its Instagram account switcher does "
        "not list. Nothing is wrong with the phone or with the account it *is* signed in as; "
        "the row simply names an account that is not there, and every post planned for that "
        "handle fails without ever being attempted.",
        "Open the phone, read the account switcher, and make Airtable match it — correct the "
        "handle on the profile, or put the missing account back on the device. Retrying "
        "cannot help: the rows at the front of this profile's queue are the ones starving "
        "the account that does work."),
    "Held For Supervised Run": (
        "Deliberately held. Somebody proved this profile posts, and left the flag on so its "
        "backlog does not all come due at once — a profile works through everything due on a "
        "single launch, which can hold a phone for an hour.",
        "Nothing, unless you are the person draining it. Read the note below: it says what "
        "was proved and how many rows are waiting. Drain the backlog a post at a time, and "
        "untag it only when the queue is short enough to release."),
}
# No reason was ever written on these. In practice that means the flag came
# from somebody tagging the phone by hand in MultiLogin -- the tag raises the
# flag and carries no reason with it -- so the person who tagged it knows
# something the page does not.
DEFAULT_GUIDE = ("Flagged with no reason recorded. Almost always this is the "
                 "MultiLogin Issue tag applied by hand: the tag raises the flag, "
                 "and whoever applied it is the only record of why.",
                 "Open the phone in MultiLogin and see what state Instagram is in. "
                 "If it looks healthy, it probably is — a quarter of the phones "
                 "flagged this way turned out to have nothing wrong with them.")


def _dash(value: str, empty: str = "—") -> str:
    """An escaped value, or a muted placeholder when it is blank."""
    text = _e(value or "")
    return text if text else f'<span class="empty">{_e(empty)}</span>'


def _handle_cell(handle: str) -> str:
    """An Instagram handle with its @, or a dash. Never a bare @."""
    return f'<span class="mono">@{_e(handle)}</span>' if handle else _dash("")


def _pills(labels, tone: str) -> str:
    """A list of short labels as coloured pills, or a dash when it is empty."""
    if not labels:
        return '<span class="empty">—</span>'
    return " ".join(f'<span class="pill {tone}">{_e(label)}</span>' for label in labels)


def _handoff_folder(folder) -> str:
    """The MultiLogin folder cell on the hand-off list.

    Three answers, not two, because they send a person to different places.
    A name is the folder to open. `?` means MultiLogin answered and does not
    have this phone -- searching for it is wasted time, somebody deleted or
    moved it. Empty means MultiLogin did not answer at all, so the folder is
    unknown rather than absent, and the phone is very likely fine.
    """
    if folder and folder != "?":
        return f"<span class='mono'>{_e(folder)}</span>"
    if folder == "?":
        return "<span class='pill warn'>not in MultiLogin</span>"
    return "<span class='sub'>—</span>"


def _section_handoff(handoff: dict) -> str:
    """Profiles that finished their warm-up and are waiting on a person.

    Deliberately the first thing on the tab. Every other group here is a repair
    -- something broke and somebody unblocks it -- while this one is the only
    place the fleet *grows*, and it is the one nothing used to ask for. A phone
    can sit finished for a week and no page would have said so.
    """
    profiles = handoff.get("profiles") or []
    done = handoff.get("done") or 0
    if not profiles:
        if done:
            return (f'<p class="empty">Nothing waiting. All {done} profile(s) that have '
                    f'finished their warm-up have had their bio, picture and first post '
                    f'done.</p>')
        # Not "the day the plan ends". That reading is what put 41 profiles on
        # the Warm-up tab as finished and none of them here: reaching the end of
        # the calendar is not the same as doing the work, and this list is for
        # profiles that did it.
        finish = int(handoff.get("finish_day") or 0)
        gate = (f' — day {finish} is the last one that asks for any' if finish else "")
        return (f'<p class="empty">No profile has finished its warm-up yet. A profile '
                f'appears here once it has <strong>completed</strong> every run its plan '
                f'asks for{gate}. Running past the last day is not finishing: a profile '
                f'that did is on the Warm-up tab as <span class="mono">past the plan, not '
                f'finished</span>.</p>')

    lead = (f'<p class="sub">{len(profiles)} profile(s) have finished the warm-up and are '
            f'waiting on you. Each needs a <strong>bio</strong>, a <strong>profile '
            f'picture</strong> and a <strong>first post made by hand</strong> before the bot '
            f'may schedule reels for it — a fresh account whose first ever post is an '
            f'automated reel is the one Instagram acts on.'
            + (f' {done} other profile(s) are already done.' if done else "") + '</p>')

    head = ("<tr><th>Profile</th><th>Folder</th><th class='num'>Serial</th>"
            "<th class='num'>Finished</th><th>Still to do</th><th>Already done</th></tr>")
    rows = "".join(
        f"<tr><td class='mono'>{_e(p['name'])}</td>"
        f"<td>{_handoff_folder(p.get('folder'))}</td>"
        f"<td class='num mono'>{_e(p['serial'])}</td>"
        f"<td class='num mono'>{_e(p['finished_at'] or '—')}</td>"
        f"<td>{_pills(p['outstanding'], 'bad')}</td>"
        f"<td>{_pills(p['done_tasks'], 'ok')}</td>"
        f"</tr>" for p in profiles)
    return (lead + f'<div class="scroll"><table>{head}{rows}</table></div>'
            + '<div class="howto"><dl>'
              '<dt>What to do</dt><dd>Open the phone in MultiLogin, write the bio, set the '
              'profile picture, then make one post by hand and watch it appear on the '
              'profile. <span class="mono">Folder</span> is where to look for it — these '
              'phones are mostly called <span class="mono">Blank (NN)</span>, so the folder '
              'is what says whose account you are setting up.</dd>'
              '<dt>When each part is done</dt><dd>Tick <span class="mono">Bio Done</span>, '
              '<span class="mono">Profile Picture Done</span> and <span class="mono">First '
              'Post Done</span> on that profile in Airtable (Profiles (Cloning)). Tick them '
              'as you go — the row drops off this list when all three are ticked, and the '
              'bot will not schedule a reel until then.</dd>'
              '</dl></div>')


def _rows_mlx_issue(entries) -> str:
    """One table body for a group of hand-tagged phones."""
    out = []
    for e in entries:
        where = e["stage"] or (f"day {e['day']}" if e["day"] else "-")
        out.append(
            f"<tr><td class='mono'>{_e(e['name'])}</td>"
            f"<td class='mono'>{_e(e['serial'] or '-')}</td>"
            f"<td>{_e(e['status'])}</td>"
            f"<td>{_e(where)}</td>"
            f"<td class='mono'>{_e(e['last_run'] or '-')}</td>"
            f"<td class='mono'>{_e(', '.join(e['tags']) or '-')}</td></tr>")
    return "".join(out)


def _section_mlx_issues(tagged: dict) -> str:
    """Phones a person marked in MultiLogin that no Airtable field records.

    The `Issue` tag is applied by hand, in the workspace where the VAs actually
    work; the bot's own flag is `Needs Human Check` in Airtable, and the mirror
    between them runs one way only. So a tag put on by hand reaches nothing --
    not this page, not `posting_planner`, not the Telegram alert. It is the one
    kind of "this phone is broken" the system could not see.

    Warm-up phones get the table because they are the ones nothing else can
    catch: no queue rows to fail, no flag to tick, so neither the flagged list
    above nor the abandoned rows on the Posts tab can ever mention them. Parked
    phones get a single line -- the tag there is usually the note saying why
    somebody switched it off, and listing eighteen of those as work would bury
    the ones that are work.
    """
    if tagged.get("error"):
        return (f'<h2>Tagged in MultiLogin</h2><p class="sub">'
                f'<span class="pill warn">could not read MultiLogin</span> '
                f'<span class="mono">{_e(tagged["error"])}</span> — '
                f'hand-applied tags were not checked this refresh.</p>')

    warmup = tagged.get("warmup") or []
    parked = tagged.get("parked") or []
    other = tagged.get("other") or []
    if not (warmup or parked or other):
        return ('<h2>Tagged in MultiLogin</h2><p class="empty">Every phone carrying the '
                '<span class="mono">Issue</span> tag is also flagged in Airtable, so it is '
                'already in the list above.</p>')

    header = ('<tr><th>Profile</th><th>Serial</th><th>Status</th><th>Warm-up</th>'
              '<th>Last run</th><th>Other tags</th></tr>')
    parts = ['<h2>Tagged <span class="mono">Issue</span> in MultiLogin, '
             'not flagged in Airtable</h2>',
             '<p class="sub">Somebody marked these phones in the MultiLogin workspace. '
             'Nothing in the bot reads that tag, so until the checkbox is ticked in Airtable '
             'they are invisible to every other list on this page.</p>']

    if warmup:
        parts.append(
            f'<h3>In warm-up — {len(warmup)} phone(s)</h3>'
            f'<div class="howto"><dl>'
            f'<dt>What happened</dt><dd>These phones are part-way through the warm-up and '
            f'somebody tagged them <span class="mono">Issue</span> in MultiLogin. The '
            f'warm-up loop does not read tags, so it will keep running them as if nothing '
            f'were wrong — and because a warming phone has no posts queued, nothing else '
            f'can notice either.</dd>'
            f'<dt>What to do</dt><dd>Open the phone and see what the tag was about. If it '
            f'still needs a person, tick <span class="mono">Needs Human Check</span> on that '
            f'profile in Airtable (Profiles (Cloning)) — that is what stops the loops and '
            f'puts it in the list above. If it is fine now, remove the tag in MultiLogin.</dd>'
            f'<dt>Why it is not already up there</dt><dd>The bot copies the Airtable checkbox '
            f'onto the MultiLogin tag, never the other way round. Ticking a box because '
            f'somebody added a tag would also re-tick it every time a VA cleared one.</dd>'
            f'</dl></div>'
            f'<div class="scroll"><table>{header}{_rows_mlx_issue(warmup)}</table></div>')

    if other:
        parts.append(
            f'<h3>Active, not warming up — {len(other)} phone(s)</h3>'
            f'<p class="sub">Switched on and tagged, but carrying no flag — so every other '
            f'list on this page reads them as healthy. Mostly unassigned blanks.</p>'
            f'<div class="scroll"><table>{header}{_rows_mlx_issue(other)}</table></div>')

    if parked:
        names = ", ".join(f"<span class='mono'>{_e(e['name'])}</span>" for e in parked[:12])
        more = f" and {len(parked) - 12} more" if len(parked) > 12 else ""
        parts.append(
            f'<h3>Already parked — {len(parked)} phone(s)</h3>'
            f'<p class="sub">Tagged and <span class="mono">Inactive</span>, so the bot is '
            f'already ignoring them and the tag is most likely the note explaining why. '
            f'Listed for completeness, not as work: {names}{more}.</p>')

    return "".join(parts)


def _section_needs_human(data: dict) -> str:
    """Everything waiting on a person, in one place, for the people who do it.

    This used to be the Profiles tab, which also had to be the place you looked
    up a phone -- so a VA's worklist and an inventory sat in the same scroll.
    Split: the work lives here, the inventory lives there.
    """
    triage = data.get("needs_human") or {}
    profiles = triage.get("profiles") or []
    retrying = triage.get("retrying") or []
    handoff = data.get("handoff") or {}
    queue = data.get("queue") or {}
    verifying = (queue.get("by_status") or {}).get("Verifying", 0)

    if triage.get("error"):
        return (f'<p class="sub"><span class="pill warn">could not read Airtable</span> '
                f'<span class="mono">{_e(triage["error"])}</span></p>')

    reasons = {}
    for profile in profiles:
        reasons.setdefault(profile["reason"], []).append(profile)

    waiting = handoff.get("profiles") or []
    tagged = data.get("mlx_issues") or {}
    tagged_warmup = tagged.get("warmup") or []
    tiles = [
        _tile("Ready for hand-off", len(waiting), "warmed up, need bio / picture / first post",
              "warn" if waiting else "ok"),
        _tile("Need you", len(profiles), "profiles flagged for review",
              "bad" if profiles else "ok"),
        _tile("Tagged in warm-up", len(tagged_warmup),
              "marked Issue in MultiLogin, never flagged",
              "bad" if tagged_warmup else "ok"),
        _tile("Being checked", verifying,
              "posted, waiting on confirmation", "warn" if verifying else "ok"),
        _tile("Retrying by itself", len(retrying),
              "no action needed", "ok"),
    ]
    parts = [
        '<p class="lead">Everything waiting on a person, in one place. '
        'Three kinds of work: profiles that have finished their warm-up and need setting up, '
        'accounts the bot has given up on, and phones somebody tagged '
        '<span class="mono">Issue</span> in MultiLogin that the bot never heard about. '
        'Everything else it handles on its own.</p>',
        f'<div class="grid">{"".join(tiles)}</div>',
        '<h2>Finished warm-up — ready for a person</h2>',
        _section_handoff(handoff),
        _section_mlx_issues(tagged),
    ]
    # Flagged phones MultiLogin no longer has. They stay flagged in Airtable on
    # purpose -- the flag protects the diagnosis written in Issue Notes -- so
    # this worklist has to drop them itself, and say that it did.
    parts.append(_retired_note(triage.get("retired")))

    if not profiles:
        parts.append('<h2>Nothing broken</h2><p class="empty">No account is flagged for '
                     'review. Anything failing is either retrying automatically or '
                     'already parked.</p>')
    else:
        parts.append('<h2>Accounts to fix</h2>')
        parts.append('<p class="sub">Grouped by what is wrong. Work top to bottom — the '
                     'first group is the most serious.</p>')
        # "No Recent Success" sits third because it is the undiagnosed one: the
        # others name what is wrong, this one only says the account has gone
        # quiet and somebody has to find out why. An account silently posting
        # nothing for a day outranks one whose problem is already understood.
        # "Held For Supervised Run" sorts below even the unlabelled ones: it is
        # the one group on this list that is not a fault and not work, and a
        # deliberate hold at the top of a worklist reads as the worst problem
        # on it.
        order = ["Banned / Blocked", "Human Verification Required", "No Recent Success",
                 "Device Unreachable", "Account Not On Phone", "Repeated Failures",
                 "Retries Exhausted"]
        held_last = {"Held For Supervised Run": 100}
        for reason in sorted(reasons, key=lambda r: held_last.get(
                r, order.index(r) if r in order else 99)):
            group = reasons[reason]
            what, todo = ISSUE_GUIDE.get(reason, DEFAULT_GUIDE)
            names = "".join(
                f"<tr><td class='mono'>{_e(p['name'])}</td>"
                f"<td>{_e(p['status'])}</td>"
                f"<td class='mono'>{_e(p['flagged_at'] or '-')}</td>"
                f"<td class='wrap-cell'>{_e(p['note'][0] if p['note'] else '')}</td></tr>"
                for p in group)
            fixed = (
                'Take the <span class="mono">Issue</span> tag off that profile in '
                'MultiLogin. Within about fifteen minutes the flag clears itself, the '
                'profile goes back to Active and whatever was stuck is re-queued — you '
                'do not need to open Airtable. Putting the tag <em>on</em> a profile is '
                'how one gets onto this list in the first place.')
            if reason == "Held For Supervised Run":
                # The same sentence, but it is an instruction not to follow yet:
                # untagging releases the whole backlog on one launch, which is
                # the thing the hold exists to prevent.
                fixed = (
                    'Only when the backlog is drained. Taking the '
                    '<span class="mono">Issue</span> tag off releases every row that is due '
                    'at once, and the profile works through all of them on a single launch — '
                    'which is what the hold is for. Drain it first, then untag.')
            parts.append(
                f'<h3>{_e(reason)} — {len(group)} account(s)</h3>'
                f'<div class="howto"><dl>'
                f'<dt>What happened</dt><dd>{_e(what)}</dd>'
                f'<dt>What to do</dt><dd>{_e(todo)}</dd>'
                f'<dt>When it is fixed</dt><dd>{fixed}</dd>'
                f'</dl></div>'
                f'<div class="scroll"><table>'
                f'<tr><th>Account</th><th>Profile status</th><th>Flagged</th><th>Latest note</th></tr>'
                f'{names}</table></div>')

    parts.append('<h2>What the words mean</h2>')
    parts.append(
        '<div class="howto"><dl>'
        '<dt>Being checked (Verifying)</dt>'
        '<dd>The bot posted the reel but has not yet proved it went live. It re-opens the '
        'phone about 15 minutes later and counts the account\u2019s posts. This needs '
        'nothing from you — it resolves by itself into posted or failed.</dd>'
        '<dt>Retrying by itself</dt>'
        '<dd>A post failed for a reason that often clears up on its own — the phone was '
        f'slow, MultiLogin hiccuped. The bot tries up to {DEFAULT_MAX_RETRIES} times, waiting '
        'longer after each one, before it gives up and asks for you.</dd>'
        '<dt>Profile status Active / Inactive</dt>'
        '<dd>Inactive means the bot ignores this profile completely: no posts are planned '
        'for it. That is how you park an account you are still fixing, or one that is gone '
        'for good.</dd>'
        '<dt>Why an account can be flagged but still posting</dt>'
        '<dd>The flag is about one bad run. If the next run works, the account keeps '
        'posting — the flag simply stays until somebody unticks it.</dd>'
        '</dl></div>')

    return "".join(parts)


def _section_abandoned(data: dict) -> str:
    """Queue rows the retry pass will never pick up again.

    Lives on the Posts tab rather than beside the flagged accounts. They are a
    *consequence* of those accounts -- one flagged profile leaves six dead rows
    behind it -- so on a worklist they inflate the job by a factor of six, and
    the number a VA has to work through stops matching the number on the page.
    """
    triage = data.get("needs_human") or {}
    rows = triage.get("rows") or []
    if triage.get("error"):
        return (f'<p class="sub"><span class="pill warn">could not read Airtable</span> '
                f'<span class="mono">{_e(triage["error"])}</span></p>')
    if not rows:
        return ('<p class="empty">No abandoned posts. Every failed row is either still '
                'retrying or has been settled.</p>')

    body = "".join(
        f"<tr><td class='mono'>{_e(r['name'])}</td><td class='mono'>{_e(r['slot'])}</td>"
        f"<td>{_e(r['issue'])}</td><td class='num'>{_e(r['retries'])}</td></tr>"
        for r in rows)
    return (f'<p class="sub">{len(rows)} scheduled post(s) will not be tried again. They are '
            f'listed so nothing disappears silently — but fixing the profile on the '
            f'<strong>Needs human</strong> tab is what matters, not these rows. One flagged '
            f'profile leaves a day of them behind it.</p>'
            '<div class="scroll"><table>'
            '<tr><th>Post</th><th>Was due</th><th>Why it stopped</th>'
            "<th class='num'>Tries</th></tr>" + body + "</table></div>")


def _section_posts_today(posts: dict) -> str:
    """What is going out today: which clip, on which profile, and how it went.

    The Schedules tab answers "when", per model and as policy. This answers
    "what" -- which existed nowhere: the queue was only ever five status counts,
    so nobody could see the actual reel a profile was sending.
    """
    rows = posts.get("posts") or []
    by_status = posts.get("by_status") or {}
    if not rows:
        return (f'<p class="empty">No posts are scheduled for {_e(posts.get("day") or "today")}. '
                f'The queue loop fills the day as each model\'s slots come round.</p>')

    pending = posts.get("pending", by_status.get("Pending", 0))
    parked, to_post = posts.get("parked"), posts.get("to_post")
    verifying = by_status.get("Verifying", 0)

    if parked is None:
        # No profile map this render. The old single tile rather than two
        # columns that would both be guesses.
        pending_tiles = [_tile("Still to go", pending, "scheduled, not sent yet")]
        split = ('<p class="sub"><strong>Still to go</strong> could not be split this '
                 'refresh — the profile list did not read, so the page cannot say which '
                 'of those rows a phone will actually send.</p>')
    else:
        # Two tiles, because "scheduled, not sent yet" reads as a promise. Most
        # of that number is usually a phone nobody has touched, and on a tab
        # that lists the day's posts one by one, the count that will never
        # become a post is the one worth its own tile.
        pending_tiles = [
            _tile("Will post", to_post, "phone healthy, goes out on its own",
                  "ok" if to_post else ""),
            _tile("Waiting on a person", parked, "flagged, parked or mid hand-off",
                  "bad" if parked > to_post else ("warn" if parked else "")),
        ]
        reasons = posts.get("parked_reasons") or {}
        detail = ", ".join(f"{n} {_e(why)}" for why, n in reasons.items())
        split = (f'<p class="sub"><strong>Still to go is {pending}, but only '
                 f'{to_post} of it will go out.</strong> The other {parked} '
                 f'{"is" if parked == 1 else "are"} waiting on a <em>person</em> — the '
                 f'posting loop skips {"it" if parked == 1 else "them"} every tick and '
                 f'will keep skipping {"it" if parked == 1 else "them"} however long '
                 f'{"it is" if parked == 1 else "they are"} left'
                 + (f': {detail}' if detail else '') + '. '
                 'Each stuck row says which gate it is behind in the '
                 '<em>Why it is stuck</em> column below.</p>'
                 '<p class="sub">The Technical tab counts the same day as '
                 f'<strong>{to_post + verifying} will post</strong>, not {to_post}: its '
                 f'column also holds the {verifying} <em>Verifying</em> row(s), which are '
                 'already on Instagram and only waiting to be proved. Same rows, and the '
                 'two tabs differ by exactly that count — nothing is missing from either.')

    tiles = [
        _tile("Posts today", posts.get("total", 0),
              f'{len(posts.get("by_profile") or [])} profile(s)'),
        _tile("Posted", by_status.get("Posted", 0), "confirmed live",
              "ok" if by_status.get("Posted") else ""),
    ] + pending_tiles + [
        _tile("Verifying", verifying, "sent, not yet proved",
              "warn" if verifying else ""),
        _tile("Failed", by_status.get("Failed", 0), "see abandoned posts below",
              "bad" if by_status.get("Failed") else "ok"),
        _tile("Distinct clips", posts.get("clips", 0), "one file per account, ideally"),
    ]

    reused = posts.get("reused_clips") or []
    warn = ""
    if reused:
        # The failure the whole spoof pipeline exists to prevent: two accounts
        # posting the same file is what gets them flagged. It cannot be seen
        # from a status count, only by laying the day's clips side by side.
        detail = "; ".join(f'{_e(c["clip"])} → {_e(", ".join(c["profiles"]))}'
                           for c in reused[:5])
        warn = (f'<p><span class="pill bad">{len(reused)} clip(s) on more than one '
                f'profile</span> {detail}. Each account is supposed to get its own spoofed '
                f'encode — the same file on two accounts is what gets them flagged.</p>')

    # Its own column rather than folded into Issue: Issue is what the *queue
    # row* recorded when it last ran, and this is what the *phone* looks like
    # now. A row can carry both, and reading one as the other sent someone to
    # re-run a post whose phone was flagged.
    head = ("<tr><th class='num'>Due</th><th>Profile</th><th>Reel</th><th>Account</th>"
            "<th>Status</th><th>Why it is stuck</th><th>Issue</th>"
            "<th class='num'>Tries</th></tr>")
    body = "".join(
        f"<tr><td class='num mono'>{_e(p['when'])}</td>"
        f"<td class='mono'>{_e(p['profile'])}</td>"
        f"<td class='mono wrap-cell'>{_e(p['clip'] or '—')}</td>"
        f"<td class='mono'>{_e(p['handle'] or ('second account' if p['slot'] == 'Second' else '—'))}</td>"
        f"<td>{_status_pill(p['status'])}</td>"
        f"<td>" + (f'<span class="pill warn">{_e(p.get("parked_reason"))}</span>'
                   if p.get("parked_reason") else '<span class="dim">—</span>') + "</td>"
        f"<td>{_e(p['issue'] or '—')}</td>"
        f"<td class='num'>{_e(p['retries'])}</td></tr>" for p in rows)

    per_profile = posts.get("by_profile") or []
    # "To go" split the same way as the tiles: a profile with 6 to go and 6 of
    # them parked is the one to open, and one column could not say so.
    tally_head = ("<tr><th>Profile</th><th class='num'>Posts</th><th class='num'>Posted</th>"
                  "<th class='num'>To go</th><th class='num'>Stuck</th>"
                  "<th class='num'>Verifying</th><th class='num'>Failed</th></tr>")
    tally = "".join(
        f"<tr><td class='mono'>{_e(p['profile'])}</td><td class='num'>{p['total']}</td>"
        f"<td class='num'>{p['posted']}</td><td class='num'>{p['pending']}</td>"
        + ("<td class='num'>" + (f"<span class='warn'>{p['parked']}</span>"
                                 if p.get("parked") else "") + "</td>")
        + f"<td class='num'>{p['verifying']}</td><td class='num'>{p['failed']}</td></tr>"
        for p in per_profile)

    return (f'<div class="grid">{"".join(tiles)}</div>' + split + warn
            + '<h3>Every post, in the order it is due</h3>'
            + f'<div class="scroll"><table>{head}{body}</table></div>'
            + '<h3>By profile</h3>'
            + f'<div class="scroll"><table>{tally_head}{tally}</table></div>')


def _retired_note(retired) -> str:
    """One line naming the phones MultiLogin no longer has.

    They are off every table on this page, which is the point -- but a count
    that shrank with no explanation is how the last blind spot started, so the
    page says how many went and what they were called.
    """
    names = list(retired or ())
    if not names:
        return ""
    shown = ", ".join(_e(name) for name in names[:12])
    rest = f" and {len(names) - 12} more" if len(names) > 12 else ""
    return (f'<p class="sub"><span class="pill">{len(names)} retired</span> '
            f'left off the lists above: their MultiLogin profile no longer exists, so '
            f'nothing can launch for them. Parked in Airtable with '
            f'<span class="mono">Issue Reason = Profile Deleted From MLX</span>; the row '
            f'is kept because retiring a phone for good is a client decision. The queue '
            f'rows they left behind are still counted under Posts and Schedules, where '
            f'they read as held — cancelling those is a separate job. '
            f'{shown}{rest}.</p>')


def _section_folders(folders: dict, retired=()) -> str:
    """Every MultiLogin folder, and what its phones are doing."""
    if folders.get("error"):
        return (f'<p class="sub"><span class="pill warn">could not read the profiles</span> '
                f'<span class="mono">{_e(folders["error"])}</span></p>')
    rows = folders.get("folders") or []
    if not rows:
        return ('<p class="empty">No profiles to group. Either Airtable could not be read, '
                'or the MultiLogin folder list could not be.</p>')
    totals = folders.get("totals") or {}

    from adb_bot.automation.report import STAGE_LABELS, STAGE_ORDER

    note = ""
    if not folders.get("known_folders"):
        note = ('<p class="sub"><span class="pill warn">no folder list</span> MultiLogin\'s '
                'folder list could not be read, so every profile is shown as having no '
                'folder. The counts are still right; only the grouping is missing.</p>')

    head = ("<tr><th>Folder</th><th class='num'>Phones</th>"
            + "".join(f"<th class='num'>{_e(STAGE_LABELS[key])}</th>" for key in STAGE_ORDER)
            + "</tr>")

    def _row(entry, klass=""):
        cells = "".join(
            f"<td class='num'>{entry.get(key, 0) or '<span class=\"empty\">—</span>'}</td>"
            for key in STAGE_ORDER)
        return (f"<tr{klass}><td class='mono'>{_e(entry.get('folder'))}</td>"
                f"<td class='num'>{entry.get('total', 0)}</td>{cells}</tr>")

    body = "".join(_row(entry) for entry in rows)
    body += _row(totals, klass=" style='font-weight:600'")
    return (note + f'<p class="sub">{len(rows)} folder(s), {totals.get("total", 0)} phone(s). '
            f'The folder is the model — it is how MultiLogin groups them, and the only '
            f'grouping that survives 46 phones all called "Blank (NN)". A phone is counted '
            f'in exactly one column, worst first: a flagged phone that is also posting is '
            f'somebody\'s job, not a healthy row.</p>'
            + f'<div class="scroll"><table>{head}{body}</table></div>'
            + _retired_note(retired))


def _geelark_unconfigured(geelark: dict) -> str:
    """The one line to add, rather than a bare “not configured”.

    A dashboard section that only says something is missing wastes the reader's
    trip. This says exactly what to do, because the fix is two lines in a file.
    """
    return (
        '<p class="sub"><span class="pill warn">not configured</span> '
        'This host has no Geelark API credentials, so nothing on this tab can '
        'be read live. Add them to <span class="mono">/etc/adbbot/env</span> '
        'and restart the site:</p>'
        '<div class="scroll"><table><tr><td class="mono">'
        'GEELARK_APP_ID=…<br>GEELARK_API_KEY=…<br><br>'
        'systemctl restart adbbot-site'
        '</td></tr></table></div>'
        '<p class="sub">The credentials are read/write and are the only ones '
        'Geelark issues — there is no separate session or bearer token. '
        'Everything below stays blank until they are installed.</p>')


def _geelark_state_pill(state: str, label: str) -> str:
    """Colour a bucket by whether it is good news, work, or a dead end."""
    if state == "connected":
        return f'<span class="pill ok">{_e(label)}</span>'
    if state in ("blocked", "no_credentials"):
        return f'<span class="pill bad">{_e(label)}</span>'
    if state in ("needs_code", "mailbox", "retry"):
        return f'<span class="pill warn">{_e(label)}</span>'
    return f'<span class="pill">{_e(label)}</span>'


def _section_geelark_migration(geelark: dict) -> str:
    """The headline answer: how much of the fleet can actually move.

    This leads the tab because it is the only question the migration turns on.
    Everything else here -- phones, proxies, money -- is the machinery; this is
    the result.

    The three numbers are drawn deliberately, and the third is the one that is
    easy to get wrong. A phone whose *run* failed (never booted, ADB never
    answered, Instagram never opened) is **untested**, not lost, and is kept out
    of "cannot connect" on purpose. Folding it in would let one bad afternoon of
    phone launches read as a verdict on the accounts -- the same mistake the
    "Retries Exhausted" and "Human Verification Required" labels made on the
    MultiLogin side, where a counter and a screen-check got reported as
    diagnoses.
    """
    if not geelark.get("configured"):
        return _geelark_unconfigured(geelark)

    migration = geelark.get("migration") or {}
    rows = migration.get("rows") or []
    if not rows:
        return ('<p class="empty">No phones to assess — either the account is '
                'empty or the phone list could not be read.</p>')

    total = migration.get("migration_total", 0)
    can = migration.get("can_connect", 0)
    cannot = migration.get("cannot_connect", 0)
    untested = migration.get("untested", 0)
    counts = migration.get("counts") or {}

    def _pct(value: int) -> str:
        return f"{(100.0 * value / total):.0f}%" if total else "—"

    headline = (
        f'<p class="sub">Of <strong>{total}</strong> phone(s) carrying a '
        f'migrated account: <span class="pill ok">{can} can connect</span> '
        f'({_pct(can)}) — already signed in, or holding a password Instagram '
        f'has accepted or has not been asked about yet. '
        f'<span class="pill bad">{cannot} cannot</span> ({_pct(cannot)}) — the '
        f'account or its password is the problem. '
        f'<span class="pill warn">{untested} untested</span> ({_pct(untested)}) '
        f'— the phone or the mailbox failed, so the account was never actually '
        f'tried. Those are not losses; they need running again.</p>')

    # Every bucket, with the sentence that says what it means. The blurb matters
    # more than the number: "needs a code" and "wrong password" look equally red
    # in a bare table and mean opposite things for the migration.
    bucket_rows = "".join(
        f'<tr><td>{_geelark_state_pill(state, _gm.STATE_LABELS.get(state, state))}</td>'
        f'<td class="num">{counts.get(state, 0)}</td>'
        f'<td>{_e(_gm.STATE_BLURBS.get(state, ""))}</td></tr>'
        for state in _gm.STATE_ORDER if counts.get(state)
    )
    buckets = (f'<div class="scroll"><table>'
               f'<tr><th>State</th><th class="num">Phones</th><th>What it means</th></tr>'
               f'{bucket_rows}</table></div>')

    reasons = migration.get("reasons") or []
    reason_block = ""
    if reasons:
        reason_rows = "".join(
            f'<tr><td>{_e(why)}</td><td class="num">{count}</td></tr>'
            for why, count in reasons)
        reason_block = (
            '<h3>Why the rest are not connected</h3>'
            '<p class="sub">Grouped by cause rather than listed per phone, '
            'because the shape of the problem is what decides whether this is '
            'worth fixing account by account or needs one change that clears '
            'many at once.</p>'
            f'<div class="scroll"><table>'
            f'<tr><th>Reason</th><th class="num">Phones</th></tr>'
            f'{reason_rows}</table></div>')

    # The per-phone table, which is what somebody works through. No password or
    # mailbox address is rendered -- the remark holds both, and a dashboard is
    # not a secret store.
    detail_rows = "".join(
        f'<tr><td class="mono">{_e(row["name"])}</td>'
        f'<td>{_dash(row["folder"])}</td>'
        f'<td>{_handle_cell(row["handle"])}</td>'
        f'<td>{_geelark_state_pill(row["state"], row["state_label"])}</td>'
        f'<td>{_e(row["why"])}</td>'
        f'<td class="mono">{_dash(row["last_tried"], "never")}</td></tr>'
        for row in rows
    )
    detail = (
        '<h3>Every phone</h3>'
        '<p class="sub">Sorted best-news first. “Last tried” is the day the '
        'login was last attempted — <em>never</em> means this account has not '
        'been tested against Geelark at all.</p>'
        f'<div class="scroll"><table>'
        f'<tr><th>Phone</th><th>Folder</th><th>Handle</th><th>State</th>'
        f'<th>Why</th><th>Last tried</th></tr>{detail_rows}</table></div>')

    return headline + buckets + reason_block + detail


def _section_geelark_folders(geelark: dict) -> str:
    """The same answer per model, because the fleet is run per model.

    A migration that is 80% done overall but has lost one model entirely is a
    different problem from one that is evenly 80% done, and the total cannot
    tell those apart.
    """
    if not geelark.get("configured"):
        return '<p class="empty">Needs Geelark credentials — see above.</p>'

    folders = (geelark.get("migration") or {}).get("folders") or []
    if not folders:
        return '<p class="empty">No folders to show.</p>'

    columns = [state for state in _gm.STATE_ORDER
               if any(folder.get(state) for folder in folders)]
    head = ("<tr><th>Folder</th><th class='num'>Phones</th>"
            + "".join(f"<th class='num'>{_e(_gm.STATE_LABELS.get(state, state))}</th>"
                      for state in columns)
            + "</tr>")
    body = "".join(
        f'<tr><td class="mono">{_e(folder["folder"])}</td>'
        f'<td class="num">{folder["total"]}</td>'
        + "".join(f'<td class="num">{folder.get(state) or ""}</td>'
                  for state in columns)
        + '</tr>'
        for folder in folders)

    return (f'<p class="sub">One row per model folder, mirroring the '
            f'MultiLogin folder names so the two sides can be read side by '
            f'side.</p>'
            f'<div class="scroll"><table>{head}{body}</table></div>')


def _section_geelark_signup(geelark: dict) -> str:
    """Phones set aside to create brand-new accounts, and what they produced.

    Separate from the migration numbers above on purpose. A new account is not
    migration progress -- it does not recover an MLX account, it adds a
    different one -- and adding the two would make a fleet that is losing
    accounts look like one that is holding steady.
    """
    signup = geelark.get("signup") or {}
    migration = geelark.get("migration") or {}
    set_aside = (migration.get("counts") or {}).get("new_account", 0)

    made = migration.get("new_accounts_made", 0)

    lead = ""
    if geelark.get("configured"):
        lead = (f'<p class="sub"><span class="pill ok">{made} account(s) '
                f'made</span> on Geelark phones so far, with '
                f'<strong>{set_aside}</strong> phone(s) tagged '
                f'<span class="mono">new profile</span> still reserved for the '
                f'signup flow. These are counted <em>apart from</em> the '
                f'migration numbers above: a new account does not bring back a '
                f'MultiLogin account, it adds a different one, and letting the '
                f'two share a total would make a fleet that is losing accounts '
                f'look like one holding steady.</p>'
                f'<p class="sub">Accounts are made against a mailbox rather '
                f'than a rented number — an account created on an SMS number '
                f'cannot be recovered by anyone once the number is released, '
                f'and roughly sixteen fleet profiles are already in that '
                f'hole.</p>')

    if not signup.get("exists"):
        return lead + ('<p class="empty">No signup run has been recorded yet — '
                       f'the ledger at <span class="mono">'
                       f'{_e(signup.get("ledger") or "~/.adb_bot/signup/geelark_signups.jsonl")}'
                       '</span> does not exist. It is written only by a run '
                       'started with <span class="mono">--apply</span>; a dry '
                       'run leaves nothing behind.</p>')

    attempts = signup.get("attempts", 0)
    created = signup.get("created", 0)
    by_status = signup.get("by_status") or {}

    rate = f"{(100.0 * created / attempts):.0f}%" if attempts else "—"
    summary = (f'<p class="sub"><strong>{created}</strong> account(s) created '
               f'from <strong>{attempts}</strong> attempt(s) ({rate}). '
               f'Two phones run at a time — Geelark sells four parallel slots, '
               f'and a started phone bills by the minute whether or not '
               f'anything is driving it.</p>')

    status_rows = "".join(
        f'<tr><td class="mono">{_e(status)}</td><td class="num">{count}</td></tr>'
        for status, count in sorted(by_status.items(), key=lambda kv: -kv[1]))
    status_block = (f'<div class="scroll"><table>'
                    f'<tr><th>Outcome</th><th class="num">Runs</th></tr>'
                    f'{status_rows}</table></div>')

    recent = signup.get("recent") or []
    recent_block = ""
    if recent:
        recent_rows = "".join(
            f'<tr><td class="mono">{_e(row["profile"])}</td>'
            f'<td>{_dash(row["folder"])}</td>'
            f'<td>{_handle_cell(row["handle"])}</td>'
            f'<td class="mono">{_e(row["status"])}</td></tr>'
            for row in recent)
        recent_block = ('<h3>Most recent runs</h3>'
                        f'<div class="scroll"><table>'
                        f'<tr><th>Phone</th><th>Folder</th><th>Handle</th>'
                        f'<th>Outcome</th></tr>{recent_rows}</table></div>')

    return lead + summary + status_block + recent_block


def _section_geelark(geelark: dict) -> str:
    """The Geelark account, kept deliberately apart from the MultiLogin fleet.

    Geelark is a second cloud-phone host under evaluation. Its phones have no
    Airtable row, no model and no posting history, so nothing here is added to
    an MLX number anywhere else on the page -- a combined count would be wrong
    in both directions.
    """
    if not geelark.get("configured"):
        return (f'<p class="sub"><span class="pill warn">not configured</span> '
                f'{_e(geelark.get("error") or "No Geelark credentials on this host.")}</p>')

    error_note = ""
    if geelark.get("error"):
        error_note = (f'<p class="sub"><span class="pill warn">partial</span> '
                      f'<span class="mono">{_e(geelark["error"])}</span></p>')

    rows = geelark.get("phones") or []
    if not rows:
        return (error_note or '') + '<p class="empty">No cloud phones on this Geelark account.</p>'

    counts = geelark.get("counts") or {}

    def _adb_pill(state: str) -> str:
        if state == "active":
            return '<span class="pill ok">reachable</span>'
        if state == "adb-not-enabled":
            return '<span class="pill warn">ADB off</span>'
        # A stopped phone cannot answer ADB, which is expected rather than a
        # fault -- showing it as an error made an idle account look broken.
        if state == "phone-not-running":
            return '<span class="empty">phone off</span>'
        if state == "unknown":
            return '<span class="empty">—</span>'
        return f'<span class="pill bad">{_e(state)}</span>'

    def _status_cell(status: str) -> str:
        if status == "started":
            return '<span class="pill ok">started</span>'
        if status == "stopped":
            return '<span class="pill">stopped</span>'
        return f'<span class="pill warn">{_e(status)}</span>'

    def _ig_count_cell(value, exact) -> str:
        if value is None or value < 0:
            return '<span class="empty">—</span>'
        text = f"{value:,}"
        if not exact:
            text = f"~{text}"
        return f'<span class="mono">{text}</span>'

    def _ig_updated_cell(at) -> str:
        if not at:
            return '<span class="empty">—</span>'
        age = max(0.0, time.time() - at)
        return f'<span class="mono">vor {_fmt_seconds(age)}</span>'

    head = ("<tr><th>Phone</th><th>Status</th><th>ADB</th><th>Device</th>"
            "<th>Android</th><th>Country</th><th>Proxy</th><th>Tags</th>"
            "<th>IG Handle</th><th>Follower</th><th>Posts</th><th>Stats</th></tr>")
    body = "".join(
        f"<tr><td class='mono'>{_e(phone['name'])}</td>"
        f"<td>{_status_cell(phone['status'])}</td>"
        f"<td>{_adb_pill(phone['adb'])}</td>"
        f"<td>{_e(phone['device'])}</td>"
        f"<td>{_e(phone['os'])}</td>"
        f"<td>{_e(phone['country'])}</td>"
        f"<td class='mono'>{_e(phone['proxy']) or '<span class=\"empty\">—</span>'}</td>"
        f"<td>{_e(', '.join(phone['tags'])) or '<span class=\"empty\">—</span>'}</td>"
        f"<td class='mono'>{_e(phone.get('ig_handle')) or '<span class=\"empty\">—</span>'}</td>"
        f"<td>{_ig_count_cell(phone.get('ig_followers'), phone.get('ig_followers_exact'))}</td>"
        f"<td>{_ig_count_cell(phone.get('ig_posts'), phone.get('ig_posts_exact'))}</td>"
        f"<td>{_ig_updated_cell(phone.get('ig_stats_at'))}</td></tr>"
        for phone in rows
    )

    summary = (f'<p class="sub">{counts.get("phones", 0)} cloud phone(s) — '
               f'{counts.get("running", 0)} started, {counts.get("stopped", 0)} stopped, '
               f'{counts.get("adb_enabled", 0)} reachable over ADB. '
               f'A started phone bills by the minute whether or not anything is '
               f'driving it, and a phone with ADB off cannot be driven by the bot '
               f'at all — ADB is off per phone until switched on, and switching it '
               f'on needs the phone already started. IG Handle/Follower/Posts '
               f'are read off the profile header the moment each account '
               f'posts — no dedicated scan — so they are only as fresh as '
               f'that account\'s last post, not live.</p>')

    proxies = geelark.get("proxies") or []
    proxy_block = ""
    if proxies:
        proxy_rows = "".join(
            f"<tr><td class='mono'>{_e(entry['endpoint'])}</td>"
            f"<td class='num'>{entry['profiles']}</td></tr>"
            for entry in proxies
        )
        shared = [entry for entry in proxies if entry["profiles"] > 1]
        shared_note = ""
        if shared:
            shared_note = (' <span class="pill warn">shared</span> '
                           f'{len(shared)} endpoint(s) carry more than one profile.')
        proxy_block = (f'<h3>Proxies</h3><p class="sub">'
                       f'{counts.get("proxies", 0)} proxy record(s) across '
                       f'{len(proxies)} endpoint(s) on '
                       f'{counts.get("gateways", 0)} gateway host(s).{shared_note} '
                       f'The gateway host is <em>not</em> the exit IP — separate '
                       f'ports on one host commonly leave from different '
                       f'addresses. Geelark reports the real one through its proxy '
                       f'check, not on the proxy record.</p>'
                       f'<div class="scroll"><table>'
                       f'<tr><th>Endpoint</th><th class="num">Profiles</th></tr>'
                       f'{proxy_rows}</table></div>')

    tags = geelark.get("tags") or []
    tag_block = ""
    if tags:
        tag_block = ('<h3>Tags</h3><p class="sub">'
                     + ", ".join(f'<span class="mono">{_e(tag["name"])}</span>'
                                 for tag in tags)
                     + '. Geelark tags are writable through its API, so the '
                       'MultiLogin habit of using a tag pair to decide who may '
                       'post has an equivalent here — but nothing reads these yet.</p>')

    return (error_note + summary + _geelark_billing(geelark.get("billing") or {},
                                                     counts)
            + f'<div class="scroll"><table>{head}{body}</table></div>'
            + proxy_block + tag_block)


def _geelark_billing(billing: dict, counts: dict) -> str:
    """Money: what is left, and how many phones can run without spending it.

    Unlike MultiLogin -- where an exhausted allowance stops every launch and
    every log blames the server -- Geelark reports this, so it is worth showing
    prominently rather than waiting for launches to start failing.
    """
    if not billing:
        return ('<p class="sub"><span class="pill warn">no billing read</span> '
                'The plan and wallet endpoints could not be read. They are rate '
                'limited to 10 and 1 calls per minute respectively.</p>')

    parallels = billing.get("parallels", 0)
    running = counts.get("running", 0)
    minutes = billing.get("minutes_left", 0)
    credit = billing.get("credit", 0.0)

    over = max(0, running - parallels)
    if over:
        slot_note = (f'<span class="pill bad">{over} over</span> '
                     f'{running} phone(s) running against {parallels} parallel '
                     f'slot(s) — the extra {over} are billing per minute.')
    else:
        slot_note = (f'<span class="pill ok">within slots</span> '
                     f'{running} of {parallels} parallel slot(s) in use — '
                     f'nothing is billing per minute right now.')

    runway = ""
    if minutes:
        hours = minutes / 60.0
        runway = (f' About <strong>{minutes:,} minute(s)</strong> '
                  f'(~{hours:,.0f}h) of per-minute runway at '
                  f'${0.007:.3f}/min, counting ${credit:,.2f} of credit and '
                  f'{billing.get("time_addon_minutes", 0):,} bought minute(s).')
    else:
        runway = (' <span class="pill bad">no runway</span> No credit and no '
                  'bought minutes: any phone outside a parallel slot will fail '
                  'to start.')

    return (f'<p class="sub">{slot_note}{runway}</p>'
            f'<p class="sub">Plan <strong>{_e(billing.get("plan"))}</strong>, '
            f'{billing.get("profiles_available", 0)} of '
            f'{billing.get("profiles", 0)} profile slot(s) free. '
            f'Parallel slots are dynamic — stopping a phone frees its slot — '
            f'and they cover phones started through the API and driven over ADB. '
            f'They do <em>not</em> cover Geelark\'s own RPA tasks, which always '
            f'bill per minute.</p>')


def _counts_cell(counts: dict) -> str:
    """A row's per-status tally as pills, or a dash when it has none."""
    if not counts:
        return "<span class='empty'>—</span>"
    order = ["Posted", "Verifying", "Pending", "Failed"]
    parts = [f"{_status_pill(status)} {counts[status]}"
             for status in order if counts.get(status)]
    parts += [f"{_e(status)} {value}" for status, value in sorted(counts.items())
              if status not in order and value]
    return " ".join(parts)


def _section_second_accounts(data: dict) -> str:
    """Phones running two Instagram accounts, and today's posts for each.

    Every other table on this page counts a phone once, because a phone IS one
    Profiles row -- so a second account that has quietly stopped being scheduled
    is invisible: the phone still posts, still reports Active, still shows a
    healthy row. Splitting the day's rows by account is the only view that shows
    it.
    """
    if not data.get("supported", True):
        return ('<p class="empty">This base has no <span class="mono">Has Second '
                'Account</span> field, so nothing here can be shown.</p>')
    if data.get("error"):
        return (f'<p class="sub"><span class="pill warn">could not read Airtable</span> '
                f'<span class="mono">{_e(data["error"])}</span></p>')

    profiles = data.get("profiles") or []
    if not profiles:
        return ('<p class="empty">No phone is marked as carrying a second account. '
                'Tick <span class="mono">Has Second Account</span> on a profile in '
                'Airtable and fill in both handles to give it one.</p>')

    counts = data.get("counts") or {}
    tiles = [
        _tile("Phones with two accounts", counts.get("phones", 0),
              "one device, two Instagram accounts", "ok"),
        _tile("Ready to post", counts.get("usable", 0),
              "both handles known", "ok"),
        _tile("Not usable yet", counts.get("incomplete", 0),
              "a handle is missing — no posts planned",
              "bad" if counts.get("incomplete") else "ok"),
        _tile("Second-account posts today", counts.get("expected_today", 0),
              "rows scheduled for the second account", ""),
    ]

    body = []
    for profile in profiles:
        if not profile["usable"]:
            state = "<span class='pill bad'>handle missing</span>"
        elif profile["second_queued"]:
            state = "<span class='pill ok'>posting</span>"
        else:
            state = "<span class='pill warn'>nothing queued today</span>"
        body.append(
            f"<tr><td class='mono'>{_e(profile['name'])}</td>"
            f"<td>{_e(profile['status'] or '-')}</td>"
            f"<td class='mono'>{_e(profile['primary'] or '—')}</td>"
            f"<td>{_counts_cell(profile['primary_today'])}</td>"
            f"<td class='mono'>{_e(profile['second'] or '—')}</td>"
            f"<td>{_counts_cell(profile['second_today'])}</td>"
            f"<td>{state}</td>"
            f"<td class='mono'>{_e((profile['checked_at'] or '-')[:16].replace('T', ' '))}</td></tr>")

    # The handles are only ever right as of the day somebody read them off the
    # device, and accounts drop out of these switchers on their own -- that is
    # what the second-accounts loop keeps catching. An eleven-day-old reading
    # presented as current is how a phone posts to an account it no longer has.
    stale = ""
    oldest = min((p["checked_at"] or "" for p in profiles), default="")
    if oldest:
        age = _days_since(oldest[:10])
        if age >= 3:
            stale = (f'<p class="sub"><span class="pill warn">handles {age} day(s) old</span> '
                     f'"Accounts last read" is when somebody last read the account switcher on '
                     f'the device itself, not a live check. Accounts do fall off these phones '
                     f'between readings, and a stale handle is posted against until the switch '
                     f'fails. Re-read them with '
                     f'<span class="mono">run_loop second-accounts --apply</span>.</p>')

    untracked = data.get("untracked") or []
    extra = ""
    if untracked:
        live = [p for p in untracked if p["live"]]
        rows_html = "".join(
            f"<tr><td class='mono'>{_e(p['name'])}</td>"
            f"<td class='mono'>{_e(p['serial'])}</td>"
            f"<td class='mono'>{_e(p['tag'])}</td>"
            f"<td>{_e(p['status'] or '-')}</td>"
            f"<td>{'<span class=\"pill bad\">posting single</span>' if p['live'] else '<span class=\"pill\">parked</span>'}</td></tr>"
            for p in untracked)
        extra = (
            f'<h3>Tagged as two-account in MultiLogin, not ticked in Airtable</h3>'
            f'<p class="sub">The MultiLogin tag is what a person applied when they set the '
            f'phone up; <span class="mono">Has Second Account</span> on the Airtable row is '
            f'what the bot acts on, and nothing carries one to the other. These '
            f'{len(untracked)} phone(s) are two-account phones everywhere except where it '
            f'counts'
            + (f' — and <strong>{len(live)} of them are Active</strong>, so every post their '
               f'second account should be making is simply not being planned. '
               if live else '. ')
            + f'Tick the box and fill in both handles, or read them off the device with '
              f'<span class="mono">run_loop second-accounts --apply</span>.</p>'
            f'<div class="scroll"><table>'
            f'<tr><th>Phone</th><th>Serial</th><th>MultiLogin tag</th><th>Status</th>'
            f'<th>What it does today</th></tr>{rows_html}</table></div>')

    return (
        '<p class="sub">Both accounts live in one cloned Instagram app on one phone. '
        'The bot posts to each of them separately — its own spoofed video, its own '
        'scheduled times, its own check that the post landed — switching accounts on '
        'the phone in between.</p>'
        '<div class="grid">' + "".join(tiles) + '</div>'
        '<div class="scroll"><table>'
        '<tr><th>Phone</th><th>Status</th><th>First account</th><th>Its posts today</th>'
        '<th>Second account</th><th>Its posts today</th><th>State</th>'
        '<th>Accounts last read</th></tr>' + "".join(body) + '</table></div>'
        + stale + extra +
        '<div class="howto"><dl>'
        '<dt>Nothing queued today</dt>'
        '<dd>The second account is set up but has no posts scheduled today. Usually it '
        'is waiting on spoofed video: each account needs its <em>own</em> encode of every '
        'clip, so a second account cannot borrow the first one’s.</dd>'
        '<dt>Handle missing</dt>'
        '<dd>Both handles have to be filled in — the bot needs to name the account it '
        'switches <em>to</em> and the one it switches <em>back to</em>. With one of them '
        'blank the phone is treated as a normal single-account phone, and only the '
        'account it is signed in as posts.</dd>'
        '</dl></div>')


def _section_health(health_data: dict, alerts) -> str:
    rows = health_data["loops"]
    if not rows:
        parts = ['<p class="empty">No loop has reported to the watchdog yet.</p>']
    else:
        head = ("<tr><th>Loop</th><th>State</th><th class='num'>Due</th>"
                "<th class='num'>Produced</th><th>Last seen</th><th>Detail</th></tr>")
        body = []
        for row in rows:
            tone = "bad" if row["bad"] else "ok"
            seen = (datetime.fromtimestamp(row["last_seen"]).strftime("%H:%M:%S")
                    if row["last_seen"] else "-")
            body.append(
                f"<tr><td class='mono'>{_e(row['loop'])}</td>"
                f"<td><span class='pill {tone}'>{_e(row['state'])}</span></td>"
                f"<td class='num'>{row['due']}</td><td class='num'>{row['produced']}</td>"
                f"<td class='mono'>{_e(seen)}</td>"
                f"<td class='wrap-cell'>{_e(row['detail'])}</td></tr>")
        parts = [f'<div class="scroll"><table>{head}{"".join(body)}</table></div>']

    if alerts:
        parts.append('<h2>Recent alerts</h2><div class="alerts">'
                     + "".join(f"<div>{_e(line)}</div>" for line in alerts) + "</div>")
    return "".join(parts)


def _section_content(content: dict) -> str:
    """Drawable first, held second.

    A variant only becomes `Used` on a successful post, so a Failed row leaves
    its variant `Ready` and linked -- counted, but unusable for any future slot.
    Leading with the raw Ready count reads as "there is content" when there may
    be none the queue can actually draw.
    """
    if not content.get("ready"):
        return ('<p class="empty">No spoofed videos in stock at all. The pipeline turns raw '
                'footage from Drive into one video per profile; until new footage lands there, '
                'no run can post.</p>')

    drawable, held = content.get("drawable", 0), content.get("held", 0)
    if not drawable:
        lead = ('<p class="sub"><span class="pill bad">no postable content</span> '
                f'Every one of the {held} spoofed video(s) in stock is already attached to '
                f'a queue row, so the next run has nothing new to post. Attached videos are '
                f'never reused, even when the post failed — only fresh raw footage in Drive '
                f'clears this.</p>')
    else:
        lead = (f'<p class="sub"><strong>{drawable}</strong> spoofed video(s) are free for the '
                f'next run to post. A further <strong>{held}</strong> exist but are already '
                f'attached to a queue row and will never be reused — a video is released only '
                f'by posting successfully, so anything attached to a failed row stays counted '
                f'but unusable.</p>')

    held_by_model = content.get("held_by_model", {})
    head = ("<tr><th>Model</th><th class='num'>Free to post</th>"
            "<th class='num'>Stuck on a row</th></tr>")
    rows = []
    for model, count in content["by_model"].items():
        tone = "" if count else ' class="pill bad"'
        cell = f"<span{tone}>{count}</span>" if not count else str(count)
        rows.append(f"<tr><td class='mono'>{_e(model)}</td><td class='num'>{cell}</td>"
                    f"<td class='num'>{held_by_model.get(model, 0) or ''}</td></tr>")
    return (f'{lead}<div class="scroll"><table>{head}{"".join(rows)}'
            f"<tr><td><strong>Total</strong></td>"
            f"<td class='num'><strong>{drawable}</strong></td>"
            f"<td class='num'><strong>{held}</strong></td></tr></table></div>")


def _coverage(free: int, per_day: int) -> tuple:
    """How long the stock lasts, as words rather than a fraction.

    At this fleet's numbers the honest ratio is almost always between zero and
    one, and "0 days" reads as an outage when it means "today is covered,
    tomorrow is not". Days only once there is more than a day.
    """
    if not per_day:
        return "—", ""
    if not free:
        return "none left", "bad"
    if free < per_day:
        return "under a day", "warn"
    return f"{free // per_day} day(s)", "ok"


def _section_schedules(schedules: dict) -> str:
    """When each model posts, and whether there is content to feed it."""
    if schedules.get("error"):
        return (f'<p class="empty">The schedules could not be read '
                f'({_e(schedules["error"])}).</p>')
    models = schedules.get("models") or []
    if not models:
        return '<p class="empty">No models are set up to post.</p>'

    active = [m for m in models if m["profiles"] and m["known"]]
    idle = [m for m in models if not m["profiles"] and m["known"]]
    stray = [m for m in models if m["profiles"] and not m["known"]]

    fixed = [m for m in active if not m["flexible"]]
    zone, server_zone = schedules.get("timezone") or "local", schedules.get("server_timezone")
    clash = (f' The server\'s own clock is {_e(server_zone)}, so these are not the same times '
             f'as the loop timetable above.'
             if server_zone and not schedules.get("same_clock", True) else "")
    where = (f'Times are {_e(zone)} wall clock, set per model in Airtable under '
             f'<span class="mono">Reel Post Times</span>.{clash}')
    if not active:
        lead = ""
    elif not fixed:
        # The default state of every model row, so it is worth saying plainly
        # rather than leaving somebody to read an empty column as "switched off".
        lead = (f'<p class="sub">No model has picked posting times, so every one posts '
                f'whenever a spoofed video is free, up to a daily cap per profile. That is '
                f'the default, not an outage. {where}</p>')
    else:
        lead = (f'<p class="sub">{len(fixed)} of {len(active)} model(s) post at times somebody '
                f'chose; the rest post whenever a spoofed video is free, up to a daily cap. '
                f'{where}</p>')

    head = ("<tr><th>Model</th><th>Scheduled?</th><th>Posts at</th>"
            "<th class='num'>Profiles</th><th class='num'>Posts/day</th><th>Next</th>"
            "<th class='num'>Free videos</th><th>Stock covers</th></tr>")
    rows = []
    for entry in active + stray:
        # The distinction the table exists to make, said in a word rather than
        # left to be inferred from an empty "Next" cell.
        kind = ('<span class="pill warn">bot decides</span>' if entry["flexible"]
                else '<span class="pill ok">fixed times</span>')
        when = (f'whenever a video is free <span class="sub">· max {entry["per_day"]}/day '
                f'each</span>' if entry["flexible"] else _e(", ".join(entry["times"])))
        # A ceiling and a plan are different numbers and must not look alike.
        per_day = (f'up to {entry["posts_per_day"]}' if entry["flexible"]
                   else str(entry["posts_per_day"]))
        covers, tone = _coverage(entry["free"], entry["posts_per_day"])
        pill = f'<span class="pill {tone}">{_e(covers)}</span>' if tone else _e(covers)
        # On a grid-only loop the Models row buys nothing: slots come from the
        # unit and per-model times are ignored for everybody, so a model without
        # one posts identically to one with it. Red here sent somebody looking
        # for a fault in Nikki, which was posting 3x a day across 14 profiles.
        flag = ""
        if not entry["known"]:
            tone = "warn" if not schedules.get("per_model") else "bad"
            flag = f' <span class="pill {tone}">no Models row</span>'
        rows.append(
            f"<tr><td class='mono'>{_e(entry['model'])}{flag}</td>"
            f"<td>{kind}</td>"
            f"<td>{when}</td>"
            f"<td class='num'>{entry['profiles']}</td>"
            f"<td class='num'>{_e(per_day)}</td>"
            f"<td class='mono'>{_e(entry['next'] or 'see below')}</td>"
            f"<td class='num'>{entry['free']}</td>"
            f"<td>{pill}</td></tr>")

    body = f'{lead}<div class="scroll"><table>{head}{"".join(rows)}</table></div>'
    body += ('<p class="sub">"Free videos" is that model\'s spoofed stock that no queue row '
             'has claimed — the same number the Content stock section totals below. A model '
             'posting at a cap it has no content for simply posts less; it does not fail.</p>')

    # The per-prefix notes below each explain one name. Nobody adds them up, and
    # the total is the number that matters: on 2026-08-16 it was 115 of 224
    # phones, 83 of them under staging names with no model behind them at all.
    if stray and schedules.get("per_model"):
        stray_total = sum(e["profiles"] for e in stray)
        orphan = sum(e["profiles"] for e in stray if not e.get("raw_folder"))
        body += (f'<p class="sub"><span class="pill bad">{stray_total} profile(s) match no '
                 f'Models row</span> across {len(stray)} name(s), so none of them can pick up a '
                 f'per-model posting time — they stay flexible whatever is set in Airtable. '
                 + (f'{orphan} of those are under names with no model behind them at all '
                    f'(staging phones); the rest are a model filed under a second spelling, '
                    f'named below. ' if orphan and orphan != stray_total else '')
                 + f'Each name is explained underneath.</p>')

    for entry in stray:
        # Two names for one person, not a missing model — say which, because the
        # flag alone reads as an inventory gap and sends somebody to create a
        # duplicate row.
        alias = (f' Its raw footage is filed under <span class="mono">{_e(entry["raw_folder"])}'
                 f'</span>, which is the same person under the other name — so the content '
                 f'exists, only the two spellings do not meet.' if entry.get("raw_folder") else "")
        # What the missing row actually costs depends on the loop that is
        # running, and on this box it costs nothing: the grid comes from the
        # unit. Saying "can never pick up per-model times and stays flexible"
        # describes the other runner, and reads as "these profiles are not
        # posting" about profiles that posted all day.
        if not schedules.get("per_model"):
            consequence = (f'That costs nothing while the queue loop fills a fixed grid — '
                           f'slots come from the unit and per-model times are ignored for '
                           f'every model, so these post on the same grid as everyone else. '
                           f'It would only matter if per-model posting times were switched on.')
            tone = "warn"
        else:
            consequence = ('A profile is matched to its model by the first word of its name, '
                           'so this one can never pick up per-model posting times and stays '
                           'flexible.')
            tone = "bad"
        body += (f'<p class="sub"><span class="pill {tone}">{entry["profiles"]} profile(s) named '
                 f'"{_e(entry["model"])} …"</span> are Active in the MLX inventory, but Airtable '
                 f'has no <span class="mono">{_e(entry["model"])}</span> row in Models. '
                 f'{consequence}{alias}</p>')
    # A model is only idle if nobody is posting as it. One whose footage feeds a
    # differently-named set of profiles is the same person twice, not an idle
    # model -- Corina reads as "nothing scheduled" while the 14 Nikki profiles
    # drawing from its Drive folder post three times a day. Listing it with the
    # genuinely empty models is how somebody concludes those posts are not going out.
    aliased = {str(s.get("raw_folder") or "").strip().lower(): s["model"]
               for s in stray if s.get("raw_folder")}
    twins = [m for m in idle if m["model"].strip().lower() in aliased]
    truly_idle = [m for m in idle if m["model"].strip().lower() not in aliased]
    for entry in twins:
        other = aliased[entry["model"].strip().lower()]
        body += (f'<p class="sub"><span class="mono">{_e(entry["model"])}</span> has no Active '
                 f'profile of its own, but it is not idle — its raw footage is what the '
                 f'<span class="mono">{_e(other)}</span> profiles post. Same person, two '
                 f'spellings: the Models row uses one and the MLX profiles the other.</p>')
    if truly_idle:
        body += (f'<p class="sub">{_e(", ".join(m["model"] for m in truly_idle))} — '
                 f'{"a model" if len(truly_idle) == 1 else "models"} in Airtable with no Active '
                 f'profile, so nothing is scheduled for '
                 f'{"it" if len(truly_idle) == 1 else "them"}.</p>')
    if not schedules.get("per_model"):
        body += (f'<p class="sub">This base has no <span class="mono">Reel Post Times</span> '
                 f'field, so every model is on the standing grid: '
                 f'{_e(", ".join(schedules.get("fallback") or []))}.</p>')
    return body


_WARMUP_STATE_PILL = {
    "running": ("ok", "warming up"),
    "blocked": ("bad", "will not run"),
    "finished": ("warn", "warm-up done"),
    "not_started": ("warn", "starts later"),
}

#: What to do about each blocker, in the words of somebody with Airtable open
#: and no interest in the planner's source. Every string here is a *field edit*,
#: because that is the only thing that unblocks any of them.
WARMUP_FIXES = {
    "lifecycle stage Paused": (
        "Set Lifecycle Stage to Active on the account row. This is the only "
        "switch; nothing else is stopping it."),
    "lifecycle stage Banned": (
        "Instagram banned this account. Nothing here starts it again — decide "
        "whether to appeal or retire the account."),
    "automation mode paused": (
        "Set Automation Mode to Posting on the account row."),
    "needs human verification": (
        "Open the phone in MultiLogin and clear whatever Instagram is asking "
        "for, then untick Needs Human Verification."),
    "no MLX API ID on linked profile": (
        "The linked profile has no MLX API ID, so there is no phone to open. "
        "Re-run the mlx-sync loop, or link the account to the right profile."),
    "no creation date": (
        "Set Creation Date on the account row. It is the warm-up start date — "
        "day 1 is that date, not the day you flip the switch."),
}


_PROGRESS_PILL = {
    "ok": ("ok", "on track"),
    "failed": ("bad", "last run failed"),
    "running": ("warn", "running now"),
    "never": ("warn", "never run"),
    # Red, and never merged into "finished". This is the profile the page used
    # to call plan complete purely because the calendar had run past the plan:
    # on 2026-08-11 that was 41 phones reading "plan complete" with a day of the
    # plan they had never once completed, nothing scheduling them and nobody
    # asked to finish them. Without a pill of its own the state renders as the
    # neutral "unknown" and the blindness survives the fix.
    "stalled": ("bad", "past the plan, not finished"),
    "finished": ("", "plan complete"),
}


def _section_warmup_progress(progress: dict) -> str:
    """The warm-up campaign: 45 profiles moving through a plan an hour at a
    time, and which of them have stopped moving."""
    if progress.get("error"):
        return (f'<p class="empty">Could not read the warm-up campaign: '
                f'<span class="mono">{_e(progress["error"])}</span></p>')
    profiles = progress.get("profiles") or []
    counts = progress.get("counts") or {}
    days = progress.get("plan_days") or 0
    # Two different numbers, deliberately kept apart: `plan_days` is how long the
    # plan table is, `finish_day` is the last day of it that asks for warm-up
    # activity and so the day whose completion finishes a profile. They are both
    # 4 on the live plan and would only diverge on a plan whose tail asks for a
    # picture or a reel -- work the warm-up does not gate on.
    finish = int(progress.get("finish_day") or 0)

    banner = ""
    # Loudest thing on the section, because it is the failure that looks like
    # success: the loop runs hourly, plans nothing and exits 0.
    if progress.get("account_driven"):
        banner += ('<p><span class="pill bad">these profiles are not scheduled</span> '
                   'The warm-up service runs without <span class="mono">--targets '
                   'profiles</span>, so it plans against the Accounts table and finds '
                   'nothing. It exits 0 every hour and no watchdog can see it.</p>')
    if progress.get("timer_stopped"):
        banner += ('<p><span class="pill bad">timer not active</span> '
                   'Nothing is firing the warm-up loop at all.</p>')
    # Said out loud rather than absorbed: without the plan there is no day that
    # finishes a warm-up, so every count below reads "nobody is finished" and
    # nothing reaches the hand-off list. That is the safe answer, but only if
    # the reader knows it is an answer about the plan table and not about the
    # phones.
    if progress.get("plan_warning"):
        banner += (f'<p><span class="pill bad">plan not read</span> '
                   f'<span class="mono">{_e(progress["plan_warning"])}</span> — '
                   f'until the Warmup Plan table reads, no profile can be called '
                   f'finished and none will reach the hand-off list.</p>')
    if not profiles:
        return banner + ('<p class="empty">No MultiLogin profile carries the '
                         '<span class="mono">Created</span> tag — that tag is what puts '
                         'a profile on warm-up.</p>')

    # "The plan is N days long" was the whole sentence, and it let a reader take
    # "past day N" for "done". Naming the day that *finishes* a profile is what
    # makes the stalled count above readable as the problem it is.
    if not finish:
        plan_sentence = f'The plan is {days} day(s) long.'
    elif finish == days:
        plan_sentence = (f'The plan is {days} day(s) long, and a profile is finished '
                         f'when it has <em>completed</em> day {finish} — not when the '
                         f'calendar runs past it.')
    else:
        plan_sentence = (f'The plan is {days} day(s) long, and a profile is finished '
                         f'when it has <em>completed</em> day {finish} — the days after '
                         f'that ask for nothing the warm-up runs.')
    lead = (f'<p class="sub">{len(profiles)} profile(s) on warm-up · '
            f'{counts.get("ok", 0)} on track · '
            f'{counts.get("failed", 0)} last run failed · '
            f'{counts.get("stalled", 0)} past the plan, not finished · '
            f'{counts.get("running", 0)} running now · '
            f'{counts.get("never", 0)} never run · '
            f'{counts.get("finished", 0)} finished. '
            + plan_sentence
            + f' Day 1 is <strong>Warm-up Started</strong>, '
            f'stamped on the first run and never moved.</p>')
    when = (f'<p class="sub">Next run <strong>{_e(progress.get("next_run") or "—")}</strong>'
            f' · last fired {_e(progress.get("last_run") or "—")}. '
            f'That is the loop\'s timer: it takes every profile due that tick, so it is '
            f'when <em>all</em> of these run, not one at a time.</p>')

    head = ("<tr><th>Profile</th><th class='num'>Serial</th><th class='num'>Day</th>"
            "<th class='num'>Completed" + _hint(
                "The furthest day of the plan this profile has actually finished, which "
                "is not the same as the day it is on — the day advances at midnight "
                "whether or not the night's run worked. This is the number written to "
                "Airtable's Warm-up Stage and to the profile's MultiLogin tag.")
            + "</th><th class='num'>Runs done</th><th>Last run</th><th>Result</th>"
            "<th>State</th></tr>")
    # The denominator is the day that finishes a profile, not the length of the
    # plan table, so this column and the State pill next to it are judged
    # against the same number: "day 5 of 6" beside "past the plan, not finished"
    # would look like a contradiction on a plan whose last days ask for nothing.
    # They are the same number on the live plan; `plan_days` is spelled out in
    # the lead above when it differs.
    gate = finish or days
    rows = []
    for p in profiles:
        tone, label = _PROGRESS_PILL.get(p["state"], ("warn", "unknown"))
        day = (f'{p["day"]} of {gate}' if p["day"] <= gate else f'{p["day"]} (past {gate})')
        result = _e(p["last_result"] or "—")
        if p["last_notes"]:
            result += _hint(p["last_notes"])
        # Marked, not silently merged: history under a bare name cannot be
        # pinned to one twin, and showing it against each of them would invent
        # runs none of them made.
        name = _e(p["name"]) + (_hint(
            "This profile shares its MultiLogin name with another, and these runs were "
            "logged before runs carried a serial — so this history may belong to its "
            "twin. Runs from now on are recorded per profile."
        ) if p["ambiguous"] else "")
        rows.append(
            f"<tr><td class='mono'>{name}</td>"
            f"<td class='num mono'>{_e(p['serial'])}</td>"
            f"<td class='num'>{_e(day)}</td>"
            f"<td class='num'>{('day ' + str(p['day_done'])) if p.get('day_done') else '—'}</td>"
            f"<td class='num'>{p['runs_done']}</td>"
            f"<td class='num mono'>{_e(p['last_at'] or '—')}</td>"
            f"<td>{result}</td>"
            f"<td><span class='pill {tone}'>{_e(label)}</span></td></tr>")

    note = ""
    if counts.get("stalled"):
        # First of the notes because it is the state that had no name: these
        # are the profiles that read "finished" here while appearing on no
        # worklist at all. They clear on their own now -- the planner hands a
        # profile back the day it lost instead of retiring it on the calendar --
        # so the note says "wait", not "act". Saying otherwise is worse than
        # saying nothing: re-stamping `Warm-up Started` makes the Run Log rows
        # before that date invisible, which throws away every day these phones
        # have already completed and starts all of them over.
        note += (f'<p class="sub">{counts["stalled"]} profile(s) are '
                 f'<strong>past the plan and not finished</strong> — the calendar ran '
                 f'out while a day the plan asks for was never completed. The next '
                 f'ticks re-run the days they still owe, a few profiles an hour, so '
                 f'this number should fall on its own; they reach the '
                 f'<strong>Needs human</strong> tab as each one finishes. Nothing to '
                 f'do unless a profile is still here tomorrow. Do <em>not</em> re-stamp '
                 f'<span class="mono">Warm-up Started</span> — that hides the days they '
                 f'have already done and starts them again from day 1.</p>')
    if counts.get("never"):
        note += (f'<p class="sub">"Never run" is not the same as broken: a profile tagged '
                f'today has simply not had its first tick yet. It becomes a problem when '
                f'it is still there after the next run above.</p>')
    if counts.get("failed"):
        note += ('<p class="sub">A failed run does not stop the calendar — the day advances '
                 'either way, so a profile can reach the end of its plan having completed '
                 'none of it. That is what the <strong>Completed</strong> column is for: '
                 'compare it against the day. A profile whose calendar runs out before that '
                 'number reaches the last day is the <span class="mono">past the plan, not '
                 'finished</span> state above.</p>')
    return (banner + lead + when + f'<div class="scroll"><table>{head}{"".join(rows)}</table></div>'
            + note)


def _section_warmup_waiting(progress: dict) -> str:
    """Phones that exist and are not being warmed up, and whose turn it is."""
    if progress.get("error"):
        return ""
    waiting = progress.get("waiting") or []
    if not waiting:
        return ('<p class="empty">Every MultiLogin profile is either on warm-up, '
                'already posting, or parked. Nothing is sitting unclaimed.</p>')

    untagged = sum(1 for p in waiting if not p["tags"])
    lead = (f'<p class="sub">{len(waiting)} profile(s) exist in MultiLogin, are Active in '
            f'Airtable, and are <strong>not</strong> being warmed up'
            + (f' — {untagged} of them carry no tag at all' if untagged else '') + '. '
            'The warm-up population is the <span class="mono">Created</span> tag, so a '
            'phone joins it when somebody adds that tag and not before. That gate is '
            'deliberate — a phone tagged <span class="mono">gmail</span> has an email '
            'account and no Instagram, and warming it up would drive an empty app — but '
            'nothing else on this page would ever mention these phones.</p>')

    head = ("<tr><th>Profile</th><th class='num'>Serial</th><th>Tags</th>"
            "<th class='num'>Created</th><th>Why it is not warming up</th></tr>")
    rows = "".join(
        f"<tr><td class='mono'>{_e(p['name'])}</td>"
        f"<td class='num mono'>{_e(p['serial'])}</td>"
        f"<td class='mono'>{_e(p['tags'] or '—')}</td>"
        f"<td class='num mono'>{_e(p['created'] or '—')}</td>"
        f"<td>{_e(p['reason'])}</td></tr>" for p in waiting)
    return (lead + f'<div class="scroll"><table>{head}{rows}</table></div>'
            + '<p class="sub">Adding <span class="mono">Created</span> in MultiLogin is the '
              'whole of it: the next warm-up tick picks the profile up, stamps its '
              '<strong>Warm-up Started</strong> and runs day 1.</p>')


def _section_warmup(warmup: dict) -> str:
    """Who is warming up, and for everyone else, the one edit that would start them."""
    if warmup.get("error"):
        return (f'<p class="empty">Could not read the warm-up tables: '
                f'<span class="mono">{_e(warmup["error"])}</span></p>')
    accounts = warmup.get("accounts") or []
    plan = warmup.get("plan") or []
    if not accounts:
        return '<p class="empty">No accounts in the Accounts table to warm up.</p>'

    counts = warmup.get("counts") or {}
    days = warmup.get("plan_days") or 0
    lead = (f'<p class="sub">{len(accounts)} account(s) · '
            f'{counts.get("running", 0)} warming up · '
            f'{counts.get("blocked", 0)} will not run · '
            f'{counts.get("finished", 0)} past the plan · '
            f'{counts.get("not_started", 0)} dated in the future. '
            f'The plan is {days} day(s) long; day 1 is the account\'s '
            f'<strong>Creation Date</strong>, so an account dated before '
            f'{days} day(s) ago is already past warm-up and will do nothing.</p>')

    head = ("<tr><th>Account</th><th>Profile</th><th class='num'>Day</th>"
            "<th>State</th><th>Today</th><th>What to change</th></tr>")
    rows = []
    for acc in accounts:
        tone, label = _WARMUP_STATE_PILL.get(acc.get("state"), ("warn", "unknown"))
        day = acc.get("day")
        # A day number past the plan is noise dressed as data -- "day 51" of a
        # 4-day plan says nothing a person can use -- so say what it means.
        day_cell = "-" if day is None else (f"{day}" if 1 <= day <= days else f"{day}")
        todo = acc.get("blocker") or ""
        if todo:
            fix = WARMUP_FIXES.get(todo, "")
            todo_cell = (f'<strong>{_e(todo)}</strong>'
                         + (f'<br><span class="sub">{_e(fix)}</span>' if fix else ""))
            if acc.get("stale_date"):
                todo_cell += (
                    f'<br><span class="pill warn">and set Creation Date</span> '
                    f'<span class="sub">This account is on day {_e(day)} of a '
                    f'{_e(days)}-day plan, so clearing the above on its own runs '
                    f'<strong>nothing</strong>. Set Creation Date to the day warm-up '
                    f'should start — that date becomes day 1.</span>')
        elif acc.get("state") == "finished":
            todo_cell = ('<span class="sub">Nothing. Warm-up is over for this account; '
                         'it posts from the Posting Queue now.</span>')
        elif acc.get("state") == "not_started":
            todo_cell = ('<span class="sub">Nothing. Its Creation Date is in the future — '
                         'warm-up begins on that date.</span>')
        else:
            todo_cell = '<span class="sub">Nothing — it is running.</span>'
        actions = ", ".join(acc.get("actions") or []) or "-"
        rows.append(
            f"<tr><td class='mono'>{_e(acc.get('name'))}</td>"
            f"<td class='mono'>{_e(acc.get('profile'))}</td>"
            f"<td class='num'>{_e(day_cell)}</td>"
            f"<td><span class='pill {tone}'>{_e(label)}</span></td>"
            f"<td class='wrap-cell'>{_e(actions)}</td>"
            f"<td class='wrap-cell'>{todo_cell}</td></tr>")
    table = f'<div class="scroll"><table>{head}{"".join(rows)}</table></div>'

    plan_html = ""
    if plan:
        phead = "<tr><th class='num'>Day</th><th>What the bot does</th></tr>"
        prows = "".join(
            f"<tr><td class='num'>{_e(p['day'])}</td>"
            f"<td class='wrap-cell'>{_e(', '.join(p['actions']) or 'nothing')}</td></tr>"
            for p in plan)
        plan_html = (f'<h2>The warm-up plan</h2><p class="sub">Read from the '
                     f'<strong>Warmup Plan</strong> table — edit it there and the '
                     f'next run follows it.</p>'
                     f'<div class="scroll"><table>{phead}{prows}</table></div>')
    return lead + table + plan_html


def _section_outlook(outlook: dict) -> str:
    """The next posts as times on a clock, not as a policy."""
    profiles = outlook.get("profiles") or []
    queued = outlook.get("queued") or []
    if not profiles and not queued and outlook.get("mode") != "grid":
        return ('<p class="empty">No profile has a posting history yet, so there is nothing '
                'to work a next time out from.</p>')

    zone = outlook.get("timezone") or "local"
    gap_hours = (outlook.get("gap_minutes") or 0) / 60.0
    ready, waiting, capped = (outlook.get("eligible_now", 0), outlook.get("waiting", 0),
                              outlook.get("capped", 0))
    # Counted apart from the clock states: the posting loop checks the flag
    # first, so these rows are not due-soon, they are not going anywhere.
    blocked_profiles = outlook.get("blocked", 0)
    queued_rows_blocked = outlook.get("queued_blocked", 0)
    # The loop on this box decides one of two ways, and the numbers that answer
    # "when" differ for each. Describing the wrong one is how this page came to
    # report 59 profiles free to post against a gap rule the running loop does
    # not implement, when the true answer was "at 18:00, like every day".
    grid_mode = outlook.get("mode") == "grid"
    slots = ", ".join(outlook.get("slots") or [])

    # A slot time is the audience's clock; every other timestamp on this page is
    # the server's, and this box runs UTC while the slots are Berlin. Unlabelled,
    # "next slot 20:00" read against a UTC "generated 16:09" looks two hours out
    # -- which is exactly how it was read on 2026-08-06. The countdown was right
    # all along; only the clock it was in went unsaid.
    server_zone = outlook.get("server_timezone")
    off_clock = bool(server_zone) and not outlook.get("same_clock", True)

    if grid_mode:
        tiles = [
            _tile("Next slot", f'{outlook.get("next_slot") or "—"}',
                  f'in {_fmt_seconds(outlook.get("next_slot_seconds") or 0)} · {zone}', "ok"),
            _tile("Slots a day", len(outlook.get("slots") or []), slots or "none configured"),
            _tile("Queued now", len(queued), "rows with a time on them",
                  "ok" if queued else ""),
            _tile("Targets", len(profiles), "profiles with a posting history"),
        ]
    else:
        tiles = [
            _tile("Queued now", len(queued), "rows with a time on them",
                  "ok" if queued else ""),
            # "Could" and "will" are different words, and the gap between them on
            # this fleet is content -- see the note below.
            _tile("Could post now", ready, "schedule allows it this minute"),
            # Ahead of the clock tiles: a flag outranks every timing rule below
            # it, and these rows were being counted as imminent for days.
            _tile("Held on a flag", blocked_profiles,
                  f"{queued_rows_blocked} queued row(s) frozen",
                  "bad" if blocked_profiles else ""),
            _tile("Waiting on the gap", waiting, f"posted within {gap_hours:g}h",
                  "warn" if waiting else ""),
            _tile("Done for today", capped, "hit the daily cap", "warn" if capped else ""),
        ]
    body = f'<div class="grid">{"".join(tiles)}</div>'
    if grid_mode:
        body += (f'<p class="sub" style="margin-top:.7rem">The queue loop fills a fixed grid: at '
                 f'each of <span class="mono">{_e(slots)}</span> it writes one row for every '
                 f'target that has a spoofed video free, and between slots it writes nothing — '
                 f'so an empty queue in the middle of the afternoon is the loop working, not the '
                 f'loop stuck. The grid comes from <span class="mono">--slots</span> on '
                 f'<span class="mono">{_e(outlook.get("unit") or "the queue unit")}</span>, not '
                 f'from Airtable.</p>')
        if off_clock:
            body += (f'<p class="sub">Slot times above are <span class="mono">{_e(zone)}</span> — '
                     f'the audience\'s clock. Every other time on this page is the server\'s, '
                     f'which runs <span class="mono">{_e(server_zone)}</span>, '
                     f'{abs(outlook.get("clock_gap_hours") or 0):g}h behind. A slot time and a '
                     f'timestamp on this page are not the same clock.</p>')

    if queued:
        head = ("<tr><th>Queue row</th><th>Scheduled for</th><th>Goes out</th></tr>")
        rows = []
        for row in queued:
            if row.get("blocked"):
                # Never "on the next posting tick": the loop skips this row every
                # tick and will go on skipping it until the flag is cleared.
                cell = f'<span class="pill bad">{_e(row["blocked"])}</span>'
            elif row["due"]:
                cell = _e("on the next posting tick")
            else:
                cell = _e(f'in {_fmt_seconds(row["seconds"])}')
            rows.append(f"<tr><td class='mono'>{_e(row['name'])}</td>"
                        f"<td class='mono'>{_e(row['day'])} {_e(row['when'])}</td>"
                        f"<td>{cell}</td></tr>")
        held_note = ""
        if queued_rows_blocked:
            held_note = (
                f' <strong>{queued_rows_blocked} of these {len(queued)} row(s) are held</strong> '
                f'on a flagged or parked profile: the posting loop refuses them before it reads '
                f'the clock, so they are not a backlog that is about to move — they wait on the '
                f'Needs human tab, not on a tick. Clearing the flag is what releases them, and '
                f'the whole of that profile\'s backlog comes due at once when it does.')
        body += (f'<h3>Queued to post</h3><div class="scroll"><table>{head}'
                 f'{"".join(rows)}</table></div>'
                 '<p class="sub">A row exists and carries a real time. Anything already due '
                 'goes out on the next posting tick; a future one is the retry pass holding a '
                 f'failed row back, which is the only thing here that schedules ahead.{held_note}</p>')

    # Only the profiles whose answer is a time. When most of the fleet is free to
    # post -- which is the normal state here -- a table of forty rows all saying
    # "now" buries the handful that are actually waiting for something.
    # The gap and the daily cap are the flexible runner's rules; on a grid they
    # are not what anybody is waiting for.
    # A blocked profile is not waiting on the gap or the cap, and describing it
    # with gap arithmetic ("2h left of the 2h gap") reads as a profile that is
    # about to post. It gets its own table below.
    held = ([] if grid_mode else
            [entry for entry in profiles
             if entry["state"] not in ("ready", "blocked")])
    frozen = [entry for entry in profiles if entry["state"] == "blocked"]
    if held:
        head = ("<tr><th>Profile</th><th>Last scheduled</th><th>Next possible</th>"
                "<th class='num'>Today</th><th>Why</th></tr>")
        rows = []
        for entry in held[:15]:
            if entry["state"] == "capped":
                why = f'{entry["today"]} of {entry["cap"]} posted today'
            elif entry.get("ahead"):
                # Saying "4h left of the 2h gap" of a row that has not gone out
                # yet is arithmetic that contradicts itself on the page.
                why = f'a row is queued for {entry["last"]}; the gap runs from there'
            else:
                why = f'{_fmt_seconds(entry["seconds"])} left of the {gap_hours:g}h gap' 
            tone = "warn" if entry["state"] == "waiting" else ""
            nxt = (f'<span class="pill {tone}">{_e(entry["next"])}</span>' if tone
                   else _e(entry["next"]))
            rows.append(f"<tr><td class='mono'>{_e(entry['profile'])}</td>"
                        f"<td class='mono'>{_e(entry['last_day'])} {_e(entry['last'])}</td>"
                        f"<td>{nxt}</td>"
                        f"<td class='num'>{entry['today']}</td>"
                        f"<td class='sub'>{_e(why)}</td></tr>")
        body += (f'<h3>Waiting on the clock</h3><div class="scroll"><table>{head}'
                 f'{"".join(rows)}</table></div>')
        if len(held) > 15:
            body += f'<p class="sub">Soonest 15 of {len(held)}.</p>'
    elif not grid_mode:
        body += ('<h3>Waiting on the clock</h3><p class="empty">No profile is waiting on the '
                 'gap or its daily cap.</p>')

    if frozen:
        head = ("<tr><th>Profile</th><th>Last scheduled</th><th class='num'>Rows queued</th>"
                "<th>Why it will not post</th></tr>")
        per_profile: dict = {}
        for row in queued:
            if row.get("blocked"):
                per_profile[row["profile"]] = per_profile.get(row["profile"], 0) + 1
        # Biggest backlog first, not alphabetical: this list is cut at 20 and
        # what matters is which profile is sitting on seventeen frozen rows, not
        # which one comes first in the alphabet. Several here hold none at all --
        # parked long ago, nothing queued since -- and they belong at the bottom.
        frozen = sorted(frozen, key=lambda e: (-per_profile.get(e["profile"], 0),
                                               e["profile"]))
        rows = []
        for entry in frozen[:20]:
            rows.append(f"<tr><td class='mono'>{_e(entry['profile'])}</td>"
                        f"<td class='mono'>{_e(entry['last_day'])} {_e(entry['last'])}</td>"
                        f"<td class='num'>{per_profile.get(entry['profile'], 0)}</td>"
                        f"<td class='sub'>{_e(entry['blocked'])}</td></tr>")
        body += (f'<h3>Held on a flag</h3><div class="scroll"><table>{head}'
                 f'{"".join(rows)}</table></div>')
        if len(frozen) > 20:
            body += f'<p class="sub">First 20 of {len(frozen)}.</p>'
        body += ('<p class="sub">These have queue rows and a clock that says now, and the '
                 'posting loop skips every one of them on every tick — the flag is checked '
                 'before the schedule. Nothing here is a timing problem; the work is on the '
                 '<strong>Needs human</strong> tab.</p>')

    if ready and not grid_mode:
        body += (f'<p class="sub"><strong>{ready}</strong> profile(s) could post the moment a '
                 f'spoofed video is free for them — there is no time to wait for. The queue '
                 f'loop writes the row on its next tick (see the timetable above) and the '
                 f'posting loop takes it on the one after.</p>')

    if grid_mode:
        return body + (
            f'<p class="sub">Times are {_e(zone)}. Every target posts on the same grid, so there '
            f'is no per-profile time to look up. Whether a profile actually gets a row at the '
            f'next slot depends on a spoofed video no other row has claimed, and on this fleet '
            f'that is what usually decides it.</p>')

    body += (f'<p class="sub">Times are {_e(zone)}. "Next possible" is the schedule only — a '
             f'flexible profile may post again {gap_hours:g}h after its last one, up to its '
             f'model\'s daily cap. Whether it actually does depends on a spoofed video no other '
             f'row has claimed, and on this fleet that is usually what decides it, not the '
             f'clock.</p>')
    if not outlook.get("any_fixed"):
        body += ('<p class="sub">Nothing here is posting to a fixed timetable: no model has '
                 'picked <span class="mono">Reel Post Times</span>, so every row is written '
                 'for the moment the queue loop finds a video free — which is why "Scheduled '
                 'for" and "Last post" are times things happened, not times chosen in advance.</p>')
    return body


def _refresh_words(seconds: int) -> str:
    """`30s` / `5 min` -- the page says how often it moves, in the reader's units."""
    if seconds < 60:
        return f"{seconds}s"
    minutes = seconds // 60
    return f"{minutes} min" if minutes > 1 else "1 min"


def render(data: dict, *, live: bool = True, title: str = "ADB bot",
           standalone: bool = True, refresh_seconds: int = REFRESH_SECONDS) -> str:
    """The whole page.

    `live` adds the meta-refresh; a snapshot must not have one -- a shared copy
    that reloads itself once it is off the box just goes blank.

    `refresh_seconds` is both the meta-refresh and what the header claims, so a
    host that rebuilds on a slower beat than the loopback server (the public
    site rebuilds every five minutes) cannot promise a freshness it does not
    deliver.

    `standalone=False` returns the style and body content *without* the document
    skeleton, for hosts that supply their own `<html>`/`<head>`/`<body>`. Same
    markup either way, so the shared copy and the local one cannot drift.
    """
    refresh = (f'<meta http-equiv="refresh" content="{refresh_seconds}">' if live else "")
    triage = data.get("needs_human") or {}
    # Profiles only. The abandoned rows below them are a *consequence* of those
    # profiles -- one flagged account leaves six dead slots behind it -- so
    # adding the two counted the same problem twice and put a number on the
    # banner (72) that nothing else on the page agreed with: the Profiles tab
    # said 22, because 22 accounts is what a person actually has to work
    # through. Fixing the account is the job; the rows are just its wreckage.
    waiting = len(triage.get("profiles") or [])
    # Profiles off the warm-up waiting on a bio, a picture and a first post.
    # Counted alongside the flagged ones because both are the same errand to the
    # person reading this -- open a phone and do something to it -- and both
    # live on the same tab now.
    handoff = len((data.get("handoff") or {}).get("profiles") or [])
    bad = data["health"]["bad"]
    banner = ""
    if waiting or handoff:
        what = " and ".join(
            part for part in (f"{waiting} profile(s) need a person" if waiting else "",
                              f"{handoff} finished warm-up" if handoff else "") if part)
        banner = (f'<p><span class="pill bad">{what}</span> '
                  f'— open the <strong>Needs human</strong> tab.</p>')
    stopped_timers = [t["loop"] for t in (data.get("timers") or []) if t.get("stopped")]
    if stopped_timers:
        banner += (f'<p><span class="pill bad">{len(stopped_timers)} loop(s) not scheduled</span> '
                   f'{_e(", ".join(stopped_timers))} — these produce nothing and cannot alert.</p>')
    if bad:
        names = ", ".join(sorted(r["loop"] for r in bad))
        # Appended, not assigned. A sick loop and a queue of people-work are
        # different problems with different readers, and this line used to
        # replace the worklist banner outright -- so on any day a loop was
        # unhappy, the twenty profiles waiting on somebody vanished from the
        # top of the page.
        banner += (f'<p><span class="pill bad">needs attention</span> '
                   f'{_e(names)} — see Health below.</p>')
    if data.get("airtable_error"):
        banner += (f'<p><span class="pill warn">Airtable unreachable</span> '
                   f'<span class="mono">{_e(data["airtable_error"])}</span> — '
                   f'the local sections below are still accurate.</p>')

    mode = (f"live, refreshes every {_refresh_words(refresh_seconds)}"
            if live else "snapshot — not live")
    # The tab badge is the size of the worklist, so it has to count both kinds
    # of work on it -- a badge that only counted the broken ones would read 0
    # with twenty profiles sitting finished and unclaimed.
    # The hand-tagged warm-up phones count too: they are somebody's finding that
    # no loop will ever act on, so if the badge ignored them the tab would read
    # 0 with twenty-one phones marked in the workspace people work in.
    # Counted as distinct phones, not as list entries added together. A profile
    # can be flagged *and* finished its warm-up -- seven were on 2026-08-16 --
    # and adding the lists made the badge 86 for 79 phones. The badge is read as
    # "how many phones need me", so it counts phones.
    def _names(entries):
        return {str((entry or {}).get("name") or "").strip()
                for entry in (entries or [])} - {""}

    todo = len(_names((data.get("needs_human") or {}).get("profiles"))
               | _names((data.get("handoff") or {}).get("profiles"))
               | _names((data.get("mlx_issues") or {}).get("warmup")))
    badge = f'<span class="count">{todo}</span>' if todo else ""
    # Only the rows nothing will retry. Pending and Verifying are the loop
    # working; badging them would put a permanent number on a healthy day.
    dead_rows = len((data.get("needs_human") or {}).get("rows") or [])
    posts_badge = f'<span class="count">{dead_rows}</span>' if dead_rows else ""
    # Counts only what a person can fix by editing a field. "Finished" and
    # "starts later" are correct states, and badging them would put a permanent
    # red number on a tab where nothing is wrong.
    stuck = ((data.get("warmup") or {}).get("counts") or {}).get("blocked", 0)
    # A failed warm-up run is the thing on this tab someone has to act on, and
    # it outranks the account-side blockers the badge used to carry: those are
    # about a population the profile-driven loop no longer runs from.
    progress = data.get("warmup_progress") or {}
    # Failed *and* stalled. A stalled profile is the worse of the two -- a
    # failed run gets another tick tomorrow, a profile past its plan gets none
    # ever -- and counting only failures is exactly how 41 of them sat behind a
    # badge reading 0 for two days while the tab called them finished.
    progress_counts = progress.get("counts") or {}
    unattended = int(progress_counts.get("failed", 0)) + int(progress_counts.get("stalled", 0))
    # `or` here read the account-side count whenever the profile-driven campaign
    # was healthy, which is the normal state -- so a tab with nothing wrong on it
    # wore a badge of 11 accounts that were paused deliberately months ago and
    # that no loop has run from since. Only fall back when the campaign really is
    # account-driven; on a profile-driven fleet 0 failed and 0 stalled means 0.
    stuck = unattended if not progress.get("account_driven") else (unattended or stuck)
    if progress.get("account_driven") and progress.get("profiles"):
        # Not "a number of profiles need a person" -- one switch does, and every
        # profile is stalled behind it.
        stuck = len(progress["profiles"])
    warmup_badge = f'<span class="count">{stuck}</span>' if stuck else ""

    body = f"""<div class="wrap">
  <h1>{_e(title)}</h1>
  <div class="sub">{_e(data['day'])} · generated {_e(data['generated_at'])} · {_e(mode)}</div>
  {banner}

  <div class="tabnav">
    <input type="radio" name="adbbot-tab" id="tab-server" checked>
    <input type="radio" name="adbbot-tab" id="tab-human">
    <input type="radio" name="adbbot-tab" id="tab-posts">
    <input type="radio" name="adbbot-tab" id="tab-schedules">
    <input type="radio" name="adbbot-tab" id="tab-warmup">
    <input type="radio" name="adbbot-tab" id="tab-profiles">
    <input type="radio" name="adbbot-tab" id="tab-geelark">
    <input type="radio" name="adbbot-tab" id="tab-technical">
    <div class="tabs">
      <label for="tab-server">Server</label>
      <label for="tab-human">Needs human{badge}</label>
      <label for="tab-posts">Posts{posts_badge}</label>
      <label for="tab-schedules">Schedules</label>
      <label for="tab-warmup">Warm-up{warmup_badge}</label>
      <label for="tab-profiles">Profiles</label>
      <label for="tab-geelark">Geelark</label>
      <label for="tab-technical">Technical</label>
    </div>

    <section class="panel" id="panel-server">
      <h2>Right now</h2>
      {_section_now(data['now'])}

      <h2>Server</h2>
      {_section_server(data.get('server') or {}, data.get('cpu_processes') or [])}

      {_section_disks_and_uptime(data)}

      <h2>Top CPU use</h2>
      {_section_cpu_processes(data.get('cpu_processes') or [], data.get('server') or {})}

      <h2>MultiLogin minutes</h2>
      {_section_minutes(data.get('minutes'))}

      <h2>Spoofing</h2>
      {_section_spoof(data.get('spoof') or {})}

      <h2>Open phones</h2>
      {_section_phones(data.get('phones') or [])}

      <h2>Top memory use</h2>
      {_section_top_processes(data.get('top_processes') or [])}
    </section>

    <section class="panel" id="panel-human">
      {_section_needs_human(data)}
    </section>

    <section class="panel" id="panel-posts">
      <h2>Today's posts</h2>
      {_section_posts_today(data.get('posts_today') or {})}

      <h2>Posts that were abandoned</h2>
      {_section_abandoned(data)}
    </section>

    <section class="panel" id="panel-schedules">
      <h2>When each loop runs</h2>
      {_section_timers(data.get('timers') or [])}

      <h2>When each model posts</h2>
      {_section_schedules(data.get('schedules') or {})}

      <h2>When the next posts go out</h2>
      {_section_outlook(data.get('outlook') or {})}
    </section>

    <section class="panel" id="panel-warmup">
      <h2>Warm-up progress</h2>
      {_section_warmup_progress(data.get('warmup_progress') or {})}

      <h2>Waiting to join the warm-up</h2>
      {_section_warmup_waiting(data.get('warmup_progress') or {})}

      <h2>Can each account run?</h2>
      {_section_warmup(data.get('warmup') or {})}
    </section>

    <section class="panel" id="panel-profiles">
      <h2>Phones by folder</h2>
      {_section_folders(data.get('folders') or {}, data.get('retired') or [])}

      <h2>Phones with two accounts</h2>
      {_section_second_accounts(data.get('second_accounts') or {})}
    </section>

    <section class="panel" id="panel-geelark">
      <h2>Can the fleet move?</h2>
      {_section_geelark_migration(data.get('geelark') or {})}

      <h2>By model folder</h2>
      {_section_geelark_folders(data.get('geelark') or {})}

      <h2>New accounts being created</h2>
      {_section_geelark_signup(data.get('geelark') or {})}

      <h2>Geelark cloud phones</h2>
      {_section_geelark(data.get('geelark') or {})}
    </section>

    <section class="panel" id="panel-technical">
      <h2>Live right now</h2>
      {_section_live_work(data.get('live_work') or [], refresh_seconds)}

      <h2>Today</h2>
      {_section_today(data)}

      <h2>Runs</h2>
      {_section_runs(data['runs'])}

      <h2>The same day by posting tick</h2>
      {_section_run_detail(data['runs'])}

      <h2>Success rate by day</h2>
      {_section_daily(data.get('daily') or {})}

      <h2>Failed queue rows</h2>
      {_section_failures(data['queue']['failures'])}

      <h2>Loop health</h2>
      {_section_health(data['health'], data['alerts'])}

      <h2>Content stock</h2>
      {_section_content(data['content'])}
    </section>
  </div>

  <footer>
    Read-only. Sources: Posting Queue, post ledger, logs/loop_posting.log,
    the lock directory, the watchdog state files, /proc and systemd.
  </footer>
</div>"""

    if not standalone:
        return f"<style>{_CSS}</style>\n{body}"
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
{refresh}
<title>{_e(title)} — {_e(data['day'])}</title>
<style>{_CSS}</style>
</head>
<body>
{body}
</body>
</html>"""
