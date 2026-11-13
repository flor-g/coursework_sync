"""Website readers (on HTML shaped like the real course sites) and the website sync."""

from datetime import datetime
from zoneinfo import ZoneInfo

from scheduleaid import notion as n
from scheduleaid.sync_websites import WebsiteSync
from scheduleaid.websites import parse_time_range, reader_for, resolve_date
from tests.fakes import FakeNotion
from tests.test_sync import course_props, make_config

TZ = ZoneInfo("America/New_York")
# the readers in courses/sites/, found by URL
parse_cs3157 = reader_for("https://cs3157.github.io/www/2026-9/")
parse_discrete = reader_for("https://www.cs.columbia.edu/~gtchuang/discrete/index.html")
NOW = datetime(2026, 11, 2, 12, 0, tzinfo=TZ)
AP_URL = "https://cs3157.github.io/www/2026-9/"
DM_URL = "https://www.cs.columbia.edu/~gtchuang/discrete/index.html"

AP_HTML = """
<h2>Past Lectures</h2>
<table><tbody><tr><td>Lecture 1: T 9/8</td></tr></tbody></table>
<ul><li>Overview<ul><li>Slides: <a href="/www/2026-9/ap-overview.pdf">OVERVIEW</a></li></ul></li></ul>
<table><tbody><tr><td>Lecture 3: T 9/15</td></tr></tbody></table>
<ul><li>Compiling<ul>
  <li><a href="/www/2026-9/lecture-notes/02-compile.pdf">Lecture notes 02: Separate Compilation and Make</a></li>
</ul></li></ul>
<table><tbody><tr><td>Lecture 4: R 9/17</td></tr></tbody></table>
<ul><li>Makefile<ul>
  <li><a href="/www/2026-9/lecture-notes/02-compile.pdf">Lecture notes 02: Separate Compilation and Make</a></li>
</ul></li></ul>
<table><tbody><tr><td>Recitation 1: pre-recorded</td></tr></tbody></table>
<ul><li>CLI</li></ul>
<h2>Assignments</h2>
<ul>
<li>HW0 Part A due Tue 9/8</li>
<li>Lab3 due Sun 10/25</li>
<li>Lab4 due Sun 11/8</li>
<li>Exam 2 on Tuesday  Nov 17, 4:10-5:25pm</li>
<li><em>Lab5 tentatively due Sun 11/22</em></li>
<li>Exam 3 (final exam) on Tuesday Dec 22, 3:30-5:00pm (90 min)</li>
</ul>
"""

DM_HTML = """
<table>
<tr><th>Date</th><th>Topic</th><th>Readings</th><th>Homework</th><th>Recitation</th></tr>
<tr class="unit-row"><td colspan="5">Introduction</td></tr>
<tr>
<td>We 9/9</td>
<td>Introduction. Logical connectives, quantifiers.
<a href="lectures/Lecture1.pdf">lecture notes</a>, <a href="slides/slides-coms-3203-1.pdf">slides</a>.</td>
<td>Newstead 1.1</td>
<td><a href="homeworks/problem_set_0.pdf">HW 0</a> out<br> (due Tues 9/15) <br> <a href="homeworks/HW0-sol.pdf"> HW0 solutions</a></td>
<td><a href="recitations/rec_0_problems.pdf">recitation 0</a></td>
</tr>
<tr class="exam-row"><td>Mo 10/12</td><td colspan="4"><strong>MIDTERM 1</strong> (in class). Topics: logic.</td></tr>
<tr>
<td>We 10/28</td>
<td>Graphs and subgraphs.
<a href="lectures/Lecture14.pdf">lecture notes</a>, <a href="slides/slides-coms-3203-14.pdf">slides</a>.</td>
<td></td>
<td><a href="homeworks/problem_set_6.pdf">HW 6</a> out <br> (due <b>Fri</b> 11/6)</td>
<td></td>
</tr>
<tr class="holiday-row"><td>Mo 11/2</td><td>ELECTION DAY &mdash; no class</td><td></td><td></td><td></td></tr>
<tr class="exam-row"><td>We 11/11</td><td colspan="4"><strong>MIDTERM 2</strong> (in class)</td></tr>
</table>
"""


def test_helpers():
    assert resolve_date(12, 22, NOW).year == 2026
    assert resolve_date(1, 15, NOW).year == 2027
    s, e = parse_time_range("4:10-5:25pm")
    assert (s.hour, s.minute, e.hour, e.minute) == (16, 10, 17, 25)
    s, e = parse_time_range("11:40am – 12:55pm")
    assert (s.hour, e.hour) == (11, 12)
    s, e = parse_time_range("11:40-12:55pm")
    assert (s.hour, e.hour) == (11, 12)
    assert reader_for(AP_URL).__module__ == "scheduleaid_sites.cs3157"
    assert reader_for(DM_URL).__module__ == "scheduleaid_sites.discrete"
    assert reader_for("https://example.com") is None


def test_cs3157_reader():
    content = parse_cs3157(AP_HTML, AP_URL, NOW, TZ)
    items = {i.key: i for i in content.items}
    assert set(items) == {"hw0parta", "lab3", "lab4", "exam2", "lab5", "exam3"}
    assert items["lab4"].name == "Lab 4"
    assert items["lab4"].due == "2026-11-08T23:59:00-05:00"
    assert any("tentative" in note for note in items["lab5"].notes)
    assert any("[cs3157] [ANN]" in note for note in items["lab4"].notes)
    assert items["exam2"].kind == "exam"
    assert (items["exam2"].due, items["exam2"].end) == ("2026-11-17T16:10:00-05:00", "2026-11-17T17:25:00-05:00")
    assert items["exam3"].name == "Exam 3 (final exam)"
    assert items["exam3"].due == "2026-12-22T15:30:00-05:00"
    # notes PDF shared by two sessions appears once, dated by its first session; slides are ignored
    assert [(lec.key, lec.number, lec.date) for lec in content.lectures] == [("notes02", 2, "2026-09-15")]
    assert content.lectures[0].url == "https://cs3157.github.io/www/2026-9/lecture-notes/02-compile.pdf"


def test_discrete_reader():
    content = parse_discrete(DM_HTML, DM_URL, NOW, TZ)
    items = {i.key: i for i in content.items}
    assert set(items) == {"hw0", "midterm1", "hw6", "midterm2"}
    assert items["hw0"].due == "2026-09-15T23:59:00-04:00"
    assert items["hw0"].url.endswith("/discrete/homeworks/problem_set_0.pdf")
    assert items["hw0"].links == [("Solutions", "https://www.cs.columbia.edu/~gtchuang/discrete/homeworks/HW0-sol.pdf")]
    assert items["hw6"].due == "2026-11-06T23:59:00-05:00" and items["hw6"].links == []
    assert items["midterm2"].name == "Midterm 2" and items["midterm2"].due == "2026-11-11"
    lec = {lec.key: lec for lec in content.lectures}
    assert lec["lecture14"].name == "Lecture 14: Graphs and subgraphs"
    assert [label for label, _ in lec["lecture1"].links] == ["Lecture notes", "Slides"]


def make_world():
    notion = FakeNotion()
    ap = notion.add_page("courses", {**course_props("Advanced Programming", "COMS W3157"),
                                     "Website": n.url(AP_URL)})
    dm = notion.add_page("courses", {**course_props("Discrete Math", "COMS W3203"),
                                     "Website": n.url(DM_URL)})
    pages = {AP_URL: AP_HTML, DM_URL: DM_HTML}
    return notion, pages, ap, dm


def run_web(notion, pages, now=NOW):
    return WebsiteSync(make_config(), notion, now=now, fetch=lambda url: pages[url]).run()


def assignments(notion):
    return {n.get_text(p, "Source ID"): p for p in notion.in_ds("assignments")}


def test_website_sync_creates_and_is_idempotent():
    notion, pages, ap, dm = make_world()
    report = run_web(notion, pages)
    assert report.errors == [] and report.warnings == []
    pages_by_sid = assignments(notion)
    lab4 = pages_by_sid["web:COMSW3157:lab4"]
    assert n.get_select(lab4, "Source") == "Website"
    assert n.get_select(lab4, "Status") == "Not started"
    assert n.get_select(pages_by_sid["web:COMSW3157:lab3"], "Status") == "Done"   # already past
    assert n.get_select(pages_by_sid["web:COMSW3157:exam2"], "Type") == "Exam"
    assert n.get_select(pages_by_sid["web:COMSW3203:midterm2"], "Status") == "Not started"
    assert lab4["properties"]["Course"] == n.relation(ap["id"])

    # exams keep their end time (the calendar event spans it); all-day exams are bare dates
    assert pages_by_sid["web:COMSW3157:exam2"]["properties"]["Due"] == {"date": {
        "start": "2026-11-17T16:10:00-05:00", "end": "2026-11-17T17:25:00-05:00"}}
    assert n.get_date(pages_by_sid["web:COMSW3203:midterm2"], "Due") == "2026-11-11"

    lectures = {n.get_text(p, "Source ID"): p for p in notion.in_ds("lectures")}
    assert set(lectures) == {"web:COMSW3157:notes02", "web:COMSW3203:lecture1", "web:COMSW3203:lecture14"}
    lec = lectures["web:COMSW3203:lecture14"]
    assert n.get_select(lec, "Conversion status") == "Pending" and n.get_select(lec, "Format") == "PDF"
    toggles = [b for b in notion.children[lec["id"]] if b["type"] == "heading_2"]
    assert [b["heading_2"]["rich_text"][0]["text"]["content"] for b in toggles] == [
        "Notes", "Files", "Source", "My notes"]

    notion.writes.clear()
    again = run_web(notion, pages)
    assert notion.writes == []
    assert again.created == 0 and again.updated == 0


def test_website_due_change_and_solutions_posted():
    notion, pages, *_ = make_world()
    run_web(notion, pages)
    pages[AP_URL] = pages[AP_URL].replace("<em>Lab5 tentatively due Sun 11/22</em>", "Lab5 due Tue 11/24")
    pages[DM_URL] = pages[DM_URL].replace(
        "(due <b>Fri</b> 11/6)", '(due <b>Fri</b> 11/6) <a href="homeworks/HW6-sol.pdf">HW6 solutions</a>')
    report = run_web(notion, pages)

    lab5 = assignments(notion)["web:COMSW3157:lab5"]
    assert n.get_date(lab5, "Due") == "2026-11-24T23:59:00-05:00"
    change = [p for p in notion.in_ds("updates") if n.get_select(p, "Category") == "Deadline change"]
    assert [n.get_text(p, "Name") for p in change] == ["Lab 5: due date changed to Tue, Nov 24, 11:59 PM"]
    assert any(note.startswith("Deadline change: COMS W3157 Lab 5") for note in report.notes)

    hw6 = assignments(notion)["web:COMSW3203:hw6"]
    instructions = notion.children[notion.children[hw6["id"]][0]["id"]]
    assert any("Solutions" in b["paragraph"]["rich_text"][0]["text"]["content"] for b in instructions)


def test_user_status_is_never_overwritten():
    notion, pages, *_ = make_world()
    run_web(notion, pages)
    lab4 = assignments(notion)["web:COMSW3157:lab4"]
    notion.update_page(lab4["id"], {"Status": n.status("In progress")})
    run_web(notion, pages)
    assert n.get_select(assignments(notion)["web:COMSW3157:lab4"], "Status") == "In progress"


def test_exam_marked_done_after_it_happens():
    notion, pages, *_ = make_world()
    run_web(notion, pages)
    run_web(notion, pages, now=datetime(2026, 11, 18, 9, 0, tzinfo=TZ))
    assert n.get_select(assignments(notion)["web:COMSW3157:exam2"], "Status") == "Done"


def test_broken_or_unknown_site_is_reported_not_fatal():
    notion, pages, *_ = make_world()
    notion.add_page("courses", {**course_props("Other", "COMS W9999"), "Website": n.url("https://example.com")})
    pages[DM_URL] = "<html><body>Under construction</body></html>"

    def fetch(url):
        if url == AP_URL:
            raise RuntimeError("timeout")
        return pages[url]

    report = WebsiteSync(make_config(), notion, now=NOW, fetch=fetch).run()
    assert any("timeout" in e for e in report.errors)
    assert any("no reader" in w for w in report.warnings)
    assert any("found nothing" in w for w in report.warnings)


def test_a_date_only_source_keeps_a_known_time_on_that_day():
    from scheduleaid.sync_common import AssignmentData, BaseSync

    class Site(BaseSync):
        SOURCE = "Website"

    notion = FakeNotion()
    course = notion.add_page("courses", course_props("Discrete Math", "COMS W3203"))
    page = notion.add_page("assignments", {
        "Name": n.title("Final Exam"), "Course": n.relation(course["id"]), "Source": n.select("Website"),
        "Source ID": n.rich_text("web:x:final"), "Content hash": n.rich_text("h"),
        "Due": n.date("2026-12-21T15:30:00-05:00", "2026-12-21T18:30:00-05:00")})
    sync = Site(make_config(), notion, now=NOW)
    data = AssignmentData(sid="web:x:final", name="Final Exam", due="2026-12-21", source_url=None, blocks=[],
                          hash="h", type="Exam", initial_status="Not started", is_exam=True)
    sync.upsert_assignment(course, data)
    assert n.get_date(notion.pages[page["id"]], "Due") == "2026-12-21T15:30:00-05:00"
    assert not notion.in_ds("updates")                                    # no "deadline change" logged
    sync = Site(make_config(), notion, now=NOW)
    sync.upsert_assignment(course, AssignmentData(**{**data.__dict__, "due": "2026-12-22"}))
    assert n.get_date(notion.pages[page["id"]], "Due") == "2026-12-22"     # a new day is a real change
