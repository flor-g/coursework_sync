"""Ed Discussion → the assignment and exam pages they clarify.

For each course on Ed, threads are matched to the course's assignments and exams by their Ed
category, subcategory and title ("HW3", "Homework 3", "Midterm 1" ...). A matched thread counts as
a clarification when course staff wrote it, or when staff answered or commented on it (or endorsed
an answer). Those go into a "Clarifications from Ed" section on the assignment's page, newest
first: the post or question, then the staff replies, with a link to the thread.

Student-only discussion is left out. A page is rewritten only when its threads change on Ed.
"""

from __future__ import annotations

import logging
import re
from datetime import timedelta

from . import notion as n
from .blocks import heading, paragraph, text_item
from .ed import EdClient, document_blocks, is_staff
from .email_analysis import KnownAssignment, match_known
from .sync_common import BaseSync, content_hash, course_key, parse_dt

log = logging.getLogger(__name__)

TOGGLE = "Clarifications from Ed"
MAX_BODY_BLOCKS = 25
ED_COURSE_LINK = re.compile(r"edstem\.org/(?:\w+/)?courses/(\d+)")


class EdSync(BaseSync):
    SOURCE = "Ed"

    def __init__(self, cfg, ed: EdClient, notion_client, now=None, report=None, course_list=None):
        super().__init__(cfg, notion_client, now, report)
        self.ed = ed
        if course_list is None:
            from .course_list import load

            course_list = load()
        self.aliases = {c.key: c.aliases for c in course_list.courses}
        self._ed_courses: list[dict] | None = None
        self._details: dict = {}

    # --- courses --------------------------------------------------------------------
    def run(self, course_pages: list[dict] | None = None):
        for course in self.active_courses(course_pages):
            label = self.label(course)
            try:
                ed_id = self.resolve(course)
                if ed_id:
                    self.sync_course(course, ed_id)
            except Exception as exc:
                log.exception("Ed sync failed for %s", label)
                self.report.errors.append(f"{label} (Ed): {exc}")
        return self.report

    def ed_courses(self) -> list[dict]:
        if self._ed_courses is None:
            self._ed_courses = self.ed.courses()
        return self._ed_courses

    def resolve(self, course: dict) -> str | None:
        """The course's Ed id: from "Ed ID", an Ed link in "Ed URL", or your Ed courses by code."""
        pinned = n.get_text(course, "Ed ID").strip()
        if pinned.isdigit():
            return pinned
        m = ED_COURSE_LINK.search(n.get_url(course, "Ed URL") or "")
        found = m.group(1) if m else None
        if not found:
            key = course_key(n.get_text(course, "Code"))
            matches = [c for c in self.ed_courses()
                       if key and key in course_key(f"{c.get('code', '')} {c.get('name', '')}")]
            if not matches:
                return None
            best = max(matches, key=lambda c: (str(c.get("status", "")).lower() == "active",
                                               str(c.get("year", "")), c.get("id", 0)))
            found = str(best["id"])
            self.report.notes.append(f"{self.label(course)}: linked to Ed course {found} ({best.get('code')}).")
        changes = {"Ed ID": n.rich_text(found)}
        current = n.get_url(course, "Ed URL") or ""
        if not current or "external_tools" in current:   # a CourseWorks launch link → the Ed page itself
            changes["Ed URL"] = n.url(self.ed.course_url(found))
        self.notion.update_page(course["id"], changes)
        course.setdefault("properties", {}).update(changes)
        return found

    # --- threads → assignments -----------------------------------------------------------
    def sync_course(self, course: dict, ed_id: str) -> None:
        since = self.now - timedelta(days=self.cfg.announcements_lookback_days)
        threads, users = self.ed.threads(ed_id, since)
        pages = {n.get_text(p, "Name"): p for p in self.notion.query_all(
            self.cfg.assignments_ds, {"property": "Course", "relation": {"contains": course["id"]}})
            if n.get_text(p, "Name")}
        known = [KnownAssignment(name, n.get_date(p, "Due")) for name, p in pages.items()]
        aliases = self.aliases.get(course_key(n.get_text(course, "Code")), ())
        by_assignment: dict[str, list[dict]] = {}
        for t in threads:
            if not self.may_clarify(t, users):
                continue
            for name in self.match(t, known, aliases):
                by_assignment.setdefault(name, []).append(t)
        for name, page in pages.items():
            matched = by_assignment.get(name, [])
            # Ed bumps updated_at on activity that changes nothing (views), so fingerprint what
            # matters: replies, title and post text.
            signature = content_hash("ed-v2", *(f"{t.get('id')}|{t.get('reply_count')}|{t.get('title')}|"
                                                f"{content_hash(t.get('content') or '')}" for t in matched))
            if n.get_text(page, "Ed hash") == signature:
                continue
            if not matched and self.find_toggle(page["id"], TOGGLE) is None:
                self.notion.update_page(page["id"], {"Ed hash": n.rich_text(signature)})
                continue
            if self.notion.dry_run:
                self.report.notes.append(f"[dry-run] {self.label(course)} / {name}: {len(matched)} Ed thread(s)")
                continue
            count = self.write_clarifications(page, name, ed_id, matched)
            self.notion.update_page(page["id"], {"Ed hash": n.rich_text(signature), "Last synced": self.stamp})
            self.report.updated += 1
            if count:
                self.report.notes.append(f"{self.label(course)} / {name}: {count} clarification(s) from Ed.")

    @staticmethod
    def match(thread: dict, known: list[KnownAssignment], aliases=()) -> list[str]:
        """The assignments a thread is about: its title decides if it names any; otherwise its
        subcategory, then its category ("Assignments / P4 - Stacks", "Homework / Homework 4")."""
        for field in ("title", "subcategory", "subsubcategory", "category"):
            names = match_known(str(thread.get(field) or ""), known, aliases)
            if names:
                return names
        return []

    @staticmethod
    def may_clarify(thread: dict, users: dict) -> bool:
        """Worth opening: staff wrote it, or someone replied (staff may have)."""
        if is_staff(users.get(thread.get("user_id"))):
            return True
        replies = thread.get("reply_count")
        return replies is None or replies > 0

    def detail(self, thread_id) -> tuple[dict, dict]:
        if thread_id not in self._details:
            self._details[thread_id] = self.ed.thread(thread_id)
        return self._details[thread_id]

    def write_clarifications(self, page: dict, name: str, ed_id: str, threads: list[dict]) -> int:
        blocks, count = [], 0
        for t in sorted(threads, key=lambda t: t.get("created_at") or "", reverse=True):
            thread, users = self.detail(t["id"])
            if not thread:
                continue
            section = self.thread_blocks(thread, users, ed_id)
            if section:
                blocks += section
                count += 1
        intro = paragraph(text_item(
            f"Staff posts and staff-answered questions about {name} on Ed Discussion, newest first. "
            f"Updated {self.now:%b} {self.now.day}.", color="gray"))
        body = [intro, *blocks] if blocks else [paragraph(text_item(
            f"No staff clarifications about {name} on Ed yet.", color="gray"))]
        self.ensure_toggle(page["id"])
        self.write_toggle(page["id"], TOGGLE, body)
        return count

    def ensure_toggle(self, page_id: str) -> None:
        if self.find_toggle(page_id, TOGGLE) is not None:
            return
        instructions = self.find_toggle(page_id, "Instructions")
        self.notion.append_children(page_id, [heading(2, TOGGLE, toggleable=True)],
                                    after=instructions["id"] if instructions else None)

    def thread_blocks(self, thread: dict, users: dict, ed_id: str) -> list[dict]:
        author = users.get(thread.get("user_id"))
        staff_author = is_staff(author)
        replies = []

        def collect(items):
            for r in items or []:
                replies.append(r)
                collect(r.get("comments"))

        collect(thread.get("answers"))
        collect(thread.get("comments"))
        accepted = thread.get("accepted_id")
        official = [r for r in replies
                    if is_staff(users.get(r.get("user_id"))) or r.get("is_endorsed") or r.get("id") == accepted]
        if not staff_author and not official:
            return []
        kind = {"announcement": "Announcement", "question": "Question"}.get(thread.get("type"), "Post")
        who = self.person(author, thread.get("is_anonymous"))
        url = self.ed.thread_url(ed_id, thread.get("id"))
        number = f"#{thread['number']} " if thread.get("number") else ""
        out = [
            heading(3, f"{number}{thread.get('title') or 'Ed thread'}"),
            paragraph(text_item(f"{kind} {'by' if staff_author else 'from'} {who} · {self.day(thread.get('created_at'))} · ",
                                color="gray"), text_item("open on Ed", link=url)),
            *document_blocks(thread.get("content", ""), MAX_BODY_BLOCKS),
        ]
        out[0]["heading_3"]["rich_text"][0]["text"]["link"] = {"url": url}
        for r in sorted(official, key=lambda r: r.get("created_at") or ""):
            replier = users.get(r.get("user_id"))
            what = "Answer" if r.get("type") == "answer" else "Comment"
            tag = "" if is_staff(replier) else (" (accepted)" if r.get("id") == accepted else " (endorsed by staff)")
            out.append(paragraph(text_item(f"{what} from {self.person(replier, r.get('is_anonymous'))}{tag} · "
                                           f"{self.day(r.get('created_at'))}", bold=True)))
            out += document_blocks(r.get("content", ""), MAX_BODY_BLOCKS)
        out.append({"object": "block", "type": "divider", "divider": {}})
        return out

    @staticmethod
    def person(user: dict | None, anonymous: bool | None = False) -> str:
        if user and is_staff(user):
            return f"{user.get('name') or 'course staff'} (staff)"
        return "a student"

    def day(self, iso: str | None) -> str:
        dt = parse_dt(iso, self.tz)
        return f"{dt.astimezone(self.tz):%b} {dt.astimezone(self.tz).day}" if dt else "?"
