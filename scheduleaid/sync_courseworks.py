"""CourseWorks → Notion (+ Google Calendar): assignments, announcements, lecture slides and code."""

from __future__ import annotations

import html as html_lib
import logging
import mimetypes
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup

from . import notion as n
from .blocks import html_to_blocks, paragraph, text_item
from .http import ApiError
from .notion import MAX_UPLOAD_BYTES
from . import materials as mat
from .sync_common import (
    AssignmentData, Attachment, BaseSync, CodeData, LectureData, content_hash, course_key, file_format,
    human_dt, parse_dt, to_local_iso,
)

__all__ = ["CourseWorksSync", "human_dt", "to_local_iso"]

log = logging.getLogger(__name__)

OLD_ASSIGNMENT_CUTOFF = timedelta(days=14)
NO_ONLINE_SUBMISSION = {"none", "on_paper", "not_graded", "external_tool"}
SUBMITTABLE = {"online_upload", "online_quiz", "external_tool", "online_url", "media_recording",
               "student_annotation", "discussion_topic"}
FILE_LINK = re.compile(r"/courses/(\d+)/files/(\d+)")
GRADEBOOK_NAME = re.compile(
    r"\b(raw score|final score|final grade|class rank|late days|attendance|absences)\b", re.I)


# --- pure helpers (unit-tested) ---------------------------------------------
def match_course(code: str, canvas_courses: list[dict]) -> tuple[dict | None, list[dict]]:
    """Find the CourseWorks course for a code like "COMS W3134". Returns (best, all matches)."""
    key = course_key(code)
    if not key:
        return None, []
    matches = [c for c in canvas_courses
               if key in course_key(f"{c.get('course_code', '')} {c.get('name', '')}")]
    if not matches:
        return None, []
    # Prefer courses whose own course_code starts with the key (lectures over cross-listings),
    # then the most recently created (highest id).
    best = max(matches, key=lambda c: (course_key(c.get("course_code", "")).startswith(key), c["id"]))
    return best, matches


def linked_files(description: str) -> list[tuple[str, str, str, str | None, str]]:
    """CourseWorks files linked from an assignment description:
    [(course_id, file_id, link text, verifier, href)], each file once."""
    found, seen = [], set()
    for a in BeautifulSoup(description or "", "html.parser").find_all("a", href=True):
        href = html_lib.unescape(a["href"])
        m = FILE_LINK.search(href)
        if not m or m.group(2) in seen:
            continue
        seen.add(m.group(2))
        verifier = re.search(r"verifier=([\w-]+)", href)
        found.append((m.group(1), m.group(2), a.get_text(" ", strip=True) or f"file {m.group(2)}",
                      verifier.group(1) if verifier else None, href))
    return found


def uses_gradescope(assignment: dict) -> bool:
    """Submitted through Gradescope, or the description sends you there."""
    tool = (assignment.get("external_tool_tag_attributes") or {}).get("url") or ""
    return "gradescope" in tool.lower() or "gradescope" in (assignment.get("description") or "").lower()


def is_gradebook_only(assignment: dict) -> bool:
    """Gradebook columns (scores, rank, attendance) that CourseWorks lists as assignments."""
    if assignment.get("omit_from_final_grade") or GRADEBOOK_NAME.search(assignment.get("name") or ""):
        return True
    types = set(assignment.get("submission_types") or [])
    return not assignment.get("due_at") and not (types & SUBMITTABLE)


def submission_state(assignment: dict) -> str | None:
    """'submitted', 'graded' (offline work marked graded), 'excused', or None."""
    sub = assignment.get("submission") or {}
    if sub.get("excused"):
        return "excused"
    if sub.get("submitted_at") or sub.get("workflow_state") in ("submitted", "pending_review"):
        return "submitted"
    types = set(assignment.get("submission_types") or [])
    if sub.get("workflow_state") == "graded" and types & NO_ONLINE_SUBMISSION:
        return "graded"
    return None


def initial_status(assignment: dict, due_iso: str | None, now: datetime, tz: ZoneInfo) -> str:
    state = submission_state(assignment)
    if state == "excused":
        return "Done"
    if state:
        return "Submitted"
    due = parse_dt(due_iso, tz)
    missing = (assignment.get("submission") or {}).get("missing")
    if due and due < now - OLD_ASSIGNMENT_CUTOFF and not missing:
        return "Done"  # first import of long-past work: keep the to-do list clean
    return "Not started"


def advance_status(current: str | None, assignment: dict) -> str | None:
    """New status if CourseWorks shows progress; never moves a status backwards."""
    state = submission_state(assignment)
    if state == "excused" and current != "Done":
        return "Done"
    if state in ("submitted", "graded") and current not in ("Submitted", "Done"):
        return "Submitted"
    return None


def guess_type(assignment: dict) -> str:
    name = (assignment.get("name") or "").lower()
    types = set(assignment.get("submission_types") or [])
    if "online_quiz" in types or assignment.get("is_quiz_assignment") or re.search(r"\bquiz", name):
        return "Quiz"
    if re.search(r"\bproject\b", name):  # before exams: "Final Project" is a project
        return "Project"
    if re.search(r"\b(exam|midterm|final)\b", name):
        return "Exam"
    if re.search(r"\breading", name):
        return "Reading"
    return "Homework"


# --- sync -----------------------------------------------------------------
class CourseWorksSync(BaseSync):
    SOURCE = "CourseWorks"

    def __init__(self, cfg, canvas, notion_client, now=None, report=None):
        super().__init__(cfg, notion_client, now, report)
        self.canvas = canvas

    def run(self, course_pages: list[dict] | None = None):
        canvas_courses = self.canvas.active_courses()
        for course_page in self.active_courses(course_pages):
            label = self.label(course_page)
            try:
                course = self.resolve_course(course_page, canvas_courses, label)
                if course is None:
                    continue
                gradescope = self.link_tools(course_page, course)
                self.sync_assignments(course_page, course, label, gradescope)
                self.sync_announcements(course_page, course)
                self.sync_materials(course_page, course, label)
            except Exception as exc:  # keep going with the other courses
                log.exception("Failed to sync %s", label)
                self.report.errors.append(f"{label} (CourseWorks): {exc}")
        return self.report

    def resolve_course(self, course_page: dict, canvas_courses: list[dict], label: str) -> dict | None:
        pinned = n.get_text(course_page, "CourseWorks ID").strip()
        if pinned:
            found = next((c for c in canvas_courses if str(c.get("id")) == pinned), None)
            return found or {"id": int(pinned), "name": label}
        best, matches = match_course(n.get_text(course_page, "Code"), canvas_courses)
        if best is None:
            self.report.warnings.append(
                f"{label}: no active CourseWorks course matches. Set its CourseWorks ID in Notion.")
            return None
        if len(matches) > 1:
            others = ", ".join(f"{c['id']} ({c.get('course_code')})" for c in matches if c is not best)
            self.report.warnings.append(
                f"{label}: several CourseWorks courses match; picked {best['id']} "
                f"({best.get('course_code')}). Others: {others}. Change CourseWorks ID in Notion if wrong.")
        self.notion.update_page(course_page["id"], {
            "CourseWorks ID": n.rich_text(str(best["id"])),
            "CourseWorks URL": n.url(self.canvas.course_url(best["id"])),
        })
        self.report.notes.append(f"{label}: linked to CourseWorks course {best['id']} ({best.get('course_code')}).")
        return best

    def link_tools(self, course_page: dict, course: dict) -> str | None:
        """Save the course's Gradescope and Ed links (from its CourseWorks tabs) if not set yet.
        Returns the Gradescope link."""
        gradescope = n.get_url(course_page, "Gradescope URL")
        if gradescope and n.get_url(course_page, "Ed URL"):
            return gradescope
        try:
            tabs = self.canvas.tabs(course["id"])
        except Exception as exc:
            log.warning("Could not read course tabs: %s", exc)
            return gradescope
        changes = {}
        for tab in tabs:
            label = (tab.get("label") or "").lower()
            url = tab.get("html_url") or ""
            url = url if url.startswith("http") else self.cfg.canvas_base_url + url
            url = url.replace("?display=borderless", "")
            if "gradescope" in label and not gradescope:
                gradescope = url
                changes["Gradescope URL"] = n.url(url)
            elif "ed discussion" in label and not n.get_url(course_page, "Ed URL"):
                changes["Ed URL"] = n.url(url)
        if changes:
            self.notion.update_page(course_page["id"], changes)
            course_page.setdefault("properties", {}).update(changes)
        return gradescope

    def sync_assignments(self, course_page: dict, course: dict, label: str,
                         gradescope: str | None = None) -> None:
        for a in self.canvas.assignments(course["id"]):
            if a.get("published") is False:
                continue
            if is_gradebook_only(a):
                self.report.skipped += 1
                continue
            try:
                self.upsert_assignment(course_page, self.to_data(a, gradescope))
            except Exception as exc:
                log.exception("Failed to sync assignment %s", a.get("id"))
                self.report.errors.append(f"{label} / {a.get('name')}: {exc}")

    def to_data(self, a: dict, gradescope: str | None = None) -> AssignmentData:
        sid = f"canvas:assignment:{a['id']}"
        due = to_local_iso(a.get("due_at"), self.tz)
        html = a.get("description") or ""
        existing = self.index(self.cfg.assignments_ds).get(sid)
        current_status = n.get_select(existing, "Status") if existing else None
        files = [Attachment(name=text, link=href if href.startswith("http") else self.cfg.canvas_base_url + href,
                            fetch=lambda c=cid, f=fid, v=verifier, t=text: self.fetch_file(c, f, v, t))
                 for cid, fid, text, verifier, href in linked_files(html)]
        blocks = html_to_blocks(html, self.cfg.canvas_base_url)
        on_gradescope = uses_gradescope(a)
        if on_gradescope and gradescope:
            blocks.insert(0, paragraph(text_item("Questions and submission are on Gradescope: ", bold=True),
                                       text_item("open in Gradescope", link=gradescope)))
        if not blocks:
            blocks = [paragraph(text_item("No instructions on CourseWorks yet.", color="gray"))]
        return AssignmentData(
            sid=sid,
            name=a.get("name") or "Untitled assignment",
            due=due,
            source_url=a.get("html_url"),
            blocks=blocks,
            hash=content_hash("files-v3", html, *([gradescope] if on_gradescope and gradescope else [])),
            files=files,
            type=guess_type(a),
            initial_status=initial_status(a, due, self.now, self.tz),
            points=a.get("points_possible"),
            manage_points=True,
            advanced_status=advance_status(current_status, a) if existing else None,
        )

    def fetch_file(self, course_id: str, file_id: str, verifier: str | None, name: str):
        """Download a CourseWorks file linked from an assignment. None → keep it as a link."""
        meta = None
        for scope in (None, course_id):
            try:
                meta = self.canvas.file(file_id, course_id=scope, verifier=verifier)
                break
            except ApiError as exc:
                if exc.status not in (401, 403, 404):
                    raise
        if not meta or not meta.get("url"):
            return None
        if (meta.get("size") or 0) > MAX_UPLOAD_BYTES:
            return None
        filename = meta.get("display_name") or meta.get("filename") or name
        mime = meta.get("content-type") or mimetypes.guess_type(filename)[0] or "application/octet-stream"
        return self.canvas.download(meta["url"]), filename, mime

    def sync_announcements(self, course_page: dict, course: dict) -> None:
        updates = self.index(self.cfg.updates_ds)
        since = self.now - timedelta(days=self.cfg.announcements_lookback_days)
        for ann in self.canvas.announcements(course["id"], since):
            sid = f"canvas:announcement:{ann['id']}"
            title = ann.get("title") or "Announcement"
            html = ann.get("message") or ""
            chash = content_hash(title, html)
            blocks = html_to_blocks(html, self.cfg.canvas_base_url) or [
                paragraph(text_item("Empty announcement.", color="gray"))]
            page = updates.get(sid)
            if page is None:
                props = {
                    "Name": n.title(title),
                    "Course": n.relation(course_page["id"]),
                    "Date": n.date(to_local_iso(ann.get("posted_at"), self.tz)),
                    "Category": n.select("Announcement"),
                    "Source": n.select(self.SOURCE),
                    "Source URL": n.url(ann.get("html_url")),
                    "Source ID": n.rich_text(sid),
                    "Content hash": n.rich_text(chash),
                    "Last synced": self.stamp,
                }
                page = self.notion.create_page(self.cfg.updates_ds, props,
                                               self.skeleton(["Message"], ann.get("html_url")))
                updates[sid] = page
                self.write_toggle(page["id"], "Message", blocks)
                self.report.created += 1
            elif n.get_text(page, "Content hash") != chash:
                self.notion.update_page(page["id"], {
                    "Name": n.title(title), "Content hash": n.rich_text(chash), "Last synced": self.stamp})
                self.write_toggle(page["id"], "Message", blocks)
                self.report.updated += 1
            else:
                self.report.unchanged += 1

    # --- lecture slides and code ----------------------------------------------------
    def sync_materials(self, course_page: dict, course: dict, label: str) -> None:
        """Slides → Lecture Notes, source files → Code. Read from the Files area when students can
        see it, and from the files the syllabus links."""
        cid = str(course["id"])
        seen: set[str] = set()
        self._current_files: set[str] = set()
        try:
            listing = self.canvas.files(cid)
        except ApiError as exc:
            if exc.status not in (401, 403, 404):
                raise
            listing = None
        try:
            syllabus = self.canvas.course(cid, ["syllabus_body"]).get("syllabus_body") or ""
        except ApiError as exc:
            if exc.status not in (401, 403, 404):
                raise
            syllabus = ""
        # Every file the course links right now: a page for a file outside this set was replaced.
        self._current_files = {str(f.get("id")) for f in listing or []} | {
            link.file_id for row in mat.syllabus_rows(syllabus, self.now) for link in row.links}
        if listing:
            try:
                folders = {f["id"]: f.get("full_name") or "" for f in self.canvas.folders(cid)}
            except ApiError:
                folders = {}
            self.materials_from_folders(course_page, cid, listing, folders, seen)
        if syllabus:
            self.materials_from_syllabus(course_page, cid, syllabus, seen)

    def file_url(self, cid: str, fid) -> str:
        return f"{self.cfg.canvas_base_url}/courses/{cid}/files/{fid}"

    def lecture_from_file(self, course_page: dict, cid: str, meta: dict, name: str, number: int | None,
                          day: str | None) -> str:
        sid = f"cw:{cid}:file:{meta['id']}"
        display = meta.get("display_name") or name
        url = self.file_url(cid, meta["id"])
        lectures = self.index(self.cfg.lectures_ds)
        if sid not in lectures:
            old = self.replaced(lectures, cid, lambda p: n.get_text(p, "Name") == name
                                and (n.get_date(p, "Date") or "")[:10] == (day or ""))
            if old:
                self.adopt_lecture(lectures, old, sid, meta)
        self.upsert_lecture(course_page, LectureData(
            sid=sid, name=name, number=number, date=day, original_url=url, source_url=url,
            files=[(f"{display} (original, on CourseWorks)", url)], format=file_format(display)))
        return sid

    def code_from_file(self, course_page: dict, cid: str, meta: dict, group: str, day: str | None,
                       lecture_sid: str | None, name: str | None = None) -> None:
        display = meta.get("display_name") or str(meta["id"])
        version = f"{meta.get('updated_at') or meta.get('modified_at') or ''}|{meta.get('size')}"
        url = self.file_url(cid, meta["id"])
        if display.lower().endswith(".zip"):
            prefix = f"cw:{cid}:file:{meta['id']}:"
            existing = [p for sid, p in self.index(self.cfg.code_ds).items() if sid.startswith(prefix)] \
                if self.cfg.code_ds else []
            if existing and all(n.get_text(p, "Content hash") == version for p in existing):
                self.report.unchanged += len(existing)
                return
            if self.notion.dry_run or not self.cfg.code_ds:
                return
            members = mat.zip_code_members(self.canvas.download(meta["url"])) if meta.get("url") else []
            for path, data in members:
                self.upsert_code(course_page, CodeData(
                    sid=f"cw:{cid}:file:{meta['id']}:{path}", name=path.rsplit("/", 1)[-1],
                    group=f"{group} · {display}", date=day, source_url=url, version=version,
                    fetch=lambda d=data: d, lecture_sid=lecture_sid))
            return
        sid = f"cw:{cid}:file:{meta['id']}"
        if self.cfg.code_ds:
            pages = self.index(self.cfg.code_ds)
            if sid not in pages:
                old = self.replaced(pages, cid, lambda p: n.get_text(p, "Name") == (name or display)
                                    and n.get_text(p, "Group") == group)
                if old:
                    self.rekey(pages, old, sid)
        self.upsert_code(course_page, CodeData(
            sid=sid, name=name or display, group=group, date=day,
            source_url=url, version=version, lecture_sid=lecture_sid, filename=display,
            fetch=lambda u=meta.get("url"): self.canvas.download(u)))

    # --- a file replaced on CourseWorks keeps its Notion page ---------------------------------
    def replaced(self, pages: dict[str, dict], cid: str, same) -> str | None:
        """The Source ID of a page for a file this course no longer links that matches `same`
        (the professor uploaded a new version, which CourseWorks gives a new file id)."""
        prefix = f"cw:{cid}:file:"
        for sid, page in pages.items():
            if not sid.startswith(prefix):
                continue
            file_id = sid[len(prefix):].split(":", 1)[0]
            if ":" not in sid[len(prefix):] and file_id not in self._current_files and same(page):
                return sid
        return None

    def rekey(self, pages: dict[str, dict], old_sid: str, new_sid: str) -> dict:
        page = pages.pop(old_sid)
        self.notion.update_page(page["id"], {"Source ID": n.rich_text(new_sid)})
        page["properties"]["Source ID"] = n.rich_text(new_sid)
        pages[new_sid] = page
        return page

    def adopt_lecture(self, lectures: dict[str, dict], old_sid: str, new_sid: str, meta: dict) -> None:
        page = self.rekey(lectures, old_sid, new_sid)
        name = n.get_text(page, "Name")
        self.report.notes.append(f"{name}: CourseWorks has a new version of the file; the lecture page now points to it.")
        if n.get_select(page, "Conversion status") not in ("Converted", "Needs review") or self.notion.dry_run:
            return
        self.notion.update_page(page["id"], {"Conversion status": n.select("Pending")})
        notes = self.find_toggle(page["id"], "Notes")
        first = self.notion.list_children(notes["id"])[:1] if notes and notes.get("has_children") else []
        if first and first[0].get("type") == "paragraph":
            uploaded = to_local_iso(meta.get("updated_at") or meta.get("created_at"), self.tz)
            when = f" on {datetime.fromisoformat(uploaded):%b} {datetime.fromisoformat(uploaded).day}" if uploaded else ""
            warning = text_item(f"These notes are from an earlier version of the slides. A new version was "
                                f"uploaded{when}; click Conversion to rewrite the notes from it. ", color="red")
            rest = [r for r in first[0]["paragraph"]["rich_text"] if "earlier version" not in n._plain([r])]
            self.notion.update_block(first[0]["id"], {"paragraph": {"rich_text": [warning, *rest]}})

    def materials_from_folders(self, course_page: dict, cid: str, listing: list[dict],
                               folders: dict, seen: set[str]) -> None:
        lectures: dict[int, str] = {}
        ordered = sorted(listing, key=lambda f: (f.get("created_at") or "", f.get("id")))
        for meta in ordered:
            folder = folders.get(meta.get("folder_id"), "")
            display = meta.get("display_name") or ""
            if mat.is_lecture_slides_in_folder(folder, display) and not meta.get("locked_for_user"):
                number = mat.lecture_number(folder)
                title = f"Lecture {number}" if number not in lectures else f"Lecture {number}: {mat.pretty_stem(display)}"
                sid = self.lecture_from_file(course_page, cid, meta, title, number,
                                             to_local_iso(meta.get("created_at"), self.tz)[:10]
                                             if meta.get("created_at") else None)
                lectures.setdefault(number, sid)
                seen.add(str(meta["id"]))
        # The same file is often uploaded once per section: keep one copy per name and size. When the
        # sections' versions differ, keep each, labeled with its section.
        kept: list[tuple[str, str, dict]] = []
        sizes: dict[tuple[str, str], set] = {}
        for meta in ordered:
            folder = folders.get(meta.get("folder_id"), "")
            display = meta.get("display_name") or ""
            if not mat.is_code_in_folder(display) or meta.get("locked_for_user"):
                continue
            seen.add(str(meta["id"]))
            group = mat.folder_group(folder)
            versions = sizes.setdefault((group, display), set())
            if meta.get("size") not in versions:
                versions.add(meta.get("size"))
                kept.append((folder, group, meta))
        for folder, group, meta in kept:
            display = meta.get("display_name") or ""
            label = display if len(sizes[(group, display)]) == 1 else f"{display} ({folder.rsplit('/', 1)[-1]})"
            day = to_local_iso(meta.get("created_at"), self.tz)[:10] if meta.get("created_at") else None
            self.code_from_file(course_page, cid, meta, group, day, lectures.get(mat.lecture_number(folder)),
                                name=label)

    def materials_from_syllabus(self, course_page: dict, cid: str, syllabus: str, seen: set[str]) -> None:
        lecture_no = 0
        for row in mat.syllabus_rows(syllabus, self.now):
            row_lecture = None
            metas = []
            for link in row.links:
                if link.file_id in seen:
                    continue
                seen.add(link.file_id)
                meta = self.file_meta(cid, link.file_id)
                if meta is None:
                    continue
                metas.append((link, meta))
            for link, meta in metas:
                display = meta.get("display_name") or ""
                if mat.is_lecture_material(display, link.text):
                    lecture_no += 1
                    sid = self.lecture_from_file(course_page, cid, meta, mat.lecture_title(display, link.text),
                                                 lecture_no, row.day.isoformat())
                    if row_lecture is None and not display.lower().startswith("recitation") \
                            and not link.text.lower().startswith("recitation"):
                        row_lecture = sid
            for link, meta in metas:
                display = meta.get("display_name") or ""
                if mat.is_code_file(display) or display.lower().endswith(".zip"):
                    self.code_from_file(course_page, cid, meta, row.label, row.day.isoformat(), row_lecture)

    def file_meta(self, cid: str, file_id: str) -> dict | None:
        for scope in (None, cid):
            try:
                meta = self.canvas.file(file_id, course_id=scope)
                return None if meta.get("locked_for_user") else meta
            except ApiError as exc:
                if exc.status not in (401, 403, 404):
                    raise
        return None
