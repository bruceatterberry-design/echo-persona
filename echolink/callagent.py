"""Echo makes a phone call himself (the owner approves each one on his tablet first).

Sound: the Mac's Phone app (calls go out through the owner's iPhone) is switched, for this call only, to
  microphone = "BlackHole 2ch"  ← Echo's voice is played into it
  output     = "BlackHole 16ch" → what the other person says is recorded from it
and switched back to "Use System Setting" afterwards. Nothing else on the Mac changes.

the owner's rules for every call (2026-11-10):
  1. "Hello <first name>, this is Echo. the owner wanted me to call you <business>." + one logical question about it.
  2. Listen. Follow-up questions or more talk → keep the conversation going (as Echo, honestly).
  3. Otherwise: "Thank you for your time, <name>. Talk to you soon." and WAIT for their answer before hanging up.
     If that answer is a follow-up question → continue; if not → hang up.
  Afterwards: a summary for the owner (saved with his call notes) and the full transcript.
Echo never pretends to be the owner, never agrees to money or commitments for him, and says plainly that he is an AI
assistant if asked. Nothing is recorded to disk except the transcript text and summary the owner sees.
"""
from __future__ import annotations

import io
import json
import os
import logging
import re
import secrets
import subprocess
import threading
import time
import wave
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from echolink import business as biz
from echolink.names import greeting_name, hello, thanks
from echolink.owner import personalise

log = logging.getLogger("echolink.call")

CORE = "http://127.0.0.1:8765"
OLLAMA = "http://127.0.0.1:11434"
HEAR_DEV = "BlackHole 16ch"     # Phone app output → Echo hears
SPEAK_DEV = "BlackHole 2ch"     # Echo speaks → Phone app microphone
RATE = 48000
MAX_CALL_S = 6 * 60
MAX_TURNS = 10
START_RMS = 0.0012     # someone started talking (a phone line's "hello" can be ~0.003 RMS)
QUIET_RMS = 0.0008     # ...and stopped
BUSINESS_WORDS = re.compile(r"\b(?:pharmacy|prescription|medication|medicine|refill|store|shop|price|cost|how much|"
                            r"in stock|available|availability|appointment|book|schedule|order|tires?|repair|clinic|"
                            r"office|doctor|dentist|bank|insurance|walmart|walgreens|cvs|restaurant|company|hours|"
                            r"open|closed)\b", re.I)
VOICEMAIL = re.compile(r"\b(voice ?mail|leave (?:a|your) (?:message|name)|after the (?:tone|beep)|not available|"
                       r"can'?t (?:come|get) to the phone|mailbox|record your message)\b", re.I)

SELF = """You are Echo: the owner's AI assistant. the owner built you on his own Mac at home, and you speak with a
voice made from his. You are not the owner and never pretend to be: you are Echo, his assistant, calling for him. You are
warm, plain-spoken and brief, like a friendly person on the phone.
Honesty rules (never break them):
- If asked whether you're a computer, a robot or AI: yes, you're the owner's AI assistant.
- You know ONLY what is written below (the reason for the call and what was said on this call). You do NOT know
  what the owner is doing right now, how he feels, his plans, places, times, prices, past conversations or inside jokes.
  Never invent any of that, and never claim to have heard or seen something. If asked, say you don't know and that
  you'll ask the owner to get back to them.
- Never agree to money, payments, favours, plans or promises for the owner, and never accept details on his behalf as
  settled (like who pays or where to meet): say you'll pass it on to the owner and he'll confirm.
- If someone doubts you're really from the owner, don't try to prove it with details: say that's fair, they can check
  with the owner directly, and you'll let him know they asked.
- It's the owner's Mac, not theirs. Talk to them as "you" and about the owner as "he".
- Plans they suggest (a time, a place, who pays, a favour) are NOT settled: say you'll pass it to the owner and he'll
  confirm with them. Never say "we're set" or that you will do something only a person can do.
What is true about you (use this when asked; never add to it): you run on the owner's Mac at home; you help the owner with
texts, calls, reminders, his documents and conversation; your voice is made from recordings of the owner's voice; the owner
asked you to make this call. You don't know what you or the owner did earlier today."""

PLAN_PROMPT = SELF + """

the owner asked you to phone {name}. His request, in his words: "{request}"
Plan the opening. Reply as JSON:
- "business": ONLY the end of the sentence "the owner wanted me to call you ..." (don't repeat those words). It is said
  TO {first}: "you/your" means {first}, "he/his" means the owner. Examples:
  "to introduce myself", "to see if you're free for lunch on Friday", "to let you know he'll be about 30 minutes
  late for dinner", "to see how your week is going".
- "question": ONE short, natural question to {first} about that business, the way a friendly person would ask it.
  Examples: introduction → "What do you think of the owner having an AI assistant?"; lunch → "Would Friday work for
  you?"; running late → "Is that okay with you?"; how's your week → "How's it been going?"
- "goal": one line: what the owner wants to find out or get across.
JSON only."""

WORDS_PROMPT = SELF + """

You are on a phone call with {name} for the owner. the owner wanted you to call {business}. Goal: {goal}
Call so far (Echo = you):
{transcript}

{task}
Rules: speak like a real person on the phone: short, warm, natural, 1-2 sentences. Don't repeat anything you
already said on this call. No lists, no emoji, no stage directions. Reply with only the words you say."""

TASKS = {
    "answer": "{first} just said something or asked you something. Reply to exactly that in 1-2 short sentences. "
              "Do NOT end with a question.",
    "ack": "{first} answered. Say ONE short, warm sentence that responds to what they actually said. No question, "
           "and no goodbye (your goodbye is added right after).",
    "answer_ack": "{first} asked you something after you'd said goodbye. Answer it in 1-2 short sentences. No "
                  "question and no goodbye (your goodbye is added right after).",
}
LIMIT = {"answer": 2, "ack": 1, "answer_ack": 2, "reply": 3, "give": 3, "wrap": 1, "agree": 3}
MAX_WORDS = 40       # sentences: phone-length, always
META = re.compile(r"\b(?:my bad|that'?s a question|i can'?t ask (?:you )?questions|as an ai language model|oh wait)\b", re.I)
BYE_SENTENCE = re.compile(r"\b(?:bye|goodbye|talk (?:to you )?soon|take care|have a (?:good|great|nice) (?:day|one|night))\b", re.I)

SUMMARY_PROMPT = """the owner asked his AI assistant Echo to phone {name} {business}. The call ("Echo" is the assistant,
"{first}" is {name}):
{transcript}
Write the summary the owner will read. 2-4 short sentences, plain words. Only what was actually said: never guess a
price, time or "no cost" that nobody stated. Always use names ("{first}", "the owner",
"Echo"), never "you", "I" or "me". Say what {first} actually said and answered, and list anything {first} asked for
or suggested (call it a request or suggestion for the owner to confirm; never say it was agreed or promised). Only what was said on the call: no guesses, no advice. If nobody answered, it went to
voicemail, or it was a wrong number, say that. Plain text only."""
SELF, PLAN_PROMPT, WORDS_PROMPT, SUMMARY_PROMPT = (personalise(x) for x in (SELF, PLAN_PROMPT, WORDS_PROMPT, SUMMARY_PROMPT))

PLAN_SCHEMA = {"type": "object", "properties": {"business": {"type": "string"}, "question": {"type": "string"},
                                                "goal": {"type": "string"}}, "required": ["business", "question"]}

# What the other person's words mean for the call (the code follows the owner's rules; the model only writes words)
QUESTION = re.compile(r"\?|^\s*(?:what|who|why|how|when|where|which)\b|\b(?:tell me|i wonder|any idea)\b", re.I)
BYE = re.compile(r"\b(?:bye|goodbye|talk (?:to you )?soon|talk later|gotta go|got to go|have to go|need to go|"
                 r"have a (?:good|great|nice)|take care|see (?:ya|you)|later,? (?:man|buddy|echo)|catch you)\b", re.I)
MISHEARD = re.compile(r"\b(?:who is this|who'?s this|what\?|sorry\?|pardon|can'?t hear|breaking up|say that again|"
                      r"come again|didn'?t catch)\b", re.I)
WRONG = re.compile(r"\b(?:wrong number|no(?:body|one)? (?:named|called) \w+ here|there'?s no \w+ here|"
                   r"don'?t know (?:any|a) \w+|no \w+ (?:lives )?here)\b", re.I)


def reaction(heard: str, closed: bool) -> str:
    """the owner's rules, as code: what Echo does next.
    misheard → say the opening again; wrong number → apologise and hang up; after the goodbye: a question → answer it
    and say goodbye again, anything else → hang up; before it: their goodbye → our goodbye; a question → answer;
    otherwise (they answered) → a short acknowledgement and the goodbye."""
    if WRONG.search(heard):
        return "wrong"
    if MISHEARD.search(heard) and len(heard.split()) <= 14:
        return "repeat"
    q = bool(QUESTION.search(heard))
    if closed:
        return "answer_close" if q else "hang_up"
    if BYE.search(heard) and not q:
        return "close"
    return "answer" if q else "ack_close"


def first_name(name: str) -> str:
    """How Echo refers to them (the greeting name from the contact; a business keeps its saved name)."""
    return greeting_name(name) or (name or "").strip() or "them"


def business_phrase(business: str) -> str:
    b = re.sub(r"^(?:hello \w+,? )?(?:this is echo\.? )?(?:owner (?:wanted|asked) me to (?:call|phone|ring) you\s*)+", "",
               business.strip(), flags=re.I).strip().rstrip(".")
    if re.match(r"^(to|about|because|so|for|and|since)\b", b, re.I):
        return b
    if re.match(r"^(the|your|his|her|a|an|our|this|that|some|lunch|dinner|plans)\b", b, re.I) or b.split(" ")[0].endswith("ing"):
        return "about " + b
    return "to " + b


def opening(name: str, business: str, question: str) -> str:
    b = business_phrase(business)
    q = question.strip()
    if q and not q.endswith("?"):
        q += "?"
    return personalise(f"{hello(name)}, this is Echo. the owner wanted me to call you {b}. {q}".strip())


def closing_line(name: str) -> str:
    return thanks(name)


# ---------------------------------------------------------------------------------------------------------------
# The brain: Echo's own words, from the same local model he always uses

class Brain:
    def __init__(self, http=None, model: str | None = None):
        import httpx
        self.http = http or httpx.Client(timeout=120)
        self._model = model

    def model(self) -> str:
        if not self._model:
            try:
                self._model = self.http.get(f"{CORE}/health", timeout=5).json()["llm"]["model"]
            except Exception:  # noqa: BLE001
                self._model = "qwen3.5:9b-q4_K_M"
        return self._model

    def ask(self, prompt: str, schema: dict | None, temperature: float = 0.3) -> str:
        body = {"model": self.model(), "stream": False, "think": False, "keep_alive": "24h",
                "options": {"temperature": temperature},
                "messages": [{"role": "user", "content": prompt}]}
        if schema:
            body["format"] = schema
        r = self.http.post(f"{OLLAMA}/api/chat", json=body)
        r.raise_for_status()
        return r.json()["message"]["content"]

    def plan_any(self, name: str, request: str, business_hint: bool = False, facts: dict | None = None) -> dict:
        """A business (shop, pharmacy, office ...): checklist + details needed; otherwise a personal call."""
        if business_hint or not greeting_name(name) or BUSINESS_WORDS.search(request + " " + name):
            p = biz.plan(self, SELF, name, request, facts or {})
            if p["kind"] == "business" or business_hint or not greeting_name(name):
                p["kind"] = "business"
                p["opening"] = personalise(f"Hello, this is Echo, the owner's AI assistant. the owner wanted me to "
                                           f"call you {business_phrase(p['business'])}. {p['question'].rstrip('?')}?")
                return p
        p = self.plan(name, request)
        p["kind"] = "personal"
        return p

    def plan(self, name: str, request: str) -> dict:
        j = json.loads(self.ask(PLAN_PROMPT.format(name=name, first=first_name(name), request=request[:500]),
                                PLAN_SCHEMA))
        business = str(j.get("business") or "to say hello").strip()
        question = str(j.get("question") or "How are you doing?").strip()
        return {"business": business, "question": question, "goal": str(j.get("goal") or "").strip(),
                "opening": opening(name, business, question)}

    def words(self, call: "Call", task: str):
        """Echo's words for this moment, one sentence at a time as the model writes them."""
        import httpx
        if call.kind == "business":
            prompt = biz.words_prompt(SELF, call, task, business_phrase(call.business))
        else:
            prompt = WORDS_PROMPT.format(name=call.name, business=business_phrase(call.business),
                                         goal=call.goal or business_phrase(call.business), transcript=call.text(),
                                         task=TASKS[task].format(first=first_name(call.name)))
        limit = LIMIT.get(task, 3)
        max_words = 50 if call.kind == "business" else MAX_WORDS
        asking_ok = call.kind == "business" and task in ("reply", "give", "agree") and not biz.all_answered(call)
        said = [x["text"].lower() for x in call.lines if x["who"] == "echo"]
        body = {"model": self.model(), "stream": True, "think": False, "keep_alive": "24h",
                "options": {"temperature": 0.7, "repeat_penalty": 1.15, "num_predict": 90},
                "messages": [{"role": "user", "content": prompt}]}
        buf, n, spoken = "", 0, 0
        with httpx.Client(timeout=120).stream("POST", f"{OLLAMA}/api/chat", json=body) as r:
            for line in r.iter_lines():
                if not line:
                    continue
                buf += json.loads(line).get("message", {}).get("content", "")
                while (m := re.search(r"^(.+?[.!?])\s+", buf, re.S)):
                    sent, buf = m.group(1).strip(), buf[m.end():]
                    if sent.lower() in " ".join(said):              # never the same sentence twice in a call
                        continue
                    if task not in ("answer", "reply", "give", "agree") and BYE_SENTENCE.search(sent):   # goodbye comes after
                        continue
                    if (sent.rstrip().endswith("?") and not asking_ok) or META.search(sent):   # personal: no questions
                        continue
                    words_n = len(sent.split())
                    if n and spoken + words_n > max_words:          # phone-length, even with long sentences
                        return
                    if words_n > max_words:
                        sent = " ".join(sent.split()[:max_words]).rstrip(",;:") + "."
                    yield sent.strip('"')
                    n += 1
                    spoken += words_n
                    if n >= limit or sent.rstrip().endswith("?"):      # one question, then let them answer
                        return
        last = buf.strip().strip('"')
        if last and last.lower() not in " ".join(said) and not (task not in ("answer", "reply", "give", "agree")
                                                                and BYE_SENTENCE.search(last)) \
                and (asking_ok or not last.endswith("?")) and not META.search(last) and n < limit \
                and spoken + len(last.split()) <= max_words:
            yield last

    def summary(self, call: "Call") -> str:
        return self.ask(SUMMARY_PROMPT.format(name=call.name, first=first_name(call.name), business=business_phrase(call.business),
                                              transcript=call.text() or "(nobody spoke)"), None, 0.2).strip()


# ---------------------------------------------------------------------------------------------------------------
# Sound in and out of the call

class CallAudio:
    def __init__(self):
        import sounddevice as sd
        self.sd = sd
        self._buf: list[np.ndarray] = []
        self._lock = threading.Lock()
        self._in = None
        self.muted = False                     # while Echo talks, what comes back is ignored (no echo of himself)

    def _dev(self, name: str, kind: str) -> int:
        for i, d in enumerate(self.sd.query_devices()):
            if d["name"] == name and d[f"max_{kind}_channels"] > 0:
                return i
        raise RuntimeError(f"{name} isn't installed")

    def start(self) -> None:
        self.level_max = 0.0
        self.callbacks = 0

        def cb(data, frames, t, status):
            self.callbacks += 1
            self.level_max = max(self.level_max, float(np.abs(data).max()))
            if not self.muted:
                with self._lock:
                    self._buf.append(data.mean(axis=1).copy())
        self._in = self.sd.InputStream(device=self._dev(HEAR_DEV, "input"), channels=2, samplerate=RATE,
                                       dtype="float32", blocksize=1440, callback=cb)
        self._in.start()

    def stop(self) -> None:
        if self._in:
            self._in.stop()
            self._in.close()
            self._in = None
        if hasattr(self, "_out"):
            self._out.stop()
            self._out.close()
            del self._out

    def drain(self) -> None:
        with self._lock:
            self._buf.clear()

    def _take(self) -> np.ndarray | None:
        with self._lock:
            if not self._buf:
                return None
            x = np.concatenate(self._buf)
            self._buf.clear()
            return x

    def listen(self, wait_s: float, quiet_s: float = 1.3, max_s: float = 25.0,
               cancelled=lambda: False) -> np.ndarray | None:
        """One utterance from the other person (16 kHz mono), or None if nobody spoke within wait_s."""
        frame = int(RATE * 0.03)
        noise, spoke, quiet, got = 0.0005, False, 0.0, []
        pre: list[np.ndarray] = []
        t0 = time.time()
        pending = np.zeros(0, dtype=np.float32)
        while not cancelled():
            x = self._take()
            if x is None:
                time.sleep(0.02)
                if not spoke and time.time() - t0 > wait_s:
                    return None
                continue
            pending = np.concatenate([pending, x])
            while len(pending) >= frame:
                f, pending = pending[:frame], pending[frame:]
                rms = float(np.sqrt(np.mean(f * f)))
                if not spoke:
                    noise = noise * 0.95 + rms * 0.05
                    pre = (pre + [f])[-10:]                      # 0.3 s before the first word
                    if rms > max(START_RMS, noise * 4):          # phone audio is quiet: very sensitive
                        spoke, got = True, pre[:]
                else:
                    got.append(f)
                    quiet = quiet + 0.03 if rms < max(QUIET_RMS, noise * 2) else 0.0
            if spoke and (quiet >= quiet_s or len(got) * 0.03 >= max_s):
                break
            if not spoke and time.time() - t0 > wait_s:
                return None
        if not got:
            return None
        y = np.concatenate(got)
        y = y[: len(y) // 3 * 3].reshape(-1, 3).mean(axis=1)    # 48 kHz → 16 kHz
        peak = float(np.max(np.abs(y))) if len(y) else 0.0
        if 0 < peak < 0.5:                                       # quiet phone line: bring it up for the recogniser
            y = y * min(40.0, 0.5 / peak)
        return y

    # --- Echo's voice into the call: ONE output stream for the whole call (no gaps between pieces), a small reserve
    # before speaking, and a clean refill pause (not stutter) if his voice is being made slower than real time.
    START_S = 0.5
    REFILL_S = 1.2

    def _out_start(self) -> None:
        self._q: list[np.ndarray] = []
        self._qn = 0                      # samples waiting
        self._qlock = threading.Lock()
        self.gate = False                 # playing (True) or gathering a reserve (False)
        self.dry = False                  # ran out mid-speech → bigger reserve next time
        self._carry = None                # last sample of the previous piece (smooth resampling across pieces)
        self._odd = b""

        def cb(out, frames, t, status):
            need, got = frames, []
            with self._qlock:
                if self.gate:
                    while need and self._q:
                        x = self._q[0]
                        take = x[:need]
                        got.append(take)
                        need -= len(take)
                        if len(take) == len(x):
                            self._q.pop(0)
                        else:
                            self._q[0] = x[len(take):]
                    self._qn -= frames - need
                    if need and not self._q:
                        self.gate, self.dry = False, True          # ran dry: wait for a reserve again
            y = np.concatenate(got) if got else np.zeros(0, np.float32)
            if len(y) < frames:
                y = np.concatenate([y, np.zeros(frames - len(y), np.float32)])
            out[:] = np.repeat(y[:, None], 2, axis=1)

        self._out = self.sd.OutputStream(device=self._dev(SPEAK_DEV, "output"), channels=2, samplerate=RATE,
                                         dtype="float32", blocksize=960, callback=cb)
        self._out.start()

    def feed(self, pcm: bytes, rate: int) -> None:
        """Add a piece of Echo's voice (16-bit mono at `rate`)."""
        if not hasattr(self, "_out"):
            self._out_start()
        pcm = self._odd + pcm
        self._odd = pcm[len(pcm) // 2 * 2:]
        x = np.frombuffer(pcm[: len(pcm) // 2 * 2], dtype="<i2").astype(np.float32) / 32768.0
        if not len(x):
            return
        if rate != RATE:                   # resample, continuing smoothly from the previous piece
            prev = np.array([x[0] if self._carry is None else self._carry], np.float32)
            xx = np.concatenate([prev, x])
            pos = np.arange(1, len(xx), rate / RATE)
            x = np.interp(pos, np.arange(len(xx)), xx).astype(np.float32)
            self._carry = xx[-1]
        with self._qlock:
            self._q.append(x)
            self._qn += len(x)
            if not self.gate and self._qn >= RATE * (self.REFILL_S if self.dry else self.START_S):
                self.gate = True

    def flush(self) -> None:
        """The end of what's to be said: play whatever is waiting, even if it's less than the reserve."""
        if hasattr(self, "_out"):
            with self._qlock:
                if self._qn:
                    self.gate = True

    def pending_s(self) -> float:
        return self._qn / RATE if hasattr(self, "_out") else 0.0

    def wait_drained(self, cancelled=lambda: False) -> None:
        while self.pending_s() > 0 and not cancelled():
            if not self.gate:
                self.flush()
            time.sleep(0.03)
        time.sleep(0.12)                    # the last block leaves the device
        self.dry = False
        self._carry = None

    def hush(self) -> None:
        """Stop talking now (the owner pressed End call)."""
        if hasattr(self, "_out"):
            with self._qlock:
                self._q, self._qn, self.gate = [], 0, False

    def speak(self, pcm: bytes, rate: int) -> None:
        self.feed(pcm, rate)
        self.flush()
        self.wait_drained()


def to_wav(x16: np.ndarray) -> bytes:
    b = io.BytesIO()
    with wave.open(b, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes((np.clip(x16, -1, 1) * 32767).astype("<i2").tobytes())
    return b.getvalue()


# ---------------------------------------------------------------------------------------------------------------
# The Phone app (Accessibility: the Echo Link process must be allowed in Privacy & Security → Accessibility)

def _osa(script: str, timeout: float = 15) -> str:
    r = subprocess.run(["osascript", "-"], input=script, capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip()[-300:])
    return r.stdout.strip()


AUDIO_MENU = """
tell application "Phone" to activate
tell application "System Events"
  repeat 40 times
    if exists process "Phone" then exit repeat
    delay 0.25
  end repeat
end tell
delay 0.8
tell application "System Events" to tell process "Phone"
  set m to menu 1 of menu bar item "Audio" of menu bar 1
  set names to name of every menu item of m
  set section to ""
  repeat with i from 1 to count of names
    set n to item i of names
    if n is "Microphone" then set section to "mic"
    if n is "Output" then set section to "out"
    if (section is "mic" and n is "{mic}") or (section is "out" and n is "{out}") then click menu item i of m
  end repeat
end tell
"""

# The Phone app is SwiftUI: AppleScript's "entire contents" can't see its buttons, so this walks the accessibility tree
# itself. press: the first button whose description/name is one of the wanted words → AXPress.
PRESS_JS = """
function run(argv) {
  const se = Application('System Events');
  const wants = argv[0].toLowerCase().split('|');
  const procs = argv[1].split('|');
  for (const pn of procs) {
    let p; try { p = se.processes.byName(pn); p.name(); } catch (e) { continue; }
    let hit = null;
    function walk(el, d) {
      if (hit || d > 16) return;
      let kids = []; try { kids = el.uiElements(); } catch (e) { return; }
      for (const k of kids) {
        let role = ''; try { role = k.role(); } catch (e) {}
        let s = ''; try { s = ((k.description() || '') + ' ' + (k.name() || '')).toLowerCase().replace('null', '').trim(); } catch (e) {}
        if (role === 'AXButton' && wants.some(w => s === w || s.startsWith(w + ' ') || s.endsWith(' ' + w))) { hit = k; return; }
        walk(k, d + 1);
      }
    }
    let wins = []; try { wins = p.windows(); } catch (e) {}
    for (const w of wins) { walk(w, 0); if (hit) break; }
    if (hit) {
      let label = ''; try { label = (hit.description() || '') + '/' + (hit.name() || ''); } catch (e) {}
      hit.actions.byName('AXPress').perform(); return 'pressed ' + pn + ': ' + label;
    }
  }
  return 'not found';
}
"""

FIELD_JS = """
function run(argv) {
  const se = Application('System Events');
  const p = se.processes.byName('Phone');
  let hit = null;
  function walk(el, d) {
    if (hit || d > 16) return;
    let kids = []; try { kids = el.uiElements(); } catch (e) { return; }
    for (const k of kids) {
      let role = ''; try { role = k.role(); } catch (e) {}
      let s = ''; try { s = (k.description() || '').toLowerCase(); } catch (e) {}
      if (role === 'AXTextField' && s === 'phone number') { hit = k; return; }
      walk(k, d + 1);
    }
  }
  for (const w of p.windows()) { walk(w, 0); if (hit) break; }
  if (!hit) return 'no field';
  if (argv[0] === 'get') return String(hit.value() || '');
  hit.focused = true;
  hit.value = argv[0];
  return String(hit.value() || '');
}
"""


DIAL_JS = """
function run(argv) {
  const number = argv[0], dry = argv[1] === 'dry';
  const se = Application('System Events');
  const phone = Application('Phone');
  phone.activate();
  delay(1.2);
  const p = se.processes.byName('Phone');
  p.frontmost = true;
  delay(0.4);
  function find(test) {
    let hit = null;
    function walk(el, d) {
      if (hit || d > 16) return;
      let kids = []; try { kids = el.uiElements(); } catch (e) { return; }
      for (const k of kids) { if (test(k)) { hit = k; return; } walk(k, d + 1); }
    }
    for (const w of p.windows()) { walk(w, 0); if (hit) break; }
    return hit;
  }
  const role = k => { try { return k.role(); } catch (e) { return ''; } };
  const desc = k => { try { return String(k.description() || '').toLowerCase(); } catch (e) { return ''; } };
  const isField = k => role(k) === 'AXTextField' && desc(k) === 'phone number';
  let field = find(isField);
  if (!field) {
    const kp = find(k => role(k) === 'AXButton' && desc(k) === 'keypad');
    if (!kp) return 'error: no keypad button';
    kp.actions.byName('AXPress').perform();
    for (let i = 0; i < 20 && !field; i++) { delay(0.25); field = find(isField); }
    if (!field) return 'error: keypad did not open';
  }
  const want = number.replace(/[^0-9]/g, '');            // exactly these digits, nothing before or after
  const digits = s => String(s || '').replace(/[^0-9]/g, '');
  const ok = () => digits(field.value()) === want;
  try { field.focused = true; field.value = want; } catch (e) {}
  delay(0.5);
  for (let tries = 0; tries < 2 && !ok(); tries++) {
    p.frontmost = true; try { field.focused = true; } catch (e) {}
    delay(0.2);
    se.keystroke('a', {using: 'command down'}); se.keyCode(51);    // clear what's there
    for (let i = 0; i < 20 && digits(field.value()); i++) { se.keyCode(51); }
    delay(0.2);
    se.keystroke(want);
    delay(0.7);
  }
  if (!ok()) return 'error: number did not go in (' + field.value() + ')';
  if (dry) return 'ready ' + field.value();
  const call = find(k => role(k) === 'AXButton' && desc(k) === 'call');
  if (!call) return 'error: no Call button';
  call.actions.byName('AXPress').perform();
  return 'calling ' + field.value();
}
"""

MAC_WINDOW = """on run argv
  display dialog (item 2 of argv) with title (item 1 of argv) buttons {"OK"} default button "OK" giving up after 1800
end run"""


def _js(script: str, *args: str, timeout: float = 30) -> str:
    r = subprocess.run(["osascript", "-l", "JavaScript", "-", *args], input=script, capture_output=True, text=True,
                       timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip()[-300:])
    return r.stdout.strip()


def press(words: str, procs: str = "Phone") -> bool:
    out = _js(PRESS_JS, words, procs)
    if out.startswith("pressed") and words not in "0123456789":
        log.info("call ui: %s", out)
    return out.startswith("pressed")


# What the call screens look like (written to the log during a call, so the hang-up can be aimed precisely)
SNAPSHOT_JS = """
function run(argv) {
  const se = Application('System Events');
  const out = [];
  for (const pn of ['Phone', 'FaceTime', 'NotificationCenter', 'ControlCenter', 'callservicesd', 'InCallService']) {
    let p; try { p = se.processes.byName(pn); p.name(); } catch (e) { continue; }
    try {
      const menus = p.menuBars[0].menuBarItems.name();
      out.push(pn + ' menus: ' + menus.join(','));
    } catch (e) {}
    function walk(el, d) {
      if (d > 16 || out.length > 120) return;
      let kids = []; try { kids = el.uiElements(); } catch (e) { return; }
      for (const k of kids) {
        let role = ''; try { role = k.role(); } catch (e) {}
        if (role === 'AXButton' || role === 'AXStaticText' || role === 'AXMenuButton') {
          let s = ''; try { s = (k.description() || '') + '/' + (k.name() || '') + '/' + (role === 'AXStaticText' ? k.value() : ''); } catch (e) {}
          out.push(pn + ' ' + role + ' ' + s);
        }
        walk(k, d + 1);
      }
    }
    let wins = []; try { wins = p.windows(); } catch (e) {}
    for (const w of wins) { let n = ''; try { n = w.name(); } catch (e) {} out.push(pn + ' WINDOW ' + n); walk(w, 0); }
  }
  return out.join(' | ');
}
"""


def snapshot(tag: str) -> None:
    try:
        log.info("call ui %s: %s", tag, _js(SNAPSHOT_JS, timeout=40)[:6000])
    except Exception as e:  # noqa: BLE001
        log.info("call ui %s: (couldn't read: %s)", tag, e)


TICKS = """
tell application "System Events" to tell process "Phone"
  set m to menu 1 of menu bar item "Audio" of menu bar 1
  set out to ""
  repeat with i in menu items of m
    try
      if (value of attribute "AXMenuItemMarkChar" of i) is not missing value then set out to out & (name of i as text) & "|"
    end try
  end repeat
  return out
end tell
"""


class PhoneApp:
    @staticmethod
    def _audio(mic: str, out: str) -> None:
        err = None
        for _ in range(4):                    # a just-opened Phone app needs a moment before its menus exist
            try:
                _osa(AUDIO_MENU.replace("{mic}", mic).replace("{out}", out), timeout=30)
                return
            except Exception as e:  # noqa: BLE001
                err = e
                time.sleep(1.5)
        raise err

    @staticmethod
    def ticked() -> list[str]:
        try:
            return [x for x in _osa(TICKS).split("|") if x]
        except Exception:  # noqa: BLE001
            return []

    def route(self) -> None:
        for _ in range(3):
            self._audio(SPEAK_DEV, HEAR_DEV)
            t = self.ticked()
            if SPEAK_DEV in t and HEAR_DEV in t:
                log.info("call sound: Phone app on %s", t)
                return
            log.warning("call sound: Phone app shows %s, trying again", t)
            time.sleep(1.0)
        raise RuntimeError("The Phone app wouldn't switch to Echo's sound cables.")

    def call_live(self) -> bool:
        """The call banner (with its End button) is still showing."""
        try:
            return "End" in _js(SNAPSHOT_JS, timeout=40)
        except Exception:  # noqa: BLE001
            return False

    def unroute(self) -> None:
        try:
            self._audio("Use System Setting", "Use System Setting")
        except Exception as e:  # noqa: BLE001
            log.warning("couldn't put the Phone app's sound back: %s", e)

    def dial(self, number: str, dry: bool = False) -> str:
        """Phone app in front → keypad → the number (checked) → Call; all in one go so nothing steals focus."""
        d = re.sub(r"\D", "", number)
        if len(d) == 10:
            d = "1" + d                                  # US: 1 + area code + number (no "+", it types as 0)
        out = _js(DIAL_JS, d, "dry" if dry else "", timeout=60)
        if out.startswith("error"):
            raise RuntimeError(out)
        return out

    def hang_up(self) -> bool:
        try:
            if press("end|end call|hang up|leave|decline", "Phone|FaceTime|NotificationCenter"):
                return True
        except Exception as e:  # noqa: BLE001
            log.warning("hang up button not found: %s", e)
        return False

    @staticmethod
    def dtmf(digit: str) -> bool:
        """'Press 1 for the pharmacy': the call banner's Keypad, then the digit (best effort; else Echo says it)."""
        try:
            press("keypad", "NotificationCenter")
            time.sleep(0.6)
            ok = press(digit.lower().replace("*", "star").replace("#", "pound"), "NotificationCenter|Phone")
            log.info("call ui: keypad %s → %s", digit, ok)
            return ok
        except Exception as e:  # noqa: BLE001
            log.warning("keypad press failed: %s", e)
            return False

    @staticmethod
    def snapshot(tag: str) -> None:
        snapshot(tag)

    def quit(self) -> None:
        try:
            _osa('tell application "Phone" to quit')
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------------------------------------------------------

def _quiet(fn, *a):
    try:
        fn(*a)
    except Exception as e:  # noqa: BLE001
        log.info("checklist update skipped: %s", e)


PHRASE_MAX = 90


def phrases(s: str) -> list[str]:
    """A long sentence → short phrases (the voice model drops words in long ones; same rule as the tablet)."""
    s = s.strip()
    if len(s) <= PHRASE_MAX:
        return [s] if s else []
    parts = [p for p in re.split(r"(?<=[,;:—])\s+|\s+(?=—)", s) if p.strip()]
    pieces: list[str] = []
    for p in parts:
        if len(p) <= PHRASE_MAX:
            pieces.append(p)
            continue
        cur: list[str] = []
        for w in p.split():
            if len(cur) >= 8 and w.lower() in {"and", "but", "which", "who", "because", "so", "then", "while", "with"}:
                pieces.append(" ".join(cur))
                cur = []
            cur.append(w)
            if len(cur) >= 16:
                pieces.append(" ".join(cur))
                cur = []
        if cur:
            pieces.append(" ".join(cur))
    out, cur_s = [], ""
    for p in pieces:
        if not cur_s:
            cur_s = p
        elif len(cur_s) + 1 + len(p) <= PHRASE_MAX:
            cur_s += " " + p
        else:
            out.append(cur_s)
            cur_s = p
    if cur_s:
        out.append(cur_s)
    return out


def list_or_stream(sentences, tail: str):
    for x in sentences:
        if x:
            yield x
    if tail:
        yield tail


@dataclass
class Call:
    id: str
    name: str
    number: str
    request: str
    business: str = ""
    question: str = ""
    goal: str = ""
    opening: str = ""
    state: str = "planned"        # planned → dialing → ringing → talking → ended
    lines: list[dict] = field(default_factory=list)
    summary: str = ""
    error: str = ""
    started: float = 0.0
    ended: float = 0.0
    stop: bool = False
    kind: str = "personal"
    checklist: list = field(default_factory=list)
    answers: dict = field(default_factory=dict)
    needs: list = field(default_factory=list)
    facts: dict = field(default_factory=dict)
    may_agree: str = ""
    result_text: str = ""

    def say(self, who: str, text: str) -> None:
        self.lines.append({"who": who, "text": text, "at": time.strftime("%H:%M:%S")})

    def last_echo_closed(self) -> bool:
        said = [x["text"] for x in self.lines if x["who"] == "echo"]
        return bool(said) and closing_line(self.name).lower()[:25] in said[-1].lower()

    def text(self) -> str:
        return "\n".join(f"{'Echo' if x['who'] == 'echo' else first_name(self.name)}: {x['text']}" for x in self.lines)

    def public(self) -> dict:
        out = {k: getattr(self, k) for k in ("id", "name", "number", "business", "opening", "state", "lines",
                                              "summary", "error", "kind", "checklist", "needs", "may_agree",
                                              "result_text")}
        out["answers"] = {str(k): v for k, v in self.answers.items()}
        out["lines"] = [dict(x, text=biz.redact(x["text"], self.facts)) for x in self.lines]
        return out


class CallAgent:
    def __init__(self, notes_root: Path, brain=None, audio_factory=CallAudio, phone=None, http=None):
        import httpx
        self.notes_root = notes_root
        self.brain = brain or Brain()
        self.audio_factory = audio_factory
        self.phone = phone or PhoneApp()
        self.http = http or httpx.Client(timeout=60)
        self.calls: dict[str, Call] = {}
        self.active: Call | None = None

    # -- planning (before the owner approves) --
    def plan(self, name: str, number: str, request: str, business_hint: bool = False) -> Call:
        p = self.brain.plan_any(name, request, business_hint) if hasattr(self.brain, "plan_any") else \
            dict(self.brain.plan(name, request), kind="personal")
        c = Call(secrets.token_hex(6), name, number, request, p["business"], p["question"], p.get("goal", ""),
                 p["opening"], kind=p.get("kind", "personal"), checklist=p.get("checklist", []),
                 needs=p.get("needs", []), may_agree=p.get("may_agree", ""))
        self.calls[c.id] = c
        return c

    def start(self, call_id: str, facts: dict | None = None, may_agree: str | None = None) -> Call:
        c = self.calls.get(call_id)
        if c is None:
            raise KeyError(call_id)
        for k, v in (facts or {}).items():
            k, v = str(k)[:60], str(v)[:200].strip()
            if v and not biz.NEVER.search(k):
                c.facts[k] = v
        if may_agree is not None:
            c.may_agree = str(may_agree)[:300]
        if self.active and self.active.state not in ("ended",):
            raise RuntimeError("Echo is already on a call.")
        if c.state != "planned":
            raise RuntimeError("That call was already made.")
        self.active = c
        c.state, c.started = "dialing", time.time()
        threading.Thread(target=self._run, args=(c,), daemon=True, name=f"call-{c.id}").start()
        return c

    def end(self, call_id: str) -> None:
        c = self.calls.get(call_id)
        if c:
            c.stop = True

    # -- the call itself --
    def _hear(self, audio, c: Call, wait_s: float, quiet_s: float = 1.3) -> str | None:
        """Their words; "" for sound that wasn't words (ringing); None when nothing at all came within wait_s."""
        x = audio.listen(wait_s, quiet_s, cancelled=lambda: c.stop)
        if x is None:
            return None
        if len(x) < 16000 * 0.3:
            return ""
        try:
            r = self.http.post(f"{CORE}/api/stt", params={"mode": "conversation"}, content=to_wav(x))
            return str(r.json().get("text") or "").strip() if r.status_code == 200 else ""
        except Exception:  # noqa: BLE001
            return ""

    def _speak(self, audio, c: Call, text: str, record: bool = True, wait: bool = True) -> None:
        text = personalise(text)
        if not text or c.stop:
            return
        if record:
            c.say("echo", text)
        audio.muted = True
        try:
            for part in phrases(text):                   # long sentences in short phrases: the voice skips words
                with self.http.stream("POST", f"{CORE}/api/tts/stream", json={"text": part}) as r:
                    rate = int(r.headers.get("X-Sample-Rate", "24000"))
                    for chunk in r.iter_raw():       # straight into the call's one audio stream, as it's made
                        if c.stop:
                            audio.hush()
                            return
                        audio.feed(chunk, rate)
            audio.flush()
        finally:
            if wait:
                self._done_speaking(audio, c)

    @staticmethod
    def _done_speaking(audio, c: Call) -> None:
        audio.wait_drained(cancelled=lambda: c.stop)
        time.sleep(0.2)
        audio.drain()                                # what came back while Echo talked isn't the other person
        audio.muted = False

    FILLER = "Okay."
    FILLER_AFTER_S = 1.0

    def _filler_pcm(self) -> tuple[bytes, int] | None:
        """'Okay.' in Echo's voice, made once, so it can be said instantly while he works out the rest."""
        if getattr(self, "_filler", None) is None:
            try:
                with self.http.stream("POST", f"{CORE}/api/tts/stream", json={"text": self.FILLER}) as r:
                    self._filler = (b"".join(r.iter_raw()), int(r.headers.get("X-Sample-Rate", "24000")))
            except Exception:  # noqa: BLE001
                self._filler = (b"", 24000)
        return self._filler if self._filler[0] else None

    def _speak_stream(self, audio, c: Call, sentences, tail: str = "") -> None:
        """Say each sentence the moment it's written (less waiting on the phone); one line in the transcript.
        If nothing is ready after a second, a quick "Okay." first, so the other person isn't left in silence."""
        import queue
        q: queue.Queue = queue.Queue()

        def produce():
            try:
                for x in list_or_stream(sentences, tail):
                    q.put(x)
            except Exception as e:  # noqa: BLE001
                log.warning("words failed: %s", e)
                if tail:
                    q.put(tail)
            q.put(None)
        threading.Thread(target=produce, daemon=True).start()
        said = []
        try:
            first = q.get(timeout=self.FILLER_AFTER_S)
        except queue.Empty:
            f = self._filler_pcm()
            if f and not c.stop:
                audio.muted = True
                audio.feed(*f)
                audio.flush()
                said.append(self.FILLER)
            first = q.get()
        sent = first
        while sent is not None and not c.stop:      # each sentence's voice follows on in the same stream
            self._speak(audio, c, sent, record=False, wait=False)
            said.append(sent)
            sent = q.get()
        self._done_speaking(audio, c)
        if said:
            c.say("echo", " ".join(said))

    def _converse(self, audio, c: Call) -> None:
        """After the opening: the owner's rules, until the goodbye has been answered (or nobody is there)."""
        closed, turns, nudged = False, 0, False
        while not c.stop and turns < MAX_TURNS and time.time() - c.started < MAX_CALL_S:
            heard = self._hear(audio, c, wait_s=7 if closed else 15)
            if not heard:
                if closed or nudged:
                    break                        # no answer to the goodbye (or twice silent): done
                nudged = True
                g = greeting_name(c.name)
                self._speak(audio, c, f"{g}, are you still there?" if g else "Are you still there?")
                continue
            c.say(first_name(c.name), heard)
            turns += 1
            what = reaction(heard, closed)
            if turns >= MAX_TURNS - 1 and what == "answer":
                what = "answer_close"
            if what == "hang_up":
                break
            if what == "wrong":
                self._speak(audio, c, "Oh, I'm so sorry, I must have the wrong number. Have a good day.")
                break
            if what == "repeat":
                self._speak(audio, c, "Sorry about that. " + c.opening)
                continue
            if what == "close":
                self._speak(audio, c, closing_line(c.name))
                closed = True
                continue
            task = {"answer": "answer", "ack_close": "ack", "answer_close": "answer_ack"}[what]
            self._speak_stream(audio, c, self.brain.words(c, task),
                               tail=closing_line(c.name) if what != "answer" else "")
            closed = what != "answer"

    def _converse_business(self, audio, c: Call, first: str) -> None:
        """A business: phone menus, hold, then the checklist; the owner's goodbye rules at the end.
        Silence vs music: None = true silence; "" = sound that isn't words (hold music, tones) → keep waiting."""
        heard, said_opening, closed, turns = first, False, False, 0
        on_hold, quiet_since, nudged = False, None, False
        while not c.stop and turns < 40 and time.time() - c.started < biz.BUSINESS_MAX_S:
            if heard is None or heard == "":
                if closed:
                    break                                # no answer to the goodbye: done
                if heard is None:                        # real silence
                    quiet_since = quiet_since or time.time()
                    limit = biz.HOLD_MAX_S if on_hold else 75
                    if time.time() - quiet_since > limit:
                        break
                    if said_opening and not on_hold and not nudged and time.time() - quiet_since > 14:
                        nudged = True
                        self._speak(audio, c, "Hello, are you still there?")
                heard = self._hear(audio, c, wait_s=15)
                continue
            quiet_since, nudged = None, False
            c.say(c.name, heard)
            turns += 1
            if closed:                                   # after Echo's goodbye: a question → answer, else hang up
                if biz.ANYTHING_ELSE.search(heard):
                    self._speak(audio, c, "No, that's everything. Thanks again!")
                    break
                if reaction(heard, True) == "hang_up":
                    break
                self._speak_stream(audio, c, self.brain.words(c, "reply" if not biz.all_answered(c) else "wrap"),
                                   tail=biz.CLOSING)
                heard = self._hear(audio, c, wait_s=7)
                continue
            kind = biz.quick_kind(heard, c)
            if kind == "menu":
                a = biz.assess(self.brain, c) if hasattr(self.brain, "ask") else {"menu_choice": "0"}
                choice = a["menu_choice"] or "0"
                c.say("note", f"Phone menu: choosing {choice}")
                if not (choice.isdigit() and hasattr(self.phone, "dtmf") and self.phone.dtmf(choice)):
                    self._speak(audio, c, choice if not choice.isdigit() else
                                ("Representative." if choice == "0" else choice))
                heard = self._hear(audio, c, wait_s=20)
                continue
            if kind == "recording":                      # a recorded message: we're waiting in a queue
                on_hold = True
                heard = self._hear(audio, c, wait_s=30)
                continue
            if kind == "hold":
                self._speak(audio, c, "Sure, I'll hold.")
                on_hold = True
                heard = biz.wait_on_hold(lambda w: self._hear(audio, c, wait_s=w), c, time.time())
                continue
            on_hold = False
            if not said_opening:                         # a person picked up: the owner's opening
                self._speak(audio, c, c.opening)
                said_opening = True
                heard = self._hear(audio, c, wait_s=20)
                continue
            if kind == "wrap" or (BYE.search(heard) and not QUESTION.search(heard)):
                self._speak_stream(audio, c, self.brain.words(c, "wrap"), tail=biz.CLOSING)
                closed = True
                heard = self._hear(audio, c, wait_s=7)
                continue
            self._speak_stream(audio, c, self.brain.words(c, kind if kind in ("give", "agree") else "reply"))
            if hasattr(self.brain, "ask"):               # checklist bookkeeping while THEY talk (the brain is free)
                threading.Thread(target=lambda: _quiet(biz.assess, self.brain, c), daemon=True).start()
            heard = self._hear(audio, c, wait_s=25)

    def _run(self, c: Call) -> None:
        audio = None
        try:
            audio = self.audio_factory()
            self._filler_pcm()                           # ready before anyone answers
            self.phone.route()
            audio.start()
            self.phone.dial(c.number)
            c.state = "ringing"
            if hasattr(self.phone, "ticked"):
                def reroute():                           # once the call exists, select the cables again for it
                    try:
                        self.phone.route()
                    except Exception as e:  # noqa: BLE001
                        log.warning("call sound: re-route failed: %s", e)
                threading.Timer(2.5, reroute).start()

                def levels():
                    while c.state in ("ringing", "talking") and not c.stop:
                        log.info("call hearing: loudest %.4f, %d sound blocks", getattr(audio, "level_max", -1),
                                 getattr(audio, "callbacks", -1))
                        audio.level_max = 0.0
                        time.sleep(3)
                threading.Thread(target=levels, daemon=True).start()
            if hasattr(self.phone, "snapshot"):          # what the call screen shows (for aiming the hang-up)
                for t in (4, 20):
                    threading.Timer(t, self.phone.snapshot, args=(f"{t}s",)).start()
            # answered = real words from the other end (ringing tones are never words)
            first_heard = ""
            t0 = time.time()
            heard_any = 0.0
            while not c.stop and time.time() - t0 < 50 and not first_heard:
                first_heard = self._hear(audio, c, wait_s=min(15.0, max(1.0, 50 - (time.time() - t0))), quiet_s=0.8)
                heard_any = max(heard_any, getattr(audio, "level_max", 1.0))
                if first_heard is None and hasattr(self.phone, "call_live") and heard_any < 1e-4 \
                        and self.phone.call_live():
                    # the call is up but not a sound reaches Echo (not even ringing): speak anyway, keep listening
                    log.warning("call hearing: total silence on a live call; starting anyway")
                    c.say("note", "I couldn't hear the line, so I started talking anyway.")
                    first_heard = "(silence)"
                    break
                if first_heard is None:
                    break                                # nothing at all for a while, and no live call: no answer
            if c.stop:
                c.say("note", "the owner ended the call.")
            elif not first_heard:
                c.say("note", "No answer.")
            elif c.kind == "business":
                c.state = "talking"
                if VOICEMAIL.search(first_heard or "") and not biz.MENU.search(first_heard or ""):
                    c.say(first_name(c.name), first_heard)
                    self._hear(audio, c, wait_s=25, quiet_s=2.5)
                    self._speak(audio, c, f"Hello, this is Echo, the owner's AI assistant, calling "
                                          f"{business_phrase(c.business)}. the owner will call back. Thank you.")
                else:
                    self._converse_business(audio, c, "" if first_heard == "(silence)" else first_heard)
            elif first_heard == "(silence)":
                c.state = "talking"
                self._speak(audio, c, c.opening)
                first_heard = ""
                self._converse(audio, c)
            elif VOICEMAIL.search(first_heard) and c.kind != "business":
                c.state = "talking"
                c.say(first_name(c.name), first_heard)
                self._hear(audio, c, wait_s=25, quiet_s=2.5)     # let the greeting finish (until the beep)
                self._speak(audio, c, f"{hello(c.name)}, this is Echo, the owner's assistant. the owner wanted me "
                                      f"to call you {business_phrase(c.business)}. He'll try you again soon. "
                                      f"Talk to you soon.")
            else:
                c.state = "talking"
                c.say(first_name(c.name), first_heard)
                self._speak(audio, c, c.opening)
                self._converse(audio, c)
                if c.stop:
                    c.say("note", "the owner ended the call.")
        except Exception as e:  # noqa: BLE001
            log.warning("call failed: %s", e)
            c.error = str(e)[:300]
        finally:
            if hasattr(self.phone, "snapshot"):
                self.phone.snapshot("before hang-up")
            try:
                hung = self.phone.hang_up()
            except Exception:  # noqa: BLE001
                hung = False
            if audio:
                try:
                    audio.stop()
                except Exception:  # noqa: BLE001
                    pass
            self.phone.unroute()                         # sound back to normal BEFORE anything else
            if not hung and hasattr(self.phone, "quit") and self.phone.call_live():
                self.phone.quit()                        # last resort: closing the Phone app ends the call
            c.state, c.ended = "summary", time.time()
            try:
                c.summary = self.brain.summary(c) if any(x["who"] not in ("echo", "note") for x in c.lines) else \
                (c.lines[-1]["text"] if c.lines else "Nobody answered.")
            except Exception:  # noqa: BLE001
                c.summary = "The call ended. (I couldn't write a summary.)"
            if c.kind == "business" and any(x["who"] not in ("echo", "note") for x in c.lines):
                try:
                    c.result_text = biz.result_text(self.brain, c, business_phrase(c.business))
                    self.text_owner(c.result_text)
                except Exception as e:  # noqa: BLE001
                    log.warning("results text failed: %s", e)
            self._save(c)
            c.state = "ended"
            if not os.environ.get("PYTEST_CURRENT_TEST") and not getattr(self, "no_windows", False):
                self.show_on_mac(c)                      # (practice calls and tests never pop windows on the Mac)

    def text_owner(self, text: str) -> None:
        """The answers from a business call, as a text to the owner's own phone (config/echo_calls.json)."""
        try:
            cfg = json.loads((Path(__file__).resolve().parent.parent / "config" / "echo_calls.json").read_text())
        except Exception:  # noqa: BLE001
            cfg = {}
        to = cfg.get("text_results_to")
        if not to or getattr(self, "no_texts", False):
            return
        from echolink.macphone import MacPhone
        MacPhone(self.notes_root).text("owner", "the owner", to, text[:400])

    @staticmethod
    def show_on_mac(c: Call) -> None:
        """A small window on the Mac with the summary (the tablet shows the same, with the whole conversation)."""
        try:
            who = re.sub(r"\s*\(.*?\)\s*", " ", c.name).strip() or "someone"     # "Terry (cell)" → "Terry"
            title = "Echo's call (test window)" if c.id == "test" else f"Echo's call with {who}"
            subprocess.Popen(["osascript", "-e", MAC_WINDOW, title,
                              (c.summary or "The call ended.") + "\n\n(Saved with the owner's call notes.)"],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception as e:  # noqa: BLE001
            log.warning("couldn't show the summary window: %s", e)

    def _save(self, c: Call) -> None:
        d = self.notes_root / "owner"
        d.mkdir(parents=True, exist_ok=True, mode=0o700)
        rec = {"id": c.id, "at": time.strftime("%Y-%m-%d %H:%M"), "who": c.name,
               "text": biz.redact(f"Echo called {c.name} ({c.business}). {c.summary}"
                                  + (f"\n\n{c.result_text}" if c.result_text else "")
                                  + f"\n\nWhat was said:\n{c.text()}", c.facts)}
        with (d / "notes.jsonl").open("a") as f:
            f.write(json.dumps(rec) + "\n")
        (d / "notes.jsonl").chmod(0o600)
        with (d / "outbox.jsonl").open("a") as f:
            f.write(json.dumps({"at": time.strftime("%Y-%m-%d %H:%M:%S"), "kind": "echo_call", "name": c.name,
                                "number": c.number, "ok": not c.error, "seconds": round(c.ended - c.started)}) + "\n")
