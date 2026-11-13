"""Convert CourseWorks HTML (assignment descriptions, announcements) into Notion blocks,
plus small builders for the blocks the sync writes itself."""

from __future__ import annotations

import re
from urllib.parse import urljoin

from bs4 import BeautifulSoup, NavigableString, Tag
from bs4.element import Comment

MAX_TEXT = 2000
MAX_RICH_TEXT_ITEMS = 100

BLOCK_TAGS = {
    "p", "div", "section", "article", "header", "footer", "main", "figure", "figcaption",
    "h1", "h2", "h3", "h4", "h5", "h6", "ul", "ol", "li", "pre", "blockquote", "table", "hr",
}
SKIP_TAGS = {"script", "style", "noscript", "iframe", "head", "title", "meta", "link"}
INLINE_STYLES = {
    "strong": "bold", "b": "bold", "em": "italic", "i": "italic", "u": "underline",
    "s": "strikethrough", "strike": "strikethrough", "del": "strikethrough", "code": "code",
}


# --- block builders -------------------------------------------------------
def text_item(content: str, link: str | None = None, **annotations: bool) -> dict:
    item: dict = {"type": "text", "text": {"content": content}}
    if link:
        item["text"]["link"] = {"url": link}
    if annotations:
        item["annotations"] = annotations
    return item


def block(kind: str, rich: list[dict] | None = None, **extra) -> dict:
    body = {"rich_text": rich or []}
    body.update(extra)
    return {"object": "block", "type": kind, kind: body}


def paragraph(*items: dict) -> dict:
    return block("paragraph", list(items))


def heading(level: int, text: str, toggleable: bool = False) -> dict:
    return block(f"heading_{level}", [text_item(text)], is_toggleable=toggleable)


def divider() -> dict:
    return {"object": "block", "type": "divider", "divider": {}}


def uploaded_file_block(upload_id: str, filename: str, mime_type: str) -> dict:
    kind = "pdf" if mime_type == "application/pdf" else "image" if mime_type.startswith("image/") else "file"
    body = {"type": "file_upload", "file_upload": {"id": upload_id}}
    if kind == "file":
        body["name"] = filename
    else:
        body["caption"] = [text_item(filename)]
    return {"object": "block", "type": kind, kind: body}


def heading3(text: str) -> dict:
    return block("heading_3", [text_item(text)])


def source_section(label: str, source_url: str | None) -> list[dict]:
    """The Source section every synced page ends with."""
    line = [text_item(f"{label} · ", color="gray")]
    line.append(text_item("Open original", link=source_url) if source_url
                else text_item("no link", color="gray"))
    return [heading(2, "Source"), paragraph(*line)]


def page_skeleton(content_heading: str, source_label: str, source_url: str | None) -> list[dict]:
    """Synced content goes inside a toggle heading so it can be replaced later without
    touching anything you write under "My notes"."""
    return [
        heading(2, content_heading, toggleable=True),
        *source_section(source_label, source_url),
        heading(2, "My notes"),
        paragraph(),
    ]


# --- HTML conversion ------------------------------------------------------
def html_to_blocks(html: str | None, base_url: str | None = None) -> list[dict]:
    if not html or not html.strip():
        return []
    converter = _Converter(base_url)
    converter.walk(BeautifulSoup(html, "html.parser"), frozenset(), None)
    converter.flush()
    return converter.blocks


class _Converter:
    def __init__(self, base_url: str | None):
        self.base_url = base_url
        self.blocks: list[dict] = []
        self.segments: list[tuple[str, frozenset, str | None]] = []
        self.mode = ["paragraph"]  # block type used when inline text is flushed

    def _abs(self, href: str | None) -> str | None:
        href = (href or "").strip()
        if not href or href.startswith(("#", "javascript:", "data:")):
            return None
        if href.startswith("mailto:"):
            return href
        if self.base_url:
            return urljoin(self.base_url + "/", href)
        return href if re.match(r"https?://", href) else None

    # inline text accumulation
    def add_text(self, text: str, styles: frozenset, link: str | None) -> None:
        text = re.sub(r"[ \t\r\n\f\v]+", " ", text)
        if not text:
            return
        if not self.segments or self.segments[-1][0].endswith(("\n", " ")):
            text = text.lstrip(" ")
        if text:
            self.segments.append((text, styles, link))

    def flush(self, kind: str | None = None) -> None:
        kind = kind or self.mode[-1]
        merged: list[tuple[str, frozenset, str | None]] = []
        for text, styles, link in self.segments:
            if merged and merged[-1][1] == styles and merged[-1][2] == link:
                merged[-1] = (merged[-1][0] + text, styles, link)
            else:
                merged.append((text, styles, link))
        self.segments = []
        if merged:  # trim surrounding whitespace of the whole run
            merged[0] = (merged[0][0].lstrip(), *merged[0][1:])
            merged[-1] = (merged[-1][0].rstrip(), *merged[-1][1:])
        items = []
        for text, styles, link in merged:
            for start in range(0, len(text), MAX_TEXT):
                piece = text[start:start + MAX_TEXT]
                if piece:
                    items.append(text_item(piece, link, **{s: True for s in sorted(styles)}))
        if not items:
            return
        for start in range(0, len(items), MAX_RICH_TEXT_ITEMS):
            self.blocks.append(block(kind, items[start:start + MAX_RICH_TEXT_ITEMS]))

    # tree walk
    def walk(self, node, styles: frozenset, link: str | None) -> None:
        for child in node.children:
            if isinstance(child, Comment):
                continue
            if isinstance(child, NavigableString):
                self.add_text(str(child), styles, link)
            elif isinstance(child, Tag):
                self.visit(child, styles, link)

    def visit(self, tag: Tag, styles: frozenset, link: str | None) -> None:
        name = tag.name.lower()
        if name in SKIP_TAGS:
            return
        if name in BLOCK_TAGS:
            self.flush()
            self.visit_block(tag, name)
            return
        if name == "br":
            self.segments.append(("\n", styles, link))
        elif name == "img":
            alt = (tag.get("alt") or "image").strip()
            self.add_text(f"[{alt}]", styles, self._abs(tag.get("src")))
        elif name == "a":
            self.walk(tag, styles, self._abs(tag.get("href")) or link)
        elif name in INLINE_STYLES:
            self.walk(tag, styles | {INLINE_STYLES[name]}, link)
        else:
            self.walk(tag, styles, link)

    def visit_block(self, tag: Tag, name: str) -> None:
        if name in {"h1", "h2", "h3"}:
            self.walk(tag, frozenset(), None)
            self.flush("heading_3")
        elif name in {"h4", "h5", "h6"}:
            self.walk(tag, frozenset({"bold"}), None)
            self.flush("paragraph")
        elif name in {"ul", "ol"}:
            kind = "numbered_list_item" if name == "ol" else "bulleted_list_item"
            for child in tag.children:
                if isinstance(child, Tag) and child.name == "li":
                    self.list_item(child, kind)
                elif isinstance(child, Tag):
                    self.visit(child, frozenset(), None)
                elif isinstance(child, NavigableString) and child.strip():
                    self.add_text(str(child), frozenset(), None)
            self.flush()
        elif name == "li":
            self.list_item(tag, "bulleted_list_item")
        elif name == "pre":
            code = tag.get_text()
            items = [text_item(code[i:i + MAX_TEXT]) for i in range(0, len(code), MAX_TEXT)][:MAX_RICH_TEXT_ITEMS]
            self.blocks.append(block("code", items, language="plain text"))
        elif name == "blockquote":
            self.mode.append("quote")
            self.walk(tag, frozenset(), None)
            self.flush()
            self.mode.pop()
        elif name == "hr":
            self.blocks.append(divider())
        elif name == "table":
            self.table(tag)
        else:  # p, div, section, ...
            self.walk(tag, frozenset(), None)
            self.flush()

    def list_item(self, li: Tag, kind: str) -> None:
        self.mode.append(kind)
        self.walk(li, frozenset(), None)
        self.flush()
        self.mode.pop()

    def table(self, tag: Tag) -> None:
        rows = [tr for tr in tag.find_all("tr") if tr.find_parent("table") is tag]
        grid = [[cell.get_text(" ", strip=True) for cell in tr.find_all(["td", "th"], recursive=False)]
                for tr in rows]
        grid = [row for row in grid if row]
        if not grid:
            return
        width = max(len(row) for row in grid)
        header = bool(rows and rows[0].find("th"))
        children = [{
            "object": "block", "type": "table_row",
            "table_row": {"cells": [[text_item(c[:MAX_TEXT])] if c else []
                                    for c in row + [""] * (width - len(row))]},
        } for row in grid[:100]]
        self.blocks.append({"object": "block", "type": "table", "table": {
            "table_width": width, "has_column_header": header, "has_row_header": False,
            "children": children,
        }})
