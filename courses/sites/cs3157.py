"""Reader for COMS W3157 Advanced Programming (cs3157.github.io).

Assignments and exams from the "Assignments" list; lecture-note PDFs (lecture-notes/NN-*.pdf) from
the "Lecture N: T 9/8" session tables; example code from the code/NN/ pages they link.
"""

from __future__ import annotations

import re
from datetime import datetime
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup, Tag

from scheduleaid.websites import (
    DEFAULT_DUE_TIME, MONTHS, SiteContent, WebCodeDir, WebItem, WebLecture, at, key_of,
    parse_single_time, parse_time_range, resolve_date, squash,
)

# Course websites this reader handles (a regular expression on the website URL).
MATCHES = r"cs3157\.github\.io"

AP_DUE = re.compile(r"^(?P<name>.+?)\s+(?P<tent>tentatively\s+)?due\s+(?:[A-Za-z]{3,9}\.?\s+)?"
                    r"(?P<m>\d{1,2})/(?P<d>\d{1,2})(?P<rest>.*)$", re.I)
AP_EXAM = re.compile(r"^(?P<name>.*?\bexam\b.*?)\s+on\s+(?:[A-Za-z]+\s+)?(?P<mon>[A-Za-z]{3})[a-z]*\.?"
                     r"\s+(?P<d>\d{1,2})(?P<rest>.*)$", re.I)
AP_SESSION = re.compile(r"^Lecture\s+(\d+):\s*[A-Za-z]+\s+(\d{1,2})/(\d{1,2})")


def read(html: str, page_url: str, now: datetime, tz: ZoneInfo) -> SiteContent:
    soup = BeautifulSoup(html, "html.parser")
    out = SiteContent()

    heading = next((h for h in soup.find_all(["h2", "h3"]) if squash(h.get_text()) == "Assignments"), None)
    lst = heading.find_next_sibling(["ul", "ol"]) if heading else None
    for li in (lst.find_all("li", recursive=False) if lst else []):
        text = squash(li.get_text(" "))
        link = li.find("a", href=True)
        url = urljoin(page_url, link["href"]) if link else None
        if m := AP_EXAM.match(text):
            day = resolve_date(MONTHS[m["mon"][:3].lower()], int(m["d"]), now)
            span = parse_time_range(m["rest"])
            name = squash(m["name"])
            out.items.append(WebItem(
                "exam", key_of(name.split("(")[0]), name,
                due=at(day, span[0], tz) if span else day.isoformat(),
                end=at(day, span[1], tz) if span else None, url=url,
                notes=[f"From the course website: “{text}”"]))
        elif m := AP_DUE.match(text):
            day = resolve_date(int(m["m"]), int(m["d"]), now)
            due_time = parse_single_time(m["rest"])
            name = re.sub(r"^([A-Za-z]+)(\d)", r"\1 \2", squash(m["name"]))  # "Lab4" → "Lab 4"
            notes = []
            if m["tent"]:
                notes.append("The due date is tentative on the course website.")
            if due_time is None:
                notes.append("The website gives no due time; assumed 11:59 PM.")
            if name.lower().startswith("lab"):
                notes.append("Lab instructions are announced by email: [cs3157] [ANN].")
            out.items.append(WebItem("assignment", key_of(name), name,
                                     due=at(day, due_time or DEFAULT_DUE_TIME, tz), url=url, notes=notes))

    # Lecture notes: numbered PDFs under lecture-notes/, first used in some "Lecture N: T 9/8" session.
    seen: dict[str, WebLecture] = {}
    for table in soup.find_all("table"):
        m = AP_SESSION.match(squash(table.get_text(" ")))
        if not m:
            continue
        session_day = resolve_date(int(m.group(2)), int(m.group(3)), now)
        body = table.find_next_sibling()
        if not isinstance(body, Tag) or body.name not in ("ul", "ol"):
            continue
        for a in body.find_all("a", href=True):
            if "lecture-notes/" not in a["href"]:
                continue
            url = urljoin(page_url, a["href"])
            if url in seen:
                continue
            num = re.search(r"/(\d+)-[^/]*$", a["href"])
            name = squash(a.get_text(" "))
            seen[url] = WebLecture(
                key=f"notes{int(num.group(1)):02d}" if num else key_of(name), name=name,
                number=int(num.group(1)) if num else None, date=session_day.isoformat(),
                url=url, links=[("Lecture notes (PDF)", url)])
    out.lectures = list(seen.values())

    # Example code: code/NN/ pages, linked from the sessions that use them.
    code: dict[int, WebCodeDir] = {}
    for a in soup.find_all("a", href=re.compile(r"(^|/)code/\d+/?")):
        num = int(re.search(r"code/(\d+)", a["href"]).group(1))
        if num in code:
            continue
        table = a.find_previous("table")
        m = AP_SESSION.match(squash(table.get_text(" "))) if table else None
        day = resolve_date(int(m.group(2)), int(m.group(3)), now).isoformat() if m else None
        code[num] = WebCodeDir(urljoin(page_url, f"code/{num:02d}/"), num, day)
    out.code_dirs = list(code.values())
    out.code_index = urljoin(page_url, "code/")
    return out
