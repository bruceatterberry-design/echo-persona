"""Policy: what may be sent where. The rules live in code on the coordinator and on every node, not in a prompt.

classification  private : stays on the owner's own computers, always
                shared  : may go to a computer owned by someone else, after secrets are removed
"""
import re

CLASSES = ("private", "shared")
MAX_PROMPT_CHARS = 20000

# things that must never travel to someone else's computer. Replaced, not guessed around.
SECRET_PATTERNS = [
    ("private key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|$)", re.S)),
    ("access key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("GitHub token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b")),
    ("API key", re.compile(r"\b(?:sk|pk|rk)-[A-Za-z0-9_\-]{16,}\b")),
    ("bearer token", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]{16,}")),
    ("password or secret", re.compile(r"(?i)\b(?:password|passwd|secret|api[_-]?key|token)\s*[:=]\s*\S{4,}")),
    ("Slack token", re.compile(r"\bxox[abprs]-[A-Za-z0-9\-]{10,}\b")),
]
# Personal details that must not travel to someone else's computer either (11-17 review: only credentials were removed before).
# Generic shapes live here; the NAMES of the family, pets and town live in the owner's own file (private-terms.json in the Mesh data folder), never in the repo.
PERSONAL_PATTERNS = [
    ("email address", re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b")),
    ("phone number", re.compile(r"(?<![\w.])(?:\+?1[\s.\-]?)?(?:\(\d{3}\)\s?|\d{3}[\s.\-])\d{3}[\s.\-]\d{4}(?!\d)")),
    ("social security number", re.compile(r"\b\d{3}-\d{2}-\d{4}\b")),
    ("street address", re.compile(r"\b\d{1,6}\s+(?:[A-Za-z0-9.']+\s+){1,3}(?:Street|St|Avenue|Ave|Road|Rd|Drive|Dr|Lane|Ln|Court|Ct|Boulevard|Blvd|Way|Place|Pl|Circle|Cir|Highway|Hwy)\b\.?", re.I)),
    ("home network address", re.compile(r"\b(?:10\.\d{1,3}|192\.168|172\.(?:1[6-9]|2\d|3[01])|100\.(?:6[4-9]|[7-9]\d|1[01]\d|12[0-7]))\.\d{1,3}\.\d{1,3}\b|\b[\w\-]+\.ts\.net\b|\b(?:[0-9A-Fa-f]{2}[:\-]){5}[0-9A-Fa-f]{2}\b")),
]
# A teacher's school work (students, IEPs, classroom documents) stays home: a task that mentions it is refused outright, never cleaned. Names live in private-terms.json, not here.
SCHOOL_WORK = ("school work", re.compile(
    r"\b(?:(?:her|his|my|our)\s+(?:\w+\s+){0,2}(?:students?|class(?:room)?|pupils?)|IEPs?|IEP\s+goals?|Infinite\s+Campus|FERPA|MSD|teacher\s+portal|"
    r"parent\s+(?:letters?|conferences?)|progress\s+monitoring|visual\s+schedules?|school\s+documents?|special\s+education)\b", re.I))
HARD_BLOCK = {"private key", "school work"}       # a task containing one of these is refused outright, not just cleaned

_terms = {"path": None, "mtime": None, "rx": None}


def set_terms(path):
    """Where this owner's private words live (names of family and pets, the town...): {"terms": ["..."]} or a plain list. Re-read when the file changes."""
    _terms.update(path=path, mtime=None, rx=None)


def _terms_rx():
    import json, os
    p = _terms["path"]
    try:
        m = os.stat(p).st_mtime if p else None
    except OSError:
        m = None
    if m != _terms["mtime"]:
        _terms["mtime"], _terms["rx"] = m, None
        if m is not None:
            try:
                with open(p) as fh:
                    d = json.load(fh)
                words = sorted({str(w).strip() for w in (d.get("terms", []) if isinstance(d, dict) else d) if len(str(w).strip()) >= 2}, key=len, reverse=True)
                if words:
                    _terms["rx"] = re.compile(r"(?<![\w])(?:" + "|".join(re.escape(w) for w in words) + r")(?![\w])", re.I)
            except (OSError, ValueError):
                _terms["rx"] = None
    return _terms["rx"]


def redact(text):
    """-> (clean_text, found) where found is a list of kinds that were removed."""
    found = []
    for kind, rx in [SCHOOL_WORK] + SECRET_PATTERNS + PERSONAL_PATTERNS:
        if rx.search(text or ""):
            found.append(kind)
            text = rx.sub(f"[{kind} removed]", text)
    trx = _terms_rx()
    if trx and trx.search(text or ""):
        found.append("private name")
        text = trx.sub("[private name removed]", text)
    return text, found


def check_dispatch(classification, prompt, to_outside):
    """Decide whether a task's text may go out. -> (ok, text_to_send, reason, redacted_kinds)."""
    if classification not in CLASSES:
        return False, prompt, "unknown classification", []
    if len(prompt or "") > MAX_PROMPT_CHARS:
        return False, prompt, f"too long to send ({len(prompt)} characters; the limit is {MAX_PROMPT_CHARS})", []
    if not to_outside:
        return True, prompt, "", []
    if classification == "private":
        return False, prompt, "private tasks never go to someone else's computer", []
    clean, found = redact(prompt)
    if any(k in HARD_BLOCK for k in found):
        return False, prompt, ("it mentions school work, which stays home" if "school work" in found else "it contains a private key, so it will not be sent anywhere outside"), found
    return True, clean, "", found


def node_allows(policy, job):
    """The node's own owner rules (policy.json): which models it will run and how long a prompt it will accept. -> (ok, reason)."""
    p = (job or {}).get("payload") or {}
    allowed = policy.get("allowed_models")
    if allowed and p.get("model") not in allowed:
        return False, f"this computer's owner has not allowed the model '{p.get('model')}'"
    limit = int(policy.get("max_prompt_chars", MAX_PROMPT_CHARS))
    if len(str(p.get("prompt", ""))) > limit:
        return False, "the request is longer than this computer's owner allows"
    return True, ""
