from datetime import datetime
from zoneinfo import ZoneInfo

from scheduleaid import notion as n
from scheduleaid.blocks import heading, paragraph, text_item
from scheduleaid.course_pages import CoursePageSync
from tests.fakes import FakeNotion
from tests.test_sync import course_props, make_config

TZ = ZoneInfo("America/New_York")
NOW = datetime(2026, 11, 2, 12, 0, tzinfo=TZ)


def course_page(notion):
    course = notion.add_page("courses", course_props("Advanced Programming", "COMS W3157"))
    callout = {"object": "block", "type": "callout",
               "callout": {"rich_text": [text_item("Important", bold=True, color="red")]}}
    notion._store_blocks(course["id"], [paragraph(text_item("COMS W3157 · Fall 2026")), callout,
                                        heading(2, "At a glance")])
    box = notion.children[course["id"]][1]["id"]
    notion._store_blocks(box, [paragraph(text_item("Nothing urgent right now."))])
    return course, box


def update(notion, course, name, date, category, important=True, source="Gmail"):
    return notion.add_page("updates", {"Name": n.title(name), "Course": n.relation(course["id"]),
                                       "Date": n.date(date), "Category": n.select(category),
                                       "Important": n.checkbox(important), "Source": n.select(source)})


def texts(notion, box):
    return ["".join(i["text"]["content"] for i in b[b["type"]]["rich_text"]) for b in notion.children[box]]


def run(notion):
    return CoursePageSync(make_config(), notion, now=NOW).run()


def test_important_box_lists_exams_and_recent_important_updates():
    notion = FakeNotion()
    course, box = course_page(notion)
    update(notion, course, "Lab 4 extended to Tue Nov 10", "2026-11-01T15:00:00-05:00", "Deadline change")
    update(notion, course, "Office hours on Zoom", "2026-10-31T09:00:00-04:00", "Logistics", source="Ed")
    update(notion, course, "Routine note", "2026-11-01T10:00:00-05:00", "Announcement", important=False)
    update(notion, course, "Old news", "2026-10-01T10:00:00-04:00", "Exam")
    notion.add_page("assignments", {"Name": n.title("Exam 2"), "Course": n.relation(course["id"]),
                                    "Type": n.select("Exam"),
                                    "Due": n.date("2026-11-10T16:10:00-05:00", "2026-11-10T17:25:00-05:00")})
    notion.add_page("assignments", {"Name": n.title("Exam 3"), "Course": n.relation(course["id"]),
                                    "Type": n.select("Exam"), "Due": n.date("2026-12-22T15:30:00-05:00")})
    run(notion)
    assert texts(notion, box) == [
        "Exam 2: Tue, Nov 10, 4:10 PM  ·  upcoming exam",
        "Lab 4 extended to Tue Nov 10  ·  Nov 1  ·  Gmail",
        "Office hours on Zoom  ·  Oct 31  ·  Ed",
    ]
    rows = notion.children[box]
    assert rows[0]["bulleted_list_item"]["rich_text"][0]["annotations"] == {"color": "red"}
    assert rows[1]["bulleted_list_item"]["rich_text"][0]["annotations"] == {"color": "red"}
    assert "annotations" not in rows[2]["bulleted_list_item"]["rich_text"][0]

    notion.writes.clear()
    run(notion)
    assert notion.writes == []          # unchanged box is not rewritten


def test_empty_box_and_missing_box():
    notion = FakeNotion()
    course, box = course_page(notion)
    notion.writes.clear()
    run(notion)
    assert texts(notion, box) == ["Nothing urgent right now."] and notion.writes == []
    other = notion.add_page("courses", course_props("Data Structures", "COMS W3134"))
    assert any("no Important box" in w for w in run(notion).warnings)
    assert other


def test_template_placeholders_and_empty_cells_are_filled_but_your_edits_kept():
    from scheduleaid.blocks import block, heading, paragraph, text_item

    notion = FakeNotion()
    course = notion.add_page("courses", {
        **course_props("Counterfactuals", "PHIL GR6552"), "Term": n.select("Fall 2026"),
        "Instructor": n.rich_text("Sam Fischer"), "Office hours": n.rich_text("Tue 2-3pm"),
        "CourseWorks URL": n.url("https://canvas.example.edu/courses/300111"),
        "Gradescope URL": n.url("https://gs/1")})

    def row(label, value=""):
        return {"object": "block", "type": "table_row",
                "table_row": {"cells": [[text_item(label)], [text_item(value)] if value else []]}}

    notion._store_blocks(course["id"], [
        paragraph(text_item("(course code · term)\n(instructor · meeting times)", color="gray")),
        block("callout", [text_item("Important")]), heading(2, "At a glance"),
        {"object": "block", "type": "table", "table": {"table_width": 2}}])
    table_id = notion.children[course["id"]][-1]["id"]
    notion._store_blocks(table_id, [row("Course code"), row("Instructor"), row("Lectures"),
                                    row("Office hours", "By appointment"), row("Links")])

    CoursePageSync(make_config(), notion, now=NOW).run()
    header = n._plain(notion.children[course["id"]][0]["paragraph"]["rich_text"])
    assert header == "PHIL GR6552 · Fall 2026\nSam Fischer"
    cells = {n._plain(r["table_row"]["cells"][0]): r["table_row"]["cells"][1] for r in notion.children[table_id]}
    assert n._plain(cells["Course code"]) == "PHIL GR6552" and n._plain(cells["Instructor"]) == "Sam Fischer"
    assert n._plain(cells["Office hours"]) == "By appointment"          # typed in: kept
    assert cells["Lectures"] == []                                       # nothing known: left empty
    assert n._plain(cells["Links"]) == "CourseWorks · Gradescope"
    assert cells["Links"][0]["text"]["link"] == {"url": "https://canvas.example.edu/courses/300111"}

    before = len(notion.writes)
    CoursePageSync(make_config(), notion, now=NOW).run()
    assert not [w for w in notion.writes[before:] if w[0] == "update_block"]   # nothing left to fill


def test_exams_row_lists_every_exam_and_placeholders_and_old_links_are_refreshed():
    from scheduleaid.blocks import text_item

    notion = FakeNotion()
    course = notion.add_page("courses", {
        **course_props("Data Structures", "COMS W3134"),
        "CourseWorks URL": n.url("https://cw/300646"), "Ed URL": n.url("https://edstem.org/us/courses/90588/discussion")})
    for name, due, end in (("Exam 2", "2026-11-17T16:10:00-05:00", "2026-11-17T17:25:00-05:00"),
                           ("Exam 1", "2026-10-05", None)):
        notion.add_page("assignments", {"Name": n.title(name), "Course": n.relation(course["id"]),
                                        "Type": n.select("Exam"), "Due": n.date(due, end)})

    def row(label, *value):
        return {"object": "block", "type": "table_row", "table_row": {"cells": [[text_item(label)], list(value)]}}

    notion._store_blocks(course["id"], [{"object": "block", "type": "table", "table": {"table_width": 2}}])
    table = notion.children[course["id"]][0]["id"]
    notion._store_blocks(table, [
        row("Exams", text_item("Three exams; add dates", color="gray")),
        row("Office hours", text_item("Add times", color="gray")),
        row("Grading", text_item("40% exams")),
        row("Links", text_item("CourseWorks", link="https://cw/300646"), text_item(" · "),
            text_item("Ed", link="https://cw/300646/external_tools/37606"))])
    CoursePageSync(make_config(), notion, now=NOW).run()
    cells = {n._plain(r["table_row"]["cells"][0]): r["table_row"]["cells"][1] for r in notion.children[table]}
    assert n._plain(cells["Exams"]) == "Exam 1 Mon, Oct 5 · Exam 2 Tue, Nov 17, 4:10–5:25 PM"
    assert cells["Exams"][0]["text"]["link"]["url"].startswith("https://notion.so/")
    assert n._plain(cells["Grading"]) == "40% exams"                                  # yours: kept
    assert cells["Links"][-1]["text"]["link"] == {"url": "https://edstem.org/us/courses/90588/discussion"}
    assert n._plain(cells["Office hours"]) == "Add times"                              # nothing to fill in

    before = len(notion.writes)
    CoursePageSync(make_config(), notion, now=NOW).run()
    assert not [w for w in notion.writes[before:] if w[0] == "update_block"]


def test_links_you_added_are_left_alone():
    from scheduleaid.blocks import text_item

    notion = FakeNotion()
    course = notion.add_page("courses", {**course_props("AP", "COMS W3157"), "CourseWorks URL": n.url("https://cw/1")})
    notion._store_blocks(course["id"], [{"object": "block", "type": "table", "table": {"table_width": 2}}])
    table = notion.children[course["id"]][0]["id"]
    notion._store_blocks(table, [{"object": "block", "type": "table_row", "table_row": {"cells": [
        [text_item("Links")], [text_item("Lab workflow", link="https://lab")]]}}])
    CoursePageSync(make_config(), notion, now=NOW).run()
    assert n._plain(notion.children[table][0]["table_row"]["cells"][1]) == "Lab workflow"
