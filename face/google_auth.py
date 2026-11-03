"""Google sign-in for Echo Persona (used by face/gphotos.py for the Google Photos picker).

Signs a Google account in once (OAuth, your own client in config/google-client.json), keeps the token on this Mac only
(data/google/<email>.json, readable only by your Mac account) and refreshes it when needed. Echo only ever sees what you
pick yourself in Google's picker. The upload helpers below are kept for apps that save documents to Drive.
"""
from __future__ import annotations

import json
import secrets
import time
import urllib.parse
from pathlib import Path

import httpx

SCOPES = "openid email https://www.googleapis.com/auth/drive.file"
AUTH = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN = "https://oauth2.googleapis.com/token"
FOLDER = "AI Documents"
MIME = {"doc": ("application/vnd.google-apps.document", "text/html"),
        "sheet": ("application/vnd.google-apps.spreadsheet", "text/csv")}


class Drive:
    def __init__(self, client_path: Path, token_dir: Path, redirect: str):
        self.client_path, self.token_dir, self.redirect = client_path, token_dir, redirect
        self.scopes = SCOPES
        self.pending: dict[str, float] = {}

    def client(self) -> dict | None:
        try:
            c = json.loads(self.client_path.read_text())
            return c.get("installed") or c.get("web")
        except (OSError, json.JSONDecodeError):
            return None

    def accounts(self) -> list[str]:
        return sorted(p.stem for p in self.token_dir.glob("*.json")) if self.token_dir.is_dir() else []

    def sign_in_url(self, hint: str = "") -> str:
        c = self.client()
        if not c:
            raise RuntimeError("Google isn't set up yet (config/google-client.json is missing)")
        state = secrets.token_urlsafe(16)
        self.pending[state] = time.time()
        q = {"client_id": c["client_id"], "redirect_uri": self.redirect, "response_type": "code", "scope": self.scopes,
             "access_type": "offline", "prompt": "consent", "state": state}
        if hint:
            q["login_hint"] = hint
        return AUTH + "?" + urllib.parse.urlencode(q)

    def finish(self, code: str, state: str) -> str:
        if time.time() - self.pending.pop(state, 0) > 600:
            raise RuntimeError("sign-in expired: try again")
        c = self.client()
        r = httpx.post(TOKEN, timeout=30, data={"code": code, "client_id": c["client_id"], "client_secret": c["client_secret"],
                                                "redirect_uri": self.redirect, "grant_type": "authorization_code"})
        r.raise_for_status()
        tok = r.json()
        info = httpx.get("https://openidconnect.googleapis.com/v1/userinfo", timeout=30,
                         headers={"Authorization": f"Bearer {tok['access_token']}"}).json()
        email = info["email"].lower()
        tok["expires_at"] = time.time() + tok.get("expires_in", 3600) - 60
        self._save(email, tok)
        return email

    def _path(self, email: str) -> Path:
        return self.token_dir / f"{Path(email.lower()).name}.json"

    def _save(self, email: str, tok: dict) -> None:
        self.token_dir.mkdir(parents=True, exist_ok=True)
        self.token_dir.chmod(0o700)
        p = self._path(email)
        p.write_text(json.dumps(tok))
        p.chmod(0o600)

    def forget(self, email: str) -> None:
        self._path(email).unlink(missing_ok=True)

    def _token(self, email: str) -> str:
        tok = json.loads(self._path(email).read_text())
        if time.time() > tok.get("expires_at", 0):
            c = self.client()
            r = httpx.post(TOKEN, timeout=30, data={"client_id": c["client_id"], "client_secret": c["client_secret"],
                                                    "refresh_token": tok["refresh_token"], "grant_type": "refresh_token"})
            r.raise_for_status()
            tok |= r.json()
            tok["expires_at"] = time.time() + tok.get("expires_in", 3600) - 60
            self._save(email, tok)
        return tok["access_token"]

    def _h(self, email: str) -> dict:
        return {"Authorization": f"Bearer {self._token(email)}"}

    def folder(self, email: str) -> str:
        """The "AI Documents" folder Echo made in this account (made now if needed)."""
        tok = json.loads(self._path(email).read_text())
        h = self._h(email)
        if fid := tok.get("folder_id"):
            r = httpx.get(f"https://www.googleapis.com/drive/v3/files/{fid}", headers=h, params={"fields": "id,trashed"}, timeout=30)
            if r.status_code == 200 and not r.json().get("trashed"):
                return fid
        r = httpx.post("https://www.googleapis.com/drive/v3/files", headers=h, timeout=30,
                       json={"name": FOLDER, "mimeType": "application/vnd.google-apps.folder"})
        r.raise_for_status()
        tok = json.loads(self._path(email).read_text()) | {"folder_id": r.json()["id"]}
        self._save(email, tok)
        return tok["folder_id"]

    def upload(self, email: str, name: str, content: bytes, kind: str = "doc", file_id: str | None = None,
               raw_mime: str | None = None) -> dict:
        """Upload into AI Documents. kind "doc"/"sheet" converts to Google Docs/Sheets; raw_mime keeps the file as is
        (the phone app). With file_id, replaces that file's contents (same link)."""
        target, source = (raw_mime, raw_mime) if raw_mime else MIME[kind]
        meta = {"name": name} if file_id else {"name": name, "mimeType": target, "parents": [self.folder(email)]}
        boundary = "echo" + secrets.token_hex(8)
        body = (f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n{json.dumps(meta)}\r\n"
                f"--{boundary}\r\nContent-Type: {source}\r\n\r\n").encode() + content + f"\r\n--{boundary}--".encode()
        url = "https://www.googleapis.com/upload/drive/v3/files" + (f"/{file_id}" if file_id else "")
        r = httpx.request("PATCH" if file_id else "POST", url, params={"uploadType": "multipart", "fields": "id,name,webViewLink"},
                          headers=self._h(email) | {"Content-Type": f"multipart/related; boundary={boundary}"},
                          content=body, timeout=120)
        r.raise_for_status()
        return r.json()
