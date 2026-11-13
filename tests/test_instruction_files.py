"""Instruction files on assignment pages: CourseWorks links, website handouts, emails."""

from datetime import datetime
from zoneinfo import ZoneInfo

from scheduleaid import notion as n
from scheduleaid.course_pages import CoursePageSync
from scheduleaid.email_analysis import RuleAnalyzer
from scheduleaid.sync_courseworks import CourseWorksSync, linked_files
from scheduleaid.sync_gmail import GmailSync
from scheduleaid.sync_websites import WebsiteSync
from tests.fakes import FakeCanvas, FakeNotion
from tests.test_gmail import email_page
from tests.test_sync import CANVAS_COURSES, assignment, course_props, make_config
from tests.test_websites import DM_HTML, DM_URL

TZ = ZoneInfo("America/New_York")
NOW = datetime(2026, 11, 2, 12, 0, tzinfo=TZ)
BASE = "https://canvas.example.edu"

DESCRIPTION = (
    '<p>Read the spec.</p>'
    f'<p><a href="{BASE}/courses/111/files/501?verifier=abc&amp;wrap=1">pa5.pdf</a> and '
    f'<a href="/courses/111/files/502?wrap=1">starter.zip</a> and '
    f'<a href="{BASE}/courses/111/files/501?verifier=abc">pa5.pdf again</a> and '
    f'<a href="{BASE}/courses/111/files/503">locked.pdf</a> and '
    f'<a href="{BASE}/courses/111/files/504">Main.java</a> and '
    f'<a href="{BASE}/courses/111/files/505">tests.zip</a></p>')


def toggle_children(notion, page, name):
    toggle = next(b for b in notion.children[page["id"]]
                  if b["type"] == "heading_2" and b["heading_2"]["rich_text"][0]["text"]["content"] == name)
    return notion.children[toggle["id"]]


def texts(blocks):
    return [b["type"] + ":" + "".join(i["text"]["content"] for i in
                                      (b[b["type"]].get("rich_text") or b[b["type"]].get("caption") or []))
            for b in blocks]


def test_linked_files():
    assert [(c, f, t, v) for c, f, t, v, _ in linked_files(DESCRIPTION)][:3] == [
        ("111", "501", "pa5.pdf", "abc"), ("111", "502", "starter.zip", None), ("111", "503", "locked.pdf", None)]


def courseworks_world():
    notion = FakeNotion()
    course = notion.add_page("courses", course_props("Data Structures", "COMS W3134"))
    canvas = FakeCanvas(CANVAS_COURSES, {111: [assignment(5, "Programming Assignment 5", "2026-11-08T04:59:59Z",
                                                          description=DESCRIPTION)]})
    canvas.files_by_id = {
        "501": ({"url": f"{BASE}/files/501/download?verifier=abc", "display_name": "pa5.pdf",
                 "content-type": "application/pdf", "size": 2000}, b"%PDF-1.4 spec"),
        "502": ({"url": f"{BASE}/files/502/download", "display_name": "starter.zip",
                 "content-type": "application/zip", "size": 9 * 1024 * 1024}, b"too big"),
        "504": ({"url": f"{BASE}/files/504/download", "display_name": "Main.java",
                 "content-type": "text/x-java", "size": 40}, b"class Main {\n  // TODO\n}\n"),
        "505": ({"url": f"{BASE}/files/505/download", "display_name": "tests.zip",
                 "content-type": "application/x-zip-compressed", "size": 10}, b"PK\x03\x04zip"),
    }
    return notion, canvas, course


def test_courseworks_files_are_uploaded_or_linked():
    notion, canvas, _ = courseworks_world()
    report = CourseWorksSync(make_config(), canvas, notion, now=NOW).run()
    assert report.errors == []
    page = notion.in_ds("assignments")[0]
    blocks = toggle_children(notion, page, "Instructions")
    assert texts(blocks) == [
        "paragraph:Synced from CourseWorks",
        "paragraph:Read the spec.",
        "paragraph:pa5.pdf and starter.zip and pa5.pdf again and locked.pdf and Main.java and tests.zip",
        "heading_3:Files",
        "pdf:pa5.pdf",                        # uploaded
        "paragraph:starter.zip: open",        # over 5 MB → link
        "paragraph:locked.pdf: open",         # 403 → link
        "paragraph:Main.java",                # source file → code block
        "code:class Main {\n  // TODO\n}\n",
        "file:",                              # zip uploaded as application/zip
    ]
    assert blocks[8]["code"]["language"] == "java"
    assert blocks[9]["file"]["name"] == "tests.zip"
    assert ("upload", "pa5.pdf", 13) in notion.writes
    assert blocks[5]["paragraph"]["rich_text"][1]["text"]["link"]["url"] == f"{BASE}/courses/111/files/502?wrap=1"


def test_website_handout_is_uploaded():
    notion = FakeNotion()
    notion.add_page("courses", {**course_props("Discrete Math", "COMS W3203"), "Website": n.url(DM_URL)})
    pdf = b"%PDF-1.5 problem set"
    pages = {DM_URL: DM_HTML}

    def fetch(url):
        return pages[url] if url in pages else pdf

    WebsiteSync(make_config(), notion, now=NOW, fetch=fetch).run()
    hw6 = next(p for p in notion.in_ds("assignments") if n.get_text(p, "Name") == "HW 6")
    blocks = toggle_children(notion, hw6, "Instructions")
    assert "heading_3:Files" in texts(blocks) and "pdf:problem_set_6.pdf" in texts(blocks)


def test_refreshing_instructions_keeps_email_sections():
    notion, canvas, course = courseworks_world()
    CourseWorksSync(make_config(), canvas, notion, now=NOW).run()
    page = notion.in_ds("assignments")[0]
    toggle = next(b for b in notion.children[page["id"]] if b["type"] == "heading_2")
    notion._store_blocks(toggle["id"], [
        {"object": "block", "type": "heading_3", "heading_3": {"rich_text": [
            {"type": "text", "text": {"content": "From email: PA5 clarification"}}]}},
        {"object": "block", "type": "paragraph", "paragraph": {"rich_text": [
            {"type": "text", "text": {"content": "Use Java 21."}}]}}])
    canvas.by_course[111][0]["description"] = "<p>Updated spec.</p>"
    CourseWorksSync(make_config(), canvas, notion, now=NOW).run()
    assert texts(notion.children[toggle["id"]]) == [
        "paragraph:Synced from CourseWorks", "paragraph:Updated spec.",
        "heading_3:From email: PA5 clarification", "paragraph:Use Java 21."]


def test_assignment_email_is_copied_into_the_assignment():
    notion = FakeNotion()
    ap = notion.add_page("courses", course_props("Advanced Programming", "COMS W3157"))
    lab4 = notion.add_page("assignments", {"Name": n.title("Lab 4"), "Course": n.relation(ap["id"]),
                                           "Due": n.date("2026-11-08T23:59:00-05:00"),
                                           "Source": n.select("Website"), "Status": n.status("Not started")})
    notion._store_blocks(lab4["id"], [{"object": "block", "type": "heading_2", "heading_2": {
        "rich_text": [{"type": "text", "text": {"content": "Instructions"}}], "is_toggleable": True}}])
    instructions = notion.children[lab4["id"]][0]["id"]
    notion._store_blocks(instructions, [{"object": "block", "type": "paragraph", "paragraph": {
        "rich_text": [{"type": "text", "text": {"content": "Synced from Website"}}]}}])
    page = email_page(notion, ap, "m1", "[cs3157] [ANN] [LAB4] Lab 4 released",
                      ["Lab 4 is out.", "Part 1: read records.\nPart 2: print them."], files=["lab4.pdf"])
    att_toggle = next(b for b in notion.children[page["id"]]
                      if b["type"] == "heading_2" and b["heading_2"]["rich_text"][0]["text"]["content"] == "Attachments")
    notion.children[att_toggle["id"]][0]["pdf"]["file"] = {"url": "https://files.notion/lab4.pdf?sig=1"}

    sync = GmailSync(make_config(), notion, RuleAnalyzer(), now=NOW, fetch=lambda url: b"%PDF lab4")
    sync.run()
    blocks = texts(notion.children[instructions])
    assert blocks[0] == "paragraph:Synced from Website"
    assert blocks[1] == "heading_3:From email: [cs3157] [ANN] [LAB4] Lab 4 released"
    assert blocks[3:5] == ["paragraph:Lab 4 is out.", "paragraph:Part 1: read records.\nPart 2: print them."]
    assert blocks[5] == "pdf:lab4.pdf"
    assert any(n_ == "lab4.pdf" for kind, n_, *_ in notion.writes if kind == "upload")

    # copied once, even if the email page is summarized again
    notion.pages[page["id"]]["properties"]["Content hash"] = n.rich_text("")
    sync = GmailSync(make_config(), notion, RuleAnalyzer(), now=NOW, fetch=lambda url: b"%PDF lab4")
    sync.run()
    assert texts(notion.children[instructions]).count("heading_3:From email: [cs3157] [ANN] [LAB4] Lab 4 released") == 1


def test_due_in_14_days_flag():
    notion = FakeNotion()
    def add(name, due, flag=False):
        return notion.add_page("assignments", {"Name": n.title(name), "Due": n.date(due),
                                               "Due in 14 days": n.checkbox(flag)})
    soon = add("Lab 4", "2026-11-08T23:59:00-05:00")
    edge = add("Exam", "2026-11-16")
    later = add("Lab 5", "2026-11-22T23:59:00-05:00", flag=True)
    past = add("Old", "2026-10-28T23:59:00-04:00", flag=True)
    report = CoursePageSync(make_config(), notion, now=NOW).run([])
    flags = {k: notion.pages[p["id"]]["properties"]["Due in 14 days"]["checkbox"]
             for k, p in dict(soon=soon, edge=edge, later=later, past=past).items()}
    assert flags == {"soon": True, "edge": True, "later": False, "past": False}
    assert "updated 4" in report.notes[0]


def test_gradescope_assignments_link_to_the_course_gradescope_tab():
    notion = FakeNotion()
    course = notion.add_page("courses", course_props("Data Structures", "COMS W3134"))
    canvas = FakeCanvas(CANVAS_COURSES, {111: [
        assignment(7, "Theoretical Assignment 2", "2026-11-04T21:10:00Z", submission_types=["none"],
                   description="<p>Answer the questions directly on GradeScope.</p>"),
        assignment(8, "Homework 1", "2026-11-05T04:59:00Z", submission_types=["external_tool"],
                   description="<p>Written part.</p>",
                   external_tool_tag_attributes={"url": "https://www.gradescope.com/auth/lti/callback"}),
        assignment(9, "Programming Assignment 6", "2026-11-19T04:55:00Z", description=""),
    ]})
    canvas.tabs_by_course = {111: [
        {"label": "Ed Discussion", "html_url": "/courses/111/external_tools/37606?display=borderless"},
        {"label": "Gradescope", "html_url": "/courses/111/external_tools/29680?display=borderless"}]}
    CourseWorksSync(make_config(), canvas, notion, now=NOW).run()

    props = notion.pages[course["id"]]["properties"]
    assert n.get_url(notion.pages[course["id"]], "Gradescope URL") == f"{BASE}/courses/111/external_tools/29680"
    assert props["Ed URL"] == n.url(f"{BASE}/courses/111/external_tools/37606")
    pages = {n.get_text(p, "Name"): p for p in notion.in_ds("assignments")}
    for name in ("Theoretical Assignment 2", "Homework 1"):
        first = toggle_children(notion, pages[name], "Instructions")[1]["paragraph"]["rich_text"]
        assert first[0]["text"]["content"] == "Questions and submission are on Gradescope: "
        assert first[1]["text"]["link"]["url"] == f"{BASE}/courses/111/external_tools/29680"
    assert texts(toggle_children(notion, pages["Programming Assignment 6"], "Instructions"))[1] == \
        "paragraph:No instructions on CourseWorks yet."
