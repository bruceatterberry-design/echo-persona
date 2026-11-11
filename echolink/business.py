"""Echo phoning a business for the owner: a pharmacy, a store, a tire shop, an office.

Before the call (shown on the tablet for the owner to approve and fill in):
  checklist  - the 2-5 things the owner wants found out ("Is the loved one's prescription ready?", "What does it cost?")
  needs      - details the business will probably ask for (the loved one's date of birth, the car's tire size, when the owner is
               free) - the owner types them in; Echo only ever says what is written there
  may_agree  - what Echo may agree to for the owner ("book an appointment Tue-Thu after 1 pm"), or nothing
During the call: works through the checklist, answers their questions from those details only, waits on hold (music,
recordings, transfers) for up to 15 minutes, gets through "press 1 for ..." menus, stays polite with someone short or
rude. Afterwards: the summary (tablet + Mac) and a text to the owner's phone with the answers. The details the owner typed are
blanked out of the saved transcript.
"""
from __future__ import annotations

import json
import re
import time

HOLD_MAX_S = 15 * 60
BUSINESS_MAX_S = 25 * 60
NEVER = re.compile(r"\b(?:card number|credit card|debit card|social security|ssn|password|pin\b|bank account|"
                   r"routing number|cvv|security code)\b", re.I)
RECORDED_ALWAYS = re.compile(r"\b(?:continue to hold|your call is important|next available|estimated wait|"
                             r"in the order (?:it was|they were) received|remain on the line)\b", re.I)
RECORDED = re.compile(r"\b(?:your call is important|please (?:continue to )?hold|please stay on the line|all of our|"
                      r"representatives? (?:are|is)|next available|estimated wait|in the order (?:it was|they were) "
                      r"received|thank you for (?:your patience|waiting|calling)|visit (?:us )?(?:at|online)|"
                      r"www\.|dot com|hold music|this call may be (?:recorded|monitored))\b", re.I)
HOLD_ASK = re.compile(r"\b(?:hold on|please hold|can you hold|hold please|could you hold|one (?:moment|minute|second|sec)|"
                      r"just a (?:moment|minute|second|sec)|let me (?:check|look|see|find|grab|go|pull)|"
                      r"(?:i'?ll|let me) transfer|transferring you|hang on|bear with me)\b", re.I)
MENU = re.compile(r"\b(?:press \d|dial \d|for [\w ]{2,30},? (?:press|say)|say [\"']?\w+[\"']? (?:for|to)|"
                  r"main menu|para español|if you know your party'?s extension|please (?:say|choose|select))\b", re.I)
BACK = re.compile(r"\b(?:thank(?:s| you) for holding|sorry (?:about|for) the wait|are you (?:still )?there|hello\??|"
                  r"you still there|okay,? so|alright,? so|i'?m back|i have (?:it|that)|i found)\b", re.I)

PLAN_PROMPT = """{self_text}

the owner asked you to phone {name}. His words: "{request}"
What the owner has already told you: {facts}
Plan the call. Reply as JSON:
- "kind": "business" if {name} is a business, shop, pharmacy, office, clinic, school or company; else "personal".
- "business": ONLY the end of the sentence "the owner wanted me to call you ..." - it is said TO them, so "you" is the
  business (for example "to ask if you have a battery for his 2015 F-150", "to check whether the loved one
  's prescription is ready for pickup", "to ask about the price and availability of a new 65-inch TV",
  "to get a price on four new tires and book an appointment").
- "question": the ONE question you ask first.
- "checklist": 2 to 4 short questions that must be answered for the owner (only what he needs; no extras), the first one
  being "question". Ask them as Echo speaking for the owner ("his truck", "the loved one's prescription", never "my"). Think like a
  sensible person: for a prescription - is it ready, the cost/copay, until when it can be picked up; for a product - in
  stock, price, size/model options, until when; for tires - price for the set (installed), how soon, booking a time.
- "needs": details the business will probably ask for that the owner has NOT given, each {{"label": "...", "why": "..."}}
  (for a pharmacy: the patient's full name and date of birth; for tires: the car's year, make, model and tire size;
  for an appointment: when the owner is free). Never ask for card numbers, bank details, social security numbers or
  passwords. [] if nothing is needed.
- "may_agree": what the owner clearly asked you to arrange (for example "book a tire appointment"), otherwise "".
JSON only."""

PLAN_SCHEMA = {"type": "object", "properties": {
    "kind": {"type": "string", "enum": ["business", "personal"]}, "business": {"type": "string"},
    "question": {"type": "string"}, "checklist": {"type": "array", "items": {"type": "string"}},
    "needs": {"type": "array", "items": {"type": "object", "properties": {"label": {"type": "string"},
                                                                          "why": {"type": "string"}}}},
    "may_agree": {"type": "string"}}, "required": ["kind", "business", "question", "checklist"]}

ASSESS_PROMPT = """You are helping Echo, an AI assistant, on a phone call to {name} for the owner.
Checklist (what the owner wants to find out):
{checklist}
The call so far:
{transcript}
Look at what {name}'s side JUST said (the last line). Reply as JSON:
- "answers": checklist items it answers: [{{"i": number, "answer": "short answer in plain words"}}] (only real
  answers; [] if none).
- "kind": one of "talk" (a normal reply or question), "hold" (they ask Echo to hold or wait, or are transferring),
  "menu" (a recorded phone menu: "press 1 for ..."), "goodbye" (they're ending the call), "recording" (any other
  recorded message).
- "menu_choice": for "menu": the digit (or word to say) that best reaches what the owner needs, else "".
- "asked_for": if they asked Echo for a detail (a name, date of birth, phone number, car, time ...), what it is, else "".
JSON only."""

ASSESS_SCHEMA = {"type": "object", "properties": {
    "answers": {"type": "array", "items": {"type": "object", "properties": {"i": {"type": "integer"},
                                                                            "answer": {"type": "string"}}}},
    "kind": {"type": "string", "enum": ["talk", "hold", "menu", "goodbye", "recording"]},
    "menu_choice": {"type": "string"}, "asked_for": {"type": "string"}}, "required": ["kind"]}

WORDS_PROMPT = """{self_text}

You are on a phone call with {name} for the owner. the owner wanted you to call {business}.
Details the owner gave you for this call (the ONLY details you may give out; say them exactly):
{facts}
What you may agree to for the owner: {may_agree}
Checklist - answered so far:
{status}
The call so far (Echo = you):
{transcript}

{task}
You are Echo, not the owner: never say "my name is the owner" or "my truck"; say "It's for the owner", "his truck".
Speak like a polite, efficient person on the phone: 1-3 short sentences. If they were short or rude, stay calm,
friendly and brief; never argue. If they ask for a detail that isn't listed above, say you don't have it on hand and
the owner can follow up. Never invent prices, dates, names or numbers. Reply with only the words you say."""

TASKS = {
    "reply": "Reply to what they just said. Then, ONLY if a checklist question is still unanswered in the call so far "
             "(read the conversation, not just the list), ask that one; never re-ask something they already told "
             "you and never add new topics of your own. If nothing is left, just acknowledge briefly.",
    "give": "They asked for a detail. Give it if it's listed above (exactly as written), then continue with the next "
            "unanswered checklist question (only one).",
    "wrap": "You're done (everything is answered, or they asked if there's anything else). In ONE sentence, say "
            "that's everything and repeat the key answers back, e.g. \"No, that's everything - so it's ready, $12.50, "
            "open until 9.\" No question and no goodbye (your goodbye is added right after).",
    "agree": "They're offering times or asking about booking. If an offered time fits what you may agree to, CHOOSE "
             "it yourself and say so clearly (\"Wednesday at 2 works - please book that.\"); give the name/number for "
             "it from the details if they need them. If nothing fits, say which times would work instead. Never "
             "ask them which time works; you decide within what the owner allowed.",
}

RESULT_PROMPT = """Echo, the owner's AI assistant, phoned {name} for the owner ({business}). The call:
{transcript}
Checklist answers found: {answers}
Write the text message Echo sends the owner: start "Echo: ", then the key facts only (ready or not, prices, availability,
times, appointment booked or not, anything the owner must do). ONLY what was actually said on the call: if a price or
time wasn't given, say "price not mentioned" (never guess "no cost"). At most 280 characters, plain words, use names
not "you"/"I". If nobody answered or it didn't get anywhere, say that."""

from echolink.owner import personalise  # noqa: E402
PLAN_PROMPT, ASSESS_PROMPT, WORDS_PROMPT, RESULT_PROMPT = (personalise(x) for x in (PLAN_PROMPT, ASSESS_PROMPT, WORDS_PROMPT, RESULT_PROMPT))


def plan(brain, self_text: str, name: str, request: str, facts: dict) -> dict:
    j = json.loads(brain.ask(PLAN_PROMPT.format(self_text=self_text, name=name, request=request[:500],
                                                facts=json.dumps(facts) if facts else "nothing yet"),
                             PLAN_SCHEMA, 0.2))
    needs = [{"label": str(n.get("label", ""))[:60], "why": str(n.get("why", ""))[:120]}
             for n in (j.get("needs") or []) if n.get("label") and not NEVER.search(str(n.get("label")))]
    checklist = []
    for x in (j.get("checklist") or []):
        x = str(x).strip()
        if x and not re.match(r"^(?:hello|hi|i'?m calling|this is)\b", x, re.I) and \
                not any(_close(x, y) for y in checklist):
            checklist.append(x)
    q = str(j.get("question") or "").strip()
    if not q or re.match(r"^(?:hello|hi|i'?m calling|this is)\b", q, re.I) or len(q.split()) > 18:
        q = checklist[0] if checklist else "Could you help me with that?"
    if not any(_close(q, y) for y in checklist):
        checklist = [q] + checklist
    checklist = [_his(x) for x in checklist[:4]]
    q = _his(q)
    business = re.sub(r"^(?:owner (?:wanted|asked) me to )?(?:call|phone|ring)\b.*?\b(?=to |about )", "",
                      str(j.get("business") or "").strip(), flags=re.I).strip(" .,:;-")
    if not q or q.lower().startswith(("to ", "about ")) or not q.rstrip().endswith("?"):
        q = next((x for x in checklist if x.rstrip().endswith("?")), "") or "Could you help me with that?"
    # the reason for the call in the owner's own words when he gave it ("... and ask if ..."): always clean
    business = business_from_request(request) or business
    if len(re.sub(r"\W", "", business)) < 4 or re.search(r"\bwanted me\b|^owner\b", business, re.I):
        business = "to ask a quick question for him"
    return {"kind": j.get("kind", "personal"), "business": business, "question": q,
            "checklist": checklist, "needs": needs[:5], "may_agree": str(j.get("may_agree") or "")}


def business_from_request(request: str) -> str:
    """'Call Walmart pharmacy and ask if the loved one's medication is ready' → 'to ask if the loved one's medication is ready'
    (said TO the business: they → you; the owner's my/me/I → his/him/he)."""
    m = re.match(r"^\s*(?:hey |ok )?(?:echo,? )?(?:please |can you |could you )?(?:call|phone|ring)\s+.+?\s+and\s+(.+)$",
                 request.strip().rstrip(".!"), re.I)
    if not m:
        return ""
    rest = m.group(1).strip()
    rest = re.sub(r"\bthey\b", "you", rest, flags=re.I)
    rest = re.sub(r"\bthey're\b", "you're", rest, flags=re.I)
    rest = re.sub(r"\bmy\b", "his", rest, flags=re.I)
    rest = re.sub(r"\bme\b", "him", rest, flags=re.I)
    rest = re.sub(r"\bI\b", "he", rest)
    rest = re.sub(r"\bI'm\b", "he's", rest)
    if re.match(r"^(?:ask|see|find|check|get|tell|let|make|book|schedule|order|confirm|introduce)\b", rest, re.I):
        return "to " + rest[0].lower() + rest[1:]
    return "about " + rest


def _his(q: str) -> str:
    """Echo asks for the owner: "my truck" → "his truck"."""
    return re.sub(r"\bmy\b", "his", q, flags=re.I)


def _close(a: str, b: str) -> bool:
    wa, wb = set(re.findall(r"[a-z]+", a.lower())), set(re.findall(r"[a-z]+", b.lower()))
    return bool(wa and wb) and len(wa & wb) / min(len(wa), len(wb)) >= 0.7


ANYTHING_ELSE = re.compile(r"\b(?:anything else|something else|what else can i|is that all|is that it|"
                           r"all (?:i|we) need|that'?s all|you'?re all set|all set)\b", re.I)
ASKED = re.compile(r"\b(?:your name|the name|last name|first name|date of birth|birth ?date|d\.?o\.?b|phone number|"
                   r"callback|call(?:-| )back number|what year|which (?:vehicle|car|truck)|make and model|tire size|"
                   r"what size|address|zip|which day|what day|what time|when (?:are you|is he|can you|would)|"
                   r"spell|who(?:'s| is) (?:the )?(?:patient|calling))\b", re.I)


APPOINTMENT = re.compile(r"\b(?:appointment|schedule|book|slot|opening|come in|bring it in|which time|what time works|"
                         r"works (?:best|for you)|monday|tuesday|wednesday|thursday|friday|saturday|sunday|tomorrow|"
                         r"\d{1,2}(?::\d\d)? ?(?:am|pm|a\.m\.|p\.m\.))\b", re.I)


def quick_kind(last: str, call) -> str:
    """What just happened, from the words alone (instant): menu / recording / hold / wrap / give / reply."""
    if MENU.search(last):
        return "menu"
    if RECORDED_ALWAYS.search(last) or (RECORDED.search(last) and not BACK.search(last)):
        return "recording"
    if HOLD_ASK.search(last) or re.search(r"\b(?:can|could|would) you (?:please )?hold\b", last, re.I):
        return "hold"
    if ANYTHING_ELSE.search(last) or all_answered(call):
        return "wrap"
    if call.may_agree and APPOINTMENT.search(last):
        return "agree"
    if ASKED.search(last):
        return "give"
    return "reply"


def assess(brain, call) -> dict:
    j = json.loads(brain.ask(ASSESS_PROMPT.format(
        name=call.name, checklist="\n".join(f"{i}. {q}" for i, q in enumerate(call.checklist)),
        transcript=call.text()[-3000:]), ASSESS_SCHEMA, 0.0))
    for a in j.get("answers") or []:
        i = a.get("i")
        if isinstance(i, int) and 0 <= i < len(call.checklist) and a.get("answer"):
            call.answers[i] = str(a["answer"])[:200]
    last = call.lines[-1]["text"] if call.lines else ""
    kind = j.get("kind", "talk")
    if MENU.search(last):                     # the words decide when they're unmistakable
        kind = "menu"
    elif HOLD_ASK.search(last) and not re.search(r"\?\s*$", last):
        kind = "hold"
    return {"kind": kind, "menu_choice": str(j.get("menu_choice") or "").strip(),
            "asked_for": str(j.get("asked_for") or "").strip()}


def status(call) -> str:
    return "\n".join(f"- {q} -> {call.answers.get(i, '(not yet)')}" for i, q in enumerate(call.checklist))


def all_answered(call) -> bool:
    return bool(call.checklist) and all(i in call.answers for i in range(len(call.checklist)))


def words_prompt(self_text: str, call, task: str, business: str) -> str:
    facts = "\n".join(f"- {k}: {v}" for k, v in call.facts.items() if v) or "- (none)"
    return WORDS_PROMPT.format(self_text=self_text, name=call.name, business=business, facts=facts,
                               may_agree=call.may_agree or "nothing (say you'll check with the owner)",
                               status=status(call), transcript=call.text()[-3500:], task=TASKS[task])


def result_text(brain, call, business: str) -> str:
    t = brain.ask(RESULT_PROMPT.format(name=call.name, business=business, transcript=call.text()[-4000:],
                                       answers=json.dumps(call.answers)), None, 0.2).strip()
    t = t.strip('"')
    return t if t.startswith("Echo:") else "Echo: " + t


def redact(text: str, facts: dict) -> str:
    """Details the owner typed for the call (a date of birth, an address) are not kept in the saved transcript."""
    for k, v in facts.items():
        if v and len(str(v)) >= 3:
            text = re.sub(re.escape(str(v)), f"[{k}]", text, flags=re.I)
    return text


CLOSING = "Thank you for your time. Talk to you soon."


def wait_on_hold(hear, call, started: float) -> str:
    """On hold: wait (music, recordings) until a person speaks again; returns what they said ("" if time ran out)."""
    call.state = "on hold"
    call.say("note", "On hold…")
    while not call.stop and time.time() - started < HOLD_MAX_S:
        heard = hear(60.0)
        if heard is None or heard == "":
            continue                      # silence or music: keep waiting
        if RECORDED_ALWAYS.search(heard) or (RECORDED.search(heard) and not BACK.search(heard)):
            continue
        call.state = "talking"
        return heard
    call.state = "talking"
    return ""
