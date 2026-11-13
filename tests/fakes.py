"""In-memory stand-ins for CourseWorks, Notion and Google Calendar."""

from __future__ import annotations

import copy
import itertools

_ids = itertools.count(1)


class FakeCanvas:
    base = "https://canvas.example.edu"

    def __init__(self, courses, assignments=None, announcements=None):
        self.courses = courses
        self.by_course = assignments or {}
        self.ann_by_course = announcements or {}

    def active_courses(self):
        return copy.deepcopy(self.courses)

    def assignments(self, course_id):
        return copy.deepcopy(self.by_course.get(course_id, []))

    def announcements(self, course_id, since):
        return copy.deepcopy(self.ann_by_course.get(course_id, []))

    def course_url(self, course_id):
        return f"{self.base}/courses/{course_id}"

    tabs_by_course: dict = {}

    def tabs(self, course_id):
        return copy.deepcopy(self.tabs_by_course.get(course_id, []))

    # files linked from descriptions: {file_id: (meta, data)}; missing ids → 403
    files_by_id: dict = {}
    # Files area listing per course ([meta]); a course missing here answers 403
    listing_by_course: dict = {}
    folders_by_course: dict = {}
    syllabus_by_course: dict = {}

    @staticmethod
    def _forbidden():
        from scheduleaid.http import ApiError
        return ApiError("CourseWorks", type("R", (), {
            "status_code": 403, "text": "forbidden", "url": "https://cw/api",
            "request": type("Q", (), {"method": "GET"})()})())

    def file(self, file_id, course_id=None, verifier=None):
        if str(file_id) not in self.files_by_id:
            raise self._forbidden()
        return self.files_by_id[str(file_id)][0]

    def files(self, course_id):
        if course_id not in self.listing_by_course:
            raise self._forbidden()
        return copy.deepcopy(self.listing_by_course[course_id])

    def folders(self, course_id):
        return copy.deepcopy(self.folders_by_course.get(course_id, []))

    def course(self, course_id, include=None):
        return {"id": course_id, "syllabus_body": self.syllabus_by_course.get(course_id, "")}

    def download(self, url):
        for meta, data in self.files_by_id.values():
            if meta["url"] == url:
                return data
        raise KeyError(url)


def _matches(page, f):
    if "and" in f:
        return all(_matches(page, x) for x in f["and"])
    if "or" in f:
        return any(_matches(page, x) for x in f["or"])
    prop = page["properties"].get(f["property"], {})
    if "select" in f:
        value = (prop.get("select") or {}).get("name")
        if "does_not_equal" in f["select"]:
            return value != f["select"]["does_not_equal"]
        return value == f["select"]["equals"]
    if "checkbox" in f:
        return prop.get("checkbox", False) == f["checkbox"]["equals"]
    if "date" in f:
        start = ((prop.get("date") or {}).get("start") or "")[:10]
        if not start:
            return False
        if "on_or_after" in f["date"]:
            return start >= f["date"]["on_or_after"]
        if "on_or_before" in f["date"]:
            return start <= f["date"]["on_or_before"]
    if "relation" in f:
        return any(r["id"] == f["relation"]["contains"] for r in prop.get("relation", []))
    if "rich_text" in f and f["rich_text"].get("is_empty"):
        return not "".join(i["text"]["content"] for i in prop.get("rich_text", []))
    raise NotImplementedError(f)


class FakeNotion:
    def __init__(self):
        self.dry_run = False
        self.pages: dict[str, dict] = {}      # page_id -> {"ds":..., "properties":...}
        self.children: dict[str, list[dict]] = {}
        self.writes: list[tuple] = []

    # helpers for tests
    def add_page(self, ds, properties):
        pid = f"page-{next(_ids)}"
        self.pages[pid] = {"id": pid, "url": f"https://notion.so/{pid}", "ds": ds,
                           "properties": copy.deepcopy(properties)}
        self.children[pid] = []
        return self.pages[pid]

    def in_ds(self, ds):
        return [p for p in self.pages.values() if p["ds"] == ds]

    def _store_blocks(self, parent, blocks, after=None):
        siblings = self.children.setdefault(parent, [])
        pos = len(siblings)
        if after is not None:
            pos = next(i for i, b in enumerate(siblings) if b["id"] == after) + 1
        for b in blocks:
            bid = f"block-{next(_ids)}"
            stored = copy.deepcopy(b)
            stored["id"] = bid
            stored["has_children"] = False
            siblings.insert(pos, stored)
            pos += 1
            self.children[bid] = []
        if parent in self.children:
            for blk in self._all_blocks():
                if blk["id"] == parent:
                    blk["has_children"] = bool(self.children[parent])

    def _all_blocks(self):
        for blocks in self.children.values():
            yield from blocks

    # NotionClient interface
    def query_all(self, ds, filter=None):
        pages = self.in_ds(ds)
        if filter:
            pages = [p for p in pages if _matches(p, filter)]
        return copy.deepcopy(pages)

    def get_page(self, page_id):
        return copy.deepcopy(self.pages[page_id])

    course_templates: list = []

    def templates(self, ds):
        return copy.deepcopy(self.course_templates) if ds == "courses" else []

    def create_page(self, ds, properties, children=None, template_id=None):
        self.writes.append(("create", ds) if template_id is None else ("create", ds, "template", template_id))
        page = self.add_page(ds, properties)
        self._store_blocks(page["id"], children or [])
        return copy.deepcopy(page)

    def update_page(self, page_id, properties):
        self.writes.append(("update", page_id, tuple(sorted(properties))))
        self.pages[page_id]["properties"].update(copy.deepcopy(properties))
        return {"id": page_id}

    def list_children(self, block_id):
        return copy.deepcopy(self.children.get(block_id, []))

    def append_children(self, block_id, children, after=None):
        self.writes.append(("append", block_id, len(children)))
        self._store_blocks(block_id, children, after=after)

    def upload_file(self, filename, content_type, data):
        self.writes.append(("upload", filename, len(data)))
        return f"upload-{next(_ids)}"

    def update_block(self, block_id, body):
        self.writes.append(("update_block", block_id))
        for blk in self._all_blocks():
            if blk["id"] == block_id:
                blk[blk["type"]].update(copy.deepcopy(body[blk["type"]]))

    def delete_block(self, block_id):
        self.writes.append(("delete", block_id))
        for parent, blocks in self.children.items():
            self.children[parent] = [b for b in blocks if b["id"] != block_id]
        for blk in self._all_blocks():
            blk["has_children"] = bool(self.children.get(blk["id"]))
