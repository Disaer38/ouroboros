"""Web search tool.

Primary backend: OpenRouter (perplexity/sonar-pro-search) — uses the already-configured
OPENROUTER_API_KEY, so no extra secrets are needed.

Legacy/fallback backend: OpenAI Responses API (requires OPENAI_API_KEY).
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, List

from ouroboros.tools.registry import ToolContext, ToolEntry

_DEFAULT_OR_SEARCH_MODEL = "perplexity/sonar-pro-search"
_DEFAULT_OAI_SEARCH_MODEL = "gpt-4o-mini-search-preview"


def _web_search_openrouter(query: str) -> str:
    """Search via OpenRouter (no extra API key required)."""
    api_key = os.environ.get("OPENROUTER_API_KEY", "")
    if not api_key:
        return ""  # caller will try next backend
    try:
        from openai import OpenAI
        model = os.environ.get("OUROBOROS_WEBSEARCH_MODEL_OR", _DEFAULT_OR_SEARCH_MODEL)
        client = OpenAI(
            api_key=api_key,
            base_url="https://openrouter.ai/api/v1",
        )
        resp = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": query}],
        )
        text = (resp.choices[0].message.content or "").strip()
        return json.dumps({"answer": text or "(no answer)"}, ensure_ascii=False, indent=2)
    except Exception as e:
        return json.dumps({"error": f"openrouter: {repr(e)}"}, ensure_ascii=False)


def _web_search_openai(query: str) -> str:
    """Search via OpenAI Responses API (requires OPENAI_API_KEY)."""
    api_key = os.environ.get("OPENAI_API_KEY", "")
    if not api_key:
        return ""  # not available
    try:
        from openai import OpenAI
        model = os.environ.get("OUROBOROS_WEBSEARCH_MODEL", _DEFAULT_OAI_SEARCH_MODEL)
        client = OpenAI(api_key=api_key)
        resp = client.responses.create(
            model=model,
            tools=[{"type": "web_search_preview"}],
            tool_choice="auto",
            input=query,
        )
        d = resp.model_dump()
        text = ""
        for item in d.get("output", []) or []:
            if item.get("type") == "message":
                for block in item.get("content", []) or []:
                    if block.get("type") in ("output_text", "text"):
                        text += block.get("text", "")
        return json.dumps({"answer": text or "(no answer)"}, ensure_ascii=False, indent=2)
    except Exception as e:
        return json.dumps({"error": f"openai: {repr(e)}"}, ensure_ascii=False)


def _web_search(ctx: ToolContext, query: str) -> str:
    # Try OpenRouter first (always available when the system is running)
    or_result = _web_search_openrouter(query)
    if or_result and '"error"' not in or_result:
        return or_result

    # Fallback: OpenAI Responses API
    oai_result = _web_search_openai(query)
    if oai_result:
        return oai_result

    # Both unavailable
    return json.dumps({
        "error": "web_search unavailable: neither OPENROUTER_API_KEY (unexpected) nor OPENAI_API_KEY is configured."
    })


def get_tools() -> List[ToolEntry]:
    return [
        ToolEntry("web_search", {
            "name": "web_search",
            "description": "Search the web via OpenAI Responses API. Returns JSON with answer + sources.",
            "parameters": {"type": "object", "properties": {
                "query": {"type": "string"},
            }, "required": ["query"]},
        }, _web_search),
    ]
