"""Northfeed video courses (15.11, Rene: "wie Memberspot"): a course is an ordered list of lessons
(own MP4 + title + text). Lessons unlock strictly one after another -- lesson n+1 opens only once
lesson n was watched to >= 90 % of its duration. Watched seconds and the resume position are kept
server-side per user and lesson, so progress survives devices and the manager can see who is where.
One engine for Model tutorials (DE), the Chatter course (EN) and anything else; `Audience` decides.

Data: Airtable base "Northfeed Portal" -- Courses, Lessons, Course Progress. Videos: on this server,
/opt/northfeed-course/<course_rec>/<lesson_rec>.mp4 (served with HTTP Range so <video> can seek).

    course.catalog(email, audience)                      -> dict for the learner page
    course.progress(email, lesson_id, position, duration) -> new lesson state (+ unlocked next)
    course.manager_overview()                            -> per user / course percent
    course.course_api(action, body)                      -> CRUD for the manager tab
    course.save_upload(lesson_id, fileobj, size)         -> stores MP4, probes duration
    course.handle_media(handler, lesson_id)              -> Range/206 for stdlib http.server
    course.parse_multipart(handler)                      -> {"fields": {...}, "files": {name: (filename, path)}}
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path

import requests

from . import portal

T_COURSES = os.environ.get("PORTAL_COURSES_TABLE", "tblDMRKle1OuZHDV1")
T_LESSONS = os.environ.get("PORTAL_LESSONS_TABLE", "tblBQHb358KH2zQTL")
T_PROGRESS = os.environ.get("PORTAL_PROGRESS_TABLE", "tblDMTgnAsuMwchDl")
MEDIA_ROOT = Path(os.environ.get("PORTAL_COURSE_DIR", "/opt/northfeed-course"))
COMPLETE_AT = 0.90                       # share of the duration that counts as "watched to the end"
MAX_UPLOAD = 2 * 1024 ** 3               # 2 GB per lesson video; disk is the real limit (see df)
CACHE_TTL = 60
AUDIENCES = ("Model", "Chatter", "Manager")
_REC = re.compile(r"rec[A-Za-z0-9]{14}")
_cache: dict[str, tuple[float, list]] = {}
_lock = threading.Lock()


# ---------------------------------------------------------------- airtable

def _hdr() -> dict:
    return {"Authorization": f"Bearer {os.environ.get('AIRTABLE_TOKEN', '')}", "Content-Type": "application/json"}


def _url(table: str, rec: str = "") -> str:
    return f"https://api.airtable.com/v0/{portal.BASE}/{table}" + (f"/{rec}" if rec else "")


def _rows(table: str, fresh: bool = False) -> list:
    """Cached table read (60 s). Progress rows are read per user with a formula and never cached."""
    with _lock:
        hit = _cache.get(table)
        if hit and not fresh and time.time() - hit[0] < CACHE_TTL:
            return hit[1]
    if not fresh:                                           # 16.11: warm shared cache (stale copy now, refresh behind it)
        return portal.fetch_cached(table, CACHE_TTL)
    rows = portal._fetch(table)
    with _lock:
        _cache[table] = (time.time(), rows)
    return rows


def _bust(*tables: str) -> None:
    with _lock:
        for t in tables:
            _cache.pop(t, None)
            portal.bust(t)


def _check(r: requests.Response) -> dict:
    if r.status_code >= 300:
        try:
            msg = r.json().get("error", {}).get("message", r.text)
        except Exception:  # noqa: BLE001
            msg = r.text
        raise ValueError(str(msg)[:300])
    return r.json()


def _create(table: str, fields: dict) -> dict:
    r = _check(requests.post(_url(table), headers=_hdr(), json={"fields": fields, "typecast": True}, timeout=40))
    _bust(table)
    return r


def _update(table: str, rec: str, fields: dict) -> dict:
    if not _REC.fullmatch(rec):
        raise ValueError("bad record id")
    r = _check(requests.patch(_url(table, rec), headers=_hdr(), json={"fields": fields, "typecast": True}, timeout=40))
    _bust(table)
    return r


def _delete(table: str, rec: str) -> None:
    if not _REC.fullmatch(rec):
        raise ValueError("bad record id")
    _check(requests.delete(_url(table, rec), headers=_hdr(), timeout=40))
    _bust(table)


def _s(v) -> str:
    return "" if v is None else str(v)


def _n(v) -> int:
    try:
        return int(float(v or 0))
    except (TypeError, ValueError):
        return 0


# ---------------------------------------------------------------- model

def _courses() -> list[dict]:
    out = []
    for r in _rows(T_COURSES):
        f = r["fields"]
        out.append({"id": r["id"], "title": _s(f.get("Title")), "audience": _s(f.get("Audience") or "Model"),
                    "language": _s(f.get("Language") or "DE"), "description": _s(f.get("Description")),
                    "order": _n(f.get("Order")), "active": bool(f.get("Active"))})
    out.sort(key=lambda c: (c["order"], c["title"]))
    return out


def _lessons() -> list[dict]:
    out = []
    for r in _rows(T_LESSONS):
        f = r["fields"]
        att = [{"url": a.get("url"), "name": a.get("filename"), "type": a.get("type", "")} for a in (f.get("Attachments") or [])]
        answers = [a.strip() for a in _s(f.get("Quiz answers")).splitlines() if a.strip()]
        out.append({"id": r["id"], "course": (f.get("Course") or [""])[0], "title": _s(f.get("Title")), "order": _n(f.get("Order")),
                    "video": _s(f.get("Video file")), "duration": _n(f.get("Duration sec")), "text": _s(f.get("Text")),
                    "attachments": att, "quiz": {"question": _s(f.get("Quiz question")),
                                                 "answers": [a.lstrip("*").strip() for a in answers],
                                                 "correct": next((i for i, a in enumerate(answers) if a.startswith("*")), -1)}
                    if f.get("Quiz question") else None})
    out.sort(key=lambda x: (x["order"], x["title"]))
    return out


def _progress_rows(email: str) -> dict[str, dict]:
    """lesson_id -> progress row for one user (fresh read, small)."""
    em = email.strip().lower()
    rows = [r for r in portal.fetch_cached(T_PROGRESS, 20) if str(r["fields"].get("User email") or "").strip().lower() == em]   # 16.11: was a fresh 0.5 s read per page
    out = {}
    for r in rows:
        f = r["fields"]
        lid = (f.get("Lesson") or [""])[0]
        if lid:
            out[lid] = {"rec": r["id"], "watched": _n(f.get("Watched sec")), "position": _n(f.get("Last position sec")),
                        "completed": bool(f.get("Completed")), "completedAt": _s(f.get("Completed at"))}
    return out


def media_path(lesson_id: str, course_id: str | None = None) -> Path | None:
    """Stored MP4 for a lesson, or None. Looks the course up when not given (no path tricks: ids are rec…)."""
    if not _REC.fullmatch(lesson_id or ""):
        return None
    if not course_id:
        course_id = next((l["course"] for l in _lessons() if l["id"] == lesson_id), None)
    if not course_id or not _REC.fullmatch(course_id):
        return None
    p = MEDIA_ROOT / course_id / f"{lesson_id}.mp4"
    return p if p.exists() else None


def _sequential(course_id: str) -> bool:
    """Only the onboarding gate (Plattform) and team courses unlock one after another. Every other model course
    is free to watch in any order (Rene 16.11)."""
    c = next((c for c in _courses() if c["id"] == course_id), None)
    return not c or c["audience"] != "Model" or bool(re.search(r"plattform|platform|onboarding", c["title"], re.I))


def _extras(lesson_id: str, course_id: str) -> dict:
    """17.11 Rene: desktop lesson view with chapters ("In diesem Video") and "Das nimmst du mit"; optional JSON next to the MP4."""
    try:
        d = json.loads((MEDIA_ROOT / course_id / f"{lesson_id}.json").read_text())
        return {"chapters": d.get("chapters") or [], "takeaways": d.get("takeaways") or []}
    except Exception:  # noqa: BLE001
        return {}


def _state(lessons: list[dict], prog: dict[str, dict]) -> list[dict]:
    """Attach locked/available/done + resume info to each lesson of ONE course (already ordered)."""
    out, prev_done = [], True
    free = bool(lessons) and not _sequential(lessons[0]["course"])
    for l in lessons:
        p = prog.get(l["id"], {})
        done = bool(p.get("completed"))
        state = "done" if done else ("available" if prev_done or free else "locked")
        out.append({**l, **_extras(l["id"], l["course"]), "state": state, "position": p.get("position", 0), "watched": p.get("watched", 0),
                    "percent": min(100, round(100 * p.get("watched", 0) / l["duration"])) if l["duration"] else (100 if done else 0),
                    "hasVideo": bool(media_path(l["id"], l["course"])), "completedAt": p.get("completedAt", "")})
        prev_done = done
    return out


def catalog(email: str, audience: str | None = None) -> dict:
    """Everything the learner page needs: active courses for the audience with per-lesson state."""
    prog = _progress_rows(email) if email else {}
    lessons = _lessons()
    courses = []
    prev_ok = True                       # 18.11 Rene: the chatter course is split into modules (one course each), watched in order
    for c in _courses():
        if not c["active"] or (audience and c["audience"] != audience):
            continue
        ls = _state([l for l in lessons if l["course"] == c["id"]], prog)
        if c["audience"] == "Chatter":
            if not prev_ok:
                ls = [l if l["state"] == "done" else {**l, "state": "locked"} for l in ls]
            prev_ok = prev_ok and all(l["state"] == "done" for l in ls)
        done = sum(1 for l in ls if l["state"] == "done")
        nxt = next((l for l in ls if l["state"] == "available"), None)
        courses.append({**c, "lessons": ls, "total": len(ls), "done": done,
                        "percent": round(100 * done / len(ls)) if ls else 0,
                        "next": nxt["id"] if nxt else None,
                        "minutes": round(sum(l["duration"] for l in ls) / 60)})
    return {"email": email, "audience": audience, "courses": courses, "completeAt": COMPLETE_AT}


_progress_lock = threading.Lock()


def progress(email: str, lesson_id: str, position_sec: float, duration_sec: float | None = None) -> dict:
    """17.11 funnel test: at the end of a video the player sends "pause" and "ended" within milliseconds; both requests found
    no row yet and each created one (duplicate rows, the manager's course tab showed "7 von 5"). One at a time."""
    with _progress_lock:
        return _progress(email, lesson_id, position_sec, duration_sec)


def _progress(email: str, lesson_id: str, position_sec: float, duration_sec: float | None = None) -> dict:
    """Upsert the user's progress for one lesson. Watched = max(previous, position) -- the player blocks
    seeking past the furthest point, so the position is a fair measure. Completes at >= 90 % of the
    lesson duration (server value wins over the browser's). Returns the lesson's new state and whether the
    next lesson just unlocked."""
    email = (email or "").strip().lower()
    if not email or not _REC.fullmatch(lesson_id or ""):
        raise ValueError("email or lesson missing")
    lesson = next((l for l in _lessons() if l["id"] == lesson_id), None)
    if not lesson:
        raise ValueError("unknown lesson")
    duration = lesson["duration"] or _n(duration_sec)
    try:
        pos = max(0, int(round(float(position_sec or 0))))   # round, not truncate: 4.75 s of 5 s is 95 %
    except (TypeError, ValueError):
        pos = 0
    if duration:
        pos = min(pos, duration)
    siblings = [l for l in _lessons() if l["course"] == lesson["course"]]
    prog = _progress_rows(email)
    before = _state(siblings, prog)
    me = next(l for l in before if l["id"] == lesson_id)
    if me["state"] == "locked":
        raise ValueError("lesson locked")
    row = prog.get(lesson_id)
    watched = max(row["watched"] if row else 0, pos)
    completed = bool(row and row["completed"]) or (duration > 0 and watched >= COMPLETE_AT * duration)
    fields = {"Watched sec": watched, "Last position sec": pos, "Completed": completed}
    if completed and not (row and row["completed"]):
        fields["Completed at"] = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")   # 17.11: naive time was read as Berlin, 1 h early
    if row:
        _update(T_PROGRESS, row["rec"], fields)
    else:
        _create(T_PROGRESS, {"Key": f"{email}|{lesson_id}", "User email": email, "Lesson": [lesson_id], **fields})
    prog[lesson_id] = {"rec": row["rec"] if row else "", "watched": watched, "position": pos, "completed": completed,
                       "completedAt": fields.get("Completed at", row.get("completedAt", "") if row else "")}
    after = _state(siblings, prog)
    idx = next(i for i, l in enumerate(after) if l["id"] == lesson_id)
    nxt = after[idx + 1] if idx + 1 < len(after) else None
    just_unlocked = bool(nxt and nxt["state"] == "available" and before[idx + 1]["state"] == "locked")
    done = sum(1 for l in after if l["state"] == "done")
    if completed and not (row and row["completed"]) and done == len(after) and _sequential(lesson["course"]):
        try:                                       # 16.11 launch check: the gate just opened -> her first tasks must already be
            from . import onboarding_portal, portal, portal_auth   # there when she taps "Weiter zu deinem Portal"
            me_rec = (portal_auth.lookup(email) or {}).get("id")
            if me_rec:
                onboarding_portal.ensure_onboarding_tasks(me_rec)
                portal.bust(portal.T_TASKS)
        except Exception as e:  # noqa: BLE001
            print("[course] onboarding tasks:", e)
    return {"ok": True, "lesson": {k: after[idx][k] for k in ("id", "state", "position", "watched", "percent", "completedAt")},
            "next": nxt["id"] if nxt else None, "unlocked": just_unlocked,
            "course": {"id": lesson["course"], "done": done, "total": len(after), "percent": round(100 * done / len(after)) if after else 0}}


def manager_overview() -> dict:
    """Per user and course: done/total/percent, last activity. For the manager's course tab."""
    courses = _courses()
    lessons = _lessons()
    by_course = {c["id"]: [l for l in lessons if l["course"] == c["id"]] for c in courses}
    lesson_course = {l["id"]: l["course"] for l in lessons}
    users: dict[str, dict] = {}
    per: dict[tuple, dict] = {}                              # 17.11: one entry per user and lesson (older duplicate rows counted twice)
    for r in portal._fetch(T_PROGRESS):
        f = r["fields"]
        em, lid = _s(f.get("User email")).lower(), (f.get("Lesson") or [""])[0]
        if not em or not lesson_course.get(lid):
            continue
        x = per.setdefault((em, lid), {"done": False, "watched": 0, "last": ""})
        x["done"] = x["done"] or bool(f.get("Completed"))
        x["watched"] = max(x["watched"], _n(f.get("Watched sec")))
        x["last"] = max(x["last"], _s(f.get("Completed at")))
    for (em, lid), x in per.items():
        c = users.setdefault(em, {}).setdefault(lesson_course[lid], {"done": 0, "watched": 0, "last": ""})
        c["done"] += 1 if x["done"] else 0
        c["watched"] += x["watched"]
        c["last"] = max(c["last"], x["last"])
    rows = []
    for em, cs in sorted(users.items()):
        for cid, c in cs.items():
            total = len(by_course.get(cid, []))
            rows.append({"email": em, "course": cid, "courseTitle": next((x["title"] for x in courses if x["id"] == cid), ""),
                         "done": c["done"], "total": total, "percent": round(100 * c["done"] / total) if total else 0,
                         "watchedMin": round(c["watched"] / 60), "last": c["last"]})
    return {"courses": [{**c, "lessons": [{**l, "hasVideo": bool(media_path(l["id"], l["course"]))} for l in by_course[c["id"]]],
                         "total": len(by_course[c["id"]])} for c in courses],
            "progress": rows, "audiences": list(AUDIENCES), "languages": ["DE", "EN"],
            "disk": _disk()}


def _disk() -> dict:
    try:
        st = shutil.disk_usage(MEDIA_ROOT if MEDIA_ROOT.exists() else "/")
        used = sum(p.stat().st_size for p in MEDIA_ROOT.rglob("*.mp4")) if MEDIA_ROOT.exists() else 0
        return {"freeGB": round(st.free / 1024 ** 3, 1), "courseGB": round(used / 1024 ** 3, 2)}
    except OSError:
        return {}


# ---------------------------------------------------------------- manager CRUD

COURSE_FIELDS = {"title": "Title", "audience": "Audience", "language": "Language", "description": "Description", "order": "Order"}
LESSON_FIELDS = {"title": "Title", "order": "Order", "text": "Text", "quizQuestion": "Quiz question", "quizAnswers": "Quiz answers"}


def _clean(body: dict, allowed: dict) -> dict:
    out = {}
    for key, field in allowed.items():
        if key in body:
            v = body[key]
            if isinstance(v, str):
                v = v.strip()
            if v in ("", None, []):
                continue
            out[field] = v
    return out


def course_api(action: str, body: dict) -> dict:
    """course.create|update|delete, lesson.create|update|reorder|delete. Raises ValueError with a message."""
    if action == "course.create":
        f = _clean(body, COURSE_FIELDS)
        if not f.get("Title"):
            raise ValueError("Titel fehlt")
        f.setdefault("Audience", "Model")
        f.setdefault("Language", "DE")
        f.setdefault("Order", len(_courses()) + 1)
        f["Active"] = bool(body.get("active", True))
        r = _create(T_COURSES, f)
        _bust(T_COURSES)
        return {"ok": True, "id": r["id"]}
    if action == "course.update":
        f = _clean(body, COURSE_FIELDS)
        if "active" in body:
            f["Active"] = bool(body["active"])
        _update(T_COURSES, _s(body.get("id")), f)
        _bust(T_COURSES)
        return {"ok": True}
    if action == "course.delete":
        cid = _s(body.get("id"))
        if any(l["course"] == cid for l in _lessons()):
            raise ValueError("Kurs hat noch Lektionen")
        _delete(T_COURSES, cid)
        _bust(T_COURSES)
        return {"ok": True}
    if action == "lesson.create":
        f = _clean(body, LESSON_FIELDS)
        cid = _s(body.get("course"))
        if not f.get("Title") or not _REC.fullmatch(cid):
            raise ValueError("Titel und Kurs fehlen")
        f["Course"] = [cid]
        f.setdefault("Order", len([l for l in _lessons() if l["course"] == cid]) + 1)
        r = _create(T_LESSONS, f)
        _bust(T_LESSONS)
        return {"ok": True, "id": r["id"]}
    if action == "lesson.update":
        f = _clean(body, LESSON_FIELDS)
        _update(T_LESSONS, _s(body.get("id")), f)
        _bust(T_LESSONS)
        return {"ok": True}
    if action == "lesson.reorder":
        ids = [i for i in (body.get("ids") or []) if _REC.fullmatch(_s(i))]
        for n, lid in enumerate(ids, 1):
            _update(T_LESSONS, lid, {"Order": n})
        _bust(T_LESSONS)
        return {"ok": True}
    if action == "lesson.delete":
        lid = _s(body.get("id"))
        p = media_path(lid)
        _delete(T_LESSONS, lid)
        if p:
            p.unlink(missing_ok=True)
        _bust(T_LESSONS)
        return {"ok": True}
    raise ValueError(f"unknown action {action}")


def _ffprobe_duration(p: Path) -> int:
    try:
        out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(p)],
                             capture_output=True, text=True, timeout=60).stdout.strip()
        return int(round(float(out)))
    except (ValueError, subprocess.SubprocessError, OSError):
        return 0


def save_upload(lesson_id: str, fileobj, size: int | None = None) -> dict:
    """Store the uploaded MP4 under /opt/northfeed-course/<course>/<lesson>.mp4 and write the duration."""
    lesson = next((l for l in _lessons() if l["id"] == lesson_id), None)
    if not lesson:
        raise ValueError("unknown lesson")
    if size and size > MAX_UPLOAD:
        raise ValueError("Datei größer als 2 GB")
    free = shutil.disk_usage(MEDIA_ROOT if MEDIA_ROOT.exists() else "/").free
    if size and free - size < 5 * 1024 ** 3:
        raise ValueError(f"zu wenig Platz auf dem Server ({free // 1024 ** 3} GB frei)")
    d = MEDIA_ROOT / lesson["course"]
    d.mkdir(parents=True, exist_ok=True)
    tmp = d / f"{lesson_id}.part"
    with open(tmp, "wb") as fh:
        shutil.copyfileobj(fileobj, fh, 1024 * 1024)
    dur = _ffprobe_duration(tmp)
    if not dur:
        tmp.unlink(missing_ok=True)
        raise ValueError("keine gültige Videodatei (ffprobe findet keine Dauer)")
    final = d / f"{lesson_id}.mp4"
    tmp.replace(final)
    _update(T_LESSONS, lesson_id, {"Video file": final.name, "Duration sec": dur})
    _bust(T_LESSONS)
    return {"ok": True, "duration": dur, "bytes": final.stat().st_size}


# ---------------------------------------------------------------- stdlib http.server helpers

def handle_media(handler, lesson_id: str, email: str | None = None) -> None:
    """Serve the lesson video with Range/206 (needed for <video> seeking and resume). When `email` is given,
    locked lessons are refused -- the page only links available/done ones, this is the backstop."""
    p = media_path(lesson_id)
    if not p:
        handler.send_response(404); handler.end_headers(); return
    if email:
        lesson = next((l for l in _lessons() if l["id"] == lesson_id), None)
        if lesson:
            sib = [l for l in _lessons() if l["course"] == lesson["course"]]
            st = next(l for l in _state(sib, _progress_rows(email)) if l["id"] == lesson_id)["state"]
            if next((c["audience"] for c in _courses() if c["id"] == lesson["course"]), "") == "Chatter":   # modules unlock in order
                st = next((x["state"] for c in catalog(email, "Chatter")["courses"] for x in c["lessons"] if x["id"] == lesson_id), st)
            if st == "locked":
                handler.send_response(403); handler.end_headers(); return
    size = p.stat().st_size
    start, end = 0, size - 1
    rng = handler.headers.get("Range")
    m = re.fullmatch(r"bytes=(\d*)-(\d*)", rng or "")
    partial = bool(m)
    if m:
        a, b = m.group(1), m.group(2)
        if a:
            start = int(a)
            end = int(b) if b else size - 1
        elif b:                                   # suffix range: last N bytes
            start = max(0, size - int(b))
        if start >= size or end >= size or start > end:
            handler.send_response(416); handler.send_header("Content-Range", f"bytes */{size}"); handler.end_headers(); return
    length = end - start + 1
    handler.send_response(206 if partial else 200)
    handler.send_header("Content-Type", "video/mp4")
    handler.send_header("Accept-Ranges", "bytes")
    handler.send_header("Content-Length", str(length))
    handler.send_header("Cache-Control", "private, max-age=3600")
    handler.send_header("Content-Disposition", "inline")
    if partial:
        handler.send_header("Content-Range", f"bytes {start}-{end}/{size}")
    handler.end_headers()
    if getattr(handler, "command", "GET") == "HEAD":
        return
    with open(p, "rb") as fh:
        fh.seek(start)
        left = length
        try:
            while left > 0:
                chunk = fh.read(min(1024 * 1024, left))
                if not chunk:
                    break
                handler.wfile.write(chunk)
                left -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass


def parse_multipart(handler, max_bytes: int = MAX_UPLOAD) -> dict:
    """Minimal multipart/form-data reader for stdlib handlers: text parts -> fields, file parts -> temp files.
    Returns {"fields": {name: str}, "files": {name: {"filename": str, "path": Path, "size": int}}}.
    Streams to disk (no full-body buffering); caller removes the temp files."""
    ctype = handler.headers.get("Content-Type", "")
    m = re.search(r'boundary="?([^";]+)"?', ctype)
    if not m or not ctype.startswith("multipart/form-data"):
        raise ValueError("multipart/form-data expected")
    boundary = b"--" + m.group(1).encode()
    length = int(handler.headers.get("Content-Length") or 0)
    if length <= 0 or length > max_bytes:
        raise ValueError("bad or too large upload")
    stream, remaining = handler.rfile, length
    fields, files = {}, {}
    buf = b""

    def fill(n: int = 1024 * 1024) -> bool:
        nonlocal buf, remaining
        if remaining <= 0:
            return False
        chunk = stream.read(min(n, remaining))
        remaining -= len(chunk)
        buf += chunk
        return bool(chunk)

    # skip preamble up to first boundary
    while boundary not in buf and fill():
        pass
    buf = buf[buf.find(boundary) + len(boundary):]
    while True:
        if buf.startswith(b"--"):
            break
        while b"\r\n\r\n" not in buf and fill():
            pass
        head, _, buf = buf.partition(b"\r\n\r\n")
        hdrs = head.decode("utf-8", "replace")
        name = re.search(r'name="([^"]*)"', hdrs)
        fname = re.search(r'filename="([^"]*)"', hdrs)
        key = name.group(1) if name else ""
        marker = b"\r\n" + boundary
        if fname:
            tmp = tempfile.NamedTemporaryFile(prefix="nfcourse_", suffix=".upload", delete=False)
            size = 0
            while True:
                idx = buf.find(marker)
                if idx >= 0:
                    tmp.write(buf[:idx]); size += idx
                    buf = buf[idx + len(marker):]
                    break
                keep = len(marker)
                if len(buf) > keep:
                    tmp.write(buf[:-keep]); size += len(buf) - keep
                    buf = buf[-keep:]
                if not fill():
                    tmp.write(buf); size += len(buf); buf = b""
                    break
            tmp.close()
            files[key] = {"filename": Path(fname.group(1)).name, "path": Path(tmp.name), "size": size}
        else:
            while marker not in buf and fill():
                pass
            val, _, buf = buf.partition(marker)
            fields[key] = val.decode("utf-8", "replace")
        if buf.startswith(b"\r\n"):
            buf = buf[2:]
    return {"fields": fields, "files": files}


# ---------------------------------------------------------------- pages

def _tpl(name: str) -> str:
    return Path(__file__).with_name(name).read_text(encoding="utf-8")


def _logo_uri() -> str:
    import base64
    f = portal.LOGO
    return "data:image/png;base64," + base64.b64encode(f.read_bytes()).decode() if f.exists() else ""


def build(email: str, audience: str, lang: str | None = None, back: str = "/") -> str:
    """Learner page. lang: DE|EN (default: course language of the audience, Model->DE, Chatter->EN)."""
    d = catalog(email, audience)
    d["lang"] = (lang or ("EN" if audience == "Chatter" else "DE")).upper()
    d["back"] = back
    return (_tpl("course.html").replace("__LOGO__", _logo_uri())
            .replace("__DATA__", json.dumps(d, ensure_ascii=False).replace("</", "<\\/")))


def admin_fragment() -> str:
    """HTML+JS fragment for the manager's course tab (expects the manager page's CSS and api() helper)."""
    return _tpl("course_admin.html")
