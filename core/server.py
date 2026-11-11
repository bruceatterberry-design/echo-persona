"""Echo Persona Core HTTP/WebSocket server. Serves the renderer UI on localhost."""
from __future__ import annotations

import logging
import time
from logging.handlers import RotatingFileHandler

import asyncio
import base64
import concurrent.futures
import json
import subprocess
import os
import re
import threading
from pathlib import Path
from contextlib import asynccontextmanager
from typing import Literal

import httpx
import numpy as np
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, PlainTextResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from core import browser, hearing, music, permissions, tools, trainer as training
from core import workspace as cws
from core import controller
from core import executive
from core import people
from core import development
from core import initiative
from core import longterm
from core import skills
from core import selfmodel
from core import datecalc
from memory import grounding
from memory import gaps as mem_gaps
from core.config import ROOT, PersonaConfig, load_config, load_personality
from core.llm import LLMBackend, make_backend
from core.sched import SCHED, Preempted
from core.state import CognitiveState
from core import focus
from core import social
from core import metacog
from core import edim_link   # EDIM + awareness (Phase 3): off unless ECHO_EDIM / ECHO_AWARENESS
from core import showphoto
from core.flowers import Flowers
from core.schema import speakable, StreamParser
from voice.stt import MAX_AUDIO_BYTES, MLXWhisperSTT, STTBackend, STTError, decode_audio, make_stt
from face import expressions as face_expressions
from face.library import FaceError, FaceLibrary
from face.gphotos import GooglePhotos
from memory.photos import OllamaDescriber, PhotoError, PhotoLibrary
from transfer import phone_sync
from processing import ingest
from personality import build as persona
from personality import evals as pevals
from personality.versions import Versions
from voice.studio import CONSENT_TEXT, MAX_TAKE_BYTES, Studio, StudioError
from voice.wake import detect_end, detect_wake, is_noise_transcript
from voice.speaker import NAME_RE as SPEAKER_NAME_RE, SpeakerID
from core import errands
from documents.store import Documents, doc_note
from memory.store import LIFE_FIELDS, Kind, Memory, MemoryStore, Period, Status, format_for_prompt, format_summaries, parse_remember
from memory import archive, summarize as summarizer
from memory.entities import Entities
from memory.retrieve import Retriever, format_for_message
from memory import backup as mem_backup, consolidate
from memory.context import Pack
from memory.learn import Learner
from memory import conflicts as conflict_check
from memory.embed import Embedder
from memory import candidates as mem_candidates
from memory.curiosity import ASK_ME, Curiosity, asked_in, hint as curiosity_hint
from memory import dreamq
from core.llm import OllamaBackend

log = logging.getLogger("echo")
MAX_TURNS = 20  # short-term context sent to the LLM


def setup_logging(cfg: PersonaConfig) -> None:
    d = ROOT / cfg.log_dir
    d.mkdir(exist_ok=True)
    h = RotatingFileHandler(d / "echo.log", maxBytes=2_000_000, backupCount=3)
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    log.addHandler(h)
    log.setLevel(logging.INFO)


STOP = set("""a about after again all also am an and any are as at be because been before being but by can could
did do does doing down for from get got had has have having he her here hers him his how i if in into is it its just
know like me more my no not now of off on once only or our out over own really remember said say she so some still
such talk talked tell than that the their them then there these they thing things think this those through to today
too up us very was we well were what when where which while who why will with would yes yesterday you your day days
time good great nice okay loved_one echo owner""".split())


def content_words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z']+", text.lower()) if len(w) > 2 and w not in STOP}


class ConsentIn(BaseModel):
    name: str


class StatusIn(BaseModel):
    status: str


class TraitIn(BaseModel):
    status: str | None = None   # pending | approved | rejected
    trait: str | None = None    # the owner's edited wording


class CondensedIn(BaseModel):
    lines: list[str] | None = None   # the owner's edited summary lines
    use: bool | None = None          # use the summary (True) or the full approved list (False)


class FactIn(BaseModel):
    status: str                 # saved | rejected


class VersionIn(BaseModel):
    note: str | None = Field(None, max_length=200)


class EvalCaseIn(BaseModel):
    approved: bool | None = None
    remove: bool = False


class TryIn(BaseModel):
    question: str


class SayIn(BaseModel):
    text: str


class RateIn(BaseModel):
    question: str
    answer: str
    rating: str                 # like_me | not_like_me
    note: str = ""


class FaceReviewIn(BaseModel):
    status: str | None = None
    preferred: bool | None = None
    age_label: str | None = None


class FacePickIn(BaseModel):
    email: str = Field(max_length=200)
    group: Literal["current", "younger"]
    age: str = Field("unknown", max_length=10)


class PhotoPatchIn(BaseModel):
    status: str | None = None
    caption: str | None = Field(None, max_length=500)
    category: str | None = Field(None, max_length=30)


class PhotoCategoryIn(BaseModel):
    label: str = Field(max_length=40)


class PhotoPickIn(BaseModel):
    email: str = Field(max_length=200)
    category: str = Field(max_length=30)


class PhotoDescribeIn(BaseModel):
    sha256: str | None = Field(None, max_length=64)


class MemoryPatch(BaseModel):
    content: str | None = None
    kind: Kind | None = None
    status: Status | None = None
    period: Period | None = None
    when_text: str | None = None
    year: int | None = None
    people: str | None = None
    place: str | None = None
    told_by: str | None = None


class CandidateIn(BaseModel):
    action: Literal["approve", "reject"]
    content: str | None = Field(None, min_length=1, max_length=2000)
    period: Period | None = None
    when_text: str | None = Field(None, max_length=60)
    year: int | None = Field(None, ge=1900, le=2200)
    people: str | None = Field(None, max_length=300)
    place: str | None = Field(None, max_length=200)


class ConflictIn(BaseModel):
    action: Literal["keep_both", "keep_a", "keep_b"]


class ClaudeIn(BaseModel):
    enabled: bool | None = None
    jobs: dict[str, bool] | None = None


# Dream Bridge bodies live at module level: with `from __future__ import annotations` a model defined
# inside create_app can't be resolved by FastAPI, which then wants `body` as a query parameter (422).
class InsightIn(BaseModel):
    text: str = Field(min_length=10, max_length=training.INSIGHT_CHARS)
    source: str = Field(pattern=r"^dream-insight:[A-Za-z0-9_.:-]{1,80}$")
    approved_by: str = Field(default="owner", max_length=40)


class DreamLinkIn(BaseModel):
    a: int
    b: int
    source: str = Field(pattern=r"^dream:[A-Za-z0-9_.:;-]{1,200}$")
    approved_by: str = Field(default="owner", max_length=40)




SYSTEM_EXTRA = (
    "\n\nI can open web pages in Chrome on this Mac when asked with \"open\" or \"go to\" plus a saved place (shortcuts "
    "in config/browser.json, such as the Voice Studio or the Weather Channel) or an address said out "
    "loud (\"go to apple dot com\"). That happens before I answer. If a request to open something reaches me anyway, "
    "I never say I can't open websites; I ask them to say it again with the address, like \"go to apple dot com\"."
    "\n\nI can also read a web page myself when someone gives me its address and asks what it says: I use my "
    "read_webpage tool when it's offered, and I never ask them to open, copy or paste the page for me."
    "\n\nI can play music on YouTube when asked like \"play Hotel California on YouTube\" or \"open YouTube and play "
    "some blues\"; that also happens before I answer. I never say I'm opening, playing or doing something unless it "
    "really happened: if such a request reaches me, it didn't, so I say I couldn't do it that way and suggest those words."
    "\n\nI can show a photo from my photo memories on the screen when someone asks to see it (\"Can you show me the "
    "picture?\"); that also happens before I answer. I may ask \"Would you like to see it?\" about a photo I remember, "
    "but I never mention a photo gallery or album: there isn't one to open."
    " Other than that I never offer to open, play or show something myself (\"Shall I open this link?\"), because a yes can't make it "
    "happen; I tell them the words that work instead, like \"play a cat video on YouTube\"."
    "\n\nWhen someone talks to me out loud, their message may start with a note like [Recognised by voice: the owner "
    "is speaking]. I trust it, so I know who I'm talking with, but voice recognition can be wrong: if something "
    "important depends on who it is, I ask kindly. I never read these notes aloud."
    "\n\nHow I use my memory: I only know what is in my notes (above, and the notes that come with a message) and "
    "what is said in this conversation. When someone asks about a person, pet, place or event, I tell exactly what my "
    "notes say, in a sentence or two, then say that's all I remember and invite them to tell me more. I never add "
    "details to make a memory sound fuller: no feelings, habits, looks, outcomes, places or times that aren't written "
    "down. If my notes have nothing, I say I don't know.\n"
    "Two made-up examples to show how (Pat, Rex and Lake Elm are not real; they are not in my memory):\n"
    "- If my notes said only \"Pat has a dog, Rex.\" and someone asked \"Tell me about Rex\", I'd say: \"Rex is Pat's "
    "dog. That's all I have about him so far. Will you tell me more?\" Not \"Rex is a playful dog who loves walks\".\n"
    "- If my notes said only \"Pat went to Lake Elm once.\" and someone asked about that trip, I'd say: \"Pat went to "
    "Lake Elm once; that's all my notes say, so I don't know how it went.\" I don't guess how it went, who was there, "
    "how anyone felt, how often it happened, or what anyone learned from it, and I don't join two separate notes into "
    "one story unless a note says they belong together.")


def voice_ask(owner_name: str = "") -> str:
    """Hint when the voice check wasn't sure but it sounds most like the owner."""
    q = f"Is that you, {owner_name}?" if owner_name else "Is that you?"
    return (f"[My voice check wasn't sure who this is, but it sounds most like the owner. I answer normally without using "
            f"a name, then END my reply with exactly: \"{q}\"]")
OFFER_S = 180   # a "yes" counts for my offer this long
PHOTO_S = 600   # "show me the picture" means the photo we talked about this recently


def self_named(text: str, names: dict[str, str]) -> str | None:
    """"This is Pat" / "Hi Echo, it's Sam" → the profile ("owner" / "loved_one") whose configured name was said
    (owner_name / loved_one_name in config/echo.yaml). "It's Pat's birthday" → None. No names set → None."""
    for profile, name in names.items():
        if not name:
            continue
        pat = (r"(?:^|[.!?,]\s*)(?:(?:hey|hi|hello|okay|ok)[,]?\s+)?(?:echo[,.]?\s+)?(?:this\s+is|it'?s|it\s+is|i'?m|"
               r"i\s+am|here'?s)\s+" + re.escape(name.split()[0]) + r"(?!'|’|s\b)\b")
        if re.search(pat, text or "", re.I):
            return profile
    return None


def _who_q() -> re.Pattern:
    """My "who is this?" questions (names from this install's settings, core/people.py)."""
    n = people.alternation()
    return re.compile(r"\bis (?:that|this) (?:you|" + n + r")\b|\bam i (?:talking|speaking|chatting) (?:with|to)\b|"
                      r"\b(?:" + n + r") or (?:" + n + r")\b|\bwho(?:'s| is| am i) (?:this|speaking|talking|here)\b", re.I)


_ONE_TIME = re.compile(r"\s*\[(?:I'm the owner's digital self, and my memories|(?:the owner|the loved one) (?:just answered|told me more)|"
                       r"Nothing was opened or played|My voice check wasn't sure|You can use tools in this message|"
                       r"the owner asked you to research|My memory check|About me, checked facts|Date check|Social note|Memory check|"
                       r"Correction check|Style note|Conversation note|Camera|Document|Call prep|After the call|Text message|Errands)[^\]]*\]")


DOCS = Documents()          # scanned documents per person (documents/store.py); read here, received by Echo Link
CAMERA_MAX_PICTURES = 3
CAMERA_MAX_B64 = 2_000_000          # ~1.5 MB per JPEG; the app sends ~1024 px pictures


def camera_pictures(msg: dict) -> dict | None:
    """Pictures from Show Echo: {"images": [base64 JPEG, …], "camera": "back"|"front"}, oldest first. Anything that
    isn't a reasonable JPEG is dropped; nothing is written to disk or logged."""
    imgs = msg.get("images")
    if not isinstance(imgs, list) or not imgs:
        return None
    ok = []
    for b in imgs[-CAMERA_MAX_PICTURES:]:
        if isinstance(b, str) and 100 < len(b) <= CAMERA_MAX_B64:
            try:
                if base64.b64decode(b[:16] + "=" * (-len(b[:16]) % 4))[:3] == b"\xff\xd8\xff":
                    ok.append(b)
            except (ValueError, TypeError):
                pass
    return {"images": ok, "camera": "front" if msg.get("camera") == "front" else "back"} if ok else None


def group_talk(msg: dict) -> dict | None:
    """{"group": {"with": "Joe", "asker": "owner"|"guest", "lines": [{"who": "owner"|"guest"|"echo", "text"}, …]}}"""
    g = msg.get("group")
    if not isinstance(g, dict):
        return None
    name = re.sub(r"[^\w .'-]", "", str(g.get("with", "")))[:40].strip() or "the other person"
    lines = []
    for x in (g.get("lines") or [])[-12:]:
        if isinstance(x, dict) and x.get("who") in ("owner", "guest", "echo") and str(x.get("text", "")).strip():
            lines.append({"who": x["who"], "text": re.sub(r"[\[\]]", "", str(x["text"]))[:400]})
    return {"with": name, "asker": "guest" if g.get("asker") == "guest" else "owner", "lines": lines}


def group_note(g: dict) -> str:
    names = {"owner": "the owner", "guest": g["with"], "echo": "You (Echo)"}
    said = "\n".join(f"{names[x['who']]}: {x['text']}" for x in g["lines"]) or "(nothing yet)"
    return (f"[Group talk: the owner is on a speakerphone call with {g['with']}, who agreed that you listen and talk. "
            f"What was said just now, oldest first:\n{said}\nThe newest message was said to you by "
            f"{names[g['asker']]}. Answer out loud to both of them: briefly and naturally (one to three sentences), as "
            f"the owner's digital self. Don't bring up the owner's private matters, health, money, or the loved one's school work with "
            f"{g['with']} unless the owner asks you to. You can't hear the call except through this room. Nothing from "
            f"this call is kept.]")


TASKS = ("call_prep", "after_call", "text_draft")


_INFO_Q = re.compile(r"^\s*(?:(?:hey |ok )?echo,?\s+)?(?:who|what|when|where|which|how (?:many|much|tall|old|far|long|big)|"
                     r"why did|is it true|did)\b", re.I)
_PERSONAL = re.compile(r"\b(?:i|me|my|mine|we|us|our|you|your|yourself|echo|loved_one|owner|owner|mom|milo|"
                       r"remember|feel|feeling|today'?s schedule|calendar|weather)\b", re.I)


def side_window_question(text: str) -> bool:
    """A general-knowledge question (worth showing sources beside the answer); never one about the owner, the loved one or Echo."""
    t = (text or "").strip()
    return bool(_INFO_Q.search(t)) and len(t.split()) >= 4 and not _PERSONAL.search(t)


def recent_sources(text: str, turns: list[dict]) -> list[dict]:
    """Sources kept with a recent answer about the same thing ("Sources: title (url); ...")."""
    words = {w for w in re.findall(r"[a-z0-9]{4,}", text.lower())}
    for t in reversed(turns):
        c = t.get("content") or ""
        if t.get("role") != "assistant" or "\n\nSources: " not in c:
            continue
        body, src = c.split("\n\nSources: ", 1)
        if len(words & set(re.findall(r"[a-z0-9]{4,}", body.lower()))) >= 2:
            return [{"title": m.group(1).strip()[:120], "url": m.group(2), "snippet": ""}
                    for m in re.finditer(r"([^;]+?) \((https?://[^)\s]+)\)", src)][:6]
    return []


def phone_task(msg: dict) -> dict | None:
    """{"task": {"kind": call_prep|after_call|text_draft, "who": "the loved one"}} from the calling section of the app. Only the
    contact's NAME comes to the Mac; numbers and the contact list stay on the device."""
    t = msg.get("task")
    if not isinstance(t, dict) or t.get("kind") not in TASKS:
        return None
    who = re.sub(r"[^\w .'-]", "", str(t.get("who", "")))[:40].strip() or "them"
    return {"kind": t["kind"], "who": who}


def task_note(task: dict, speaker: str | None) -> str:
    from echolink.owner import display
    me, who = display(speaker or "owner"), task["who"]
    if task["kind"] == "call_prep":
        return (f"[Call prep: {me} is about to call {who}. Help him get ready in a few short points: what he wants to "
                f"say or ask, and anything you truly remember about {who} that matters for this call. Don't make up "
                f"facts about {who}. You can't hear the call itself.]")
    if task["kind"] == "after_call":
        return (f"[After the call: {me} just finished a call with {who} and is telling you how it went. Turn it into "
                f"short notes and a list of things to do (who and when, if he said). Ask only if something important "
                f"is unclear.]")
    return (f"[Text message: {me} wants to text {who}. Write ONLY the message itself, ready to send: short, natural, "
            f"in {me}'s own voice (first person), no quotes, no greeting line unless it fits, nothing else. "
            f"He will check it before it is sent.]")


def camera_note(seen: dict, speaker: str | None) -> str:
    from echolink.owner import display
    who = display(speaker) if speaker in ("owner", "loved_one") else "the owner"
    n = len(seen["images"])
    what = (f"{who} himself (the tablet's front camera, pointed at him)" if seen["camera"] == "front"
            else f"what {who} is looking at (the tablet's back camera)")
    pics = "this picture" if n == 1 else f"these {n} pictures, taken while he spoke, oldest first"
    return (f"[Camera: {who} is showing you {what}, live, in {pics}. Answer what he said using what you can actually "
            f"see. Say only what is really visible; if something is unclear, too dark or too far, say so and ask him "
            f"to move closer, hold it still or turn on the light. Don't guess names, labels or numbers you can't read.]")


def strip_hints(sent: str) -> str:
    """A past message without the one-time instructions that came with it ("END my reply by asking…", follow-up and
    tool notes). They were kept in the history, so the model obeyed them again on every later turn (2026-10-31: it kept
    asking "Tell me about a trip…" after the owner had answered). Memory notes and who-is-speaking stay."""
    return _ONE_TIME.sub("", sent or "").rstrip()


def scrub_identity(reply: str) -> str:
    """My old reply without "Is that you, the owner?"-type questions: the model copied them onto later replies even right
    after the owner said yes (2026-10-31). Only what the model sees changes; the stored conversation keeps everything."""
    parts = re.split(r"(?<=[.?!])\s+", reply.strip())
    who_q = _who_q()
    keep = [p for p in parts if not (p.rstrip().endswith("?") and who_q.search(p))]
    return " ".join(keep) if keep else reply


VOICE_YES = re.compile(r"^\W*(?:yes|yeah|yep|yup|it'?s me|it is|that'?s me|sure is|of course)\b", re.I)


# following the music in Chrome (2026-11-03): look every 5 s; first 15 s the window is still opening; at most 4 hours
FOLLOW_EVERY, FOLLOW_GRACE, FOLLOW_MAX = 5.0, 15.0, 4 * 3600
EARLY_CLOSE_S = 4.0   # a "closed" from my page this soon after opening is YouTube's opener policy, not the owner

def create_app(cfg: PersonaConfig | None = None, backend: LLMBackend | None = None,
               store: MemoryStore | None = None, stt: STTBackend | None = None,
               studio: Studio | None = None, faces: FaceLibrary | None = None,
               conflict_judge: conflict_check.Judge | None = None, speakers: SpeakerID | None = None,
               embedder: Embedder | None = None, photos: PhotoLibrary | None = None) -> FastAPI:
    cfg = cfg or load_config()
    speakers = speakers or SpeakerID(cfg.resolve(cfg.speaker_model), cfg.resolve(cfg.speakers_dir),
                                     cfg.speaker_threshold, cfg.speaker_margin)
    backend = backend or make_backend(cfg.llm)
    store = store or MemoryStore(cfg.resolve(cfg.memory_db))
    if conflict_judge is None and isinstance(backend, OllamaBackend):
        conflict_judge = conflict_check.ollama_judge(cfg.llm.host, cfg.llm.model)
    if embedder is None and isinstance(backend, OllamaBackend):
        embedder = Embedder(cfg.llm.host)   # search by meaning; without it, keyword search only
    answered_on_phone = set()
    try:   # questions already answered with "Tell me a memory" aren't asked again in conversation
        for f in (cfg.resolve(cfg.dataset_root) / "raw" / "phone").glob("*/*.json"):
            if f.name != "manifest.json" and '"memory"' in (t := f.read_text()):
                answered_on_phone.add(json.loads(t).get("task_id"))
    except Exception:
        pass
    ents = Entities(store)   # people, places, things and facts (seeded from config/entities.json)
    recall = Retriever(store, ents, embedder.model if embedder else None)   # memory/retrieve.py (Phase 3)
    pack = Pack(store, ents, recall)   # memory/context.py (Phase 5): what I always have in mind
    learner = Learner(store, cfg.resolve(cfg.dataset_root))   # memory/learn.py (Phase 7): documents and web pages
    # memory/gaps.py (Stage 5a/5b): what Echo noticed he doesn't know → proposed learning objectives, decided by the owner;
    # approved "ask the owner" ones go to the front of curiosity's questions
    learning = mem_gaps.Gaps(Path(store.path).parent / "learning.sqlite")
    def learning_ask(question: str) -> tuple[str, list]:
        """Stage 5c: what memory would give for this question now (same retrieval as a reply, k=3; no LLM)."""
        qv = embedder.query(question) if embedder else None
        res = recall.retrieve(question, qv, None, k=3)
        g = grounding.label(res, controller.classify(question, known_names()).kind)
        return g.label, [(h.memory.id, h.memory.created, grounding.hit_label(h).label) for h in res.hits]

    flowers = Flowers(store)   # 2026-11-05: flowers as gifts of appreciation (core/flowers.py)
    curiosity = Curiosity(store, cfg.ask_enabled, cfg.ask_every_minutes, cfg.ask_min_turns, answered=answered_on_phone,
                          extra=learning.approved_questions, photos=lambda: initiative.photo_questions(store),
                          dreams=dreamq.questions)   # unknowns from my dreams that I agreed to ask about

    def check_conflicts_later(m: Memory, dup: bool) -> None:
        """New memory → look for contradictions in the background (the language model may be busy replying)."""
        if dup or m.kind == "summary":
            return
        def run():   # CA 2.0: waits until nobody is talking (it used to run right while Echo replied)
            give_up = time.time() + 3600
            while time.time() < give_up:
                try:
                    if (n := SCHED.in_thread("conflicts", conflict_check.check, store, m, conflict_judge)):
                        log.info("memory %s: %d possible conflict(s) flagged for review", m.id, n)
                    return
                except Preempted:
                    time.sleep(5)
                except Exception as e:
                    log.warning("conflict check failed: %s", type(e).__name__)
                    return
        threading.Thread(target=run, daemon=True, name="memory-conflicts").start()
    lessons_path = Path(store.path).parent / "lessons.json"   # core/trainer.py: rules learned in "Train Echo"
    prompt = {"text": load_personality(cfg) + SYSTEM_EXTRA + training.lessons_block(lessons_path)}   # reloaded when a personality version is restored or a lesson changes
    voice_cfg = ROOT / "config" / "voice.json"   # {"stt": "parakeet" | "whisper", "reference": ...}
    stt_choice = (json.loads(voice_cfg.read_text()).get("stt") if voice_cfg.exists() else None) or "whisper"
    stt = stt or make_stt(stt_choice, cfg.voice.stt_model, cfg.voice.language)
    stt_ready = {"ok": False}
    tools_on = os.environ.get("ECHO_TOOLS", "1") != "0"   # Echo's own web tools (core/tools.py); 0 turns them off
    tools_cfg = ROOT / "config" / "tools.json"                # follow_links any|same_site|never, max_pages (core/permissions.py)
    tool_audit = permissions.Audit(Path(store.path).parent / "tool_audit.sqlite")   # beside the memory DB (tests: a temp dir)
    tools.AUDIT = tool_audit
    # core/workspace.py (Stage 1a, observe only): one record per turn, no text, kept 30 days
    wslog = cws.WorkspaceLog(Path(store.path).parent / "workspace.sqlite")
    recall_info: dict = {}
    # UCA2 Phase 2's NDAS shadow (core/attention.py) was retired in CA 2.0 Phase 7 (see core/executive.py)
    # CA 2.0 Phase 10: long-term evaluation, dormant until each measure has enough data (config/longterm.json)
    lterm = longterm.LongTerm(Path(store.path).parent / "longterm.sqlite", ROOT / "config" / "longterm.json")

    def run_longterm() -> dict:
        s = lterm.settings()
        r = lterm.run(wslog.records(s["window_days"]), development.report(trainer.lessons(), conversation_turns()),
                      curiosity.state(), store)
        log.info("long-term evaluation: %s", {k: v["state"] for k, v in r.items()})   # states only
        return r
    if cfg.owner_name or cfg.loved_one_name:   # names for every rule that needs one (core/people.py)
        people.configure(cfg.owner_name, cfg.loved_one_name)

    def people_names() -> list[str]:
        return people.names()
    keeper = executive.CacheKeeper()   # CA 2.0 Phase 7: records the model-cache state per turn and the re-reads

    # 2026-11-02: what I last offered to open/play ("would you like to…"), for a "yes". One Echo, one conversation, so it's
    # kept here, not per connection (the window reconnects; a relayed message is its own connection).
    pending_offer: dict = {}
    last_opened: dict = {}   # 2026-11-02: what I opened last in Chrome ({"host", "name", "at"}), for "close that"
    last_play: dict = {}     # 2026-11-03: the song I played last ({"artist", "vid", "at"}), for "a different song by them"
    last_photo: dict = {}    # 2026-11-03: the photo we were last talking about ({"sha", "at"}), for "show me the picture"
    # CA 2.0 Phase 2: what I was in the middle of survives a restart (core/state.py), each item only while fresh
    cstate = CognitiveState(Path(store.path).parent / "state.json")
    edim = edim_link.EdimLink(cstate)   # EDIM + awareness: off | shadow | on (core/edim_link.py)
    if (r := cstate.restored_offer(browser.Action)):
        pending_offer.update(act=r[0], at=r[1])
    if (o := cstate.get("opened")):
        last_opened.update(o)
    if cstate.restored_from:
        log.info("cognitive state restored from %s (offer %s, opened %s)", cstate.restored_from,
                 "yes" if pending_offer else "no", "yes" if last_opened else "no")

    def entity_names(ids) -> list[str]:
        ids = [i for i in ids or [] if isinstance(i, int)][:12]
        if not ids:
            return []
        try:
            q = f"SELECT id, name FROM entities WHERE id IN ({','.join('?' * len(ids))})"
            names = dict(store.db.execute(q, ids).fetchall())
            return [names[i] for i in ids if i in names]
        except Exception:  # noqa: BLE001
            return []
    known = {"names": frozenset(), "at": 0.0}   # entity names for core/controller.py, refreshed every 5 min

    def known_names() -> frozenset:
        if time.time() - known["at"] > 300:
            try:
                known["names"] = frozenset(e["name"].lower() for e in ents.list()   # people/pets/places only:
                                               if e.get("name") and e.get("type") in ("person", "pet", "place"))
            except Exception:  # noqa: BLE001
                pass
            known["at"] = time.time()
        return known["names"]
    from core.claude_helper import ClaudeHelper   # optional: the owner's Claude account for background jobs (off by default)
    helper = ClaudeHelper(ROOT / "config" / "claude.json", ROOT / "data" / "claude-usage.json")

    def reload_lessons() -> None:
        prompt["text"] = load_personality(cfg) + SYSTEM_EXTRA + training.lessons_block(lessons_path)
        log.info("lessons changed: instructions reloaded")
    trainer = training.Trainer(Path(store.path).parent / "training.sqlite", lessons_path, ROOT / "docs" / "TRAINING_REQUESTS.md",
                               helper, instructions=lambda: prompt["text"], on_lessons=reload_lessons)

    from core import daydream   # Daydream (2026-11-04): the owner's development Mac only; hidden everywhere else
    dd_cfg = daydream.load_config(daydream.CONFIG)
    dd_on = daydream.available(dd_cfg)
    dd_local = daydream.Local(cfg.llm.host, cfg.llm.model, {"temperature": cfg.llm.temperature, **cfg.llm.options},
                              lambda: prompt["text"], think=dd_cfg["local_think"], timeout_s=dd_cfg["task_timeout_s"])
    log.info("daydream %s", "available" if dd_on else "off")
    from core import daydream_score, daydream_train
    dd_examples = daydream_train.Examples() if dd_on else None
    dd_activity = {"at": 0.0}   # last message from anyone: score-card comparisons wait for quiet

    async def dd_quiet(seconds: float = 60) -> None:
        while time.time() - dd_activity["at"] < seconds:
            await asyncio.sleep(5)

    async def dd_compare(res) -> None:
        """After the owner has his answer: score Echo alone and Echo + tower on the same task (score card, step 3)."""
        try:
            card = await daydream_score.compare_live(res, dd_local, daydream.merge_prompt, dd_quiet)
            log.info("daydream score card %s: %s", res.id, card.get("summary"))
            if dd_examples is not None:   # step 5: a training example, kept or left out with the reason
                ex = await asyncio.to_thread(dd_examples.consider, res, card, dd_cfg["training"]["min_score"])
                log.info("daydream example %s: include=%s (%s)", res.id, ex["include"], ex["reason"])
        except Exception as e:  # noqa: BLE001  (a score card must never disturb Echo)
            log.warning("daydream score card failed: %s", type(e).__name__)

    async def warm_stt():  # load speech recognition in the background so the first voice turn is fast
        try:
            if getattr(stt, "name", "") == "parakeet":   # the service loads itself; wait for it (Whisper only if needed)
                for _ in range(120):
                    if await asyncio.to_thread(stt.healthy):
                        break
                    await asyncio.sleep(2)
            await stt.transcribe(np.random.default_rng(0).normal(0, 0.05, 16000).astype(np.float32))
            stt_ready["ok"] = True
        except Exception as e:
            log.warning("stt warmup failed: %s", type(e).__name__)

    def last_turn_row():
        r = store.db.execute("SELECT id, ts FROM turns ORDER BY id DESC LIMIT 1").fetchone()
        return (r["id"], r["ts"]) if r else (None, None)

    async def reread(why: str) -> None:
        """CA 2.0 Phase 7 (core/executive.py): read the instructions + conversation into the model's cache now, so the
        next reply only reads what's new. A scheduler job like all background LLM work: someone talking stops it."""
        if not hasattr(backend, "warm"):
            return
        tid, _ = last_turn_row()
        t = time.perf_counter()
        await SCHED.run("warm", backend.warm(build_messages("Hello.", warm_only=True)))
        ms = (time.perf_counter() - t) * 1000
        was = SCHED.cache_restored()
        keeper.done(why, ms, tid)
        log.info("conversation re-read (%s%s): %d ms", why, f"; model last used by {was['by']}" if was else "", round(ms))

    async def warm_llm():  # read Echo's instructions + recent conversation now, so the first reply is quick
        await asyncio.sleep(2)
        try:
            # (2026-11-02: a reply waited ~9 s behind this warm-up right after a restart: now it stops for a reply)
            await reread("startup")
        except Preempted:
            log.info("llm warmup stopped: someone started talking")
        except Exception as e:
            log.warning("llm warmup failed: %s", type(e).__name__)

    last_turn = {"at": time.time()}
    photo_cons: dict = {}   # index file time at the last photo consolidation
    originals = {"at": 0.0}   # last time finished days were archived and new originals catalogued

    def consolidate_due(now: float) -> bool:
        """Nightly (2–5 am) once quiet for 10 minutes; or any time if it hasn't run for 30 hours."""
        last = json.loads(store.get_meta("consolidation_last", "{}")).get("at", 0)
        if now - last_turn["at"] < 600:
            return False
        return now - last > 30 * 3600 or (now - last > 20 * 3600 and 2 <= time.localtime(now).tm_hour < 5)

    def run_consolidation() -> dict:
        r = consolidate.run(store, ents, embedder.model if embedder else None, conflict_judge, vindex=recall.index)
        if not r["ok"]:
            log.warning("memory upkeep stopped: %s", "; ".join(r["problems"]))
            return r
        log.info("memory upkeep: %d links, %d repeats, %d conflicts flagged, %d patterns, %.1f s", r["links"],
                 r["repeats"], r["conflicts_flagged"], len(r["patterns"]), r["seconds"])
        if Path(store.path).resolve() != cfg.resolve(cfg.memory_db).resolve():
            return r   # snapshots only ever of Echo's own memory database
        folder = cfg.resolve(cfg.backup_dir)   # nightly snapshot (14 daily, 12 monthly, yearly); weekly restore drill
        try:
            snap = mem_backup.snapshot(store, folder)
            log.info("memory snapshot: %s", ", ".join(snap["made"]) or "already made today")
            last = json.loads((folder / "drill.json").read_text())["at"] if (folder / "drill.json").exists() else 0
            if time.time() - last > 7 * 86400:
                d = mem_backup.drill(store, folder, cfg.resolve(cfg.dataset_root))
                log.info("restore drill: %s in %.1f s", "ok" if d["ok"] else "FAILED", d["total_s"])
        except Exception as e:
            log.warning("memory snapshot/drill failed: %s: %s", type(e).__name__, e)
        return r

    def keep_originals():
        """Level 1 upkeep (memory/archive.py): link old notes once, archive finished days, catalogue new originals."""
        root = cfg.resolve(cfg.dataset_root)
        if (n := archive.link_old_notes(store)):
            log.info("old conversation notes linked to their turns: %d", n)
        if (days := archive.archive_days(store, root)):
            log.info("conversation days archived: %d", len(days))
        c = archive.catalogue(store, root, max_bytes=2 << 30)   # at most 2 GB read per run; the rest next time
        if c.get("added") or c.get("changed"):
            log.info("originals catalogued: %s", c)

    def speech_seconds(text: str) -> float:
        """Roughly how long the owner's voice takes to say this (measured 2026-11-02: ~17 characters per second)."""
        return len(text or "") / 17 if cfg.voice and getattr(cfg.voice, "tts_url", None) else 0.0

    async def summarize_when_quiet():
        """Every 15 s: if nobody has spoken for 20 s, write notes for conversation that slid out of view, then re-read
        the (changed) instructions so the next reply is still quick."""
        while True:
            await asyncio.sleep(15)
            if embedder:   # vectors for new/edited memories (one quick query when there's nothing to do)
                try:
                    if (n := await asyncio.to_thread(embedder.index, store)):
                        log.info("memory vectors made: %d", n)
                except Exception as e:
                    log.warning("memory vectors failed: %s", type(e).__name__)
            if hasattr(stt, "release_idle_fallback"):   # Whisper fallback (2.3 GB) only while it's being used
                try:
                    await asyncio.to_thread(stt.release_idle_fallback)
                except Exception as e:  # noqa: BLE001
                    log.warning("whisper unload failed: %s", type(e).__name__)
            try:   # the music-splitting model (part 2 of listening with the owner) only while it's being used
                if await asyncio.to_thread(hear.release_idle):
                    log.info("hearing: music-splitting model let go after 10 idle minutes")
            except Exception as e:  # noqa: BLE001
                log.warning("hearing release failed: %s", type(e).__name__)
            if time.time() - last_turn["at"] < 20 or not SCHED.quiet() or not hasattr(backend, "warm"):
                continue
            try:
                await quiet_jobs()
                # CA 2.0 Phase 7: after a 30-minute break the window restarts → read the conversation now, once, so the
                # next message doesn't wait for it (measured: memory questions 6–7 s → 3.2–3.8 s; core/executive.py)
                tid, ts = last_turn_row()
                if os.environ.get("ECHO_CACHE_KEEPER", "1") != "0" and (why := keeper.due(tid, ts)):
                    await reread(why)
            except Preempted:   # someone started talking (or work is paused): the rest waits for the next quiet pass
                continue

    async def quiet_jobs():
        """Background work while nobody is talking (CA 2.0 Phase 2, core/sched.py): every language-model step runs as a
        scheduler job, so it is cancelled the moment someone speaks and simply retried on a later pass."""
        if True:
            try:   # Stage 5c: did learning help? re-ask decided objectives against memory (no LLM; every 10 min each)
                for chk in await asyncio.to_thread(mem_gaps.check_learning, learning, learning_ask):
                    if chk["outcome"] != "not_yet":
                        log.info("learning check: objective %d %s", chk["id"], chk["outcome"])
            except Exception as e:  # noqa: BLE001
                log.warning("learning check failed: %s", type(e).__name__)
            answers = curiosity.pop_answers()   # the owner's answers to my questions → suggestions on My memories
            for i, a in enumerate(answers):
                try:
                    await asyncio.to_thread(SCHED.in_thread, "suggestions", suggest_memories,
                                            {"transcript": a["text"], "question": a["question"],
                                             "source": a["source"], "period": a["period"]})
                except Preempted:
                    curiosity.requeue(answers[i:])   # not lost: done on the next quiet pass
                    raise
            try:
                if (n := archive.build_episodes(store)):
                    log.info("episodes made: %d", n)
                if time.time() - originals["at"] > 3600:
                    originals["at"] = time.time()
                    await asyncio.to_thread(keep_originals)
            except Exception as e:
                log.warning("originals upkeep failed: %s: %s", type(e).__name__, e)
            try:
                n = await asyncio.to_thread(SCHED.in_thread, "summaries", summarizer.summarize_pending, store,
                                            len(store.turns_window()), cfg.llm.host, cfg.llm.model, 4, helper)
                if n:
                    log.info("conversation notes saved: %d", n)   # the notes themselves are not logged
                    await reread("changed")
            except Exception as e:
                log.warning("conversation notes failed: %s", type(e).__name__)
            if time.time() - last_turn["at"] < 20:
                return
            try:
                if consolidate_due(time.time()):
                    await asyncio.to_thread(SCHED.in_thread, "consolidation", run_consolidation)
            except Exception as e:
                log.warning("memory upkeep failed: %s: %s", type(e).__name__, e)
            try:   # memories changed (new facts, "remember that", upkeep): refresh the pack now, while it's quiet, and
                # read the new prompt once, so the next reply doesn't have to (~20 s on qwen3.5 after a change)
                if pack.stale() and time.time() - last_turn["at"] >= 20:
                    pack.get(refresh=True)
                    await reread("changed")
                    log.info("context pack refreshed and read")
            except Exception as e:
                log.warning("context pack refresh failed: %s", type(e).__name__)
            try:   # documents and web pages someone asked me to learn (one at a time)
                if (n := await asyncio.to_thread(learner.process_pending, 1)):
                    log.info("learning jobs finished: %d", n)
            except Exception as e:
                log.warning("learning failed: %s", type(e).__name__)
            try:   # people, places and facts from memories (~8 s each on the local model), ONE per pass and only if
                # it's still quiet: two in a row kept the GPU busy ~16 s, long enough to collide with the next reply's voice
                if time.time() - last_turn["at"] >= 20 and \
                        (n := await asyncio.to_thread(SCHED.in_thread, "facts", ents.extract_pending, cfg.llm.host,
                                                      cfg.llm.model, 1)):
                    log.info("memories read for people and facts: %d", n)
            except Exception as e:
                log.warning("fact extraction failed: %s", type(e).__name__)
            try:   # CA 2.0 Phase 10: long-term evaluation, at most once a day (no model call; dormant measures only count)
                if lterm.due():
                    await asyncio.to_thread(SCHED.in_thread, "longterm", run_longterm)
            except Preempted:
                raise
            except Exception as e:  # noqa: BLE001
                log.warning("long-term evaluation failed: %s", type(e).__name__)
            try:   # CA 2.0 Phase 4: photo memories get camera dates, same-day links and the collection overview
                ip = photos.index_path
                if ip.exists() and ip.stat().st_mtime != photo_cons.get("mtime"):
                    r = await asyncio.to_thread(SCHED.in_thread, "photo-memory", photos.consolidate)
                    photo_cons["mtime"] = ip.stat().st_mtime
                    if r.get("updated") or r.get("overview") in ("made", "updated"):
                        log.info("photo memories consolidated: %s", r)
            except Exception as e:  # noqa: BLE001
                log.warning("photo consolidation failed: %s", type(e).__name__)

    @asynccontextmanager
    async def lifespan(_app):
        SCHED.attach(asyncio.get_running_loop())   # background LLM calls run here, so they can be cancelled
        tasks = [asyncio.create_task(warm_stt()), asyncio.create_task(warm_llm()), asyncio.create_task(summarize_when_quiet())]
        yield
        for t in tasks:
            t.cancel()

    studio = studio or Studio(cfg.resolve(cfg.dataset_root))
    faces = faces or FaceLibrary(cfg.resolve(cfg.dataset_root))
    app = FastAPI(title="Echo Persona Core", lifespan=lifespan)
    # DNS-rebinding guard (Stage 8, the owner OK'd 2026-11-01): a web page whose own domain is made to point at 127.0.0.1
    # would otherwise talk to me as if it were this Mac. Only these names are answered (HTTP and WebSocket); everything
    # here is reached as 127.0.0.1:8765 (scripts, Chrome window). Tailscale serves only the phone gateway (:8770).
    from starlette.middleware.trustedhost import TrustedHostMiddleware
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "testserver"])

    @app.get("/api/workspace/current")
    def workspace_current():
        """CA 2.0 Phase 3: the shared workspace right now — focus (state), carry-over from before a break, open items."""
        carry, ids = focus.carryover(store)
        cur = curiosity.pending()
        return {"state": cstate.snapshot(), "carryover": {"turn_ids": ids, "lines": carry.count("\n- ") + (1 if carry else 0)},
                "open": {"my_question": bool(cur), "offer": bool(pending_offer),
                         "learning_objectives": len(learning.objectives("open"))},
                "scheduler": SCHED.snapshot()["background"]}

    @app.get("/api/state")
    def cognitive_state():
        """CA 2.0: what Echo is in the middle of (survives restarts). Ids and labels only."""
        return cstate.snapshot()

    @app.get("/api/dialogue")
    def dialogue_state():
        """EDIM + awareness (Phase 3): modes, topics/threads, focus, update times, errors. Localhost only."""
        return edim.report()

    @app.get("/api/executive")
    def executive_state(days: int = 7):
        """CA 2.0 Phase 7: what the executive decided and why (counts per module and reason), the model-cache re-reads,
        and the wait for the first word by cache state at the start of the turn. Ids and rule names only."""
        return {**executive.report(wslog.records(days)), "cache_keeper": keeper.snapshot(),
                "cache_disturbed_by": (SCHED.cache_disturbed or {}).get("by"),
                "retired": {"ndas": "CA 2.0 Phase 7 (D3): never changed a reply; 0 memories added over retrieval"}}

    @app.get("/api/scheduler")
    def scheduler_state():
        """CA 2.0: what background work is doing and why it waits (conversation first)."""
        return SCHED.snapshot()

    @app.post("/api/scheduler")
    def scheduler_set(body: dict):
        """{"pause": true|false|null}: pause all background work now (null = follow config/scheduler.json)."""
        p = body.get("pause")
        if p not in (True, False, None):
            raise HTTPException(400, "pause must be true, false or null")
        SCHED.manual_pause = p
        log.info("background work %s by the owner", {True: "paused", False: "resumed", None: "back to settings"}[p])
        return SCHED.snapshot()

    @app.get("/api/learning/objectives")
    def learning_objectives(status: str = "open"):
        """Stage 5a: gaps Echo noticed, as proposed learning objectives (nothing happens until the owner decides)."""
        return {"objectives": learning.objectives(status), "stats": mem_gaps.learning_stats(learning)}

    @app.post("/api/learning/objectives/{gid}")
    async def learning_decide(gid: int, body: dict):
        decision, note = str(body.get("decision", "")), str(body.get("note", ""))
        if decision not in mem_gaps.DECISIONS:
            raise HTTPException(400, f"decision must be one of {mem_gaps.DECISIONS}")
        job = None
        if decision == "research":   # only from a source the owner gives, through the safe reader (memory/learn.py)
            url = str(body.get("url", "")).strip()
            if not url:
                raise HTTPException(400, "give a web address to learn this from")
            try:
                job = await asyncio.to_thread(learner.add_web, url, "")
            except (ValueError, RuntimeError, httpx.HTTPError) as e:
                raise HTTPException(400, str(e) or type(e).__name__)
            note = f"learn job {job}: {url}"
        if not learning.decide(gid, decision, "owner:panel", note):
            raise HTTPException(404, "no such objective")
        return {"ok": True, "job": job, "history": learning.history(gid)}

    self_cache: dict = {"model": None, "at": 0.0}

    async def current_self(max_age: float = 300) -> dict:
        """The self-model, rebuilt at most every 5 minutes (Stage 7b uses it for questions about Echo himself)."""
        if self_cache["model"] is None or time.time() - self_cache["at"] > max_age:
            self_cache.update(model=await gather_self(), at=time.time())
        return self_cache["model"]

    @app.get("/api/self")
    async def self_model():
        """Stage 7a: Echo's self-model, built from the live system."""
        return await current_self(max_age=0)

    async def gather_self() -> dict:
        f: dict = {"name": "Echo", "owner": cfg.owner_name or "the owner", "loved_one": cfg.loved_one_name or "my loved one"}
        try:
            f["personality_version"] = versions.current_id()
            prof = persona.load_profile(pdir) or {}
            f["traits"] = len(prof.get("traits") or [])
        except Exception:  # noqa: BLE001
            pass
        try:
            f["voice"] = (await tts_client.get("/health", timeout=1.5)).status_code == 200
        except Exception:  # noqa: BLE001
            f["voice"] = False
        f["stt"] = stt_ready["ok"]
        f["speakers"], f["speaker_names"] = bool(speakers.profiles), sorted(x.title() for x in speakers.profiles)
        f["tools"] = bool(tools_on and permissions.load_settings(tools_cfg)["enabled"])
        try:
            f["browser_places"] = len(json.loads((ROOT / "config" / "browser.json").read_text()).get("shortcuts", []))
            f["browser"] = True
        except Exception:  # noqa: BLE001
            f["browser"] = False
        try:
            mj = json.loads(music_list.read_text())
            f["music"], f["music_genres"] = True, len(mj.get("approved_genres", []))
        except Exception:  # noqa: BLE001
            f["music"] = False
        f["curiosity"], f["learning"], f["remember"] = bool(curiosity.enabled), True, True
        f["helper"] = bool(helper.status().get("enabled"))
        db = store.db
        q = lambda sql: db.execute(sql).fetchone()[0]   # noqa: E731
        f["memory"] = {"confirmed": q("SELECT count(*) FROM memories WHERE status='confirmed' AND hidden IS NULL AND kind!='knowledge'"),
                       "inferred": q("SELECT count(*) FROM memories WHERE status='inferred' AND hidden IS NULL"),
                       "life": q("SELECT count(*) FROM memories WHERE kind='memory' AND hidden IS NULL"),
                       "learned": q("SELECT count(*) FROM memories WHERE kind='knowledge' AND hidden IS NULL")}
        f["entities"] = q("SELECT count(*) FROM entities")
        f["documents"] = q("SELECT count(*) FROM learn_jobs WHERE status='done'")
        first, last, n = db.execute("SELECT min(ts), max(ts), count(*) FROM turns").fetchone()
        day = lambda t: time.strftime("%Y-%m-%d", time.localtime(t)) if t else None   # noqa: E731
        f["experience"] = {"turns": n, "episodes": q("SELECT count(*) FROM episodes"), "first": day(first), "latest": day(last)}
        f["learning"] = mem_gaps.learning_stats(learning)
        try:   # photos the owner approved (data drive); left out if the drive isn't there
            ps = photos.summary()
            labels = {c["id"]: c["label"] for c in ps["categories"]}
            f["photos"] = {labels[c]: v["approved"] for c, v in ps["counts"].items() if v["approved"]}
        except Exception:  # noqa: BLE001
            pass
        return selfmodel.build(f)

    @app.get("/api/skills")
    def skills_report(days: float = 30.0):
        """Stage 6a: repeated procedures and skill candidates from the turn records (report only)."""
        return skills.report(wslog.records(days))

    @app.get("/api/workspace")
    def workspace_report(days: float = 1.0, limit: int = 20):
        """Stage 1a: how recent turns were handled (routes, memory ids, timings; never message text)."""
        return {"summary": wslog.summary(days), "recent": wslog.recent(limit)}

    @app.get("/api/tools")
    async def tools_report(limit: int = 50):
        """What tools Echo has, the settings, and the latest uses (no page text)."""
        return {"settings": permissions.load_settings(tools_cfg),
                "tools": {k: {"does": v[0], "risk": v[1], "asks_first": v[2]} for k, v in permissions.CATALOGUE.items()},
                "recent": tool_audit.recent(limit)}
    app.state.store = store


    # Echo listens for the owner's voice now and then (the owner, 2026-10-31: "he swaps the owner with the loved one"): his profile came
    # from studio takes, and the room mic scored him only 0.30-0.44. Room-mic prints teach it the room:
    #  - a phrase that is confidently the owner adds itself, at most once every AUTO_EVERY_S;
    #  - an unclear phrase that sounds most like the owner makes Echo ask "Is that you?" (at most every ASK_EVERY_S);
    #    "yes" adds it. Only the owner's profile ever grows this way (the loved one's only with her own consent, on /voices.html).
    voice = {"candidate": None, "check": None, "asked_at": 0.0, "auto_at": 0.0, "last": None,
             "who": None, "confirmed_until": 0.0}
    # Who I'm talking with stays known for the conversation (the owner, 2026-10-31: "don't ask constantly if I already
    # confirmed once"): once someone is recognised, says "This is …" or answers "yes", an unclear phrase is still them
    # until STICKY_S of quiet or a voice that clearly is someone else; after a "yes", no "Is that you?" for CONFIRMED_S.
    STICKY_S, CONFIRMED_S = 3600, 12 * 3600
    AUTO_EVERY_S, ASK_EVERY_S, AUTO_SCORE, AUTO_GAP = 6 * 3600, 30 * 60, 0.82, 0.20

    async def identify_speaker(audio) -> dict:
        if not speakers.profiles or not speakers.available:
            return {}
        try:
            r = await asyncio.to_thread(speakers.identify, audio)
            log.info("speaker %s %s", r["speaker"], r["scores"])   # scores only (no words), to tune the thresholds
            emb, now = r.pop("emb", None), time.time()
            if emb is not None:
                voice["last"] = {"emb": emb, "at": now, "scores": r.get("scores", {})}   # for "This is the owner/the loved one"
            if emb is not None and r.get("best") == "owner":
                if r["speaker"] == "owner" and r["score"] >= AUTO_SCORE and r["gap"] >= AUTO_GAP \
                        and now - voice["auto_at"] >= AUTO_EVERY_S:
                    n = await asyncio.to_thread(speakers.add_live, "owner", emb, "auto")
                    voice["auto_at"] = now
                    log.info("voice: room print added for owner (auto), %d live", n)
                elif r["speaker"] == "unknown":
                    voice["candidate"] = {"emb": emb, "at": now}
            return {"speaker": r["speaker"], "speaker_score": r["score"]}
        except Exception as e:   # never let this break listening
            log.warning("speaker check failed: %s", type(e).__name__)
            return {}

    @app.post("/api/stt")
    async def speech_to_text(request: Request, mode: str = "direct", debug: bool = False):
        body = await request.body()
        if len(body) > MAX_AUDIO_BYTES:
            raise HTTPException(413, "audio too large")
        try:
            audio = await asyncio.to_thread(decode_audio, body)
            # who's talking, worked out alongside the transcript (CPU vs GPU); only returned with text that's kept
            text, who = await asyncio.gather(stt.transcribe(audio), identify_speaker(audio))
        except STTError as e:
            raise HTTPException(400, str(e))
        log.info("stt ok mode=%s seconds=%.1f", mode, len(audio) / 16000)  # transcript deliberately not logged
        if os.environ.get("ECHO_LOG_TRANSCRIPTS") == "1":  # opt-in, for supervised testing only
            log.info("TEST transcript mode=%s: %r", mode, text)
        if mode == "wake":
            woke, command = detect_wake(text)
            # Without the wake phrase the transcript is discarded and never returned.
            out = {"wake": woke, "command": command, **who} if woke else {"wake": False}
            if debug:  # the owner explicitly turned on "Show what I hear" in the window
                out["heard"] = text
            return out
        if mode == "conversation":  # follow-up turn after "Hey Echo": no wake word needed
            if is_noise_transcript(text):
                return {"text": ""}
            return {"text": text, "end": detect_end(text), **who}
        return {"text": text, **who}

    @app.get("/health")
    async def health():
        return {"core": "ok", "llm": await backend.health(), "memories": len(store.all()),
                "stt": {"backend": stt.name, "ready": stt_ready["ok"],
                        "fallbacks_to_whisper": getattr(stt, "fallbacks", 0)}}

    # ---- memory API (used by the "What I remember" panel) ----
    @app.get("/api/memory")
    def list_memory():   # learned passages are listed per document on My memories → Learn, not here
        return [m.model_dump() for m in store.all() if m.kind != "knowledge"]

    # ---- learning from documents and web pages (memory/learn.py) ----
    @app.get("/api/learn")
    def learn_jobs():
        return learner.jobs()

    @app.post("/api/learn/file")
    async def learn_file(request: Request, name: str, title: str = ""):
        body = await request.body()
        try:
            jid = await asyncio.to_thread(learner.add_file, name, body, title)
        except (ValueError, RuntimeError) as e:
            raise HTTPException(400, str(e))
        return {"job": jid}

    @app.post("/api/learn/web")
    async def learn_web(body: dict):
        try:
            jid = await asyncio.to_thread(learner.add_web, str(body.get("url", "")).strip(), str(body.get("title", "")))
        except (ValueError, RuntimeError, httpx.HTTPError) as e:
            raise HTTPException(400, str(e) or type(e).__name__)
        return {"job": jid}

    @app.post("/api/learn/{jid}/forget")
    def learn_forget(jid: int):
        return {"hidden": learner.forget(jid)}

    @app.post("/api/learn/{jid}/restore")
    def learn_restore(jid: int):
        return {"restored": learner.restore(jid)}

    @app.post("/api/memory")
    def add_memory(m: Memory):
        saved, dup = store.add(m.model_copy(update={"source": "owner:panel"}))
        check_conflicts_later(saved, dup)
        return saved.model_dump() | {"duplicate": dup}

    @app.patch("/api/memory/{mid}")
    def patch_memory(mid: int, p: MemoryPatch):
        m = store.correct(mid, p.content) if p.content else store.get(mid)
        extra = {f: getattr(p, f) for f in LIFE_FIELDS if getattr(p, f) is not None}
        if m and (p.kind or p.status or extra):
            m = store.update(mid, kind=p.kind, status=p.status, **extra)
        if not m:
            raise HTTPException(404)
        return m.model_dump()

    @app.delete("/api/memory/{mid}")
    def forget_memory(mid: int):
        """Forget = hide: Echo stops using it; it's kept and can be restored (the owner, 2026-10-31)."""
        if not store.forget(mid):
            raise HTTPException(404)
        return {"forgotten": mid, "hidden": True}

    @app.get("/api/memory/hidden")
    def list_hidden():
        return [m.model_dump() for m in store.hidden()]

    @app.post("/api/memory/{mid}/restore")
    def restore_memory(mid: int):
        m = store.restore_hidden(mid)
        if not m:
            raise HTTPException(404)
        return m.model_dump()   # its meaning vector was kept, so search by meaning finds it again at once

    @app.get("/api/memory/{mid}/history")
    def memory_history(mid: int):
        return store.history(mid)

    # ---- Level 2: people, places, things and facts (memory/entities.py) ----
    @app.get("/api/entities")
    def list_entities():
        return ents.list()

    @app.get("/api/about")
    def about(name: str):
        """Everything about one person/place/thing: /api/about?name=the loved one"""
        if not (a := ents.about(name)):
            raise HTTPException(404, "I don't know anyone or anything by that name yet")
        return a

    @app.get("/api/entities/{eid}")
    def get_entity(eid: int):
        if not (a := ents.about(eid)):
            raise HTTPException(404)
        return a

    # ---- consolidation (memory/consolidate.py) ----
    @app.post("/api/memory/consolidate")
    async def consolidate_now():
        return await asyncio.to_thread(run_consolidation)

    @app.get("/api/memory/consolidation")
    def consolidation_report():
        return {"last": json.loads(store.get_meta("consolidation_last", "{}")),
                "problem": json.loads(store.get_meta("consolidation_problem", "") or "null")}

    @app.get("/api/memory/repeats")
    def memory_repeats():
        return consolidate.repeats(store)

    @app.get("/api/memory/{mid}/related")
    def memory_related(mid: int):
        return consolidate.related(store, mid)

    # ---- Level 1: originals and episodes (memory/archive.py) ----
    @app.get("/api/archive/status")
    def archive_status():
        return archive.status(store)

    @app.get("/api/episodes")
    def list_episodes(since: float = 0.0, until: float | None = None, limit: int = 100):
        return archive.episodes(store, since, until, min(limit, 1000))

    @app.get("/api/episodes/{eid}")
    def get_episode(eid: int):
        if not (e := archive.episode(store, eid)):
            raise HTTPException(404)
        return e

    @app.get("/api/turns/{tid}/episode")
    def turn_episode(tid: int):
        if not (e := archive.episode_for_turn(store, tid)):
            raise HTTPException(404, "not in an episode yet (the conversation may still be going)")
        return e

    @app.get("/api/memory/conflicts")
    def list_conflicts():
        return store.open_conflicts()

    @app.post("/api/memory/conflicts/{cid}")
    def resolve_conflict(cid: int, body: ConflictIn):
        if not store.resolve_conflict(cid, body.action):
            raise HTTPException(404)
        return {"resolved": cid, "action": body.action}

    # ---- suggested life memories (renderer/memories.html) ----
    @app.get("/api/memories/candidates")
    def list_candidates():
        out = []
        for c in store.candidates("pending"):
            similar = None
            if embedder and (v := embedder.query(c["content"])):   # "I may already know this"
                ids, sims, _, _ = recall.index.search(v, 1)   # exact up to 50k memories, FAISS beyond
                similar = m.content if len(ids) and sims[0] >= 0.8 and (m := store.get(int(ids[0]))) else None
            out.append(c | {"similar": similar})
        return out

    @app.post("/api/memories/candidates/{cid}")
    def decide_candidate(cid: int, body: CandidateIn):
        if body.action == "reject":
            if not store.reject_candidate(cid):
                raise HTTPException(404)
            return {"rejected": cid}
        got = store.approve_candidate(cid, body.model_dump(exclude={"action"}, exclude_none=True))
        if not got:
            raise HTTPException(404)
        m, dup = got
        check_conflicts_later(m, dup)
        return m.model_dump() | {"duplicate": dup}

    @app.get("/api/memories/candidates/{cid}/audio")
    def candidate_audio(cid: int):
        """Play the recording a suggestion came from (only files inside the dataset's raw folder)."""
        c = store.candidate(cid)
        root = cfg.resolve(cfg.dataset_root).resolve()
        f = (root / c["source"]).resolve() if c else None
        if not f or not f.is_relative_to(root / "raw") or not f.is_file():
            raise HTTPException(404)
        return FileResponse(f)

    @app.get("/api/memories/life")
    def life_story():
        """Approved life memories, roughly in order: by period, then year."""
        order = {p: i for i, p in enumerate(mem_candidates.PERIODS)}
        mems = [m for m in store.all() if m.kind == "memory"]
        return [m.model_dump() for m in sorted(mems, key=lambda m: (order.get(m.period or "unknown", 9), m.year or 9999, m.id))]

    # ---- who's talking: voice profiles (renderer/voices.html) ----
    @app.get("/api/speakers")
    def list_speakers():
        return {"available": speakers.available, "threshold": speakers.threshold,
                "people": [speakers.info(n) for n in speakers.profiles]}

    @app.post("/api/speakers/{name}/sample")
    async def add_speaker_sample(name: str, request: Request, consent: bool = False):
        """One spoken sample for `name`'s voice profile. The person must have agreed (consent=true from the page).
        The original is kept read-only on the dataset drive (raw/speakers/<name>/) so a better model can re-learn later."""
        if not SPEAKER_NAME_RE.match(name):
            raise HTTPException(400, "bad name")
        if not consent:
            raise HTTPException(403, "the person must agree first")
        if not speakers.available:
            raise HTTPException(503, "speaker model missing")
        body = await request.body()
        if len(body) > MAX_AUDIO_BYTES:
            raise HTTPException(413, "audio too large")
        try:
            audio = await asyncio.to_thread(decode_audio, body)
        except STTError as e:
            raise HTTPException(400, str(e))
        if len(audio) < 3 * 16000:
            raise HTTPException(400, "too short: please read the whole sentence (at least 3 seconds)")
        src = "not kept (dataset drive not connected)"
        root = cfg.resolve(cfg.dataset_root)
        if root.exists():
            import hashlib
            d = root / "raw" / "speakers" / name
            d.mkdir(parents=True, exist_ok=True)
            f = d / time.strftime("%Y%m%d-%H%M%S.wav")
            with open(f, "xb") as fh:   # never overwrite an original
                fh.write(body)
            (d / (f.stem + ".meta.json")).write_text(json.dumps(
                {"file": f.name, "sha256": hashlib.sha256(body).hexdigest(), "seconds": round(len(audio) / 16000, 1),
                 "consent": True, "at": time.time()}))
            f.chmod(0o444)
            src = f"raw/speakers/{name}/{f.name}"
        info = await asyncio.to_thread(speakers.enroll, name, [audio], [src])
        log.info("voice profile %s: %d samples", name, info["samples"])
        return info

    @app.post("/api/speakers/test")
    async def test_speaker(request: Request):
        """Say something → who does it sound like? Nothing is kept."""
        try:
            audio = await asyncio.to_thread(decode_audio, await request.body())
        except STTError as e:
            raise HTTPException(400, str(e))
        r = await asyncio.to_thread(speakers.identify, audio)
        r.pop("emb", None)   # the voice print itself stays on the server
        return r

    @app.delete("/api/speakers/{name}")
    def forget_speaker(name: str):
        """Forget a voice: the profile and the kept samples are deleted."""
        if not SPEAKER_NAME_RE.match(name):
            raise HTTPException(400, "bad name")
        found = speakers.forget(name)
        d = cfg.resolve(cfg.dataset_root) / "raw" / "speakers" / name
        if d.is_dir():
            import shutil
            for f in d.iterdir():
                f.chmod(0o644)
            shutil.rmtree(d)
            found = True
        if not found:
            raise HTTPException(404)
        return {"forgotten": name}

    @app.get("/api/memory/export", response_class=PlainTextResponse)
    def export_memory():
        return store.export_json()

    @app.post("/api/memory/backup")
    def backup_memory():
        dest = cfg.resolve(cfg.backup_dir) / time.strftime("memory-%Y%m%d-%H%M%S.sqlite")
        return {"backup": str(store.backup(dest))}

    # ---- voice recording studio ----
    def studio_call(fn, *a):
        try:
            return fn(*a)
        except StudioError as e:
            raise HTTPException(400, str(e))

    @app.get("/api/studio/status")
    def studio_status():
        ok = studio.available()
        return {"dataset_ok": ok, "dataset_root": str(studio.root), "consent": studio.consent() if ok else None,
                "consent_text": CONSENT_TEXT, "stats": studio.stats() if ok else None}

    @app.get("/api/studio/prompts")
    def studio_prompts():
        return list(studio.prompts.values())

    @app.post("/api/studio/consent")
    def studio_consent(c: ConsentIn):
        return studio_call(studio.give_consent, c.name)

    @app.post("/api/studio/session")
    def studio_session():
        return {"session": studio_call(studio.new_session)}

    @app.post("/api/studio/take")
    async def studio_take(request: Request, session: str, prompt_id: str, custom_text: str = ""):
        body = await request.body()
        if len(body) > MAX_TAKE_BYTES:
            raise HTTPException(413, "recording too large")
        qc = studio_call(studio.save_take, session, body, prompt_id, custom_text)
        try:  # check what was actually said (local Whisper)
            heard = await stt.transcribe(await asyncio.to_thread(decode_audio, body))
            qc = studio.add_transcript(session, qc["take"], heard)
        except (STTError, StudioError) as e:
            log.warning("studio transcript failed: %s", type(e).__name__)
        log.info("studio take saved %s/%s verdict=%s", session, qc["take"], qc.get("verdict"))
        return qc

    @app.patch("/api/studio/take/{session}/{take}")
    def studio_take_status(session: str, take: str, s: StatusIn):
        return studio_call(studio.set_status, session, take, s.status)

    # ---- The owner's voice (separate local voice service, see tts/service.py) ----
    tts_client = httpx.AsyncClient(base_url=cfg.voice.tts_url, timeout=60)

    @app.get("/api/tts/health")
    async def tts_health():
        try:
            return (await tts_client.get("/health", timeout=2)).json()
        except httpx.HTTPError:
            return {"ok": False, "error": "voice service not running"}

    @app.post("/api/tts")
    async def tts(body: dict):
        text = speakable(str(body.get("text", "")))[:600]   # emoji are for the window only
        if not text:
            raise HTTPException(400, "empty text")
        try:
            r = await tts_client.post("/speak", json={"text": text})
        except httpx.HTTPError:
            raise HTTPException(503, "voice service not running")
        if r.status_code != 200:
            raise HTTPException(503, "voice service unavailable")
        return Response(r.content, media_type="audio/wav")

    @app.post("/api/tts/stream")
    async def tts_stream(body: dict):
        """Pass the owner's voice through as it is generated (raw 16-bit PCM)."""
        text = speakable(str(body.get("text", "")))[:600]   # emoji are for the window only
        if not text:
            raise HTTPException(400, "empty text")
        req = tts_client.build_request("POST", "/stream", json={"text": text})
        try:
            r = await tts_client.send(req, stream=True)
        except httpx.HTTPError:
            raise HTTPException(503, "voice service not running")
        if r.status_code != 200:
            await r.aclose()
            raise HTTPException(503, "voice service unavailable")

        async def pump():
            try:
                async for chunk in r.aiter_raw():
                    yield chunk
            finally:
                await r.aclose()  # also closes the service stream if the window stops listening (barge-in)

        return StreamingResponse(pump(), media_type="application/octet-stream",
                                 headers={"X-Sample-Rate": r.headers.get("X-Sample-Rate", "24000")})

    # ---- face reference library ----
    def face_call(fn, *a, **k):
        try:
            return fn(*a, **k)
        except FaceError as e:
            raise HTTPException(400, str(e))

    @app.post("/api/face/scan")
    async def face_scan():
        return await asyncio.to_thread(face_call, faces.scan)

    @app.get("/api/face/library")
    def face_library():
        return face_call(faces.summary)

    @app.patch("/api/face/{sha}/{face_i}")
    def face_review(sha: str, face_i: int, body: FaceReviewIn):
        return face_call(faces.review, sha, face_i, body.status, body.preferred, body.age_label)

    # ---- Google Photos → face staging (the owner picks photos in Google's picker; faces still need their review) ----
    gphotos = GooglePhotos(ROOT / "config" / "google-client.json", ROOT / "data" / "face-google",
                           f"http://127.0.0.1:{cfg.server.port}/api/face/google/callback", faces)

    @app.get("/api/face/google")
    def face_google():
        return {"set_up": bool(gphotos.client()), "accounts": gphotos.accounts()}

    @app.get("/api/face/google/connect")
    def face_google_connect(hint: str = ""):
        from fastapi.responses import RedirectResponse
        try:
            return RedirectResponse(gphotos.sign_in_url(hint))
        except RuntimeError as e:
            raise HTTPException(400, str(e))

    @app.get("/api/face/google/callback", response_class=Response)
    def face_google_callback(code: str = "", state: str = "", error: str = ""):
        page = "<body style='font:18px system-ui;padding:40px'>{}</body>"
        if error or not code:
            return Response(page.format("<h2>Google sign-in was cancelled.</h2><p>You can close this tab.</p>"), media_type="text/html")
        try:
            email = gphotos.finish(code, state)
        except Exception as e:
            log.warning("google photos sign-in failed: %s", type(e).__name__)
            return Response(page.format("<h2>That didn't work.</h2><p>Close this tab and press “Connect Google Photos” again.</p>"),
                            media_type="text/html")
        log.info("google photos account connected")   # the address itself isn't logged
        import html as _html
        return Response(page.format(f"<h2>✓ Connected {_html.escape(email)}</h2><p>Echo can only see photos you pick "
                                    "yourself in Google's picker. You can close this tab.</p>"), media_type="text/html")

    @app.post("/api/face/google/pick")
    def face_google_pick(body: FacePickIn):
        if not faces.staging.is_dir():
            raise HTTPException(400, "Face staging folder not found. Is the data drive plugged in?")
        try:
            return gphotos.start(body.email, body.group, body.age)
        except (RuntimeError, ValueError) as e:
            raise HTTPException(400, str(e))
        except httpx.HTTPError as e:
            log.warning("google photos picker failed: %s", type(e).__name__)
            raise HTTPException(502, "Google Photos didn't answer: check the internet, or reconnect the account")

    @app.get("/api/face/google/pick/{sid}")
    def face_google_status(sid: str):
        try:
            return gphotos.status(sid)
        except KeyError:
            raise HTTPException(404, "unknown pick")
        except httpx.HTTPError:
            raise HTTPException(502, "Google Photos didn't answer: check the internet")

    @app.delete("/api/face/google/{email}")
    def face_google_forget(email: str):
        gphotos.forget(email)
        return {"ok": True}

    # ---- photo collections Echo learns from (cats, places, nature, home, + the owner's own); learned only when approved ----
    photos = photos or PhotoLibrary(cfg.resolve(cfg.dataset_root), store, OllamaDescriber(
        cfg.llm.host, cfg.llm.model, {"temperature": cfg.llm.temperature, **cfg.llm.options}, cfg.llm.keep_alive))

    def photo_call(fn, *a, **k):
        try:
            return fn(*a, **k)
        except PhotoError as e:
            raise HTTPException(400, str(e))

    @app.get("/api/photos")
    def photos_list():
        return photo_call(photos.summary)

    @app.post("/api/photos/scan")
    async def photos_scan():
        return await asyncio.to_thread(photo_call, photos.scan)

    @app.post("/api/photos/categories")
    def photos_add_category(body: PhotoCategoryIn):
        return photo_call(photos.add_category, body.label)

    @app.post("/api/photos/describe")
    async def photos_describe(body: PhotoDescribeIn):
        if not body.sha256:
            return photo_call(photos.describe_waiting)
        try:
            return {"description": await asyncio.to_thread(photo_call, photos.describe, body.sha256)}
        except httpx.HTTPError:
            raise HTTPException(502, "Echo's model didn't answer: try again in a moment")

    @app.patch("/api/photos/{sha}")
    def photos_review(sha: str, body: PhotoPatchIn):
        return photo_call(photos.review, sha, body.status, body.caption, body.category)

    @app.post("/api/photos/{sha}/loved_one")
    def photos_remove_loved_one(sha: str):
        return photo_call(photos.remove_loved_one, sha)

    @app.post("/api/photos/consolidate")
    async def photos_consolidate():
        """CA 2.0 Phase 4: update photo memories now (camera dates, same-day links, overview). Versioned, no deletes."""
        return await asyncio.to_thread(photo_call, photos.consolidate)

    @app.get("/api/photos/view/{name}")
    def photos_view(name: str):
        path = photo_call(photos.view_path, name)
        if not path.exists():
            raise HTTPException(404, "not found")
        return FileResponse(path, media_type="image/jpeg")

    @app.post("/api/photos/google/pick")
    def photos_google_pick(body: PhotoPickIn):
        dest = photo_call(photos.folder, body.category) / "google"
        try:
            return gphotos.start_to(body.email, dest, photos.scan)
        except RuntimeError as e:
            raise HTTPException(400, str(e))
        except httpx.HTTPError as e:
            log.warning("google photos picker failed: %s", type(e).__name__)
            raise HTTPException(502, "Google Photos didn't answer: check the internet, or reconnect the account")

        # ---- expressions from the Collector app → the avatar's face (previewed, applied only when the owner clicks) ----
    @app.get("/api/face/expressions")
    def face_expr_status():
        root = cfg.resolve(cfg.dataset_root)
        return face_expressions.status(root) if root.is_dir() else {"error": "data folder not available"}

    @app.get("/api/face/expressions/{session}/sheet")
    def face_expr_sheet(session: str):
        pv = face_expressions.previews(cfg.resolve(cfg.dataset_root))
        if not face_expressions.SESSION_RE.match(session) or session not in pv:
            raise HTTPException(404, "no preview for that session")
        return FileResponse(pv[session] / "expressions-sheet.jpg", media_type="image/jpeg")

    @app.post("/api/face/expressions/{session}/apply")
    def face_expr_apply(session: str):
        if not face_expressions.SESSION_RE.match(session):
            raise HTTPException(400, "bad session")
        try:
            started = face_expressions.apply(cfg.resolve(cfg.dataset_root), session)
        except KeyError:
            raise HTTPException(404, "no preview for that session")
        if not started:
            raise HTTPException(409, "already working on the face")
        return face_expressions.status(cfg.resolve(cfg.dataset_root))

    @app.post("/api/face/expressions/previews")
    def face_expr_previews():
        face_expressions.make_missing_previews(cfg.resolve(cfg.dataset_root))
        return face_expressions.status(cfg.resolve(cfg.dataset_root))

    @app.get("/api/face/atlas")
    def face_atlas():
        return faces.atlas_meta() or {"available": False}

    @app.get("/api/face/atlas/frames/{name}")
    def face_atlas_frame(name: str):
        p = face_call(faces.atlas_frame, name)
        if not p.exists():
            raise HTTPException(404)
        return FileResponse(p, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=86400"})

    @app.get("/api/face/motion")
    def face_motion():
        return faces.motion_meta() or {"clips": []}

    @app.get("/api/face/motion/{name}")
    def face_motion_clip(name: str):
        p = face_call(faces.motion_file, name)
        if not p.exists():
            raise HTTPException(404)
        return FileResponse(p, media_type="video/mp4", headers={"Cache-Control": "private, max-age=86400"})

    @app.get("/api/face/crop/{name}")
    def face_crop(name: str):
        p = face_call(faces.crop_path, name)
        if not p.exists():
            raise HTTPException(404)
        return FileResponse(p, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=3600"})

    # ---- phone recordings over USB (Echo Persona Collector) ----
    phone = phone_sync.AdbPhone()

    @app.get("/api/phone/status")
    def phone_status():
        root = cfg.resolve(cfg.dataset_root)
        return phone_sync.status(root) if root.is_dir() else {"sessions": 0, "error": "external SSD not connected"}

    @app.post("/api/phone/sync")
    async def phone_copy():
        try:
            r = await asyncio.to_thread(phone_sync.sync, phone, cfg.resolve(cfg.dataset_root))
        except phone_sync.SyncError as e:
            raise HTTPException(400, str(e))
        log.info("phone sync copied=%d failed=%d", len(r["copied"]), len(r["failed"]))
        start_processing()   # new sessions go into the pipelines right away, in the background
        return r

    job = {"running": False, "current": None, "done": [], "error": None}

    def suggest_memories(told: dict) -> None:
        """A "Tell me a memory" recording was transcribed → suggestions for the My memories page (local model only)."""
        try:
            n = mem_candidates.suggest(store, told["transcript"], told.get("question") or "", told["source"],
                                       cfg.llm.host, cfg.llm.model, told.get("period"))
            log.info("memory suggestions from a recording: %d", n)   # the memories themselves are not logged
        except Exception as e:
            log.warning("memory suggestions failed: %s", type(e).__name__)

    def start_processing():
        if job["running"]:
            return
        root = cfg.resolve(cfg.dataset_root)

        def run():
            job.update(running=True, error=None, done=[])
            try:
                for rep in ingest.process_all(root, stt, lambda m: job.update(current=m), on_memory=suggest_memories):
                    job["done"].append(rep)
                if job["done"]:
                    update_personality()   # new own-words answers → new traits to review (decisions are kept)
                face_expressions.make_missing_previews(root)   # new expression recordings → preview in Avatar Collector
            except Exception as e:
                job["error"] = type(e).__name__
                log.warning("phone processing failed: %s", type(e).__name__)
            job.update(running=False, current=None)

        threading.Thread(target=run, daemon=True, name="phone-processing").start()

    @app.post("/api/phone/process")
    def phone_process():
        start_processing()
        return phone_progress()

    @app.get("/api/phone/process")
    def phone_progress():
        root = cfg.resolve(cfg.dataset_root)
        return {**job, "pending": ingest.pending(root) if root.is_dir() else []}

    # ---- personality ("How I talk") ----
    pdir = cfg.resolve(cfg.personality_dir)

    def draft() -> dict:
        p = pdir / "draft.json"
        if not p.exists():
            raise HTTPException(404, "No personality draft yet.")
        return json.loads(p.read_text())

    prompt_file = cfg.resolve(cfg.personality_file)
    versions = Versions(pdir, prompt_file if prompt_file.exists() else ROOT / "config" / "personality.example.yaml")

    def save_draft(d: dict) -> None:
        versions.snapshot_if_stale()   # a copy of how it was before this editing session (undo)
        (pdir / "draft.json").write_text(json.dumps(d, indent=2))
        # what Echo uses: only approved traits, plus style numbers and The owner's own sentences as examples
        approved = [{"category": t["category"], "trait": t["trait"]} for t in d["traits"] if t["status"] == "approved"]
        prof = {"updated": time.strftime("%Y-%m-%dT%H:%M:%S"), "traits": approved, "style": d["style"],
                "quotes": [q["text"] for q in d["quotes"]]}
        if d.get("use_condensed") and d.get("condensed"):   # the short summary of the approved traits is what's used
            prof["condensed"] = [c["text"] for c in d["condensed"]]
        (pdir / "profile.json").write_text(json.dumps(prof, indent=2))

    @app.get("/api/personality")
    def personality():
        d = draft()
        return {k: d.get(k) for k in ("built_at", "examples", "dropped_ungrounded", "style", "traits", "facts",
                                      "condensed", "use_condensed")}

    pjob = {"running": False, "last": None, "error": None}

    def update_personality():
        """Analyze any new own-words recordings (keeps every decision the owner made). Runs in the background."""
        if pjob["running"] or not (pdir / "draft.json").exists():
            return
        def run():
            pjob.update(running=True, error=None)
            try:
                d = persona.build(cfg.resolve(cfg.dataset_root), pdir, cfg.llm.host, cfg.llm.model, helper=helper)
                save_draft(d)   # refresh profile.json (approved traits only)
                pjob["last"] = d["last_update"] | {"at": time.strftime("%Y-%m-%dT%H:%M:%S")}
                log.info("personality update: %s", pjob["last"])
            except Exception as e:
                pjob["error"] = type(e).__name__
                log.warning("personality update failed: %s", type(e).__name__)
            pjob["running"] = False
        threading.Thread(target=run, daemon=True, name="personality-update").start()

    @app.post("/api/personality/update")
    def personality_update():
        update_personality()
        return pjob

    @app.get("/api/personality/update")
    def personality_update_status():
        return pjob

    @app.post("/api/personality/condense")
    async def personality_condense():
        d = draft()
        d["condensed"] = await asyncio.to_thread(persona.condense, d, cfg.llm.host, cfg.llm.model, helper)
        d["use_condensed"] = bool(d["condensed"])
        save_draft(d)
        return {"condensed": d["condensed"], "use_condensed": d["use_condensed"]}

    @app.patch("/api/personality/condensed")
    def personality_condensed(body: CondensedIn):
        d = draft()
        if body.lines is not None:
            d["condensed"] = [{"text": t.strip()[:300], "from": (d.get("condensed") or [{}] * len(body.lines))[i].get("from", [])
                               if i < len(d.get("condensed") or []) else [], "edited_by_owner": True}
                              for i, t in enumerate(body.lines) if t.strip()]
        if body.use is not None:
            d["use_condensed"] = body.use and bool(d.get("condensed"))
        save_draft(d)
        return {"condensed": d.get("condensed"), "use_condensed": d.get("use_condensed")}

    @app.patch("/api/personality/trait/{tid}")
    def personality_trait(tid: str, body: TraitIn):
        d = draft()
        t = next((t for t in d["traits"] if t["id"] == tid), None)
        if not t or (body.status and body.status not in ("pending", "approved", "rejected")):
            raise HTTPException(400, "bad trait or status")
        if body.trait is not None and body.trait.strip():
            t.setdefault("trait_original", t["trait"])   # so later rebuilds still recognise it
            t["trait"] = body.trait.strip()[:300]
            t["edited_by_owner"] = True
        if body.status:
            t["status"] = body.status
        save_draft(d)
        return t

    @app.patch("/api/personality/fact/{fid}")
    def personality_fact(fid: str, body: FactIn):
        d = draft()
        f = next((f for f in d["facts"] if f["id"] == fid), None)
        if not f or body.status not in ("saved", "rejected"):
            raise HTTPException(400, "bad fact or status")
        if body.status == "saved" and f["status"] != "saved":   # the owner confirmed it: a real memory now
            m, dup = store.add(Memory(content=f["fact"], kind="fact", status="confirmed", source="owner:personality-review"))
            check_conflicts_later(m, dup)
            f["memory_id"] = m.id
        f["status"] = body.status
        save_draft(d)
        return f

    @app.post("/api/personality/try")
    async def personality_try(body: TryIn):
        q = body.question.strip()[:500]
        msgs = build_messages(q)
        msgs = [msgs[0]] + [m for m in msgs[1:-1] if m["role"] == "system"] + [msgs[-1]]   # no conversation history
        reply = await backend.chat(msgs)
        return {"question": q, "answer": reply.text}

    @app.post("/api/personality/rate")
    def personality_rate(body: RateIn):
        if body.rating not in ("like_me", "not_like_me"):
            raise HTTPException(400, "bad rating")
        pdir.mkdir(parents=True, exist_ok=True)
        with open(pdir / "evals.jsonl", "a") as f:
            f.write(json.dumps(body.model_dump() | {"at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                                                     "traits_approved": len((persona.load_profile(pdir) or {}).get("traits", []))}) + "\n")
        return {"ok": True}

    # ---- personality versions (compare / undo) ----
    def conversation_turns() -> list[dict]:
        return [dict(r) for r in store.db.execute("SELECT role, content, ts FROM turns ORDER BY id")]

    @app.get("/api/longterm")
    def longterm_state():
        """CA 2.0 Phase 10: each measure's state (dormant with progress / baseline_set / ok / watch with reasons)."""
        last = lterm.last_run()
        return {"last_run": last, "baselines": lterm.baselines(), "settings": lterm.settings(),
                "next_run_due": lterm.due(), "data_leaves_this_computer": False}

    @app.post("/api/longterm/run")
    async def longterm_run_now():
        return await asyncio.to_thread(run_longterm)

    @app.post("/api/longterm/baseline/{metric}/reset")
    def longterm_reset(metric: str):
        """After an approved change meant to move a measure: start its baseline afresh (logged)."""
        done = lterm.reset_baseline(metric)
        log.info("long-term baseline reset: %s (%s)", metric, done)
        return {"reset": done}

    @app.get("/api/initiative")
    def initiative_state():
        """CA 2.0 Phase 9: what Echo would ask about next and why (scores are bookkeeping, not feelings)."""
        return initiative.report(store, curiosity, learning)

    @app.post("/api/lessons/insight")
    def add_insight(body: InsightIn):
        """Dream Bridge: an approved general insight becomes a lesson (never a memory). Max 3 active, 200 chars."""
        try:
            return trainer.add_insight(body.text, body.source, body.approved_by)
        except OverflowError as e:
            raise HTTPException(409, str(e))
        except ValueError as e:
            raise HTTPException(422, str(e))

    @app.delete("/api/lessons/insight/{source}")
    def drop_insight(source: str):
        return {"dropped": trainer.drop_insight(source)}

    @app.post("/api/memory/dream_link")
    def add_dream_link(body: DreamLinkIn):
        """Dream Bridge: an approved dream connection between two EXISTING memories (never a new memory)."""
        from memory import dreamlinks
        try:
            return dreamlinks.add(store.db, body.a, body.b, body.source, by=f"{body.approved_by}:dream")
        except OverflowError as e:
            raise HTTPException(409, str(e))
        except ValueError as e:
            raise HTTPException(422, str(e))

    @app.delete("/api/memory/dream_link/{a}/{b}")
    def remove_dream_link(a: int, b: int):
        from memory import dreamlinks
        return {"removed": dreamlinks.remove(store.db, a, b)}

    @app.get("/api/daydream/report")
    def daydream_report():
        if not dd_on:
            raise HTTPException(404)
        return daydream_score.report()

    @app.get("/api/memory/dream_links")
    def dream_links():
        from memory import dreamlinks
        return {"links": dreamlinks.listing(store.db)}

    @app.get("/api/development")
    def development_report():
        """CA 2.0 Phase 8: does each lesson the owner taught work? Before vs since, from Echo's real conversation (local,
        my test turns left out). Rates and counts only. Nothing is switched off automatically: the owner decides."""
        return development.report(trainer.lessons(), conversation_turns())

    @app.get("/api/personality/layers")
    def personality_layers():
        """CA 2.0 Phase 8 (directive Layer A / §9): Echo's personality as four separate layers plus its change history.
        1 foundation = the base instructions (changed only by a personality version the owner restores/saves);
        2 stable tendencies = traits the owner approved from their own words; 3 learned preferences = Train Echo lessons, each
        with its measured effect; 4 temporary state = the current conversation's state (cleared as conversations end)."""
        prof = json.loads((pdir / "profile.json").read_text()) if (pdir / "profile.json").exists() else {}
        measured = {x["id"]: x for x in development.report(trainer.lessons(), conversation_turns())["lessons"]}
        vers = versions.list()
        history = [{"at": v.get("time") or v.get("at"), "what": "personality version", "note": v.get("note"),
                    "id": v.get("id")} for v in vers]
        for les in trainer.lessons():
            history.append({"at": les.get("added"), "what": "lesson added", "id": les["id"], "by": les.get("taught_by")})
            if les.get("dropped"):
                history.append({"at": les["dropped"], "what": "lesson dropped", "id": les["id"]})
        return {"foundation": {"file": versions.prompt.name, "sha256": versions.current_hashes().get("personality.yaml"),
                               "latest_version": (vers[-1] if vers else {}).get("id")},
                "stable_tendencies": {"traits": prof.get("traits", []), "updated": prof.get("updated"),
                                      "approved_by": "the owner (How I talk page)"},
                "learned_preferences": [{"id": les["id"], "text": les["text"], "active": les.get("active", True),
                                         "verdict": measured.get(les["id"], {}).get("verdict")}
                                        for les in trainer.lessons()],
                "temporary_state": cstate.snapshot(),
                "history": sorted(history, key=lambda h: str(h["at"] or ""))}

    @app.get("/api/personality/versions")
    def list_versions():
        return {"versions": versions.list(), "current": versions.current_id()}

    @app.post("/api/personality/versions")
    def save_version(body: VersionIn):
        return versions.snapshot(body.note or "saved by the owner")

    @app.get("/api/personality/versions/{vid}/diff")
    def diff_version(vid: str, against: str = "current"):
        try:
            return versions.diff(vid, against)
        except KeyError:
            raise HTTPException(404)

    @app.post("/api/personality/versions/{vid}/restore")
    def restore_version(vid: str):
        try:
            r = versions.restore(vid)
        except KeyError:
            raise HTTPException(404)
        prompt["text"] = load_personality(cfg) + SYSTEM_EXTRA + training.lessons_block(lessons_path)
        log.info("personality version restored: %s", vid)
        return r

    # ---- personality checks ----
    edir = pdir / "evals"
    eval_job = {"running": False, "done": 0, "total": 0, "error": None}

    async def run_evals(note: str = "", repeat: int = 2):
        cases = pevals.load_cases(edir / "cases.json")
        eval_job.update(running=True, done=0, total=len(cases) * repeat, error=None)
        results = []
        try:
            for c in cases:
                for _ in range(repeat):
                    qvec = await asyncio.to_thread(embedder.query, c["question"]) if embedder else None
                    msgs = build_messages(c["question"], c.get("speaker"), qvec)
                    msgs = [msgs[0], msgs[-1]]   # no conversation history: each check starts fresh
                    parser = StreamParser(allow_name=True)
                    parser.ask(c["question"])
                    async for piece in backend.chat_stream(msgs):
                        parser.feed(piece)
                    text = parser.finish().text
                    why = pevals.check(text, c.get("checks", {}))
                    results.append({"id": c["id"], "category": c["category"], "approved": c.get("approved", False),
                                    "reply": text, "passed": not why, "why": why})
                    eval_job["done"] += 1
            pevals.save_run(edir / "runs", results, versions.current_id() or versions.snapshot("automatic, checks run")["id"], note)
        except Exception as e:
            eval_job["error"] = type(e).__name__
            log.warning("personality checks failed: %s", type(e).__name__)
        eval_job["running"] = False

    @app.get("/api/personality/evals")
    def get_evals():
        rs = pevals.runs(edir / "runs")
        return {"cases": pevals.load_cases(edir / "cases.json"), "job": eval_job,
                "last": rs[-1] if rs else None, "history": [{"at": r["at"], "version": r["version"], "note": r["note"],
                                                            "summary": r["summary"]} for r in rs]}

    @app.post("/api/personality/evals/run")
    async def start_evals(body: VersionIn):
        if not eval_job["running"]:
            asyncio.create_task(run_evals(body.note or ""))
        return eval_job

    @app.patch("/api/personality/evals/{cid}")
    def patch_eval(cid: str, body: EvalCaseIn):
        cases = pevals.load_cases(edir / "cases.json")
        c = next((c for c in cases if c["id"] == cid), None)
        if not c:
            raise HTTPException(404)
        if body.remove:
            cases.remove(c)
        if body.approved is not None:
            c["approved"] = body.approved
        (edir / "cases.json").write_text(json.dumps(cases, indent=2))
        return {"ok": True}

    # ---- status page for whoever looks after Echo ----
    status_cache = {"at": 0.0, "report": None}

    @app.get("/api/status")
    async def status_report():
        if time.time() - status_cache["at"] > 5:   # a few system commands: don't run them more than every 5 s
            from core import status as st
            status_cache["report"] = await asyncio.to_thread(st.report, store, stt, ROOT, cfg.resolve(cfg.dataset_root), pdir)
            status_cache["report"]["claude"] = helper.status()
            status_cache["at"] = time.time()
        return status_cache["report"]

    @app.get("/api/claude")
    def claude_status():
        return helper.status()

    @app.post("/api/claude/test")
    async def claude_test():
        return await asyncio.to_thread(helper.test)

    @app.patch("/api/claude")
    def claude_settings(body: ClaudeIn):
        c = helper.config()
        if body.enabled is not None:
            c["enabled"] = body.enabled
        for job, on in (body.jobs or {}).items():
            if job in c["jobs"]:
                c["jobs"][job] = bool(on)
        helper.save_config(c)
        status_cache["at"] = 0.0
        return helper.status()

    @app.delete("/api/turns")
    def clear_conversation():
        """Clears Echo's view only; the conversation stays in the archive (the owner, 2026-10-31)."""
        return {"cleared": True, "archived_turns": store.clear_turns()}

    # ---- conversation ----
    def build_messages(text: str, speaker: str | None = None, qvec: list[float] | None = None,
                       extra_hint: str = "", query: str | None = None, warm_only: bool = False) -> list[dict]:
        """Stable part first (base personality, approved traits, memories while few), then the conversation, then the
        parts that change every message (the owner's example sentences, memory search). Ollama reuses its work on an
        unchanged beginning, which cuts the wait for the first word. warm_only: just the part every next message shares
        (instructions + conversation), for reading it in advance — no memory search, nothing counted as used."""
        profile = persona.load_profile(pdir)
        notes = store.summaries()
        pack_text, pack_ids = pack.get(refresh=False)   # people + top memories; refreshed in quiet time (below)
        recent_turns = store.turns_window(as_sent=True)   # first: after a break this is where the window resets
        carry, _ = focus.carryover(store)   # CA 2.0 Phase 3: their own words from before a break, until a note covers them
        stable = [prompt["text"], persona.traits_block(profile), pack_text,
                  format_summaries(notes[-3:]), carry]                              # the latest conversation notes
        system = "\n\n".join(x for x in stable if x)
        # Older notes or facts that match this message go with the message itself, not the system block: Ollama merges
        # every system message into the first one, so anything that changes there makes it re-read the whole
        # conversation (≈ 3 s). The stored conversation keeps only what was really said.
        if warm_only:
            window = [t if t["role"] != "user" else {**t, "content": strip_hints(t["content"])} for t in recent_turns]
            return [{"role": "system", "content": system}] + _scrub_window(window) + [{"role": "user", "content": text}]
        recent_ids = {m.id for m in notes[-3:]} | pack_ids
        # memory/retrieve.py: by meaning, words, who/what is named ("my" = the speaker), and when ("on Thursday");
        # at most 3, only ones the message really points to, plus short facts about the people named
        user = text
        if query == "":   # small talk ("No problem."): no memory search (core/executive.py small_talk, 2026-11-05)
            log.info("recall skipped: small talk")
            recall_info.update(ids=[], ms=0.0, res=None)
        else:
            res = recall.retrieve(query or text, qvec, speaker, k=3, exclude=recent_ids)   # query: topic-aware (focus.py)
            log.info("recall %d memories, facts about %d, %.1f ms: %s", len(res.hits), len(res.facts), res.ms,
                     "; ".join("+".join(r.split()[0] for r in h.why) for h in res.hits))   # reasons only, no content
            store.note_use([h.memory.id for h in res.hits])
            recall_info.update(ids=[h.memory.id for h in res.hits], ms=round(res.ms, 1), res=res)
            if (note := format_for_message(res, store)):
                user = note + "\n\n" + text
        if extra_hint:
            user = user + "\n\n" + extra_hint
        if speaker:   # like the notes, this goes with the message only; the stored conversation keeps what was said
            who = {"owner": cfg.owner_name or "the owner", "loved_one": cfg.loved_one_name or "my loved one"}.get(
                speaker, speaker.replace("_", " "))
            user = (f"[Recognised by voice: {who} is speaking]" if speaker != "unknown"
                    else "[Voice not recognised for sure: I don't call anyone by name until I know who it is]") + "\n" + user
        window = [t if t["role"] != "user" else {**t, "content": strip_hints(t["content"])}
                  for t in recent_turns]
        return [{"role": "system", "content": system}] + _scrub_window(window) + [{"role": "user", "content": user}]

    def _scrub_window(window: list[dict]) -> list[dict]:
        waiting = False   # old life questions always hidden: the model kept re-asking them (a story hint names the one answered)
        last_a = max((i for i, t in enumerate(window) if t["role"] == "assistant"), default=-1)
        window = [t if t["role"] != "assistant" or (waiting and i == last_a) else {**t, "content": curiosity.scrub(t["content"])}
                  for i, t in enumerate(window)]
        checking = voice["check"] is not None   # my "Is that you?" waiting for an answer stays visible
        window = [t if t["role"] != "assistant" or (checking and i == last_a) else {**t, "content": scrub_identity(t["content"])}
                  for i, t in enumerate(window)]
        return window

    windows: list[WebSocket] = []  # open Echo windows; only the newest one may use the microphone

    async def training_hint(sock: WebSocket) -> None:
        try:
            await sock.send_json({"type": "training", "testing": trainer.testing, "busy": trainer.busy,
                                  "wrong": len(trainer.pending())})
        except Exception:
            pass

    async def daydream_hint(sock: WebSocket, state: str) -> None:
        """The small line in Echo's window: is Daydream on, and how to start or stop it (only where it exists)."""
        if not dd_on:
            return
        try:
            await sock.send_json({"type": "daydream", "state": state})
        except Exception:
            pass

    async def show_step(sock: WebSocket, step: "training.Step") -> None:
        """Training conversation (core/trainer.py): say it, offer buttons, replay the owner's words, or wait for Claude."""
        while step:
            if step.say:
                store.add_turn("assistant", step.say)
                await sock.send_json({"type": "reply", "text": step.say, "expression": step.expression,
                                      "intensity": 0.3 if step.expression != "neutral" else 0.0, "training": True})
            if step.choices:
                await sock.send_json({"type": "choices", "options": step.choices})
            if step.replay:
                await sock.send_json({"type": "replay", **step.replay})
            await training_hint(sock)
            if not step.work:
                await sock.send_json({"type": "state", "state": "idle"})
                return
            await sock.send_json({"type": "state", "state": "thinking"})
            try:
                step = await asyncio.to_thread(step.work)
            except Exception as e:  # noqa: BLE001
                log.warning("training step failed: %s", type(e).__name__)
                trainer.state = {"mode": "idle"}
                step = training.Step("Something went wrong in training, so I've paused it. Say \"Train Echo\" to go on.",
                                     expression="neutral")

    # core/music.py: "listen to this <YouTube link>" → approved genres/artists only (config/music.json), one at a time
    music_list = ROOT / "config" / "music.json"
    music_lock = asyncio.Lock()
    # 2026-11-03: listening WITH the owner/the loved one through the room mic (core/hearing.py; audio in memory only)
    def lyric_lookup(phrase: str) -> list[str]:
        """Part 2 (the owner: "lyric words only"): a few sung words → DuckDuckGo → result titles. Audited."""
        from core import search
        t0 = time.perf_counter()
        try:
            hits = search.search(f"{phrase} lyrics", limit=6)
            outcome = "ok"
        except Exception as e:  # noqa: BLE001
            hits, outcome = [], type(e).__name__
        tool_audit("lyric_lookup", "duckduckgo.com", "listening with me", outcome, len(phrase),
                   round((time.perf_counter() - t0) * 1000))
        return [h.title for h in hits]

    hear = hearing.Hearing(Path(store.path).parent / "hearing",
                           speaker_scores=lambda a: speakers.scores(a) if speakers.available else {},
                           stt_sync=getattr(stt, "transcribe_sync", None), may_split=lambda: SCHED.quiet(),
                           lookup=lyric_lookup, voice_embed=lambda a: speakers.embed(a) if speakers.available else None)
    hear_pool = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="hearing")

    def song_print_saved(info, y) -> None:
        title, artist = hearing.title_artist(info)
        hear.library.add(title, artist, hearing.song_print(y), vid=info.id)

    async def tell_windows(text: str, expression: str = "neutral", why: str = "something I said on my own") -> None:
        """Something Echo says on his own (e.g. after listening): shown, spoken and kept in the conversation.
        In testing mode it gets 👍/👎 too (logged as said on his own: it can't be replayed)."""
        store.add_turn("assistant", text)
        fid = trainer.record(f"({why})", text, training.SELF)
        for w in windows[-1:]:
            try:
                await w.send_json({"type": "reply", "text": text, "expression": expression,
                                   "intensity": 0.3 if expression != "neutral" else 0.0, **({"fid": fid} if fid else {})})
                await w.send_json({"type": "state", "state": "idle"})
            except Exception:
                pass

    quiet = {"until": 0.0}         # no "tell me about your life" questions until then (after commands, during songs)
    along: dict = {"task": None, "closed": None, "vid": None, "title": ""}   # the song I'm listening along to (newest only)

    def hush(seconds: float = 300) -> None:
        quiet["until"] = max(quiet["until"], time.time() + seconds)

    async def run_listen(vid: str, by: str, along_mode: bool = False) -> "music.Result":
        loop = asyncio.get_running_loop()

        def stt_call(seg):
            return asyncio.run_coroutine_threadsafe(stt.transcribe(seg), loop).result(timeout=120)
        async with music_lock:
            try:
                res = await asyncio.to_thread(
                    music.listen, vid, list_path=music_list, tmp_root=ROOT / "data" / "tmp" / "music", learner=learner,
                    classify=music.ollama_classifier(cfg.llm.host, cfg.llm.model), stt_call=stt_call, by=by,
                    along=along_mode, on_audio=song_print_saved, needs_print=lambda v: hear.library.by_vid(v) is None)
            except Exception as e:  # noqa: BLE001
                log.warning("listen job crashed: %s", type(e).__name__)
                res = music.Result(False, "Something went wrong while I was listening, sorry.")
        tool_audit("listen_music", "youtube.com", "along" if along_mode else "asked", res.kind, 0,
                   max(res.timings.values()) if res.timings else 0)
        return res

    async def listen_job(vid: str, by: str) -> None:
        res = await run_listen(vid, by)
        await tell_windows(res.say, "smiling" if res.ok else "neutral", "I said this after listening to a song you sent")

    async def follow_tabs(vid: str, by: str, started: float, closed: asyncio.Event, tabs: list,
                          first: asyncio.Task) -> "music.Result | None":
        """2026-11-03: follow the song in Chrome itself. YouTube plays the next song on its own (and its opener policy
        makes my page think the music window closed at once), so every few seconds I look at the YouTube tabs: a new
        video there → I listen along to that one too; no YouTube tab any more → the music is over. Returns the
        result for the last song that played."""
        jobs: dict[str, asyncio.Task] = {vid: first}
        current, gone, seen = vid, 0, False
        # older songs' listening jobs finish in the background (one at a time, music_lock)
        while time.time() - started < FOLLOW_MAX:
            vids = [v for v in (music.video_id(u) for u, _t in tabs) if v]
            seen = seen or bool(vids)
            if not seen and time.time() - started > FOLLOW_GRACE:
                return None   # my window isn't among the tabs I can see (Echo's own Chrome): use the song's length
            if vids:
                gone = 0
                if current not in vids and time.time() - started > FOLLOW_GRACE:   # YouTube went on to another song
                    # (autoplay, or the owner clicked one); not in the first seconds, while my window is still opening
                    current = vids[0]
                    title = next((browser.tab_song(t) for u, t in tabs if music.video_id(u) == current), "")
                    along.update(vid=current, title=title)
                    log.info("music moved on: now id=%s", current)
                    if current not in jobs:
                        jobs[current] = asyncio.create_task(run_listen(current, by, along_mode=True))
            elif time.time() - started > FOLLOW_GRACE:
                gone += 1
                if gone >= 2:   # twice in a row, so a page reload doesn't end it
                    break
            hush(600)
            try:   # my page's "closed" signal only makes me look sooner; Chrome decides
                await asyncio.wait_for(closed.wait(), timeout=FOLLOW_EVERY)
                closed.clear()
            except asyncio.TimeoutError:
                pass
            got = await asyncio.to_thread(browser.youtube_tabs)
            if got is None:   # can't ask Chrome any more: treat it as stopped
                break
            tabs = got
        return await jobs[current]

    async def listen_along(vid: str, by: str, started: float, closed: asyncio.Event) -> None:
        """the owner/the loved one asked me to play a song: I listen to it too, stay quiet while it plays, then talk about it.
        When it ends I close the music window (the mic comes back); if they close it first, I talk right away.
        If I can see Chrome's tabs, I follow what's really playing there instead of the song's length."""
        try:
            along.update(vid=vid, title="", started=started)
            await asyncio.sleep(min(1.0, FOLLOW_EVERY))   # let the music window open first
            tabs = await asyncio.to_thread(browser.youtube_tabs)
            first = asyncio.create_task(run_listen(vid, by, along_mode=True))
            first.add_done_callback(lambda t: along.update(title=t.result().info.title)
                                    if not t.cancelled() and t.result().info and along.get("vid") == vid else None)
            res = await follow_tabs(vid, by, started, closed, tabs, first) if tabs is not None else None
            if res is not None:
                along.update(vid=None, title="")
                for w in windows[-1:]:
                    try:
                        await w.send_json({"type": "music_stop"})
                    except Exception:
                        pass
                await asyncio.sleep(1.5)
                await tell_windows(music.after_song(res), "smiling", "I said this when the music I listened along to ended")
                return
            res = await first
            if not closed.is_set() and res.info and res.info.duration:
                left = res.info.duration - (time.time() - started) + 4
                hush(left + 300)
                log.info("listening along id=%s kind=%s, speaking in %.0f s", vid, res.kind, left)
                try:
                    await asyncio.wait_for(closed.wait(), timeout=max(0.0, min(left, music.MAX_SECONDS)))
                except asyncio.TimeoutError:
                    pass
            for w in windows[-1:]:
                try:
                    await w.send_json({"type": "music_stop"})   # song over: close it, mic back on
                except Exception:
                    pass
            along.update(vid=None, title="")
            await asyncio.sleep(1.5)   # let the mic settle before I speak
            await tell_windows(music.after_song(res), "smiling", "I said this when a song I listened along to ended")
        except asyncio.CancelledError:   # another song was asked for: that one takes over
            pass

    duck_state = {"warned": False}

    async def duck(on: bool) -> None:
        got = await asyncio.to_thread(browser.duck_music, on)
        if got == "js_off" and not duck_state["warned"]:   # once per run: tell the owner how to switch it on
            duck_state["warned"] = True
            log.info("music duck: Chrome's 'Allow JavaScript from Apple Events' is off")
            for w in windows[-1:]:
                try:
                    await w.send_json({"type": "note", "text": "To make the music softer while I talk, Chrome needs one "
                                       "setting: View > Developer > Allow JavaScript from Apple Events."})
                except Exception:
                    pass
        elif got not in ("ok", "none"):
            log.info("music duck: %s", got)

    async def now_playing() -> str:
        """2026-11-03 "What is playing right now?": read it from Chrome itself (YouTube may have moved on by itself)."""
        tabs = await asyncio.to_thread(browser.youtube_tabs)
        if tabs is None:
            return ("I can't see Chrome's tabs just now, so I can't tell what's playing. macOS may need to let me "
                    "control Google Chrome: System Settings, Privacy and Security, Automation.")
        if along.get("vid") and not any(music.video_id(u) == along["vid"] for u, _t in tabs):
            known = along.get("title") or ""
            return ((f"The last song I put on was {music.Info(id='', url='', title=known).label()}" if known else
                     "I put a song on in my music window") +
                    ", but I can't see that window, so if YouTube has moved on to another song I can't tell which.")
        if not tabs:
            return "Nothing is playing on YouTube in Chrome right now."
        mine = along.get("vid")
        url, title = next(((u, t) for u, t in tabs if mine and music.video_id(u) == mine), tabs[0])
        song = browser.tab_song(title)
        if not song:
            return "There's a YouTube video open in Chrome, but I can't read its name yet."
        label = music.Info(id=music.video_id(url) or "", url=url, title=song).label()
        more = f" There are {len(tabs)} YouTube videos open; that's the one in front." if len(tabs) > 1 and not mine else ""
        return f"It's {label}.{more}"

    async def watch_video(vid: str, started: float, closed: asyncio.Event) -> None:
        """A video (not music): no listening along; when it ends I close its window so the mic comes back on."""
        try:
            info = await asyncio.to_thread(music.fetch_info, vid)
            left = min(max(0.0, info.duration - (time.time() - started) + 3), music.MAX_SECONDS) if info.duration else 0
            hush(left + 300)
            if left:
                try:
                    await asyncio.wait_for(closed.wait(), timeout=left)
                except asyncio.TimeoutError:
                    pass
            if not closed.is_set():
                for w in windows[-1:]:
                    await w.send_json({"type": "music_stop"})
        except asyncio.CancelledError:
            pass
        except Exception as e:  # noqa: BLE001  (no length known: the window stays until it is closed)
            log.info("video length unknown: %s", type(e).__name__)

    async def start_music(sock: WebSocket, url: str, vid: str, by: str, along_on: bool = True) -> None:
        """Play in Echo's own music window (the page mutes the mic); music is listened along to, videos just play."""
        if along["task"] and not along["task"].done():
            along["task"].cancel()
        along["closed"] = asyncio.Event()
        along["started"] = time.time()
        await sock.send_json({"type": "music", "url": url, "vid": vid})
        along["task"] = asyncio.create_task(listen_along(vid, by, time.time(), along["closed"]) if along_on
                                            else watch_video(vid, time.time(), along["closed"]))

    @app.post("/api/hearing/chunk")
    async def hearing_chunk(request: Request):
        """A few seconds of raw room-mic audio (float32, 22,050 Hz) while listening with the owner/the loved one. This Mac only;
        turned into numbers in memory, never written to disk."""
        if not request.client or request.client.host not in ("127.0.0.1", "::1", "testclient"):   # (tests' client)
            raise HTTPException(403, "local only")
        if not hear.active():
            return {"active": False}
        body = await request.body()
        if len(body) % 4 or len(body) > 4 * hearing.SR * 20:
            raise HTTPException(400, "bad audio")
        y = np.frombuffer(body, dtype="<f4").copy()
        got = await asyncio.get_running_loop().run_in_executor(hear_pool, hear.feed, y)
        if got.get("new") and got.get("song"):
            log.info("hearing: recognised (conf %.2f)", got.get("confidence", 0))
            for w in windows[-1:]:
                try:
                    await w.send_json({"type": "note", "text": f"🎧 I think this is {got['song']}."})
                except Exception:
                    pass
        if got.get("guess"):
            log.info("hearing: guessed a song from the words")
            for w in windows[-1:]:
                try:
                    await w.send_json({"type": "note", "text": f"🎧 From the words I made out, it might be {got['guess']}. "
                                       "Ask me what I hear."})
                except Exception:
                    pass
        if "room_off_db" in got:
            log.info("hearing: room tuned (%d), was off by %.2f dB", hear.room.count, got["room_off_db"])
        return {k: v for k, v in got.items() if k in ("active", "skipped", "why", "song", "confidence", "new")}

    @app.post("/api/say")
    async def say_test(body: SayIn, request: Request):
        """Test only (scripts/say_test.py): the open window says this text as if the LLM wrote it, so stage-direction
        cues drive the face. Only from this Mac; not stored in the conversation."""
        if not request.client or request.client.host not in ("127.0.0.1", "::1"):
            raise HTTPException(403, "local only")
        if not windows:
            raise HTTPException(409, "Echo's window isn't open")
        sock, parser = windows[-1], StreamParser(allow_name=True)
        text = body.text.strip()[:2000]
        for i in range(0, len(text), 8):   # small pieces, like the model streams
            delta = parser.feed(text[i:i + 8])
            if delta:
                await sock.send_json({"type": "delta", "text": delta, "expression": parser.expression,
                                                  "blinks": parser.blinks})
            await asyncio.sleep(0.02)
        reply = parser.finish()
        if parser.leftover.strip():
            await sock.send_json({"type": "delta", "text": parser.leftover, "expression": parser.expression,
                                      "blinks": parser.blinks})
        await sock.send_json({"type": "reply", "text": reply.text, "expression": reply.expression,
                              "intensity": reply.intensity})
        await sock.send_json({"type": "state", "state": "idle"})
        return {"spoken": reply.text}

    async def group_reply(sock: WebSocket, text: str, grp: dict) -> None:
        """Echo talking WITH the owner and someone on a speakerphone call (they agreed on the device). His personality and
        memories are read as usual, but nothing is written: no turns, no memories, no questions of his own, no EDIM."""
        SCHED.turn_started()
        await sock.send_json({"type": "state", "state": "thinking"})
        try:
            msgs = build_messages(text, "owner", None)       # his memories may help; nothing is written
            msgs[-1]["content"] += "\n\n" + group_note(grp)
            parser = StreamParser(allow_name=True)
            async for piece in backend.chat_stream(msgs):
                if (delta := parser.feed(piece)):
                    await sock.send_json({"type": "delta", "text": delta, "expression": parser.expression,
                                          "blinks": parser.blinks})
            reply = parser.finish()
            await sock.send_json({"type": "reply", "text": reply.text, "expression": reply.expression,
                                  "intensity": reply.intensity, "group": True})
        except Exception as e:  # noqa: BLE001
            log.warning("group reply failed: %s", type(e).__name__)
            await sock.send_json({"type": "error", "text": "I couldn't answer just now."})
        await sock.send_json({"type": "state", "state": "idle"})

    @app.websocket("/ws")
    async def ws(sock: WebSocket):
        await sock.accept()
        # A phone through Echo Link (echolink/, which checked the phone's key and fixed its person) never silences the
        # Mac window and gets no shared history (the owner's and the loved one's conversations stay apart). Windows: newest wins.
        phone_user = sock.query_params.get("user") if sock.query_params.get("client") == "phone" else None
        if phone_user not in (None, "owner", "loved_one"):
            await sock.close(code=1008)
            return
        if not phone_user:
            for old in list(windows):  # works across browsers/profiles, unlike the page's BroadcastChannel
                try:
                    await old.send_json({"type": "stand_down"})
                except Exception:
                    pass
            windows.append(sock)
            if hear.active():   # a reloaded window picks the listening back up
                await sock.send_json({"type": "hear", "on": True})
        await sock.send_json({"type": "history", "turns": [] if phone_user else store.recent_turns(MAX_TURNS),
                              "training": {"testing": trainer.testing, "busy": trainer.busy, "wrong": len(trainer.pending())}})
        asked: dict[str, str] = (cstate.get("which_one") or {}).get("answers") or {}   # my last "which one?" (restored)
        dds = daydream.Session()
        await daydream_hint(sock, dds.state)

        async def plain_reply(say: str, expression: str = "neutral", fid: int | None = None) -> None:
            store.add_turn("assistant", say)
            await sock.send_json({"type": "reply", "text": say, "expression": expression,
                                  "intensity": 0.3 if expression != "neutral" else 0.0, **({"fid": fid} if fid else {})})
            SCHED.spoken_until(time.time() + speech_seconds(say))
            await sock.send_json({"type": "state", "state": "idle"})

        async def daydream_job(task: str, spk: str | None) -> None:
            """Runs beside the conversation, so "wake up" still gets through while Echo is thinking."""
            try:
                use_claude = dd_cfg["claude"]["use"]   # + jobs.daydream in config/claude.json, checked on each call
                drafters = {}
                if dd_cfg["tower"]["use"]:
                    drafters["tower"] = daydream.tower_drafter(dd_cfg)   # through the Dream Bridge (rev e)
                if use_claude:
                    drafters["claude"] = daydream.claude_drafter(helper)
                dds.result = daydream.Result(task=task)
                res = await asyncio.wait_for(daydream.run(
                    task, dd_local, dd_cfg, drafters, daydream.claude_critic(helper) if use_claude else None,
                    daydream.claude_checker(helper) if use_claude else None, on_event=dds.report, res=dds.result),
                    dd_cfg["task_timeout_s"])
                log.info("daydream done ok=%s parts=%d helpers=%s issues=%d ms=%s", res.ok, len(res.parts),
                         res.helpers, len(res.issues), res.ms.get("total"))
                if res.ok:   # the leading [smiling] tag and *face cues* become the face, never spoken (schema.py)
                    sp = StreamParser()
                    sp.feed(res.answer)
                    said = sp.finish()
                    fid = trainer.record(task, said.text, spk)
                    store.add_turn("assistant", said.text + "\n\n" + daydream.LEAVE)
                    await sock.send_json({"type": "reply", "text": said.text + "\n\n" + daydream.LEAVE,
                                          "expression": said.expression, "intensity": 0.3, "ddid": res.id,
                                          **({"fid": fid} if fid else {})})
                    SCHED.spoken_until(time.time() + speech_seconds(said.text))
                    await sock.send_json({"type": "state", "state": "idle"})
                    asyncio.create_task(dd_compare(res))
                else:
                    await plain_reply(daydream.FAILED)
            except asyncio.CancelledError:
                raise   # "wake up": already answered
            except Exception as e:  # noqa: BLE001  (timeout, model gone: never take the conversation down)
                log.warning("daydream failed: %s", type(e).__name__)
                try:
                    await plain_reply(daydream.FAILED)
                except Exception:  # noqa: BLE001
                    pass
            dds.state, dds.job = "idle", None
            await daydream_hint(sock, "idle")

        async def perform(act, spk) -> str:
            """Carry out an open/play Action (a command, or a "yes" to my offer); returns what to say."""
            say = act.say
            if act.close is not None:   # 2026-11-02: closing Chrome tabs showing that site
                n = await asyncio.to_thread(browser.run_close, act.close)
                log.info("browser close: %s tab(s)", n)
                if n is None:
                    return ("I couldn't close it. macOS may need to let me control Google Chrome: System Settings, "
                            "Privacy and Security, Automation.")
                return say if n else f"I don't see {act.say.removeprefix('Closing ').rstrip('.')} open in Chrome."
            hush()
            who_by = spk if spk and spk != "unknown" else "owner"
            top = await asyncio.to_thread(browser.youtube_top, act.play, act.random, act.exclude,
                                          act.avoid) if act.play else None
            if top and (vid := music.video_id(top)):   # play in my own window, mic off (music: listen along too)
                await start_music(sock, top, vid, who_by, along_on=not act.video)
                if not act.video:
                    last_play.update(artist=browser.last_artist(act.play, browser.music_artists()), vid=vid,
                                     at=time.time())
            elif (act.url is not None or act.script) and not await asyncio.to_thread(browser.run, act):
                say = "I couldn't open that just now. Is Google Chrome installed?"
            elif act.url:
                last_opened.update(host=browser._host(act.opened or act.url), name=act.say.removeprefix("Opening ").rstrip("."),
                                   at=time.time(), url=act.opened or act.url)
                cstate.set("opened", dict(last_opened))
            log.info("browser action url=%s script=%s play=%s", bool(act.url), bool(act.script), bool(act.play))
            return say

        def done(rec: cws.TurnRecord, route: str, spk: str | None) -> None:   # workspace record (observe only)
            rec.speaker = spk or "typed"
            if not rec.ecw:   # commands etc.: a minimal workspace snapshot
                rec.ecw = cws.LiveState(current_goal=rec.plan.get("kind"), next_action=route,
                                        active_context={"route": route}).snapshot()
            if not route.startswith("llm"):
                rec.first_word()   # commands answer at once
            if not rec.exec:   # Phase 7: commands, trainer, errors: the kind and the cache state still recorded
                rec.exec = {"kind": rec.plan.get("kind"), "cache": cache_state, "answered_by": route}
            rec.dialogue = edim.record()   # EDIM/awareness (ids and rule names only; {} when off)
            wslog.save(rec, route)
            try:   # CA 2.0: the persistent state after this turn (ids/labels only)
                ecw = rec.ecw if isinstance(rec.ecw, dict) else {}
                cstate.turn(spk, route, rec.plan.get("kind"), list(rec.memory_ids or []),
                            entity_names(ecw.get("related_concepts")))
            except Exception as e:  # noqa: BLE001
                log.warning("state save failed: %s", type(e).__name__)
        try:
            while True:
                msg = await sock.receive_json()
                if msg.get("type") == "music_closed":   # the owner closed the music window: talk about it now
                    if time.time() - along.get("started", 0) < EARLY_CLOSE_S:
                        continue   # YouTube's opener policy makes my page see the window "closed" ~1 s after it opens
                    if along.get("closed"):
                        along["closed"].set()
                    continue
                if msg.get("type") == "duck":   # I'm talking over music: softer music while I speak (2026-11-03)
                    asyncio.create_task(duck(bool(msg.get("on"))))
                    continue
                if msg.get("type") == "music_failed":   # the window couldn't open one: open it in Chrome instead
                    url = str(msg.get("url", ""))
                    if music.video_id(url):
                        await asyncio.to_thread(browser.run, browser.Action(say="", url=url))
                    continue
                if msg.get("type") == "feedback":   # 👍/👎 on a reply (testing mode)
                    try:
                        fid = int(msg.get("fid"))
                    except (TypeError, ValueError):
                        continue
                    step = trainer.vote(fid, str(msg.get("vote", "")), str(msg.get("note", "")))
                    log.info("feedback fid=%s vote=%s note=%s", fid, msg.get("vote"), bool(msg.get("note")))
                    await training_hint(sock)
                    if step:
                        await show_step(sock, step)
                    continue
                if msg.get("type") == "daydream_vote" and dd_on:   # 👍/👎 on a daydream answer → its score card
                    did, v = str(msg.get("ddid", ""))[:64], str(msg.get("vote", ""))
                    ok = await asyncio.to_thread(daydream_score.vote, did, v)
                    if dd_examples is not None and v in ("up", "down"):
                        await asyncio.to_thread(dd_examples.vote, did, v, dd_cfg["training"]["min_score"])
                    log.info("daydream vote %s ok=%s", msg.get("vote"), ok)
                    continue
                dd_activity["at"] = time.time()
                replay_of = msg.get("replay_of") if isinstance(msg.get("replay_of"), int) else None
                text = str(msg.get("text", "")).strip()[:4000]
                seen = camera_pictures(msg)   # Show Echo (the owner's tablet): pictures taken while he spoke, never stored
                # "Ask Echo about this document": only a linked device, only that person's own documents
                doc_ask = msg.get("doc") if phone_user and isinstance(msg.get("doc"), str) else None
                task = phone_task(msg) if phone_user else None   # calls & texts on the owner's device: prep / notes / draft
                if phone_user == "owner" and text and (grp := group_talk(msg)):
                    await group_reply(sock, text, grp)   # "Echo joins in" (speakerphone): nothing from it is kept
                    continue
                # who said it, from the voice (spoken messages only); anything that isn't a known profile is ignored
                speaker = msg.get("speaker") if msg.get("speaker") in (*speakers.profiles, "unknown") else None
                if phone_user:   # the linked phone's owner, decided on the Mac when it was linked
                    speaker = phone_user
                if not text:
                    continue
                rec = cws.TurnRecord()
                recall_info.clear()
                _tid, _ts = last_turn_row()
                cache_state = keeper.state(SCHED.cache_disturbed, _ts)   # Phase 7: was the conversation still cached?
                try:   # Stage 2a shadow mode: decision recorded only, nothing acts on it
                    rec.plan = controller.classify(text, known_names()).as_record()
                except Exception as e:  # noqa: BLE001
                    log.warning("controller failed: %s", type(e).__name__)
                last_turn["at"] = time.time()   # someone is talking: no note-writing now
                if (stopped := SCHED.turn_started()):   # conversation first: background LLM work stops right now
                    log.info("background work paused for the conversation: %d request(s) stopped", stopped)
                # "This is the owner" / "This is the loved one", said out loud: that phrase is a fresh room-mic sample of their voice
                # (the owner, 2026-10-31). Only for people who already have a profile (the loved one's was made with her consent).
                if speaker and not phone_user and (named := self_named(text, {"owner": cfg.owner_name, "loved_one": cfg.loved_one_name})) \
                        and named in speakers.profiles:
                    last = voice["last"]
                    sc = (last or {}).get("scores", {})
                    other = max((v for k, v in sc.items() if k != named), default=0.0)
                    clearly_other = other >= speakers.threshold and other - sc.get(named, 0.0) >= 0.15   # a joke? skip
                    if last and time.time() - last["at"] < 30 and not clearly_other:
                        try:
                            n = await asyncio.to_thread(speakers.add_live, named, last["emb"], "said")
                            log.info("voice: room print added for %s (said their name), %d live", named, n)
                        except Exception as e:  # noqa: BLE001
                            log.warning("voice sample failed: %s", type(e).__name__)
                        voice["last"] = None
                    speaker, voice["check"] = named, None   # they told me who they are
                    voice["who"] = {"name": named, "at": time.time()}
                    if named == "owner":
                        voice["confirmed_until"] = time.time() + CONFIRMED_S
                chk = voice["check"]
                if chk and time.time() - chk["at"] < 180:   # the answer to my "Is that you?"
                    voice["check"] = None
                    if VOICE_YES.match(text):
                        n = await asyncio.to_thread(speakers.add_live, "owner", chk["emb"], "confirmed")
                        log.info("voice: room print added for owner (confirmed), %d live", n)
                        voice["who"] = {"name": "owner", "at": time.time()}
                        voice["confirmed_until"] = time.time() + CONFIRMED_S
                        say = f"Thanks{", " + cfg.owner_name if cfg.owner_name else ""}. I'll know your voice better now."
                        store.add_turn("user", text, speaker)
                        store.add_turn("assistant", say)
                        await sock.send_json({"type": "reply", "text": say, "expression": "smiling", "intensity": 0.3})
                        SCHED.spoken_until(time.time() + speech_seconds(say))
                        await sock.send_json({"type": "state", "state": "idle"})
                        done(rec, "voice_confirm", speaker)
                        continue
                now = time.time()
                heard_how = "voice" if speaker and speaker != "unknown" else ("typed" if not speaker else "unknown")
                if speaker and speaker != "unknown":                 # recognised: this is who I'm talking with now
                    voice["who"] = {"name": speaker, "at": now}
                elif speaker == "unknown" and (w := voice["who"]) and now - w["at"] < STICKY_S:
                    speaker, w["at"] = w["name"], now                  # unclear phrase, same conversation: still them
                    heard_how = "sticky"
                    log.info("voice: unclear, still %s (same conversation)", speaker)
                if replay_of is None:
                    edim.user(text, speaker, heard_how)   # EDIM/awareness observe every message (rules, < 1 ms; off = no-op)
                else:
                    edim.skip()                           # a replay: no stale note from the previous message
                if dd_on and replay_of is None and daydream.is_undo_training(text):   # back to the last good model
                    store.add_turn("user", text, speaker)
                    back = await asyncio.to_thread(daydream_train.Versions().undo)
                    if back:
                        await plain_reply(f"Okay. I'm going back to my previous model, {back['to']}. I'll restart in a "
                                          f"few seconds; give me half a minute.", "attentive")
                        daydream_train.restart_core()
                    else:
                        await plain_reply("There's no training to undo: I'm still on the model I started with.")
                    done(rec, "daydream_undo_training", speaker)
                    continue
                if dd_on and replay_of is None and daydream.is_report(text):   # "daydream report": the trends
                    store.add_turn("user", text, speaker)
                    say = await asyncio.to_thread(lambda: daydream_score.report_text(daydream_score.report()))
                    await plain_reply(say, "attentive")
                    done(rec, "daydream_report", speaker)
                    continue
                if dd_on and replay_of is None:   # "daydream" / "wake up" (core/daydream.py)
                    starts, task = daydream.parse_start(text)
                    if dds.state != "idle" and daydream.is_stop(text):
                        if dds.state == "working" and dds.result:   # the Observatory sees it end
                            asyncio.create_task(dds.report(dds.result, "stopped"))
                        dds.leave()
                        store.add_turn("user", text, speaker)
                        await plain_reply(daydream.LEAVE_STOPPED)
                        await daydream_hint(sock, "idle")
                        done(rec, "daydream_stop", speaker)
                        continue
                    if dds.state == "waiting" and not starts:
                        starts, task = True, text
                    if starts:
                        hush()
                        store.add_turn("user", text, speaker)
                        if dds.state == "working":
                            await plain_reply("I'm still daydreaming about the last one. Say \"wake up\" to stop it.")
                        elif not task:
                            dds.enter(waiting=True)
                            await plain_reply(daydream.ENTER_ASK, "attentive")
                        else:
                            dds.enter(waiting=False)
                            await plain_reply(daydream.ENTER, "attentive")
                            await sock.send_json({"type": "state", "state": "thinking"})
                            dds.job = asyncio.create_task(daydream_job(task, speaker))
                        await daydream_hint(sock, dds.state)
                        done(rec, "daydream", speaker)
                        continue
                if replay_of is None and (step := trainer.handle(text)):   # testing / "Train Echo" / yes-no / pick a fix
                    hush()
                    store.add_turn("user", text, speaker)
                    await show_step(sock, step)
                    done(rec, "trainer", speaker)
                    continue
                music_say = None
                if (genre := music.add_request(text)):
                    added = await asyncio.to_thread(music.add_genre, music_list, genre)
                    music_say = (f"Okay, {genre} is on my music list now." if added else f"{genre.capitalize()} is already on my music list.")
                elif hearing.asks_start(text):   # 2026-11-03: listen with me through the room mic
                    hear.start(speaker if speaker and speaker != "unknown" else "owner", along.get("vid") or "")
                    await sock.send_json({"type": "hear", "on": True})
                    asyncio.get_running_loop().run_in_executor(hear_pool, hear.warm)   # ready for the first split
                    n = hear.library.count()
                    music_say = ("Okay, I'm listening with you through the microphone. I'll stay quiet; ask me what I "
                                 "hear anytime, and say \"stop listening\" when we're done." +
                                 ("" if n else " I don't know any songs by ear yet, so tell me what one is and I'll remember it."))
                elif hear.active() and hearing.asks_stop(text):
                    sess = hear.stop()
                    await sock.send_json({"type": "hear", "on": False})
                    music_say = hear.summary(sess) if sess else "Okay."
                    asyncio.get_running_loop().run_in_executor(hear_pool, hear.release_now)   # GPU back to my voice
                elif (hear.active() and hear.session and hear.session.guess and browser.is_yes(text) and
                      "Is that right?" in next((t["content"] for t in reversed(store.recent_turns(2))
                                                if t["role"] == "assistant"), "")):
                    music_say = await asyncio.get_running_loop().run_in_executor(hear_pool, hear.confirm_guess)
                elif hear.active() and (who := hearing.teaching_who(text)):   # part 3: "that's Donald Fagen singing"
                    music_say = await asyncio.get_running_loop().run_in_executor(hear_pool, hear.teach_who, *who)
                elif hear.active() and (role := hearing.asks_who(text)):     # "who's on drums?"
                    music_say = await asyncio.get_running_loop().run_in_executor(hear_pool, hear.who, role)
                elif hear.active() and (taught := hearing.teaching(text)):
                    music_say = await asyncio.get_running_loop().run_in_executor(hear_pool, hear.teach, *taught)
                elif hear.active() and (hearing.asks_what(text) or browser.asks_now_playing(text)):
                    music_say = await asyncio.get_running_loop().run_in_executor(hear_pool, hear.describe_now)
                elif (recap := hear.recap(text)):   # 2026-11-04: "who was that?" right after "stop listening"
                    music_say = recap
                elif browser.asks_now_playing(text):   # 2026-11-03: from Chrome, not from what I last opened
                    music_say = await now_playing()
                elif (vid := music.listen_request(text)):
                    busy = music_lock.locked()
                    asyncio.create_task(listen_job(vid, speaker if speaker and speaker != "unknown" else "owner"))
                    music_say = ("I'll listen to that as soon as I finish the song I'm on." if busy else
                                 "I'll check it's music on my list, then listen. It takes a minute or two; I'll tell you when I'm done.")
                    log.info("music request id=%s", vid)
                if music_say:
                    hush()
                    store.add_turn("user", text, speaker)
                    store.add_turn("assistant", music_say)
                    fid = trainer.record(text, music_say, speaker, replay_of)   # commands can be wrong too: 👍/👎
                    await sock.send_json({"type": "reply", "text": music_say, "expression": "smiling", "intensity": 0.3,
                                          **({"fid": fid} if fid else {})})
                    SCHED.spoken_until(time.time() + speech_seconds(music_say))
                    await sock.send_json({"type": "state", "state": "idle"})
                    done(rec, "music", speaker)
                    continue
                # "Yes, let's do it." right after I offered to open/play something: do exactly that (2026-11-02)
                offer = pending_offer.get("act") if time.time() - pending_offer.get("at", 0) < OFFER_S else None
                pending_offer.clear()
                if cstate.data.get("offer"):
                    cstate.offer(None)
                from_offer = offer if offer and browser.is_yes(text) else None
                recent_words = " ".join(t["content"] for t in store.recent_turns(6))   # "them" = who we talked about
                act = (from_offer or (asked and browser.answer(text, asked)) or browser.parse_close(text, last_opened)
                       or browser.parse_play(text, speaker, recent=recent_words, last_play=last_play) or browser.parse(text))
                if asked:
                    cstate.set("which_one", None)
                asked = {}
                if act:   # "open the Weather Channel": done here, the LLM never picks an address
                    say = await perform(act, speaker)
                    asked = act.answers or {}
                    if asked:
                        cstate.set("which_one", {"at": time.time(), "answers": asked})
                    if from_offer:
                        rec.hints.append("yes_to_offer")
                    store.add_turn("user", text, speaker)
                    store.add_turn("assistant", say)
                    fid = trainer.record(text, say, speaker, replay_of)
                    await sock.send_json({"type": "reply", "text": say, "expression": act.expression,
                                          "intensity": 0.3 if act.expression != "neutral" else 0.0,
                                          **({"fid": fid} if fid else {})})
                    SCHED.spoken_until(time.time() + speech_seconds(say))
                    await sock.send_json({"type": "state", "state": "idle"})
                    done(rec, "browser", speaker)
                    continue
                # 2026-11-03: "Can you show me the picture?" / a yes to my offer to show it → the photo on screen
                prev_reply = next((t["content"] for t in reversed(store.recent_turns(2)) if t["role"] == "assistant"), "")
                showing = time.time() - last_photo.get("shown", 0) < PHOTO_S
                # 2026-11-05 (the owner): a photo I put up closes by itself when the talk moves to something else
                if showing and last_photo.pop("keep_once", None):
                    pass   # a flower I just gave stays for their answer ("Thank you, Echo!")
                elif showing and not (showphoto.asks_to_close(text, True) or showphoto.asks_to_show(text)):
                    try:
                        pend = curiosity.pending() or {}
                        if showphoto.moved_on(text, showphoto.description(store, last_photo.get("sha")), prev_reply,
                                              str(pend.get("id", "")).startswith("photo:")):
                            last_photo.pop("shown", None)
                            showing = False
                            await sock.send_json({"type": "photo_close"})
                            log.info("photo closed: the conversation moved on")
                    except Exception as e:  # noqa: BLE001
                        log.warning("photo auto-close failed: %s", type(e).__name__)
                if showphoto.asks_to_close(text, showing) or showphoto.asks_to_show(text) or (
                        browser.is_yes(text) and showphoto.offered(prev_reply)):
                    last = last_photo.get("sha") if time.time() - last_photo.get("at", 0) < PHOTO_S else None
                    if showphoto.asks_to_close(text, showing):
                        last_photo.pop("shown", None)
                        say, expr = "Okay, I've put it away.", "neutral"
                        await sock.send_json({"type": "photo_close"})
                    # "Sure, show it to me" names nothing: the picture is the one I just offered (the owner, 2026-11-10)
                    elif not (sha := showphoto.pick(text, store, last) or (
                            showphoto.pick(prev_reply, store, last, tie_ok=True) if showphoto.PHOTO_TALK.search(prev_reply or "") else None)):
                        say, expr = "Which picture would you like to see? Tell me a little about it, like who's in it.", "neutral"
                    else:
                        try:
                            ok = photos.view_path(sha + ".jpg").exists()
                        except Exception:  # noqa: BLE001 (photo drive not connected)
                            ok = False
                        if ok:
                            last_photo.update(sha=sha, at=time.time(), shown=time.time(), shown_sha=sha)
                            say, expr = "Here it is.", "smiling"
                            await sock.send_json({"type": "photo", "url": f"/api/photos/view/{sha}.jpg"})
                        else:
                            say, expr = "I can't reach my photos right now. The photo drive may not be connected.", "neutral"
                    hush()
                    store.add_turn("user", text, speaker)
                    store.add_turn("assistant", say)
                    fid = trainer.record(text, say, speaker, replay_of)
                    await sock.send_json({"type": "reply", "text": say, "expression": expr,
                                          "intensity": 0.3 if expr != "neutral" else 0.0, **({"fid": fid} if fid else {})})
                    SCHED.spoken_until(time.time() + speech_seconds(say))
                    await sock.send_json({"type": "state", "state": "idle"})
                    done(rec, "photo", speaker)
                    continue
                fact = parse_remember(text)
                if fact:
                    teller = speaker if speaker and speaker != "unknown" else None
                    m, dup = store.add(Memory(content=fact, source=f"{teller or 'owner'}:chat", told_by=teller))
                    check_conflicts_later(m, dup)
                    rec.remembered = True
                    await sock.send_json({"type": "memory_saved", "memory": m.model_dump(), "duplicate": dup})
                await sock.send_json({"type": "state", "state": "thinking"})
                # Story threads (the owner, 2026-10-31): an answer to my question gets a thoughtful reply about that same
                # story and ONE follow-up question the model writes from what it knows about the topic (up to 2, then a
                # warm close); never "tell me about another trip". Answers become suggestions on My memories.
                follow_hint = None
                if (q := curiosity.take_answer(speaker, text)):   # the owner answered my life question → reviewed later
                    if (gid := mem_gaps.gap_id(q)) is not None:   # an objective the owner approved: answered now
                        learning.decide(gid, "done", "owner:answered")
                    curiosity.queue_answer(q, text)
                    follow_hint = curiosity.start_thread(q["text"], speaker, q.get("period"),
                                                         photo=str(q.get("id", "")).startswith("photo:"), answer=text)
                else:
                    last_reply = next((t["content"] for t in reversed(store.recent_turns(4)) if t["role"] == "assistant"), "")
                    if (f := curiosity.follow(speaker, text, last_reply)):
                        follow_hint, ans = f
                        curiosity.queue_answer(ans, text, teller="loved_one" if speaker == "loved_one" else None)
                not_done = browser.wants_action(text) and not tools.urls_in(text)   # "open/play …" but no command matched
                if not_done:
                    hush()
                ask = (None if follow_hint or curiosity.in_thread()
                       or (time.time() < quiet["until"] and not ASK_ME.search(text))   # not right after a command
                       else curiosity.maybe_ask(speaker, text))
                ask_held = None
                if ask and not ASK_ME.search(text) and not (held := edim.curiosity_ok())[0]:   # EDIM: not a good moment
                    log.info("curiosity question held by EDIM: %s", held[1])                  # (asked at a later pause)
                    ask, ask_held = None, held[1]
                # 2026-11-05 (the owner): a flower as a gift of appreciation (asked for, or after kindness: once a day)
                gift = None
                if not not_done:
                    try:
                        if (gift := flowers.decide(speaker, text)):
                            ask = None   # no life question on top of a gift
                            if gift.sha:
                                try:
                                    if not photos.view_path(gift.sha + ".jpg").exists():
                                        gift.sha = None
                                except Exception:  # noqa: BLE001 (photo drive not connected → the emoji)
                                    gift.sha = None
                    except Exception as e:  # noqa: BLE001
                        log.warning("flower gift failed: %s", type(e).__name__)
                        gift = None
                # CA 2.0 Phase 7 (core/executive.py): one plan for this message, every decision with its reason.
                # Memory search: a real follow-up ("what season was it taken in?") is searched with the previous
                # message (Phase 3); "It's freezing in here" is not a follow-up (11-03: it pulled in bridge photos).
                xplan, query = executive.plan_turn(text, focus.previous_user(store), rec.plan.get("kind", "chat"),
                                                   rec.plan.get("why"), not_done, cache_state)
                qvec = await asyncio.to_thread(embedder.query, query) if embedder and query else None   # ~15 ms
                cand, check_voice = voice["candidate"], False
                if (speaker == "unknown" and cand and time.time() - cand["at"] < 60 and not ask and not not_done
                        and not follow_hint
                        and time.time() - voice["asked_at"] >= ASK_EVERY_S and not trainer.busy
                        and time.time() >= voice["confirmed_until"]):
                    check_voice = True
                hint = (browser.NOT_DONE if not_done else
                        (follow_hint or (curiosity_hint(ask) if ask else (voice_ask(cfg.owner_name) if check_voice else ""))))
                if gift:
                    hint = (hint + "\n\n" if hint else "") + gift.hint(speaker)
                messages = build_messages(text, speaker, qvec, hint, query=query)
                if gift:   # the flower is on screen as he starts to talk
                    if gift.sha:
                        last_photo.update(sha=gift.sha, at=time.time(), shown=time.time(), shown_sha=gift.sha, keep_once=True)
                        await sock.send_json({"type": "photo", "url": f"/api/photos/view/{gift.sha}.jpg", "caption": gift.card()})
                    flowers.given(gift, shown=bool(gift.sha))
                    log.info("flower given to %s (%s, %s)", gift.who, "asked" if gift.asked else "appreciation",
                             "photo" if gift.sha else "emoji")
                rec.hints = [n for n, on in (("follow", follow_hint), ("ask", ask), ("not_done", not_done), ("flower", gift),
                                             ("voice_ask", check_voice), ("referential", query not in (text, "")),
                                             ("ask_held", ask_held)) if on]
                rec.memory_ids, rec.recall_ms = list(recall_info.get("ids", [])), recall_info.get("ms")
                turn_photo, asked_photo = None, False
                try:   # 2026-11-03: the photo this turn is about, for a later "show me the picture"
                    if ask and str(ask.get("id", "")).startswith("photo:") and ask.get("memory_id"):
                        row = store.db.execute("SELECT source FROM memories WHERE id = ?", (ask["memory_id"],)).fetchone()
                        ps = showphoto.sha_of(row[0]) if row else None
                    else:
                        ps = showphoto.top_photo(getattr(recall_info.get("res"), "hits", None))
                    if ps:
                        last_photo.update(sha=ps, at=time.time())
                        turn_photo = ps
                        asked_photo = bool(ask and str(ask.get("id", "")).startswith("photo:"))
                except Exception as e:  # noqa: BLE001
                    log.warning("photo tracking failed: %s", type(e).__name__)
                rec.meaning_search = (qvec is not None) if embedder else None
                try:   # Stage 4a: how well memory grounds this answer (recorded only)
                    rec.grounding = grounding.label(recall_info.get("res"), rec.plan.get("kind")).as_record()
                except Exception as e:  # noqa: BLE001
                    log.warning("grounding failed: %s", type(e).__name__)
                # Stage 4b (the owner OK'd 2026-11-01): nothing / only loose matches in memory → a one-time note so I say
                # "I don't remember" or "I think…" instead of guessing. ECHO_GROUNDING_NOTES=0 turns it off.
                note = grounding.note(rec.grounding.get("label")) if xplan.may("grounding") else ""
                if note:
                    messages[-1]["content"] += "\n\n" + note
                    rec.hints.append("grounding_" + rec.grounding["label"])
                    xplan.fired("grounding")
                # CA 2.0 Phase 6: metacognition checks (core/metacog.py) — a name no photo memory supports; a challenge
                if xplan.may("name_check") and xplan.may("correction_check") and xplan.may("count_check"):
                    res_now = recall_info.get("res")
                    photos_found = [h.memory.content for h in (getattr(res_now, "hits", None) or [])
                                    if (h.memory.source or "").startswith("photo:")]
                    if re.search(r"\b(?:photos?|pictures?|pics)\b", text, re.I):   # about the photos: check them all
                        photos_found = [r[0] for r in store.db.execute(
                            "SELECT content FROM memories WHERE source LIKE 'photo:%' AND hidden IS NULL")]
                    pets = frozenset(e["name"].lower() for e in ents.list() if e.get("type") == "pet" and e.get("name"))
                    for mnote, tag in ((metacog.name_note(text, known_names(), photos_found, pets), "name_check"),
                                       (metacog.correction_note(text, focus.previous_user(store)),
                                        "correction_check"),
                                       (metacog.count_note(text, known_names(), photos_found), "count_check")):
                        if mnote:
                            messages[-1]["content"] += "\n\n" + mnote
                            rec.hints.append(tag)
                            xplan.fired(tag)
                # CA 2.0 Phase 5: social notes (core/social.py) — "I'm fine" after sad news; a physical need in the room
                if xplan.may("social"):
                    rows = store.db.execute("SELECT role, content, ts FROM turns WHERE id > ? ORDER BY id DESC LIMIT 8",
                                            (store._view_from(),)).fetchall()
                    if (snote := social.note(text, [dict(r) for r in reversed(rows)])):
                        messages[-1]["content"] += "\n\n" + snote
                        rec.hints.append("social_note")
                        xplan.fired("social")
                # EDIM (Phase 3): the conversation note, only in mode "on" (shadow/off: nothing is added)
                if (enote := edim.note()) and xplan.may("dialogue"):
                    messages[-1]["content"] += "\n\n" + enote
                    rec.hints.append("dialogue_note")
                    xplan.fired("dialogue")
                # CA 2.0 Phase 8: the owner's own lessons about how to answer news/stories, next to the message (core/development.py)
                # (not when a social note fired: grief/physical needs come first — 11-03 eval_social: with both notes,
                #  "I'm fine, really" after a loss got "I'm glad to hear you're doing okay" 2/2)
                if "social_note" in rec.hints:
                    xplan.modules["style"] = "social_note_first"
                elif xplan.may("style") and (stnote := development.reminder(
                        text, trainer.lessons(active=True), asking_hint=bool(follow_hint or ask))):
                    messages[-1]["content"] += "\n\n" + stnote
                    rec.hints.append("style_note")
                    xplan.fired("style")
                # Date / clock sums worked out exactly (core/datecalc.py, the owner 2026-11-01): the model words the answer
                if xplan.may("date") and (dnote := datecalc.note(text)):
                    messages[-1]["content"] += "\n\n" + dnote
                    rec.hints.append("date_check")
                    xplan.fired("date")
                # Stage 7b (the owner OK'd 2026-11-01): a question about me → checked facts from my self-model, this once
                if xplan.may("self_model") and selfmodel.is_self_question(text):
                    try:
                        messages[-1]["content"] += "\n\n" + selfmodel.note(await current_self())
                        rec.hints.append("self_model")
                        xplan.fired("self_model")
                    except Exception as e:  # noqa: BLE001
                        log.warning("self note failed: %s", type(e).__name__)
                if seen:   # Show Echo: the pictures go with this one message only (not kept in history or memory)
                    messages[-1]["images"] = seen["images"]
                    messages[-1]["content"] += "\n\n" + camera_note(seen, speaker)
                    rec.hints.append("camera")
                if task:
                    messages[-1]["content"] += "\n\n" + task_note(task, phone_user)
                    rec.hints.append("task_" + task["kind"])
                # Errands (the owner's device): Echo prepares them, the owner approves each with one tap; money never spent
                elif phone_user == "owner" and (errands.looks_like_errand(text) or errands.wants_money(text)):
                    try:
                        items = await errands.extract(backend, text) if errands.looks_like_errand(text) else []
                        if items:
                            await sock.send_json({"type": "errands", "items": items})
                        # always: he can text and call (even when nothing could be prepared from this one)
                        messages[-1]["content"] += "\n\n" + errands.errand_note(items, errands.wants_money(text), phone_user)
                        rec.hints.append("errands")
                    except Exception as e:  # noqa: BLE001
                        log.warning("errands failed: %s", type(e).__name__)
                if doc_ask:
                    try:
                        found = DOCS.relevant(phone_user, doc_ask, text)
                        if found:
                            messages[-1]["content"] += "\n\n" + doc_note(found[0], found[1], __import__("echolink.owner", fromlist=["display"]).display(phone_user))
                            rec.hints.append("document")
                    except Exception as e:  # noqa: BLE001
                        log.warning("document note failed: %s", type(e).__name__)
                # UCA2 Phase 2: the live workspace for this turn (ids/labels only)
                ecw = cws.LiveState()
                ecw.publish("message", current_goal=rec.plan.get("kind"), active_systems=rec.plan.get("needs", []),
                            active_context={"speaker": speaker or "typed"})
                res_now = recall_info.get("res")
                ecw.publish("retrieved", retrieved_evidence=list(rec.memory_ids),
                            related_concepts=[e.get("id") for e in (getattr(res_now, "entities", None) or [])])
                ecw.publish("grounded", confidence=rec.grounding.get("label"))
                # core/tools.py: on web questions Echo may read pages himself (addresses the owner gave only)
                turn = None
                tset = permissions.load_settings(tools_cfg)
                # 2026-11-03: "what do you see on the weather channel?" → read the page I opened (as Chrome shows it)
                page_q = None if not_done else browser.page_to_read(text, last_opened)
                if tools_on and tset["enabled"] and hasattr(backend, "chat_stream_tools") and (
                        (tools.wants_tools(text) and not (phone_user and controller.phone_request(text))) or page_q):
                    turn = tools.Turn(research=tools.wants_research(text), follow=tset["follow_links"],
                                      max_pages=tset["max_pages"])
                    turn.allow(text, *[t["content"] for t in store.recent_turns(6)])
                    if page_q:
                        turn.allow(page_q[0])
                        turn.live.add(tools._key(page_q[0]))
                        messages[-1]["content"] += "\n\n" + browser.page_note(page_q[1])
                        rec.hints.append("read_open_page")
                    turn.question = text   # Stage 6b shadow: the shortcut ranks links by the user's own words
                    xplan.fired("tools")
                    messages[-1]["content"] += "\n\n" + tools.HINT
                    if turn.research and tset["follow_links"] != "never":
                        messages[-1]["content"] += "\n" + tools.RESEARCH_HINT.format(n=tset["max_pages"])
                try:
                    # their name only now and then: allowed if none of the last 5 replies used it
                    recent = [t["content"] for t in store.recent_turns(10) if t["role"] == "assistant"][-5:]
                    name = cfg.loved_one_name.strip()
                    own = {cfg.owner_name.strip().lower(), "owner"} - {""}
                    parser = StreamParser(trim_opener=xplan.may("style_filter"),   # Phase 8: the owner's lesson
                                          opener_names=people_names(),
                                          allow_name=(gift is not None and gift.who == "loved_one")   # "<name>, this one's for you"
                                          or not name or not any(name.lower() in r.lower() for r in recent),
                                          name=name,
                                          block_names=[n for n in social.blocked_names(speaker, cfg)   # my own "Is that
                                                       if not (check_voice and n == cfg.owner_name)   # you, <name>?" stays
                                                       and not (gift and n.lower() in (own if gift.who == "owner" else {name.lower()}))])
                    parser.ask(text)   # "blink for me" / "smile for me" happen even if the model forgets
                    t_start, t_first = time.perf_counter(), None
                    pre = (tools.prefetch_calls(text) if turn else []) or ([{"function": {"name": "read_webpage", "arguments": {
                        "url": page_q[0], "question": text}}}] if turn and page_q else [])
                    words = (tools.stream_with_tools(backend, messages, turn, prefetch=pre) if turn
                             else backend.chat_stream(messages))
                    async for piece in words:
                        t_first = t_first or time.perf_counter()
                        rec.first_word()
                        delta = parser.feed(piece)
                        if delta:  # words go to the window (and the voice) as they are written
                            await sock.send_json({"type": "delta", "text": delta, "expression": parser.expression,
                                                  "blinks": parser.blinks})
                    reply = parser.finish()
                    if parser.leftover.strip():
                        await sock.send_json({"type": "delta", "text": parser.leftover, "expression": parser.expression,
                                      "blinks": parser.blinks})
                except Exception as e:  # keep the UI alive if the backend fails
                    log.warning("llm error: %s", type(e).__name__)
                    await sock.send_json({"type": "error", "message": "My thinking engine isn't responding. Is Ollama running?"})
                    await sock.send_json({"type": "state", "state": "idle"})
                    done(rec, "llm_error", speaker)
                    continue
                SCHED.cache_restored()   # this reply just read the whole conversation: it is in the model's cache now
                store.add_turn("user", text, speaker, sent=strip_hints(messages[-1]["content"]))   # as sent, minus one-time notes
                cited = ""
                if turn and turn.sources:   # sources kept with the reply, so follow-up questions know the address
                    seen = {s["url"]: s for s in turn.sources}.values()
                    cited = "\n\nSources: " + "; ".join(f"{s['title']} ({s['url']})" for s in seen)
                    await sock.send_json({"type": "sources", "sources": list(seen)})
                store.add_turn("assistant", reply.text + cited)
                edim.reply(reply.text)
                # quiet-time jobs wait until the reply has been SPOKEN, not just written: the voice is still talking for
                # about len/14 s after the text is done, and a background LLM job then makes the voice hitch (2026-11-02)
                last_turn["at"] = time.time() + speech_seconds(reply.text)
                SCHED.spoken_until(last_turn["at"])
                if check_voice and "is that you" in reply.text.lower():   # asked: the next "yes" teaches my ears
                    voice.update(check={"emb": cand["emb"], "at": time.time()}, asked_at=time.time(), candidate=None)
                if ask and asked_in(reply.text, ask):   # he really asked it: wait for the owner's answer
                    curiosity.asked(ask)
                    if (gid := mem_gaps.gap_id(ask)) is not None:
                        learning.event(gid, "asked the owner", "echo")
                st = getattr(backend, "last_stats", {})
                log.info("turn ok expression=%s first_token_ms=%d prompt_tokens=%s prompt_ms=%s reply_tokens=%s "
                         "thinking_chars=%s tools=%s", reply.expression, round(((t_first or time.perf_counter()) - t_start) * 1000),
                         st.get("prompt_tokens"), st.get("prompt_ms"), st.get("reply_tokens"),
                         st.get("thinking_chars", 0), [c["name"] + (":ok" if c["ok"] else ":no") for c in turn.calls]
                         if turn else "-")   # content never logged
                fid = trainer.record(text, reply.text, speaker, replay_of)   # testing on: logged for 👍/👎
                await sock.send_json({"type": "reply", **reply.model_dump(), **({"fid": fid} if fid else {}),
                                      **({"replay_of": replay_of} if replay_of else {})})
                # 2026-11-05 (the owner): when I talk about a photo, it goes on screen so he can see what I mean
                try:
                    if (turn_photo and not gift and not (time.time() - last_photo.get("shown", 0) < PHOTO_S
                                            and last_photo.get("shown_sha") == turn_photo)
                            and showphoto.reply_shows(reply.text, showphoto.description(store, turn_photo), asked_photo)
                            and photos.view_path(turn_photo + ".jpg").exists()):
                        last_photo.update(sha=turn_photo, at=time.time(), shown=time.time(), shown_sha=turn_photo)
                        await sock.send_json({"type": "photo", "url": f"/api/photos/view/{turn_photo}.jpg"})
                        rec.hints.append("photo_shown")
                    # the owner asked to see one ("show it to me") and my answer names it ("how about the photo of you and
                    # Daisy from January?"): show that one now instead of only offering it (2026-11-10)
                    elif (showphoto.asks_to_show(text) or browser.is_yes(text) or re.search(r"\bshow\b", text, re.I)) \
                            and not showphoto._BRUCE_SHOWS.search(text) and showphoto.PHOTO_TALK.search(reply.text) \
                            and (sha := showphoto.pick(reply.text, store, last_photo.get("sha"), tie_ok=True)) \
                            and photos.view_path(sha + ".jpg").exists():
                        last_photo.update(sha=sha, at=time.time(), shown=time.time(), shown_sha=sha)
                        await sock.send_json({"type": "photo", "url": f"/api/photos/view/{sha}.jpg"})
                        rec.hints.append("photo_shown")
                except Exception as e:  # noqa: BLE001 (photo drive not connected: just talk)
                    log.warning("photo auto-show failed: %s", type(e).__name__)
                # 2026-11-02: an offer or announcement in my reply ("would you like to go to The Weather Channel?",
                # "I will open YouTube and play Do It Again") becomes real: kept for a "yes", or done now if I said I'm
                # doing it right after they said yes. Only things browser/music can really open or play.
                try:
                    if not not_done and (o := browser.offer_from_reply(reply.text, speaker, recent=recent_words + " " + text,
                                                                       last_play=last_play)):
                        o_act, claim = o
                        if claim and browser.is_yes(text):
                            said = await perform(o_act, speaker)
                            rec.hints.append("claim_done")
                            if said.startswith("I couldn't"):
                                await sock.send_json({"type": "reply", "text": said, "expression": "neutral", "intensity": 0.0})
                        else:
                            pending_offer.update(act=o_act, at=time.time())
                            cstate.offer(o_act)
                            rec.hints.append("offer_kept")
                except Exception as e:  # noqa: BLE001
                    log.warning("offer handling failed: %s", type(e).__name__)
                await sock.send_json({"type": "state", "state": "idle"})
                rec.llm_calls = st.get("rounds") or 1
                rec.prompt_tokens, rec.reply_tokens = st.get("prompt_tokens"), st.get("reply_tokens")
                rec.tools = [c["name"] + (":ok" if c["ok"] else ":no") for c in turn.calls] if turn else []
                rec.skill_shadow = list(turn.shadow) if turn else []
                ecw.publish("replied", next_action="tools" if turn else "reply",
                            active_context={"speaker": speaker or "typed", "route": "llm_tools" if turn else "llm"})
                rec.ecw = ecw.snapshot()
                rec.exec = xplan.as_record()
                if phone_user and turn is not None and getattr(turn, "hits", None):
                    try:   # the phone's side window: the websites this answer came from
                        await sock.send_json({"type": "panel", "kind": "web", "title": "Where this came from",
                                              "items": [{"title": h.title[:120], "url": h.url, "snippet": h.snippet[:240]}
                                                        for h in turn.hits[:6]]})
                    except Exception:  # noqa: BLE001
                        pass
                elif phone_user and side_window_question(text):
                    try:   # answered without looking anything up: earlier sources on this, or a quick search
                        items = recent_sources(text, store.recent_turns(12))
                        title = "Where this came from"
                        if not items:
                            from core import search as web_search
                            hits = await asyncio.to_thread(web_search.search, text, 5)
                            items = [{"title": h.title[:120], "url": h.url, "snippet": h.snippet[:240]} for h in hits]
                            title = "More about this"
                        if items:
                            await sock.send_json({"type": "panel", "kind": "web", "title": title, "items": items[:6]})
                    except Exception as e:  # noqa: BLE001
                        log.info("side window skipped: %s", type(e).__name__)
                try:   # Stage 5a: a question I couldn't answer → a proposed learning objective (recorded only)
                    if (gk := mem_gaps.detect(rec.plan.get("kind"), rec.grounding.get("label"), reply.text, text)):
                        learning.note(gk, text, speaker)
                        rec.hints.append("gap_" + gk)
                except Exception as e:  # noqa: BLE001
                    log.warning("gap not recorded: %s", type(e).__name__)
                done(rec, "llm_tools" if turn else "llm", speaker)
        except WebSocketDisconnect:
            pass
        finally:
            dds.leave()   # window closed mid-daydream: stop the work
            if sock in windows:
                windows.remove(sock)

    @app.middleware("http")
    async def fresh_pages(request: Request, call_next):
        """Pages always check for a newer copy, so a reload after an update never runs old code."""
        resp = await call_next(request)
        if resp.headers.get("content-type", "").startswith("text/html"):
            resp.headers["Cache-Control"] = "no-cache"
        return resp

    app.state.build_messages, app.state.reread = build_messages, reread   # for tests (Phase 7: warm prompt = next prefix)
    app.mount("/", StaticFiles(directory=ROOT / "renderer", html=True), name="ui")
    return app


def main() -> None:
    import uvicorn
    cfg = load_config()
    setup_logging(cfg)
    log.info("starting on %s:%s", cfg.server.host, cfg.server.port)
    uvicorn.run(create_app(cfg), host=cfg.server.host, port=cfg.server.port)


if __name__ == "__main__":
    main()
