"""Lecture slides and course code from CourseWorks and course websites."""

import io
import zipfile
from datetime import datetime
from zoneinfo import ZoneInfo

from scheduleaid import materials as mat
from scheduleaid import notion as n
from scheduleaid.sync_courseworks import CourseWorksSync
from scheduleaid.sync_websites import WebsiteSync
from scheduleaid.websites import parse_code_dir
from tests.fakes import FakeCanvas, FakeNotion
from tests.test_sync import course_props, make_config

TZ = ZoneInfo("America/New_York")
NOW = datetime(2026, 11, 2, 12, 0, tzinfo=TZ)
CW = "https://canvas.example.edu"

SYLLABUS = f"""
<table><tr><th>Dates (Week)</th><th>Topics</th><th>Weiss</th><th>Notes</th></tr>
<tr><td>Sep. 9</td><td>Configuring environment Source code: <a href="{CW}/courses/111/files/10?wrap=1">Add.java</a>
  <a href="{CW}/courses/111/files/11">Java Review Slides</a></td><td></td><td></td></tr>
<tr><td>Sep. 28 and 30</td><td>Array Lists, Linked Lists, Interfaces, and Iterators.
  <a href="{CW}/courses/111/files/12">Slides</a> <a href="{CW}/courses/111/files/13">MyArrayList.java</a>
  <a href="{CW}/courses/111/files/14">MyMath.zip</a></td><td>Ch. 1.2</td>
  <td><a href="{CW}/courses/111/files/15">ProcessGradesAnnotations.pdf</a>
  <a href="{CW}/courses/111/files/16">Recitation - Array Lists and Linked Lists</a></td></tr>
<tr><td>Oct. 5</td><td>In-class midterm 1</td><td></td><td></td></tr>
<tr><td>Nov. 16 and 18</td><td><a href="{CW}/courses/111/files/17">Slides - Sorting</a></td><td></td><td></td></tr>
</table>
<table><tr><td>A</td><td>93-100</td></tr></table>
"""


def zipped(files):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, data in files.items():
            z.writestr(name, data)
    return buf.getvalue()


def meta(fid, name, size=100, updated="2026-09-01T00:00:00Z", **extra):
    m = {"id": fid, "display_name": name, "size": size, "updated_at": updated,
         "created_at": "2026-09-15T14:00:00Z", "url": f"{CW}/files/{fid}/download?sig=1"}
    m.update(extra)
    return m


def canvas_with(files, listing=None, folders=None, syllabus=""):
    canvas = FakeCanvas([{"id": 111, "course_code": "COMSW3134_001_2026_3", "name": "Data Structures"}])
    canvas.files_by_id = {str(m["id"]): (m, data) for m, data in files}
    canvas.listing_by_course = {"111": listing} if listing is not None else {}
    canvas.folders_by_course = {"111": folders or []}
    canvas.syllabus_by_course = {"111": syllabus}
    canvas.tabs_by_course = {}
    return canvas


def sync(notion, canvas):
    return CourseWorksSync(make_config(code_ds="code"), canvas, notion, now=NOW).run()


def by_name(notion, ds):
    return {n.get_text(p, "Name"): p for p in notion.in_ds(ds)}


def toggle_children(notion, page, name):
    toggle = next(b for b in notion.children[page["id"]]
                  if b["type"] == "heading_2" and n._plain(b["heading_2"]["rich_text"]) == name)
    return notion.children[toggle["id"]]


# --- helpers ------------------------------------------------------------------
def test_syllabus_rows_find_dates_topics_and_file_links():
    rows = mat.syllabus_rows(SYLLABUS, NOW)
    assert [r.label for r in rows] == [
        "Week 01 · Sep 9: Configuring environment",
        "Week 04 · Sep 28: Array Lists, Linked Lists, Interfaces, and Iterators",
        "Week 11 · Nov 16",
    ]
    assert [link.file_id for link in rows[1].links] == ["12", "13", "14", "15", "16"]
    assert rows[1].day.isoformat() == "2026-09-28"


def test_what_counts_as_lecture_material():
    assert mat.is_lecture_material("Sorting.pdf", "Slides - Sorting")
    assert mat.is_lecture_material("Recitation - Stacks.pptx")
    for name, text in [("Add.java", ""), ("Solutions.pdf", "Solutions"), ("x.pdf", "Complexity Exercises"),
                       ("ProcessGradesAnnotations.pdf", ""), ("DS_SoftwareInstallation.pdf", ""),
                       ("BinarySearchWorksheet.docx", ""), ("Preorder.png", "")]:
        assert not mat.is_lecture_material(name, text), name
    assert mat.lecture_title("MasterTheorem.pdf", "Slides") == "MasterTheorem"
    assert mat.lecture_title("x.pptx", "Recitation - Analysis of Algorithms.pptx") == "Recitation - Analysis of Algorithms"


def test_folder_helpers():
    assert mat.folder_group("course files/Lecture 12/section 2") == "Lecture 12"
    assert mat.folder_group("course files/Lecture 3") == "Lecture 03"
    assert mat.folder_group("course files/Misc Homework Files") == "Misc Homework Files"
    assert mat.is_lecture_slides_in_folder("course files/Lecture 3", "1004-3.ppt")
    assert not mat.is_lecture_slides_in_folder("course files/midterm", "midterm1.pdf")
    assert mat.is_code_in_folder("Wrapper.java") and mat.is_code_in_folder("homework-2.zip")
    assert not mat.is_code_in_folder("AIToolkit Transcript.txt")


def test_zip_members_skip_folders_and_mac_junk():
    data = zipped({"MyMath/MyMath.java": "class MyMath {}", "MyMath/MyMathTest.java": "class T {}",
                   "__MACOSX/MyMath/._MyMath.java": "junk", "MyMath/README.pdf": "%PDF"})
    assert [p for p, _ in mat.zip_code_members(data)] == ["MyMath/MyMath.java", "MyMath/MyMathTest.java"]


# --- CourseWorks: syllabus-linked files (Files area closed) ---------------------------
def syllabus_world():
    files = [
        (meta(10, "Add.java"), b"public class Add {}\n"),
        (meta(11, "JavaReview.pptx"), b"pptx"),
        (meta(12, "ListsIterators.pdf"), b"%PDF"),
        (meta(13, "MyArrayList.java"), b"public class MyArrayList {}\n"),
        (meta(14, "MyMath.zip"), zipped({"MyMath/MyMath.java": "class MyMath {}"})),
        (meta(15, "ProcessGradesAnnotations.pdf"), b"%PDF"),
        (meta(16, "Recitation - Array Lists and Linked Lists.pptx"), b"pptx"),
        # 17 (Sorting) is not published yet: 403
    ]
    notion = FakeNotion()
    course = notion.add_page("courses", course_props("Data Structures", "COMS W3134", "111"))
    return notion, canvas_with(files, syllabus=SYLLABUS), course


def test_syllabus_slides_become_lectures_and_code_gets_its_own_pages():
    notion, canvas, course = syllabus_world()
    report = sync(notion, canvas)
    assert report.errors == []

    lectures = by_name(notion, "lectures")
    assert set(lectures) == {"Java Review Slides", "ListsIterators", "Recitation - Array Lists and Linked Lists"}
    lists = lectures["ListsIterators"]
    assert n.get_number(lists, "Lecture #") == 2 and n.get_date(lists, "Date") == "2026-09-28"
    assert n.get_url(lists, "Original URL") == f"{CW}/courses/111/files/12"
    assert n.get_select(lists, "Format") == "PDF" and n.get_select(lectures["Java Review Slides"], "Format") == "Slides"
    assert n.get_select(lists, "Conversion status") == "Pending"

    code = by_name(notion, "code")
    assert set(code) == {"Add.java", "MyArrayList.java", "MyMath.java"}
    arr = code["MyArrayList.java"]
    assert n.get_text(arr, "Group") == "Week 04 · Sep 28: Array Lists, Linked Lists, Interfaces, and Iterators"
    assert n.get_select(arr, "Language") == "java" and n.get_select(arr, "Source") == "CourseWorks"
    assert arr["properties"]["Lecture"]["relation"] == [{"id": lists["id"]}]     # not the recitation
    assert arr["properties"]["Course"]["relation"] == [{"id": course["id"]}]
    blocks = toggle_children(notion, arr, "Code")
    assert blocks[0]["type"] == "code" and blocks[0]["code"]["language"] == "java"
    assert blocks[0]["code"]["rich_text"][0]["text"]["content"] == "public class MyArrayList {}\n"
    assert blocks[1]["type"] == "file" and blocks[1]["file"]["name"] == "MyArrayList.java.txt"
    assert ("upload", "MyArrayList.java.txt", 28) in notion.writes
    assert n.get_text(code["MyMath.java"], "Group").endswith("· MyMath.zip")


def test_second_run_downloads_nothing_new():
    notion, canvas, _ = syllabus_world()
    sync(notion, canvas)
    before = len(notion.writes)
    downloads = []
    original = canvas.download
    canvas.download = lambda url: downloads.append(url) or original(url)
    report = sync(notion, canvas)
    assert downloads == [] and len(notion.writes) == before and report.created == 0


def test_changed_file_is_rewritten():
    notion, canvas, _ = syllabus_world()
    sync(notion, canvas)
    m, _ = canvas.files_by_id["13"]
    canvas.files_by_id["13"] = (dict(m, updated_at="2026-10-01T00:00:00Z"), b"class MyArrayList { int n; }\n")
    sync(notion, canvas)
    page = by_name(notion, "code")["MyArrayList.java"]
    code_block = toggle_children(notion, page, "Code")[0]
    assert code_block["code"]["rich_text"][0]["text"]["content"] == "class MyArrayList { int n; }\n"
    assert len(toggle_children(notion, page, "Code")) == 2


# --- CourseWorks: Files area with Lecture folders (Intro to Java) ----------------------
def test_lecture_folders_give_slides_and_deduplicated_code():
    folders = [{"id": 1, "full_name": "course files/Lecture 3"},
               {"id": 2, "full_name": "course files/Lecture 3/section 1"},
               {"id": 3, "full_name": "course files/Lecture 3/section 2"},
               {"id": 4, "full_name": "course files/Misc Homework Files"},
               {"id": 5, "full_name": "course files/midterm"}]
    files = [
        (meta(20, "1004-3.ppt", folder_id=1), b"ppt"),
        (meta(21, "Wrapper.java", size=50, folder_id=2), b"class Wrapper {}"),
        (meta(22, "Wrapper.java", size=50, folder_id=3), b"class Wrapper {}"),          # same file again
        (meta(23, "Tester.java", size=60, folder_id=2), b"class Tester {}"),
        (meta(24, "Tester.java", size=70, folder_id=3), b"class Tester { }"),          # differs
        (meta(25, "Transcript.txt", folder_id=2), b"chat"),
        (meta(26, "homework-2.zip", folder_id=4), zipped({"hw2/Main.java": "class Main {}"})),
        (meta(27, "midterm1.pdf", folder_id=5), b"%PDF"),
    ]
    notion = FakeNotion()
    notion.add_page("courses", course_props("Data Structures", "COMS W3134", "111"))
    canvas = canvas_with(files, listing=[m for m, _ in files], folders=folders)
    assert sync(notion, canvas).errors == []

    lectures = by_name(notion, "lectures")
    assert list(lectures) == ["Lecture 3"]
    assert n.get_number(lectures["Lecture 3"], "Lecture #") == 3
    assert n.get_date(lectures["Lecture 3"], "Date") == "2026-09-15"
    assert n.get_select(lectures["Lecture 3"], "Format") == "Slides"

    code = by_name(notion, "code")
    assert set(code) == {"Wrapper.java", "Tester.java (section 1)", "Tester.java (section 2)", "Main.java"}
    tester2 = code["Tester.java (section 2)"]
    assert n.get_select(tester2, "Language") == "java"
    blocks = toggle_children(notion, tester2, "Code")
    assert blocks[0]["code"]["language"] == "java" and blocks[1]["file"]["name"] == "Tester.java.txt"
    assert n.get_text(code["Wrapper.java"], "Group") == "Lecture 03"
    assert code["Wrapper.java"]["properties"]["Lecture"]["relation"] == [{"id": lectures["Lecture 3"]["id"]}]
    assert n.get_text(code["Main.java"], "Group") == "Misc Homework Files · homework-2.zip"


# --- course website code pages (Advanced Programming) --------------------------------
CODE_PAGE = """<h1>Index of 2026-9/code/02</h1>
<h2 id="makefile">Makefile</h2><p><a href="/www/2026-9/code/02/Makefile"><button>Download</button></a></p>
<figure><pre><code>myprogram: myprogram.o\n\tgcc -o myprogram myprogram.o\n</code></pre></figure>
<h2 id="myaddc">myadd.c</h2><p><a href="/www/2026-9/code/02/myadd.c">Download</a></p>
<figure><pre><code>int add(int x, int y) { return x + y; }</code></pre></figure>"""


def test_code_dir_page_is_parsed():
    files = parse_code_dir(CODE_PAGE)
    assert [(f.name, f.anchor) for f in files] == [("Makefile", "makefile"), ("myadd.c", "myaddc")]
    assert files[0].text.startswith("myprogram: myprogram.o\n\tgcc")


def test_website_code_goes_to_the_code_database():
    site = "https://cs3157.github.io/www/2026-9/"
    index = """<h2>Assignments</h2><ul></ul>
    <table><tr><td>Lecture 4: R 9/17</td></tr></table>
    <ul><li><a href="lecture-notes/02-compile.pdf">Lecture notes 02: Separate Compilation and Make</a>
      <ul><li><a href="code/02/#makefile">Makefile</a></li></ul></li></ul>"""
    pages = {site: index, site + "code/": '<a href="/www/2026-9/code/02">02</a>',
             site + "code/02/": CODE_PAGE}
    notion = FakeNotion()
    notion.add_page("courses", {**course_props("Advanced Programming", "COMS W3157"),
                                "Website": n.url(site)})
    report = WebsiteSync(make_config(code_ds="code"), notion, now=NOW, fetch=lambda u: pages[u]).run()
    assert report.errors == []
    code = by_name(notion, "code")
    assert set(code) == {"Makefile", "myadd.c"}
    lecture = by_name(notion, "lectures")["Lecture notes 02: Separate Compilation and Make"]
    mk = code["Makefile"]
    assert n.get_text(mk, "Group") == "Lecture notes 02: Separate Compilation and Make"
    assert n.get_date(mk, "Date") == "2026-09-17" and n.get_select(mk, "Language") == "makefile"
    assert n.get_url(mk, "Source URL") == site + "code/02/#makefile"
    assert mk["properties"]["Lecture"]["relation"] == [{"id": lecture["id"]}]
    assert n.get_select(code["myadd.c"], "Language") == "c"


def test_a_reuploaded_file_takes_over_its_lecture_and_flags_old_notes():
    notion, canvas, _ = syllabus_world()
    sync(notion, canvas)
    lists = by_name(notion, "lectures")["ListsIterators"]
    # pretend it was converted
    notion.pages[lists["id"]]["properties"]["Conversion status"] = n.select("Converted")
    notes = next(b for b in notion.children[lists["id"]] if n._plain(b["heading_2"]["rich_text"]) == "Notes")
    for child in list(notion.children[notes["id"]]):
        notion.delete_block(child["id"])
    notion._store_blocks(notes["id"], [{"object": "block", "type": "paragraph", "paragraph": {"rich_text": [
        {"type": "text", "text": {"content": "Rewritten as lecture notes on Nov 2."}}]}}])

    # the professor uploads a new version: new file id, same link in the syllabus row
    canvas.syllabus_by_course = {"111": SYLLABUS.replace("/files/12\"", "/files/112\"")}
    canvas.files_by_id["112"] = (meta(112, "ListsIterators.pdf", updated="2026-11-03T05:00:00Z"), b"%PDF v2")
    report = sync(notion, canvas)

    pages = [p for p in notion.in_ds("lectures") if n.get_text(p, "Name") == "ListsIterators"]
    assert len(pages) == 1                                                  # no duplicate
    page = pages[0]
    assert n.get_text(page, "Source ID") == "cw:111:file:112"
    assert n.get_url(page, "Original URL") == f"{CW}/courses/111/files/112"
    assert n.get_url(page, "Source URL") == f"{CW}/courses/111/files/112"
    blocks = notion.children[page["id"]]
    source = next(i for i, b in enumerate(blocks) if b["type"] == "heading_2"
                  and n._plain(b["heading_2"]["rich_text"]) == "Source")
    assert blocks[source + 1]["paragraph"]["rich_text"][-1]["text"]["link"] == {"url": f"{CW}/courses/111/files/112"}
    assert n.get_select(page, "Conversion status") == "Pending"
    first = notion.children[notes["id"]][0]["paragraph"]["rich_text"]
    assert "earlier version of the slides" in first[0]["text"]["content"] and first[0]["annotations"] == {"color": "red"}
    assert any("new version of the file" in m for m in report.notes)


def test_a_reuploaded_code_file_keeps_its_code_page():
    notion, canvas, _ = syllabus_world()
    sync(notion, canvas)
    canvas.syllabus_by_course = {"111": SYLLABUS.replace("/files/13\"", "/files/113\"")}
    canvas.files_by_id["113"] = (meta(113, "MyArrayList.java", updated="2026-11-03T05:00:00Z"),
                                 b"public class MyArrayList { int size; }\n")
    sync(notion, canvas)
    pages = [p for p in notion.in_ds("code") if n.get_text(p, "Name") == "MyArrayList.java"]
    assert len(pages) == 1 and n.get_text(pages[0], "Source ID") == "cw:111:file:113"
    assert "int size" in toggle_children(notion, pages[0], "Code")[0]["code"]["rich_text"][0]["text"]["content"]
