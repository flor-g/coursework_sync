"""The prompts in prompts/ load, and the user templates have the fields the code fills in."""

from string import Formatter

import pytest

from scheduleaid import prompts

TEMPLATES = {
    "email_summary.user": {"course", "today", "known_assignments", "sender", "sender_email", "sent",
                           "subject", "attachments", "body"},
    "lecture_notes.user": {"lecture"},
}


def test_every_prompt_file_is_used():
    files = {p.name for p in prompts.PROMPTS_DIR.iterdir()} - {"README.md"}
    assert files == {"email_summary.system.md", "lecture_notes.system.md", "email_summary.schema.json",
                     *(f"{name}.md" for name in TEMPLATES)}


def test_email_schema_is_strict_and_feeds_the_code():
    from scheduleaid import email_analysis as ea

    schema = prompts.load_json("email_summary.schema")
    assert ea.SCHEMA is schema

    def check(obj):   # structured outputs need every property required and no extras
        if obj.get("type") == "object":
            assert set(obj["required"]) == set(obj["properties"]) and obj["additionalProperties"] is False
            for prop in obj["properties"].values():
                check(prop)
        elif obj.get("type") == "array":
            check(obj["items"])

    check(schema)
    for field in ("title", "summary", "category", "important", "from_staff", "related_assignments",
                  "new_tasks"):
        assert field in schema["properties"] and field in prompts.load("email_summary.system")
    assert {"Other", "Announcement"} <= set(ea.CATEGORIES) and "Action item" in ea.TASK_TYPES   # fallbacks


@pytest.mark.parametrize("name", ["email_summary.system", "lecture_notes.system"])
def test_system_prompts_load(name):
    text = prompts.load(name)
    assert len(text) > 200 and not text.endswith("\n")


@pytest.mark.parametrize("name,fields", TEMPLATES.items())
def test_templates_have_their_fields(name, fields):
    used = {f for _, f, _, _ in Formatter().parse(prompts.load(name)) if f}
    assert used == fields
    prompts.render(name, **{f: "x" for f in fields})   # renders without error
