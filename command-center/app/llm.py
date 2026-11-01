"""Local AI client. Talks to any OpenAI-compatible server (Ollama, LM Studio, llama.cpp...)."""
from __future__ import annotations

import json
import re
from typing import Any

import httpx

from .config import get_config


class LLMUnavailable(RuntimeError):
    pass


def _settings() -> dict[str, Any]:
    cfg = get_config()
    return {
        "enabled": cfg.get("ai", "enabled", True),
        "base_url": (cfg.get("ai", "base_url", "http://localhost:11434/v1") or "").rstrip("/"),
        "model": cfg.get("ai", "model", "llama3.1:8b"),
        "api_key": cfg.get("ai", "api_key", "") or "local",
        "timeout": float(cfg.get("ai", "timeout_seconds", 120)),
    }


def status() -> dict[str, Any]:
    s = _settings()
    if not s["enabled"]:
        return {"ok": False, "reason": "disabled in config", "model": s["model"]}
    try:
        r = httpx.get(f"{s['base_url']}/models", headers={"Authorization": f"Bearer {s['api_key']}"}, timeout=3)
        r.raise_for_status()
        models = [m.get("id") for m in r.json().get("data", [])]
        return {"ok": True, "model": s["model"], "models": models, "model_installed": s["model"] in models or not models}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "reason": f"can't reach {s['base_url']} ({type(e).__name__})", "model": s["model"]}


def chat(messages: list[dict[str, str]], *, temperature: float = 0.2, max_tokens: int = 1200,
         json_mode: bool = False) -> str:
    s = _settings()
    if not s["enabled"]:
        raise LLMUnavailable("AI disabled in config")
    body: dict[str, Any] = {
        "model": s["model"],
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": False,
    }
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    try:
        r = httpx.post(
            f"{s['base_url']}/chat/completions",
            json=body,
            headers={"Authorization": f"Bearer {s['api_key']}"},
            timeout=s["timeout"],
        )
        r.raise_for_status()
    except httpx.HTTPError as e:
        raise LLMUnavailable(f"local AI request failed: {e}") from e
    return r.json()["choices"][0]["message"]["content"]


def extract_json(text: str) -> Any:
    """Pull the first JSON object/array out of a model reply."""
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*(.+?)```", text, re.S)
    if fence:
        text = fence.group(1).strip()
    for opener, closer in (("{", "}"), ("[", "]")):
        start = text.find(opener)
        end = text.rfind(closer)
        if start != -1 and end > start:
            try:
                return json.loads(text[start:end + 1])
            except json.JSONDecodeError:
                continue
    raise ValueError("no JSON in model output")
