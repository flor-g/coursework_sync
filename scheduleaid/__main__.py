"""Command line entry point.

    python -m scheduleaid sync             # the course list, CourseWorks, course websites, and
                                           # emails the Apps Script copied into Notion
    python -m scheduleaid courses          # only make Notion's Courses match courses/courses.toml
    python -m scheduleaid ed               # only Ed Discussion clarifications
    python -m scheduleaid sync --dry-run   # read everything, write nothing
    python -m scheduleaid courseworks      # one source only (also: websites, gmail)
    python -m scheduleaid convert --page ID   # convert one lecture's notes to Markdown
    python -m scheduleaid convert          # convert every lecture whose conversion was requested
    python -m scheduleaid preview          # CourseWorks only: list courses and assignments
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

from .canvas import CanvasClient
from .config import ConfigError, load_config
from .http import ApiError
from .sync_common import Report
from .sync_courseworks import CourseWorksSync, human_dt, to_local_iso
from .sync_gmail import GmailSync
from .sync_websites import WebsiteSync


def preview(cfg, canvas) -> str:
    tz = ZoneInfo(cfg.timezone)
    now = datetime.now(tz)
    lines = ["Active CourseWorks courses:"]
    for course in canvas.active_courses():
        lines.append(f"- {course['id']}: {course.get('course_code', '?')} — {course.get('name', '?')}")
        teachers = ", ".join(t.get("display_name", "?") for t in course.get("teachers") or [])
        lines.append(f"    teachers: {teachers or 'none listed'}")
        try:
            assignments = canvas.assignments(course["id"])
        except Exception as exc:
            lines.append(f"    (could not read assignments: {exc})")
            continue
        upcoming = sorted(
            (a for a in assignments if a.get("due_at") and datetime.fromisoformat(
                a["due_at"].replace("Z", "+00:00")) >= now),
            key=lambda a: a["due_at"])
        lines.append(f"    {len(assignments)} assignments, {len(upcoming)} upcoming")
        for a in assignments:
            due = human_dt(to_local_iso(a.get("due_at"), tz), tz)
            types = ",".join(a.get("submission_types") or [])
            flags = " ".join(f for f, on in [("unpublished", a.get("published") is False),
                                            ("omit-from-final", a.get("omit_from_final_grade")),
                                            ("muted", a.get("muted"))] if on)
            lines.append(f"    · {a.get('name')} | {due} | {types} | pts={a.get('points_possible')} {flags}")
    return "\n".join(lines)


def inspect(cfg, canvas, course_ids: list[str]) -> str:
    """Where do assignment instructions live? Read-only report for debugging."""
    import re

    lines = []
    for cid in course_ids:
        lines.append(f"== course {cid}")
        try:
            for tab in canvas.tabs(cid):
                lines.append(f"  tab {tab.get('label')} | {tab.get('type')} | {tab.get('html_url')}")
        except Exception as exc:
            lines.append(f"  tabs: not readable ({str(exc)[:80]})")
        for a in canvas.assignments(cid):
            desc = a.get("description") or ""
            if "gradescope" in desc.lower() or "Theoretical" in (a.get("name") or ""):
                from bs4 import BeautifulSoup
                lines.append(f"      text: {BeautifulSoup(desc, 'html.parser').get_text(' ', strip=True)[:300]}")
            if a.get("external_tool_tag_attributes"):
                lines.append(f"      external tool: {a['external_tool_tag_attributes'].get('url')}")
            links = re.findall(r'href="([^"]+)"', desc)
            file_links = [l for l in links if "/files/" in l]
            lines.append(f"  · {a.get('name')} | desc {len(desc)} chars | links {len(links)} "
                         f"(files {len(file_links)}) | types {','.join(a.get('submission_types') or [])}")
            for link in links[:4]:
                lines.append(f"      {link[:120]}")
        try:
            info = canvas.course(cid, ["syllabus_body"])
            body = info.get("syllabus_body") or ""
            lines.append(f"  syllabus: {len(body)} chars")
            import re as _re2

            from bs4 import BeautifulSoup
            text = BeautifulSoup(body, "html.parser").get_text(" ", strip=True) if body else ""
            for hit in _re2.finditer(r"[^.]*\b(exam|midterm|final)\b[^.]*\.", text, _re2.I):
                lines.append(f"    EXAMTEXT {hit.group(0).strip()[:400]}")
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(body, "html.parser")
            for row in soup.find_all("tr"):
                cells = row.find_all(["td", "th"], recursive=False)
                lines.append("    ROW " + " || ".join(c.get_text(" ", strip=True)[:70] for c in cells)[:300])
                for a in row.find_all("a", href=True):
                    lines.append(f"        a [{a.get_text(' ', strip=True)[:40]}] {a['href'][:75]}")
        except Exception as exc:
            lines.append(f"  syllabus: not readable ({str(exc)[:80]})")
        try:
            pages = canvas.pages(cid)
            lines.append(f"  pages: {len(pages)}")
            for p in pages[:30]:
                lines.append(f"      page {p.get('title')} | {p.get('url')}")
        except Exception as exc:
            lines.append(f"  pages: not readable ({str(exc)[:80]})")
        for label, fetch in (("files", canvas.files), ("modules", canvas.modules)):
            try:
                items = fetch(cid)
            except Exception as exc:
                lines.append(f"  {label}: not readable ({str(exc)[:80]})")
                continue
            lines.append(f"  {label}: {len(items)}")
            if label == "files":
                folder_names = {}
                try:
                    folder_names = {f["id"]: f.get("full_name") for f in canvas.folders(cid)}
                except Exception:
                    pass
                kinds = {}
                for f in items:
                    ext = (f.get("display_name") or "").rsplit(".", 1)[-1].lower()
                    kinds[ext] = kinds.get(ext, 0) + 1
                lines.append(f"      by type: {kinds}")
                for f in items:
                    if not (f.get("display_name") or "").lower().endswith(".java"):
                        lines.append(f"      file {f.get('id')} | {folder_names.get(f.get('folder_id'), '?')} | "
                                     f"{f.get('display_name')} | {f.get('size', 0) // 1024} KB")
                java = [f for f in items if (f.get("display_name") or "").lower().endswith(".java")]
                for f in java[:12]:
                    lines.append(f"      java {folder_names.get(f.get('folder_id'), '?')} | {f.get('display_name')}")
            else:
                for mod in items:
                    lines.append(f"      module {mod.get('name')}")
                    for item in (mod.get("items") or [])[:30]:
                        lines.append(f"         {item.get('type')} | {item.get('title')}")
    return "\n".join(lines)


def inspect_ed(cfg) -> str:
    """Ed Discussion, read-only: your courses, recent threads, and the shape of one thread."""
    from datetime import timedelta

    from .ed import EdClient, is_staff

    if not cfg.ed_token:
        return "ED_TOKEN is not set."
    ed = EdClient(cfg.ed_token, cfg.ed_region)
    lines = []
    since = datetime.now(ZoneInfo(cfg.timezone)) - timedelta(days=cfg.announcements_lookback_days)
    for c in ed.courses():
        lines.append(f"== Ed course {c.get('id')} | {c.get('code')} | {c.get('name')} | "
                     f"{c.get('year')} {c.get('session')} | {c.get('status')}")
        if str(c.get("status", "active")).lower() not in ("active", ""):
            continue
        try:
            threads, users = ed.threads(c["id"], since, max_pages=2)
        except Exception as exc:
            lines.append(f"  threads: not readable ({str(exc)[:100]})")
            continue
        lines.append(f"  {len(threads)} threads; thread keys: {sorted(threads[0])[:60] if threads else []}")
        lines.append(f"  user keys: {sorted(next(iter(users.values()), {}))}; roles: "
                     f"{sorted({str(u.get('course_role')) for u in users.values()})}")
        for t in threads[:25]:
            author = users.get(t.get("user_id"))
            lines.append(f"  #{t.get('number')} {t.get('type')} | {t.get('category')} / {t.get('subcategory')} / "
                         f"{t.get('subsubcategory')} | {str(t.get('title'))[:60]} | replies {t.get('reply_count')} | "
                         f"{'staff' if is_staff(author) else (author or {}).get('course_role')} | {t.get('updated_at')}")
        import re as _re

        from .ed import plain
        for t in threads:
            text = f"{t.get('category')} {t.get('subcategory')} {t.get('title')}"
            if not _re.search(r"\b(exam|midterm|final)\b", text, _re.I):
                continue
            detail, dusers = ed.thread(t["id"])
            lines.append(f"  EXAM #{t.get('number')} {t.get('title')} | {t.get('created_at', '')[:10]} | "
                         f"{plain(detail.get('content', ''))[:400]}")
            stack = list(detail.get("answers") or []) + list(detail.get("comments") or [])
            while stack:
                r = stack.pop(0)
                stack += r.get("comments") or []
                if is_staff(dusers.get(r.get("user_id"))):
                    lines.append(f"     staff: {plain(r.get('content', ''))[:400]}")
        sample = None
        if sample:
            detail, dusers = ed.thread(sample["id"])
            lines.append(f"  detail #{sample.get('number')} keys: {sorted(detail)[:60]}")
            for kind in ("answers", "comments"):
                for r in (detail.get(kind) or [])[:3]:
                    lines.append(f"    {kind[:-1]} keys {sorted(r)[:40]} | role "
                                 f"{(dusers.get(r.get('user_id')) or {}).get('course_role')} | endorsed {r.get('is_endorsed')}"
                                 f" | nested {len(r.get('comments') or [])}")
            lines.append(f"    content sample: {str(detail.get('content'))[:300]}")
    return "\n".join(lines)


def check(cfg, canvas) -> tuple[str, bool]:
    """Can each service be reached with the configured keys? Costs nothing."""
    results = []
    try:
        results.append(f"CourseWorks: ok, {len(canvas.active_courses())} active courses")
    except Exception as exc:
        results.append(f"CourseWorks: FAILED ({str(exc)[:150]})")
    if cfg.notion_token:
        from .notion import NotionClient

        try:
            client = NotionClient(cfg.notion_token)
            courses = client.query_all(cfg.courses_ds)
            results.append(f"Notion: ok, {len(courses)} courses")
            try:
                templates = client.templates(cfg.courses_ds)
                results.append(f"Notion course templates: {len(templates)} "
                               f"({', '.join(t.get('name') or '?' for t in templates) or 'none'})")
            except Exception as exc:
                results.append(f"Notion course templates: not available ({str(exc)[:150]})")
        except Exception as exc:
            results.append(f"Notion: FAILED ({str(exc)[:150]})")
    else:
        results.append("Notion: NOTION_TOKEN not set")
    if cfg.ed_token:
        from .ed import EdClient

        try:
            courses = EdClient(cfg.ed_token, cfg.ed_region).courses()
            results.append(f"Ed: ok, {len(courses)} courses ({', '.join(str(c.get('code')) for c in courses[:8])})")
        except Exception as exc:
            results.append(f"Ed: FAILED ({str(exc)[:150]})")
    else:
        results.append("Ed: ED_TOKEN not set")
    if cfg.anthropic_enabled:
        import anthropic

        client = anthropic.Anthropic()
        for label, model in (("emails", cfg.email_model), ("lecture conversion", cfg.convert_model)):
            try:
                client.models.retrieve(model)
                results.append(f"Anthropic ({label}): ok, {model} is available")
            except anthropic.AuthenticationError:
                results.append("Anthropic: FAILED, the API key was rejected")
                break
            except anthropic.APIStatusError as exc:
                results.append(f"Anthropic ({label}): FAILED for {model} ({exc.status_code})")
    else:
        results.append("Anthropic: ANTHROPIC_API_KEY not set")
    return "\n".join(results), not any("FAILED" in r for r in results)


def write_step_summary(text: str) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(text + "\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="scheduleaid")
    parser.add_argument("command", choices=["sync", "courses", "courseworks", "websites", "gmail", "ed", "convert", "preview",
                                            "inspect", "check"])
    parser.add_argument("--dry-run", action="store_true", help="read only; log what would change")
    parser.add_argument("--page", help="convert: the Notion page id of one lecture")
    parser.add_argument("--ed", action="store_true", help="inspect: Ed Discussion instead of CourseWorks")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(levelname)s %(name)s: %(message)s")
    logging.getLogger("googleapiclient").setLevel(logging.WARNING)

    try:
        cfg = load_config()
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2
    canvas = CanvasClient(cfg.canvas_base_url, cfg.canvas_token)

    if args.command == "check":
        text, ok = check(cfg, canvas)
        print(text)
        write_step_summary("## Check\n\n" + text.replace("\n", "  \n"))
        return 0 if ok else 1

    if args.command == "inspect" and args.ed:
        print(inspect_ed(cfg))
        return 0

    if args.command == "inspect":
        from .course_list import load

        print(inspect(cfg, canvas, [c.canvas_id for c in load().courses if c.canvas_id]))
        return 0

    if args.command == "preview":
        text = preview(cfg, canvas)
        print(text)
        write_step_summary("## CourseWorks preview\n\n```\n" + text + "\n```")
        return 0

    if not cfg.notion_token:
        print("Configuration error: NOTION_TOKEN is not set.", file=sys.stderr)
        return 2
    from .notion import NotionClient

    notion_client = NotionClient(cfg.notion_token, dry_run=args.dry_run)
    try:
        report = Report()
        course_pages = notion_client.query_all(cfg.courses_ds)
        def stage(name, fn):
            try:
                fn()
            except ApiError as exc:
                if exc.status == 401 or (exc.status == 404 and "Notion" in str(exc)):
                    raise  # a credential/access problem affects everything: stop and explain
                logging.exception("%s failed", name)
                report.errors.append(f"{name}: {exc}")

        if args.command in ("sync", "courses"):
            from .course_list import CourseListSync

            def course_list():
                nonlocal course_pages
                course_pages = CourseListSync(cfg, notion_client, report=report).run(course_pages)

            stage("Course list", course_list)
        if args.command in ("sync", "courseworks"):
            stage("CourseWorks", lambda: CourseWorksSync(cfg, canvas, notion_client, report=report).run(course_pages))
        if args.command in ("sync", "websites"):
            stage("Websites", lambda: WebsiteSync(cfg, notion_client, report=report).run(course_pages))
        if args.command in ("sync", "gmail"):
            from .email_analysis import ClaudeAnalyzer, RuleAnalyzer

            analyzer = ClaudeAnalyzer(cfg.email_model) if cfg.anthropic_enabled else RuleAnalyzer()
            stage("Gmail", lambda: GmailSync(cfg, notion_client, analyzer, report=report).run(course_pages))
            if not cfg.anthropic_enabled:
                report.notes.append("ANTHROPIC_API_KEY not set: emails are summarized with subject-line rules.")
        if args.command in ("sync", "ed"):
            if cfg.ed_token:
                from .ed import EdClient
                from .sync_ed import EdSync

                ed = EdClient(cfg.ed_token, cfg.ed_region)
                stage("Ed", lambda: EdSync(cfg, ed, notion_client, report=report).run(course_pages))
            elif args.command == "ed":
                report.errors.append("ED_TOKEN is not set; add it as a repository secret.")
        if args.command in ("sync", "convert"):
            from .convert import LectureConverter

            converter = LectureConverter(cfg, notion_client, report=report)
            if args.page:
                stage("Conversion", lambda: converter.convert_page_id(args.page))
            elif cfg.anthropic_enabled or args.command == "convert":
                stage("Conversion", converter.run_waiting)
        if args.command == "sync":
            from .course_pages import CoursePageSync

            stage("Course pages", lambda: CoursePageSync(cfg, notion_client, report=report).run(course_pages))
    except ApiError as exc:
        hint = ""
        if exc.status == 404 and "Notion" in str(exc):
            hint = ("\nHint: open the Notion page that holds your databases → ⋯ → Connections and add your "
                    "integration, so it can see the databases.")
        elif exc.status == 401:
            hint = "\nHint: the token was rejected. Check the repository secret."
        print(f"Sync failed: {exc}{hint}", file=sys.stderr)
        write_step_summary(f"## Sync failed\n\n{exc}{hint}")
        return 1
    title = f"## {args.command.capitalize()}" + (" (dry run)" if args.dry_run else "")
    print(report.summary())
    write_step_summary(f"{title}\n\n{report.summary()}")
    return 1 if report.errors else 0


if __name__ == "__main__":
    sys.exit(main())
