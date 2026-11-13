"""Minimal Notion API client plus helpers for reading and writing page properties."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Iterable

import requests

from .http import request_with_retry

log = logging.getLogger(__name__)

API = "https://api.notion.com/v1"
VERSION = "2025-09-03"
MAX_CHILDREN_PER_REQUEST = 100
MAX_TEXT = 2000
MAX_UPLOAD_BYTES = 5 * 1024 * 1024  # Notion free plan limit per file


class NotionClient:
    def __init__(self, token: str, session: requests.Session | None = None, dry_run: bool = False):
        self.dry_run = dry_run
        self.session = session or requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {token}",
            "Notion-Version": VERSION,
            "Content-Type": "application/json",
        })

    def _call(self, method: str, path: str, json: dict | None = None) -> dict:
        response = request_with_retry(self.session, method, f"{API}{path}", service="Notion", json=json)
        return response.json() if response.content else {}

    # --- reads -----------------------------------------------------------
    def query_all(self, data_source_id: str, filter: dict | None = None) -> list[dict]:
        pages: list[dict] = []
        body: dict[str, Any] = {"page_size": 100}
        if filter:
            body["filter"] = filter
        while True:
            data = self._call("POST", f"/data_sources/{data_source_id}/query", body)
            pages.extend(data.get("results", []))
            if not data.get("has_more"):
                return pages
            body["start_cursor"] = data["next_cursor"]

    def templates(self, data_source_id: str) -> list[dict]:
        """The data source's page templates: [{"id", "name", "is_default"}]."""
        return self._call("GET", f"/data_sources/{data_source_id}/templates").get("templates", [])

    def get_page(self, page_id: str) -> dict:
        return self._call("GET", f"/pages/{page_id}")

    def list_children(self, block_id: str) -> list[dict]:
        blocks: list[dict] = []
        cursor = None
        while True:
            path = f"/blocks/{block_id}/children?page_size=100"
            if cursor:
                path += f"&start_cursor={cursor}"
            data = self._call("GET", path)
            blocks.extend(data.get("results", []))
            if not data.get("has_more"):
                return blocks
            cursor = data["next_cursor"]

    # --- writes ----------------------------------------------------------
    def create_page(self, data_source_id: str, properties: dict, children: list[dict] | None = None,
                    template_id: str | None = None) -> dict:
        """With template_id, Notion fills in the page from that database template (shortly after)."""
        if self.dry_run:
            log.info("[dry-run] create page in %s: %s", data_source_id, title_of(properties))
            return {"id": "dry-run", "url": "", "properties": {}}
        body = {"parent": {"type": "data_source_id", "data_source_id": data_source_id},
                "properties": properties}
        if template_id:
            body["template"] = {"type": "template_id", "template_id": template_id}
        elif children:
            body["children"] = children
        return self._call("POST", "/pages", body)

    def update_page(self, page_id: str, properties: dict) -> dict:
        if self.dry_run:
            log.info("[dry-run] update page %s: %s", page_id, sorted(properties))
            return {"id": page_id}
        return self._call("PATCH", f"/pages/{page_id}", {"properties": properties})

    def append_children(self, block_id: str, children: list[dict], after: str | None = None) -> None:
        """Append blocks to a block's children; with `after`, insert them right after that child."""
        if self.dry_run:
            log.info("[dry-run] append %d blocks to %s", len(children), block_id)
            return
        for start in range(0, len(children), MAX_CHILDREN_PER_REQUEST):
            body: dict[str, Any] = {"children": children[start:start + MAX_CHILDREN_PER_REQUEST]}
            if after:
                body["after"] = after
            result = self._call("PATCH", f"/blocks/{block_id}/children", body)
            if after and result.get("results"):
                after = result["results"][-1]["id"]

    def upload_file(self, filename: str, content_type: str, data: bytes) -> str | None:
        """Upload a file (single part, up to 5 MB on the free plan); returns the file upload id."""
        if self.dry_run:
            log.info("[dry-run] upload %s (%d bytes)", filename, len(data))
            return None
        created = self._call("POST", "/file_uploads", {
            "mode": "single_part", "filename": filename, "content_type": content_type})
        request_with_retry(self.session, "POST", f"{API}/file_uploads/{created['id']}/send",
                           service="Notion", files={"file": (filename, data, content_type)},
                           headers={"Content-Type": None})  # let requests set the multipart boundary
        return created["id"]

    def update_block(self, block_id: str, body: dict) -> None:
        if self.dry_run:
            log.info("[dry-run] update block %s", block_id)
            return
        self._call("PATCH", f"/blocks/{block_id}", body)

    def delete_block(self, block_id: str) -> None:
        if self.dry_run:
            log.info("[dry-run] delete block %s", block_id)
            return
        self._call("DELETE", f"/blocks/{block_id}")


# --- property readers ------------------------------------------------------
def _plain(items: Iterable[dict]) -> str:
    return "".join(i.get("plain_text") or i.get("text", {}).get("content", "") for i in items or [])


def get_text(page: dict, name: str) -> str:
    prop = page.get("properties", {}).get(name) or {}
    if "title" in prop:
        return _plain(prop["title"])
    if "rich_text" in prop:
        return _plain(prop["rich_text"])
    return ""


def get_select(page: dict, name: str) -> str | None:
    prop = page.get("properties", {}).get(name) or {}
    value = prop.get("select") or prop.get("status")
    return value.get("name") if value else None


def get_date(page: dict, name: str) -> str | None:
    value = (page.get("properties", {}).get(name) or {}).get("date")
    return value.get("start") if value else None


def get_date_end(page: dict, name: str) -> str | None:
    value = (page.get("properties", {}).get(name) or {}).get("date")
    return value.get("end") if value else None


def get_url(page: dict, name: str) -> str | None:
    return (page.get("properties", {}).get(name) or {}).get("url")


def get_number(page: dict, name: str) -> float | None:
    return (page.get("properties", {}).get(name) or {}).get("number")


# --- property writers ------------------------------------------------------
def _text_items(text: str) -> list[dict]:
    return [{"type": "text", "text": {"content": text[i:i + MAX_TEXT]}}
            for i in range(0, len(text), MAX_TEXT)] if text else []


def title(text: str) -> dict:
    return {"title": _text_items(text)}


def rich_text(text: str) -> dict:
    return {"rich_text": _text_items(text)}


def select(name: str | None) -> dict:
    return {"select": {"name": name} if name else None}


def status(name: str) -> dict:
    return {"status": {"name": name}}


def date(iso: str | None, end: str | None = None) -> dict:
    if not iso:
        return {"date": None}
    return {"date": {"start": iso, "end": end} if end else {"start": iso}}


def url(value: str | None) -> dict:
    return {"url": value or None}


def number(value: float | None) -> dict:
    return {"number": value}


def checkbox(value: bool) -> dict:
    return {"checkbox": value}


def relation(*page_ids: str) -> dict:
    return {"relation": [{"id": pid} for pid in page_ids]}


def now_iso(tz) -> str:
    return datetime.now(tz).replace(microsecond=0).isoformat()


def title_of(properties: dict) -> str:
    for prop in properties.values():
        if "title" in prop:
            return _plain(prop["title"])
    return "?"
