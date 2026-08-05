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
code, .mono { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
              font-size: .85em; }
.empty { color: var(--muted); font-style: italic; }
.alerts { background: var(--card); border: 1px solid var(--line); border-radius: 10px;
          padding: .7rem .9rem; }
.alerts div { font-family: ui-monospace, Menlo, Consolas, monospace; font-size: .8rem;
              padding: .15rem 0; overflow-wrap: anywhere; }
footer { margin-top: 2.5rem; color: var(--muted); font-size: .78rem;
         border-top: 1px solid var(--line); padding-top: .8rem; }

/* Tabs without JavaScript: three radios drive which panel is displayed. The
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
#tab-profiles:checked ~ #panel-profiles,
#tab-technical:checked ~ #panel-technical { display: block; }
#tab-server:checked ~ .tabs label[for="tab-server"],
#tab-profiles:checked ~ .tabs label[for="tab-profiles"],
#tab-technical:checked ~ .tabs label[for="tab-technical"] {
  color: var(--fg); border-bottom-color: var(--accent); }
#tab-server:focus-visible ~ .tabs label[for="tab-server"],
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


def _section_server(server: dict) -> str:
    """The box itself. Memory leads because this machine has been OOM-killed."""
    mem_pct = server.get("mem_percent", 0.0)
    swap_total = server.get("swap_total_mb", 0)
    swap_used = server.get("swap_used_mb", 0)
    swap_pct = (100.0 * swap_used / swap_total) if swap_total else 0.0
    cores = server.get("cores", 0) or 1
    load = server.get("load1", 0.0)

    tiles = [
        _tile("Processes", server.get("processes", 0), "running on the box"),
        _tile("CPU", f'{server.get("cpu_percent", 0.0):.0f}%',
              f"load {load:.2f} over {cores} core(s)",
              "bad" if load > cores * 2 else "warn" if load > cores else "ok"),
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


def _section_timers(timers) -> str:
    if not timers:
        return '<p class="empty">No scheduled loops found.</p>'
    head = ("<tr><th>Loop</th><th>State</th><th class='num'>Every</th>"
            "<th>Last run</th><th>Next run</th></tr>")
    rows = []
    for timer in timers:
        tone = "bad" if timer["stopped"] else "ok"
        every = (f'{timer["interval_min"]} min' if timer["interval_min"] < 1440
                 else "daily") if timer["interval_min"] else "-"
        rows.append(
            f"<tr><td class='mono'>{_e(timer['loop'])}</td>"
            f"<td><span class='pill {tone}'>{_e(timer['state'])}</span></td>"
            f"<td class='num'>{_e(every)}</td>"
            f"<td class='mono'>{_e(timer['last'] or '-')}</td>"
            f"<td class='mono'>{_e(timer['next'] or 'running now')}</td></tr>")
    stopped = [t["loop"] for t in timers if t["stopped"]]
    note = (f'<p class="sub"><span class="pill bad">{len(stopped)} stopped</span> '
            f'{_e(", ".join(stopped))} — a stopped loop produces nothing and raises no alert, '
            f'because alerts are only recorded when a loop actually runs.</p>'
            if stopped else
            '<p class="sub">All loops are scheduled. An empty "next run" means that loop is '
            'executing right now.</p>')
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
    "skipped": ("warn", "skipped"),
    "unknown": ("warn", "no result"),
}


def _run_detail(run, index: int) -> str:
    """One run as a collapsible card: the counts, then every profile in it."""
    counts = run.counts()
    # A run still in flight has profiles with no result *yet*. Calling those
    # "no result" would read as a fault; they are simply mid-post.
    running = not run.finished
    unknown_label = "still going" if running else "no result"
    pills = []
    for outcome, label in (("posted", "posted"), ("verifying", "verifying"),
                           ("failed", "did not post"), ("skipped", "skipped"),
                           ("unknown", unknown_label)):
        if counts.get(outcome):
            tone = _OUTCOME_PILL[outcome][0]
            pills.append(f'<span class="pill {tone}">{counts[outcome]} {label}</span>')
    if not pills:
        pills.append('<span class="pill warn">no profile reached a phone</span>')

    finished = _e(run.finished[11:16]) if run.finished else "running"
    summary = (f'<summary>Run {index} '
               f'<span class="when">{_e(run.started[11:16])} → {finished}</span> '
               f'{"".join(pills)}</summary>')

    if not run.profiles:
        body = ('<p class="empty">No profile is named in this run\'s log — it '
                'planned work but nothing reached a phone.</p>')
    else:
        head = ("<tr><th>Profile</th><th>Result</th><th>What happened</th>"
                "<th>What happens next</th></tr>")
        rows = []
        for entry in run.sorted_profiles():
            tone, label = _OUTCOME_PILL.get(entry.outcome, ("warn", entry.outcome))
            if entry.outcome == "unknown":
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
        body = f'<div class="scroll"><table>{head}{"".join(rows)}</table></div>'

    # Open the runs that lost something; a clean run is a line, not a page.
    unresolved = counts.get("failed", 0) + (0 if running else counts.get("unknown", 0))
    return f'<details class="run"{" open" if unresolved else ""}>{summary}{body}</details>'


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
    lead = (f'<p class="sub">{len(runs)} run(s) today · {totals["posted"]} posted · '
            f'{totals["verifying"]} verifying · {missed} did not post{still}. '
            'Counted per profile per run, so a profile that failed twice appears '
            'twice — unlike the Posts column above, which counts profiles a run '
            'worked on, not posts that landed. Runs that lost a profile are open; '
            'the rest fold away. "What happens next" is the retry pass\'s own '
            'verdict, so a red row with a green pill needs nobody.</p>')
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


def render(data: dict, *, live: bool = True, title: str = "ADB bot",
           standalone: bool = True) -> str:
    """The whole page.

    `live` adds the meta-refresh; a snapshot must not have one -- a shared copy
    that reloads itself once it is off the box just goes blank.

    `standalone=False` returns the style and body content *without* the document
    skeleton, for hosts that supply their own `<html>`/`<head>`/`<body>`. Same
    markup either way, so the shared copy and the local one cannot drift.
    """
    refresh = (f'<meta http-equiv="refresh" content="{REFRESH_SECONDS}">' if live else "")
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

    mode = "live, refreshes every 30s" if live else "snapshot — not live"
    flagged = len((data.get("needs_human") or {}).get("profiles") or [])
    badge = f'<span class="count">{flagged}</span>' if flagged else ""

    body = f"""<div class="wrap">
  <h1>{_e(title)}</h1>
  <div class="sub">{_e(data['day'])} · generated {_e(data['generated_at'])} · {_e(mode)}</div>
  {banner}

  <div class="tabnav">
    <input type="radio" name="adbbot-tab" id="tab-server" checked>
    <input type="radio" name="adbbot-tab" id="tab-profiles">
    <input type="radio" name="adbbot-tab" id="tab-technical">
    <div class="tabs">
      <label for="tab-server">Server</label>
      <label for="tab-profiles">Profiles{badge}</label>
      <label for="tab-technical">Technical</label>
    </div>

    <section class="panel" id="panel-server">
      <h2>Right now</h2>
      {_section_now(data['now'])}

      <h2>Server</h2>
      {_section_server(data.get('server') or {})}

      {_section_disks_and_uptime(data)}

      <h2>Scheduled loops</h2>
      {_section_timers(data.get('timers') or [])}

      <h2>Open phones</h2>
      {_section_phones(data.get('phones') or [])}

      <h2>Top memory use</h2>
      {_section_top_processes(data.get('top_processes') or [])}
    </section>

    <section class="panel" id="panel-profiles">
      {_section_profiles(data)}
    </section>

    <section class="panel" id="panel-technical">
      <h2>Today</h2>
      {_section_today(data)}

      <h2>Runs</h2>
      {_section_runs(data['runs'])}

      <h2>Run by run</h2>
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
