"""On-demand lecture conversion: original slides/PDF → written lecture notes in the page's "Notes".

Nothing converts automatically. A lecture is converted when its Conversion status is set to "Requested"
(the Conversion button in Notion does that). The Apps Script notices within a minute
and starts the GitHub "Convert lecture" workflow for that page; the 3-hourly sync also picks up
anything still waiting.

Claude reads the original as a PDF and rewrites it as lecture notes (numbered sections, full
sentences, proofs as steps with reasons, code with explanations). PowerPoint and Word files are
turned into PDFs with LibreOffice first; the Convert workflow installs it.
"""

from __future__ import annotations

import base64
import logging
import re
import shutil
import subprocess
import tempfile
from datetime import timedelta
from pathlib import Path

import requests

from . import notion as n
from . import prompts
from .blocks import paragraph, text_item
from .claude import messages_api
from .http import request_with_retry
from .markdown import to_blocks
from .sync_common import BaseSync, parse_dt

log = logging.getLogger(__name__)

WAITING = {"or": [
    {"property": "Conversion status", "select": {"equals": "Requested"}},
    {"property": "Conversion status", "select": {"equals": "Converting"}},
]}
STUCK_AFTER = timedelta(minutes=20)     # a "Converting" page older than this is retried
MAX_PDF_BYTES = 24 * 1024 * 1024        # the API request limit is 32 MB after base64 encoding
OFFICE_EXTENSIONS = (".ppt", ".pptx", ".pps", ".ppsx", ".odp", ".doc", ".docx", ".odt", ".rtf")
CANVAS_FILE = re.compile(r"/files/(\d+)")

SYSTEM_PROMPT = prompts.load("lecture_notes.system")   # prompts/lecture_notes.system.md


class ConversionError(RuntimeError):
    pass


def office_available() -> bool:
    return shutil.which("soffice") is not None or shutil.which("libreoffice") is not None


def office_to_pdf(data: bytes, filename: str) -> bytes:
    """PowerPoint/Word → PDF with LibreOffice (headless)."""
    binary = shutil.which("soffice") or shutil.which("libreoffice")
    if not binary:
        raise ConversionError(f"{filename} needs LibreOffice to be converted, and it is not installed here")
    with tempfile.TemporaryDirectory() as tmp:
        safe = re.sub(r"[^A-Za-z0-9._-]", "_", filename) or "lecture"
        source = Path(tmp) / safe
        source.write_bytes(data)
        result = subprocess.run(
            [binary, "--headless", "--convert-to", "pdf", "--outdir", tmp, str(source)],
            capture_output=True, timeout=300, check=False)
        out = source.with_suffix(".pdf")
        if not out.exists():
            raise ConversionError(f"LibreOffice could not convert {filename}: "
                                  f"{(result.stderr or result.stdout).decode(errors='replace')[:200]}")
        return out.read_bytes()


def is_office(filename: str) -> bool:
    return filename.lower().endswith(OFFICE_EXTENSIONS)


class LectureConverter(BaseSync):
    SOURCE = "Conversion"

    def __init__(self, cfg, notion_client, client=None, fetch=None, now=None, report=None,
                 locate=None, to_pdf=office_to_pdf, can_convert_office=None):
        super().__init__(cfg, notion_client, now, report)
        self._client = client
        self.fetch = fetch or self._download
        self.locate = locate or self._locate
        self.to_pdf = to_pdf
        self.can_convert_office = office_available if can_convert_office is None else can_convert_office

    @property
    def client(self):
        if self._client is None:
            import anthropic

            self._client = anthropic.Anthropic()
        return self._client

    # --- which pages ---------------------------------------------------------------------
    def run_waiting(self) -> None:
        """Convert every lecture whose conversion was requested (or got stuck converting)."""
        for page in self.notion.query_all(self.cfg.lectures_ds, WAITING):
            if n.get_select(page, "Conversion status") == "Converting":
                edited = parse_dt(page.get("last_edited_time"), self.tz)
                if edited and self.now - edited < STUCK_AFTER:
                    continue  # another run is working on it
            self.convert(page, defer_office=True)

    def convert_page_id(self, page_id: str) -> None:
        page = self.notion.get_page(page_id)
        if n.get_select(page, "Conversion status") not in ("Requested", "Converting"):
            self.report.notes.append(f"{n.get_text(page, 'Name')}: conversion was not requested; skipped.")
            return
        self.convert(page)

    # --- one page ------------------------------------------------------------------------
    def convert(self, page: dict, defer_office: bool = False) -> None:
        """defer_office: this run has no LibreOffice (the regular sync), so a PowerPoint/Word
        original is handed back to the Convert workflow, which has it."""
        name = n.get_text(page, "Name") or "Lecture notes"
        if self.notion.dry_run:
            self.report.notes.append(f"[dry-run] would convert {name}")
            return
        self.notion.update_page(page["id"], {"Conversion status": n.select("Converting")})
        try:
            url, filename = self.locate(self.original(page))
            if is_office(filename) and defer_office and not self.can_convert_office():
                # back to Requested: the Apps Script starts the Convert workflow for it
                self.notion.update_page(page["id"], {"Conversion status": n.select("Requested")})
                self.report.notes.append(f"{name}: left for the Convert workflow ({filename} needs LibreOffice).")
                return
            data = self.fetch(url)
            markdown, how, complete = self.to_markdown(data, filename, name, self.course_line(page))
            blocks = to_blocks(markdown)
            note = f"Rewritten as lecture notes on {self.now:%b} {self.now.day} with {how}, from {filename}. "
            note += ("Diagrams are redrawn or described in words; the original is under Files."
                     if complete else "The conversion was cut off before the end; see the original for the rest.")
            self.write_toggle(page["id"], "Notes", [paragraph(text_item(note, color="gray")), *blocks])
            self.notion.update_page(page["id"], {
                "Conversion status": n.select("Converted" if complete else "Needs review"),
                "Last synced": self.stamp,
            })
            self.report.notes.append(f"Converted {name} ({len(blocks)} blocks).")
            self.report.updated += 1
        except Exception as exc:
            log.exception("Conversion failed for %s", name)
            self.write_toggle(page["id"], "Notes", [paragraph(text_item(
                f"Conversion failed: {exc}. Click the Conversion button to try again.", color="red"))])
            self.notion.update_page(page["id"], {"Conversion status": n.select("Needs review")})
            self.report.errors.append(f"Converting {name}: {exc}")

    def original(self, page: dict) -> str:
        url = n.get_url(page, "Original URL")
        if not url:
            files = (page.get("properties", {}).get("Original file") or {}).get("files") or []
            if files:
                f = files[0]
                url = (f.get("file") or f.get("external") or {}).get("url")
        if not url:
            raise ConversionError("no original file or link on this lecture")
        return url

    def course_line(self, page: dict) -> str:
        """"CS 3134 Data Structures, taught by Prof. Rivera", so the notes can name the course."""
        rel = (page.get("properties", {}).get("Course") or {}).get("relation") or []
        if not rel:
            return ""
        try:
            course = self.notion.get_page(rel[0]["id"])
        except Exception:
            return ""
        line = " ".join(x for x in (n.get_text(course, "Code"), n.get_text(course, "Name")) if x)
        instructor = n.get_text(course, "Instructor")
        return f"{line}, taught by {instructor}" if line and instructor else line

    def to_markdown(self, data: bytes, filename: str, name: str, course: str = "") -> tuple[str, str, bool]:
        if is_office(filename):
            data = self.to_pdf(data, filename)
        elif not data.startswith(b"%PDF"):
            raise ConversionError(f"{filename} is not a PDF, PowerPoint or Word file")
        if len(data) > MAX_PDF_BYTES:
            raise ConversionError(f"{filename} is {len(data) / 1048576:.0f} MB; the limit is 24 MB")
        return self.claude_pdf(data, name, course)

    def claude_pdf(self, data: bytes, name: str, course: str = "") -> tuple[str, str, bool]:
        model = self.cfg.convert_model
        api, extra = messages_api(self.client, model)
        about = f"{name} ({course})" if course else name
        with api.stream(
            model=model,
            max_tokens=64000,
            output_config={"effort": "medium"},
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": [
                {"type": "document", "source": {"type": "base64", "media_type": "application/pdf",
                                                "data": base64.standard_b64encode(data).decode()}},
                {"type": "text", "text": prompts.render("lecture_notes.user", lecture=about)},
            ]}],
            **extra,
        ) as stream:
            message = stream.get_final_message()
        if message.stop_reason == "refusal":
            raise ConversionError("Claude declined to convert this file")
        markdown = "".join(b.text for b in message.content if b.type == "text").strip()
        markdown = re.sub(r"^```(?:markdown|md)?\s*\n(.*)\n```\s*$", r"\1", markdown, flags=re.S)
        if not markdown:
            raise ConversionError("Claude returned no text")
        return markdown, model, message.stop_reason != "max_tokens"

    def _locate(self, url: str) -> tuple[str, str]:
        """(download URL, file name). A CourseWorks file page is looked up through the API, which
        gives its name and a download link."""
        m = CANVAS_FILE.search(url)
        if url.startswith(self.cfg.canvas_base_url) and m:
            from .canvas import CanvasClient

            meta = CanvasClient(self.cfg.canvas_base_url, self.cfg.canvas_token).file(m.group(1))
            if not meta.get("url"):
                raise ConversionError("CourseWorks did not give a download link for this file")
            return meta["url"], meta.get("display_name") or meta.get("filename") or m.group(1)
        return url, url.split("?")[0].rstrip("/").rsplit("/", 1)[-1] or "lecture"

    def _download(self, url: str) -> bytes:
        session = requests.Session()
        session.headers["User-Agent"] = "coursework_sync/1.0 (personal coursework organizer)"
        if url.startswith(self.cfg.canvas_base_url):
            session.headers["Authorization"] = f"Bearer {self.cfg.canvas_token}"
        return request_with_retry(session, "GET", url, service="Download").content
