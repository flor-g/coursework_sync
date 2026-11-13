"""Settings, read from environment variables (GitHub Actions secrets in production)."""

from __future__ import annotations

import os
from dataclasses import dataclass

class ConfigError(RuntimeError):
    pass


def _env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name, "").strip()
    return value or default


@dataclass(frozen=True)
class Config:
    canvas_base_url: str
    canvas_token: str
    notion_token: str | None
    timezone: str
    courses_ds: str
    assignments_ds: str
    lectures_ds: str
    updates_ds: str
    files_ds: str
    announcements_lookback_days: int
    email_model: str = "claude-haiku-5-5"
    convert_model: str = "claude-opus-5-5"
    anthropic_enabled: bool = False
    code_ds: str = ""
    ed_token: str | None = None
    ed_region: str = "us"


def load_config() -> Config:
    """Settings from the environment, falling back to [school] and [notion] in courses/courses.toml."""
    from .course_list import CourseListError, load

    canvas_token = _env("CANVAS_TOKEN")
    if not canvas_token:
        raise ConfigError("CANVAS_TOKEN is not set.")
    try:
        course_list = load()
    except CourseListError as exc:
        raise ConfigError(str(exc)) from exc
    school, ids = course_list.school, course_list.notion

    def data_source(env: str, value: str, name: str, required: bool = True) -> str:
        found = _env(env, value)
        if not found and required:
            raise ConfigError(f"Set {name} under [notion] in courses/courses.toml (the id of the {name} "
                              f"data source in Notion; see docs/notion-setup.md).")
        return found or ""
    canvas_url = _env("CANVAS_BASE_URL", school.canvas_url)
    if not canvas_url:
        raise ConfigError("Set canvas_url under [school] in courses/courses.toml.")
    return Config(
        canvas_base_url=canvas_url.rstrip("/"),
        canvas_token=canvas_token,
        notion_token=_env("NOTION_TOKEN"),
        timezone=_env("TIMEZONE", school.timezone or "America/New_York"),
        courses_ds=data_source("NOTION_COURSES_DS", ids.courses, "courses"),
        assignments_ds=data_source("NOTION_ASSIGNMENTS_DS", ids.assignments, "assignments"),
        lectures_ds=data_source("NOTION_LECTURES_DS", ids.lectures, "lectures"),
        updates_ds=data_source("NOTION_UPDATES_DS", ids.updates, "updates"),
        files_ds=data_source("NOTION_FILES_DS", ids.files, "files", required=False),
        announcements_lookback_days=int(_env("ANNOUNCEMENTS_LOOKBACK_DAYS", "120")),
        email_model=_env("EMAIL_MODEL", "claude-haiku-5-5"),
        convert_model=_env("CONVERT_MODEL", "claude-opus-5-5"),
        anthropic_enabled=bool(_env("ANTHROPIC_API_KEY")),
        code_ds=data_source("NOTION_CODE_DS", ids.code, "code", required=False),
        ed_token=_env("ED_TOKEN"),
        ed_region=_env("ED_REGION", school.ed_region or "us"),
    )
