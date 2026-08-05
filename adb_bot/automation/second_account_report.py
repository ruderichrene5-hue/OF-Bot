"""Render the second-account dashboard as a standalone HTML page.

`run_loop second-accounts --report <path>` writes this. It is the operator view
of the two-account phones: which are posting twice, which are tagged but held
up, and which profiles the bot has flagged for a manual check -- the last of
which is the point, because a flagged profile posts nothing until somebody opens
it, and a number in a log line does not make that visible.

Pure rendering: `collect()` does the Airtable/MLX reads, `render()` turns the
result into HTML, and neither launches a phone.
"""

from __future__ import annotations

import html
import json

from adb_bot.automation import second_accounts
from adb_bot.clients import airtable as at


def collect(airtable, mlx_items) -> dict:
    """Everything the page shows, as a plain dict (also handy as JSON)."""
    scan = second_accounts.scan_profiles(mlx_items)

    rows = airtable._list_table(
        at.TABLE_PROFILES,
        fields=[at.F_PROF_NAME, at.F_PROF_MLX_SERIAL, at.F_PROF_STATUS,
                at.F_PROF_HAS_SECOND, at.F_PROF_PRIMARY_HANDLE,
                at.F_PROF_SECOND_HANDLE, at.F_PROF_ACCOUNTS_CHECKED,
                at.F_PROF_NEEDS_HUMAN, at.F_PROF_ISSUE_REASON,
                at.F_PROF_ISSUE_NOTES, at.F_PROF_FLAGGED_AT])

    by_serial, needs_human = {}, []
    for rec in rows:
        f = rec.get("fields", {}) or {}
        serial = str(f.get(at.F_PROF_MLX_SERIAL) or "").strip()
        entry = {
            "name": str(f.get(at.F_PROF_NAME) or "").strip(),
            "status": at._select_name(f.get(at.F_PROF_STATUS)) or "Active",
            "has_second": bool(f.get(at.F_PROF_HAS_SECOND)),
            "primary": str(f.get(at.F_PROF_PRIMARY_HANDLE) or "").strip(),
            "second": str(f.get(at.F_PROF_SECOND_HANDLE) or "").strip(),
            "checked_at": f.get(at.F_PROF_ACCOUNTS_CHECKED),
            "needs_human": bool(f.get(at.F_PROF_NEEDS_HUMAN)),
            "issue_reason": at._select_name(f.get(at.F_PROF_ISSUE_REASON)),
            "issue_notes": str(f.get(at.F_PROF_ISSUE_NOTES) or "")[:400],
            "flagged_at": f.get(at.F_PROF_FLAGGED_AT),
        }
        if serial:
            by_serial[serial] = entry
        if entry["needs_human"]:
            needs_human.append({**entry, "serial": serial})

    phones = []
    for p in scan.tagged:
        info = by_serial.get(p.serial_no, {})
        primary, second = info.get("primary", ""), info.get("second", "")
        status = info.get("status", "?")
        # Order matters: a flagged phone is blocked whatever else is true of it.
        # It posts nothing at all until a person clears the box -- including the
        # account that has never failed, because the challenge is against the
        # device, not the handle.
        if info.get("needs_human"):
            state = "blocked"
        elif not info:
            state = "no-airtable-row"
        elif not (primary and second):
            state = "not-read"
        elif status != "Active":
            state = "parked"
        else:
            state = "doubled"
        phones.append({
            "name": p.name, "serial": p.serial_no, "tag": p.tag, "model": p.model_key,
            "status": status, "primary": primary, "second": second,
            "checked_at": info.get("checked_at"), "state": state,
            "needs_human": info.get("needs_human", False),
            "issue_reason": info.get("issue_reason"),
            "remark_claimed": p.remark_handles,
        })

    return {
        "phones": sorted(phones, key=lambda x: (x["model"], x["name"])),
        "needs_human": sorted(needs_human, key=lambda x: x["name"]),
        "untagged_hints": [{"name": n, "remark": r} for n, r in scan.untagged_hints],
        "tag_counts": scan.tag_counts,
        "base_id": getattr(airtable, "base_id", "") or "",
        "totals": {
            "tagged": len(scan.tagged),
            "doubled": sum(1 for p in phones if p["state"] == "doubled"),
            "parked": sum(1 for p in phones if p["state"] == "parked"),
            "blocked": sum(1 for p in phones if p["state"] == "blocked"),
            "not_read": sum(1 for p in phones if p["state"] in ("not-read", "no-airtable-row")),
            "profiles_total": len(rows),
            "needs_human": len(needs_human),
        },
    }


STATE_LABEL = {
    "doubled": "Posting twice",
    "blocked": "Blocked \u2014 needs a human",
    "parked": "Parked in Airtable",
    "not-read": "Accounts not read",
    "no-airtable-row": "No Airtable row",
}
STATE_TONE = {"doubled": "ok", "blocked": "crit", "parked": "warn",
              "not-read": "crit", "no-airtable-row": "crit"}

REASON_HELP = {
    "Retries Exhausted": "Posts kept failing until the row ran out of retries. Open the phone and see what Instagram is showing.",
    "Human Verification Required": "Instagram asked for a verification the bot cannot answer. Solve the challenge by hand.",
    "Banned / Blocked": "The account is banned or action-blocked. Nothing to retry until that clears.",
    "Account Switch Failed": "The row's Instagram account could not be made active \u2014 logged out, renamed, or the switcher would not open. Log it back in.",
    "Repeated Failures": "The same failure keeps recurring. Worth looking at the phone directly.",
    "Device Unreachable": "The phone never came up over ADB. Check it in MultiLogin.",
}

WHY_NOT_DOUBLED = {
    "blocked": "The bot has flagged this phone, so it posts <b>nothing</b> \u2014 neither account \u2014 "
               "until somebody clears <b>Needs Human Check</b> in Airtable. Instagram challenges the "
               "device, not one handle, so the account that has not failed yet is in the same trouble "
               "as the one that has.",
    "parked": "Both handles are recorded, but the profile's <b>Status</b> is Inactive, so nothing schedules for it. Set it Active and it posts twice from the next slot.",
    "not-read": "Its Instagram accounts have not been read off the phone yet, so there is no second handle to switch to.",
    "no-airtable-row": "Tagged in MultiLogin but there is no Profiles (Cloning) row for this serial. Run <code>mlx-sync</code> first.",
}


def esc(text):
    return html.escape(str(text if text is not None else ""))


def short_date(value):
    return esc(str(value or "")[:10]) or "&mdash;"


CSS = """
:root {
  color-scheme: light dark;

  /* Slate-biased neutrals: a mid grey with a touch of blue in it, so the
     page reads as chosen rather than inherited. */
  --ground:      #f4f6f9;
  --surface:     #ffffff;
  --surface-2:   #eef1f6;
  --line:        #dde3ec;
  --line-strong: #c3ccda;
  --ink:         #0f1720;
  --ink-2:       #3d4a5c;
  --muted:       #66748a;

  /* Accent carries structure and emphasis only. */
  --accent:      #2258cf;
  --accent-soft: #e6edfc;

  /* Semantic, deliberately separate from the accent. */
  --ok:          #10714e;
  --ok-soft:     #dcf3e8;
  --warn:        #8f5c06;
  --warn-soft:   #fbeed3;
  --crit:        #b02f47;
  --crit-soft:   #fbe3e7;

  --radius: 10px;
  --mono: ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, "Liberation Mono", monospace;
  --sans: ui-sans-serif, system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
}

@media (prefers-color-scheme: dark) {
  :root {
    --ground:      #0b0f15;
    --surface:     #131a23;
    --surface-2:   #1a2330;
    --line:        #263140;
    --line-strong: #35455a;
    --ink:         #e4ebf4;
    --ink-2:       #b3c0d1;
    --muted:       #7e8da1;
    --accent:      #6f9dff;
    --accent-soft: #16233b;
    --ok:          #46c894;
    --ok-soft:     #10281f;
    --warn:        #e3ae4c;
    --warn-soft:   #2a2113;
    --crit:        #ff7d92;
    --crit-soft:   #2d151b;
  }
}
/* The viewer's own toggle must win over the OS preference, both ways. */
:root[data-theme="dark"] {
  --ground: #0b0f15; --surface: #131a23; --surface-2: #1a2330;
  --line: #263140; --line-strong: #35455a;
  --ink: #e4ebf4; --ink-2: #b3c0d1; --muted: #7e8da1;
  --accent: #6f9dff; --accent-soft: #16233b;
  --ok: #46c894; --ok-soft: #10281f;
  --warn: #e3ae4c; --warn-soft: #2a2113;
  --crit: #ff7d92; --crit-soft: #2d151b;
}
:root[data-theme="light"] {
  --ground: #f4f6f9; --surface: #ffffff; --surface-2: #eef1f6;
  --line: #dde3ec; --line-strong: #c3ccda;
  --ink: #0f1720; --ink-2: #3d4a5c; --muted: #66748a;
  --accent: #2258cf; --accent-soft: #e6edfc;
  --ok: #10714e; --ok-soft: #dcf3e8;
  --warn: #8f5c06; --warn-soft: #fbeed3;
  --crit: #b02f47; --crit-soft: #fbe3e7;
}

* { box-sizing: border-box; }

body {
  margin: 0;
  background: var(--ground);
  color: var(--ink);
  font-family: var(--sans);
  font-size: 15px;
  line-height: 1.55;
  -webkit-font-smoothing: antialiased;
}

.wrap {
  max-width: 1080px;
  margin: 0 auto;
  padding: 40px 22px 96px;
  display: flex;
  flex-direction: column;
  gap: 44px;
}

/* --- masthead --- */
.masthead { display: flex; flex-direction: column; gap: 14px; }
.eyebrow {
  font-family: var(--mono);
  font-size: 11.5px;
  letter-spacing: .13em;
  text-transform: uppercase;
  color: var(--muted);
}
h1 {
  margin: 0;
  font-size: clamp(28px, 4.4vw, 40px);
  font-weight: 800;
  letter-spacing: -.022em;
  line-height: 1.12;
  text-wrap: balance;
}
.standfirst {
  margin: 0;
  max-width: 62ch;
  color: var(--ink-2);
  font-size: 16px;
}
.stamp { font-family: var(--mono); font-size: 12px; color: var(--muted); }

/* --- stat tiles --- */
.stats {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
  gap: 12px;
}
.stat {
  background: var(--surface);
  border: 1px solid var(--line);
  border-radius: var(--radius);
  padding: 16px 16px 14px;
  display: flex;
  flex-direction: column;
  gap: 2px;
  position: relative;
  overflow: hidden;
}
.stat::before {
  content: "";
  position: absolute;
  inset: 0 auto 0 0;
  width: 3px;
  background: var(--line-strong);
}
.stat.is-ok::before   { background: var(--ok); }
.stat.is-warn::before { background: var(--warn); }
.stat.is-crit::before { background: var(--crit); }
.stat .n {
  font-family: var(--mono);
  font-size: 30px;
  font-weight: 700;
  letter-spacing: -.02em;
  font-variant-numeric: tabular-nums;
  line-height: 1.1;
}
.stat.is-ok .n   { color: var(--ok); }
.stat.is-warn .n { color: var(--warn); }
.stat.is-crit .n { color: var(--crit); }
.stat .k {
  font-size: 12.5px;
  color: var(--muted);
  letter-spacing: .01em;
}

/* --- sections --- */
section { display: flex; flex-direction: column; gap: 18px; }
h2 {
  margin: 0;
  font-size: 20px;
  font-weight: 700;
  letter-spacing: -.015em;
  display: flex;
  align-items: baseline;
  gap: 10px;
  flex-wrap: wrap;
}
h2 .count {
  font-family: var(--mono);
  font-size: 12px;
  font-weight: 600;
  color: var(--muted);
  font-variant-numeric: tabular-nums;
}
.lede { margin: 0; max-width: 68ch; color: var(--ink-2); font-size: 14.5px; }

.model-head {
  font-family: var(--mono);
  font-size: 11.5px;
  letter-spacing: .13em;
  text-transform: uppercase;
  color: var(--muted);
  padding-bottom: 2px;
  border-bottom: 1px solid var(--line);
  margin-top: 8px;
}

/* --- the phone card: one device, two accounts --- */
.fleet { display: flex; flex-direction: column; gap: 10px; }
.phone {
  background: var(--surface);
  border: 1px solid var(--line);
  border-radius: var(--radius);
  border-left: 3px solid var(--line-strong);
  display: grid;
  grid-template-columns: minmax(180px, 1fr) minmax(240px, 1.5fr) auto;
  gap: 16px;
  align-items: center;
  padding: 13px 16px;
}
.phone.is-ok   { border-left-color: var(--ok); }
.phone.is-warn { border-left-color: var(--warn); }
.phone.is-crit { border-left-color: var(--crit); }

.device { display: flex; flex-direction: column; gap: 3px; min-width: 0; }
.device .name { font-weight: 650; letter-spacing: -.01em; }
.device .meta {
  font-family: var(--mono);
  font-size: 11.5px;
  color: var(--muted);
  display: flex;
  gap: 10px;
  flex-wrap: wrap;
}

/* Two handles braced together to show they share one phone. */
.accounts {
  display: flex;
  flex-direction: column;
  gap: 5px;
  border-left: 2px solid var(--line);
  padding-left: 12px;
  min-width: 0;
}
.acct { display: flex; align-items: center; gap: 8px; min-width: 0; }
.acct .slot {
  font-family: var(--mono);
  font-size: 10px;
  letter-spacing: .09em;
  text-transform: uppercase;
  color: var(--muted);
  width: 52px;
  flex: none;
}
.acct .handle {
  font-family: var(--mono);
  font-size: 13px;
  color: var(--ink);
  overflow-wrap: anywhere;
}
.acct.is-second .handle { color: var(--accent); }
.accounts .none { font-size: 13px; color: var(--muted); font-style: italic; }
.accounts.is-halted { border-left-color: var(--crit); margin-top: 2px; }
.acct .handle.is-stopped { color: var(--muted); text-decoration: line-through; text-decoration-thickness: 1px; }
.acct .halt {
  font-family: var(--mono);
  font-size: 10px;
  letter-spacing: .08em;
  text-transform: uppercase;
  color: var(--crit);
}

.pill {
  font-size: 11.5px;
  font-weight: 600;
  padding: 3px 10px;
  border-radius: 999px;
  white-space: nowrap;
  border: 1px solid transparent;
}
.pill.is-ok   { background: var(--ok-soft);   color: var(--ok);   border-color: color-mix(in srgb, var(--ok) 30%, transparent); }
.pill.is-warn { background: var(--warn-soft); color: var(--warn); border-color: color-mix(in srgb, var(--warn) 32%, transparent); }
.pill.is-crit { background: var(--crit-soft); color: var(--crit); border-color: color-mix(in srgb, var(--crit) 32%, transparent); }

/* --- blocked list --- */
.blocked { display: flex; flex-direction: column; gap: 10px; }
.blk {
  background: var(--surface);
  border: 1px solid var(--line);
  border-left: 3px solid var(--warn);
  border-radius: var(--radius);
  padding: 13px 16px;
  display: flex;
  flex-direction: column;
  gap: 5px;
}
.blk.is-crit { border-left-color: var(--crit); }
.blk .top { display: flex; align-items: baseline; gap: 10px; flex-wrap: wrap; }
.blk .who { font-weight: 650; }
.blk .why { margin: 0; font-size: 14px; color: var(--ink-2); }
.blk .why code {
  font-family: var(--mono);
  font-size: 12.5px;
  background: var(--surface-2);
  padding: 1px 5px;
  border-radius: 4px;
}

/* --- manual-check table --- */
.tablewrap {
  overflow-x: auto;
  border: 1px solid var(--line);
  border-radius: var(--radius);
  background: var(--surface);
}
table { border-collapse: collapse; width: 100%; min-width: 640px; }
th, td { text-align: left; padding: 10px 14px; border-bottom: 1px solid var(--line); vertical-align: top; }
thead th {
  font-family: var(--mono);
  font-size: 10.5px;
  letter-spacing: .11em;
  text-transform: uppercase;
  color: var(--muted);
  font-weight: 600;
  background: var(--surface-2);
  white-space: nowrap;
}
tbody tr:last-child td { border-bottom: none; }
td.who { font-weight: 600; white-space: nowrap; }
td.when { font-family: var(--mono); font-size: 12.5px; color: var(--muted); font-variant-numeric: tabular-nums; white-space: nowrap; }
td.what { font-size: 13.5px; color: var(--ink-2); }
.reason {
  font-family: var(--mono);
  font-size: 11.5px;
  white-space: nowrap;
}
.reason.r-crit { color: var(--crit); }
.reason.r-warn { color: var(--warn); }
.dbl-note {
  display: inline-block;
  margin-left: 8px;
  font-size: 11px;
  font-weight: 600;
  color: var(--accent);
  background: var(--accent-soft);
  border-radius: 999px;
  padding: 1px 8px;
  white-space: nowrap;
}

/* --- how it works --- */
.cols { display: grid; grid-template-columns: repeat(auto-fit, minmax(250px, 1fr)); gap: 14px; }
.card {
  background: var(--surface);
  border: 1px solid var(--line);
  border-radius: var(--radius);
  padding: 16px;
  display: flex;
  flex-direction: column;
  gap: 7px;
}
.card h3 { margin: 0; font-size: 14px; font-weight: 700; letter-spacing: -.005em; }
.card p { margin: 0; font-size: 13.5px; color: var(--ink-2); }
pre {
  margin: 0;
  background: var(--surface-2);
  border: 1px solid var(--line);
  border-radius: 8px;
  padding: 11px 13px;
  overflow-x: auto;
  font-family: var(--mono);
  font-size: 12.5px;
  line-height: 1.6;
  color: var(--ink);
}
code { font-family: var(--mono); }
.note {
  border-left: 3px solid var(--accent);
  background: var(--accent-soft);
  padding: 12px 15px;
  border-radius: 0 8px 8px 0;
  font-size: 14px;
  color: var(--ink-2);
}
.note strong { color: var(--ink); }
footer {
  border-top: 1px solid var(--line);
  padding-top: 16px;
  font-size: 12.5px;
  color: var(--muted);
  display: flex;
  flex-direction: column;
  gap: 4px;
}
a { color: var(--accent); }
a:focus-visible, [tabindex]:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
@media (prefers-reduced-motion: reduce) { * { animation: none !important; transition: none !important; } }
@media (max-width: 720px) {
  .phone { grid-template-columns: 1fr; gap: 10px; }
  .accounts { border-left: none; padding-left: 0; border-top: 1px solid var(--line); padding-top: 9px; }
}
"""

def render(DATA: dict, generated_at: str = "") -> str:
    """The dashboard page as a string. Pure: no I/O and no device."""
    t = DATA["totals"]
    parts = []
    parts.append("<title>Second-account phones &mdash; adb_bot</title>")
    parts.append("<style>%s</style>" % CSS)
    parts.append('<div class="wrap">')

    # masthead
    parts.append(f"""
    <header class="masthead">
      <div class="eyebrow">adb_bot &middot; MultiLogin fleet</div>
      <h1>Phones running two Instagram accounts</h1>
      <p class="standfirst">Some cloud phones hold a second Instagram account inside the same app &mdash;
      same model, second handle. Those phones can post twice a slot. This is which ones do,
      which are held up, and which need somebody to open them.</p>
      <div class="stamp">Airtable base {esc(DATA.get("base_id"))} &middot; generated {esc(generated_at)}</div>
    </header>
    """)

    # stats
    parts.append(f"""
    <div class="stats">
      <div class="stat is-ok"><span class="n">{t['doubled']}</span><span class="k">phones posting twice</span></div>
      <div class="stat is-crit"><span class="n">{t['blocked']}</span><span class="k">blocked &mdash; both accounts stopped</span></div>
      <div class="stat is-warn"><span class="n">{t['parked'] + t['not_read']}</span><span class="k">tagged but not doubled yet</span></div>
      <div class="stat is-crit"><span class="n">{t['needs_human']}</span><span class="k">profiles need a manual check</span></div>
    </div>
    """)

    # --- fleet ---
    doubled = [p for p in DATA["phones"] if p["state"] == "doubled"]
    parts.append('<section id="doubled">')
    parts.append(f'<h2>Posting twice <span class="count">{len(doubled)} phones &rarr; {len(doubled) * 2} accounts</span></h2>')
    parts.append('<p class="lede">One phone, one Instagram install, two accounts. The reel flow switches to the '
                 'handle the queue row names and confirms it before opening the composer; if it cannot, it abandons '
                 'the post rather than publishing as the wrong account.</p>')
    parts.append('<div class="fleet">')
    last_model = None
    for p in doubled:
        if p["model"] != last_model:
            last_model = p["model"]
            parts.append(f'<div class="model-head">{esc(last_model)}</div>')
        parts.append(f"""
    <article class="phone is-ok">
      <div class="device">
        <span class="name">{esc(p['name'])}</span>
        <span class="meta"><span>serial {esc(p['serial'])}</span><span>tag &ldquo;{esc(p['tag'])}&rdquo;</span></span>
      </div>
      <div class="accounts">
        <div class="acct"><span class="slot">Primary</span><span class="handle">{esc(p['primary'])}</span></div>
        <div class="acct is-second"><span class="slot">Second</span><span class="handle">{esc(p['second'])}</span></div>
      </div>
      <span class="pill is-ok">2&times; per slot</span>
    </article>""")
    parts.append("</div></section>")

    # --- not doubled ---
    blocked = [p for p in DATA["phones"] if p["state"] != "doubled"]
    parts.append('<section id="blocked">')
    parts.append(f'<h2>Tagged, but not doubled yet <span class="count">{len(blocked)} phones</span></h2>')
    parts.append('<p class="lede">Each of these is tagged as a two-account phone but is not posting twice. '
                 'A <b>blocked</b> phone posts nothing at all &mdash; the bot refuses to launch it for either '
                 'account until somebody clears <b>Needs Human Check</b>.</p>')
    parts.append('<div class="blocked">')
    for p in blocked:
        tone = STATE_TONE.get(p["state"], "warn")
        reason = ""
        if p["state"] == "blocked" and p.get("issue_reason"):
            reason = f'<span class="reason r-crit">{esc(p["issue_reason"])}</span>'
        # Both handles, each shown as stopped. A blocked two-account phone is the
        # case that is easiest to misread: one account is why it got flagged, the
        # other has done nothing wrong and is off the air all the same.
        stopped = ""
        if p["primary"] and p["second"]:
            halt = "not scheduled" if p["state"] == "parked" else "stopped"
            cls = "" if p["state"] == "parked" else " is-stopped"
            rows = "".join(
                f'<div class="acct{" is-second" if slot == "Second" else ""}">'
                f'<span class="slot">{slot}</span>'
                f'<span class="handle{cls}">{esc(h)}</span>'
                f'<span class="halt">{halt}</span></div>'
                for slot, h in (("Primary", p["primary"]), ("Second", p["second"])))
            stopped = f'<div class="accounts is-halted">{rows}</div>'
        parts.append(f"""
    <div class="blk {'is-crit' if tone == 'crit' else ''}">
      <div class="top">
        <span class="who">{esc(p['name'])}</span>
        <span class="pill is-{tone}">{STATE_LABEL.get(p['state'], p['state'])}</span>
        {reason}
      </div>
      <p class="why">{WHY_NOT_DOUBLED.get(p['state'], '')}</p>
      {stopped}
    </div>""")
    parts.append("</div></section>")

    # --- needs manual check ---
    nh = DATA["needs_human"]
    two_up_names = {p["name"] for p in DATA["phones"]}
    parts.append('<section id="manual">')
    parts.append(f'<h2>Needs a manual check <span class="count">{len(nh)} profiles</span></h2>')
    parts.append('<p class="lede">Profiles the bot has flagged and will not keep retrying. The bot sets these and '
                 'never clears them &mdash; clearing the <b>Needs Human Check</b> box in Airtable is how you record that '
                 'somebody looked. A failed account switch on a two-account phone lands here too, straight away, '
                 'instead of spending three phone launches first.</p>')
    parts.append('<div class="tablewrap"><table>')
    parts.append("<thead><tr><th>Profile</th><th>Reason</th><th>Flagged</th><th>What it means</th></tr></thead><tbody>")
    for n in nh:
        reason = n["issue_reason"] or "&mdash;"
        tone = "r-crit" if reason in ("Banned / Blocked", "Account Switch Failed") else "r-warn"
        dbl = '<span class="dbl-note">two-account phone</span>' if n["name"] in two_up_names else ""
        parts.append(f"""<tr>
      <td class="who">{esc(n['name'])}{dbl}</td>
      <td><span class="reason {tone}">{esc(reason)}</span></td>
      <td class="when">{short_date(n['flagged_at'])}</td>
      <td class="what">{REASON_HELP.get(reason, '')}</td>
    </tr>""")
    parts.append("</tbody></table></div>")
    parts.append("</section>")

    # --- tagging gap ---
    hints = DATA["untagged_hints"]
    if hints:
        parts.append('<section id="gap">')
        parts.append(f'<h2>Missing a tag <span class="count">{len(hints)} phone</span></h2>')
        parts.append('<div class="blocked">')
        for h in hints:
            remark = esc(h["remark"].replace("\n", " · "))
            parts.append(f"""
    <div class="blk">
      <div class="top"><span class="who">{esc(h['name'])}</span>
        <span class="pill is-warn">Posting once</span></div>
      <p class="why">Its MultiLogin note mentions a second account &mdash; <code>{remark}</code> &mdash;
      but the profile carries no second-account tag, so nothing schedules a second post for it.
      Tag it in MultiLogin and it joins the list above.</p>
    </div>""")
        parts.append("</div></section>")

    # --- how it works ---
    tc = DATA["tag_counts"]
    parts.append(f"""
    <section id="how">
      <h2>How a phone gets here</h2>
      <div class="note"><strong>The tag has two spellings.</strong> This workspace marks these phones
      <code>Second Account</code> ({tc.get('Second Account', 0)} phones) and <code>2 accounts</code>
      ({tc.get('2 accounts', 0)} phones). Both mean the same thing and both are honoured &mdash; reading only
      one of them would miss {tc.get('2 accounts', 0)} phones.</div>
      <div class="cols">
        <div class="card">
          <h3>1 &middot; Tag it in MultiLogin</h3>
          <p>The tag is the switch. It says a phone has two accounts &mdash; it does not say which.</p>
        </div>
        <div class="card">
          <h3>2 &middot; Read the handles off the phone</h3>
          <p>The bot launches the phone and reads Instagram's own account switcher. The MultiLogin
          note is <em>not</em> trusted: on all 14 phones read, it named only the second account, and on
          Jasmin 5 it named an account the phone is not signed into at all.</p>
          <pre>run_loop second-accounts --apply</pre>
        </div>
        <div class="card">
          <h3>3 &middot; It schedules twice</h3>
          <p>With both handles recorded, the phone becomes two posting targets: two spoofed clips,
          two queue rows per slot, each naming its handle. They draw from one pool, so the two
          accounts never get the same video.</p>
        </div>
      </div>
    </section>
    """)

    parts.append("""
    <section id="run">
      <h2>Commands</h2>
      <div class="cols">
        <div class="card">
          <h3>See what it would read</h3>
          <p>Launches nothing.</p>
          <pre>python -m adb_bot.automation.run_loop \\
      second-accounts</pre>
        </div>
        <div class="card">
          <h3>Read the phones still missing</h3>
          <p>About two minutes per phone.</p>
          <pre>python -m adb_bot.automation.run_loop \\
      second-accounts --apply</pre>
        </div>
        <div class="card">
          <h3>Re-read one phone</h3>
          <p>After logging an account back in.</p>
          <pre>python -m adb_bot.automation.run_loop \\
      second-accounts --apply \\
      --serials 184090 --recheck</pre>
        </div>
      </div>
    </section>
    """)

    parts.append(f"""
    <footer>
      <div>Profiles (Cloning): <code>Has Second Account</code>, <code>Primary IG Handle</code>,
      <code>Second IG Handle</code>, <code>Accounts Checked At</code>. Posting Queue:
      <code>Target IG Handle</code>, <code>Account Slot</code>.</div>
      <div>{t['profiles_total']} profiles in the base &middot; {t['tagged']} tagged as two-account &middot;
      {t['doubled']} currently posting twice.</div>
    </footer>
    """)

    parts.append("</div>")

    return "\n".join(parts)
