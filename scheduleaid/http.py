"""Shared HTTP helper with retries for rate limits and transient server errors."""

from __future__ import annotations

import logging
import time

import requests

log = logging.getLogger(__name__)

RETRY_STATUSES = {429, 500, 502, 503, 504}


class ApiError(RuntimeError):
    def __init__(self, service: str, response: requests.Response):
        self.status = response.status_code
        body = response.text[:500]
        super().__init__(f"{service} API error {response.status_code} on {response.request.method} "
                         f"{_redact(response.url)}: {body}")


def _redact(url: str) -> str:
    return url.split("?", 1)[0]


def request_with_retry(session: requests.Session, method: str, url: str, *, service: str,
                       attempts: int = 5, **kwargs) -> requests.Response:
    delay = 1.0
    for attempt in range(1, attempts + 1):
        try:
            response = session.request(method, url, timeout=60, **kwargs)
        except (requests.ConnectionError, requests.Timeout) as exc:
            if attempt == attempts:
                raise
            log.warning("%s: %s, retrying in %.0fs", service, exc.__class__.__name__, delay)
            time.sleep(delay)
            delay *= 2
            continue
        if response.status_code in RETRY_STATUSES and attempt < attempts:
            wait = float(response.headers.get("Retry-After", delay))
            log.warning("%s: HTTP %s, retrying in %.0fs", service, response.status_code, wait)
            time.sleep(wait)
            delay *= 2
            continue
        if response.status_code >= 400:
            raise ApiError(service, response)
        return response
    raise AssertionError("unreachable")
