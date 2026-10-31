"""Background jobs still get JSON from a model without Ollama's structured output (qwen3.5 on the MLX engine)."""
import json

import httpx

from core.llm import chat_post


class Resp:
    def __init__(self, code, data=None, text=""):
        self.status_code, self._d, self.text = code, data, text or json.dumps(data or {})

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("x", request=None, response=None)

    def json(self):
        return self._d


def test_retries_without_format_and_extracts_json(monkeypatch):
    sent = []

    def post(url, timeout, json):
        sent.append(json)
        if "format" in json:
            return Resp(500, text='{"error":"structured output is unavailable"}')
        return Resp(200, {"message": {"content": "```json\n{\"contradicts\": false, \"why\": \"ok\"}\n```"}})
    monkeypatch.setattr(httpx, "post", post)
    r = chat_post("http://x", {"model": "m", "format": {"type": "object"}, "messages": [{"role": "user", "content": "Q"}]}, 5)
    assert json.loads(r.json()["message"]["content"]) == {"contradicts": False, "why": "ok"}
    assert "format" not in sent[1] and "JSON only" in sent[1]["messages"][-1]["content"]
    assert sent[0]["messages"][-1]["content"] == "Q"                     # the caller's request isn't changed


def test_models_with_structured_output_are_asked_once(monkeypatch):
    sent = []
    monkeypatch.setattr(httpx, "post", lambda url, timeout, json: sent.append(json) or Resp(200, {"message": {"content": "{}"}}))
    chat_post("http://x", {"model": "m", "format": "json", "messages": [{"role": "user", "content": "Q"}]}, 5)
    assert len(sent) == 1
