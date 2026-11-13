"""HTTP clients against a stub session: pagination, headers, retries, dry-run."""

import json

import pytest
import requests

from scheduleaid import http as http_mod
from scheduleaid.canvas import CanvasClient
from scheduleaid.http import ApiError
from scheduleaid.notion import NotionClient


class StubResponse:
    def __init__(self, status=200, body=None, links=None, headers=None, url="https://x/y"):
        self.status_code = status
        self._body = body if body is not None else {}
        self.links = links or {}
        self.headers = headers or {}
        self.content = json.dumps(self._body).encode()
        self.text = json.dumps(self._body)
        self.url = url
        self.request = type("Req", (), {"method": "GET"})()

    def json(self):
        return self._body


class StubSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.headers = {}

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return self.responses.pop(0)


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(http_mod.time, "sleep", lambda s: None)


def test_canvas_follows_pagination_and_sends_token():
    session = StubSession([
        StubResponse(body=[{"id": 1}], links={"next": {"url": "https://cw/api/v1/courses?page=2"}}),
        StubResponse(body=[{"id": 2}]),
    ])
    client = CanvasClient("https://cw", "secret", session=session)
    assert [c["id"] for c in client.active_courses()] == [1, 2]
    assert session.headers["Authorization"] == "Bearer secret"
    first, second = session.calls
    assert ("include[]", "term") in first[2]["params"]
    assert second[1] == "https://cw/api/v1/courses?page=2" and second[2]["params"] is None


def test_retries_rate_limit_then_succeeds():
    session = StubSession([StubResponse(429, headers={"Retry-After": "1"}), StubResponse(body=[])])
    assert CanvasClient("https://cw", "t", session=session).active_courses() == []
    assert len(session.calls) == 2


def test_client_error_raises_without_retry():
    session = StubSession([StubResponse(401, body={"errors": "Invalid access token"})])
    with pytest.raises(ApiError) as exc:
        CanvasClient("https://cw", "t", session=session).active_courses()
    assert exc.value.status == 401 and len(session.calls) == 1


def test_notion_query_paginates_with_cursor():
    session = StubSession([
        StubResponse(body={"results": [{"id": "a"}], "has_more": True, "next_cursor": "c1"}),
        StubResponse(body={"results": [{"id": "b"}], "has_more": False}),
    ])
    client = NotionClient("tok", session=session)
    assert [p["id"] for p in client.query_all("ds1", {"property": "Source"})] == ["a", "b"]
    assert session.headers["Notion-Version"] == "2025-09-03"
    method, url, kwargs = session.calls[1]
    assert method == "POST" and url.endswith("/data_sources/ds1/query")
    assert kwargs["json"]["start_cursor"] == "c1"


def test_notion_append_chunks_by_100():
    session = StubSession([StubResponse(), StubResponse(), StubResponse()])
    NotionClient("tok", session=session).append_children("b1", [{"n": i} for i in range(250)])
    assert [len(c[2]["json"]["children"]) for c in session.calls] == [100, 100, 50]


def test_notion_dry_run_never_writes():
    session = StubSession([])
    client = NotionClient("tok", session=session, dry_run=True)
    client.create_page("ds", {"Name": {"title": [{"text": {"content": "x"}}]}})
    client.update_page("p", {"Name": {}})
    client.append_children("p", [{}])
    client.delete_block("b")
    assert session.calls == []


def test_error_message_omits_query_string():
    err = ApiError("CourseWorks", StubResponse(500, url="https://cw/api?access_token=SECRET"))
    assert "SECRET" not in str(err)


def test_connection_error_retries(monkeypatch):
    class Flaky(StubSession):
        def request(self, method, url, **kwargs):
            self.calls.append(url)
            if len(self.calls) == 1:
                raise requests.ConnectionError("reset")
            return StubResponse(body=[])

    session = Flaky([])
    assert CanvasClient("https://cw", "t", session=session).active_courses() == []
