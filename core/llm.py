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

    async def chat(self, messages: list[Message]) -> EchoReply:
        r = await self.client.post("/api/chat", json={
            "model": self.cfg.model,
            "messages": messages,
            "stream": False,
            "format": RESPONSE_JSON_SCHEMA,
            "options": {"temperature": self.cfg.temperature}, "keep_alive": self.cfg.keep_alive,
        })
        r.raise_for_status()
        return parse_reply(r.json()["message"]["content"])

    async def chat_stream(self, messages: list[Message]) -> AsyncIterator[str]:
        """Plain-text streaming (no JSON mode), so speech can start on the first words."""
        async with self.client.stream("POST", "/api/chat", json={
            "model": self.cfg.model, "messages": messages, "stream": True,
            "options": {"temperature": self.cfg.temperature}, "keep_alive": self.cfg.keep_alive,
        }) as r:
            r.raise_for_status()
            async for line in r.aiter_lines():
                if not line:
                    continue
                piece = json.loads(line)
                if piece.get("message", {}).get("content"):
                    yield piece["message"]["content"]
                if piece.get("done"):
                    ns = 1e6
                    self.last_stats = {"prompt_tokens": piece.get("prompt_eval_count", 0),
                                       "prompt_ms": round(piece.get("prompt_eval_duration", 0) / ns),
                                       "reply_tokens": piece.get("eval_count", 0),
                                       "reply_ms": round(piece.get("eval_duration", 0) / ns),
                                       "load_ms": round(piece.get("load_duration", 0) / ns)}
                    break

    async def warm(self, messages: list[Message]) -> None:
        """Read the prompt once (one token of output) so the first real reply can reuse it."""
        await self.client.post("/api/chat", json={"model": self.cfg.model, "messages": messages, "stream": False,
                                                  "options": {"num_predict": 1}, "keep_alive": self.cfg.keep_alive})

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
