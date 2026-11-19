"""RNX Viewer "Funnel" tab: what happens between the Meta ad and a finished lead.

Rene 11.11: "ein dashboard das alle Schritte anzeigt und die Prozentzahl wer weitergeklickt hat
mit den fertigen leads und daten".

Two sources, both written by the public endpoints of platform_server:
  /opt/northfeed-applications/events.jsonl  -- one line per funnel step (/apply/track)
  /opt/northfeed-applications/*.json        -- one file per finished application (/apply)

Everything is counted per visitor (vid), not per request: a reload or a step back does not count twice
(the page itself already de-duplicates in sessionStorage, this is the second safety net).
"""

from __future__ import annotations

import datetime as dt
import html
import json
from pathlib import Path

DIR = Path("/opt/northfeed-applications")
EVENTS = DIR / "events.jsonl"

# the funnel in order; "q" steps are discovered from the data because the questions can change
STEPS = [("lp_view", "Landing page opened"),
         ("lp_scroll60", "Read 60% of the page"),
         ("lp_cta", "Clicked “Apply”"),
         ("form_start", "Form opened")]
TAIL = [("submit", "Application sent"),
        ("danke_view", "Thank-you page"),
        ("wa_click", "Clicked WhatsApp")]
SCORES = {"A": ("A · hot", "var(--ok)"), "B": ("B · homework", "var(--accent)"),
          "C": ("C · waitlist", "var(--warn)"), "X": ("rejected", "var(--muted)")}


def _events(since: dt.datetime | None) -> list[dict]:
    if not EVENTS.exists():
        return []
    out = []
    for line in EVENTS.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            e = json.loads(line)
        except Exception:  # noqa: BLE001
            continue
        if since:
            try:
                if dt.datetime.fromisoformat(str(e.get("t"))) < since:
                    continue
            except Exception:  # noqa: BLE001
                pass
        out.append(e)
    return out


def _applications(since: dt.datetime | None) -> list[dict]:
    out = []
    for f in sorted(DIR.glob("*.json"), reverse=True):
        if f.name == "events.jsonl":
            continue
        try:
            d = json.loads(f.read_text(encoding="utf-8", errors="replace"))
        except Exception:  # noqa: BLE001
            continue
        if not isinstance(d, dict):
            continue
        if since:
            try:
                if dt.datetime.fromisoformat(str(d.get("_received"))) < since:
                    continue
            except Exception:  # noqa: BLE001
                pass
        d["_file"] = f.name
        out.append(d)
    return out


def _esc(v) -> str:
    return html.escape(str(v)) if v not in (None, "", []) else "–"


def _bar(pct: float, color: str) -> str:
    return (f'<div class=bar><i style="width:{max(0.0, min(100.0, pct)):.1f}%;background:{color}"></i></div>')


def build(days: int = 30) -> str:
    since = None if days <= 0 else dt.datetime.now() - dt.timedelta(days=days)
    evs = _events(since)
    apps = [a for a in _applications(since)
            if str(a.get("source") or "").strip() in ("paid-ads-funnel", "start-funnel")]

    # visitors per step
    seen: dict[str, set] = {}
    qsteps: dict[str, set] = {}
    for e in evs:
        vid = str(e.get("vid") or e.get("ip") or "?")
        ev = str(e.get("ev") or "")
        if ev == "q":
            qsteps.setdefault(str(e.get("step") or "?"), set()).add(vid)
        else:
            seen.setdefault(ev, set()).add(vid)

    def qkey(s: str) -> int:
        try:
            return int(s.split("-")[0])
        except Exception:  # noqa: BLE001
            return 999

    rows = list(STEPS)
    for s in sorted(qsteps, key=qkey):
        label = s.split("-", 1)[-1].replace("_", " ")
        rows.append(("q:" + s, f"Question {qkey(s)} · {label}"))
    rows += TAIL

    counts = [(label, len(qsteps[key[2:]]) if key.startswith("q:") else len(seen.get(key, ())))
              for key, label in rows]
    top = counts[0][1] if counts else 0

    # sources and angles
    src: dict[str, set] = {}
    ang: dict[str, set] = {}
    for e in evs:
        if str(e.get("ev")) != "lp_view":
            continue
        vid = str(e.get("vid") or "?")
        src.setdefault(str(e.get("utm_source") or "direct"), set()).add(vid)
        ang.setdefault(str(e.get("angle") or "default"), set()).add(vid)

    leads = [a for a in apps if str(a.get("score") or "") != "X"]
    bys: dict[str, int] = {}
    for a in apps:
        bys[str(a.get("score") or "?")] = bys.get(str(a.get("score") or "?"), 0) + 1

    def pct(n: int, base: int) -> str:
        return f"{(100.0 * n / base):.1f}%" if base else "–"

    # ---------- KPIs ----------
    kpis = [("People", top, "one person counts once, however often she comes back"),
            ("Applications", len(apps), "form sent, including rejected"),
            ("Leads", len(leads), "A, B and waitlist"),
            ("Landing → lead", pct(len(leads), top), "of all visitors"),
            ("WhatsApp clicks", len(seen.get("wa_click", ())), "conversation actually started")]
    kpi_html = "".join(
        f'<div class=kpi><div class=k>{html.escape(k)}</div><div class=v>{html.escape(str(v))}</div>'
        f'<div class=s>{html.escape(s)}</div></div>' for k, v, s in kpis)

    # ---------- funnel table ----------
    trs = []
    prev = None
    for label, n in counts:
        of_top = 100.0 * n / top if top else 0.0
        step_pct = pct(n, prev) if prev is not None else "–"
        lost = (prev - n) if prev is not None else 0
        lost_html = (f'<span class="bad">−{lost}</span>' if lost > 0 else
                     ('<span class=mut>0</span>' if prev is not None else '<span class=mut>–</span>'))
        trs.append(f'<tr><td class=lbl>{html.escape(label)}</td><td class=num>{n}</td>'
                   f'<td class=num>{step_pct}</td><td class=num>{of_top:.1f}%</td>'
                   f'<td class=num>{lost_html}</td><td class=barc>{_bar(of_top, "var(--pink)")}</td></tr>')
        prev = n
    funnel = ("<table class=t><thead><tr><th>Step</th><th class=num>People</th>"
              "<th class=num>of previous</th><th class=num>of all</th><th class=num>lost</th>"
              "<th></th></tr></thead><tbody>" + "".join(trs) + "</tbody></table>")

    # ---------- leads table ----------
    lrows = []
    for a in apps:
        ans = a.get("answers") if isinstance(a.get("answers"), dict) else {}
        at = a.get("attribution") if isinstance(a.get("attribution"), dict) else {}
        sc = str(a.get("score") or "?")
        slabel, scol = SCORES.get(sc, (sc, "var(--muted)"))
        when = str(a.get("_received") or "").replace("T", " ")[:16]
        phone = str(a.get("phone") or "")
        wa = (f'<a class=wa href="https://wa.me/{"".join(c for c in phone if c.isdigit())}" target=_blank '
              f'rel=noopener>{_esc(phone)}</a>') if phone else "–"
        ig = str(a.get("instagram") or "").lstrip("@")
        iglink = (f'<a class=wa href="https://instagram.com/{html.escape(ig)}" target=_blank rel=noopener>'
                  f'@{html.escape(ig)}</a>') if ig else "–"
        lrows.append(
            f'<tr><td class=mut>{html.escape(when)}</td>'
            f'<td><span class=pill style="color:{scol};border-color:{scol}">{html.escape(slabel)}</span></td>'
            f'<td><b>{_esc(a.get("first_name"))}</b></td>'
            f'<td>{iglink}</td><td>{wa}</td><td class=mut>{_esc(a.get("email"))}</td>'
            f'<td>{_esc(ans.get("country"))}</td><td>{_esc(ans.get("of"))}</td>'
            f'<td>{_esc(ans.get("revenue"))}</td><td>{_esc(ans.get("hours"))}</td>'
            f'<td class=prob>{_esc(ans.get("problem"))}</td>'
            f'<td class=mut>{_esc(at.get("utm_source") or at.get("ref") or "direct")} / '
            f'{_esc(at.get("angle") or "default")}</td></tr>')
    leads_html = ("<table class=t><thead><tr><th>When</th><th>Score</th><th>Name</th><th>Instagram</th>"
                  "<th>WhatsApp</th><th>E-mail</th><th>Country</th><th>OnlyFans</th><th>Revenue</th>"
                  "<th>Hours</th><th>Biggest problem</th><th>Source / angle</th></tr></thead><tbody>"
                  + ("".join(lrows) or '<tr><td colspan=12 class=mut>No applications in this period.</td></tr>')
                  + "</tbody></table>")

    def mini(title: str, d: dict) -> str:
        items = sorted(d.items(), key=lambda kv: -len(kv[1]))
        if not items:
            return ""
        body = "".join(f'<tr><td>{html.escape(k)}</td><td class=num>{len(v)}</td>'
                       f'<td class=num>{pct(len(v), top)}</td></tr>' for k, v in items)
        return (f'<div class=card><h2>{html.escape(title)}</h2><table class=t><tbody>{body}</tbody></table></div>')

    scoreline = " · ".join(f"{SCORES.get(k, (k, ''))[0]}: {v}" for k, v in sorted(bys.items())) or "none yet"
    segs = "".join(f'<a href="/funnel?days={d}" class="{"on" if d == days else ""}">{lbl}</a>'
                   for d, lbl in ((1, "today"), (7, "7 days"), (30, "30 days"), (0, "all")))

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>RNX Viewer · Funnel</title>
<style>
 :root {{ color-scheme: dark; --bg:#0b0d12; --panel:#161a22; --panel2:#1b2029; --line:#262c38; --fg:#e6e8ee;
   --muted:#9aa3b2; --accent:#4f8cff; --pink:#ff5c9a; --ok:#3fb950; --bad:#ff6b6b; --warn:#d29922; }}
 * {{ box-sizing:border-box; }}
 body {{ margin:0; background:var(--bg); color:var(--fg);
   font:14px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",Inter,sans-serif; }}
 a {{ color:inherit; text-decoration:none; }}
 main {{ padding:24px 30px 60px; max-width:1500px; }}
 .head {{ display:flex; justify-content:space-between; align-items:flex-start; gap:16px; flex-wrap:wrap; margin-bottom:18px; }}
 h1 {{ margin:0; font-size:24px; }} .sub {{ color:var(--muted); margin-top:3px; }}
 .seg {{ display:inline-flex; background:var(--panel); border:1px solid var(--line); border-radius:11px; padding:3px; }}
 .seg a {{ color:var(--muted); font-weight:600; padding:6px 14px; border-radius:8px; }}
 .seg a.on {{ background:var(--panel2); color:var(--fg); }}
 .kpis {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(185px,1fr)); gap:12px; margin-bottom:18px; }}
 .kpi {{ background:var(--panel); border:1px solid var(--line); border-radius:14px; padding:14px 18px; }}
 .kpi .k {{ color:var(--muted); font-size:12px; text-transform:uppercase; letter-spacing:.06em; }}
 .kpi .v {{ font-size:27px; font-weight:700; margin:3px 0 1px; }}
 .kpi .s {{ color:var(--muted); font-size:12px; }}
 .card {{ background:var(--panel); border:1px solid var(--line); border-radius:14px; padding:16px 18px; margin-bottom:16px; overflow-x:auto; }}
 .card h2 {{ margin:0 0 12px; font-size:15px; }}
 .grid {{ display:grid; grid-template-columns:1fr 1fr; gap:16px; }}
 @media (max-width:900px) {{ .grid {{ grid-template-columns:1fr; }} main {{ padding:18px 14px 50px; }} }}
 table.t {{ width:100%; border-collapse:collapse; font-size:13.3px; }}
 .t th {{ text-align:left; color:var(--muted); font-size:11.5px; text-transform:uppercase; letter-spacing:.05em;
   font-weight:600; padding:0 10px 8px; border-bottom:1px solid var(--line); white-space:nowrap; }}
 .t td {{ padding:9px 10px; border-bottom:1px solid #1e232e; vertical-align:top; }}
 .t tr:last-child td {{ border-bottom:0; }}
 .t td.num, .t th.num {{ text-align:right; font-variant-numeric:tabular-nums; white-space:nowrap; }}
 .t td.lbl {{ white-space:nowrap; }}
 .t td.barc {{ width:28%; min-width:120px; }}
 .mut {{ color:var(--muted); }} .bad {{ color:var(--bad); }}
 .prob {{ max-width:280px; }}
 .bar {{ background:#11151d; border-radius:5px; height:9px; overflow:hidden; }}
 .bar i {{ display:block; height:100%; border-radius:5px; }}
 .pill {{ border:1px solid; border-radius:999px; padding:2px 9px; font-size:11.5px; font-weight:700; white-space:nowrap; }}
 a.wa {{ color:var(--accent); }} a.wa:hover {{ text-decoration:underline; }}
 .note {{ color:var(--muted); font-size:12.5px; margin-top:10px; }}
</style></head>
<body>
<main>
  <div class=head>
    <div><h1>Funnel</h1><div class=sub>Meta ad → landing page → application → WhatsApp. {html.escape(scoreline)}</div></div>
    <div class=seg>{segs}</div>
  </div>
  <div class=kpis>{kpi_html}</div>
  <div class=card><h2>Every step, and who clicked on</h2>{funnel}
    <div class=note>One person counts once, no matter how often she reloads, goes back or returns days later.
      “of previous” is the share that made it from the step above to this one — the lowest number is
      where you lose people.</div></div>
  <div class=grid>{mini("Where they come from", src)}{mini("Headline they saw", ang)}</div>
  <div class=card><h2>Applications &amp; leads</h2>{leads_html}
    <div class=note>Newest first. Files on the server: /opt/northfeed-applications/</div></div>
</main>
</body></html>"""
