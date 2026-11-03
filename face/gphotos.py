"""Google Photos → Face Library, through Google's Photos Picker (the only way left to read Google Photos since March 2025).

the owner picks the photos himself in Google's own picker window; Echo can only see the photos he picked, only once
(read-only), and never the rest of the library. Picked photos are downloaded into the face staging folder and scanned;
every face found starts as "Needs review" and joins the library only when the owner presses "This is me".

Uses the same Google client as the loved one's Drive (config/google-client.json, see docs/GOOGLE_SETUP.md) with its own
sign-in and tokens (data/face-google/<email>.json, readable only by the owner's Mac account).
"""
from __future__ import annotations

import re
import threading
import time
from pathlib import Path

import httpx

from face.google_auth import Drive

SCOPES = "openid email https://www.googleapis.com/auth/photospicker.mediaitems.readonly"
API = "https://photospicker.googleapis.com/v1"
EXT = {"image/jpeg": ".jpg", "image/png": ".png", "image/heic": ".heic", "image/heif": ".heif", "image/webp": ".webp",
       "image/gif": ".gif", "image/tiff": ".tif", "image/bmp": ".bmp"}
AGES = {"child", "teens", "20s", "30s", "40s", "50s", "unknown"}
MAX_ITEMS = 500                 # per pick; pick again for more
MAX_BYTES = 100 * 1024 ** 2     # per photo


class GooglePhotos(Drive):
    def __init__(self, client_path: Path, token_dir: Path, redirect: str, faces):
        super().__init__(client_path, token_dir, redirect)
        self.scopes = SCOPES
        self.faces = faces      # FaceLibrary
        self.picks: dict[str, dict] = {}
        self.lock = threading.Lock()

    def start(self, email: str, group: str, age: str = "unknown") -> dict:
        """Open a picker session. the owner picks in Google's window (picker_uri); the page then polls status()."""
        if email not in self.accounts():
            raise RuntimeError("connect this Google account first")
        if group not in ("current", "younger") or (group == "younger" and age not in AGES):
            raise ValueError("bad group or age")
        return self.start_to(email, self.dest(group, age), self.faces.scan)

    def start_to(self, email: str, dest: Path, scan) -> dict:
        """Same, for any staging folder (photo categories); `scan` runs after the download."""
        if email not in self.accounts():
            raise RuntimeError("connect this Google account first")
        r = httpx.post(f"{API}/sessions", headers=self._h(email), timeout=30,
                       json={"pickingConfig": {"maxItemCount": str(MAX_ITEMS)}})
        r.raise_for_status()
        s = r.json()
        with self.lock:
            self.picks[s["id"]] = {"email": email, "dest": dest, "scan": scan, "state": "waiting", "started": time.time(),
                                   "done": 0, "total": 0, "skipped": 0, "result": None, "error": None}
        return {"id": s["id"], "picker_uri": s["pickerUri"].rstrip("/") + "/autoclose"}

    def status(self, sid: str) -> dict:
        with self.lock:
            p = self.picks.get(sid)
            if not p:
                raise KeyError(sid)
            if p["state"] != "waiting":
                return self._public(p)
        r = httpx.get(f"{API}/sessions/{sid}", headers=self._h(p["email"]), timeout=30)
        if r.status_code == 404 or time.time() - p["started"] > 3600:
            p["state"], p["error"] = "error", "the picker closed before photos were chosen: try again"
        else:
            r.raise_for_status()
            if r.json().get("mediaItemsSet"):
                p["state"] = "importing"
                threading.Thread(target=self._import, args=(sid, p), daemon=True).start()
        return self._public(p)

    @staticmethod
    def _public(p: dict) -> dict:
        return {k: p[k] for k in ("state", "done", "total", "skipped", "result", "error")}

    def dest(self, group: str, age: str) -> Path:
        base = self.faces.staging / ("photos-current" if group == "current" else f"photos-younger/{age}")
        return base / "google"

    def _import(self, sid: str, p: dict) -> None:
        h = self._h(p["email"])
        try:
            items, token = [], None
            while True:
                r = httpx.get(f"{API}/mediaItems", headers=h, timeout=30,
                              params={"sessionId": sid, "pageSize": 100} | ({"pageToken": token} if token else {}))
                r.raise_for_status()
                j = r.json()
                items += j.get("mediaItems", [])
                if not (token := j.get("nextPageToken")):
                    break
            p["total"] = len(items)
            out = p["dest"]
            out.mkdir(parents=True, exist_ok=True)
            for it in items:
                mf = it.get("mediaFile", {})
                ext = EXT.get(mf.get("mimeType") or it.get("mimeType", ""))
                if it.get("type") == "VIDEO" or not ext or not mf.get("baseUrl"):
                    p["skipped"] += 1      # videos and RAW files aren't used for faces
                    continue
                stem = re.sub(r"[^\w\-]", "_", Path(mf.get("filename") or "photo").stem)[:60]
                target = out / f"{stem}-{re.sub(r'[^A-Za-z0-9]', '', it['id'])[-10:]}{ext}"
                if not target.exists():
                    with httpx.stream("GET", mf["baseUrl"] + "=d", headers=self._h(p["email"]), timeout=120,
                                      follow_redirects=True) as resp:
                        resp.raise_for_status()
                        tmp, n = target.with_name("." + target.name + ".part"), 0
                        with open(tmp, "wb") as f:
                            for chunk in resp.iter_bytes():
                                n += len(chunk)
                                if n > MAX_BYTES:
                                    raise RuntimeError(f"{target.name} is too large")
                                f.write(chunk)
                        tmp.replace(target)
                p["done"] += 1
            p["result"] = p["scan"]()
            p["state"] = "done"
        except Exception as e:  # shown on the page; the photos already downloaded stay for the next scan
            p["state"], p["error"] = "error", f"import stopped: {type(e).__name__}: {e}"[:300]
        finally:
            try:
                httpx.delete(f"{API}/sessions/{sid}", headers=h, timeout=30)
            except httpx.HTTPError:
                pass
