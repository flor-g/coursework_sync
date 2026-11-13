from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from scheduleaid import notion as n
from scheduleaid.config import Config
from scheduleaid.sync_common import human_dt, same_minute, to_local_iso
from scheduleaid.sync_courseworks import (
    CourseWorksSync, advance_status, guess_type, initial_status, is_gradebook_only, match_course,
)
from tests.fakes import FakeCanvas, FakeNotion

TZ = ZoneInfo("America/New_York")
NOW = datetime(2026, 11, 2, 12, 0, tzinfo=TZ)


def make_config(**overrides):
    values = dict(
        canvas_base_url="https://canvas.example.edu", canvas_token="x", notion_token="y",
        timezone="America/New_York", courses_ds="courses", assignments_ds="assignments",
        lectures_ds="lectures", updates_ds="updates", files_ds="files",
        announcements_lookback_days=120,
    )
    values.update(overrides)
    return Config(**values)


def course_props(name, code, cw_id=""):
    return {"Name": n.title(name), "Code": n.rich_text(code), "Status": n.select("Active"),
            "CourseWorks ID": n.rich_text(cw_id)}


def assignment(aid, name, due_at, **extra):
    a = {"id": aid, "name": name, "due_at": due_at, "points_possible": 10,
         "html_url": f"https://canvas.example.edu/courses/111/assignments/{aid}",
         "description": f"<p>Instructions for {name}</p>", "submission_types": ["online_upload"],
         "submission": {"workflow_state": "unsubmitted", "submitted_at": None, "missing": False},
         "published": True}
    a.update(extra)
    return a


CANVAS_COURSES = [
    {"id": 111, "course_code": "COMSW3134_001_2026_3", "name": "Data Structures in Java"},
    {"id": 222, "course_code": "COMSW3203_001_2026_3", "name": "Discrete Mathematics"},
]


@pytest.fixture
def world():
    notion = FakeNotion()
    ds_page = notion.add_page("courses", course_props("Data Structures", "COMS W3134"))
    canvas = FakeCanvas(CANVAS_COURSES, {111: [
        assignment(1, "HW 4", "2026-11-08T04:59:59Z"),            # Nov 7, 11:59 PM ET
        assignment(2, "HW 1", "2026-09-20T03:59:59Z"),            # long past, not submitted
        assignment(3, "HW 3", "2026-10-25T03:59:59Z",
                   submission={"workflow_state": "submitted", "submitted_at": "2026-10-24T20:00:00Z"}),
        assignment(4, "Draft", None, published=False),
    ]}, {111: [{"id": 9, "title": "Midterm room", "message": "<p>Room 309</p>",
                "posted_at": "2026-11-01T15:00:00Z", "html_url": "https://cw/ann/9"}]})
    return notion, canvas, ds_page


def run(world, now=NOW):
    notion, canvas, _ = world
    return CourseWorksSync(make_config(), canvas, notion, now=now).run()


def by_name(notion, ds):
    return {n.get_text(p, "Name"): p for p in notion.in_ds(ds)}


# --- pure helpers -----------------------------------------------------------
def test_match_course_by_code():
    best, matches = match_course("COMS W3134", CANVAS_COURSES)
    assert best["id"] == 111 and len(matches) == 1
    assert match_course("COMS W9999", CANVAS_COURSES) == (None, [])


def test_match_course_prefers_own_code_then_newest():
    courses = CANVAS_COURSES + [
        {"id": 333, "course_code": "CROSSLIST_X", "name": "COMSW3134 recitation"},
        {"id": 444, "course_code": "COMSW3134_R01_2026_3", "name": "Data Structures recitation"},
    ]
    best, matches = match_course("COMS W3134", courses)
    assert len(matches) == 3 and best["id"] == 444


def test_time_helpers():
    assert to_local_iso("2026-11-08T04:59:59Z", TZ) == "2026-11-07T23:59:59-05:00"
    assert same_minute("2026-11-07T23:59:00.000-05:00", "2026-11-08T04:59:59Z", TZ)
    assert not same_minute("2026-11-07T23:59:00-05:00", "2026-11-09T04:59:59Z", TZ)
    assert same_minute(None, None, TZ) and not same_minute(None, "2026-11-07", TZ)
    assert human_dt("2026-11-07T23:59:00-05:00", TZ) == "Sat, Nov 7, 11:59 PM"


def test_statuses():
    submitted = assignment(1, "x", None, submission={"submitted_at": "2026-10-01T00:00:00Z"})
    excused = assignment(1, "x", None, submission={"excused": True})
    paper_graded = assignment(1, "x", None, submission_types=["on_paper"],
                              submission={"workflow_state": "graded"})
    online_graded_missing = assignment(1, "x", None, submission={"workflow_state": "graded"})
    assert advance_status("Not started", submitted) == "Submitted"
    assert advance_status("In progress", paper_graded) == "Submitted"
    assert advance_status("Done", submitted) is None          # never moves backwards
    assert advance_status("Not started", excused) == "Done"
    assert advance_status("Not started", online_graded_missing) is None
    past = assignment(1, "x", None, submission={"missing": True})
    assert initial_status(past, "2026-09-01T00:00:00-04:00", NOW, TZ) == "Not started"


def test_guess_type():
    assert guess_type({"name": "Midterm Exam"}) == "Exam"
    assert guess_type({"name": "Weekly check", "submission_types": ["online_quiz"]}) == "Quiz"
    assert guess_type({"name": "Final Project Proposal"}) == "Project"
    assert guess_type({"name": "Problem Set 3"}) == "Homework"


# --- full sync --------------------------------------------------------------
def test_first_sync_creates_everything(world):
    notion, canvas, course_page = world
    report = run(world)
    assert report.errors == []

    course = notion.pages[course_page["id"]]
    assert n.get_text(course, "CourseWorks ID") == "111"
    assert n.get_url(course, "CourseWorks URL").endswith("/courses/111")

    pages = by_name(notion, "assignments")
    assert set(pages) == {"HW 4", "HW 1", "HW 3"}         # unpublished draft skipped
    hw4 = pages["HW 4"]
    assert n.get_select(hw4, "Status") == "Not started"
    assert n.get_select(hw4, "Source") == "CourseWorks"
    assert n.get_text(hw4, "Source ID") == "canvas:assignment:1"
    assert n.get_date(hw4, "Due") == "2026-11-07T23:59:59-05:00"
    assert n.get_select(pages["HW 1"], "Status") == "Done"       # long past at first import
    assert n.get_select(pages["HW 3"], "Status") == "Submitted"

    # body: toggle "Instructions" with converted content, then Source and My notes
    top = notion.children[hw4["id"]]
    assert [b["type"] for b in top] == ["heading_2", "heading_2", "paragraph", "heading_2", "paragraph"]
    assert top[0]["heading_2"]["is_toggleable"] is True
    instructions = notion.children[top[0]["id"]]
    assert instructions[0]["paragraph"]["rich_text"][0]["text"]["content"] == "Synced from CourseWorks"
    assert instructions[1]["paragraph"]["rich_text"][0]["text"]["content"] == "Instructions for HW 4"

    updates = by_name(notion, "updates")
    assert n.get_select(updates["Midterm room"], "Category") == "Announcement"
    assert report.created == 4


def test_second_sync_is_a_no_op(world):
    notion, _, _ = world
    run(world)
    notion.writes.clear()
    report = run(world)
    assert notion.writes == []
    assert report.created == 0 and report.updated == 0 and report.unchanged == 4


def test_due_date_change_updates_page_and_logs_update(world):
    notion, canvas, _ = world
    run(world)
    canvas.by_course[111][0]["due_at"] = "2026-11-10T04:59:59Z"
    report = run(world)

    hw4 = by_name(notion, "assignments")["HW 4"]
    assert n.get_date(hw4, "Due") == "2026-11-09T23:59:59-05:00"
    change = next(p for p in notion.in_ds("updates") if n.get_select(p, "Category") == "Deadline change")
    assert n.get_text(change, "Name") == "HW 4: due date changed to Mon, Nov 9, 11:59 PM"
    assert change["properties"]["Important"] == {"checkbox": True}
    assert report.notes[-1].startswith("Deadline change")

    notion.writes.clear()
    run(world)  # the change is only logged once
    assert not any(w[0] == "create" for w in notion.writes)


def test_submission_flips_status_but_manual_done_is_kept(world):
    notion, canvas, _ = world
    run(world)
    pages = by_name(notion, "assignments")
    notion.update_page(pages["HW 3"]["id"], {"Status": n.status("Done")})   # user marked done
    canvas.by_course[111][0]["submission"] = {"workflow_state": "submitted",
                                              "submitted_at": "2026-11-05T00:00:00Z"}
    run(world)
    pages = by_name(notion, "assignments")
    assert n.get_select(pages["HW 4"], "Status") == "Submitted"
    assert n.get_select(pages["HW 3"], "Status") == "Done"


def test_changed_instructions_replace_toggle_but_keep_notes(world):
    notion, canvas, _ = world
    run(world)
    hw4 = by_name(notion, "assignments")["HW 4"]
    top = notion.children[hw4["id"]]
    notes_paragraph = top[-1]["id"]
    canvas.by_course[111][0]["description"] = "<p>New</p><ul><li>Step</li></ul>"
    run(world)
    top_after = notion.children[hw4["id"]]
    assert top_after[-1]["id"] == notes_paragraph
    content = notion.children[top_after[0]["id"]]
    assert [b["type"] for b in content] == ["paragraph", "paragraph", "bulleted_list_item"]


def test_removed_due_date_clears_due(world):
    notion, canvas, _ = world
    run(world)
    canvas.by_course[111][0]["due_at"] = None
    run(world)
    assert by_name(notion, "assignments")["HW 4"]["properties"]["Due"] == {"date": None}


def test_pinned_course_id_and_missing_course(world):
    notion, canvas, _ = world
    notion.add_page("courses", course_props("Discrete Math", "COMS W3203", cw_id="222"))
    notion.add_page("courses", course_props("Intro to Java", "COMS W1004"))
    report = run(world)
    assert any("COMS W1004" in w for w in report.warnings)
    assert report.errors == []


def test_one_failing_course_does_not_stop_others(world):
    notion, canvas, _ = world
    notion.add_page("courses", course_props("Discrete Math", "COMS W3203"))
    original = canvas.assignments

    def flaky(course_id):
        if course_id == 222:
            raise RuntimeError("boom")
        return original(course_id)

    canvas.assignments = flaky
    report = run(world)
    assert len(report.errors) == 1 and "boom" in report.errors[0]
    assert "HW 4" in by_name(notion, "assignments")


@pytest.mark.parametrize("name,due,types,omit,expected", [
    ("Exam 1 - Raw Score", None, ["none"], True, True),
    ("Exam 1 - Final Score", None, ["none"], False, True),
    ("Class Rank", None, ["none"], True, True),
    ("Late Days Remaining", None, ["none"], True, True),
    ("Attendance Grade", None, ["online_text_entry"], False, True),
    ("Code Review 1", None, ["online_text_entry"], False, True),
    ("Lab 1", None, ["none"], False, True),
    ("Theoretical Assignment 2", "2026-11-04T21:10:00Z", ["none"], False, False),
    ("zyBooks Exercise Set 3", "2026-10-29T03:59:00Z", ["not_graded"], False, False),
    ("Homework 1", "2026-09-24T03:59:00Z", ["external_tool"], False, False),
    ("Written Assignment 1", None, ["external_tool"], False, False),
])
def test_gradebook_filter(name, due, types, omit, expected):
    a = {"name": name, "due_at": due, "submission_types": types, "omit_from_final_grade": omit}
    assert is_gradebook_only(a) is expected


def test_gradebook_entries_are_skipped_in_sync(world):
    notion, canvas, _ = world
    canvas.by_course[111].append(assignment(50, "Exam 1 - Raw Score", None,
                                            submission_types=["none"], omit_from_final_grade=True))
    report = run(world)
    assert "Exam 1 - Raw Score" not in by_name(notion, "assignments")
    assert report.skipped == 1
