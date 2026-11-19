"""Chatter final test (18.11, Rene: "Test noch mit einbauen", like LUX: course → test → test shift → hire).
Part A: 17 multiple-choice questions from the course, graded at once, pass = 14 of 17, a new try after 24 h.
The answers are shown in a fixed shuffled order per question (values stay the original index, grading unchanged).
Part B: one complete scripted test sale (Rene 18.11), the manager reads it and sets Review → Test shift or Rejected (manager tab "Chatter").
Results go to Airtable Team: Stage, Test score, Test answers, Test at.

    chatter_test.page(email, err="")   -> bytes
    chatter_test.submit(email, form)   -> (ok, message)
"""

from __future__ import annotations

import datetime as dt
import html
import time

from . import portal

PASS = 14
RETRY_H = 24
# (question, [answers], index of the right one) - all from the course lessons
QUESTIONS = [
    ("Before you try to sell, you need to know…", ["his favourite content", "his name, age, where he lives, job, relationship status and hobbies", "his budget", "nothing – sell right away"], 1),
    ("A fan asks to meet you in real life. What do you do?", ["Say yes so he stays", "Keep it open and warm – never agree, never give contacts", "Tell him you never meet anyone", "Give him your Instagram"], 1),
    ("A fan says “too expensive” for a $30 PPV. Your FIRST reply?", ["Show him the value – how personal it is for you", "Send it again for $20", "Ignore him", "Ask what he wants to pay"], 0),
    ("What is the lowest discount price for a $30 PPV?", ["$20", "$25", "$28", "No discount at all"], 1),
    ("And for a $15 PPV?", ["No discount", "$10", "$12", "$5"], 0),
    ("A regular bought yesterday. When do you start the next sale yourself?", ["Right away", "After two days", "After a week", "Never again"], 1),
    ("A fan insults you. What do you do?", ["Insult him back", "Stay calm and friendly, never insult – ask your supervisor if it's serious", "Block him at once", "Stop answering for a week"], 1),
    ("A fan asks what exactly he will see in the next video.", ["Describe everything in detail", "Keep it a surprise – never promise what he will see", "Send him a free preview", "Tell him only the price"], 1),
    ("Your shift ends. What do you do?", ["Just log off", "Hand over to the next chatter, press End shift and send your report", "Post in the lounge", "Nothing, the next chatter will see it"], 1),
    ("A fan writes about something illegal or about minors.", ["Keep chatting normally", "Change the topic", "Stop, don't reply, post in #escalation right away", "Block him without telling anyone"], 2),
    # 18.11: lessons 11-21 (profiling at the top, 3rd push, renaming, TW, lists, mass messages, fixed price)
    ("You open a chat and there is no info at the top of the fan's name. What do you do?", ["Send him a PPV", "He's not profiled yet – keep profiling, don't sell", "Send him a mass message", "Rename him to TW"], 1),
    ("A fan you don't know yet pushes for pictures for the third time. What do you do?", ["Bring him back a third time", "Stop profiling and go straight into the sale", "Block him", "Stop answering"], 1),
    ("Profiling is done. How do you rename the fan?", ["Jake", "Jake 34 | Dallas | construction | fishing", "Fan from Dallas, single", "Big spender"], 1),
    ("A fan has been subscribed for three months and never bought anything. What is his name in Infloww?", ["Leave it as it is", "Just TW – delete everything else", "TW Jake 34 Dallas", "OC"], 1),
    ("You send a mass message. Which lists do you always exclude?", ["None", "TW and OC", "Big spenders", "Everyone who bought today"], 1),
    ("You sent a mass message in Infloww. What do you do next?", ["Nothing", "Press Mark as sent in your portal", "Send the same text again in an hour", "Post the text in the lounge"], 1),
    ("A fan wants one fixed price for everything, but he only has $50. What do you say?", ["OK, $50 for everything", "I was thinking of $100… then meet around $70–80", "Then you get nothing", "Tell him exactly what he gets for $50"], 1),
]
# 18.11 Rene: Part B is one complete sale ("dass er einen Verkauf komplett durchmacht"): a scripted fan from the first
# "hey" to aftercare; the candidate writes every reply (step 5 also picks the PPV price). The manager reads the whole chat.
SALE = [
    ("hey 😊", "s1", "Your first message"),
    ("I'm Jake, 34", "s2", "Keep profiling"),
    ("from Dallas, I work in construction. single rn 😅", "s3", "Keep profiling – hobbies, build a connection"),
    ("I like fishing and football. so what are you up to tonight? 😏", "s4", "Start teasing (free)"),
    ("wow you're hot. show me more", "s5", "Send your first PPV – pick the price and write the text"),
    ("that's too expensive for me rn", "s6", "Handle the objection"),
    ("ok I bought it 🔥 what's next?", "s7", "Keep the story going – never promise what he will see"),
    ("that was amazing… I have to go now", "s8", "Aftercare"),
]
PRICES = ["$15", "$29", "$55", "$79"]
WRITTEN = [(f"Fan: {m}", k) for m, k, _ in SALE]


def _order(i: int, n: int) -> list[int]:
    """Same shuffled answer order for question i on every render, so the right one isn't always in the same place."""
    import random
    o = list(range(n))
    random.Random(f"nf-test-{i}").shuffle(o)
    return o


def _last_try(me: dict) -> float:
    try:
        return dt.datetime.fromisoformat(str(me.get("test_at") or "").replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def page(email: str, err: str = "") -> bytes:
    from . import chatter_portal
    me = chatter_portal.lookup(email)
    esc = html.escape
    wait = _last_try(me) + RETRY_H * 3600 - time.time() if me.get("test_score") is not None else 0
    if me.get("stage") == "Review":
        inner = ('<h1>Test <em>sent.</em></h1><p class="lead">Thank you! Part A is passed. Your supervisor now reads your written answers '
                 'and tells you about your 4-hour test shift. You can rewatch the course any time.</p><a class="bt gh" href="/course">Open course</a>')
        return chatter_portal._wrap(inner, email, "home")
    if wait > 0:
        h = int(wait // 3600) + 1
        inner = (f'<h1>Not passed <em>yet.</em></h1><p class="lead">You had {int(me.get("test_score") or 0)} of {len(QUESTIONS)} right – you need {PASS}. '
                 f'Watch the course again and try once more in about {h} h.</p><a class="bt" href="/course">Open course</a>')
        return chatter_portal._wrap(inner, email, "home")
    qa = "".join(
        f'<fieldset class="q"><legend><b>{i + 1}.</b> {esc(q)}</legend>'
        + "".join(f'<label class="op"><input type="radio" name="q{i}" value="{j}" required> {esc(ans[j])}</label>' for j in _order(i, len(ans)))
        + "</fieldset>" for i, (q, ans, _) in enumerate(QUESTIONS))
    wr = "".join(
        f'<div class="fan"><span>Jake</span>{esc(m)}</div>'
        + (f'<label class="fl">PPV price<select name="price" required><option value="">choose…</option>'
           + "".join(f"<option>{p}</option>" for p in PRICES) + "</select></label>" if k == "s5" else "")
        + f'<label class="fl"><b>{esc(hint)}</b><textarea name="{k}" rows="2" minlength="5" required placeholder="Your reply as the creator"></textarea></label>'
        for m, k, hint in SALE)
    style = ("<style>.q{border:1px solid #2a303d;border-radius:16px;padding:14px 16px;margin:0 0 12px;background:#161a22}.q legend{padding:0 6px;font-size:15.5px}"
             ".op{display:flex;gap:10px;align-items:flex-start;padding:7px 4px;font-size:15px;cursor:pointer}.op input{margin-top:4px;accent-color:#ff5c9a}"
             ".fan{max-width:420px;margin:22px 0 4px;padding:12px 16px;border-radius:18px 18px 18px 6px;background:#1d2130;font-size:15.5px}"
             ".fan span{display:block;font-size:11.5px;color:#8d95a6;margin-bottom:3px}.fl select{display:block;margin-top:6px;background:#0f1218;border:1px solid #343c4d;border-radius:12px;color:#e6e8ee;padding:10px 12px;font:inherit}</style>")
    inner = (f'{style}<span class="eyebrow">Final test</span><h1>Your <em>test.</em></h1>'
             f'<p class="lead">Part A: {len(QUESTIONS)} questions, you need {PASS} right. Part B: one complete sale – a fan writes, you answer every message as the creator, from hello to aftercare. Your supervisor reads it. '
             f'Take your time, there is no timer.</p>{f"<p class=err>{esc(err)}</p>" if err else ""}'
             f'<form method="post" action="/test"><h2 class="sec">Part A</h2>{qa}<h2 class="sec">Part B</h2>{wr}'
             f'<button class="bt" type="submit" style="border:0;cursor:pointer;font:inherit;font-weight:700;margin-top:18px">Send my test</button></form>')
    return chatter_portal._wrap(inner, email, "home")


def submit(email: str, form: dict) -> tuple[bool, str]:
    from . import chatter_portal, manager, shifts
    me = chatter_portal.team_by_email().get((email or "").strip().lower())
    if not me or me.get("stage") not in ("Test", "Course", ""):
        return False, "The test is not open for you right now."
    if me.get("test_score") is not None and _last_try(me) + RETRY_H * 3600 > time.time():
        return False, "You can try again 24 hours after your last try."
    score = sum(1 for i, (_, _, ok) in enumerate(QUESTIONS) if str((form.get(f"q{i}") or [""])[0]) == str(ok))
    written = {k: (form.get(k) or [""])[0].strip() for _, k in WRITTEN}
    price = (form.get("price") or [""])[0]
    if any(len(v) < 5 for v in written.values()) or price not in PRICES:
        return False, "Please answer every fan message in Part B and pick the PPV price."
    passed = score >= PASS
    txt = (f"Part A: {score}/{len(QUESTIONS)}\n\nPart B – test sale:\n" + "\n".join(
        f"Fan: {m}\n{'[PPV ' + price + '] ' if k == 's5' else ''}You: {written[k]}\n" for m, k, _ in SALE))
    manager._update(chatter_portal.T_TEAM, me["id"], {"Test score": score, "Test answers": txt, "Test at": dt.datetime.now(dt.timezone.utc).isoformat(),
                                                     "Stage": "Review" if passed else "Test"})
    portal.bust(chatter_portal.T_TEAM)
    first = (me.get("name") or "").split(" ")[0]
    shifts._post("CHATTER_WH_ALERT", f"📝 **{first}** finished the test: Part A {score}/{len(QUESTIONS)} – "
                 + ("**passed**, Part B is waiting for your review in the manager (tab Chatter)." if passed else "not passed (retry in 24 h)."))
    return passed, ""
