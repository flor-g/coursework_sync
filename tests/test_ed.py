"""Ed Discussion: the post-format converter and clarifications on assignment pages."""

from datetime import datetime
from zoneinfo import ZoneInfo

from scheduleaid import notion as n
from scheduleaid.blocks import heading, paragraph
from scheduleaid.ed import document_blocks
from scheduleaid.sync_ed import TOGGLE, EdSync
from tests.fakes import FakeNotion
from tests.test_sync import course_props, make_config

TZ = ZoneInfo("America/New_York")
NOW = datetime(2026, 11, 3, 12, 0, tzinfo=TZ)

STAFF = {"id": 1, "name": "Dana Rivera", "course_role": "admin"}
TA = {"id": 2, "name": "Ada TA", "course_role": "staff"}
STUDENT = {"id": 3, "name": "Sam Student", "course_role": "student"}
USERS = {u["id"]: u for u in (STAFF, TA, STUDENT)}


def doc(*paragraphs):
    return "<document version=\"2.0\">" + "".join(f"<paragraph>{p}</paragraph>" for p in paragraphs) + "</document>"


def thread(tid, title, user, *, category="Homework", sub="", reply_count=0, kind="question",
           updated="2026-10-30T15:00:00Z", answers=(), comments=(), content=None, **extra):
    t = {"id": tid, "number": tid, "type": kind, "title": title, "category": category, "subcategory": sub,
         "user_id": user["id"], "reply_count": reply_count, "created_at": "2026-10-29T15:00:00Z",
         "updated_at": updated, "content": content or doc(f"Body of {title}"),
         "answers": list(answers), "comments": list(comments)}
    t.update(extra)
    return t


def reply(rid, user, text, kind="comment", endorsed=False, nested=()):
    return {"id": rid, "type": kind, "user_id": user["id"], "content": doc(text), "is_endorsed": endorsed,
            "created_at": "2026-10-30T12:00:00Z", "comments": list(nested)}


class FakeEd:
    def __init__(self, threads, courses=()):
        self.list = threads
        self.ed_courses = list(courses)
        self.opened = []

    def courses(self):
        return self.ed_courses

    def threads(self, course_id, since):
        return [{k: v for k, v in t.items() if k not in ("answers", "comments")} for t in self.list], USERS

    def thread(self, thread_id):
        self.opened.append(thread_id)
        return next(t for t in self.list if t["id"] == thread_id), USERS

    def thread_url(self, course_id, thread_id):
        return f"https://edstem.org/us/courses/{course_id}/discussion/{thread_id}"

    def course_url(self, course_id):
        return f"https://edstem.org/us/courses/{course_id}/discussion"


def world(threads, ed_id="900", courses=()):
    notion = FakeNotion()
    course = notion.add_page("courses", {**course_props("Data Structures", "COMS W3134"),
                                         "Ed ID": n.rich_text(ed_id)})
    pages = {}
    for name in ("Homework 3", "Homework 4", "Midterm Exam 1"):
        page = notion.add_page("assignments", {"Name": n.title(name), "Course": n.relation(course["id"])})
        notion._store_blocks(page["id"], [heading(2, "Instructions", True), heading(2, "Source"), paragraph(),
                                          heading(2, "My notes"), paragraph()])
        pages[name] = page
    return notion, course, pages, FakeEd(threads, courses)


def run(notion, ed):
    return EdSync(make_config(), ed, notion, now=NOW).run()


def section(notion, page):
    blocks = notion.children[page["id"]]
    toggle = next((b for b in blocks if b["type"] == "heading_2" and n._plain(b["heading_2"]["rich_text"]) == TOGGLE), None)
    return blocks, (notion.children[toggle["id"]] if toggle else None)


def texts(blocks):
    return [n._plain(b[b["type"]].get("rich_text", [])) for b in blocks]


# --- Ed's post format ---------------------------------------------------------------
def test_document_blocks_cover_ed_formatting():
    blocks = document_blocks(
        '<document><paragraph>Use <bold>O(n)</bold> and <code>int[]</code>, see <link href="https://x.y">this</link>'
        '<math>n^2</math></paragraph><heading level="2">Part B</heading>'
        '<list style="number"><list-item><paragraph>first</paragraph></list-item></list>'
        '<snippet language="java"><snippet-file id="code">int x = 1;</snippet-file></snippet>'
        '<figure><image src="https://img/1.png"/></figure></document>')
    assert [b["type"] for b in blocks] == ["paragraph", "heading_3", "numbered_list_item", "code", "paragraph"]
    rich = blocks[0]["paragraph"]["rich_text"]
    assert rich[1]["annotations"] == {"bold": True} and rich[3]["annotations"] == {"code": True}
    assert rich[5]["text"]["link"] == {"url": "https://x.y"} and rich[6]["type"] == "equation"
    assert blocks[3]["code"]["language"] == "java" and blocks[3]["code"]["rich_text"][0]["text"]["content"] == "int x = 1;"


# --- clarifications -----------------------------------------------------------------------
def test_staff_answered_question_and_staff_post_go_to_their_assignment():
    threads = [
        thread(11, "Q2 clarification", STUDENT, sub="HW3", reply_count=2,
               answers=[reply(101, TA, "Yes, assume n > 0.", kind="answer")],
               comments=[reply(102, STUDENT, "thanks!")]),
        thread(12, "Midterm 1 coverage", STAFF, category="Exams", kind="announcement",
               content=doc("Covers lectures 1-9.")),
        thread(13, "HW3 study group?", STUDENT, reply_count=3, comments=[reply(103, STUDENT, "me too")]),
        thread(14, "Homework 4 q1", STUDENT, reply_count=0),
    ]
    notion, _, pages, ed = world(threads)
    report = run(notion, ed)
    assert report.errors == []

    blocks, hw3 = section(notion, pages["Homework 3"])
    assert [b["type"] for b in blocks][:2] == ["heading_2", "heading_2"]   # right after Instructions
    assert n._plain(blocks[1]["heading_2"]["rich_text"]) == TOGGLE
    shown = "\n".join(texts(hw3))
    assert "#11 Q2 clarification" in shown and "Answer from Ada TA (staff)" in shown and "assume n > 0" in shown
    assert "thanks!" not in shown and "study group" not in shown              # student-only replies left out
    link = hw3[1]["heading_3"]["rich_text"][0]["text"]["link"]["url"]
    assert link == "https://edstem.org/us/courses/900/discussion/11"

    _, exam = section(notion, pages["Midterm Exam 1"])
    assert "Covers lectures 1-9." in "\n".join(texts(exam))
    assert "Announcement by Dana Rivera (staff)" in "\n".join(texts(exam))
    assert section(notion, pages["Homework 4"])[1] is None                   # nothing from staff: no section
    assert 14 not in ed.opened                                               # no replies: not even opened


def test_endorsed_student_answer_counts():
    threads = [thread(21, "HW3 edge case", STUDENT, sub="HW3", reply_count=1,
                      answers=[reply(201, STUDENT, "Return -1 for empty input.", kind="answer", endorsed=True)])]
    notion, _, pages, ed = world(threads)
    run(notion, ed)
    shown = "\n".join(texts(section(notion, pages["Homework 3"])[1]))
    assert "Answer from a student (endorsed by staff)" in shown and "Return -1" in shown


def test_nested_staff_comment_is_found():
    threads = [thread(31, "hw 3 part c", STUDENT, reply_count=2, category="General",
                      comments=[reply(301, STUDENT, "same question", nested=[reply(302, TA, "Use the hint in 2b.")])])]
    notion, _, pages, ed = world(threads)
    run(notion, ed)
    assert "Use the hint in 2b." in "\n".join(texts(section(notion, pages["Homework 3"])[1]))


def test_unchanged_threads_are_not_reopened_and_changes_rewrite_the_section():
    t = thread(11, "Q2", STUDENT, sub="HW3", reply_count=1, answers=[reply(101, TA, "First answer", kind="answer")])
    notion, _, pages, ed = world([t])
    run(notion, ed)
    ed.opened.clear()
    before = len(notion.writes)
    run(notion, ed)
    assert ed.opened == [] and len(notion.writes) == before

    t["answers"].append(reply(104, STAFF, "Correction: n >= 0.", kind="comment"))
    t["updated_at"] = "2026-11-02T10:00:00Z"          # Ed bumps this on views: not a change
    before = len(notion.writes)
    run(notion, ed)
    assert len(notion.writes) == before
    t["reply_count"] = 2
    run(notion, ed)
    blocks, hw3 = section(notion, pages["Homework 3"])
    assert "Correction: n >= 0." in "\n".join(texts(hw3))
    assert sum(1 for b in blocks if b["type"] == "heading_2" and n._plain(b["heading_2"]["rich_text"]) == TOGGLE) == 1


def test_course_is_found_on_ed_by_code_and_the_link_replaced():
    notion, course, _, ed = world([], ed_id="",
                                  courses=[{"id": 77, "code": "COMS W3134 002", "name": "Data Structures",
                                            "status": "active", "year": "2026"},
                                           {"id": 5, "code": "COMS W3134", "status": "archived", "year": "2025"}])
    notion.pages[course["id"]]["properties"]["Ed URL"] = n.url("https://canvas.example.edu/courses/1/external_tools/37606")
    report = run(notion, ed)
    page = notion.pages[course["id"]]
    assert n.get_text(page, "Ed ID") == "77"
    assert n.get_url(page, "Ed URL") == "https://edstem.org/us/courses/77/discussion"
    assert any("linked to Ed course 77" in m for m in report.notes)


def test_course_not_on_ed_is_skipped_quietly():
    notion, _, _, ed = world([], ed_id="", courses=[{"id": 9, "code": "PHIL GR6552"}])
    report = run(notion, ed)
    assert report.errors == [] and report.updated == 0


def test_matching_uses_course_aliases_and_prefers_the_title():
    from scheduleaid.email_analysis import KnownAssignment as K

    known = [K("Programming Assignment 4 - Stacks", None), K("Programming Assignment 5 - BST Map", None),
             K("Theoretical Assignment 2", None)]
    aliases = (("P", "Programming Assignment"), ("HW", "Programming Assignment"), ("T", "Theoretical Assignment"))
    assert EdSync.match({"title": "HW4 Grades Released", "subcategory": "P5 - BST Map"}, known, aliases) == \
        ["Programming Assignment 4 - Stacks"]                                  # the title wins
    assert EdSync.match({"title": "Duplicate Keys", "subcategory": "P5 - BST Map"}, known, aliases) == \
        ["Programming Assignment 5 - BST Map"]
    assert EdSync.match({"title": "Q10 red-black", "subcategory": "T2 - Theory 2"}, known, aliases) == \
        ["Theoretical Assignment 2"]
    assert EdSync.match({"title": "Duplicate Keys", "subcategory": "P5 - BST Map"}, known) == []   # no aliases


def test_aliases_in_the_course_list():
    from scheduleaid import course_list as cl

    courses = cl.parse('[[course]]\nname = "DS"\ncode = "COMS W3134"\naliases = { P = "Programming Assignment" }')
    assert courses.courses[0].aliases == (("P", "Programming Assignment"),)
    import pytest
    with pytest.raises(cl.CourseListError, match="aliases"):
        cl.parse('[[course]]\nname = "DS"\ncode = "C 1"\naliases = "P"')


def test_links_notion_would_reject_become_plain_text():
    from scheduleaid.ed import safe_url

    blocks = document_blocks('<document><paragraph><link href="/courses/1/discussion/2">see #2</link> '
                             '<link href="https://edstem.org/x">ok</link></paragraph>'
                             '<figure><image src="data:image/png;base64,AAA"/></figure></document>')
    rich = blocks[0]["paragraph"]["rich_text"]
    assert "link" not in rich[0]["text"] and rich[0]["text"]["content"] == "see #2"
    assert rich[2]["text"]["link"] == {"url": "https://edstem.org/x"}
    assert "link" not in blocks[1]["paragraph"]["rich_text"][0]["text"]
    assert safe_url("//static.us.edusercontent.com/a.png") == "https://static.us.edusercontent.com/a.png"
    assert safe_url("mailto:ta@example.edu") == "mailto:ta@example.edu"
    assert safe_url("javascript:alert(1)") is None and safe_url("http://") is None
