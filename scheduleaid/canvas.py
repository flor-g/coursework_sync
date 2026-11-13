"""Read-only client for Canvas (called "CourseWorks" throughout this code, after Columbia's Canvas)."""

from __future__ import annotations

from datetime import datetime

import requests

from .http import request_with_retry


class CanvasClient:
    def __init__(self, base_url: str, token: str, session: requests.Session | None = None):
        self.base_url = base_url.rstrip("/")
        self.session = session or requests.Session()
        self.session.headers["Authorization"] = f"Bearer {token}"

    def _get_all(self, path: str, params: list[tuple[str, str | int]] | None = None) -> list[dict]:
        url: str | None = f"{self.base_url}/api/v1{path}"
        params = list(params or []) + [("per_page", 100)]
        items: list[dict] = []
        while url:
            response = request_with_retry(self.session, "GET", url, service="CourseWorks", params=params)
            items.extend(response.json())
            url = response.links.get("next", {}).get("url")
            params = None  # the "next" link already carries the query string
        return items

    def active_courses(self) -> list[dict]:
        return self._get_all("/courses", [("enrollment_state", "active"), ("include[]", "term"),
                                          ("include[]", "teachers")])

    def assignments(self, course_id: int | str) -> list[dict]:
        return self._get_all(f"/courses/{course_id}/assignments",
                             [("include[]", "submission"), ("order_by", "due_at")])

    def announcements(self, course_id: int | str, since: datetime) -> list[dict]:
        return self._get_all("/announcements", [
            ("context_codes[]", f"course_{course_id}"),
            ("start_date", since.date().isoformat()),
        ])

    def folders(self, course_id: int | str) -> list[dict]:
        return self._get_all(f"/courses/{course_id}/folders")

    def pages(self, course_id: int | str) -> list[dict]:
        return self._get_all(f"/courses/{course_id}/pages")

    def course(self, course_id: int | str, include: list[str] | None = None) -> dict:
        params = [("include[]", i) for i in include or []]
        return request_with_retry(self.session, "GET", f"{self.base_url}/api/v1/courses/{course_id}",
                                  service="CourseWorks", params=params).json()

    def tabs(self, course_id: int | str) -> list[dict]:
        return self._get_all(f"/courses/{course_id}/tabs")

    def files(self, course_id: int | str) -> list[dict]:
        return self._get_all(f"/courses/{course_id}/files", [("sort", "updated_at"), ("order", "desc")])

    def modules(self, course_id: int | str) -> list[dict]:
        return self._get_all(f"/courses/{course_id}/modules", [("include[]", "items")])

    def file(self, file_id: int | str, course_id: int | str | None = None,
             verifier: str | None = None) -> dict:
        path = f"/courses/{course_id}/files/{file_id}" if course_id else f"/files/{file_id}"
        params = [("verifier", verifier)] if verifier else None
        return request_with_retry(self.session, "GET", f"{self.base_url}/api/v1{path}",
                                  service="CourseWorks", params=params).json()

    def download(self, url: str) -> bytes:
        """Download a CourseWorks file URL (signed, or needing the token)."""
        return request_with_retry(self.session, "GET", url, service="CourseWorks").content

    def course_url(self, course_id: int | str) -> str:
        return f"{self.base_url}/courses/{course_id}"
