"""Markdown → Notion blocks, for converted lecture notes.

Handles what lecture notes need: headings, paragraphs, bulleted/numbered/to-do lists (one level of
nesting), fenced code, block and inline math ($$…$$, $…$), tables, quotes, dividers, links,
bold/italic/code/strikethrough. Images become a "[Figure: …]" line.
"""

from __future__ import annotations

import re

MAX_TEXT = 2000
MAX_ITEMS = 100

CODE_LANGUAGES = {
    "c": "c", "h": "c", "cpp": "c++", "c++": "c++", "java": "java", "python": "python", "py": "python",
    "bash": "bash", "sh": "shell", "shell": "shell", "console": "shell", "make": "makefile",
    "makefile": "makefile", "javascript": "javascript", "js": "javascript", "json": "json",
    "sql": "sql", "text": "plain text", "txt": "plain text", "latex": "latex", "tex": "latex",
    "markdown": "markdown", "md": "markdown", "html": "html", "go": "go", "rust": "rust",
    "haskell": "haskell", "ocaml": "ocaml", "scala": "scala", "kotlin": "kotlin", "lisp": "lisp",
}

INLINE = re.compile(
    r"(?P<escape>\\[\\`*_{}\[\]()#+\-.!$|~<>])"
    r"|(?P<math>\$(?!\s)[^$\n]+?(?<!\s)\$)"
    r"|(?P<code>`[^`\n]+`)"
    r"|(?P<image>!\[[^\]]*\]\([^)\s]+\))"
    r"|(?P<link>\[[^\]]+\]\([^)\s]+\))"
    r"|(?P<bold>\*\*(?!\s)(?:[^*\n]|\*(?!\*))+?\*\*|__[^_\n]+?__)"
    r"|(?P<strike>~~[^~\n]+?~~)"
    r"|(?P<italic>\*(?!\s)[^*\n]+?\*|(?<![A-Za-z0-9])_(?!\s)[^_\n]+?_(?![A-Za-z0-9]))"
)


def _text(content: str, link: str | None = None, **annotations) -> list[dict]:
    items = []
    for i in range(0, len(content), MAX_TEXT):
        item = {"type": "text", "text": {"content": content[i:i + MAX_TEXT]}}
        if link:
            item["text"]["link"] = {"url": link}
        if annotations:
            item["annotations"] = annotations
        items.append(item)
    return items


def inline(text: str) -> list[dict]:
    """Rich text items for one line or paragraph of Markdown."""
    items: list[dict] = []
    pos = 0
    for m in INLINE.finditer(text):
        if m.start() > pos:
            items += _text(text[pos:m.start()])
        kind, token = m.lastgroup, m.group(0)
        if kind == "escape":
            items += _text(token[1])
        elif kind == "math":
            items.append({"type": "equation", "equation": {"expression": token[1:-1]}})
        elif kind == "code":
            items += _text(token[1:-1], code=True)
        elif kind == "image":
            alt, url = re.match(r"!\[([^\]]*)\]\(([^)]+)\)", token).groups()
            items += _text(f"[Figure: {alt or 'image'}]", link=url if url.startswith("http") else None,
                           italic=True)
        elif kind == "link":
            label, url = re.match(r"\[([^\]]+)\]\(([^)]+)\)", token).groups()
            items += _styled(label, link=url if re.match(r"https?://|mailto:", url) else None)
        elif kind == "bold":
            items += _styled(token[2:-2], bold=True)
        elif kind == "strike":
            items += _styled(token[2:-2], strikethrough=True)
        elif kind == "italic":
            items += _styled(token[1:-1], italic=True)
        pos = m.end()
    if pos < len(text):
        items += _text(text[pos:])
    return items


def _styled(inner: str, link: str | None = None, **annotations) -> list[dict]:
    """Formatting that can contain other formatting: **`code`**, *step $i = 5$*, [**x**](url)."""
    items = inline(inner)
    for item in items:
        if annotations:
            item["annotations"] = {**item.get("annotations", {}), **annotations}
        if link and item["type"] == "text":
            item["text"]["link"] = {"url": link}
    return items


def _block(kind: str, rich: list[dict], **extra) -> list[dict]:
    """One block, split into several if it has more rich-text items than Notion allows."""
    chunks = [rich[i:i + MAX_ITEMS] for i in range(0, len(rich), MAX_ITEMS)] or [[]]
    return [{"object": "block", "type": kind, kind: {"rich_text": chunk, **extra}} for chunk in chunks]


def _code_language(tag: str) -> str:
    return CODE_LANGUAGES.get(tag.strip().lower(), "plain text")


def _table(rows: list[str]) -> list[dict]:
    def cells(line: str) -> list[str]:
        line = line.strip().strip("|")
        return [c.strip() for c in re.split(r"(?<!\\)\|", line)]

    parsed = [cells(r) for r in rows]
    header = len(parsed) > 1 and all(re.fullmatch(r":?-{2,}:?", c) for c in parsed[1] if c)
    if header:
        parsed = [parsed[0]] + parsed[2:]
    width = max(len(r) for r in parsed)
    children = [{"object": "block", "type": "table_row", "table_row": {
        "cells": [inline(c)[:MAX_ITEMS] if c else [] for c in r + [""] * (width - len(r))]}}
        for r in parsed[:100]]
    return [{"object": "block", "type": "table", "table": {
        "table_width": width, "has_column_header": header, "has_row_header": False, "children": children}}]


LIST_ITEM = re.compile(r"^(?P<indent>\s*)(?P<marker>[-*+]|\d+[.)])\s+(?P<rest>.*)$")


def to_blocks(markdown: str) -> list[dict]:
    lines = markdown.replace("\r\n", "\n").split("\n")
    blocks: list[dict] = []
    paragraph: list[str] = []
    last_top_item: dict | None = None
    i = 0

    def flush() -> None:
        nonlocal paragraph
        if paragraph:
            blocks.extend(_block("paragraph", inline("\n".join(paragraph))))
            paragraph = []

    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        if stripped.startswith("```"):                      # fenced code
            flush()
            language = _code_language(stripped[3:])
            body = []
            i += 1
            while i < len(lines) and not lines[i].strip().startswith("```"):
                body.append(lines[i])
                i += 1
            code = "\n".join(body)
            blocks.extend(_block("code", _text(code)[:MAX_ITEMS] or _text(" "), language=language))
            last_top_item = None
            i += 1
            continue

        if stripped.startswith("$$"):                       # block math
            flush()
            expr = stripped[2:]
            if expr.endswith("$$") and len(expr) >= 2:
                expr = expr[:-2]
            else:
                body = [expr]
                i += 1
                while i < len(lines) and "$$" not in lines[i]:
                    body.append(lines[i])
                    i += 1
                if i < len(lines):
                    body.append(lines[i].split("$$")[0])
                expr = "\n".join(body)
            blocks.append({"object": "block", "type": "equation", "equation": {"expression": expr.strip()}})
            last_top_item = None
            i += 1
            continue

        if not stripped:
            flush()
            i += 1
            continue

        if re.fullmatch(r"(-{3,}|\*{3,}|_{3,})", stripped):
            flush()
            blocks.append({"object": "block", "type": "divider", "divider": {}})
            last_top_item = None
            i += 1
            continue

        heading = re.match(r"^(#{1,6})\s+(.*?)\s*#*$", stripped)
        if heading:
            flush()
            level = len(heading.group(1))
            text = inline(heading.group(2))
            if level == 1:
                blocks.extend(_block("heading_2", text))
            elif level == 2:
                blocks.extend(_block("heading_3", text))
            else:
                for item in text:
                    item.setdefault("annotations", {})["bold"] = True
                blocks.extend(_block("paragraph", text))
            last_top_item = None
            i += 1
            continue

        if stripped.startswith("|"):                        # table
            flush()
            rows = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                rows.append(lines[i])
                i += 1
            blocks.extend(_table(rows))
            last_top_item = None
            continue

        if stripped.startswith(">"):                        # quote
            flush()
            quote = []
            while i < len(lines) and lines[i].strip().startswith(">"):
                quote.append(lines[i].strip()[1:].strip())
                i += 1
            blocks.extend(_block("quote", inline("\n".join(quote))))
            last_top_item = None
            continue

        item = LIST_ITEM.match(line)
        if item:
            flush()
            rest = item.group("rest")
            todo = re.match(r"^\[( |x|X)\]\s+(.*)$", rest)
            if todo:
                new = _block("to_do", inline(todo.group(2)), checked=todo.group(1).lower() == "x")[0]
            else:
                kind = "numbered_list_item" if item.group("marker")[0].isdigit() else "bulleted_list_item"
                new = _block(kind, inline(rest))[0]
            nested = len(item.group("indent").replace("\t", "    ")) >= 2
            if nested and last_top_item is not None:
                body = last_top_item[last_top_item["type"]]
                body.setdefault("children", []).append(new)
            else:
                blocks.append(new)
                last_top_item = new
            i += 1
            continue

        paragraph.append(stripped)
        last_top_item = None
        i += 1

    flush()
    return blocks
