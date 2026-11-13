"""On-demand lecture conversion with a fake Claude client and fake downloads."""

from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from scheduleaid import notion as n
from scheduleaid.blocks import heading, paragraph, text_item
from scheduleaid.convert import SYSTEM_PROMPT, LectureConverter
from tests.fakes import FakeNotion
from tests.test_sync import make_config

TZ = ZoneInfo("America/New_York")
NOW = datetime(2026, 11, 2, 12, 0, tzinfo=TZ)
PDF = b"%PDF-1.4 fake lecture"


class FakeStream:
    def __init__(self, message):
        self.message = message

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get_final_message(self):
        return self.message


class FakeClaude:
    def __init__(self, text="# Lecture 14\n\n## Graphs\n- $G=(V,E)$", stop_reason="end_turn"):
        self.text, self.stop_reason, self.calls = text, stop_reason, []
        self.beta = SimpleNamespace(messages=SimpleNamespace(stream=lambda **kw: self.stream(True, kw)))
        self.messages = SimpleNamespace(stream=lambda **kw: self.stream(False, kw))

    def stream(self, beta, kwargs):
        self.calls.append(dict(kwargs, beta=beta))
        return FakeStream(SimpleNamespace(stop_reason=self.stop_reason,
                                          content=[SimpleNamespace(type="text", text=self.text)]))


def lecture(notion, conversion="Requested", url="https://x.edu/lectures/Lecture14.pdf",
            edited="2026-11-02T16:59:00.000Z"):
    page = notion.add_page("lectures", {"Name": n.title("Lecture 14: Graphs"),
                                        "Conversion status": n.select(conversion), "Original URL": n.url(url)})
    notion.pages[page["id"]]["last_edited_time"] = edited
    notion._store_blocks(page["id"], [heading(2, "Notes", True), heading(2, "Files", True),
                                      heading(2, "Source"), paragraph(), heading(2, "My notes"), paragraph()])
    notion._store_blocks(notion.children[page["id"]][0]["id"],
                         [paragraph(text_item("Not converted to Markdown yet."))])
    return page


def converter(notion, claude=None, files=None, **kw):
    files = files or {"https://x.edu/lectures/Lecture14.pdf": PDF}
    return LectureConverter(make_config(convert_model="claude-opus-5-5"), notion, client=claude or FakeClaude(),
                            fetch=lambda url: files[url], now=NOW, **kw)


def notes(notion, page):
    return notion.children[notion.children[page["id"]][0]["id"]]


def test_converts_requested_lecture_with_claude():
    notion = FakeNotion()
    page = lecture(notion)
    claude = FakeClaude()
    conv = converter(notion, claude)
    conv.convert_page_id(page["id"])
    assert n.get_select(notion.pages[page["id"]], "Conversion status") == "Converted"
    blocks = notes(notion, page)
    assert blocks[0]["paragraph"]["rich_text"][0]["text"]["content"].startswith(
        "Rewritten as lecture notes on Nov 2 with claude-opus-5-5, from Lecture14.pdf.")
    assert [b["type"] for b in blocks[1:]] == ["heading_2", "heading_3", "bulleted_list_item"]
    call = claude.calls[0]
    assert call["model"] == "claude-opus-5-5" and call["beta"] is True and call["fallbacks"] == "default"
    assert call["max_tokens"] == 64000 and call["output_config"] == {"effort": "medium"}
    doc = call["messages"][0]["content"][0]
    assert doc["type"] == "document" and doc["source"]["media_type"] == "application/pdf"
    assert conv.report.errors == []


def test_not_requested_is_skipped():
    notion = FakeNotion()
    page = lecture(notion, conversion="Pending")
    claude = FakeClaude()
    converter(notion, claude).convert_page_id(page["id"])
    assert claude.calls == [] and n.get_select(notion.pages[page["id"]], "Conversion status") == "Pending"


def test_cut_off_conversion_needs_review():
    notion = FakeNotion()
    page = lecture(notion)
    converter(notion, FakeClaude(stop_reason="max_tokens")).convert_page_id(page["id"])
    assert n.get_select(notion.pages[page["id"]], "Conversion status") == "Needs review"
    assert "cut off" in notes(notion, page)[0]["paragraph"]["rich_text"][0]["text"]["content"]


@pytest.mark.parametrize("claude,files,message", [
    (FakeClaude(stop_reason="refusal"), None, "declined"),
    (FakeClaude(), {"https://x.edu/lectures/Lecture14.pdf": b"<html>not a pdf</html>"}, "not a PDF"),
])
def test_failures_are_shown_on_the_page(claude, files, message):
    notion = FakeNotion()
    page = lecture(notion)
    conv = converter(notion, claude, files)
    conv.convert_page_id(page["id"])
    assert n.get_select(notion.pages[page["id"]], "Conversion status") == "Needs review"
    shown = notes(notion, page)[0]["paragraph"]["rich_text"][0]
    assert message in shown["text"]["content"] and shown["annotations"] == {"color": "red"}
    assert message in conv.report.errors[0]


def test_run_waiting_picks_requested_and_stuck_but_not_active():
    notion = FakeNotion()
    requested = lecture(notion)
    active = lecture(notion, conversion="Converting", edited="2026-11-02T16:55:00.000Z")   # 5 min ago
    stuck = lecture(notion, conversion="Converting", edited="2026-11-02T15:00:00.000Z")    # 2 h ago
    done = lecture(notion, conversion="Converted")
    claude = FakeClaude()
    converter(notion, claude).run_waiting()
    status = {k: n.get_select(notion.pages[p["id"]], "Conversion status")
              for k, p in dict(requested=requested, active=active, stuck=stuck, done=done).items()}
    assert status == {"requested": "Converted", "active": "Converting", "stuck": "Converted", "done": "Converted"}
    assert len(claude.calls) == 2


def test_prompt_asks_for_lecture_notes_not_slides():
    for phrase in ("lecture notes", "Number the sections", "Reason:", "full sentences", "> **Note:** "):
        assert phrase in SYSTEM_PROMPT


def test_powerpoint_is_turned_into_a_pdf_then_rewritten():
    notion = FakeNotion()
    page = lecture(notion, url="https://x.edu/1004-3.ppt")
    claude, seen = FakeClaude(), []

    def to_pdf(data, filename):
        seen.append((data, filename))
        return PDF

    converter(notion, claude, {"https://x.edu/1004-3.ppt": b"\xd0\xcf\x11\xe0 ppt"}, to_pdf=to_pdf,
              can_convert_office=lambda: True).convert_page_id(page["id"])
    assert seen == [(b"\xd0\xcf\x11\xe0 ppt", "1004-3.ppt")]
    assert claude.calls and n.get_select(notion.pages[page["id"]], "Conversion status") == "Converted"
    assert "from 1004-3.ppt" in notes(notion, page)[0]["paragraph"]["rich_text"][0]["text"]["content"]


def test_sync_without_libreoffice_hands_powerpoint_back_to_the_workflow():
    notion = FakeNotion()
    page = lecture(notion, url="https://x.edu/1004-3.ppt")
    claude = FakeClaude()
    conv = converter(notion, claude, {"https://x.edu/1004-3.ppt": b"ppt"}, can_convert_office=lambda: False)
    conv.run_waiting()
    assert claude.calls == [] and n.get_select(notion.pages[page["id"]], "Conversion status") == "Requested"
    assert conv.report.errors == []


def test_courseworks_file_page_is_located_through_the_api():
    notion = FakeNotion()
    page = lecture(notion, url="https://canvas.example.edu/courses/300646/files/27597758")
    located = []

    def locate(url):
        located.append(url)
        return "https://signed/download", "Mathematics Review.pdf"

    conv = converter(notion, FakeClaude(), {"https://signed/download": PDF}, locate=locate)
    conv.convert_page_id(page["id"])
    assert located == ["https://canvas.example.edu/courses/300646/files/27597758"]
    assert n.get_select(notion.pages[page["id"]], "Conversion status") == "Converted"


def test_course_and_instructor_are_named_in_the_request():
    notion = FakeNotion()
    course = notion.add_page("courses", {"Name": n.title("Data Structures"), "Code": n.rich_text("COMS W3134"),
                                         "Instructor": n.rich_text("Prof. Rivera")})
    page = lecture(notion)
    notion.pages[page["id"]]["properties"]["Course"] = n.relation(course["id"])
    claude = FakeClaude()
    converter(notion, claude).convert_page_id(page["id"])
    text = claude.calls[0]["messages"][0]["content"][1]["text"]
    assert "COMS W3134 Data Structures, taught by Prof. Rivera" in text
