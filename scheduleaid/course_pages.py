"""Keeps the "Important" box at the top of each course page current, and ticks "Due in 14 days"
on assignments for the To-do lists' "Next 14 days" tab.

The box lists important updates from the last two weeks (deadline changes, exam news, flagged
emails) and exams in the next ten days. Deadline changes and exams are in red, the template's
colour for important notes. The rest of the course page is fixed layout plus linked views.

Pages made from the Courses template start with placeholders: the "(course code · term)" header
lines and an empty At-a-glance table. They are filled in from the course's properties; a cell you
have typed into is never changed, except two the sync keeps current: Exams (every exam page of
the course, with its date and time) and Links (when it holds only the sync's own links).
"""

from __future__ import annotations

import logging
from datetime import timedelta

from . import notion as n
from .blocks import paragraph, text_item
from .sync_common import BaseSync, human_dt, parse_dt

log = logging.getLogger(__name__)

RECENT = timedelta(days=14)
DUE_WINDOW = timedelta(days=14)
EXAMS_AHEAD = timedelta(days=10)
MAX_ITEMS = 6
RED_CATEGORIES = {"Deadline change", "Exam"}
HEADER_PLACEHOLDER = "(course code · term)"   # the template's header line


def _bullet(*items: dict) -> dict:
    return {"object": "block", "type": "bulleted_list_item", "bulleted_list_item": {"rich_text": list(items)}}


def _plain_children(blocks: list[dict]) -> list[str]:
    return [n._plain(b.get(b["type"], {}).get("rich_text")) for b in blocks]


LINK_LABELS = {"Website", "CourseWorks", "Gradescope", "Ed"}


def _is_placeholder(cell: list[dict]) -> bool:
    """Empty, or only gray hint text such as "Add times"."""
    return all((i.get("annotations") or {}).get("color") == "gray" or not n._plain([i]).strip() for i in cell)


def _only_our_links(cell: list[dict]) -> bool:
    """A Links cell the sync wrote (Website · CourseWorks · Gradescope · Ed), safe to refresh."""
    words = [n._plain([i]).strip() for i in cell if n._plain([i]).strip() not in ("", "·")]
    return bool(words) and all(w in LINK_LABELS for w in words)


def _rich_signature(cell: list[dict]) -> list[tuple]:
    return [(n._plain([i]), ((i.get("text") or {}).get("link") or {}).get("url"))
            for i in cell if n._plain([i])]


def exam_when(start: str | None, end: str | None, tz) -> str:
    """"Mon, Nov 9" or "Tue, Nov 17, 4:10–5:25 PM"."""
    dt = parse_dt(start, tz)
    if not dt:
        return ""
    dt = dt.astimezone(tz)
    day = f"{dt:%a}, {dt:%b} {dt.day}"
    if start and len(start) == 10:
        return day

    def clock(t):
        return f"{t.hour % 12 or 12}:{t.minute:02d}"

    finish = parse_dt(end, tz)
    if finish:
        finish = finish.astimezone(tz)
        return f"{day}, {clock(dt)}–{clock(finish)} {finish:%p}"
    return f"{day}, {clock(dt)} {dt:%p}"


class CoursePageSync(BaseSync):
    SOURCE = "Course page"

    def run(self, course_pages: list[dict] | None = None):
        try:
            self.refresh_due_window()
        except Exception as exc:
            log.exception("Could not refresh 'Due in 14 days'")
            self.report.errors.append(f"Due in 14 days: {exc}")
        for course in self.active_courses(course_pages):
            try:
                self.fill_details(course)
                self.refresh_important(course)
            except Exception as exc:
                log.exception("Could not refresh the Important box")
                self.report.errors.append(f"{self.label(course)} (course page): {exc}")
        return self.report

    def refresh_due_window(self) -> None:
        """Tick "Due in 14 days" for assignments due today through the next 14 days; untick the rest."""
        today = self.now.astimezone(self.tz).date()
        changed = 0
        for page in self.notion.query_all(self.cfg.assignments_ds):
            due = parse_dt(n.get_date(page, "Due"), self.tz)
            want = bool(due and today <= due.astimezone(self.tz).date() <= today + DUE_WINDOW)
            have = bool((page.get("properties", {}).get("Due in 14 days") or {}).get("checkbox"))
            if want != have:
                self.notion.update_page(page["id"], {"Due in 14 days": n.checkbox(want)})
                changed += 1
        if changed:
            self.report.notes.append(f"Due in 14 days: updated {changed} assignment(s).")

    def important_items(self, course: dict) -> list[dict]:
        today = self.now.date()
        updates = self.notion.query_all(self.cfg.updates_ds, {"and": [
            {"property": "Course", "relation": {"contains": course["id"]}},
            {"property": "Important", "checkbox": {"equals": True}},
            {"property": "Date", "date": {"on_or_after": (today - RECENT).isoformat()}},
        ]})
        exams = self.notion.query_all(self.cfg.assignments_ds, {"and": [
            {"property": "Course", "relation": {"contains": course["id"]}},
            {"property": "Type", "select": {"equals": "Exam"}},
            {"property": "Due", "date": {"on_or_after": today.isoformat()}},
            {"property": "Due", "date": {"on_or_before": (today + EXAMS_AHEAD).isoformat()}},
        ]})
        rows = []
        for exam in sorted(exams, key=lambda p: n.get_date(p, "Due") or ""):
            rows.append(_bullet(
                text_item(f"{n.get_text(exam, 'Name')}: {human_dt(n.get_date(exam, 'Due'), self.tz)}",
                          link=exam.get("url"), color="red"),
                text_item("  ·  upcoming exam", color="gray")))
        for update in sorted(updates, key=lambda p: n.get_date(p, "Date") or "", reverse=True):
            when = parse_dt(n.get_date(update, "Date"), self.tz)
            red = n.get_select(update, "Category") in RED_CATEGORIES
            title = text_item(n.get_text(update, "Name"), link=update.get("url"))
            if red:
                title["annotations"] = {"color": "red"}
            rows.append(_bullet(title, text_item(
                f"  ·  {when.astimezone(self.tz):%b} {when.day}  ·  {n.get_select(update, 'Source') or ''}"
                if when else "", color="gray")))
        return rows[:MAX_ITEMS]

    # --- details on pages made from the template ------------------------------------
    def fill_details(self, course: dict) -> None:
        if not n.get_text(course, "Code") or self.notion.dry_run:
            return
        top = self.notion.list_children(course["id"])
        for b in top:
            if b.get("type") == "paragraph" and HEADER_PLACEHOLDER in n._plain(b["paragraph"].get("rich_text")):
                self.notion.update_block(b["id"], {"paragraph": {"rich_text": self.header(course)}})
        table = next((b for b in top if b.get("type") == "table"), None)
        if table is None or not table.get("has_children", True):
            return
        values = self.glance_values(course)
        values["Exams"] = self.exams_cell(course)
        for row in self.notion.list_children(table["id"]):
            cells = (row.get("table_row") or {}).get("cells") or []
            if len(cells) < 2:
                continue
            label = n._plain(cells[0]).strip()
            value = values.get(label)
            if not value:
                continue
            managed = label == "Exams" or (label == "Links" and _only_our_links(cells[1]))
            if not (managed or _is_placeholder(cells[1])):
                continue   # filled in by you: left alone
            if _rich_signature(cells[1]) != _rich_signature(value):
                self.notion.update_block(row["id"], {"table_row": {"cells": [cells[0], value, *cells[2:]]}})

    def exams_cell(self, course: dict) -> list[dict]:
        """Every exam of the course, in date order, each linking to its page."""
        exams = self.notion.query_all(self.cfg.assignments_ds, {"and": [
            {"property": "Course", "relation": {"contains": course["id"]}},
            {"property": "Type", "select": {"equals": "Exam"}}]})
        exams.sort(key=lambda p: n.get_date(p, "Due") or "9999")
        out: list[dict] = []
        for page in exams:
            if out:
                out.append(text_item(" · "))
            out.append(text_item(n.get_text(page, "Name"), link=page.get("url") or None))
            when = exam_when(n.get_date(page, "Due"), n.get_date_end(page, "Due"), self.tz)
            if when:
                out.append(text_item(f" {when}"))
        return out

    def header(self, course: dict) -> list[dict]:
        line1 = " · ".join(x for x in (n.get_text(course, "Code"), n.get_select(course, "Term")) if x)
        line2 = " · ".join(x for x in (n.get_text(course, "Instructor"), n.get_text(course, "Meeting times")) if x)
        return [text_item(line1 + ("\n" + line2 if line2 else ""), color="gray")]

    @staticmethod
    def glance_values(course: dict) -> dict[str, list[dict]]:
        def text(prop: str) -> list[dict]:
            value = n.get_text(course, prop)
            return [text_item(value)] if value else []

        links = []
        for label, prop in (("Website", "Website"), ("CourseWorks", "CourseWorks URL"),
                            ("Gradescope", "Gradescope URL"), ("Ed", "Ed URL")):
            url = n.get_url(course, prop)
            if url:
                if links:
                    links.append(text_item(" · "))
                links.append(text_item(label, link=url))
        return {"Course code": text("Code"), "Instructor": text("Instructor"),
                "Lectures": text("Meeting times"), "Office hours": text("Office hours"), "Links": links}

    def refresh_important(self, course: dict) -> None:
        box = next((b for b in self.notion.list_children(course["id"])
                    if b.get("type") == "callout"
                    and n._plain(b["callout"].get("rich_text")).strip() == "Important"), None)
        if box is None:
            self.report.warnings.append(f"{self.label(course)}: no Important box on the course page.")
            return
        rows = self.important_items(course) or [paragraph(text_item("Nothing urgent right now.", color="gray"))]
        current = self.notion.list_children(box["id"]) if box.get("has_children") else []
        if _plain_children(current) == _plain_children(rows) or self.notion.dry_run:
            return
        for child in current:
            self.notion.delete_block(child["id"])
        self.notion.append_children(box["id"], rows)
