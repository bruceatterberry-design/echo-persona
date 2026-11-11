"""Echo Link web apps.

public_app: the phones' door (port 8771, TLS with the pinned certificate, home networks + Tailscale). Only hello,
pairing and sign-in work without a session; everything else needs a signed-in, still-linked phone.
admin_app: the Mac-only page (127.0.0.1:8772, never shared through Tailscale): show a QR code, approve a phone and pick
the owner or the loved one, revoke, history. Browser pages on other sites can't drive it: POSTs need a custom header (blocked
cross-site by CORS) and the Host must be this Mac.
"""
from __future__ import annotations

import asyncio
import hashlib
import io
import json
import logging
import re
import secrets
import shutil
import subprocess
import time
from pathlib import Path

import httpx
import segno
import websockets
from fastapi import FastAPI, Header, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse

from documents.store import Documents
from echolink.link import ADMIN_PORT, Link, LinkError
from echolink.callagent import CallAgent
from echolink.macphone import MacPhone, PhoneError, dialable

MAX_BODY = 64 * 1024
MAX_AUDIO = 25 * 1024 * 1024
MAX_CHUNK = 4 * 1024 * 1024
MAX_UPLOAD = 200 * 1024 * 1024
UPLOAD_KEEP_S = 7 * 86400          # unfinished uploads are dropped after a week
CORE = "http://127.0.0.1:8765"
CORE_WS = "ws://127.0.0.1:8765/ws"
FACES = ("neutral", "smiling", "thoughtful", "concerned", "attentive", "joy", "surprised", "sad", "angry", "bewildered")
TO_PHONE_DROP = {"stand_down", "hear", "history"}      # Mac-window matters; phones never get the shared history
log = logging.getLogger("echolink")


def _guard(app: FastAPI) -> None:
    @app.exception_handler(LinkError)
    async def _link_error(_req, e: LinkError):
        return JSONResponse({"error": e.msg}, status_code=e.status)

    @app.middleware("http")
    async def _limit(req: Request, nxt):
        limit = MAX_AUDIO if req.url.path == "/v1/stt" else MAX_CHUNK + 1024 if req.url.path.startswith(
            "/v1/uploads/") else MAX_BODY
        if int(req.headers.get("content-length") or 0) > limit:
            return JSONResponse({"error": "too large"}, status_code=413)
        return await nxt(req)


def from_phone(raw: str) -> str | None:
    """What a phone may send Echo: something said or typed. The person is NOT taken from the phone (Core uses the one
    the Mac fixed when the phone was linked), and window-only controls are dropped."""
    try:
        m = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(m, dict) or m.get("type") or not isinstance(m.get("text"), str) or not m["text"].strip():
        return None
    out = {"text": m["text"][:4000]}
    if m.get("how") in ("voice", "typed"):
        out["how"] = m["how"]
    if isinstance(m.get("doc"), str) and re.fullmatch(r"[a-f0-9]{12}", m["doc"]):
        out["doc"] = m["doc"]                        # "ask Echo about this document" (Core checks it is theirs)
    g = m.get("group")
    if isinstance(g, dict):                          # "Echo joins in": Core cleans it again and keeps nothing
        out["group"] = {"with": str(g.get("with", ""))[:40], "asker": "guest" if g.get("asker") == "guest" else "owner",
                        "lines": [{"who": x.get("who"), "text": str(x.get("text", ""))[:400]}
                                  for x in (g.get("lines") or [])[-12:] if isinstance(x, dict)]}
    t = m.get("task")
    if isinstance(t, dict) and t.get("kind") in ("call_prep", "after_call", "text_draft"):
        out["task"] = {"kind": t["kind"], "who": str(t.get("who", ""))[:40]}
    imgs = m.get("images")
    if isinstance(imgs, list) and imgs and all(isinstance(b, str) and len(b) <= 2_000_000 for b in imgs[:3]):
        out["images"] = imgs[:3]                     # Show Echo: Core checks them again and never stores them
        out["camera"] = "front" if m.get("camera") == "front" else "back"
    return json.dumps(out)


def to_phone(raw: str) -> str | None:
    try:
        m = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None
    return None if isinstance(m, dict) and m.get("type") in TO_PHONE_DROP else raw


class Uploads:
    """Chunked, resumable uploads (data/echolink/uploads/<id>/): the device sends pieces at the offset the Mac reports,
    so a dropped connection or an app restart continues where it stopped; the whole file's sha256 is checked."""

    def __init__(self, root: Path):
        self.root = root
        root.mkdir(parents=True, exist_ok=True, mode=0o700)

    def _m(self, uid: str) -> dict | None:
        if not re.fullmatch(r"[A-Za-z0-9_-]{16,40}", uid):
            return None
        try:
            return json.loads((self.root / uid / "meta.json").read_text())
        except (OSError, json.JSONDecodeError):
            return None

    def _w(self, m: dict) -> None:
        (self.root / m["id"] / "meta.json").write_text(json.dumps(m))

    def start(self, s: dict, body: dict) -> dict:
        size, sha = int(body.get("size") or 0), str(body.get("sha256", "")).lower()
        if not (0 < size <= MAX_UPLOAD) or not re.fullmatch(r"[0-9a-f]{64}", sha):
            raise HTTPException(400, "size (up to 200 MB) and sha256 needed")
        if body.get("kind") != "scan":
            raise HTTPException(400, "unknown kind")
        now = time.time()
        for d in list(self.root.iterdir()):                 # resume the same file from the same device; tidy old ones
            m = self._m(d.name)
            if m and now - m["at"] > UPLOAD_KEEP_S:
                shutil.rmtree(d, ignore_errors=True)
            elif m and m["device_id"] == s["device_id"] and m["sha256"] == sha and m["size"] == size:
                return {"upload_id": m["id"], "offset": (d / "data.part").stat().st_size}
        uid = secrets.token_urlsafe(18)
        (self.root / uid).mkdir(mode=0o700)
        (self.root / uid / "data.part").touch(mode=0o600)
        self._w({"id": uid, "device_id": s["device_id"], "user": s["user"], "size": size, "sha256": sha, "at": now,
                 "kind": "scan", "name": str(body.get("name", ""))[:80], "folder": str(body.get("folder", ""))[:40],
                 "tags": [str(t)[:30] for t in (body.get("tags") or [])][:10]})
        return {"upload_id": uid, "offset": 0}

    def status(self, s: dict, uid: str) -> dict:
        m = self._m(uid)
        if not m or m["device_id"] != s["device_id"]:
            raise HTTPException(404, "no such upload")
        return {"offset": (self.root / uid / "data.part").stat().st_size, "size": m["size"]}

    def put(self, s: dict, uid: str, offset: int, chunk: bytes, docs: Documents) -> dict:
        m = self._m(uid)
        if not m or m["device_id"] != s["device_id"]:
            raise HTTPException(404, "no such upload")
        part = self.root / uid / "data.part"
        have = part.stat().st_size
        if offset != have:
            return {"offset": have, "resend_from": have}        # the device continues from here
        if have + len(chunk) > m["size"]:
            raise HTTPException(400, "more than announced")
        with part.open("ab") as f:
            f.write(chunk)
        have += len(chunk)
        if have < m["size"]:
            return {"offset": have}
        h = hashlib.sha256()
        with part.open("rb") as f:
            for b in iter(lambda: f.read(1 << 20), b""):
                h.update(b)
        if h.hexdigest() != m["sha256"]:
            shutil.rmtree(self.root / uid, ignore_errors=True)
            raise HTTPException(422, "the file arrived damaged; send it again")
        with part.open("rb") as f:
            if f.read(5) != b"%PDF-":
                shutil.rmtree(self.root / uid, ignore_errors=True)
                raise HTTPException(415, "only PDF scans")
        doc = docs.add(m["user"], part, m["name"], m["folder"], m["tags"], m["sha256"])
        shutil.rmtree(self.root / uid, ignore_errors=True)
        return {"offset": have, "done": True, "doc_id": doc["id"]}


def public_app(link: Link, core: str = CORE, core_ws: str = CORE_WS, connect=None, docs: Documents | None = None,
               uploads_dir: Path | None = None) -> FastAPI:
    app = FastAPI(title="Echo Link", docs_url=None, redoc_url=None, openapi_url=None)
    _guard(app)
    connect = connect or websockets.connect
    docs = docs or Documents()
    uploads = Uploads(uploads_dir or link.data / "uploads")

    def session(authorization: str) -> dict:
        return link.session(authorization.removeprefix("Bearer ").strip())

    @app.get("/v1/hello")
    def hello():
        return link.hello()

    @app.post("/v1/pair/start")
    async def pair_start(req: Request):
        try:
            body = await req.json()
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise HTTPException(400, "send JSON") from None
        if not isinstance(body, dict):
            raise HTTPException(400, "send a JSON object")
        return link.start_pair(body, req.client.host if req.client else "")

    @app.get("/v1/pair/{pair_id}")
    def pair_status(pair_id: str):
        return link.pair_status(pair_id)

    @app.post("/v1/auth/challenge")
    async def challenge(req: Request):
        body = await req.json()
        return link.challenge(str(body.get("device_id", "")))

    @app.post("/v1/auth/session")
    async def sign_in(req: Request):
        b = await req.json()
        return link.sign_in(str(b.get("device_id", "")), str(b.get("challenge", "")), str(b.get("sig", "")))

    @app.get("/v1/me")
    def me(authorization: str = Header("")):
        s = session(authorization)
        d = next(d for d in link.devices() if d["id"] == s["device_id"])
        return {"device_id": d["id"], "user": d["user"], "device": d["name"], "mac": link.identity["name"]}

    @app.get("/v1/ping")
    def ping(authorization: str = Header("")):
        session(authorization)
        return {"ok": True}

    @app.websocket("/v1/ws")
    async def conversation(ws: WebSocket):
        """The conversation, passed to Core's /ws as a phone of the linked person. Ends if the phone is removed."""
        try:
            s = link.session(ws.headers.get("authorization", "").removeprefix("Bearer ").strip())
        except LinkError:
            await ws.close(code=4401)
            return
        await ws.accept()
        try:
            async with connect(f"{core_ws}?client=phone&user={s['user']}", max_size=8 * 1024 * 1024,
                               ping_interval=None) as up:
                async def phone_to_core():
                    while True:
                        if (out := from_phone(await ws.receive_text())) is not None:
                            link.session(ws.headers.get("authorization", "").removeprefix("Bearer ").strip())
                            await up.send(out)

                async def core_to_phone():
                    async for raw in up:
                        if (out := to_phone(raw if isinstance(raw, str) else raw.decode())) is not None:
                            await ws.send_text(out)

                tasks = [asyncio.create_task(phone_to_core()), asyncio.create_task(core_to_phone())]
                done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for t in pending:
                    t.cancel()
                for t in done:
                    if (e := t.exception()) and isinstance(e, LinkError):
                        await ws.close(code=4401)          # removed on the Mac while connected
                        return
        except (OSError, websockets.WebSocketException) as e:
            log.warning("Echo is not answering: %s", type(e).__name__)
            await ws.send_json({"type": "error", "text": "Echo isn't answering on the Mac right now. I'll keep trying."})
        except WebSocketDisconnect:
            pass
        try:
            await ws.close()
        except RuntimeError:
            pass

    @app.get("/v1/face/{expression}")
    async def face(expression: str, authorization: str = Header("")):
        """Echo's face for the middle of the ring (front pose, mouth at rest), one picture per feeling. The phone keeps
        a copy so the ring still shows him when the Mac can't be reached."""
        session(authorization)
        if expression not in FACES:
            raise HTTPException(404, "unknown expression")
        async with httpx.AsyncClient(base_url=core, timeout=10) as c:
            r = await c.get(f"/api/face/atlas/frames/C-{expression}-m0-e0.jpg")
        if r.status_code != 200:
            raise HTTPException(503, "face not available")
        return Response(r.content, media_type="image/jpeg")

    # --- uploads and documents (each person sees only their own) ---------------------------------------------------
    @app.post("/v1/uploads")
    async def upload_start(req: Request, authorization: str = Header("")):
        return uploads.start(session(authorization), await req.json())

    @app.get("/v1/uploads/{uid}")
    def upload_status(uid: str, authorization: str = Header("")):
        return uploads.status(session(authorization), uid)

    @app.put("/v1/uploads/{uid}")
    async def upload_put(uid: str, req: Request, authorization: str = Header(""), upload_offset: int = Header(...)):
        s = session(authorization)
        chunk = await req.body()
        if len(chunk) > MAX_CHUNK:
            raise HTTPException(413, "pieces of 4 MB at most")
        return await asyncio.to_thread(uploads.put, s, uid, upload_offset, chunk, docs)

    @app.get("/v1/docs")
    def docs_list(q: str = "", authorization: str = Header("")):
        return {"docs": docs.list(session(authorization)["user"], q[:200])}

    @app.get("/v1/docs/{doc_id}")
    def doc_get(doc_id: str, authorization: str = Header("")):
        user = session(authorization)["user"]
        meta = docs.get(user, doc_id)
        if not meta:
            raise HTTPException(404, "no such document")
        return {**meta, "text": docs.pages(user, doc_id)}

    @app.get("/v1/docs/{doc_id}/{which}")
    def doc_file(doc_id: str, which: str, authorization: str = Header("")):
        p = docs.file(session(authorization)["user"], doc_id, which)
        if not p:
            raise HTTPException(404, "not available")
        return Response(p.read_bytes(), media_type="image/jpeg" if which == "thumb" else "application/pdf")

    @app.patch("/v1/docs/{doc_id}")
    async def doc_edit(doc_id: str, req: Request, authorization: str = Header("")):
        b = await req.json()
        meta = docs.update(session(authorization)["user"], doc_id, b.get("name"), b.get("folder"), b.get("tags"))
        if not meta:
            raise HTTPException(404, "no such document")
        return meta

    @app.delete("/v1/docs/{doc_id}")
    def doc_delete(doc_id: str, authorization: str = Header("")):
        try:
            ok = docs.delete(session(authorization)["user"], doc_id)
        except ValueError:
            ok = False
        if not ok:
            raise HTTPException(404, "no such document")
        return {"deleted": doc_id}

    # --- notes after a call (kept on the Mac, per person; only what the owner chose to save) --------------------------
    notes_root = link.data.parent / "calls"            # data/calls/<person>/notes.jsonl

    @app.post("/v1/calls/notes")
    async def call_note_add(req: Request, authorization: str = Header("")):
        user = session(authorization)["user"]
        b = await req.json()
        text = str(b.get("text", "")).strip()[:8000]
        if not text:
            raise HTTPException(400, "empty note")
        rec = {"id": secrets.token_hex(6), "at": time.strftime("%Y-%m-%d %H:%M"), "who": str(b.get("who", ""))[:40],
               "text": text}
        d = notes_root / user
        d.mkdir(parents=True, exist_ok=True, mode=0o700)
        with (d / "notes.jsonl").open("a") as f:
            f.write(json.dumps(rec) + "\n")
        (d / "notes.jsonl").chmod(0o600)
        return rec

    @app.get("/v1/calls/notes")
    def call_notes(authorization: str = Header("")):
        try:
            lines = (notes_root / session(authorization)["user"] / "notes.jsonl").read_text().splitlines()
        except OSError:
            lines = []
        return {"notes": [json.loads(x) for x in reversed(lines[-200:]) if x.strip()]}

    # --- texts and calls through this Mac (the owner's iPhone number); each one approved on the tablet first ----------
    phone = MacPhone(notes_root)

    def bruce_only(authorization: str) -> str:
        user = session(authorization)["user"]
        if user != "owner":
            raise HTTPException(403, "Only the owner's devices can text and call from his number.")
        return user

    def phone_call(fn, *a):
        try:
            return fn(*a)
        except PhoneError as e:
            raise HTTPException(409, str(e))

    @app.get("/v1/phone/contacts")
    async def phone_contacts(authorization: str = Header("")):
        bruce_only(authorization)
        return {"contacts": await asyncio.to_thread(phone_call, phone.contacts)}

    @app.post("/v1/phone/text")
    async def phone_text(req: Request, authorization: str = Header("")):
        user = bruce_only(authorization)
        b = await req.json()
        if b.get("approved") is not True:
            raise HTTPException(400, "the owner has to approve it first.")
        return await asyncio.to_thread(phone_call, phone.text, user, str(b.get("name", ""))[:80], str(b.get("number", "")),
                                       str(b.get("text", "")))

    @app.post("/v1/phone/call")
    async def phone_dial(req: Request, authorization: str = Header("")):
        user = bruce_only(authorization)
        b = await req.json()
        if b.get("approved") is not True:
            raise HTTPException(400, "the owner has to approve it first.")
        return await asyncio.to_thread(phone_call, phone.call, user, str(b.get("name", ""))[:80], str(b.get("number", "")))

    # --- Echo makes the call himself: plan (the owner sees the exact opening) → the owner approves → live transcript ------
    agent = CallAgent(notes_root)

    @app.post("/v1/echocall/plan")
    async def echocall_plan(req: Request, authorization: str = Header("")):
        bruce_only(authorization)
        b = await req.json()
        name, number = str(b.get("name", ""))[:80].strip(), str(b.get("number", ""))
        if not name:
            raise HTTPException(400, "Who should Echo call?")
        try:
            num = dialable(number)
        except PhoneError as e:
            raise HTTPException(400, str(e))
        c = await asyncio.to_thread(agent.plan, name, num, str(b.get("request", ""))[:500], bool(b.get("business")))
        return c.public()

    @app.post("/v1/echocall/{call_id}/start")
    async def echocall_start(call_id: str, req: Request, authorization: str = Header("")):
        bruce_only(authorization)
        b = await req.json()
        if b.get("approved") is not True:
            raise HTTPException(400, "the owner has to approve it first.")
        facts = b.get("facts") if isinstance(b.get("facts"), dict) else {}
        try:
            return agent.start(call_id, facts, b.get("may_agree") if isinstance(b.get("may_agree"), str) else None).public()
        except KeyError:
            raise HTTPException(404, "no such call")
        except RuntimeError as e:
            raise HTTPException(409, str(e))

    @app.get("/v1/echocall/{call_id}")
    def echocall_status(call_id: str, authorization: str = Header("")):
        bruce_only(authorization)
        c = agent.calls.get(call_id)
        if not c:
            raise HTTPException(404, "no such call")
        return c.public()

    @app.post("/v1/echocall/{call_id}/end")
    def echocall_end(call_id: str, authorization: str = Header("")):
        bruce_only(authorization)
        agent.end(call_id)
        return {"ending": call_id}

    @app.get("/v1/phone/lookup")
    async def phone_lookup(q: str = "", authorization: str = Header("")):
        """A place that isn't in the contacts: its number from the web (shown with its source; the owner approves)."""
        bruce_only(authorization)
        q = q.strip()[:120]
        if not q:
            raise HTTPException(400, "What should Echo look up?")
        from echolink.lookup import find_number
        return await asyncio.to_thread(find_number, q)

    # --- the side window next to Echo: weather and photos (calendar is the tablet's own; web sources come from Core)
    def dataset_root() -> str:
        import yaml
        try:
            return yaml.safe_load((Path(__file__).resolve().parent.parent / "config" / "storage.yaml").read_text())[
                "dataset_root"]
        except Exception:  # noqa: BLE001
            return "/Volumes/External storage/EchoPersona-Data"

    @app.get("/v1/panel/weather")
    async def panel_weather(authorization: str = Header("")):
        session(authorization)
        from echolink import panel
        try:
            return await asyncio.to_thread(panel.weather)
        except Exception:  # noqa: BLE001
            raise HTTPException(503, "The weather service can't be reached right now.")

    @app.get("/v1/panel/photos")
    async def panel_photos(q: str = "", authorization: str = Header("")):
        session(authorization)
        from echolink import panel
        try:
            return await asyncio.to_thread(panel.photos, dataset_root(), q[:200])
        except FileNotFoundError:
            raise HTTPException(503, "The photo drive isn't connected.")

    @app.get("/v1/panel/photo/{pid}")
    def panel_photo(pid: str, authorization: str = Header("")):
        session(authorization)
        from echolink import panel
        f = panel.photo_file(dataset_root(), pid)
        if not f:
            raise HTTPException(404, "no such photo")
        return Response(f.read_bytes(), media_type="image/jpeg")

    @app.get("/v1/phone/history")
    def phone_history(authorization: str = Header("")):
        return {"items": phone.history(bruce_only(authorization))}

    @app.post("/v1/stt")
    async def stt(req: Request, authorization: str = Header("")):
        """Speech to text on the Mac (the phone records; Echo's own recogniser writes it down)."""
        session(authorization)
        body = await req.body()
        if len(body) > MAX_AUDIO:
            raise HTTPException(413, "audio too large")
        async with httpx.AsyncClient(base_url=core, timeout=60) as c:
            r = await c.post("/api/stt", content=body, params={"mode": "direct"})
        if r.status_code != 200:
            raise HTTPException(r.status_code if r.status_code < 500 else 503, "speech not understood")
        j = r.json()   # who spoke, from the voice (only "owner" when his voice profile is sure; else unknown)
        return {"text": j.get("text", ""), "speaker": j.get("speaker") or "unknown"}

    @app.post("/v1/tts")
    async def tts(req: Request, authorization: str = Header("")):
        """Echo's voice for a reply, streamed as raw 16-bit mono PCM (sample rate in X-Sample-Rate)."""
        session(authorization)
        text = str((await req.json()).get("text", ""))[:600]
        c = httpx.AsyncClient(base_url=core, timeout=httpx.Timeout(60, connect=5))
        r = await c.send(c.build_request("POST", "/api/tts/stream", json={"text": text}), stream=True)
        if r.status_code != 200:
            await r.aclose()
            await c.aclose()
            raise HTTPException(503, "Echo's voice isn't available right now")

        async def pump():
            try:
                async for chunk in r.aiter_raw():
                    yield chunk
            finally:
                await r.aclose()
                await c.aclose()
        return StreamingResponse(pump(), media_type="application/octet-stream",
                                 headers={"X-Sample-Rate": r.headers.get("X-Sample-Rate", "24000")})

    return app


def admin_app(link: Link, allowed_hosts: tuple[str, ...] = (f"127.0.0.1:{ADMIN_PORT}", f"localhost:{ADMIN_PORT}",
                                                            "testserver")) -> FastAPI:
    app = FastAPI(title="Echo Link (Mac)", docs_url=None, redoc_url=None, openapi_url=None)
    _guard(app)

    @app.middleware("http")
    async def _mac_only(req: Request, nxt):
        if req.headers.get("host") not in allowed_hosts:              # DNS-rebinding guard
            return JSONResponse({"error": "Mac only"}, status_code=403)
        if req.method != "GET" and req.headers.get("x-echo-admin") != "1":   # no cross-site form posts
            return JSONResponse({"error": "missing header"}, status_code=403)
        return await nxt(req)

    @app.get("/", response_class=HTMLResponse)
    def page():
        return PAGE

    @app.get("/admin/state")
    def state():
        ds = [{k: d.get(k) for k in ("id", "name", "user", "method", "paired", "last_seen", "revoked")}
              for d in link.devices()]
        return {"name": link.identity["name"], "pending": link.pending_list(), "devices": ds,
                "history": link.history(30)}

    @app.post("/admin/qr")
    def qr():
        q = link.new_qr()
        buf = io.BytesIO()
        segno.make(q["payload"], error="m").save(buf, kind="svg", scale=6, border=2, dark="#111", light="#fff",
                                                  xmldecl=False, svgns=True)
        return {"svg": buf.getvalue().decode(), "expires_in": 300}

    @app.post("/admin/pair/{pair_id}")
    async def decide(pair_id: str, req: Request):
        return link.decide(pair_id, str((await req.json()).get("decision", "")))

    @app.post("/admin/devices/{device_id}/revoke")
    def revoke(device_id: str):
        return link.revoke(device_id)

    @app.post("/admin/phone/selftest")
    async def phone_selftest():
        """Mac-only: switch the Phone app's sound to Echo's cables and back (asks for Accessibility the first time),
        and show the summary window, so the first real call has nothing left to set up."""
        from echolink.callagent import Call, CallAgent, PhoneApp
        out = {}
        try:
            await asyncio.to_thread(PhoneApp().route)
            await asyncio.to_thread(PhoneApp().unroute)
            out["phone_app"] = "ok"
        except Exception as e:  # noqa: BLE001
            out["phone_app"] = str(e)[:300]
        for app_name, script in (("messages", 'tell application "Messages" to count every account'),
                                 ("contacts", 'tell application "Contacts" to count people')):
            try:   # read-only: nothing is sent
                r = await asyncio.to_thread(subprocess.run, ["osascript", "-e", script], capture_output=True, text=True,
                                            timeout=60)
                out[app_name] = r.stdout.strip() if r.returncode == 0 else "ERROR: " + r.stderr.strip()[-300:]
            except Exception as e:  # noqa: BLE001
                out[app_name] = "ERROR: " + str(e)[:200]
        try:   # dry run: keypad + number, never presses Call
            out["dial_dry_run"] = await asyncio.to_thread(PhoneApp().dial, "+18154099542", True)
        except Exception as e:  # noqa: BLE001
            out["dial_dry_run"] = str(e)[:300]
        CallAgent.show_on_mac(Call("test", "", "", "", summary="Test only, no call was made. After a real call, the "
                                                               "summary appears here, titled with who Echo called."))
        out["window"] = "shown"
        return out

    return app


PAGE = """<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>Echo Link</title><style>
body{margin:0;background:#0d0b09;color:#eee6d6;font:17px -apple-system,system-ui,sans-serif}
main{max-width:760px;margin:0 auto;padding:28px 18px}h1{color:#d8b25a;font-weight:500;margin:0 0 4px}
h2{color:#d8b25a;font-weight:500;font-size:19px;margin:28px 0 8px}.muted{color:#a89f8f;font-size:15px}
button{background:#d8b25a;color:#111;border:0;border-radius:10px;padding:11px 18px;font-size:16px;cursor:pointer;margin:4px 6px 4px 0}
button.alt{background:#2a241c;color:#eee6d6}button.deny{background:#5a2a24;color:#fff}
.card{background:#1a1611;border:1px solid #3a3020;border-radius:14px;padding:14px 16px;margin:8px 0}
.code{font-size:40px;letter-spacing:6px;color:#f3d48a;font-variant-numeric:tabular-nums}
#qr svg{background:#fff;border-radius:12px;max-width:300px;width:100%;height:auto}
table{width:100%;border-collapse:collapse;font-size:15px}td{padding:6px 4px;border-bottom:1px solid #2a241c}
</style></head><body><main>
<h1>Echo Link</h1><div class="muted" id="who"></div>
<h2>Link a phone</h2>
<div class="muted">Open Echo on the phone. Scan this code, or (on the home Wi-Fi) tap “Echo” in the list.</div>
<button onclick="showQR()">Show QR code</button><div id="qr"></div><div class="muted" id="qrt"></div>
<h2>Waiting for your answer</h2><div id="pending" class="muted">Nobody is waiting.</div>
<h2>Linked phones</h2><div id="devices" class="muted">None yet.</div>
<h2>History</h2><table id="hist"></table>
</main><script>
const H={"x-echo-admin":"1","content-type":"application/json"};let qrUntil=0;
const esc=s=>String(s??"").replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
async function post(u,b){const r=await fetch(u,{method:"POST",headers:H,body:JSON.stringify(b||{})});return r.json()}
async function showQR(){const q=await post("/admin/qr");document.getElementById("qr").innerHTML=q.svg;qrUntil=Date.now()+q.expires_in*1000}
async function decide(id,d){await post("/admin/pair/"+id,{decision:d});if(d!=="deny"){document.getElementById("qr").innerHTML="";qrUntil=0}load()}
async function revoke(id,n){if(confirm("Remove "+n+"? It will be cut off now and must be linked again."))await post("/admin/devices/"+id+"/revoke");load()}
async function load(){const s=await (await fetch("/admin/state")).json();
document.getElementById("who").textContent=s.name;
const left=Math.max(0,Math.round((qrUntil-Date.now())/1000));
document.getElementById("qrt").textContent=qrUntil?(left?`Good for ${Math.floor(left/60)}:${String(left%60).padStart(2,"0")}, one phone.`:"Expired, show a new one."):"";
if(qrUntil&&!left)document.getElementById("qr").innerHTML="";
document.getElementById("pending").innerHTML=s.pending.length?s.pending.map(p=>`<div class="card">
<b>${esc(p.device)}</b> wants to link (${p.method==="qr"?"QR code":"found on the network"}).<br>
${p.method==="discover"?'<span class="muted">Only say yes if the phone shows the same number:</span><br>':'<span class="muted">Phone check number:</span><br>'}
<div class="code">${esc(p.code)}</div>
This phone is for: <button onclick="decide('${p.pair_id}','owner')">the owner</button><button onclick="decide('${p.pair_id}','loved_one')">the loved one</button>
<button class="deny" onclick="decide('${p.pair_id}','deny')">Don't link</button>
<div class="muted">${p.seconds_left} s left</div></div>`).join(""):"Nobody is waiting.";
const live=s.devices.filter(d=>!d.revoked);
document.getElementById("devices").innerHTML=live.length?live.map(d=>`<div class="card"><b>${esc(d.name)}</b>, ${d.user==="loved_one"?"the loved one":"the owner"}'s
<div class="muted">Linked ${esc(d.paired)} · last seen ${esc(d.last_seen||"not yet")}</div>
<button class="alt" onclick="revoke('${d.id}','${esc(d.name)}')">Remove</button></div>`).join(""):"None yet.";
document.getElementById("hist").innerHTML=s.history.map(h=>`<tr><td class="muted">${esc(h.at)}</td><td>${esc(h.event.replace("_"," "))}</td><td>${esc(h.device||"")}</td><td>${esc(h.user||"")}</td></tr>`).join("")}
load();setInterval(load,2000);
</script></body></html>"""
