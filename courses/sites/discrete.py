"""Reader for COMS W3203 Discrete Mathematics (cs.columbia.edu/~gtchuang/discrete).

One schedule table: each dated row has the lecture notes (lectures/LectureN.pdf), problem sets
released that day with their due dates, and exam rows (class "exam-row").
"""

from __future__ import annotations

import re
from datetime import datetime
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup

from scheduleaid.websites import (
    DEFAULT_DUE_TIME, SiteContent, WebItem, WebLecture, at, key_of, parse_single_time, resolve_date,
    squash,
)

# Course websites this reader handles (a regular expression on the website URL).
MATCHES = r"cs\.columbia\.edu/~gtchuang/discrete"

DM_ROW_DATE = re.compile(r"^[A-Za-z]{2,4}\.?\s+(\d{1,2})/(\d{1,2})")
DM_DUE = re.compile(r"due\s+(?:[A-Za-z]{3,9}\.?\s+)?(\d{1,2})/(\d{1,2})(.*)", re.I)


def read(html: str, page_url: str, now: datetime, tz: ZoneInfo) -> SiteContent:
    soup = BeautifulSoup(html, "html.parser")
    out = SiteContent()
    for row in soup.find_all("tr"):
        cells = row.find_all("td", recursive=False)
        if not cells:
            continue
        m = DM_ROW_DATE.match(squash(cells[0].get_text(" ")))
        if not m:
            continue
        row_day = resolve_date(int(m.group(1)), int(m.group(2)), now)

        if "exam-row" in (row.get("class") or []):
            strong = row.find(["strong", "b"])
            title = squash(strong.get_text(" ")) if strong else "Exam"
            name = title.title() if title.isupper() else title
            details = squash(cells[1].get_text(" ")) if len(cells) > 1 else ""
            out.items.append(WebItem("exam", key_of(name), name, due=row_day.isoformat(),
                                     notes=[details] if details else []))
            continue

        # lecture
        if len(cells) > 1:
            notes_link = cells[1].find("a", href=re.compile(r"lectures/Lecture\d+\.pdf"))
            if notes_link:
                number = int(re.search(r"Lecture(\d+)\.pdf", notes_link["href"]).group(1))
                topic = squash(cells[1].get_text(" ").split("lecture notes")[0]).rstrip(" .,")
                links = [(squash(a.get_text(" ")).capitalize(), urljoin(page_url, a["href"]))
                         for a in cells[1].find_all("a", href=True)]
                out.lectures.append(WebLecture(
                    key=f"lecture{number}", name=f"Lecture {number}: {topic}", number=number,
                    date=row_day.isoformat(), url=urljoin(page_url, notes_link["href"]), links=links))

        # homework
        for cell in cells[2:]:
            hw = cell.find("a", href=re.compile(r"problem_set", re.I))
            if not hw:
                continue
            name = squash(hw.get_text(" "))
            due_match = DM_DUE.search(squash(cell.get_text(" ")))
            notes = [f"Released {row_day:%a, %b} {row_day.day}."]
            due = None
            if due_match:
                due_day = resolve_date(int(due_match.group(1)), int(due_match.group(2)), now)
                due_time = parse_single_time(due_match.group(3))
                if due_time is None:
                    notes.append("The website gives no due time; assumed 11:59 PM.")
                due = at(due_day, due_time or DEFAULT_DUE_TIME, tz)
            links = [("Solutions", urljoin(page_url, a["href"]))
                     for a in cell.find_all("a", href=True) if "sol" in a["href"].lower()]
            out.items.append(WebItem("assignment", key_of(name), name, due=due,
                                     url=urljoin(page_url, hw["href"]), links=links, notes=notes))
    return out
