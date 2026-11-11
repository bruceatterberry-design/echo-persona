"""Errands: things the owner asks Echo to do for him on his device ("text the loved one I'll be late and remind me at 5 to call
Joe"). Echo PREPARES them; the owner approves each with one tap on the device (nothing reaches another person, a
business or his calendar without that tap). Money is never spent: purchases/payments are refused.

Only on a linked device (the device does the actions with its own apps; contacts stay there). A short JSON-only call to
the same local model turns the request into a list; ordinary chat never pays for it (looks_like_errand first).
"""
from __future__ import annotations

import json
import re
import time

KINDS = ("text", "call", "reminder", "calendar", "email", "directions", "note")
MONEY = re.compile(r"\b(buy|order|purchase|pay|payment|venmo|paypal|zelle|transfer money|send money|checkout|subscribe)\b", re.I)
TRIGGER = re.compile(
    r"\b(text|message|call|phone|ring|remind|reminder|alarm|wake me|calendar|schedule|appointment|put .{1,30} on my|"
    r"add .{1,30} to my|email|e-mail|mail|directions|navigate|drive to|take me to|errand|for me|on my behalf|"
    r"let .{1,20} know|tell \w+ (?:that|i|we)|contacts?|phone ?book|phone list|address book)\b", re.I)

SCHEMA = {"type": "object", "properties": {"errands": {"type": "array", "items": {"type": "object", "properties": {
    "kind": {"type": "string", "enum": list(KINDS)},
    "to": {"type": "string"}, "body": {"type": "string"}, "when": {"type": "string"},
    "title": {"type": "string"}, "place": {"type": "string"}}, "required": ["kind"]}}}, "required": ["errands"]}

PROMPT = """You turn a request into a list of errands for the owner's tablet. Now: {now}. Coming days: {days}.
Include EVERY separate thing he asked to be done (a request can hold several), nothing he only asked about or already
did. Fields per kind (use only these):
- text: to (name as he said it), body. Two kinds:
  * He DICTATES the words ("text Amy I'm on my way", "send Amy a text saying see you soon"; "saying" or "that says"
    always means dictated): body = his words exactly, nothing added.
  * He asks you to PASS SOMETHING ON or to introduce yourself ("tell Amy that I love her", "let Amy know I'll be
    late", "ask Amy if she needs milk", "remind Amy about Friday", "text Amy and introduce yourself"): you (Echo,
    the owner's AI assistant) write to Amy yourself, the same way you'd open a phone call: start "Hello Amy, this is
    Echo, the owner's assistant. the owner wanted me to ..." then the message, speaking to Amy as "you" and about the owner as
    "he". An introduction ends with one friendly question: "What do you think of the owner having an AI assistant?"
- call: to
- reminder: body (what to remember), when ("YYYY-MM-DD HH:MM", 24-hour)
- calendar: title, when ("YYYY-MM-DD HH:MM"), place (if said)
- email: to, title (subject), body (the email text, the owner's words)
- directions: place
- note: body
Examples (if today were Monday 2026-01-05, 10:00):
"text Amy I'm on my way and remind me at 4 to feed the cat" -> {{"errands": [{{"kind": "text", "to": "Amy", "body": "I'm on my way."}}, {{"kind": "reminder", "body": "Feed the cat", "when": "2026-01-05 16:00"}}]}}
"send a text to Amy saying dinner is at six" -> {{"errands": [{{"kind": "text", "to": "Amy", "body": "Dinner is at six."}}]}}
"tell Amy that I love her" -> {{"errands": [{{"kind": "text", "to": "Amy", "body": "Hello Amy, this is Echo, the owner's assistant. the owner wanted me to tell you that he loves you."}}]}}
"send a text to Sam and introduce yourself" -> {{"errands": [{{"kind": "text", "to": "Sam", "body": "Hello Sam, this is Echo, the owner's assistant. the owner wanted me to text you to introduce myself. What do you think of the owner having an AI assistant?"}}]}}
"let Sam know I'll be 10 minutes late and ask him if he wants coffee" -> {{"errands": [{{"kind": "text", "to": "Sam", "body": "Hello Sam, this is Echo, the owner's assistant. the owner wanted me to let you know he'll be 10 minutes late. He also asked: do you want a coffee?"}}]}}
"put lunch with Sam on my calendar Friday at noon at Joe's" -> {{"errands": [{{"kind": "calendar", "title": "Lunch with Sam", "when": "2026-01-09 12:00", "place": "Joe's"}}]}}
"email Pat that the report is done, subject report" -> {{"errands": [{{"kind": "email", "to": "Pat", "title": "Report", "body": "The report is done."}}]}}
"what's the weather?" -> {{"errands": []}}
JSON only."""

from echolink.owner import personalise  # noqa: E402
PROMPT = personalise(PROMPT)


def looks_like_errand(text: str) -> bool:
    return bool(TRIGGER.search(text or ""))


def wants_money(text: str) -> bool:
    return bool(MONEY.search(text or ""))


def clean(items) -> list[dict]:
    out = []
    for e in items if isinstance(items, list) else []:
        if not isinstance(e, dict) or e.get("kind") not in KINDS:
            continue
        x = {"kind": e["kind"]}
        for k, n in (("to", 60), ("body", 1000), ("when", 25), ("title", 120), ("place", 160)):
            v = re.sub(r"\s+", " ", str(e.get(k) or "")).strip()[:n]
            if v:
                x[k] = v
        if (w := re.match(r"(\d{4}-\d{2}-\d{2})[ T](\d{1,2}):(\d{2})", str(e.get("when") or "").strip())):
            x["when"] = f"{w[1]} {int(w[2]):02d}:{w[3]}"          # "2026-11-17T15:00:00" → "2026-11-17 15:00"
        else:
            x.pop("when", None)
        need = {"text": ("to",), "call": ("to",), "reminder": ("body",), "calendar": ("title",), "email": ("to",),
                "directions": ("place",), "note": ("body",)}[x["kind"]]
        if all(x.get(k) for k in need):
            out.append(x)
    return out[:6]


async def extract(backend, text: str, now: float | None = None) -> list[dict]:
    """The errands in `text` ([] when none, or when the model can't be asked)."""
    client = getattr(backend, "client", None)
    if client is None or not hasattr(backend, "_body"):
        return []
    base = now or time.time()
    t = time.localtime(base)
    days = ", ".join(time.strftime("%A %Y-%m-%d", time.localtime(base + 86400 * i)) for i in range(1, 8))
    msgs = [{"role": "system", "content": PROMPT.format(now=time.strftime("%A %Y-%m-%d %H:%M", t), days=days)},
            {"role": "user", "content": text[:1500]}]
    body = backend._body(msgs, stream=False, format=SCHEMA)
    body["options"] = {**body.get("options", {}), "temperature": 0}
    body["think"] = False
    r = await client.post("/api/chat", json=body)
    r.raise_for_status()
    try:
        return first_name_greeting(dictated(text, clean(json.loads(r.json()["message"]["content"]).get("errands"))))
    except (json.JSONDecodeError, KeyError, AttributeError):
        return []


RELAY = re.compile(r"\b(?:tell|let \w+(?: \w+)? know|ask|remind|introduce)\b", re.I)
SAYING = re.compile(r"\b(?:saying|that says|which says)\b[:,]?\s*(.+?)(?:\s+and (?:then )?(?:remind|call|text|email|put|add|schedule)\b.*)?$",
                    re.I | re.S)


def first_name_greeting(items: list[dict]) -> list[dict]:
    """'Hello Danny Ruiz, this is Echo' → 'Hello Danny, this is Echo' (people greet with the first name)."""
    for e in items:
        if e["kind"] == "text" and e.get("body"):
            e["body"] = re.sub(r"^(Hello|Hey|Hi) ([A-Z][\w'-]+)(?: [A-Z][\w'-]+)+,", r"\1 \2,", e["body"])
    return items


def dictated(text: str, items: list[dict]) -> list[dict]:
    """'…saying <words>' / '…that says <words>' means the owner dictated it: his exact words, never Echo's relay."""
    m = SAYING.search(text or "")
    if not m or RELAY.search(text[:m.start()]):
        return items
    words = m.group(1).strip().rstrip(".!") + "."
    words = words[0].upper() + words[1:]
    texts = [e for e in items if e["kind"] == "text"]
    if len(texts) == 1:
        texts[0]["body"] = words[:1000]
    return items


def describe(e: dict) -> str:
    k = e["kind"]
    return {"text": lambda: f'text {e["to"]}: "{e.get("body", "")}"', "call": lambda: f'call {e["to"]}',
            "reminder": lambda: f'reminder {e.get("when", "(no time)")}: {e["body"]}',
            "calendar": lambda: f'calendar: {e["title"]} {e.get("when", "")} {e.get("place", "")}'.strip(),
            "email": lambda: f'email {e["to"]}: {e.get("title", "")}', "directions": lambda: f'directions to {e["place"]}',
            "note": lambda: f'note: {e["body"]}'}[k]()


CAN_DO = ("You CAN text and call people yourself: texts and calls go out from the owner's own iPhone number through the "
          "Mac, the moment he taps Approve on the card you prepared (it shows who, their number and the exact words). "
          "Never say you can't send texts, press send or make calls. If you don't know who or what to say, ask. "
          "His contacts are on his tablet and Mac (never online): the tablet finds people itself, so never search the "
          "web for someone he wants to reach.")


def approve_line(user: str = "owner", kind: str = "text") -> str:
    """the owner's wording (2026-11-10) for how Echo hands over an approval."""
    name, pos = ("the loved one", "her") if user == "loved_one" else ("the owner", "his")
    if kind == "call":
        return f"{name}, tap Approve on the card that appears and I'll place the call from {pos} phone."
    return (f"{name}, tap Approve on the card that appears for the message to send from {pos} phone. Here is the text "
            f"of what I'm sending for you to review before approval:")


def errand_note(items: list[dict], money: bool, user: str = "owner") -> str:
    parts = []
    texts = [e for e in items if e["kind"] == "text"]
    if items:
        parts.append("I prepared these on the owner's tablet for him to approve with one tap each: "
                     + "; ".join(describe(e) for e in items) + ". Don't say any of it is already done.")
    if texts:
        e = texts[0]
        relay = "this is Echo" in (e.get("body") or "")
        parts.append(
            "For the text: say exactly \"" + approve_line(user) + "\" and then the full message itself in double "
            "quotes. The message in quotes IS what will be sent, so " + (
                personalise(f"write it warmly in your own words as Echo, the owner's assistant, starting \"Hello <first name>, this is "
                            f"Echo, the owner's assistant.\", keeping everything the owner asked to say (draft: \"{e.get('body', '')}\")")
                if relay else f"keep the owner's own words exactly: \"{e.get('body', '')}\"") + ". Nothing after the quote.")
    elif any(e["kind"] == "call" for e in items):
        parts.append("For the call: say \"" + approve_line(user, "call") + "\"")
    parts.append(CAN_DO)
    parts.append(CAN_DO)
    if money:
        parts.append("He also asked for something that spends money: say kindly that you never buy or pay for "
                     "anything yourself, and offer to look it up or prepare it so he can do it.")
    return "[Errands: " + " ".join(parts) + "]"
