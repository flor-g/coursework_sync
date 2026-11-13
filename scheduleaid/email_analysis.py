"""Turn one course email into a record for the tracker: a title, a summary, a category, whether
it matters, which known assignments it is about, and any new to-dos with dates.

ClaudeAnalyzer asks Claude (structured JSON output). RuleAnalyzer uses subject-line rules and is
used when no Anthropic API key is set, or when a Claude call fails.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup

from . import prompts
from .claude import messages_api

log = logging.getLogger(__name__)

# The reply format Claude must follow (prompts/email_summary.schema.json). Email categories and
# to-do types are read from it, so the prompt files are the one place to change them.
SCHEMA = prompts.load_json("email_summary.schema")
CATEGORIES = SCHEMA["properties"]["category"]["enum"]
TASK_TYPES = SCHEMA["properties"]["new_tasks"]["items"]["properties"]["type"]["enum"]
MAX_BODY_CHARS = 40_000


@dataclass
class Email:
    id: str
    thread_id: str
    subject: str
    sender: str
    sender_email: str
    date: datetime
    text: str = ""
    html: str = ""
    attachments: list[str] = field(default_factory=list)   # file names


@dataclass
class RelatedRef:
    name: str
    due_stated: str = ""        # ISO local date/datetime the email gives, or ""


@dataclass
class NewTask:
    name: str
    due: str                    # ISO local date/datetime, or ""
    type: str = "Action item"


@dataclass
class Analysis:
    title: str
    summary: str
    category: str = "Announcement"
    important: bool = False
    from_staff: bool = False     # instructor, TA or the course's announcement channel
    related: list[RelatedRef] = field(default_factory=list)
    tasks: list[NewTask] = field(default_factory=list)
    analyzer: str = "rules"


@dataclass
class KnownAssignment:
    name: str
    due: str | None


def email_body_text(email: Email) -> str:
    if email.text.strip():
        return email.text
    if email.html:
        return BeautifulSoup(email.html, "html.parser").get_text("\n", strip=True)
    return ""


def clean_subject(subject: str) -> str:
    """'[cs3157] [ANN] [LAB4] Lab 4 released' → 'Lab 4 released'."""
    return re.sub(r"^(\s*(\[[^\]]*\]|re:|fwd?:)\s*)+", "", subject, flags=re.I).strip() or subject


def normalize_iso(value: str, tz: ZoneInfo) -> str | None:
    """Accept 'YYYY-MM-DD' or 'YYYY-MM-DDTHH:MM[:SS][offset]'; return ISO with offset, or None."""
    value = (value or "").strip()
    if not value:
        return None
    try:
        if len(value) == 10:
            return date.fromisoformat(value).isoformat()
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return (dt if dt.tzinfo else dt.replace(tzinfo=tz)).isoformat()
    except ValueError:
        return None


# --- rule-based ---------------------------------------------------------------
SYNONYMS = {"hw": "homework", "pa": "programming assignment", "ps": "problem set", "pset": "problem set"}
# First words too general to stand for a multi-word name on their own ("problem 5" ≠ "Problem Set 5").
GENERIC_FIRST_WORDS = {"problem", "the", "final", "assignment", "course", "class"}


EXAM_WORDS = re.compile(r"\b(exam|midterm|final|quiz)\b", re.I)


def _name_pattern(name: str, aliases: tuple[tuple[str, str], ...] = ()) -> re.Pattern | None:
    m = re.match(r"^\s*([A-Za-z][A-Za-z ]*?)\s*0*(\d+)\b", name)
    if not m:
        # Unnumbered exams ("Final Exam"): the full name, or an alias for it ({ Final = "Final Exam" }).
        # Other unnumbered names are too likely to be ordinary words.
        if not EXAM_WORDS.search(name):
            return None
        target = name.strip().lower()
        words = {target} | {alias.lower() for alias, to in aliases if to.strip().lower() == target}
        alternatives = "|".join(re.escape(w).replace(r"\ ", r"\s*") for w in sorted(words))
        not_exam = r"project|paper|presentation|report|grades?|answers?|version|submission|draft"
        return re.compile(rf"\b(?:{alternatives})\b(?!\s*\d)(?!\s+(?:{not_exam})\b)", re.I)
    prefix, num = m.group(1).strip().lower(), m.group(2)
    words = {prefix} | {k for k, v in SYNONYMS.items() if v == prefix} | (
        {SYNONYMS[prefix]} if prefix in SYNONYMS else set())
    first = prefix.split()[0]
    if " " in prefix and first not in GENERIC_FIRST_WORDS:
        words.add(first)   # "Midterm Exam 1" is also "Midterm 1"; "Theoretical Assignment 3", "Theoretical 3"
    words |= {alias.lower() for alias, target in aliases if target.strip().lower() == prefix}
    alternatives = "|".join(re.escape(w).replace(r"\ ", r"\s*") for w in sorted(words))
    return re.compile(rf"\b(?:{alternatives})\s*0*{num}\b", re.I)


def match_known(text: str, known: list[KnownAssignment],
                aliases: tuple[tuple[str, str], ...] = ()) -> list[str]:
    hits = []
    for k in known:
        pattern = _name_pattern(k.name, aliases)
        if pattern and pattern.search(text):
            hits.append(k.name)
    return hits


DEFAULT_STAFF_SUBJECTS = (r"\[ann\]", r"^\s*COMS\s?W?\d{4}\b")


class RuleAnalyzer:
    """Subject-line rules, used when Claude isn't available. Which subjects count as staff posts
    comes from staff_subjects in courses/courses.toml."""

    name = "rules"

    def __init__(self, staff_subjects: tuple[str, ...] | None = None):
        if staff_subjects is None:
            from .course_list import load

            staff_subjects = load().school.staff_subjects or DEFAULT_STAFF_SUBJECTS
        self.staff = [re.compile(p, re.I) for p in staff_subjects]

    def analyze(self, course: str, known: list[KnownAssignment], email: Email, tz: ZoneInfo) -> Analysis:
        subject = email.subject
        body = email_body_text(email)
        head = f"{subject}\n{body[:600]}".lower()
        if re.search(r"\b(extend|extension|extended|postpone|pushed back|moved to|new due|deadline)\b", head):
            category = "Deadline change"
        elif re.search(r"\b(exam|midterm|final|quiz)\b", head):
            category = "Exam"
        elif re.search(r"\b(grades?|graded|scores?|regrades?)\b", head):
            category = "Grades"
        elif re.search(r"\b(released|is out|posted|assignment|lab|homework|hw|problem set)\b", head):
            category = "Assignment"
        elif re.search(r"\b(room|office hours|cancel+ed|zoom|location|recitation)\b", head):
            category = "Logistics"
        else:
            category = "Announcement"
        summary = re.sub(r"\s+", " ", body).strip()
        reply = bool(re.match(r"\s*(re|fwd?):", subject, re.I))
        # Staff posts, e.g. [ANN] list announcements or Ed announcements ("COMS W3134 002: ...").
        from_staff = not reply and any(p.search(subject) for p in self.staff)
        if not from_staff and category in ("Announcement", "Logistics", "Assignment"):
            category = "Other"
        return Analysis(
            title=clean_subject(subject)[:120],
            summary=(summary[:280] + "…") if len(summary) > 280 else summary,
            category=category,
            important=from_staff and (bool(re.search(r"\[ann\]", subject, re.I))
                                      or category in ("Deadline change", "Exam")),
            from_staff=from_staff,
            related=[RelatedRef(name) for name in match_known(subject, known)],
        )


# --- Claude -------------------------------------------------------------------
SYSTEM_PROMPT = prompts.load("email_summary.system")   # prompts/email_summary.system.md



def build_user_message(course: str, known: list[KnownAssignment], email: Email, tz: ZoneInfo,
                       today: date) -> str:
    body = email_body_text(email)
    if len(body) > MAX_BODY_CHARS:
        body = body[:MAX_BODY_CHARS] + "\n[email truncated: too long]"
    known_lines = "\n".join(f"- {k.name} | due {k.due or 'unknown'}" for k in known) or "(none)"
    attachments = ", ".join(email.attachments) or "none"
    sent = email.date.astimezone(tz).strftime("%Y-%m-%d %H:%M %Z (%a)")
    return prompts.render(   # prompts/email_summary.user.md
        "email_summary.user", course=course, today=today.isoformat(), known_assignments=known_lines,
        sender=email.sender, sender_email=email.sender_email, sent=sent, subject=email.subject,
        attachments=attachments, body=body)


class ClaudeAnalyzer:
    name = "claude"

    def __init__(self, model: str, client=None, fallback: RuleAnalyzer | None = None):
        import anthropic

        self.anthropic = anthropic
        self.client = client or anthropic.Anthropic()
        self.model = model
        self.fallback = fallback or RuleAnalyzer()
        self.failures: list[str] = []

    def analyze(self, course: str, known: list[KnownAssignment], email: Email, tz: ZoneInfo) -> Analysis:
        try:
            api, extra = messages_api(self.client, self.model)
            response = api.create(
                model=self.model,
                max_tokens=4000,
                output_config={"effort": "low", "format": {"type": "json_schema", "schema": SCHEMA}},
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": build_user_message(
                    course, known, email, tz, datetime.now(tz).date())}],
                **extra,
            )
        except self.anthropic.APIStatusError as exc:
            return self._fall_back(email, course, known, tz, f"API error {exc.status_code}")
        except self.anthropic.APIConnectionError:
            return self._fall_back(email, course, known, tz, "connection error")
        if response.stop_reason in ("refusal", "max_tokens"):
            return self._fall_back(email, course, known, tz, f"stop reason {response.stop_reason}")
        text = next((b.text for b in response.content if b.type == "text"), "")
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return self._fall_back(email, course, known, tz, "unparseable response")
        return self._validate(data, known, tz)

    def _fall_back(self, email, course, known, tz, reason: str) -> Analysis:
        log.warning("Claude analysis failed for %s (%s); using rules", email.id, reason)
        self.failures.append(reason)
        return self.fallback.analyze(course, known, email, tz)

    @staticmethod
    def _validate(data: dict, known: list[KnownAssignment], tz: ZoneInfo) -> Analysis:
        names = {k.name for k in known}
        related = [RelatedRef(r["name"], normalize_iso(r.get("due_stated", ""), tz) or "")
                   for r in data.get("related_assignments", []) if r.get("name") in names]
        tasks = [NewTask(t["name"].strip()[:120], normalize_iso(t.get("due", ""), tz) or "",
                         t.get("type") if t.get("type") in TASK_TYPES else "Action item")
                 for t in data.get("new_tasks", []) if t.get("name", "").strip()
                 and t["name"] not in names]
        return Analysis(
            title=(data.get("title") or "").strip()[:120] or "Course email",
            summary=(data.get("summary") or "").strip(),
            category=data.get("category") if data.get("category") in CATEGORIES else "Announcement",
            important=bool(data.get("important")) and bool(data.get("from_staff")),
            from_staff=bool(data.get("from_staff")),
            related=related, tasks=tasks, analyzer="claude")
