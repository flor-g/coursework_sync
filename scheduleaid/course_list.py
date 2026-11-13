"""The course list in courses/: the school's settings, the courses, and their website readers.

courses/courses.toml is the source of truth for which courses are tracked. Each sync starts by
making Notion's Courses database match it: new courses get a course page, listed courses get the
values from the file, and courses no longer listed are archived (kept, but no longer synced).
"""

from __future__ import annotations

import importlib.util
import logging
import os
import re
import tomllib
from dataclasses import dataclass, field
from functools import cache
from datetime import datetime
from pathlib import Path

from . import notion as n
from .blocks import block, heading, paragraph, text_item
from .sync_common import AssignmentData, BaseSync, content_hash, course_key, parse_dt, slug

log = logging.getLogger(__name__)

COURSES_DIR = Path(os.environ.get("SCHEDULEAID_COURSES") or Path(__file__).resolve().parent.parent / "courses")


class CourseListError(ValueError):
    pass


@dataclass(frozen=True)
class School:
    name: str = ""
    term: str = ""
    timezone: str = "America/New_York"
    canvas_url: str = ""
    ed_region: str = "us"
    staff_subjects: tuple[str, ...] = ()


@dataclass(frozen=True)
class NotionIds:
    """The Notion data sources the sync writes to ([notion] in courses.toml)."""
    courses: str = ""
    assignments: str = ""
    lectures: str = ""
    updates: str = ""
    files: str = ""
    code: str = ""


NOTION_FIELDS = set(NotionIds.__dataclass_fields__)


@dataclass(frozen=True)
class Exam:
    """An exam listed in courses.toml: { name = "Midterm 2", date = "2026-11-18", ... }.
    When the course website or CourseWorks lists an exam of the same name, its page is used; an
    entry here then only adds the time if that source gives just the date."""
    name: str
    date: str                       # "2026-11-18", or "2026-12-23 12:00" with a start time
    end: str = ""                   # "15:00": when it ends (same day)
    note: str = ""
    source: str = ""                # where the date comes from (a link)


EXAM_FIELDS = {"name", "date", "end", "note", "source"}
EXAM_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}(?:[ T]\d{1,2}:\d{2})?$")
EXAM_TIME = re.compile(r"^\d{1,2}:\d{2}$")


@dataclass(frozen=True)
class Course:
    name: str
    code: str
    instructor: str = ""
    website: str = ""
    canvas_id: str = ""
    gmail_query: str = ""
    ed_url: str = ""
    meeting_times: str = ""
    office_hours: str = ""
    # What the course calls its assignments elsewhere (Ed, emails) → the name used in Notion,
    # e.g. ("P", "Programming Assignment"): "P4" means "Programming Assignment 4".
    aliases: tuple[tuple[str, str], ...] = ()
    exams: tuple[Exam, ...] = ()

    @property
    def key(self) -> str:
        return course_key(self.code)

    @property
    def default_gmail_query(self) -> str:
        """"COMS W3134" → ("COMS W3134" OR "COMS 3134" OR "W3134" OR "COMSW3134")."""
        m = re.match(r"^\s*([A-Za-z]+)\s+([A-Za-z]{0,2})(\d+[A-Za-z]?)\s*$", self.code)
        if not m:   # e.g. "COMSW3134": where the department ends is unclear
            return f'"{self.code.strip()}"'
        dept, letter, number = m.groups()
        variants = [f"{dept} {letter}{number}", f"{dept} {number}"]
        if letter:   # a bare number would match unrelated emails
            variants.append(f"{letter}{number}")
        variants.append(f"{dept}{letter}{number}")
        return "(" + " OR ".join(f'"{v}"' for v in dict.fromkeys(variants)) + ")"


@dataclass(frozen=True)
class CourseList:
    school: School
    courses: tuple[Course, ...] = field(default_factory=tuple)
    notion: NotionIds = field(default_factory=NotionIds)


COURSE_FIELDS = {f for f in Course.__dataclass_fields__}
SCHOOL_FIELDS = {f for f in School.__dataclass_fields__}


def parse(text: str) -> CourseList:
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise CourseListError(f"courses.toml is not valid TOML: {exc}") from exc
    school_data = data.get("school", {})
    unknown = set(school_data) - SCHOOL_FIELDS
    if unknown:
        raise CourseListError(f"courses.toml [school]: unknown setting(s) {', '.join(sorted(unknown))}")
    for pattern in school_data.get("staff_subjects", []):
        try:
            re.compile(pattern)
        except re.error as exc:
            raise CourseListError(f"courses.toml staff_subjects: {pattern!r} is not a valid pattern ({exc})")
    school = School(**{**school_data, "staff_subjects": tuple(school_data.get("staff_subjects", ()))})
    notion_data = data.get("notion", {})
    if set(notion_data) - NOTION_FIELDS:
        raise CourseListError(f"courses.toml [notion]: unknown setting(s) "
                              f"{', '.join(sorted(set(notion_data) - NOTION_FIELDS))}")
    notion = NotionIds(**{k: str(v).strip() for k, v in notion_data.items()})
    courses, keys = [], set()
    for i, entry in enumerate(data.get("course", []), start=1):
        unknown = set(entry) - COURSE_FIELDS
        if unknown:
            raise CourseListError(f"courses.toml course {i}: unknown field(s) {', '.join(sorted(unknown))}")
        if not entry.get("name") or not entry.get("code"):
            raise CourseListError(f"courses.toml course {i}: `name` and `code` are required")
        aliases = entry.pop("aliases", {})
        if not isinstance(aliases, dict) or not all(isinstance(v, str) for v in aliases.values()):
            raise CourseListError(f"courses.toml course {i}: `aliases` must look like {{ P = \"Programming Assignment\" }}")
        exams = []
        for j, ex in enumerate(entry.pop("exams", []), start=1):
            where = f"courses.toml course {i}, exam {j}"
            if not isinstance(ex, dict) or not ex.get("name") or not ex.get("date"):
                raise CourseListError(f"{where}: each exam needs a `name` and a `date`")
            if set(ex) - EXAM_FIELDS:
                raise CourseListError(f"{where}: unknown field(s) {', '.join(sorted(set(ex) - EXAM_FIELDS))}")
            exam = Exam(**{k: str(v).strip() for k, v in ex.items()})
            if not EXAM_DATE.match(exam.date):
                raise CourseListError(f"{where}: date should look like 2026-11-18 or 2026-12-23 12:00")
            if exam.end and not (EXAM_TIME.match(exam.end) and " " in exam.date.replace("T", " ")):
                raise CourseListError(f"{where}: `end` is a time like 15:00, and needs a start time in `date`")
            exams.append(exam)
        course = Course(**{k: str(v).strip() for k, v in entry.items()},
                        aliases=tuple((k.strip(), v.strip()) for k, v in aliases.items()), exams=tuple(exams))
        if course.key in keys:
            raise CourseListError(f"courses.toml: {course.code} is listed twice")
        keys.add(course.key)
        courses.append(course)
    return CourseList(school, tuple(courses), notion)


@cache
def load(directory: Path | None = None) -> CourseList:
    path = (directory or COURSES_DIR) / "courses.toml"
    if not path.exists():
        return CourseList(School())
    return parse(path.read_text(encoding="utf-8"))


@cache
def site_readers(directory: Path | None = None) -> list[tuple[re.Pattern, object]]:
    """(URL pattern, read function) for every reader in courses/sites/."""
    readers = []
    for path in sorted(((directory or COURSES_DIR) / "sites").glob("*.py")):
        if path.name.startswith("_"):
            continue
        spec = importlib.util.spec_from_file_location(f"scheduleaid_sites.{path.stem}", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        if not hasattr(module, "MATCHES") or not hasattr(module, "read"):
            raise CourseListError(f"courses/sites/{path.name} needs a MATCHES pattern and a read() function")
        readers.append((re.compile(module.MATCHES), module.read))
    return readers


# --- Notion -----------------------------------------------------------------------
TEMPLATE_HINT = re.compile(r"course", re.I)


class CourseListSync(BaseSync):
    """Makes the Notion Courses database match courses/courses.toml."""

    SOURCE = "Course list"

    def __init__(self, cfg, notion_client, course_list: CourseList | None = None, now=None, report=None):
        super().__init__(cfg, notion_client, now, report)
        self.list = course_list or load()

    def run(self, course_pages: list[dict]) -> list[dict]:
        """Returns the course pages after the update (new ones included)."""
        if not self.list.courses:
            self.report.notes.append("courses/courses.toml lists no courses; Notion's Courses were left as they are.")
            return course_pages
        by_key = {}
        for page in course_pages:
            key = course_key(n.get_text(page, "Code") or n.get_text(page, "Name"))
            if key:
                by_key.setdefault(key, page)
        template = None
        for course in self.list.courses:
            page = by_key.pop(course.key, None)
            if page is None:
                if template is None:
                    template = self.course_template()
                page = self.create(course, template)
                course_pages.append(page)
            else:
                self.update(page, course)
            if course.exams and page.get("id") != "dry-run":
                self.sync_exams(page, course)
        for page in by_key.values():   # in Notion, not in the file
            if n.get_select(page, "Status") != "Archived":
                self.notion.update_page(page["id"], {"Status": n.select("Archived")})
                page["properties"]["Status"] = n.select("Archived")
                self.report.notes.append(f"Archived {n.get_text(page, 'Code') or n.get_text(page, 'Name')}: "
                                         "it is no longer in courses/courses.toml.")
        return course_pages

    # --- exams --------------------------------------------------------------------------
    def sync_exams(self, course_page: dict, course: Course) -> None:
        """Exam pages in Assignments for the exams listed in courses.toml. An exam that the
        course's website or CourseWorks already lists (same name) is left to that source."""
        others = {_exam_key(n.get_text(p, "Name")): p for p in self.notion.query_all(
            self.cfg.assignments_ds, {"property": "Course", "relation": {"contains": course_page["id"]}})
            if n.get_select(p, "Source") != self.SOURCE}
        for exam in course.exams:
            due, end = self.exam_times(exam)
            other = others.get(_exam_key(exam.name))
            if other is not None:
                self.add_time(other, exam, due, end)
                continue
            last = parse_dt(end or due, self.tz)
            if last and not end and len(due) == 10:
                last = last.replace(hour=23, minute=59)
            past = bool(last and last < self.now)
            sid = f"list:{course.key}:{slug(exam.name)}"
            existing = self.index(self.cfg.assignments_ds).get(sid)
            blocks = [paragraph(text_item(exam.note))] if exam.note else []
            if exam.source:
                blocks.append(paragraph(text_item("Date from: ", color="gray"), text_item(exam.source, link=exam.source)))
            blocks.append(paragraph(text_item("Listed in courses/courses.toml.", color="gray")))
            self.upsert_assignment(course_page, AssignmentData(
                sid=sid, name=exam.name, due=due, end=end, source_url=exam.source or None, blocks=blocks,
                hash=content_hash("exam-v1", exam.name, due, end, exam.note, exam.source),
                type="Exam", is_exam=True, initial_status="Done" if past else "Not started",
                advanced_status=("Done" if past and existing and n.get_select(existing, "Status") != "Done"
                                 else None)))

    def add_time(self, page: dict, exam: Exam, due: str, end: str | None) -> None:
        """The website lists this exam with only a date; courses.toml knows the time that day."""
        current = n.get_date(page, "Due") or ""
        if len(due) == 10 or len(current) != 10 or due[:10] != current:
            return
        self.notion.update_page(page["id"], {"Due": n.date(due, end), "Last synced": self.stamp})
        page["properties"]["Due"] = n.date(due, end)
        self.report.notes.append(f"{exam.name}: added the time from courses.toml ({due[11:16]}).")

    def exam_times(self, exam: Exam) -> tuple[str, str | None]:
        """ISO start (or bare date) and end, in the school's time zone."""
        date_part, _, time_part = exam.date.replace("T", " ").partition(" ")
        if not time_part:
            return date_part, None
        start = datetime.fromisoformat(f"{date_part}T{int(time_part.split(':')[0]):02d}:{time_part.split(':')[1]}")
        start = start.replace(tzinfo=self.tz)
        end = None
        if exam.end:
            h, m = exam.end.split(":")
            end = start.replace(hour=int(h), minute=int(m)).isoformat()
        return start.isoformat(), end

    def props(self, course: Course, new: bool) -> dict:
        props = {"Name": n.title(course.name), "Code": n.rich_text(course.code), "Status": n.select("Active")}
        if self.list.school.term:
            props["Term"] = n.select(self.list.school.term)
        gmail = course.gmail_query or (course.default_gmail_query if new else "")
        optional = {"Instructor": n.rich_text(course.instructor), "Website": n.url(course.website),
                    "CourseWorks ID": n.rich_text(course.canvas_id),
                    "CourseWorks URL": n.url(f"{self.cfg.canvas_base_url}/courses/{course.canvas_id}"),
                    "Gmail query": n.rich_text(gmail),
                    "Ed URL": n.url(course.ed_url), "Meeting times": n.rich_text(course.meeting_times),
                    "Office hours": n.rich_text(course.office_hours)}
        values = {"Instructor": course.instructor, "Website": course.website, "CourseWorks ID": course.canvas_id,
                  "CourseWorks URL": course.canvas_id,
                  "Gmail query": gmail, "Ed URL": course.ed_url, "Meeting times": course.meeting_times,
                  "Office hours": course.office_hours}
        # A field left out of the file is left alone in Notion (the sync may have filled it in).
        props.update({k: v for k, v in optional.items() if values[k]})
        return props

    def update(self, page: dict, course: Course) -> None:
        wanted = self.props(course, new=False)
        changes = {k: v for k, v in wanted.items() if not _same(page, k, v)}
        if not changes:
            return
        self.notion.update_page(page["id"], changes)
        page["properties"].update(changes)
        self.report.notes.append(f"{course.code}: updated {', '.join(sorted(changes))} from courses.toml.")

    def course_template(self) -> dict | None:
        """The Courses database template that new course pages are made from, if there is one."""
        try:
            templates = self.notion.templates(self.cfg.courses_ds)
        except Exception as exc:
            log.warning("Could not list Courses templates: %s", exc)
            return None
        return (next((t for t in templates if t.get("is_default")), None)
                or next((t for t in templates if TEMPLATE_HINT.search(t.get("name") or "")), None)
                or (templates[0] if templates else None) or {})

    def create(self, course: Course, template: dict | None) -> dict:
        props = self.props(course, new=True)
        if template and template.get("id"):
            page = self.notion.create_page(self.cfg.courses_ds, props, template_id=template["id"])
            self.report.notes.append(f"Added {course.code} {course.name} to Notion, from the "
                                     f"\"{template.get('name') or 'course'}\" template.")
        else:
            page = self.notion.create_page(self.cfg.courses_ds, props, self.course_page_body(course))
            self.report.warnings.append(
                f"Added {course.code} {course.name} to Notion. Its page has no To-do, Assignments or other "
                "lists yet, because the Courses database has no template; see courses/README.md.")
        self.report.created += 1
        page.setdefault("properties", {}).update(props)
        return page

    def course_page_body(self, course: Course) -> list[dict]:
        """A course page without database views (used when the Courses database has no template)."""
        school = self.list.school
        sub = " · ".join(x for x in (course.code, school.term) if x)
        line2 = " · ".join(x for x in (course.instructor, course.meeting_times) if x)
        rows = [("Course code", course.code), ("Instructor", course.instructor),
                ("Lectures", course.meeting_times), ("Office hours", course.office_hours),
                ("Links", " · ".join(x for x in (course.website, course.ed_url) if x))]
        table = {"object": "block", "type": "table", "table": {
            "table_width": 2, "has_column_header": False, "has_row_header": True,
            "children": [{"object": "block", "type": "table_row", "table_row": {
                "cells": [[text_item(label)], [text_item(value)] if value else []]}} for label, value in rows]}}
        return [
            paragraph(text_item(sub, color="gray")),
            *([paragraph(text_item(line2, color="gray"))] if line2 else []),
            block("callout", [text_item("Important", bold=True, color="red")], color="gray_background"),
            heading(2, "At a glance"), table,
            paragraph(text_item("To-do, Assignments, Updates, Lecture notes, Files and Code appear here once the "
                                "Courses database has a course template (see courses/README.md).", color="gray")),
        ]


def _exam_key(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())


def _same(page: dict, name: str, value: dict) -> bool:
    current = page.get("properties", {}).get(name) or {}
    if "title" in value or "rich_text" in value:
        return n.get_text(page, name) == n._plain(value.get("title") or value.get("rich_text"))
    if "select" in value:
        return (current.get("select") or current.get("status") or {}).get("name") == value["select"]["name"]
    if "url" in value:
        return current.get("url") == value["url"]
    return False
