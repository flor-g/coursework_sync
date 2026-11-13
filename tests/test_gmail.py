"""Email analysis (rules and Claude) and the Gmail sync, which summarizes the email pages the
Apps Script copies into Notion."""

import json
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from scheduleaid import notion as n
from scheduleaid.blocks import heading, paragraph, text_item
from scheduleaid.email_analysis import (
    ClaudeAnalyzer, Email, KnownAssignment, RuleAnalyzer, clean_subject, match_known, normalize_iso,
)
from scheduleaid.sync_gmail import GmailSync
from tests.fakes import FakeNotion
from tests.test_sync import course_props, make_config

TZ = ZoneInfo("America/New_York")
NOW = datetime(2026, 11, 2, 12, 0, tzinfo=TZ)


# --- helpers ---------------------------------------------------------------------------------
def test_subject_and_name_matching():
    assert clean_subject("[cs3157] [ANN] [LAB4] Lab 4 released") == "Lab 4 released"
    assert clean_subject("Re: Fwd: HW 6 question") == "HW 6 question"
    known = [KnownAssignment("Lab 4", None), KnownAssignment("Lab 1", None), KnownAssignment("HW 6", None),
             KnownAssignment("Programming Assignment 6 - Red-Black Tree Map", None)]
    assert match_known("[cs3157] [ANN] [LAB4] Lab 4 released", known) == ["Lab 4"]
    assert match_known("lab 10 posted", known) == []                 # Lab 1 ≠ Lab 10
    assert match_known("Homework 6 solutions", known) == ["HW 6"]
    assert match_known("PA 6 autograder fixed", known) == ["Programming Assignment 6 - Red-Black Tree Map"]


def test_normalize_iso():
    assert normalize_iso("2026-11-08T23:59", TZ) == "2026-11-08T23:59:00-05:00"
    assert normalize_iso("2026-11-11", TZ) == "2026-11-11"
    assert normalize_iso("next friday", TZ) is None and normalize_iso("", TZ) is None


def test_rule_analyzer():
    email = Email("m", "t", "[cs3157] [ANN] Lab 4 deadline extended", "Pat", "p@x", NOW, text="Now due Tue.")
    a = RuleAnalyzer().analyze("COMS W3157", [KnownAssignment("Lab 4", None)], email, TZ)
    assert (a.title, a.category, a.important) == ("Lab 4 deadline extended", "Deadline change", True)
    assert [r.name for r in a.related] == ["Lab 4"]


# --- Claude analyzer (fake client) ----------------------------------------------------------
class FakeClaude:
    def __init__(self, payload=None, stop_reason="end_turn"):
        self.payload, self.stop_reason = payload, stop_reason
        self.calls = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=lambda **kw: self.create(beta=True, **kw)))
        self.messages = SimpleNamespace(create=lambda **kw: self.create(beta=False, **kw))

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(stop_reason=self.stop_reason,
                               content=[SimpleNamespace(type="text", text=json.dumps(self.payload))])


CLAUDE_PAYLOAD = {
    "title": "Lab 4 released, due Sun Nov 8",
    "summary": "Lab 4 (mdb lookup) is out; submit via git by Sunday 11:59 PM.",
    "category": "Assignment", "important": True, "from_staff": True,
    "related_assignments": [{"name": "Lab 4", "due_stated": "2026-11-08T23:59"},
                            {"name": "Lab 99", "due_stated": ""}],          # invented: dropped
    "new_tasks": [{"name": "Fill out lab partner form", "due": "2026-11-04", "type": "Action item"},
                  {"name": "Lab 4", "due": "", "type": "Homework"}],         # already known: dropped
}


def test_claude_analyzer_request_and_validation():
    fake = FakeClaude(CLAUDE_PAYLOAD)
    email = Email("m", "t", "[cs3157] [ANN] [LAB4] Lab 4 released", "Pat", "p@x", NOW, text="...",
                  attachments=["lab4.pdf"])
    a = ClaudeAnalyzer("claude-haiku-5-5", client=fake).analyze(
        "COMS W3157", [KnownAssignment("Lab 4", "2026-11-08T23:59:00-05:00")], email, TZ)
    assert a.analyzer == "claude" and a.title == "Lab 4 released, due Sun Nov 8"
    assert [(r.name, r.due_stated) for r in a.related] == [("Lab 4", "2026-11-08T23:59:00-05:00")]
    assert [(t.name, t.due) for t in a.tasks] == [("Fill out lab partner form", "2026-11-04")]
    call = fake.calls[0]
    assert call["model"] == "claude-haiku-5-5" and call["beta"] is False
    assert call["output_config"]["format"]["type"] == "json_schema"
    assert call["output_config"]["effort"] == "low"
    assert "fallbacks" not in call                    # Haiku has no server-side fallback
    user = call["messages"][0]["content"]
    assert "Lab 4 | due 2026-11-08T23:59:00-05:00" in user and "Attachments: lab4.pdf" in user


def test_opus_requests_use_the_refusal_fallback():
    fake = FakeClaude(CLAUDE_PAYLOAD)
    email = Email("m", "t", "s", "Pat", "p@x", NOW, text="...")
    ClaudeAnalyzer("claude-opus-5-5", client=fake).analyze("C", [], email, TZ)
    call = fake.calls[0]
    assert call["beta"] is True and call["fallbacks"] == "default"
    assert call["betas"] == ["server-side-fallback-2026-07-01"]


@pytest.mark.parametrize("fake", [FakeClaude(CLAUDE_PAYLOAD, stop_reason="refusal"),
                                  FakeClaude({"not": "json"}, stop_reason="max_tokens")])
def test_claude_analyzer_falls_back_to_rules(fake):
    analyzer = ClaudeAnalyzer("claude-opus-5-5", client=fake)
    email = Email("m", "t", "[cs3157] [ANN] [LAB4] Lab 4 released", "Pat", "p@x", NOW, text="Out now.")
    a = analyzer.analyze("COMS W3157", [KnownAssignment("Lab 4", None)], email, TZ)
    assert a.analyzer == "rules" and a.title == "Lab 4 released"
    assert len(analyzer.failures) == 1


# --- sync ------------------------------------------------------------------------------------
def email_page(notion, course, mid, subject, paragraphs, sender="Pat Lee <plee@cs.example.edu>",
               sent="2026-10-30T10:00:00-04:00", files=()):
    """An Updates page exactly as the Apps Script creates it."""
    page = notion.add_page("updates", {
        "Name": n.title(subject), "Course": n.relation(course["id"]), "Date": n.date(sent),
        "Source": n.select("Gmail"), "Source ID": n.rich_text(f"gmail:{mid}"),
        "Source URL": n.url(f"https://mail.google.com/mail/?authuser=ab1234%40example.edu#all/t{mid}")})
    notion._store_blocks(page["id"], [heading(2, "Summary", True), heading(2, "Email", True),
                                      heading(2, "Attachments", True), heading(2, "Source"),
                                      paragraph(), heading(2, "My notes"), paragraph()])
    summary, email, attachments = [b["id"] for b in notion.children[page["id"]][:3]]
    notion._store_blocks(summary, [paragraph(text_item("Not summarized yet."))])
    notion._store_blocks(email, [paragraph(text_item(f"From: {sender}"))]
                         + [paragraph(text_item(p)) for p in paragraphs])
    notion._store_blocks(attachments, [{"object": "block", "type": "pdf", "pdf": {
        "type": "file_upload", "file_upload": {"id": "u1"}, "caption": [text_item(name)]}} for name in files]
        or [paragraph(text_item("No attachments."))])
    return page


@pytest.fixture
def world():
    notion = FakeNotion()
    ap = notion.add_page("courses", course_props("Advanced Programming", "COMS W3157"))
    lab4 = notion.add_page("assignments", {"Name": n.title("Lab 4"), "Course": n.relation(ap["id"]),
                                           "Due": n.date("2026-11-08T23:59:00-05:00"),
                                           "Source": n.select("Website"), "Status": n.status("Not started")})
    m1 = email_page(notion, ap, "m1", "[cs3157] [ANN] [LAB4] Lab 4 released",
                    ["Hi all,", "Lab 4 is out. Due Sunday 11/8."], files=["lab4.pdf"])
    return notion, ap, lab4, m1


def run_gmail(notion, analyzer=None):
    return GmailSync(make_config(), notion, analyzer or RuleAnalyzer(), now=NOW).run()


def toggle_children(notion, page, name):
    toggle = next(b for b in notion.children[page["id"]]
                  if b["type"] == "heading_2" and b["heading_2"]["rich_text"][0]["text"]["content"] == name)
    return notion.children[toggle["id"]]


def test_summarizes_pending_email_and_links_assignment(world):
    notion, ap, lab4, m1 = world
    report = run_gmail(notion)
    assert report.errors == [] and report.updated == 1
    page = notion.pages[m1["id"]]
    assert n.get_text(page, "Name") == "Lab 4 released"
    assert n.get_select(page, "Category") == "Assignment"
    assert page["properties"]["Important"] == {"checkbox": True}          # [ANN]
    assert page["properties"]["Related assignment"] == n.relation(lab4["id"])
    assert n.get_text(page, "Content hash") == "summarized-by-rules"
    summary = toggle_children(notion, page, "Summary")
    assert summary[0]["paragraph"]["rich_text"][0]["text"]["content"].startswith("Hi all, Lab 4 is out")
    # the email and attachments the Apps Script wrote are untouched
    assert len(toggle_children(notion, page, "Email")) == 3
    assert toggle_children(notion, page, "Attachments")[0]["type"] == "pdf"


def test_each_email_is_summarized_once(world):
    notion, *_ = world
    run_gmail(notion)
    notion.writes.clear()
    report = run_gmail(notion)
    assert notion.writes == [] and report.updated == 0


def test_claude_sees_rebuilt_email(world):
    notion, *_ = world
    fake = FakeClaude(CLAUDE_PAYLOAD)
    run_gmail(notion, ClaudeAnalyzer("claude-opus-5-5", client=fake))
    user = fake.calls[0]["messages"][0]["content"]
    assert "From: Pat Lee <plee@cs.example.edu>" in user
    assert "Subject: [cs3157] [ANN] [LAB4] Lab 4 released" in user
    assert "Hi all,\n\nLab 4 is out. Due Sunday 11/8." in user
    assert "Attachments: lab4.pdf" in user
    assert "Sent: 2026-10-30 10:00 EDT (Fri)" in user


def test_claude_deadline_mismatch_and_new_task(world):
    notion, ap, lab4, m1 = world
    payload = dict(CLAUDE_PAYLOAD, title="Lab 4 extended to Tue Nov 10", category="Announcement",
                   related_assignments=[{"name": "Lab 4", "due_stated": "2026-11-10T23:59"}])
    run_gmail(notion, ClaudeAnalyzer("claude-opus-5-5", client=FakeClaude(payload)))
    page = notion.pages[m1["id"]]
    assert n.get_select(page, "Category") == "Deadline change"           # upgraded: dates disagree
    assert page["properties"]["Important"] == {"checkbox": True}
    warning = toggle_children(notion, page, "Summary")[1]["paragraph"]["rich_text"][0]
    assert warning["annotations"] == {"color": "red"}
    assert "Tue, Nov 10, 11:59 PM for Lab 4; Notion has Sun, Nov 8, 11:59 PM" in warning["text"]["content"]
    assert n.get_date(notion.pages[lab4["id"]], "Due") == "2026-11-08T23:59:00-05:00"   # not overwritten
    task = next(p for p in notion.in_ds("assignments") if n.get_text(p, "Name") == "Fill out lab partner form")
    assert n.get_select(task, "Source") == "Gmail" and n.get_select(task, "Type") == "Action item"
    assert n.get_date(task, "Due") == "2026-11-04"
    assert {"id": task["id"]} in page["properties"]["Related assignment"]["relation"]


def test_email_for_archived_course_is_left_alone(world):
    notion, ap, lab4, m1 = world
    notion.pages[ap["id"]]["properties"]["Status"] = n.select("Archived")
    report = run_gmail(notion)
    assert report.updated == 0 and n.get_text(notion.pages[m1["id"]], "Content hash") == ""


def test_page_without_email_section_is_reported(world):
    notion, ap, *_ = world
    broken = notion.add_page("updates", {"Name": n.title("odd"), "Course": n.relation(ap["id"]),
                                         "Source": n.select("Gmail"), "Source ID": n.rich_text("gmail:x")})
    report = run_gmail(notion)
    assert any("no 'Email' section" in e for e in report.errors)
    assert n.get_text(notion.pages[broken["id"]], "Content hash") == ""


def test_rule_analyzer_staff_detection():
    known = [KnownAssignment("Lab 3", None)]
    ann = RuleAnalyzer().analyze("C", known, Email("m", "t", "[cs3157] [ANN] [LAB3] Lab 3 released", "J", "j@x", NOW), TZ)
    ed = RuleAnalyzer().analyze("C", known, Email("m", "t", "COMS W3134 002: HW4 Grades Released", "E", "e@x", NOW), TZ)
    student = RuleAnalyzer().analyze("C", known, Email("m", "t", "[cs3157] [LAB3] library question", "S", "s@x", NOW), TZ)
    reply = RuleAnalyzer().analyze("C", known, Email("m", "t", "Re: [cs3157] [ANN] Lab 3", "S", "s@x", NOW), TZ)
    digest = RuleAnalyzer().analyze("C", known, Email("m", "t", "New threads in COMS W1004 001", "Ed", "e@x", NOW), TZ)
    assert (ann.from_staff, ann.important, ann.category) == (True, True, "Assignment")
    assert (ed.from_staff, ed.category) == (True, "Grades")
    assert (student.from_staff, student.important, student.category) == (False, False, "Other")
    assert (reply.from_staff, reply.important) == (False, False)
    assert (digest.from_staff, digest.category) == (False, "Other")


def test_student_threads_are_not_important_and_not_copied(world):
    notion, ap, lab4, m1 = world
    payload = dict(CLAUDE_PAYLOAD, from_staff=False, important=True,
                   related_assignments=[{"name": "Lab 4", "due_stated": "2026-11-10T23:59"}])
    run_gmail(notion, ClaudeAnalyzer("claude-haiku-5-5", client=FakeClaude(payload)))
    page = notion.pages[m1["id"]]
    assert page["properties"]["Important"] == {"checkbox": False}       # not staff → never important
    assert n.get_select(page, "Category") == "Assignment"              # no date warning from students
    assert notion.children.get(lab4["id"], []) == []                   # nothing copied into Lab 4


def test_gradescope_receipt_marks_assignment_submitted(world):
    notion, ap, lab4, m1 = world
    receipt = email_page(notion, ap, "g1", "[COMS 3157 on Gradescope] Successfully submitted to Lab 4",
                         ["Your submission was received."], sender="Gradescope <no-reply@gradescope.com>")
    fake = FakeClaude(CLAUDE_PAYLOAD)
    run_gmail(notion, ClaudeAnalyzer("claude-haiku-5-5", client=fake))
    assert n.get_select(notion.pages[lab4["id"]], "Status") == "Submitted"
    page = notion.pages[receipt["id"]]
    assert n.get_text(page, "Name") == "Submitted Lab 4 on Gradescope"
    assert n.get_select(page, "Category") == "Other"
    assert page["properties"]["Related assignment"] == n.relation(lab4["id"])
    assert n.get_text(page, "Content hash") == "summarized-by-gradescope-receipt"
    assert len(fake.calls) == 1            # only the other email went to Claude


def test_large_backlog_is_split_across_runs(world, monkeypatch):
    import scheduleaid.sync_gmail as sg
    notion, ap, *_ = world
    for i in range(4):
        email_page(notion, ap, f"x{i}", f"[cs3157] [ANN] Note {i}", ["text"])
    monkeypatch.setattr(sg, "MAX_PER_RUN", 2)
    report = run_gmail(notion)
    assert report.updated == 2 and "5 emails waiting" in report.notes[0]


def test_tasks_only_from_staff_and_old_undated_tasks_are_done(world):
    notion, ap, lab4, m1 = world
    ad = email_page(notion, ap, "ad1", "[cs3157] [adv] Workshop", ["RSVP!"], sent="2026-10-31T10:00:00-04:00")
    old = email_page(notion, ap, "old1", "[cs3157] [ANN] Code review", ["Book a slot."], sent="2026-10-06T10:00:00-04:00")
    payloads = {
        "ad1": dict(CLAUDE_PAYLOAD, category="Other", from_staff=False, related_assignments=[],
                    new_tasks=[{"name": "RSVP for workshop", "due": "", "type": "Action item"}]),
        "old1": dict(CLAUDE_PAYLOAD, category="Logistics", from_staff=True, related_assignments=[],
                     new_tasks=[{"name": "Book a code review slot", "due": "", "type": "Action item"}]),
        "m1": dict(CLAUDE_PAYLOAD, related_assignments=[]),
    }

    class ByEmail(FakeClaude):
        """Answers with the payload for whichever email is in the request (by its subject)."""
        def create(self, **kwargs):
            content = kwargs["messages"][0]["content"]
            self.payload = next(v for k, v in payloads.items() if f"Subject: {k}\n" in content)
            return super().create(**kwargs)

    for key, page in (("ad1", ad), ("old1", old), ("m1", m1)):
        notion.pages[page["id"]]["properties"]["Name"] = n.title(key)
    run_gmail(notion, ClaudeAnalyzer("claude-haiku-5-5", client=ByEmail()))
    tasks = {n.get_text(p, "Name"): n.get_select(p, "Status") for p in notion.in_ds("assignments")
             if n.get_select(p, "Source") == "Gmail"}
    assert "RSVP for workshop" not in tasks                    # not from staff
    assert tasks["Book a code review slot"] == "Done"           # undated, email 4 weeks old
    assert tasks["Fill out lab partner form"] == "Not started"  # dated, from a staff email
