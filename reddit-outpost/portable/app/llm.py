"""LLM access — the standalone replacement for Letaido's `src.llm`.

Letaido routes every call through a local proxy that injects the key and bills
per app. Standing alone, you supply a key and pick a provider. Anything with an
OpenAI-compatible `/chat/completions` and JSON mode works: OpenAI, OpenRouter,
Groq, Together, a local vLLM, Ollama.

The app asks for `response_format={"type":"json_object"}` on every call. If your
model doesn't support it, the fence-tolerant parser in `engine.py` still copes
with fenced or bare-array replies — but quality drops, so prefer a model that
does.
"""
from __future__ import annotations

import os

from openai import OpenAI

# OpenRouter by default — one key reaches every major model.
API_BASE = os.environ.get("LLM_API_BASE", "https://openrouter.ai/api/v1")
API_KEY = os.environ.get("LLM_API_KEY", "")

# Needs to be cheap: a daily scan scores every new post in every subreddit, so
# this runs thousands of times a month. A small fast model is the right call —
# the prompts do the heavy lifting.
MODEL = os.environ.get("LLM_MODEL", "google/gemini-2.5-flash")

if not API_KEY:
    print("[outpost] WARNING: LLM_API_KEY is unset — scoring and drafting will "
          "fail. Set it in .env")

client = OpenAI(base_url=API_BASE, api_key=API_KEY or "unset")


def chat_json(messages: list[dict], *, temperature: float = 0.2,
              max_tokens: int = 2000) -> str:
    """One JSON-mode completion. Returns the raw string; callers parse it."""
    r = client.chat.completions.create(
        model=MODEL, messages=messages, temperature=temperature,
        max_tokens=max_tokens, response_format={"type": "json_object"})
    return r.choices[0].message.content or "{}"
