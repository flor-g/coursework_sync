"""Shared settings for Claude calls."""

from __future__ import annotations

# Models that accept the server-side refusal fallback (`fallbacks: "default"`). On these, a request
# a safety classifier declines is re-run on a fallback model inside the same call. Haiku has no
# server-side fallback; there a declined request simply returns stop_reason "refusal".
FALLBACK_MODELS = {"claude-fable-5-1", "claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5"}
FALLBACK_BETA = "server-side-fallback-2026-07-01"


def messages_api(client, model: str):
    """The messages resource to call for this model, plus extra request arguments."""
    if model in FALLBACK_MODELS:
        return client.beta.messages, {"betas": [FALLBACK_BETA], "fallbacks": "default"}
    return client.messages, {}
