"""Course website support: the data a site reader returns, parsing helpers readers share, and the
lookup from a website URL to its reader.

The readers themselves live in courses/sites/, one file per site (see courses/README.md)."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, time
from typing import Callable
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup

DEFAULT_DUE_TIME = time(23, 59)
MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}


@dataclass
class WebItem:
    kind: str                      # "assignment" or "exam"
    key: str                       # stable id within the course, e.g. "lab4", "hw6", "midterm2"
    name: str
    due: str | None                # ISO datetime, or bare date for all-day
    end: str | None = None         # exams: end time
    url: str | None = None         # handout
    links: list[tuple[str, str]] = field(default_factory=list)   # extra links, e.g. solutions
    notes: list[str] = field(default_factory=list)


@dataclass
class WebLecture:
    key: str
    name: str
    number: int | None
    date: str | None
    url: str | None
    links: list[tuple[str, str]] = field(default_factory=list)


@dataclass
class WebCodeDir:
    """A folder of example code on the site, e.g. code/02/ (goes with lecture notes 02)."""
    url: str
    number: int
    date: str | None = None


@dataclass
class WebCodeFile:
    name: str
    anchor: str | None
    text: str


@dataclass
class SiteContent:
    items: list[WebItem] = field(default_factory=list)
    lectures: list[WebLecture] = field(default_factory=list)
    code_dirs: list[WebCodeDir] = field(default_factory=list)
    code_index: str | None = None   # a page listing every code folder, for ones not linked yet


# --- shared parsing helpers -------------------------------------------------
def squash(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def key_of(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.lower())


def resolve_date(month: int, day: int, now: datetime) -> date:
    """Course sites omit the year; pick the one that puts the date nearest to today."""
    candidates = [date(now.year + dy, month, day) for dy in (-1, 0, 1)]
    return min(candidates, key=lambda d: abs((d - now.date()).days))


def _to24(hour: int, minute: int, ampm: str) -> time:
    hour = hour % 12 + (12 if ampm.lower() == "pm" else 0)
    return time(hour, minute)


TIME = r"(\d{1,2})(?::(\d{2}))?\s*([ap]\.?m\.?)?"
TIME_RANGE = re.compile(TIME + r"\s*[-–]\s*" + TIME, re.I)
SINGLE_TIME = re.compile(r"\b(\d{1,2})(?::(\d{2}))?\s*([ap])\.?m\b", re.I)


def parse_time_range(text: str) -> tuple[time, time] | None:
    m = TIME_RANGE.search(text or "")
    if not m or not (m.group(3) or m.group(6)):
        return None
    h1, m1, ap1, h2, m2, ap2 = m.groups()
    ap2 = (ap2 or ap1).replace(".", "")[:2]
    end = _to24(int(h2), int(m2 or 0), ap2)
    start = _to24(int(h1), int(m1 or 0), (ap1 or ap2).replace(".", "")[:2])
    if not ap1 and start > end:  # "11:40-12:55pm" → the start is in the morning
        start = _to24(int(h1), int(m1 or 0), "am")
    return start, end


def parse_single_time(text: str) -> time | None:
    m = SINGLE_TIME.search(text or "")
    return _to24(int(m.group(1)), int(m.group(2) or 0), m.group(3) + "m") if m else None


def at(day: date, t: time, tz: ZoneInfo) -> str:
    return datetime.combine(day, t, tzinfo=tz).isoformat()


# --- code listing pages (e.g. cs3157.github.io/.../code/NN/) ------------------------
def parse_code_index(html: str, index_url: str) -> list[WebCodeDir]:
    """The site's code/ listing: one link per numbered folder."""
    soup = BeautifulSoup(html, "html.parser")
    out = {}
    for a in soup.find_all("a", href=True):
        m = re.search(r"code/(\d+)/?$", a["href"])
        if m:
            num = int(m.group(1))
            out.setdefault(num, WebCodeDir(urljoin(index_url, f"{m.group(1)}/"), num))
    return list(out.values())


def parse_code_dir(html: str) -> list[WebCodeFile]:
    """A code/NN/ page: for each file an <h2 id=...>name</h2>, then its source in a <pre>."""
    soup = BeautifulSoup(html, "html.parser")
    files = []
    for h in soup.find_all("h2"):
        pre = None
        for sib in h.find_next_siblings():
            if sib.name == "h2":
                break
            pre = sib if sib.name == "pre" else sib.find("pre")
            if pre:
                break
        if pre is not None:
            files.append(WebCodeFile(squash(h.get_text(" ")), h.get("id"), pre.get_text()))
    return files


# --- registry -------------------------------------------------------------------
Reader = Callable[[str, str, datetime, ZoneInfo], SiteContent]


def reader_for(url: str) -> Reader | None:
    """The reader in courses/sites/ whose MATCHES pattern fits this website URL."""
    from .course_list import site_readers

    return next((reader for pattern, reader in site_readers() if pattern.search(url or "")), None)
