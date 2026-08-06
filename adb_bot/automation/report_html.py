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
from collections import Counter
from datetime import datetime

from adb_bot.automation import schedule_spec

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
#tab-schedules:checked ~ #panel-schedules,
#tab-profiles:checked ~ #panel-profiles,
#tab-technical:checked ~ #panel-technical { display: block; }
#tab-server:checked ~ .tabs label[for="tab-server"],
#tab-schedules:checked ~ .tabs label[for="tab-schedules"],
#tab-profiles:checked ~ .tabs label[for="tab-profiles"],
#tab-technical:checked ~ .tabs label[for="tab-technical"] {
  color: var(--fg); border-bottom-color: var(--accent); }
#tab-server:focus-visible ~ .tabs label[for="tab-server"],
#tab-schedules:focus-visible ~ .tabs label[for="tab-schedules"],
#tab-profiles:focus-visible ~ .tabs label[for="tab-profiles"],
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
    return body


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
        _tile("Posts today", totals["posts"], f'{totals["runs"]} run(s)'),
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
        "The bot tried to post three times and failed every time. It has stopped "
        "trying so it does not keep hammering the account.",
        "Open the phone and see what state Instagram is in — logged out, an update "
        "prompt, a frozen screen. Fix whatever blocks it, then clear the checkbox."),
    "Repeated Failures": (
        "This profile keeps failing across different runs, so something about it is "
        "consistently wrong rather than unlucky.",
        "Open the phone and post one reel by hand. Whatever stops you is what stops "
        "the bot."),
    "Device Unreachable": (
        "The phone itself did not answer. Instagram may be perfectly fine — the "
        "device never came up.",
        "Check the phone in MultiLogin. Start it manually and see whether it boots; "
        "if it never does, the phone needs recreating."),
}
DEFAULT_GUIDE = ("This profile was flagged for review.",
                 "Open the phone in MultiLogin and see what state Instagram is in.")


def _section_profiles(data: dict) -> str:
    """The worklist, for the person who fixes accounts rather than code."""
    triage = data.get("needs_human") or {}
    profiles = triage.get("profiles") or []
    rows = triage.get("rows") or []
    retrying = triage.get("retrying") or []
    queue = data.get("queue") or {}
    verifying = (queue.get("by_status") or {}).get("Verifying", 0)

    if triage.get("error"):
        return (f'<p class="sub"><span class="pill warn">could not read Airtable</span> '
                f'<span class="mono">{_e(triage["error"])}</span></p>')

    reasons = {}
    for profile in profiles:
        reasons.setdefault(profile["reason"], []).append(profile)

    tiles = [
        _tile("Need you", len(profiles), "profiles flagged for review",
              "bad" if profiles else "ok"),
        _tile("Being checked", verifying,
              "posted, waiting on confirmation", "warn" if verifying else "ok"),
        _tile("Retrying by itself", len(retrying),
              "no action needed", "ok"),
    ]
    parts = [
        '<p class="lead">This page lists the accounts that need a person. '
        'Everything else the bot handles on its own.</p>',
        f'<div class="grid">{"".join(tiles)}</div>',
    ]

    if not profiles:
        parts.append('<h2>Nothing to do</h2><p class="empty">No account is waiting on you '
                     'right now. Anything failing is either retrying automatically or '
                     'already parked.</p>')
    else:
        parts.append('<h2>Accounts to fix</h2>')
        parts.append('<p class="sub">Grouped by what is wrong. Work top to bottom — the '
                     'first group is the most serious.</p>')
        order = ["Banned / Blocked", "Human Verification Required", "Device Unreachable",
                 "Repeated Failures", "Retries Exhausted"]
        for reason in sorted(reasons, key=lambda r: order.index(r) if r in order else 99):
            group = reasons[reason]
            what, todo = ISSUE_GUIDE.get(reason, DEFAULT_GUIDE)
            names = "".join(
                f"<tr><td class='mono'>{_e(p['name'])}</td>"
                f"<td>{_e(p['status'])}</td>"
                f"<td class='mono'>{_e(p['flagged_at'] or '-')}</td>"
                f"<td class='wrap-cell'>{_e(p['note'][0] if p['note'] else '')}</td></tr>"
                for p in group)
            parts.append(
                f'<h3>{_e(reason)} — {len(group)} account(s)</h3>'
                f'<div class="howto"><dl>'
                f'<dt>What happened</dt><dd>{_e(what)}</dd>'
                f'<dt>What to do</dt><dd>{_e(todo)}</dd>'
                f'<dt>When it is fixed</dt><dd>Untick <span class="mono">Needs Human Check</span> '
                f'on that profile in Airtable (Profiles (Cloning)). Nothing unticks it for you — '
                f'that box is how you tell everyone else you have looked.</dd>'
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
        'slow, MultiLogin hiccuped. The bot tries up to three times, half an hour apart, '
        'before it gives up and asks for you.</dd>'
        '<dt>Profile status Active / Inactive</dt>'
        '<dd>Inactive means the bot ignores this profile completely: no posts are planned '
        'for it. That is how you park an account you are still fixing, or one that is gone '
        'for good.</dd>'
        '<dt>Why an account can be flagged but still posting</dt>'
        '<dd>The flag is about one bad run. If the next run works, the account keeps '
        'posting — the flag simply stays until somebody unticks it.</dd>'
        '</dl></div>')

    if rows:
        parts.append('<h2>Posts that were abandoned</h2>')
        parts.append(f'<p class="sub">{len(rows)} scheduled post(s) will not be tried again. '
                     f'They are listed here so nothing disappears silently; fixing the '
                     f'account above is what matters, not these rows.</p>')
        body = "".join(
            f"<tr><td class='mono'>{_e(r['name'])}</td><td class='mono'>{_e(r['slot'])}</td>"
            f"<td>{_e(r['issue'])}</td><td class='num'>{_e(r['retries'])}</td></tr>"
            for r in rows)
        parts.append('<div class="scroll"><table>'
                     '<tr><th>Post</th><th>Was due</th><th>Why it stopped</th>'
                     "<th class='num'>Tries</th></tr>" + body + "</table></div>")
    return "".join(parts)


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
        flag = (' <span class="pill bad">not a model</span>' if not entry["known"] else "")
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

    for entry in stray:
        # Two names for one person, not a missing model — say which, because the
        # flag alone reads as an inventory gap and sends somebody to create a
        # duplicate row.
        alias = (f' Its raw footage is filed under <span class="mono">{_e(entry["raw_folder"])}'
                 f'</span>, which is the same person under the other name — so the content '
                 f'exists, only the two spellings do not meet.' if entry.get("raw_folder") else "")
        body += (f'<p class="sub"><span class="pill bad">{entry["profiles"]} profile(s) named '
                 f'"{_e(entry["model"])} …"</span> are Active in the MLX inventory, but Airtable '
                 f'has no <span class="mono">{_e(entry["model"])}</span> row in Models. A profile '
                 f'is matched to its model by the first word of its name, so this one can never '
                 f'pick up per-model posting times and stays flexible.{alias}</p>')
    if idle:
        body += (f'<p class="sub">{_e(", ".join(m["model"] for m in idle))} — '
                 f'{"a model" if len(idle) == 1 else "models"} in Airtable with no Active '
                 f'profile, so nothing is scheduled for {"it" if len(idle) == 1 else "them"}.</p>')
    if not schedules.get("per_model"):
        body += (f'<p class="sub">This base has no <span class="mono">Reel Post Times</span> '
                 f'field, so every model is on the standing grid: '
                 f'{_e(", ".join(schedules.get("fallback") or []))}.</p>')
    return body


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
            when = ("on the next posting tick" if row["due"]
                    else f'in {_fmt_seconds(row["seconds"])}')
            rows.append(f"<tr><td class='mono'>{_e(row['name'])}</td>"
                        f"<td class='mono'>{_e(row['day'])} {_e(row['when'])}</td>"
                        f"<td>{_e(when)}</td></tr>")
        body += (f'<h3>Queued to post</h3><div class="scroll"><table>{head}'
                 f'{"".join(rows)}</table></div>'
                 '<p class="sub">A row exists and carries a real time. Anything already due '
                 'goes out on the next posting tick; a future one is the retry pass holding a '
                 'failed row back, which is the only thing here that schedules ahead.</p>')

    # Only the profiles whose answer is a time. When most of the fleet is free to
    # post -- which is the normal state here -- a table of forty rows all saying
    # "now" buries the handful that are actually waiting for something.
    # The gap and the daily cap are the flexible runner's rules; on a grid they
    # are not what anybody is waiting for.
    held = [] if grid_mode else [entry for entry in profiles if entry["state"] != "ready"]
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
    waiting = len(triage.get("rows") or []) + len(triage.get("profiles") or [])
    bad = data["health"]["bad"]
    banner = ""
    if waiting:
        banner = (f'<p><span class="pill bad">{waiting} item(s) need a person</span> '
                  f'— open the <strong>Profiles</strong> tab.</p>')
    stopped_timers = [t["loop"] for t in (data.get("timers") or []) if t.get("stopped")]
    if stopped_timers:
        banner += (f'<p><span class="pill bad">{len(stopped_timers)} loop(s) not scheduled</span> '
                   f'{_e(", ".join(stopped_timers))} — these produce nothing and cannot alert.</p>')
    if bad:
        names = ", ".join(sorted(r["loop"] for r in bad))
        banner = (f'<p><span class="pill bad">needs attention</span> '
                  f'{_e(names)} — see Health below.</p>')
    if data.get("airtable_error"):
        banner += (f'<p><span class="pill warn">Airtable unreachable</span> '
                   f'<span class="mono">{_e(data["airtable_error"])}</span> — '
                   f'the local sections below are still accurate.</p>')

    mode = (f"live, refreshes every {_refresh_words(refresh_seconds)}"
            if live else "snapshot — not live")
    flagged = len((data.get("needs_human") or {}).get("profiles") or [])
    badge = f'<span class="count">{flagged}</span>' if flagged else ""

    body = f"""<div class="wrap">
  <h1>{_e(title)}</h1>
  <div class="sub">{_e(data['day'])} · generated {_e(data['generated_at'])} · {_e(mode)}</div>
  {banner}

  <div class="tabnav">
    <input type="radio" name="adbbot-tab" id="tab-server" checked>
    <input type="radio" name="adbbot-tab" id="tab-schedules">
    <input type="radio" name="adbbot-tab" id="tab-profiles">
    <input type="radio" name="adbbot-tab" id="tab-technical">
    <div class="tabs">
      <label for="tab-server">Server</label>
      <label for="tab-schedules">Schedules</label>
      <label for="tab-profiles">Profiles{badge}</label>
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

      <h2>Spoofing</h2>
      {_section_spoof(data.get('spoof') or {})}

      <h2>Open phones</h2>
      {_section_phones(data.get('phones') or [])}

      <h2>Top memory use</h2>
      {_section_top_processes(data.get('top_processes') or [])}
    </section>

    <section class="panel" id="panel-schedules">
      <h2>When each loop runs</h2>
      {_section_timers(data.get('timers') or [])}

      <h2>When each model posts</h2>
      {_section_schedules(data.get('schedules') or {})}

      <h2>When the next posts go out</h2>
      {_section_outlook(data.get('outlook') or {})}
    </section>

    <section class="panel" id="panel-profiles">
      {_section_profiles(data)}
    </section>

    <section class="panel" id="panel-technical">
      <h2>Today</h2>
      {_section_today(data)}

      <h2>Run by run — one video at a time</h2>
      {_section_videos(data.get('videos') or [], data.get('day') or '')}

      <h2>Runs</h2>
      {_section_runs(data['runs'])}

      <h2>The same day by posting tick</h2>
      {_section_run_detail(data['runs'])}

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
