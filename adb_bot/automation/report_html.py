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
    # Phones above the slots we believe we hold is the leak signature; phones
    # above the ceiling means the cap is not doing its job at all.
    if ceiling and phones > ceiling:
        phone_tone = "bad"
    elif phones > slots:
        phone_tone = "warn"
    else:
        phone_tone = "ok"

    active = now["active_loops"]
    tiles = [
        _tile("Loops running", ", ".join(active) if active else "idle",
              "systemd ActiveState", "ok" if active else ""),
        _tile("Profiles locked", len(now["profiles"]), "held by a running loop"),
        _tile("Live phones", phones,
              f"{slots} slot(s) held, ceiling {ceiling}", phone_tone),
        _tile("MultiLogin agent", "up" if now["agent_up"] else "DOWN",
              "listening on :45001", "ok" if now["agent_up"] else "bad"),
    ]
    body = f'<div class="grid">{"".join(tiles)}</div>'
    if phones > slots:
        body += (f'<p class="sub" style="margin-top:.7rem">'
                 f'{phones - slots} phone(s) open with no slot held — either a run is '
                 f'launching right now, or these leaked from an earlier run and nothing '
                 f'will reap them.</p>')
    if now["profiles"]:
        body += ('<div class="scroll"><table><tr><th>Profile locks held</th></tr>'
                 + "".join(f'<tr><td class="mono">{_e(p)}</td></tr>' for p in now["profiles"])
                 + "</table></div>")
    return body


def _section_today(data: dict) -> str:
    totals, ledger = data["totals"], data["ledger"]
    queue = data["queue"]
    posted = queue["by_status"].get("Posted", 0)
    verifying = queue["by_status"].get("Verifying", 0)
    failed = queue["by_status"].get("Failed", 0)

    rate = totals["mlx_rate"]
    tiles = [
        _tile("Posts today", totals["posts"], f'{totals["runs"]} run(s)'),
        _tile("Avg per post", _fmt_seconds(totals["seconds_per_post"]),
              f'{_fmt_seconds(totals["seconds"])} of run time'),
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
    return f'<div class="grid">{"".join(tiles)}</div>'


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


def _section_slots(by_slot: dict) -> str:
    if not by_slot:
        return '<p class="empty">No queue rows scheduled today.</p>'
    statuses = sorted({s for counts in by_slot.values() for s in counts})
    head = "<tr><th>Slot</th>" + "".join(f"<th class='num'>{_e(s)}</th>" for s in statuses) + \
           "<th class='num'>Total</th></tr>"
    rows = []
    for slot, counts in by_slot.items():
        cells = "".join(f"<td class='num'>{counts.get(s, 0) or ''}</td>" for s in statuses)
        rows.append(f"<tr><td class='mono'>{_e(slot or '-')}</td>{cells}"
                    f"<td class='num'>{sum(counts.values())}</td></tr>")
    return f'<div class="scroll"><table>{head}{"".join(rows)}</table></div>'


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
    if not content["ready"]:
        return ('<p class="empty">No unused Ready variants — the next slot has '
                'nothing to post until the pipeline picks up new raw videos.</p>')
    head = "<tr><th>Model</th><th class='num'>Ready variants</th></tr>"
    rows = "".join(f"<tr><td class='mono'>{_e(model)}</td><td class='num'>{count}</td></tr>"
                   for model, count in content["by_model"].items())
    return (f'<div class="scroll"><table>{head}{rows}'
            f"<tr><td><strong>Total</strong></td>"
            f"<td class='num'><strong>{content['ready']}</strong></td></tr></table></div>")


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
    bad = data["health"]["bad"]
    banner = ""
    if bad:
        names = ", ".join(sorted(r["loop"] for r in bad))
        banner = (f'<p><span class="pill bad">needs attention</span> '
                  f'{_e(names)} — see Health below.</p>')
    if data.get("airtable_error"):
        banner += (f'<p><span class="pill warn">Airtable unreachable</span> '
                   f'<span class="mono">{_e(data["airtable_error"])}</span> — '
                   f'the local sections below are still accurate.</p>')

    mode = "live, refreshes every 30s" if live else "snapshot — not live"
    body = f"""<div class="wrap">
  <h1>{_e(title)} — daily report</h1>
  <div class="sub">{_e(data['day'])} · generated {_e(data['generated_at'])} · {_e(mode)}</div>
  {banner}

  <h2>Right now</h2>
  {_section_now(data['now'])}

  <h2>Today</h2>
  {_section_today(data)}

  <h2>Runs</h2>
  {_section_runs(data['runs'])}

  <h2>Slots</h2>
  {_section_slots(data['queue']['by_slot'])}

  <h2>Failed profiles</h2>
  {_section_failures(data['queue']['failures'])}

  <h2>Health</h2>
  {_section_health(data['health'], data['alerts'])}

  <h2>Content stock</h2>
  {_section_content(data['content'])}

  <footer>
    Read-only. Sources: Posting Queue, post ledger, logs/loop_posting.log,
    the lock directory, the watchdog state files and systemd.
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
