"""Texts and calls through this Mac, which uses the owner's iPhone (Messages text forwarding + Phone app / Continuity).

Nothing here decides anything: the tablet shows exactly who and what, and the owner approves each one. This module only
does what was approved, and keeps a log (data/calls/<user>/outbox.jsonl) so every text and call can be looked back on.
Numbers and text go to osascript as arguments (never pasted into the script), so nothing in them can run as code.
"""
from __future__ import annotations

import json
import logging
import re
import subprocess
import time
from pathlib import Path

CONTACTS_JXA = r"""
const C = Application('Contacts');
const out = [];
const people = C.people();
for (const p of people) {
  let phones = [];
  try { phones = p.phones().map(x => ({number: x.value(), label: (x.label() || '').replace(/[_$!<>]/g, '')})); } catch (e) {}
  if (phones.length) out.push({name: p.name(), phones});
}
JSON.stringify(out);
"""

# Sends by SMS through the iPhone (works for every phone, Apple or not). iMessage is tried first only when the
# person already has an iMessage chat on this Mac, so Apple users still get blue bubbles.
TEXT_AS = r"""
on run argv
  set num to item 1 of argv
  set msg to item 2 of argv
  tell application "Messages"
    set smsAcct to missing value
    set imAcct to missing value
    repeat with a in every account
      try
        set t to service type of a
        if t is SMS and enabled of a then set smsAcct to a
        if t is iMessage and enabled of a then set imAcct to a
      end try
    end repeat
    if imAcct is not missing value and (item 3 of argv) is "imessage" then
      send msg to participant num of imAcct
      return "imessage"
    end if
    if smsAcct is missing value then error "Text message forwarding from the iPhone is off."
    send msg to participant num of smsAcct
    return "sms"
  end tell
end run
"""

HISTORY_AS = r"""
on run argv
  set num to item 1 of argv
  tell application "Messages"
    repeat with c in every chat
      try
        if service type of account of c is iMessage then
          repeat with p in participants of c
            if handle of p contains num then return "yes"
          end repeat
        end if
      end try
    end repeat
  end tell
  return "no"
end run
"""


class PhoneError(Exception):
    pass


def digits(number: str) -> str:
    d = re.sub(r"\D", "", number or "")
    return d[1:] if len(d) == 11 and d.startswith("1") else d


def dialable(number: str) -> str:
    """+1XXXXXXXXXX for a US 10-digit number, otherwise the digits with a leading + kept."""
    n = (number or "").strip()
    d = re.sub(r"\D", "", n)
    if not 3 <= len(d) <= 15:
        raise PhoneError("That doesn't look like a phone number.")
    if len(d) == 10:
        return "+1" + d
    if len(d) == 11 and d.startswith("1"):
        return "+" + d
    return ("+" if n.startswith("+") else "") + d


def _osa(lang: str, script: str, *args: str, timeout: float = 30) -> str:
    cmd = ["osascript"] + (["-l", "JavaScript"] if lang == "js" else []) + ["-", *args]
    try:
        r = subprocess.run(cmd, input=script, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise PhoneError("The Mac took too long to answer.")
    if r.returncode != 0:
        err = r.stderr.strip()
        logging.getLogger("echolink.phone").warning("osascript failed: %s", err[-400:])
        if "-1743" in err or "not authorized to send apple events" in err.lower():   # the real permission error
            raise PhoneError("The Mac needs permission: click Allow on the Mac (System Settings → Privacy & Security → "
                             "Automation).")
        raise PhoneError(re.sub(r"^.*execution error: ", "", err)[:200] or "It didn't work on the Mac.")
    return r.stdout.strip()


def _dial_url(url: str) -> None:
    """the owner's own call: the Phone app's keypad + Call (a tel: link only opens the number without dialing)."""
    from echolink.callagent import PhoneApp
    PhoneApp().dial(url.removeprefix("tel:"))


class MacPhone:
    def __init__(self, root: Path, runner=_osa, opener=None):
        self.root = root                       # data/calls
        self._osa = runner
        self._open = opener or _dial_url
        self._contacts: tuple[float, list] = (0.0, [])

    def contacts(self) -> list[dict]:
        """The Mac's Contacts (iCloud-synced with the iPhone when iCloud Contacts is on). Cached 5 minutes."""
        at, cached = self._contacts
        if time.time() - at < 300:
            return cached
        out = json.loads(self._osa("js", CONTACTS_JXA, timeout=60) or "[]")
        people = [{"name": str(p.get("name") or "")[:80],
                   "phones": [{"number": str(x["number"])[:40], "label": str(x.get("label") or "")[:30]}
                              for x in p.get("phones", []) if x.get("number")]}
                  for p in out if p.get("name")]
        self._contacts = (time.time(), people)
        return people

    def _log(self, user: str, rec: dict) -> dict:
        rec = {"at": time.strftime("%Y-%m-%d %H:%M:%S"), **rec}
        d = self.root / user
        d.mkdir(parents=True, exist_ok=True, mode=0o700)
        with (d / "outbox.jsonl").open("a") as f:
            f.write(json.dumps(rec) + "\n")
        (d / "outbox.jsonl").chmod(0o600)
        return rec

    def text(self, user: str, name: str, number: str, body: str) -> dict:
        body = (body or "").strip()
        if not body:
            raise PhoneError("The text is empty.")
        if len(body) > 2000:
            raise PhoneError("That text is too long.")
        num = dialable(number)
        try:
            had_imessage = self._osa("as", HISTORY_AS, digits(num), timeout=20) == "yes"
        except PhoneError:
            had_imessage = False
        try:
            how = self._osa("as", TEXT_AS, num, body, "imessage" if had_imessage else "sms")
        except PhoneError as e:
            self._log(user, {"kind": "text", "name": name, "number": num, "text": body, "ok": False, "error": str(e)})
            raise
        return self._log(user, {"kind": "text", "name": name, "number": num, "text": body, "ok": True, "how": how})

    def call(self, user: str, name: str, number: str) -> dict:
        num = dialable(number)
        try:
            self._open("tel:" + num)
        except Exception as e:
            self._log(user, {"kind": "call", "name": name, "number": num, "ok": False, "error": str(e)[:200]})
            raise PhoneError("The Mac couldn't start the call.")
        return self._log(user, {"kind": "call", "name": name, "number": num, "ok": True})

    def history(self, user: str, n: int = 50) -> list[dict]:
        try:
            lines = (self.root / user / "outbox.jsonl").read_text().splitlines()
        except OSError:
            return []
        return [json.loads(x) for x in reversed(lines[-n:]) if x.strip()]
