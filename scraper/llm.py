"""
Thin wrapper around the Anthropic SDK.

Keeps LLM calls in one place so we can swap providers later,
and makes the rest of the code testable without hitting the API.
"""

from __future__ import annotations

import json
import logging
from typing import Optional

from scraper.config import LLMConfig, get_api_key

log = logging.getLogger(__name__)

_client = None


def _get_client():
    global _client
    if _client is None:
        key = get_api_key()
        if not key:
            return None
        import anthropic
        _client = anthropic.Anthropic(api_key=key)
    return _client


def llm_available() -> bool:
    return get_api_key() is not None


def ask(prompt: str, cfg: LLMConfig, system: Optional[str] = None) -> Optional[str]:
    """
    Send a prompt to Claude and get the text response.
    Returns None if no API key is configured.
    """
    client = _get_client()
    if client is None:
        log.debug("LLM call skipped — no API key configured")
        return None

    kwargs = {
        "model": cfg.model,
        "max_tokens": cfg.max_tokens,
        "messages": [{"role": "user", "content": prompt}],
    }
    if system:
        kwargs["system"] = system

    try:
        resp = client.messages.create(**kwargs)
        return resp.content[0].text
    except Exception:
        log.exception("LLM request failed")
        return None


def ask_json(prompt: str, cfg: LLMConfig, system: Optional[str] = None) -> Optional[dict]:
    """Like ask(), but tries to parse the response as JSON."""
    raw = ask(prompt, cfg, system)
    if raw is None:
        return None
    # Strip markdown fences if the model wraps its response
    cleaned = raw.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("\n", 1)[1]
        if cleaned.endswith("```"):
            cleaned = cleaned[:-3]
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        log.warning("LLM response was not valid JSON, returning raw text")
        return None
