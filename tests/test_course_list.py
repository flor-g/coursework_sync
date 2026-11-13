"""courses/courses.toml and keeping Notion's Courses database in step with it."""

import pytest

from scheduleaid import course_list as cl
from scheduleaid import notion as n
from scheduleaid.email_analysis import Email, KnownAssignment, RuleAnalyzer
from scheduleaid.websites import reader_for
from tests.fakes import FakeNotion
from tests.test_gmail import NOW, TZ
from tests.test_sync import course_props, make_config

LIST = cl.parse("""
[school]
term = "Spring 2027"
canvas_url = "https://canvas.example.edu"
staff_subjects = ['^\\[CS101\\]']

[[course]]
name = "Data Structures"
code = "COMS W3134"
instructor = "Dana Rivera"

[[course]]
name = "Operating Systems"
code = "COMS W4118"
instructor = "Jason Nieh"
website = "https://cs4118.example.edu/"
""")


# --- the file -------------------------------------------------------------------
def test_the_example_course_list_loads_and_every_website_has_a_reader():
    courses = cl.load()
    assert courses.school.canvas_url.startswith("https://") and courses.school.timezone
    assert [c.code for c in courses.courses] == ["CS 3134", "CS 3203"]
    assert [e.name for e in courses.courses[0].exams] == ["Exam 1", "Final Exam"]
    for course in courses.courses:
        if course.website:
            assert reader_for(course.website) is not None, course.website


def test_default_gmail_query_covers_the_usual_spellings():
    assert cl.Course("DS", "COMS W3134").default_gmail_query == \
        '("COMS W3134" OR "COMS 3134" OR "W3134" OR "COMSW3134")'
    assert cl.Course("Calc", "MATH 1101").default_gmail_query == '("MATH 1101" OR "MATH1101")'
    assert cl.Course("CF", "PHIL GR6552").default_gmail_query == \
        '("PHIL GR6552" OR "PHIL 6552" OR "GR6552" OR "PHILGR6552")'
    assert cl.Course("DS", "COMSW3134").default_gmail_query == '"COMSW3134"'


@pytest.mark.parametrize("text,message", [
    ('[[course]]\nname = "X"', "`name` and `code` are required"),
    ('[[course]]\nname = "X"\ncode = "A 1"\nteacher = "Y"', "unknown field(s) teacher"),
    ('[[course]]\nname = "X"\ncode = "COMS W1"\n[[course]]\nname = "Y"\ncode = "comsw1"', "listed twice"),
    ('[school]\nstaff_subjects = ["[unclosed"]', "not a valid pattern"),
    ('[school]\ncolour = "red"', "unknown setting(s) colour"),
    ("[[course]\n", "not valid TOML"),
])
def test_mistakes_in_the_file_are_explained(text, message):
    with pytest.raises(cl.CourseListError, match=message.replace("(", r"\(").replace(")", r"\)")):
        cl.parse(text)


def test_staff_subjects_come_from_the_school_settings():
    rules = RuleAnalyzer(LIST.school.staff_subjects)
    known = [KnownAssignment("HW 1", None)]
    staff = rules.analyze("C", known, Email("m", "t", "[CS101] HW 1 released", "P", "p@x", NOW), TZ)
    other = rules.analyze("C", known, Email("m", "t", "[cs3157] [ANN] Lab 1", "P", "p@x", NOW), TZ)
    assert staff.from_staff and not other.from_staff


# --- Notion -------------------------------------------------------------------------
def world(templates=()):
    notion = FakeNotion()
    notion.course_templates = list(templates)
    ds = notion.add_page("courses", {**course_props("Data Structures", "COMS W3134", "300646"),
                                     "Gmail query": n.rich_text("subject:ds"), "Ed URL": n.url("https://ed/1")})
    old = notion.add_page("courses", course_props("Discrete Math", "COMS W3203"))
    return notion, ds, old


def sync(notion, course_list=LIST):
    sync = cl.CourseListSync(make_config(), notion, course_list)
    pages = sync.run(notion.query_all("courses"))
    return sync.report, pages


def test_new_course_is_made_from_the_course_template():
    notion, _, _ = world(templates=[{"id": "tpl-1", "name": "Course page", "is_default": False}])
    report, pages = sync(notion)
    assert ("create", "courses", "template", "tpl-1") in notion.writes
    os_page = next(p for p in notion.in_ds("courses") if n.get_text(p, "Code") == "COMS W4118")
    assert n.get_text(os_page, "Name") == "Operating Systems"
    assert n.get_select(os_page, "Status") == "Active" and n.get_select(os_page, "Term") == "Spring 2027"
    assert n.get_url(os_page, "Website") == "https://cs4118.example.edu/"
    assert n.get_text(os_page, "Gmail query") == '("COMS W4118" OR "COMS 4118" OR "W4118" OR "COMSW4118")'
    assert os_page["id"] in {p["id"] for p in pages}       # the rest of the sync sees it
    assert report.created == 1 and not report.warnings


def test_without_a_template_the_page_gets_a_basic_layout_and_a_warning():
    notion, _, _ = world()
    report, _ = sync(notion)
    os_page = next(p for p in notion.in_ds("courses") if n.get_text(p, "Code") == "COMS W4118")
    kinds = [b["type"] for b in notion.children[os_page["id"]]]
    assert "callout" in kinds and "table" in kinds          # the Important box and At a glance
    callout = next(b for b in notion.children[os_page["id"]] if b["type"] == "callout")
    assert n._plain(callout["callout"]["rich_text"]) == "Important"
    assert "no template" in report.warnings[0]


def test_listed_course_is_updated_but_fields_left_out_are_kept():
    notion, ds, _ = world()
    sync(notion)
    page = notion.pages[ds["id"]]
    assert n.get_text(page, "Instructor") == "Dana Rivera" and n.get_select(page, "Term") == "Spring 2027"
    assert n.get_text(page, "CourseWorks ID") == "300646"     # not in the file: kept
    assert n.get_text(page, "Gmail query") == "subject:ds"    # an existing query is never replaced by the default
    assert n.get_url(page, "Ed URL") == "https://ed/1"


def test_course_left_out_of_the_file_is_archived_not_deleted():
    notion, _, old = world()
    report, _ = sync(notion)
    assert n.get_select(notion.pages[old["id"]], "Status") == "Archived"
    assert any("Archived COMS W3203" in m for m in report.notes)
    assert old["id"] in notion.pages


def test_second_run_changes_nothing():
    notion, _, _ = world()
    sync(notion)
    before = len(notion.writes)
    report, _ = sync(notion)
    assert len(notion.writes) == before and report.created == 0 and not report.notes


def test_canvas_id_also_sets_the_courseworks_link():
    notion, ds, _ = world()
    listed = cl.parse('[[course]]\nname = "Data Structures"\ncode = "COMS W3134"\ncanvas_id = "300646"')
    sync(notion, listed)
    assert n.get_url(notion.pages[ds["id"]], "CourseWorks URL") == "https://canvas.example.edu/courses/300646"


def test_an_empty_list_leaves_notion_alone():
    notion, _, old = world()
    report, _ = sync(notion, cl.CourseList(cl.School()))
    assert n.get_select(notion.pages[old["id"]], "Status") == "Active" and notion.writes == []


# --- exams --------------------------------------------------------------------------
EXAMS = cl.parse("""
[school]
term = "Fall 2026"

[[course]]
name = "Intro to Java"
code = "COMS W1004"

[[course.exams]]
name = "Midterm 1"
date = "2026-10-14"
note = "In class."

[[course.exams]]
name = "Final Exam"
date = "2026-12-23 12:00"
end = "15:00"
source = "https://cw/syllabus"

[[course.exams]]
name = "Midterm 2"
date = "2026-11-18"
""")


def exam_world():
    notion = FakeNotion()
    java = notion.add_page("courses", course_props("Intro to Java", "COMS W1004"))
    return notion, java


def run_exams(notion):
    sync = cl.CourseListSync(make_config(), notion, EXAMS, now=NOW)
    sync.run(notion.query_all("courses"))
    return sync.report


def test_exams_become_exam_pages_with_times_and_status():
    notion, java = exam_world()
    report = run_exams(notion)
    exams = {n.get_text(p, "Name"): p for p in notion.in_ds("assignments")}
    assert set(exams) == {"Midterm 1", "Final Exam", "Midterm 2"} and report.created == 3
    final = exams["Final Exam"]
    assert n.get_date(final, "Due") == "2026-12-23T12:00:00-05:00"
    assert n.get_date_end(final, "Due") == "2026-12-23T15:00:00-05:00"
    assert n.get_select(final, "Type") == "Exam" and n.get_select(final, "Status") == "Not started"
    assert n.get_url(final, "Source URL") == "https://cw/syllabus"
    assert n.get_select(exams["Midterm 1"], "Status") == "Done"                 # already happened
    assert exams["Midterm 1"]["properties"]["Course"]["relation"] == [{"id": java["id"]}]
    before = len(notion.writes)
    assert run_exams(notion).created == 0 and len(notion.writes) == before     # nothing changes twice


def test_exam_already_listed_by_another_source_is_left_alone():
    notion, java = exam_world()
    notion.add_page("assignments", {"Name": n.title("Midterm 2"), "Course": n.relation(java["id"]),
                                    "Source": n.select("Website"), "Type": n.select("Exam")})
    run_exams(notion)
    names = [n.get_text(p, "Name") for p in notion.in_ds("assignments")]
    assert names.count("Midterm 2") == 1


@pytest.mark.parametrize("exam,message", [
    ('name = "X"', "needs a `name` and a `date`"),
    ('name = "X"\ndate = "Dec 23"', "date should look like"),
    ('name = "X"\ndate = "2026-12-23"\nend = "15:00"', "needs a start time"),
    ('name = "X"\ndate = "2026-12-23"\nroom = "301"', "unknown field(s) room"),
])
def test_exam_mistakes_are_explained(exam, message):
    with pytest.raises(cl.CourseListError, match=message.replace("(", r"\(").replace(")", r"\)")):
        cl.parse(f'[[course]]\nname = "J"\ncode = "COMS W1004"\n[[course.exams]]\n{exam}')


def test_an_entry_adds_the_time_to_a_website_exam_that_has_only_a_date():
    notion, java = exam_world()
    site = notion.add_page("assignments", {"Name": n.title("Final Exam"), "Course": n.relation(java["id"]),
                                           "Source": n.select("Website"), "Type": n.select("Exam"),
                                           "Due": n.date("2026-12-23")})
    report = run_exams(notion)
    page = notion.pages[site["id"]]
    assert n.get_date(page, "Due") == "2026-12-23T12:00:00-05:00"
    assert n.get_date_end(page, "Due") == "2026-12-23T15:00:00-05:00"
    assert any("added the time" in m for m in report.notes)
    assert [n.get_text(p, "Name") for p in notion.in_ds("assignments")].count("Final Exam") == 1
