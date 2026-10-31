"""Replaceable LLM backends behind one interface."""
from __future__ import annotations

import json
from typing import AsyncIterator, Protocol

import httpx

from core.config import LLMConfig
from core.schema import RESPONSE_JSON_SCHEMA, EchoReply, parse_reply

Message = dict[str, str]  # {"role": "system"|"user"|"assistant", "content": str}


class LLMBackend(Protocol):
    name: str

    async def chat(self, messages: list[Message]) -> EchoReply: ...
    def chat_stream(self, messages: list[Message]) -> AsyncIterator[str]: ...
    async def health(self) -> dict: ...


class EchoBackend:
    """Offline stand-in used by tests and when no model is available."""

    name = "echo"

    async def chat(self, messages: list[Message]) -> EchoReply:
        last = next((m["content"] for m in reversed(messages) if m["role"] == "user"), "")
        return EchoReply(text=f"(echo) {last}", expression="attentive", intensity=0.2)

    async def chat_stream(self, messages: list[Message]) -> AsyncIterator[str]:
        r = await self.chat(messages)
        yield f"[{r.expression}] "
        for w in r.text.split(" "):
            yield w + " "

    async def health(self) -> dict:
        return {"ok": True, "backend": self.name}


class OllamaBackend:
    name = "ollama"

    def __init__(self, cfg: LLMConfig):
        self.cfg = cfg
        self.last_stats: dict = {}   # timings from the last streamed reply (counts/durations only, no text)
        self.client = httpx.AsyncClient(base_url=cfg.host, timeout=cfg.timeout_s)

    def _body(self, messages: list[Message], **extra) -> dict:
        body = {"model": self.cfg.model, "messages": messages, "keep_alive": self.cfg.keep_alive,
                "options": {"temperature": self.cfg.temperature, **self.cfg.options}} | extra
        if self.cfg.think is not None:
            body["think"] = self.cfg.think
        return body

    async def chat(self, messages: list[Message]) -> EchoReply:
        r = await self.client.post("/api/chat", json=self._body(messages, stream=False, format=RESPONSE_JSON_SCHEMA))
        r.raise_for_status()
        return parse_reply(r.json()["message"]["content"])

    async def chat_stream(self, messages: list[Message]) -> AsyncIterator[str]:
        """Plain-text streaming (no JSON mode), so speech can start on the first words."""
        thinking = 0   # characters of thinking (thinking models think before the first spoken word)
        async with self.client.stream("POST", "/api/chat", json=self._body(messages, stream=True)) as r:
            r.raise_for_status()
            async for line in r.aiter_lines():
                if not line:
                    continue
                piece = json.loads(line)
                thinking += len(piece.get("message", {}).get("thinking") or "")
                if piece.get("message", {}).get("content"):
                    yield piece["message"]["content"]
                if piece.get("done"):
                    ns = 1e6
                    self.last_stats = {"prompt_tokens": piece.get("prompt_eval_count", 0),
                                       "prompt_ms": round(piece.get("prompt_eval_duration", 0) / ns),
                                       "reply_tokens": piece.get("eval_count", 0),
                                       "reply_ms": round(piece.get("eval_duration", 0) / ns),
                                       "load_ms": round(piece.get("load_duration", 0) / ns), "thinking_chars": thinking}
                    break

    async def chat_stream_tools(self, messages: list[dict], tools: list[dict] | None) -> AsyncIterator[dict]:
        """Streaming with Ollama's native tool calling (core/tools.py). Yields {"content": str} pieces and, when the model
        wants tools, {"tool_calls": [...]}. Timings land in last_stats like chat_stream."""
        body = self._body(messages, stream=True)
        if tools:
            body["tools"] = tools
        thinking = 0
        async with self.client.stream("POST", "/api/chat", json=body) as r:
            r.raise_for_status()
            async for line in r.aiter_lines():
                if not line:
                    continue
                piece = json.loads(line)
                msg = piece.get("message", {})
                thinking += len(msg.get("thinking") or "")
                if msg.get("tool_calls"):
                    yield {"tool_calls": msg["tool_calls"]}
                if msg.get("content"):
                    yield {"content": msg["content"]}
                if piece.get("done"):
                    ns = 1e6
                    prev = self.last_stats if self.last_stats.get("rounds") else {}
                    self.last_stats = {"prompt_tokens": piece.get("prompt_eval_count", 0),
                                       "prompt_ms": round(piece.get("prompt_eval_duration", 0) / ns) + prev.get("prompt_ms", 0),
                                       "reply_tokens": piece.get("eval_count", 0) + prev.get("reply_tokens", 0),
                                       "reply_ms": round(piece.get("eval_duration", 0) / ns) + prev.get("reply_ms", 0),
                                       "load_ms": round(piece.get("load_duration", 0) / ns), "thinking_chars": thinking,
                                       "rounds": prev.get("rounds", 0) + 1}
                    break

    async def warm(self, messages: list[Message]) -> None:
        """Read the prompt once (one token of output) so the first real reply can reuse it."""
        body = self._body(messages, stream=False)
        body["options"] = {**body["options"], "num_predict": 1}
        body["think"] = False if self.cfg.think is not None else None   # reading the prompt is enough
        await self.client.post("/api/chat", json={k: v for k, v in body.items() if v is not None})

    async def health(self) -> dict:
        try:
            r = await self.client.get("/api/tags", timeout=3)
            names = [m["name"] for m in r.json().get("models", [])]
            return {"ok": self.cfg.model in names, "backend": self.name,
                    "model": self.cfg.model, "installed": names}
        except httpx.HTTPError as e:
            return {"ok": False, "backend": self.name, "error": type(e).__name__}


def make_backend(cfg: LLMConfig) -> LLMBackend:
    if cfg.backend == "ollama":
        return OllamaBackend(cfg)
    if cfg.backend == "echo":
        return EchoBackend()
    raise ValueError(f"unknown LLM backend: {cfg.backend}")


class _Answer:
    """Just enough of an httpx.Response for the callers of chat_post."""

    def __init__(self, data: dict):
        self._data = data

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self._data


def _json_part(text: str) -> str:
    """The JSON in a reply that may have code fences or words around it."""
    a, b = text.find("{"), text.rfind("}")
    return text[a:b + 1] if a != -1 and b > a else text


def chat_post(host: str, body: dict, timeout: float):
    """POST /api/chat for background jobs. Some models (qwen3.5 on Ollama's MLX engine) can't do Ollama's structured
    output ("format"); then the same request is sent without it, asking for JSON in words, and the JSON part of the
    reply is returned in its place. Same shape as httpx.post(...): call .raise_for_status(), then .json()."""
    r = httpx.post(f"{host}/api/chat", timeout=timeout, json=body)
    if getattr(r, "status_code", 200) < 400 or "format" not in body or "structured output" not in r.text:
        return r
    plain = {k: v for k, v in body.items() if k != "format"}
    msgs = [dict(m) for m in body["messages"]]
    schema = body["format"] if isinstance(body["format"], dict) else None
    msgs[-1]["content"] += ("\n\nAnswer with JSON only, nothing else." +
                            (f" It must match this JSON schema: {json.dumps(schema)}" if schema else ""))
    plain["messages"] = msgs
    r = httpx.post(f"{host}/api/chat", timeout=timeout, json=plain)
    r.raise_for_status()
    data = r.json()
    data["message"]["content"] = _json_part(data["message"].get("content", ""))
    return _Answer(data)
