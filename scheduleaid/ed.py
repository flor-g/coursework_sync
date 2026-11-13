"""Read-only client for Ed Discussion, and Ed's post format → Notion blocks.

Ed has no published API; this uses the one its web app uses, with a personal API token
(edstem.org → Settings → API Tokens), sent as a Bearer token. Responses:
- GET /api/user                      {"user": {...}, "courses": [{"course": {id, code, name, year, session, status}, "role": {...}}]}
- GET /api/courses/{id}/threads      {"threads": [thread...], "users": [{id, name, course_role}...]}
- GET /api/threads/{id}?view=1       {"thread": {..., "answers": [...], "comments": [...]}, "users": [...]}
A thread has id, number, type (post/question/announcement), title, content (Ed's XML), category,
subcategory, subsubcategory, user_id, is_private, is_anonymous, reply_count, created_at, updated_at.
Answers and comments have user_id, content, is_endorsed, created_at and nested "comments".
"""

from __future__ import annotations

import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup, NavigableString, Tag

from .blocks import text_item
from .http import request_with_retry

STAFF_ROLES = {"staff", "admin", "mentor", "tutor", "instructor", "ta"}
MAX_TEXT = 2000


class EdClient:
    def __init__(self, token: str, region: str = "us", session: requests.Session | None = None):
        self.api = f"https://{region}.edstem.org/api"
        self.web = f"https://edstem.org/{region}"
        self.session = session or requests.Session()
        self.session.headers["Authorization"] = f"Bearer {token}"

    def _get(self, path: str, params: dict | None = None) -> dict:
        return request_with_retry(self.session, "GET", f"{self.api}{path}", service="Ed", params=params).json()

    def courses(self) -> list[dict]:
        """Your Ed courses: [{"id", "code", "name", "year", "session", "status"}]."""
        return [c.get("course", c) for c in self._get("/user").get("courses", [])]

    def threads(self, course_id: int | str, since: datetime, page_size: int = 100,
                max_pages: int = 20) -> tuple[list[dict], dict[int, dict]]:
        """Threads created since `since`, newest first, and the users they mention by id."""
        threads, users = [], {}
        for page in range(max_pages):
            data = self._get(f"/courses/{course_id}/threads",
                             {"limit": page_size, "offset": page * page_size, "sort": "new"})
            batch = data.get("threads", [])
            users.update({u["id"]: u for u in data.get("users", []) if "id" in u})
            for t in batch:
                created = _parse_time(t.get("created_at"))
                if created and created < since:
                    return threads, users
                threads.append(t)
            if len(batch) < page_size:
                break
        return threads, users

    def thread(self, thread_id: int | str) -> tuple[dict, dict[int, dict]]:
        data = self._get(f"/threads/{thread_id}", {"view": 1})
        return data.get("thread", {}), {u["id"]: u for u in data.get("users", []) if "id" in u}

    def thread_url(self, course_id, thread_id) -> str:
        return f"{self.web}/courses/{course_id}/discussion/{thread_id}"

    def course_url(self, course_id) -> str:
        return f"{self.web}/courses/{course_id}/discussion"


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def is_staff(user: dict | None) -> bool:
    return bool(user) and str(user.get("course_role", "")).lower() in STAFF_ROLES


# --- Ed's post format (XML) → Notion blocks -----------------------------------------
INLINE_STYLE = {"bold": "bold", "b": "bold", "strong": "bold", "italic": "italic", "i": "italic",
                "em": "italic", "underline": "underline", "u": "underline", "strike": "strikethrough",
                "code": "code"}
CODE_LANGUAGES = {"c": "c", "cpp": "c++", "c++": "c++", "java": "java", "python": "python",
                  "py": "python", "bash": "bash", "sh": "shell", "javascript": "javascript",
                  "js": "javascript", "sql": "sql", "makefile": "makefile", "latex": "latex"}


def safe_url(url: str | None) -> str | None:
    """Notion rejects relative and malformed link targets; keep only web and mail links."""
    url = (url or "").strip()
    if url.startswith("//"):
        url = "https:" + url
    if re.match(r"^(https?://[^\s/]+\.[^\s]*|mailto:[^\s@]+@[^\s]+)$", url) and len(url) <= 2000:
        return url
    return None


def _rich(node, annotations: dict | None = None, link: str | None = None) -> list[dict]:
    annotations = annotations or {}
    items: list[dict] = []
    for child in node.children:
        if isinstance(child, NavigableString):
            text = str(child)
            if text:
                for i in range(0, len(text), MAX_TEXT):
                    items.append(text_item(text[i:i + MAX_TEXT], link=link, **annotations))
        elif isinstance(child, Tag):
            name = child.name.lower()
            if name == "break":
                items.append(text_item("\n"))
            elif name == "math":
                items.append({"type": "equation", "equation": {"expression": child.get_text()}})
            elif name == "ed-link":
                items += _rich(child, annotations, safe_url(child.get("href")) or link)
            elif name in INLINE_STYLE:
                items += _rich(child, {**annotations, INLINE_STYLE[name]: True}, link)
            else:
                items += _rich(child, annotations, link)
    return items[:100]


def _block(kind: str, rich: list[dict], **extra) -> dict:
    return {"object": "block", "type": kind, kind: {"rich_text": rich or [text_item("")], **extra}}


def document_blocks(content: str, max_blocks: int = 40) -> list[dict]:
    """Ed's XML post body (<document><paragraph>…) as Notion blocks, flattened to one level."""
    # HTML parsers treat <link> as an empty element, so give Ed's link tag another name first.
    content = re.sub(r"<(/?)link\b", r"<\1ed-link", content or "")
    soup = BeautifulSoup(content, "html.parser")
    root = soup.find("document") or soup
    out: list[dict] = []

    def walk(node, list_style: str | None = None):
        for child in node.children:
            if len(out) >= max_blocks:
                return
            if isinstance(child, NavigableString):
                if child.strip():
                    out.append(_block("paragraph", [text_item(child.strip())]))
                continue
            name = child.name.lower()
            if name == "paragraph":
                kind = {"bullet": "bulleted_list_item", "number": "numbered_list_item"}.get(list_style, "paragraph")
                out.append(_block(kind, _rich(child)))
            elif name == "heading":
                out.append(_block("heading_3", _rich(child)))
            elif name == "list":
                walk(child, child.get("style") or "bullet")
            elif name == "list-item":
                walk(child, list_style)
            elif name in ("snippet", "pre"):
                code = "\n".join(f.get_text() for f in child.find_all("snippet-file")) or child.get_text()
                language = CODE_LANGUAGES.get((child.get("language") or "").lower(), "plain text")
                pieces = [code[i:i + MAX_TEXT] for i in range(0, len(code), MAX_TEXT)][:100] or [" "]
                out.append(_block("code", [text_item(p) for p in pieces], language=language))
            elif name == "callout":
                out.append(_block("callout", _rich(child), color="gray_background"))
            elif name == "math":
                out.append({"object": "block", "type": "equation", "equation": {"expression": child.get_text()}})
            elif name in ("figure", "image"):
                img = child if name == "image" else child.find("image")
                src = safe_url(img.get("src")) if img else None
                out.append(_block("paragraph", [text_item("[Image on Ed]", link=src, italic=True)]
                                  if src else [text_item("[Image on Ed]", italic=True)]))
            elif name in ("document",):
                walk(child, list_style)
            else:
                rich = _rich(child)
                if any((i.get("text") or {}).get("content", "").strip() for i in rich):
                    out.append(_block("paragraph", rich))
    walk(root)
    if len(out) >= max_blocks:
        out.append(_block("paragraph", [text_item("… (continues on Ed)", color="gray")]))
    return out


def plain(content: str) -> str:
    return re.sub(r"\s+", " ", BeautifulSoup(content or "", "html.parser").get_text(" ")).strip()
