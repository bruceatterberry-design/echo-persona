"""Echo Persona Core HTTP/WebSocket server. Serves the renderer UI on localhost."""
from __future__ import annotations

import logging
import time
from logging.handlers import RotatingFileHandler

import asyncio
import json
import subprocess
import os
import re
import threading
from contextlib import asynccontextmanager

import httpx
import numpy as np
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, PlainTextResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from core import browser
from core.config import ROOT, PersonaConfig, load_config, load_personality
from core.llm import LLMBackend, make_backend
from core.schema import StreamParser
from voice.stt import MAX_AUDIO_BYTES, MLXWhisperSTT, STTBackend, STTError, decode_audio, make_stt
from face.library import FaceError, FaceLibrary
from transfer import phone_sync
from processing import ingest
from personality import build as persona
from voice.studio import CONSENT_TEXT, MAX_TAKE_BYTES, Studio, StudioError
from voice.wake import detect_end, detect_wake, is_noise_transcript
from memory.store import Kind, Memory, MemoryStore, Status, format_for_prompt, format_summaries, parse_remember
from memory import summarize as summarizer

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


class TryIn(BaseModel):
    question: str


class RateIn(BaseModel):
    question: str
    answer: str
    rating: str                 # like_me | not_like_me
    note: str = ""


class FaceReviewIn(BaseModel):
    status: str | None = None
    preferred: bool | None = None
    age_label: str | None = None


class MemoryPatch(BaseModel):
    content: str | None = None
    kind: Kind | None = None
    status: Status | None = None


class ClaudeIn(BaseModel):
    enabled: bool | None = None
    jobs: dict[str, bool] | None = None


def create_app(cfg: PersonaConfig | None = None, backend: LLMBackend | None = None,
               store: MemoryStore | None = None, stt: STTBackend | None = None,
               studio: Studio | None = None, faces: FaceLibrary | None = None) -> FastAPI:
    cfg = cfg or load_config()
    backend = backend or make_backend(cfg.llm)
    store = store or MemoryStore(cfg.resolve(cfg.memory_db))
    system_prompt = load_personality(cfg) + (
        "\n\nI can open web pages in Chrome on this Mac when asked with \"open\" or \"go to\" plus a saved place (shortcuts "
        "in config/browser.json, such as the Voice Studio or the Weather Channel) or an address said out "
        "loud (\"go to apple dot com\"). That happens before I answer. If a request to open something reaches me anyway, "
        "I never say I can't open websites; I ask them to say it again with the address, like \"go to apple dot com\".")
    voice_cfg = ROOT / "config" / "voice.json"   # {"stt": "parakeet" | "whisper", "reference": ...}
    stt_choice = (json.loads(voice_cfg.read_text()).get("stt") if voice_cfg.exists() else None) or "whisper"
    stt = stt or make_stt(stt_choice, cfg.voice.stt_model, cfg.voice.language)
    stt_ready = {"ok": False}
    from core.claude_helper import ClaudeHelper   # optional: the owner's Claude account for background jobs (off by default)
    helper = ClaudeHelper(ROOT / "config" / "claude.json", ROOT / "data" / "claude-usage.json")

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

    async def warm_llm():  # read Echo's instructions + recent conversation now, so the first reply is quick
        await asyncio.sleep(2)
        try:
            if hasattr(backend, "warm"):
                t = time.perf_counter()
                await backend.warm(build_messages("Hello."))
                log.info("llm warmup %d ms", round((time.perf_counter() - t) * 1000))
        except Exception as e:
            log.warning("llm warmup failed: %s", type(e).__name__)

    last_turn = {"at": time.time()}

    async def summarize_when_quiet():
        """Every 15 s: if nobody has spoken for 20 s, write notes for conversation that slid out of view, then re-read
        the (changed) instructions so the next reply is still quick."""
        while True:
            await asyncio.sleep(15)
            if time.time() - last_turn["at"] < 20 or not hasattr(backend, "warm"):
                continue
            try:
                n = await asyncio.to_thread(summarizer.summarize_pending, store, len(store.turns_window()),
                                            cfg.llm.host, cfg.llm.model, 4, helper)
                if n:
                    log.info("conversation notes saved: %d", n)   # the notes themselves are not logged
                    await backend.warm(build_messages("Hello."))
            except Exception as e:
                log.warning("conversation notes failed: %s", type(e).__name__)

    @asynccontextmanager
    async def lifespan(_app):
        tasks = [asyncio.create_task(warm_stt()), asyncio.create_task(warm_llm()), asyncio.create_task(summarize_when_quiet())]
        yield
        for t in tasks:
            t.cancel()

    studio = studio or Studio(cfg.resolve(cfg.dataset_root))
    faces = faces or FaceLibrary(cfg.resolve(cfg.dataset_root))
    app = FastAPI(title="Echo Persona Core", lifespan=lifespan)
    app.state.store = store


    @app.post("/api/stt")
    async def speech_to_text(request: Request, mode: str = "direct", debug: bool = False):
        body = await request.body()
        if len(body) > MAX_AUDIO_BYTES:
            raise HTTPException(413, "audio too large")
        try:
            audio = await asyncio.to_thread(decode_audio, body)
            text = await stt.transcribe(audio)
        except STTError as e:
            raise HTTPException(400, str(e))
        log.info("stt ok mode=%s seconds=%.1f", mode, len(audio) / 16000)  # transcript deliberately not logged
        if os.environ.get("ECHO_LOG_TRANSCRIPTS") == "1":  # opt-in, for supervised testing only
            log.info("TEST transcript mode=%s: %r", mode, text)
        if mode == "wake":
            woke, command = detect_wake(text)
            # Without the wake phrase the transcript is discarded and never returned.
            out = {"wake": woke, "command": command} if woke else {"wake": False}
            if debug:  # the owner explicitly turned on "Show what I hear" in the window
                out["heard"] = text
            return out
        if mode == "conversation":  # follow-up turn after "Hey Echo": no wake word needed
            if is_noise_transcript(text):
                return {"text": ""}
            return {"text": text, "end": detect_end(text)}
        return {"text": text}

    @app.get("/health")
    async def health():
        return {"core": "ok", "llm": await backend.health(), "memories": len(store.all()),
                "stt": {"backend": stt.name, "ready": stt_ready["ok"],
                        "fallbacks_to_whisper": getattr(stt, "fallbacks", 0)}}

    # ---- memory API (used by the "What I remember" panel) ----
    @app.get("/api/memory")
    def list_memory():
        return [m.model_dump() for m in store.all()]

    @app.post("/api/memory")
    def add_memory(m: Memory):
        return store.save(m.model_copy(update={"source": "owner:panel"})).model_dump()

    @app.patch("/api/memory/{mid}")
    def patch_memory(mid: int, p: MemoryPatch):
        m = store.correct(mid, p.content) if p.content else store.get(mid)
        if m and (p.kind or p.status):
            m = store.update(mid, kind=p.kind, status=p.status)
        if not m:
            raise HTTPException(404)
        return m.model_dump()

    @app.delete("/api/memory/{mid}")
    def forget_memory(mid: int):
        if not store.forget(mid):
            raise HTTPException(404)
        return {"forgotten": mid}

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
        text = str(body.get("text", "")).strip()[:600]
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
        text = str(body.get("text", "")).strip()[:600]
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

    def start_processing():
        if job["running"]:
            return
        root = cfg.resolve(cfg.dataset_root)

        def run():
            job.update(running=True, error=None, done=[])
            try:
                for rep in ingest.process_all(root, stt, lambda m: job.update(current=m)):
                    job["done"].append(rep)
                if job["done"]:
                    update_personality()   # new own-words answers → new traits to review (decisions are kept)
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

    def save_draft(d: dict) -> None:
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
            m = store.save(Memory(content=f["fact"], kind="fact", status="confirmed", source="owner:personality-review"))
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
        store.clear_turns()
        return {"cleared": True}

    # ---- conversation ----
    def build_messages(text: str) -> list[dict]:
        """Stable part first (base personality, approved traits, memories while few), then the conversation, then the
        parts that change every message (The owner's example sentences, memory search). Ollama reuses its work on an
        unchanged beginning, which cuts the wait for the first word."""
        profile = persona.load_profile(pdir)
        facts, notes = store.facts(), store.summaries()
        stable = [system_prompt, persona.traits_block(profile),
                  format_for_prompt(facts if len(facts) <= 40 else facts[-40:]),   # facts rarely change
                  format_summaries(notes[-3:])]                                     # the latest conversation notes
        system = "\n\n".join(x for x in stable if x)
        # Older notes or facts that match this message go with the message itself, not the system block: Ollama merges
        # every system message into the first one, so anything that changes there makes it re-read the whole
        # conversation (≈ 3 s). The stored conversation keeps only what was really said.
        recent_ids = {m.id for m in notes[-3:]} | {m.id for m in facts[-40:]}
        # only clearly related notes: at least 2 meaningful words in common (not "how", "day", "the"…), at most 2
        said = content_words(text)
        extra = [m for m in store.search(text, limit=8)
                 if m.id not in recent_ids and len(said & content_words(m.content)) >= 2][:2]
        user = text
        if extra:
            user = "[From my notes, in case it helps: " + " | ".join(m.content for m in extra) + "]\n\n" + text
        return [{"role": "system", "content": system}] + store.turns_window() + [{"role": "user", "content": user}]

    windows: list[WebSocket] = []  # open Echo windows; only the newest one may use the microphone

    @app.websocket("/ws")
    async def ws(sock: WebSocket):
        await sock.accept()
        for old in list(windows):  # works across browsers/profiles, unlike the page's BroadcastChannel
            try:
                await old.send_json({"type": "stand_down"})
            except Exception:
                pass
        windows.append(sock)
        await sock.send_json({"type": "history", "turns": store.recent_turns(MAX_TURNS)})
        asked: dict[str, str] = {}   # answers to my last "which one?" question, if I asked one
        try:
            while True:
                msg = await sock.receive_json()
                text = str(msg.get("text", "")).strip()[:4000]
                if not text:
                    continue
                last_turn["at"] = time.time()   # someone is talking: no note-writing now
                act = (asked and browser.answer(text, asked)) or browser.parse(text)
                asked = {}
                if act:   # "open the Weather Channel": done here, the LLM never picks an address
                    say = act.say
                    if (act.url is not None or act.script) and not await asyncio.to_thread(browser.run, act):
                        say = "I couldn't open that just now. Is Google Chrome installed?"
                    asked = act.answers or {}
                    log.info("browser action url=%s script=%s", bool(act.url), bool(act.script))  # address not logged
                    store.add_turn("user", text)
                    store.add_turn("assistant", say)
                    await sock.send_json({"type": "reply", "text": say, "expression": act.expression,
                                          "intensity": 0.3 if act.expression != "neutral" else 0.0})
                    await sock.send_json({"type": "state", "state": "idle"})
                    continue
                fact = parse_remember(text)
                if fact:
                    m = store.save(Memory(content=fact, source="owner:chat"))
                    await sock.send_json({"type": "memory_saved", "memory": m.model_dump()})
                await sock.send_json({"type": "state", "state": "thinking"})
                messages = build_messages(text)
                try:
                    # their name only now and then: allowed if none of the last 5 replies used it
                    recent = [t["content"] for t in store.recent_turns(10) if t["role"] == "assistant"][-5:]
                    name = cfg.loved_one_name.strip()
                    parser = StreamParser(allow_name=not name or not any(name.lower() in r.lower() for r in recent),
                                          name=name)
                    t_start, t_first = time.perf_counter(), None
                    async for piece in backend.chat_stream(messages):
                        t_first = t_first or time.perf_counter()
                        delta = parser.feed(piece)
                        if delta:  # words go to the window (and the voice) as they are written
                            await sock.send_json({"type": "delta", "text": delta, "expression": parser.expression})
                    reply = parser.finish()
                    if parser.leftover.strip():
                        await sock.send_json({"type": "delta", "text": parser.leftover, "expression": parser.expression})
                except Exception as e:  # keep the UI alive if the backend fails
                    log.warning("llm error: %s", type(e).__name__)
                    await sock.send_json({"type": "error", "message": "My thinking engine isn't responding. Is Ollama running?"})
                    await sock.send_json({"type": "state", "state": "idle"})
                    continue
                store.add_turn("user", text)
                store.add_turn("assistant", reply.text)
                last_turn["at"] = time.time()
                st = getattr(backend, "last_stats", {})
                log.info("turn ok expression=%s first_token_ms=%d prompt_tokens=%s prompt_ms=%s reply_tokens=%s",
                         reply.expression, round(((t_first or time.perf_counter()) - t_start) * 1000),
                         st.get("prompt_tokens"), st.get("prompt_ms"), st.get("reply_tokens"))   # content never logged
                await sock.send_json({"type": "reply", **reply.model_dump()})
                await sock.send_json({"type": "state", "state": "idle"})
        except WebSocketDisconnect:
            pass
        finally:
            if sock in windows:
                windows.remove(sock)

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
