"""Gmail → Notion, second half.

The Apps Script (apps_script/Code.gs) runs inside your Google account and copies each new course
email into Updates: subject, date, a link to Gmail, the email text in an "Email" toggle and the
attachments in an "Attachments" toggle. This sync reads every email that hasn't been summarized
yet and fills in a title, summary, category and importance flag, links it to the assignments it
mentions, flags due dates that disagree with Notion, and turns new to-dos into assignments.
"""

from __future__ import annotations

import logging
import mimetypes
import re
from datetime import timedelta
from email.utils import parseaddr

import requests

from . import notion as n
from .blocks import heading3, paragraph, text_item
from .email_analysis import Analysis, Email, KnownAssignment, RelatedRef, match_known
from .http import request_with_retry
from .sync_common import (
    EMAIL_MARKER, AssignmentData, Attachment, BaseSync, block_plain, content_hash, human_dt,
    is_date_only, is_email_marker, parse_dt, same_minute, slug,
)

log = logging.getLogger(__name__)

PENDING = {"and": [
    {"property": "Source", "select": {"equals": "Gmail"}},
    {"property": "Content hash", "rich_text": {"is_empty": True}},
]}
FILE_BLOCKS = ("file", "pdf", "image", "video", "audio")
MAX_COPIED_PARAGRAPHS = 80
STALE_TASK_AGE = timedelta(days=7)
MAX_PER_RUN = 80          # a large backlog is split across runs (GitHub jobs have a time limit)
GRADESCOPE_RECEIPT = re.compile(r"successfully submitted to (.+?)\s*$", re.I)


def download(url: str) -> bytes:
    """Notion-hosted files come with short-lived signed URLs that need no credentials."""
    return request_with_retry(requests.Session(), "GET", url, service="Download").content


def block_text(block: dict) -> str:
    body = block.get(block.get("type", ""), {}) or {}
    return n._plain(body.get("rich_text") or body.get("caption") or [])


class GmailSync(BaseSync):
    SOURCE = "Gmail"

    def __init__(self, cfg, notion_client, analyzer, now=None, report=None, fetch=download):
        super().__init__(cfg, notion_client, now, report)
        self.analyzer = analyzer
        self.fetch = fetch

    def run(self, course_pages: list[dict] | None = None):
        courses = {p["id"]: p for p in self.active_courses(course_pages)}
        pending = self.notion.query_all(self.cfg.updates_ds, PENDING)
        known_by_course: dict[str, dict[str, dict]] = {}
        if len(pending) > MAX_PER_RUN:
            self.report.notes.append(f"{len(pending)} emails waiting; summarizing {MAX_PER_RUN} now, "
                                     "the rest in the next runs.")
        for page in pending[:MAX_PER_RUN]:
            course_ids = [r["id"] for r in page["properties"].get("Course", {}).get("relation", [])]
            course_page = courses.get(course_ids[0]) if course_ids else None
            if course_page is None:
                continue  # archived or unlinked course
            try:
                if course_page["id"] not in known_by_course:
                    known_by_course[course_page["id"]] = self.course_assignments(course_page)
                self.process(page, course_page, known_by_course[course_page["id"]])
            except Exception as exc:
                log.exception("Failed to summarize email %s", page.get("id"))
                self.report.errors.append(f"{self.label(course_page)} / {n.get_text(page, 'Name')}: {exc}")
        failures = getattr(self.analyzer, "failures", [])
        if failures:
            self.report.warnings.append(
                f"Claude could not read {len(failures)} email(s) ({failures[0]}); subject-line rules were used.")
        return self.report

    def course_assignments(self, course_page: dict) -> dict[str, dict]:
        pages = self.notion.query_all(
            self.cfg.assignments_ds, {"property": "Course", "relation": {"contains": course_page["id"]}})
        return {n.get_text(p, "Name"): p for p in pages if n.get_text(p, "Name")}

    def toggle(self, top: list[dict], name: str) -> dict | None:
        return next((b for b in top if b.get("type") == "heading_2"
                     and b["heading_2"].get("is_toggleable")
                     and n._plain(b["heading_2"].get("rich_text")) == name), None)

    def read_email(self, page: dict) -> Email:
        """Rebuild the email from the page the Apps Script wrote."""
        top = self.notion.list_children(page["id"])
        email_toggle = self.toggle(top, "Email")
        if email_toggle is None:
            raise ValueError("the page has no 'Email' section")
        lines = [block_text(b) for b in self.notion.list_children(email_toggle["id"])]
        sender_line = next((line for line in lines if line.startswith("From: ")), "")
        body = [line for line in lines if line and line is not sender_line]
        name, addr = parseaddr(sender_line[len("From: "):])
        attachments = []
        self.attachment_blocks = []
        att_toggle = self.toggle(top, "Attachments")
        if att_toggle and att_toggle.get("has_children", True):
            for b in self.notion.list_children(att_toggle["id"]):
                if b.get("type") in FILE_BLOCKS:
                    body_ = b[b["type"]]
                    attachments.append(body_.get("name") or block_text(b) or "attachment")
                    self.attachment_blocks.append(b)
        message_id = n.get_text(page, "Source ID").removeprefix("gmail:")
        return Email(id=message_id, thread_id=message_id, subject=n.get_text(page, "Name"),
                     sender=name or addr, sender_email=addr.lower(),
                     date=parse_dt(n.get_date(page, "Date"), self.tz) or self.now,
                     text="\n\n".join(body), attachments=attachments)

    def process(self, page: dict, course_page: dict, known_pages: dict[str, dict]) -> None:
        email = self.read_email(page)
        label = self.label(course_page)
        known = [KnownAssignment(name, n.get_date(p, "Due")) for name, p in known_pages.items()]
        analysis = self.gradescope_receipt(email, known_pages)
        if analysis is None:
            analysis: Analysis = self.analyzer.analyze(label, known, email, self.tz)
        link = n.get_url(page, "Source URL")

        related_ids: list[str] = []
        warnings: list[str] = []
        for ref in analysis.related:
            assignment = known_pages.get(ref.name)
            if not assignment:
                continue
            related_ids.append(assignment["id"])
            current = n.get_date(assignment, "Due")
            if analysis.from_staff and ref.due_stated and not self.same_due(current, ref.due_stated):
                warnings.append(f"This email gives {human_dt(ref.due_stated, self.tz)} for {ref.name}; "
                                f"Notion has {human_dt(current, self.tz)}. Check which is right.")
        if warnings:
            analysis.important = True
            if analysis.category not in ("Deadline change", "Exam"):
                analysis.category = "Deadline change"

        # To-dos only from staff emails about the course; an undated to-do from an email more than
        # a week old is history, filed as Done.
        tasks = analysis.tasks if analysis.from_staff and analysis.category != "Other" else []
        stale = self.now - email.date > STALE_TASK_AGE
        for task in tasks:
            due = task.due or None
            last = parse_dt(due, self.tz)
            if last and is_date_only(due):
                last = last.replace(hour=23, minute=59)
            done = (last and last < self.now) or (not last and stale)
            created = self.upsert_assignment(course_page, AssignmentData(
                sid=f"gmail:{email.id}:{slug(task.name)}",
                name=task.name, due=due, source_url=link,
                blocks=[paragraph(text_item(analysis.summary or analysis.title)),
                        paragraph(text_item("From the email: "), text_item(email.subject, link=link))],
                hash=content_hash(task.name, due),
                type=task.type,
                initial_status="Done" if done else "Not started",
            ))
            known_pages[task.name] = created
            related_ids.append(created["id"])

        self.notion.update_page(page["id"], {
            "Name": n.title(analysis.title),
            "Category": n.select(analysis.category),
            "Important": n.checkbox(analysis.important),
            "Related assignment": n.relation(*dict.fromkeys(related_ids)),
            "Content hash": n.rich_text(f"summarized-by-{analysis.analyzer}"),
            "Last synced": self.stamp,
        })
        if analysis.category == "Assignment" and analysis.from_staff:
            for ref in analysis.related:
                if ref.name in known_pages:
                    self.copy_to_assignment(known_pages[ref.name]["id"], email, link)
        summary = [paragraph(text_item(analysis.summary))] if analysis.summary else []
        summary += [paragraph(text_item(w, color="red")) for w in warnings]
        self.write_toggle(page["id"], "Summary", summary or [
            paragraph(text_item("No summary.", color="gray"))])
        self.report.updated += 1

    def gradescope_receipt(self, email: Email, known_pages: dict[str, dict]) -> Analysis | None:
        """"Successfully submitted to HW 4" from Gradescope: mark that assignment Submitted.
        Handled without Claude."""
        sent_by_gradescope = "gradescope" in email.sender_email or "on gradescope]" in email.subject.lower()
        m = GRADESCOPE_RECEIPT.search(email.subject)
        if not (sent_by_gradescope and m):
            return None
        target = m.group(1).strip()
        names = [name for name in known_pages if name.lower() == target.lower()] or \
            match_known(target, [KnownAssignment(name, None) for name in known_pages])
        related = []
        for name in names[:1]:
            page = known_pages[name]
            related.append(RelatedRef(name))
            if n.get_select(page, "Status") in ("Not started", "In progress", None):
                self.notion.update_page(page["id"], {"Status": n.status("Submitted")})
                page.setdefault("properties", {})["Status"] = n.status("Submitted")
                self.report.notes.append(f"Gradescope receipt: marked {name} Submitted.")
        sent = human_dt(email.date.astimezone(self.tz).isoformat(), self.tz)
        return Analysis(title=f"Submitted {target} on Gradescope", category="Other",
                        summary=f"Gradescope confirmed your submission on {sent}.",
                        related=related, analyzer="gradescope-receipt")

    def copy_to_assignment(self, assignment_id: str, email: Email, link: str | None) -> None:
        """Put an assignment email (e.g. "[LAB4] Lab 4 released") and its attachments into the
        assignment's Instructions, under "From email: <subject>". Copied once per email."""
        if self.notion.dry_run:
            return
        toggle = self.find_toggle(assignment_id, "Instructions")
        if toggle is None:
            return
        marker = f"{EMAIL_MARKER}{email.subject}"[:200]
        existing = self.notion.list_children(toggle["id"]) if toggle.get("has_children") else []
        if any(is_email_marker(b) and block_plain(b) == marker for b in existing):
            return
        sent = human_dt(email.date.astimezone(self.tz).isoformat(), self.tz)
        blocks = [heading3(marker),
                  paragraph(text_item(f"Sent {sent} by {email.sender} · ", color="gray"),
                            *([text_item("open in Gmail", link=link)] if link else []))]
        paragraphs = [p.strip() for p in re.split(r"\n\s*\n", email.text) if p.strip()]
        for para in paragraphs[:MAX_COPIED_PARAGRAPHS]:
            blocks.append(paragraph(*(text_item(para[i:i + 2000]) for i in range(0, len(para), 2000))))
        if len(paragraphs) > MAX_COPIED_PARAGRAPHS:
            blocks.append(paragraph(text_item("The email continues; open it in Gmail for the rest.", color="gray")))
        for b in self.attachment_blocks:
            body = b[b["type"]]
            url = (body.get("file") or {}).get("url")
            name = body.get("name") or block_text(b) or "attachment"
            mime = mimetypes.guess_type(name)[0] or ("application/pdf" if b["type"] == "pdf" else
                                                     "application/octet-stream")
            blocks.extend(self.upload_or_link(Attachment(
                name=name, link=link,
                fetch=(lambda u=url, nm=name, mt=mime: (self.fetch(u), nm, mt)) if url else None)))
        self.notion.append_children(toggle["id"], blocks)
        self.report.notes.append(f"Copied the email \"{email.subject}\" into its assignment.")

    def same_due(self, current: str | None, stated: str) -> bool:
        if is_date_only(stated) and current:
            return parse_dt(current, self.tz).astimezone(self.tz).date().isoformat() == stated
        return same_minute(current, stated, self.tz)
