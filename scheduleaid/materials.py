"""Finding lecture slides and course code in CourseWorks.

Courses keep them in two ways:
- in the Files area, in folders such as "Lecture 3" or "Lecture 12/section 2" (Intro to Java);
- linked from the syllabus, one table row per week, when the Files area is closed to students
  (Data Structures).
These helpers only read; CourseWorksSync writes the pages.
"""

from __future__ import annotations

import html as html_lib
import io
import posixpath
import re
import zipfile
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from bs4 import BeautifulSoup

from .sync_common import code_language
from .websites import MONTHS, resolve_date, squash

FILE_LINK = re.compile(r"/files/(\d+)")
SLIDE_EXTENSIONS = (".pdf", ".ppt", ".pptx", ".pps", ".ppsx", ".odp", ".doc", ".docx")
# Linked PDFs that are not lecture material.
NOT_LECTURE = re.compile(
    r"solution|exercise|worksheet|annotat|install|\blab\b|lab-|template|hackerrank|leetcode|spoj|"
    r"midterm|exam|homework|style.?guide|syllabus", re.I)
GENERIC_LINK_TEXT = {"slides", "slide", "notes", "pdf", "here", "link", "download"}
# In folder listings, notes and transcripts saved as .txt are not code.
NOT_CODE_IN_FOLDERS = (".txt", ".md", ".csv", ".tsv", ".json", ".html")
ROW_DATE = re.compile(r"^([A-Za-z]{3})[a-z]*\.?\s+(\d{1,2})\b")
LECTURE_FOLDER = re.compile(r"(?:^|/)Lecture\s*(\d+)(?:/|$)", re.I)
ROW_NOISE = re.compile(r"\b(Slides|Source code(?: and JUnit test)?|Solution|Template|Exercise|Notes)\s*[:\-–]\s*", re.I)


def ext_of(name: str) -> str:
    dot = name.rfind(".")
    return name[dot:].lower() if dot >= 0 else ""


def is_code_file(name: str) -> bool:
    return code_language(posixpath.basename(name)) is not None


def pretty_stem(name: str) -> str:
    stem = name[:-len(ext_of(name))] if ext_of(name) else name
    return squash(re.sub(r"[_]+", " ", stem))


# --- syllabus ---------------------------------------------------------------------
@dataclass
class SyllabusLink:
    file_id: str
    text: str


@dataclass
class SyllabusRow:
    day: date
    label: str                     # "Week 04 · Sep 28: Array Lists, Linked Lists, Interfaces, and Iterators"
    links: list[SyllabusLink] = field(default_factory=list)


def row_topic(cell) -> str:
    """The topic in a schedule cell, without the names of the files linked from it."""
    text = squash(cell.get_text(" "))
    for a in cell.find_all("a"):
        link_text = squash(a.get_text(" "))
        if link_text:
            text = text.replace(link_text, " ")
    text = squash(ROW_NOISE.sub(" ", text)).strip(" .,:;-–")
    first = re.split(r"(?<=[a-z)])\.\s", text)[0].strip(" .,:;-–")
    return first[:90].rstrip() + ("…" if len(first) > 90 else "")


def syllabus_rows(html: str, now: datetime) -> list[SyllabusRow]:
    """Schedule rows ("Sep. 14 and 16 | topic | … ") that link CourseWorks files."""
    soup = BeautifulSoup(html or "", "html.parser")
    rows = []
    first: date | None = None
    for tr in soup.find_all("tr"):
        cells = tr.find_all(["td", "th"], recursive=False)
        if len(cells) < 2:
            continue
        m = ROW_DATE.match(squash(cells[0].get_text(" ")))
        if not m or m.group(1).lower() not in MONTHS:
            continue
        day = resolve_date(MONTHS[m.group(1).lower()], int(m.group(2)), now)
        first = first or day - timedelta(days=day.weekday())   # Monday of the first week
        week = (day - first).days // 7 + 1                       # week of term
        topic = row_topic(cells[1])
        label = f"Week {week:02d} · {day:%b} {day.day}"   # "Week 04 · Sep 28: Array Lists"
        row = SyllabusRow(day, f"{label}: {topic}" if topic else label)
        seen = set()
        for a in tr.find_all("a", href=True):
            fm = FILE_LINK.search(html_lib.unescape(a["href"]))
            if fm and fm.group(1) not in seen:
                seen.add(fm.group(1))
                row.links.append(SyllabusLink(fm.group(1), squash(a.get_text(" "))))
        if row.links:
            rows.append(row)
    return rows


def is_lecture_material(display_name: str, link_text: str = "") -> bool:
    return (ext_of(display_name) in SLIDE_EXTENSIONS
            and not NOT_LECTURE.search(f"{link_text} {display_name}"))


def lecture_title(display_name: str, link_text: str = "") -> str:
    text = squash(link_text)
    if text and text.lower() not in GENERIC_LINK_TEXT and not text.lower().startswith("http"):
        return text[:-len(ext_of(text))] if ext_of(text) in SLIDE_EXTENSIONS else text
    return pretty_stem(display_name)


# --- folders ------------------------------------------------------------------------
def folder_group(folder: str) -> str:
    """"course files/Lecture 12/section 2" → "Lecture 12"; others keep their path."""
    path = re.sub(r"^course files/?", "", folder or "", flags=re.I).strip("/")
    m = LECTURE_FOLDER.search(path)
    if m:
        return f"Lecture {int(m.group(1)):02d}"   # zero-padded so the groups sort in order
    return path or "Course files"


def lecture_number(folder: str) -> int | None:
    m = LECTURE_FOLDER.search(folder or "")
    return int(m.group(1)) if m else None


def is_lecture_slides_in_folder(folder: str, display_name: str) -> bool:
    """Slides sit directly in a "Lecture N" folder (not in its section sub-folders)."""
    path = re.sub(r"^course files/?", "", folder or "", flags=re.I).strip("/")
    return bool(re.fullmatch(r"Lecture\s*\d+", path, re.I)) and is_lecture_material(display_name)


def is_code_in_folder(display_name: str) -> bool:
    return (is_code_file(display_name) and not display_name.lower().endswith(NOT_CODE_IN_FOLDERS)) \
        or display_name.lower().endswith(".zip")


# --- zips --------------------------------------------------------------------------
def zip_code_members(data: bytes) -> list[tuple[str, bytes]]:
    """Source files inside a zip: [(path in the zip, contents)]."""
    out = []
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        return out
    for info in archive.infolist():
        name = info.filename
        base = posixpath.basename(name)
        if info.is_dir() or name.startswith("__MACOSX/") or base.startswith(".") or not is_code_file(base):
            continue
        if info.file_size > 2_000_000:
            continue
        out.append((name, archive.read(info)))
    return out
