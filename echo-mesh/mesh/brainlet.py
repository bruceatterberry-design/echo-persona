"""The Brainlet: the small part of Echo Mesh that lives on every computer and stands between the Mesh and that computer's AI.

It does three things and nothing else:
  1. POLICY ENGINE  - decides whether a job may run here at all (job type, the owner's own rules in policy.json, read fresh every time).
  2. AI-CLIENT ADAPTERS - one small class per kind of local AI (Ollama, OpenAI-style servers such as LM Studio or llama.cpp). A new kind of AI
     (for example a partner AI, whose runtime is still unknown) is added by registering one adapter; the rest of the Mesh does not change.
  3. RESULT SHAPE - a job is text in, text out. The Brainlet never gives the Mesh the AI's memory, personality, files or training data, because it has no way to read them.

Written once here and used by the Mac side and by the Windows/Linux agent alike (docs/DASHBOARD-AND-EXE.md). Standard library only.
"""
import json, time, urllib.request

from . import policy as policy_mod
from . import cognition as cognition_mod

ALLOWED_JOBS = ("ping", "generate")
GEN_KEYS = ("model", "prompt", "think")      # "cognition" is read separately: it is a switch, not an AI option
TIMEOUT_S = 600

ADAPTERS = {}          # kind -> adapter class


def register(cls):
    """Add a kind of local AI. A class needs: kind, label, models(), generate(model, prompt, **opts) -> dict, and the classmethod probe(host)."""
    ADAPTERS[cls.kind] = cls
    return cls


def _get(url, timeout=3):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.load(r)


def _post(url, body, timeout=TIMEOUT_S):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


@register
class OllamaAdapter:
    kind, label = "ollama", "Ollama"

    def __init__(self, host):
        self.host = host.rstrip("/")

    @classmethod
    def probe(cls, host):
        try:
            return cls(host) if _get(host.rstrip("/") + "/api/version") and "models" in _get(host.rstrip("/") + "/api/tags") else None
        except Exception:
            return None

    def models(self):
        return [m["name"] for m in _get(self.host + "/api/tags").get("models", [])]

    def generate(self, model, prompt, think=None):
        body = {"model": model, "prompt": prompt, "stream": False}
        if think is not None:
            body["think"] = think
        t0 = time.time()
        d = _post(self.host + "/api/generate", body)
        return {"response": d.get("response", ""), "eval_count": d.get("eval_count"), "eval_duration_ns": d.get("eval_duration"), "seconds": round(time.time() - t0, 2)}


@register
class OpenAIAdapter:
    """Any server that answers like the OpenAI API: LM Studio, llama.cpp server, Exo, vLLM..."""
    kind, label = "openai", "An OpenAI-style AI"

    def __init__(self, host):
        self.host = host.rstrip("/")

    @classmethod
    def probe(cls, host):
        try:
            return cls(host) if "data" in _get(host.rstrip("/") + "/v1/models") else None
        except Exception:
            return None

    def models(self):
        return [m["id"] for m in _get(self.host + "/v1/models").get("data", [])]

    def generate(self, model, prompt, think=None):
        t0 = time.time()
        d = _post(self.host + "/v1/chat/completions", {"model": model, "messages": [{"role": "user", "content": prompt}], "stream": False})
        return {"response": (d.get("choices") or [{}])[0].get("message", {}).get("content", ""),
                "eval_count": (d.get("usage") or {}).get("completion_tokens"), "seconds": round(time.time() - t0, 2)}


def adapter_for(kind, host):
    cls = ADAPTERS.get(kind)
    return cls(host) if cls else None


def detect(host):
    """Which kind of AI answers at this address? Tested one kind at a time, never assumed. None when nothing we know answers."""
    for cls in ADAPTERS.values():
        a = cls.probe(host)
        if a:
            return a
    return None


class Brainlet:
    """policy_fn() returns the owner's policy dict (read fresh each job, so a change applies at once)."""

    def __init__(self, policy_fn, kind="ollama", host="http://127.0.0.1:11434", node_info=None, profile_fn=None):
        """profile_fn() returns the Echo cognition profile text (mesh/cognition.py); None means the built-in defaults."""
        self.policy_fn, self.kind, self.host, self.node_info = policy_fn, kind, host, node_info or (lambda: {})
        self.profile_fn = profile_fn or cognition_mod.load
        self.refused = 0

    def adapter(self):
        a = adapter_for(self.kind, self.host)
        if not a:
            raise ValueError(f"no adapter for an AI of kind '{self.kind}'")
        return a

    def decide(self, job):
        """(allowed, reason). The policy engine: nothing runs unless this says yes."""
        t = job.get("type")
        if t not in ALLOWED_JOBS:
            return False, f"job type '{t}' is not allowed on this node"
        if t == "generate":
            return policy_mod.node_allows(self.policy_fn(), job)
        return True, ""

    def run(self, job):
        """-> (ok, result, error). Never raises for a refused or malformed job; an AI that is down surfaces as an error text."""
        ok, why = self.decide(job)
        if not ok:
            self.refused += 1
            return False, None, why
        if job["type"] == "ping":
            return True, dict(self.node_info()), None
        p = job.get("payload", {}) or {}
        body = {k: p[k] for k in GEN_KEYS if k in p}
        if "model" not in body or "prompt" not in body:
            return False, None, "generate needs model and prompt"
        prompt = body["prompt"]
        if p.get("cognition") and self.policy_fn().get("cognition", True) is not False:      # only when the job asks, and never against the owner's switch
            try:
                prompt = cognition_mod.wrap(self.profile_fn(), prompt)
            except Exception:
                pass                                                                         # a broken profile must never stop a job
        try:
            return True, self.adapter().generate(body["model"], prompt, body.get("think")), None
        except ValueError as e:
            return False, None, str(e)
        except Exception as e:
            return False, None, f"the local AI did not answer: {type(e).__name__}"

    def status(self):
        """What the dashboard may show about this computer's AI: facts only."""
        pol = self.policy_fn()
        try:
            models = self.adapter().models()
        except Exception:
            models = []
        return {"kind": self.kind, "host": self.host, "models": models, "reachable": bool(models),
                "allowed_models": pol.get("allowed_models"), "max_prompt_chars": pol.get("max_prompt_chars", policy_mod.MAX_PROMPT_CHARS),
                "refused": self.refused}
