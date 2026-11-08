"""Accounts dashboard: turn observer snapshots (+ the Reels Airtable) into one page.

Views over a period are *gains*: how many views all of an account's reels
collected in that window. Two snapshots of the same account are aligned by how
far the reel list shifted (new reels push old ones down); the gain is the new
reels' views plus the growth of every reel seen both times. Summed day by day,
that gives 24h / 7d / 30d. Until an account has a snapshot that old, the page
says "ab <date>" instead of guessing.
"""

from __future__ import annotations

import html
import json
import time
from collections import defaultdict
from datetime import datetime
from zoneinfo import ZoneInfo

from adb_bot.automation import ig_observer

BERLIN = ZoneInfo("Europe/Berlin")
DAY = 86400


def load_snapshots(path=None) -> dict:
    """handle(lower) -> [snapshot dict, ...] oldest first."""
    path = path or ig_observer.SNAPSHOTS
    out = defaultdict(list)
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    s = json.loads(line)
                except ValueError:
                    continue
                out[s["handle"].lower()].append(s)
    except FileNotFoundError:
        pass
    for v in out.values():
        v.sort(key=lambda s: s["at"])
    return out


def align(prev: list, cur: list, max_shift: int = 12):
    """Best shift k so that cur[k+i] is the same reel as prev[i] (views never shrink).

    Returns (k, gain) or (None, None) when no shift fits."""
    best = None
    n = min(len(prev), len(cur))
    for k in range(0, max_shift + 1):
        pairs = [(prev[i], cur[i + k]) for i in range(min(len(prev), len(cur) - k))]
        pairs = [(p, c) for p, c in pairs if p is not None and c is not None]
        if len(pairs) < max(3, n // 3):
            break
        shrink = sum(1 for p, c in pairs if c < p * 0.97)          # a real reel can't lose views
        if shrink > max(1, len(pairs) // 10):
            continue
        cost = sum(max(0, c - p) for p, c in pairs) / len(pairs)    # small growth = right shift
        if best is None or cost < best[1]:
            best = (k, cost)
    if best is None:
        return None, None
    k = best[0]
    gain = sum(v for v in cur[:k] if v) + sum(max(0, cur[i + k] - prev[i])
                                               for i in range(min(len(prev), len(cur) - k))
                                               if prev[i] is not None and cur[i + k] is not None)
    return k, gain


def account_metrics(snaps: list, now: float | None = None) -> dict:
    now = now or time.time()
    last = snaps[-1]
    ok = [s for s in snaps if s["status"] == "ok" and s.get("reels")]
    gains = []                                  # (at, gain, new_reels)
    for a, b in zip(ok, ok[1:]):
        k, g = align([r["views"] for r in a["reels"]], [r["views"] for r in b["reels"]])
        if k is not None:
            gains.append((b["at"], g, k))

    def window(sec):
        rows = [(t, g, k) for t, g, k in gains if t >= now - sec - 3600]
        covered = bool(ok) and ok[0]["at"] <= now - sec + 3600
        return (sum(g for _, g, _ in rows), sum(k for _, _, k in rows), covered)

    def followers_ago(sec):
        older = [s for s in snaps if s.get("followers") is not None and s["at"] <= now - sec + 3600]
        return older[-1]["followers"] if older else None

    reels = last.get("reels") or (ok[-1]["reels"] if ok else [])
    f_now = last.get("followers")
    return {
        "handle": last["handle"],
        "status": last["status"],
        "checked": last["at"],
        "followers": f_now,
        "followers_exact": last.get("followers_exact"),
        "f_d1": (f_now - followers_ago(DAY)) if f_now is not None and followers_ago(DAY) is not None else None,
        "f_d7": (f_now - followers_ago(7 * DAY)) if f_now is not None and followers_ago(7 * DAY) is not None else None,
        "f_d30": (f_now - followers_ago(30 * DAY)) if f_now is not None and followers_ago(30 * DAY) is not None else None,
        "v24": window(DAY), "v7": window(7 * DAY), "v30": window(30 * DAY),
        "reels": reels,
        "avg30": (sum(r["views"] for r in reels if r["views"]) // max(1, len([r for r in reels if r["views"]]))) if reels else None,
        "first_seen": snaps[0]["at"],
        "history": [(s["at"], s.get("followers")) for s in snaps if s.get("followers") is not None],
    }


# ----------------------------------------------------------------------------- data for the page

def _day(ts):
    return datetime.fromtimestamp(ts, BERLIN).strftime("%Y-%m-%d")


def page_data(snaps_by_handle: dict, meta: dict) -> dict:
    """Everything the page needs, per account: daily followers, daily view gains, reels."""
    accounts = []
    for key, snaps in snaps_by_handle.items():
        if not snaps:
            continue
        m = account_metrics(snaps)
        info = meta.get(key, {})
        model = info.get("model")
        if isinstance(model, dict):
            model = model.get("name")
        followers = {}
        for s in snaps:
            if s.get("followers") is not None:
                followers[_day(s["at"])] = s["followers"]           # last reading of the day wins
        views = defaultdict(int)
        new_reels = defaultdict(int)
        ok = [s for s in snaps if s["status"] == "ok" and s.get("reels")]
        for a, b in zip(ok, ok[1:]):
            k, g = align([r["views"] for r in a["reels"]], [r["views"] for r in b["reels"]])
            if k is not None:
                views[_day(b["at"])] += g
                new_reels[_day(b["at"])] += k
        accounts.append({
            "handle": m["handle"], "model": model or "No model", "status": m["status"],
            "checked": m["checked"], "since": _day(snaps[0]["at"]),
            "followers": m["followers"], "followersDaily": followers,
            "viewsDaily": dict(views), "newReelsDaily": dict(new_reels),
            "reels": m["reels"], "avg": m["avg30"],
            "iphone": info.get("iphone"), "container": info.get("container"),
        })
    return {"generated": time.time(), "accounts": accounts}


# ----------------------------------------------------------------------------- RapidAPI accounts

def _rapid_for_page(skip: set) -> list:
    """Airtable accounts the observer doesn't track (the iPhone farm), with their RapidAPI numbers."""
    # "Model" became the formula "Model (auto)" (set via "Model auswählen (Pflicht)"); asking for the old
    # name made Airtable answer 422 and the whole iPhone part of this page stayed empty (found 2026-10-11)
    accs = _airtable("tbly0y1I4hNDlvh1K", ["Username", "Model (auto)", "iPhone", "Container", "Status", "Tracking Link (auto)"])
    names = {c["id"]: c["fields"].get("Container", "") for c in _airtable("tblyZXN7caKZZBaBj", ["Container"])}
    meta_by_id = {}
    for r in accs:
        f = r["fields"]
        u = str(f.get("Username") or "").strip().lstrip("@")
        if u and u.lower() not in skip:
            meta_by_id[r["id"]] = {"username": u, "model": f.get("Model (auto)"), "iphone": f.get("iPhone"), "status": f.get("Status"),
                                   "container_name": ", ".join(names.get(c, "") for c in f.get("Container") or []),
                                   "link": ", ".join(f.get("Tracking Link (auto)") or [])}
    return rapid_accounts(meta_by_id)

# The iPhone-farm accounts are not read by the phone observer but by reel_pipeline's
# performance_collector (RapidAPI, daily 03:30 UTC) into Airtable "Performance" (one row per account
# and day) and "Reel Stats" (one row per reel and day, "Aufrufe +/- (24h)" = views since the
# previous day). Rene 2026-10-08: no separate Performance tab -- these accounts belong in this
# dashboard, same charts. Converted here to the same per-account shape page_data() builds.
T_PERF, T_REELS = "tblySt695GOrzGqZk", "tblLkw1GLUZ88Ml5N"


def _airtable(table: str, fields: list, formula: str | None = None) -> list:
    import os
    import requests
    from adb_bot.automation.observer_run import REELS_BASE
    out, offset = [], None
    while True:
        params = {"pageSize": 100, "fields[]": fields}
        if formula:
            params["filterByFormula"] = formula
        if offset:
            params["offset"] = offset
        r = requests.get(f"https://api.airtable.com/v0/{REELS_BASE}/{table}", params=params, timeout=30,
                         headers={"Authorization": f"Bearer {os.environ.get('AIRTABLE_TOKEN', '')}"})
        r.raise_for_status()
        j = r.json()
        out.extend(j.get("records", []))
        offset = j.get("offset")
        if not offset:
            return out


def rapid_accounts(meta_by_id: dict) -> list:
    """meta_by_id: Airtable account record id -> {username, model, iphone, container, status}."""
    perf = _airtable(T_PERF, ["Account", "Datum", "Follower"])
    reels = _airtable(T_REELS, ["Account", "Datum", "Reel Code", "Aufrufe", "Likes", "Kommentare", "Aufrufe +/- (24h)"],
                      "IS_AFTER({Datum}, DATEADD(TODAY(), -40, 'days'))")
    fol, rr = defaultdict(dict), defaultdict(lambda: defaultdict(list))
    for r in perf:
        f = r["fields"]
        for a in f.get("Account") or []:
            if f.get("Datum") and f.get("Follower") is not None:
                fol[a][f["Datum"]] = f["Follower"]
    for r in reels:
        f = r["fields"]
        for a in f.get("Account") or []:
            if f.get("Datum"):
                rr[a][f["Datum"]].append(f)
    last_run = max((d for a in fol.values() for d in a), default=None)
    out = []
    for rec_id, info in meta_by_id.items():
        days_f = fol.get(rec_id, {})
        days_r = rr.get(rec_id, {})
        views, new_reels, seen = defaultdict(int), defaultdict(int), set()
        for i, d in enumerate(sorted(days_r)):
            for x in days_r[d]:
                if x.get("Aufrufe +/- (24h)") is not None:
                    views[d] += x["Aufrufe +/- (24h)"]
                code = x.get("Reel Code")
                if code and code not in seen:
                    seen.add(code)
                    if i:                                 # first snapshot day: everything is "new"
                        new_reels[d] += 1
                if i and d not in views:
                    views[d] += 0
        latest = max(days_r) if days_r else None
        cur = days_r.get(latest, [])
        reel_list = [{"pos": n + 1, "views": x.get("Aufrufe") or 0, "likes": x.get("Likes"), "comments": x.get("Kommentare"),
                      "code": x.get("Reel Code")} for n, x in enumerate(cur)]
        vs = [r["views"] for r in reel_list if r["views"]]
        first = min(list(days_f) + list(days_r), default=None)
        if days_f:
            status = "ok"
        elif last_run:
            status = "not_found"                          # the collector ran but this handle gave nothing back
        else:
            status = "pending"
        model = info.get("model")
        out.append({
            "handle": info["username"], "model": (model.get("name") if isinstance(model, dict) else model) or "No model",
            "status": status, "stage": _stage(info.get("status")), "source": "rapidapi",
            "checked": time.mktime(datetime.fromisoformat(max(days_f)).timetuple()) if days_f else None,
            "since": first or _day(time.time()),
            "followers": days_f[max(days_f)] if days_f else None, "followersDaily": dict(days_f),
            "viewsDaily": dict(views), "newReelsDaily": dict(new_reels),
            "reels": reel_list, "avg": (sum(vs) // len(vs)) if vs else None,
            "iphone": _flat(info.get("iphone")), "container": info.get("container_name") or "",
            "link": info.get("link") or "",               # oopsie.bio tracking link (table Link in Bio, 2026-10-11)
        })
    return out


def _flat(v):
    if isinstance(v, list):
        return ", ".join(str(x.get("name") if isinstance(x, dict) else x) for x in v if x)
    return v.get("name") if isinstance(v, dict) else v


def _stage(v):
    """Airtable Accounts.Status (warmup day 2, healthy, ...) -- what the team calls the account's stage."""
    return _flat(v) or ""


def build(meta: dict | None = None) -> str:
    snaps = load_snapshots()
    if meta is None:
        meta = {}
        try:
            from adb_bot.automation.observer_run import file_targets, airtable_accounts
            meta.update({t["username"].lower(): t for t in file_targets()})
            meta.update({a["username"].lower(): a for a in airtable_accounts()})   # Airtable wins
        except Exception:
            pass
    # Rene 2026-10-08: only our own phones' accounts here -- the observer's handles (old MLX/GeeLark
    # and outside accounts) are left out; page_data() still works on them if they are wanted back.
    data = {"generated": time.time(), "accounts": []}
    try:
        data["accounts"] = _rapid_for_page(skip=set())
    except Exception as exc:                       # keep the observer part when Airtable/RapidAPI hiccups
        data["warning"] = f"iPhone accounts not loaded: {exc}"
    from pathlib import Path
    tpl = (Path(__file__).with_name("observer_dashboard.html")).read_text(encoding="utf-8")
    return tpl.replace("__DATA__", json.dumps(data, ensure_ascii=False).replace("</", "<\\/"))


if __name__ == "__main__":
    import sys
    out = sys.argv[1] if len(sys.argv) > 1 else str(ig_observer.STORE_DIR / "accounts.html")
    with open(out, "w", encoding="utf-8") as f:
        f.write(build())
    print(out)
