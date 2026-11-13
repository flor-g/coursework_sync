"""Course websites → Notion (+ Google Calendar): assignments, exams and lecture notes."""

from __future__ import annotations

import logging
import mimetypes

import requests

from . import notion as n
from .blocks import paragraph, text_item
from .http import request_with_retry
from .sync_common import (
    AssignmentData, Attachment, BaseSync, CodeData, LectureData, content_hash, course_key, is_date_only,
    parse_dt,
)
from .sync_courseworks import guess_type
from .websites import SiteContent, WebItem, parse_code_dir, parse_code_index, reader_for

log = logging.getLogger(__name__)

USER_AGENT = "coursework_sync/1.0 (personal coursework organizer)"


def fetch_page(url: str) -> bytes:
    session = requests.Session()
    session.headers["User-Agent"] = USER_AGENT
    return request_with_retry(session, "GET", url, service="Website").content


class WebsiteSync(BaseSync):
    SOURCE = "Website"

    def __init__(self, cfg, notion_client, now=None, report=None, fetch=fetch_page):
        super().__init__(cfg, notion_client, now, report)
        self.fetch = fetch

    def run(self, course_pages: list[dict] | None = None):
        for course_page in self.active_courses(course_pages):
            url = n.get_url(course_page, "Website")
            if not url:
                continue
            label = self.label(course_page)
            reader = reader_for(url)
            if reader is None:
                self.report.warnings.append(f"{label}: no reader for {url} yet.")
                continue
            try:
                content = reader(self.fetch(url), url, self.now, self.tz)
                if not content.items and not content.lectures:
                    self.report.warnings.append(
                        f"{label}: found nothing on {url}. The site layout may have changed.")
                    continue
                prefix = f"web:{course_key(n.get_text(course_page, 'Code')) or course_page['id']}"
                for item in content.items:
                    self.upsert_assignment(course_page, self.to_data(prefix, item, url))
                for lec in content.lectures:
                    self.upsert_lecture(course_page, LectureData(
                        sid=f"{prefix}:{lec.key}", name=lec.name, number=lec.number, date=lec.date,
                        original_url=lec.url, source_url=url, files=lec.links))
                self.sync_code(course_page, prefix, content)
            except Exception as exc:
                log.exception("Failed to sync website for %s", label)
                self.report.errors.append(f"{label} (website): {exc}")
        return self.report

    def sync_code(self, course_page: dict, prefix: str, content: SiteContent) -> None:
        if not self.cfg.code_ds or not (content.code_dirs or content.code_index):
            return
        dirs = {d.number: d for d in content.code_dirs}
        if content.code_index:
            try:
                for d in parse_code_index(self.fetch(content.code_index), content.code_index):
                    dirs.setdefault(d.number, d)
            except Exception as exc:
                log.warning("Could not read %s: %s", content.code_index, exc)
        lectures = {lec.number: lec for lec in content.lectures if lec.number is not None}
        for number in sorted(dirs):
            d = dirs[number]
            lec = lectures.get(number)
            group = f"{lec.name}" if lec else f"code/{number:02d}"
            for f in parse_code_dir(self.fetch(d.url)):
                text = f.text
                self.upsert_code(course_page, CodeData(
                    sid=f"{prefix}:code{number:02d}:{f.name}", name=f.name, group=group,
                    date=d.date or (lec.date if lec else None),
                    source_url=d.url + (f"#{f.anchor}" if f.anchor else ""),
                    version=content_hash(text), fetch=lambda t=text: t.encode("utf-8"),
                    lecture_sid=f"{prefix}:{lec.key}" if lec else None))

    def fetch_handout(self, url: str, filename: str):
        data = self.fetch(url)
        mime = "application/pdf" if data[:4] == b"%PDF" else (
            mimetypes.guess_type(filename)[0] or "application/octet-stream")
        return data, filename, mime

    def to_data(self, prefix: str, item: WebItem, page_url: str) -> AssignmentData:
        sid = f"{prefix}:{item.key}"
        blocks = []
        files = []
        if item.url:
            filename = item.url.split("?")[0].rstrip("/").rsplit("/", 1)[-1] or "handout"
            files.append(Attachment(name=filename, link=item.url,
                                    fetch=lambda u=item.url, f=filename: self.fetch_handout(u, f)))
        for label, link in item.links:
            blocks.append(paragraph(text_item(f"{label}: "), text_item("open", link=link)))
        for note in item.notes:
            blocks.append(paragraph(text_item(note, color="gray")))
        if not blocks and not files:
            blocks = [paragraph(text_item("No details on the course website.", color="gray"))]

        is_exam = item.kind == "exam"
        last = parse_dt(item.end or item.due, self.tz)
        if last and is_date_only(item.end or item.due):
            last = last.replace(hour=23, minute=59)
        # Websites don't know what you've submitted: anything already past at first import is
        # marked Done; later status changes are yours.
        status = "Done" if last and last < self.now else "Not started"

        existing = self.index(self.cfg.assignments_ds).get(sid)
        return AssignmentData(
            sid=sid,
            name=item.name,
            due=item.due,
            source_url=page_url,
            blocks=blocks,
            hash=content_hash("files-v3", item.name, item.due, item.end, item.url,
                              *(f"{a}|{b}" for a, b in item.links), *item.notes),
            files=files,
            type="Exam" if is_exam else guess_type({"name": item.name}),
            initial_status=status,
            # an exam that has happened is done, whatever its status was
            advanced_status=("Done" if is_exam and existing and status == "Done"
                             and n.get_select(existing, "Status") != "Done" else None),
            end=item.end,
            is_exam=is_exam,
        )
