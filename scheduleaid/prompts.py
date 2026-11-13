"""The prompts sent to Claude, kept as files in prompts/ so they can be read and edited apart from
the code. "*.system.md" files are system prompts, used as written. "*.user.md" files are message
templates whose {fields} are filled in for each email or lecture. "*.schema.json" files are the JSON
schemas Claude's replies must follow."""

from __future__ import annotations

import json
from functools import cache
from pathlib import Path

PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"


@cache
def load(name: str) -> str:
    """The text of prompts/<name>.md, without the final newline."""
    return (PROMPTS_DIR / f"{name}.md").read_text(encoding="utf-8").rstrip("\n")


def render(name: str, **fields: str) -> str:
    """A user-message template with its {fields} filled in."""
    return load(name).format(**fields)


@cache
def load_json(name: str) -> dict:
    """prompts/<name>.json, e.g. a reply schema."""
    return json.loads((PROMPTS_DIR / f"{name}.json").read_text(encoding="utf-8"))
