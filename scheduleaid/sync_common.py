"""Pieces shared by every source: time helpers, the run report, and the Notion writes for
assignments, deadline changes and lecture notes. (Calendar events are made from Notion by the
Apps Script in apps_script/.)

Every page the sync creates stores a "Source ID", so runs are idempotent: an existing page is
updated in place, never duplicated.
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable
from zoneinfo import ZoneInfo

from . import notion as n
from .http import ApiError
from .blocks import heading, heading3, paragraph, source_section, text_item, uploaded_file_block
from .config import Config

log = logging.getLogger(__name__)


# --- time helpers -----------------------------------------------------------
def parse_dt(value: str | None, tz: ZoneInfo) -> datetime | None:
    if not value:
        return None
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=tz)


def to_local_iso(value: str | None, tz: ZoneInfo) -> str | None:
    dt = parse_dt(value, tz)
    return dt.astimezone(tz).isoformat() if dt else None


def is_date_only(value: str | None) -> bool:
    return bool(value) and "T" not in value


def same_minute(a: str | None, b: str | None, tz: ZoneInfo) -> bool:
    if is_date_only(a) or is_date_only(b):
        return (a or "")[:10] == (b or "")[:10] and is_date_only(a) == is_date_only(b)
    da, db = parse_dt(a, tz), parse_dt(b, tz)
    if da is None or db is None:
        return da is db
    return da.replace(second=0, microsecond=0) == db.replace(second=0, microsecond=0)


def human_dt(iso: str | None, tz: ZoneInfo) -> str:
    dt = parse_dt(iso, tz)
    if not dt:
        return "no due date"
    dt = dt.astimezone(tz)
    day = f"{dt:%a, %b} {dt.day}"
    return day if is_date_only(iso) else f"{day}, {dt.strftime('%I:%M %p').lstrip('0')}"


def content_hash(*parts: str | None) -> str:
    joined = "\x1f".join(p or "" for p in parts)
    return hashlib.sha256(joined.encode()).hexdigest()[:16]


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


def course_key(text: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", (text or "").upper())


EMAIL_MARKER = "From email: "

# Source files are shown as code blocks instead of uploads (Notion's upload API refuses them).
CODE_EXTENSIONS = {
    ".java": "java", ".c": "c", ".h": "c", ".cpp": "c++", ".cc": "c++", ".hpp": "c++",
    ".py": "python", ".sh": "bash", ".js": "javascript", ".ts": "typescript", ".go": "go",
    ".rs": "rust", ".sql": "sql", ".txt": "plain text", ".md": "markdown", ".json": "json",
    ".yml": "yaml", ".yaml": "yaml", ".xml": "xml", ".html": "html", ".css": "css",
    ".csv": "plain text", ".tsv": "plain text", ".in": "plain text", ".out": "plain text",
}
MAKEFILES = {"makefile", "gnumakefile"}
MAX_CODE_CHARS = 100_000
MIME_ALIASES = {"application/x-zip-compressed": "application/zip", "application/x-zip": "application/zip"}


def code_language(filename: str) -> str | None:
    lower = filename.lower()
    if lower in MAKEFILES:
        return "makefile"
    dot = lower.rfind(".")
    return CODE_EXTENSIONS.get(lower[dot:]) if dot >= 0 else None


def code_blocks(filename: str, source: str, language: str) -> list[dict]:
    pieces = [source[i:i + 2000] for i in range(0, len(source), 2000)] or [" "]
    blocks = [paragraph(text_item(filename, bold=True))]
    for start in range(0, len(pieces), 100):
        blocks.append({"object": "block", "type": "code", "code": {
            "language": language,
            "rich_text": [{"type": "text", "text": {"content": p}} for p in pieces[start:start + 100]]}})
    return blocks


def block_plain(block: dict) -> str:
    body = block.get(block.get("type", ""), {}) or {}
    return n._plain(body.get("rich_text") or [])


def is_email_marker(block: dict) -> bool:
    return block.get("type") == "heading_3" and block_plain(block).startswith(EMAIL_MARKER)


# --- report -----------------------------------------------------------------
@dataclass
class Report:
    created: int = 0
    updated: int = 0
    unchanged: int = 0
    skipped: int = 0
    notes: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def summary(self) -> str:
        lines = [f"Created {self.created}, updated {self.updated}, unchanged {self.unchanged}, "
                 f"skipped {self.skipped} (gradebook-only entries)."]
        lines += [f"- {m}" for m in self.notes]
        lines += [f"- WARNING: {m}" for m in self.warnings]
        lines += [f"- ERROR: {m}" for m in self.errors]
        return "\n".join(lines)


# --- data passed to the writers ------------------------------------------------
@dataclass
class Attachment:
    """An instruction file: uploaded into Notion when it can be fetched and is under 5 MB,
    otherwise shown as a link to where it lives."""
    name: str
    link: str | None
    fetch: Callable[[], tuple[bytes, str, str] | None] | None = None   # → (data, filename, mime)


@dataclass
class AssignmentData:
    sid: str
    name: str
    due: str | None                  # ISO datetime, or a bare date for all-day items
    source_url: str | None
    blocks: list[dict]               # body for the "Instructions" toggle
    hash: str                        # changes when the body should be rewritten
    type: str
    initial_status: str
    points: float | None = None
    manage_points: bool = False
    advanced_status: str | None = None   # set when the source shows progress (e.g. submitted)
    end: str | None = None           # exams: when it ends (stored as the end of the Due date)
    is_exam: bool = False
    files: list[Attachment] = field(default_factory=list)


@dataclass
class LectureData:
    sid: str
    name: str
    number: int | None
    date: str | None
    original_url: str | None
    source_url: str | None
    files: list[tuple[str, str]] = field(default_factory=list)
    format: str | None = None        # when the URL doesn't show it (CourseWorks file pages)


@dataclass
class CodeData:
    """One source file for the course's Code database."""
    sid: str
    name: str                        # the file name, e.g. "MyArrayList.java"
    group: str                       # where it belongs, e.g. "Lecture 12" or "Sep 28: Array lists"
    date: str | None
    source_url: str | None
    version: str                     # changes when the file changes (decides whether to re-download)
    fetch: Callable[[], bytes]
    lecture_sid: str | None = None   # Source ID of the lecture it goes with
    filename: str | None = None      # the real file name when `name` has a suffix ("X.java (section 2)")

    @property
    def file(self) -> str:
        return self.filename or self.name


def file_format(url: str | None) -> str:
    path = (url or "").lower().split("?")[0]
    if path.endswith(".pdf"):
        return "PDF"
    if path.endswith((".ppt", ".pptx", ".key", ".odp")):
        return "Slides"
    return "Other"


# --- base sync ----------------------------------------------------------------
class BaseSync:
    SOURCE = "?"

    def __init__(self, cfg: Config, notion_client, now: datetime | None = None,
                 report: Report | None = None):
        self.cfg = cfg
        self.notion = notion_client
        self.tz = ZoneInfo(cfg.timezone)
        self.now = now or datetime.now(self.tz)
        self.report = report or Report()
        self._indexes: dict[str, dict[str, dict]] = {}

    @property
    def stamp(self) -> dict:
        return n.date(self.now.replace(microsecond=0).isoformat())

    def index(self, data_source_id: str) -> dict[str, dict]:
        """Pages from this source in a data source, keyed by Source ID (loaded once per run)."""
        if data_source_id not in self._indexes:
            try:
                pages = self.notion.query_all(
                    data_source_id, {"property": "Source", "select": {"equals": self.SOURCE}})
            except ApiError as exc:
                # Notion refuses a filter on a select option nobody has used yet: no pages.
                if exc.status != 400 or "not found" not in str(exc):
                    raise
                pages = []
            self._indexes[data_source_id] = {
                n.get_text(p, "Source ID"): p for p in pages if n.get_text(p, "Source ID")}
        return self._indexes[data_source_id]

    def active_courses(self, course_pages: list[dict] | None) -> list[dict]:
        pages = course_pages if course_pages is not None else self.notion.query_all(self.cfg.courses_ds)
        return [p for p in pages if n.get_select(p, "Status") != "Archived"]

    @staticmethod
    def label(course_page: dict) -> str:
        return n.get_text(course_page, "Code") or n.get_text(course_page, "Name")

    # assignments
    def upsert_assignment(self, course_page: dict, data: AssignmentData) -> dict:
        assignments = self.index(self.cfg.assignments_ds)
        page = assignments.get(data.sid)

        if page is None:
            props = {
                "Name": n.title(data.name),
                "Course": n.relation(course_page["id"]),
                "Due": n.date(data.due, data.end),
                "Status": n.status(data.initial_status),
                "Type": n.select(data.type),
                "Source": n.select(self.SOURCE),
                "Source URL": n.url(data.source_url),
                "Source ID": n.rich_text(data.sid),
                "Content hash": n.rich_text(data.hash),
                "Last synced": self.stamp,
            }
            if data.manage_points:
                props["Points"] = n.number(data.points)
            page = self.notion.create_page(self.cfg.assignments_ds, props, self.skeleton(
                ["Instructions"], data.source_url))
            assignments[data.sid] = page
            self.write_instructions(page["id"], data)
            self.report.created += 1
            return page

        changes: dict = {}
        if n.get_text(page, "Name") != data.name:
            changes["Name"] = n.title(data.name)
        old_due = n.get_date(page, "Due")
        # A source that gives only the day (a website's "Final: Dec 21") doesn't undo a time we
        # already know for that day (from courses.toml).
        keep_time = bool(data.due and old_due and is_date_only(data.due) and not data.end
                         and not is_date_only(old_due)
                         and parse_dt(old_due, self.tz).astimezone(self.tz).date().isoformat() == data.due)
        due_changed = not keep_time and not same_minute(old_due, data.due, self.tz)
        end_changed = not keep_time and not same_minute(n.get_date_end(page, "Due"), data.end, self.tz)
        if due_changed or end_changed:
            changes["Due"] = n.date(data.due, data.end)
        if data.manage_points and n.get_number(page, "Points") != data.points:
            changes["Points"] = n.number(data.points)
        if n.get_url(page, "Source URL") != (data.source_url or None):
            changes["Source URL"] = n.url(data.source_url)
        if data.advanced_status:
            changes["Status"] = n.status(data.advanced_status)
        if n.get_text(page, "Content hash") != data.hash:
            changes["Content hash"] = n.rich_text(data.hash)
            self.write_instructions(page["id"], data)

        if changes:
            changes["Last synced"] = self.stamp
            self.notion.update_page(page["id"], changes)
            page["properties"].update(changes)
            self.report.updated += 1
        else:
            self.report.unchanged += 1

        if due_changed and old_due:
            self.record_deadline_change(course_page, page, data, old_due)
        return page

    def record_deadline_change(self, course_page: dict, assignment_page: dict,
                               data: AssignmentData, old_due: str) -> None:
        updates = self.index(self.cfg.updates_ds)
        sid = f"{data.sid}:due:{data.due or 'none'}"
        if sid in updates:
            return
        new_text, old_text = human_dt(data.due, self.tz), human_dt(old_due, self.tz)
        what = "date changed" if data.is_exam else "due date changed"
        props = {
            "Name": n.title(f"{data.name}: {what} to {new_text}"),
            "Course": n.relation(course_page["id"]),
            "Date": self.stamp,
            "Category": n.select("Exam" if data.type == "Exam" else "Deadline change"),
            "Important": n.checkbox(True),
            "Related assignment": n.relation(assignment_page["id"]),
            "Source": n.select(self.SOURCE),
            "Source URL": n.url(data.source_url),
            "Source ID": n.rich_text(sid),
            "Last synced": self.stamp,
        }
        children = [paragraph(text_item(f"Was: {old_text}", color="gray")),
                    paragraph(text_item(f"Now: {new_text}", bold=True)),
                    *source_section(self.SOURCE, data.source_url)]
        updates[sid] = self.notion.create_page(self.cfg.updates_ds, props, children)
        self.report.notes.append(f"Deadline change: {self.label(course_page)} {data.name} → {new_text}")

    # lecture notes
    def upsert_lecture(self, course_page: dict, data: LectureData) -> dict:
        lectures = self.index(self.cfg.lectures_ds)
        files_hash = content_hash(*(f"{label}|{link}" for label, link in data.files))
        page = lectures.get(data.sid)
        if page is None:
            props = {
                "Name": n.title(data.name),
                "Course": n.relation(course_page["id"]),
                "Lecture #": n.number(data.number),
                "Date": n.date(data.date),
                "Format": n.select(data.format or file_format(data.original_url)),
                "Conversion status": n.select("Pending"),
                "Original URL": n.url(data.original_url),
                "Source": n.select(self.SOURCE),
                "Source URL": n.url(data.source_url),
                "Source ID": n.rich_text(data.sid),
                "Content hash": n.rich_text(files_hash),
                "Last synced": self.stamp,
            }
            page = self.notion.create_page(self.cfg.lectures_ds, props,
                                           self.skeleton(["Notes", "Files"], data.original_url))
            lectures[data.sid] = page
            self.write_toggle(page["id"], "Notes", [paragraph(text_item(
                "Not converted to Markdown yet. The original is linked under Files.", color="gray"))])
            self.write_toggle(page["id"], "Files", self.file_blocks(data.files))
            self.report.created += 1
            return page

        changes: dict = {}
        if n.get_text(page, "Name") != data.name:
            changes["Name"] = n.title(data.name)
        if n.get_url(page, "Original URL") != (data.original_url or None):
            changes["Original URL"] = n.url(data.original_url)
        if data.source_url and n.get_url(page, "Source URL") != data.source_url:
            changes["Source URL"] = n.url(data.source_url)
            self.refresh_source_link(page["id"], data.source_url)
        if not n.get_select(page, "Conversion status"):
            changes["Conversion status"] = n.select("Pending")
        if n.get_text(page, "Content hash") != files_hash:
            changes["Content hash"] = n.rich_text(files_hash)
            self.write_toggle(page["id"], "Files", self.file_blocks(data.files))
        if changes:
            changes["Last synced"] = self.stamp
            self.notion.update_page(page["id"], changes)
            self.report.updated += 1
        else:
            self.report.unchanged += 1
        return page

    # course code
    def upsert_code(self, course_page: dict, data: CodeData) -> dict | None:
        """A page per source file: the code shown in a code block, and the file itself uploaded
        as .txt (Notion's upload refuses .java, .c, .py and similar)."""
        if not self.cfg.code_ds:
            return None
        pages = self.index(self.cfg.code_ds)
        page = pages.get(data.sid)
        lecture = self.index(self.cfg.lectures_ds).get(data.lecture_sid) if data.lecture_sid else None
        props = {
            "Name": n.title(data.name),
            "Group": n.rich_text(data.group),
            "Language": n.select(code_language(data.file) or "plain text"),
            "Date": n.date(data.date),
            "Source URL": n.url(data.source_url),
            "Lecture": n.relation(*([lecture["id"]] if lecture else [])),
        }
        if page is not None:
            same = (n.get_text(page, "Content hash") == data.version
                    and n.get_text(page, "Name") == data.name and n.get_text(page, "Group") == data.group
                    and n.get_select(page, "Language") == props["Language"]["select"]["name"]
                    and bool(lecture) == bool((page["properties"].get("Lecture") or {}).get("relation")))
            if same:
                self.report.unchanged += 1
                return page
        if self.notion.dry_run:
            self.report.notes.append(f"[dry-run] code {self.label(course_page)} / {data.group} / {data.name}")
            return page
        content = data.fetch()
        props.update({"Content hash": n.rich_text(data.version), "Last synced": self.stamp})
        if page is None:
            props.update({"Course": n.relation(course_page["id"]), "Source": n.select(self.SOURCE),
                          "Source ID": n.rich_text(data.sid)})
            page = self.notion.create_page(self.cfg.code_ds, props, self.skeleton(["Code"], data.source_url))
            pages[data.sid] = page
            self.report.created += 1
        else:
            self.notion.update_page(page["id"], props)
            page["properties"].update(props)
            self.report.updated += 1
        self.write_toggle(page["id"], "Code", self.code_page_blocks(data.file, content))
        return page

    def code_page_blocks(self, filename: str, data: bytes) -> list[dict]:
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            text = data.decode("latin-1")
        out = []
        if len(data) <= MAX_CODE_CHARS:
            out += code_blocks(filename, text.replace("\r\n", "\n"), code_language(filename) or "plain text")[1:]
        else:
            out.append(paragraph(text_item(f"{filename} is too long to show here; open the .txt copy below.",
                                           color="gray")))
        txt_name = filename if filename.lower().endswith(".txt") else f"{filename}.txt"
        try:
            upload_id = self.notion.upload_file(txt_name, "text/plain", data)
        except Exception as exc:
            log.warning("Notion would not take %s: %s", txt_name, exc)
            upload_id = None
        if upload_id:
            out.append(uploaded_file_block(upload_id, txt_name, "text/plain"))
        return out

    def refresh_source_link(self, page_id: str, url: str) -> None:
        """Point the page's Source section ("CourseWorks · Open original") at a new link."""
        if self.notion.dry_run:
            return
        blocks = self.notion.list_children(page_id)
        for i, b in enumerate(blocks[:-1]):
            if b.get("type") == "heading_2" and n._plain(b["heading_2"].get("rich_text")) == "Source":
                nxt = blocks[i + 1]
                if nxt.get("type") == "paragraph":
                    line = source_section(self.SOURCE, url)[1]
                    self.notion.update_block(nxt["id"], {"paragraph": line["paragraph"]})
                return

    @staticmethod
    def file_blocks(files: list[tuple[str, str]]) -> list[dict]:
        if not files:
            return [paragraph(text_item("No files linked.", color="gray"))]
        return [{"object": "block", "type": "bulleted_list_item",
                 "bulleted_list_item": {"rich_text": [text_item(label, link=link)]}}
                for label, link in files]

    # page body
    def skeleton(self, toggles: list[str], source_url: str | None) -> list[dict]:
        """Synced content lives inside toggle headings so it can be replaced later without
        touching anything you write under "My notes"."""
        return [
            *(heading(2, t, toggleable=True) for t in toggles),
            *source_section(self.SOURCE, source_url),
            heading(2, "My notes"),
            paragraph(),
        ]

    def find_toggle(self, page_id: str, heading_text: str) -> dict | None:
        return next((b for b in self.notion.list_children(page_id)
                     if b.get("type") == "heading_2"
                     and b["heading_2"].get("is_toggleable")
                     and n._plain(b["heading_2"].get("rich_text")) == heading_text), None)

    # instructions: [anchor line][this source's content][From email sections, kept]
    @property
    def anchor_text(self) -> str:
        return f"Synced from {self.SOURCE}"

    def write_instructions(self, page_id: str, data: AssignmentData) -> None:
        """Replace this source's part of the Instructions section, keeping any "From email"
        sections the Gmail sync added below it."""
        if self.notion.dry_run:
            return
        toggle = self.find_toggle(page_id, "Instructions")
        if toggle is None:
            self.report.warnings.append(f"Page {page_id}: 'Instructions' toggle not found.")
            return
        blocks = data.blocks + self.file_section(data.files)
        children = self.notion.list_children(toggle["id"]) if toggle.get("has_children") else []
        cut = next((i for i, b in enumerate(children) if is_email_marker(b)), len(children))
        own = children[:cut]
        anchor = own[0] if own and block_plain(own[0]) == self.anchor_text else None
        for child in own:
            if child is not anchor:
                self.notion.delete_block(child["id"])
        if anchor:
            self.notion.append_children(toggle["id"], blocks, after=anchor["id"])
        else:
            self.notion.append_children(toggle["id"], [paragraph(text_item(self.anchor_text, color="gray")),
                                                       *blocks])

    def file_section(self, files: list[Attachment]) -> list[dict]:
        if not files:
            return []
        out = [heading3("Files")]
        for f in files:
            out.extend(self.upload_or_link(f))
        return out

    def upload_or_link(self, f: Attachment) -> list[dict]:
        """Blocks for one file: an uploaded copy, a code block for source files, or a link."""
        fallback = paragraph(text_item(f"{f.name}: "), text_item("open", link=f.link)) if f.link else \
            paragraph(text_item(f"{f.name} (not available)", color="gray"))
        if f.fetch is None:
            return [fallback]
        try:
            fetched = f.fetch()
        except Exception as exc:
            log.warning("Could not download %s: %s", f.name, exc)
            return [fallback]
        if not fetched:
            return [fallback]
        data, filename, mime = fetched
        language = code_language(filename)
        if language and len(data) <= MAX_CODE_CHARS:
            try:
                return code_blocks(filename, data.decode("utf-8"), language)
            except UnicodeDecodeError:
                pass
        if len(data) > n.MAX_UPLOAD_BYTES:
            return [paragraph(text_item(f"{filename} ({len(data) / 1048576:.1f} MB) is over Notion's 5 MB limit. "),
                              *([text_item("Open the original", link=f.link)] if f.link else []))]
        try:
            upload_id = self.notion.upload_file(filename, MIME_ALIASES.get(mime, mime), data)
        except Exception as exc:
            log.warning("Notion would not take %s: %s", filename, exc)
            return [fallback]
        return [uploaded_file_block(upload_id, filename, MIME_ALIASES.get(mime, mime))] if upload_id else [fallback]

    def write_toggle(self, page_id: str, heading_text: str, blocks: list[dict]) -> None:
        """Replace the children of one of the page's toggle headings."""
        if self.notion.dry_run:
            return
        target = self.find_toggle(page_id, heading_text)
        if target is None:
            self.report.warnings.append(
                f"Page {page_id}: '{heading_text}' toggle not found, content not refreshed.")
            return
        if target.get("has_children"):
            for child in self.notion.list_children(target["id"]):
                self.notion.delete_block(child["id"])
        self.notion.append_children(target["id"], blocks)
