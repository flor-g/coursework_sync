# Courses

Everything about *which* courses coursework_sync tracks lives in this folder. To use it for another term, another school or another student, change these files; the code stays the same.

```
courses/
  courses.toml       the school's settings, the Notion ids and the list of courses (an example)
  sites/             one reader per course website
    cs3157.py        COMS W3157 Advanced Programming (cs3157.github.io)
    discrete.py      COMS W3203 Discrete Math (cs.columbia.edu/~gtchuang/discrete)
```

## courses.toml

**`[school]`** settings apply to every course:

| Setting | What it's for |
|---|---|
| `name`, `term` | `term` is set on every course in Notion (e.g. "Fall 2026") |
| `timezone` | due dates and email dates, e.g. `America/New_York` |
| `canvas_url` | the school's Canvas site. The token for it is the `COURSEWORKTOK` repository secret |
| `ed_region` | the part after `edstem.org/` in your Ed links: `us` or `au` |
| `staff_subjects` | patterns for email subjects that come from course staff, such as `[ANN]`. Used when Claude isn't available |

**`[[course]]`** blocks, one per course. Only `name` and `code` are required; the comments in the file describe the rest (`instructor`, `website`, `canvas_id`, `gmail_query`, `ed_url`, `meeting_times`, `office_hours`, `aliases`).

`aliases` matter for Ed. Clarifications are matched to assignments by name, so if a course calls its assignments something different on Ed, say so. For example, Data Structures posts "P4 - Stacks" for "Programming Assignment 4", and Discrete Math says "PSET 5" for "HW 5":

```toml
aliases = { P = "Programming Assignment", HW = "Programming Assignment", T = "Theoretical Assignment" }
```

### Exams

Most exams come in on their own: from CourseWorks when the course lists them there, and from the course website (Advanced Programming, Discrete Math). For the rest, list them at the end of the course's block, one `[[course.exams]]` each:

```toml
[[course.exams]]
name = "Final Exam"
date = "2026-12-23 12:00"     # or just "2026-12-23"
end = "15:00"                 # optional
note = "Section 001, projected by the registrar."
source = "https://…"          # optional: where the date comes from
```

Each becomes an Exam page in Assignments, so it shows in To-do and Google Calendar, and Ed clarifications about it attach to it. Exams already past are marked *Done*. Change a date here and the page and calendar event follow. If the website or CourseWorks later lists an exam with the same name, that source's page is used and this entry is ignored.

## What happens when you change it

Push the change; the next sync (every 3 hours, or run **Sync** from the Actions tab) makes Notion's **Courses** database match the file:

- **A new course** gets a Notion page and starts syncing: CourseWorks assignments, announcements, slides and code, its website if it has one, and its emails.
- **A listed course** gets the values in the file. A field you leave out is left alone in Notion, so links the sync found itself (Canvas id, Gradescope, Ed) and anything you typed into Notion stay.
- **A course you remove** is set to *Archived*. Its page, assignments and notes stay in Notion; it just stops syncing. Add it back to un-archive it.

The sync only reads Canvas, so a course that isn't on your Canvas account still gets a page, just without CourseWorks items. The sync report warns about it.

### New course pages and the course template

New course pages are made from the **Course page** template in the Courses database (Courses → ▾ next to **New**). It holds the course page layout:
- the header lines
- the Important box
- the *At a glance* table
- To-do (with its *Next 14 days* and *All pending* tabs), Assignments, Updates, Lecture notes, Files and Code

Each list is filtered to the template page itself, and Notion points that filter at the new course when a page is made from the template. You can edit the template like any page; changes apply to courses added afterwards.

The template can't contain course-specific text, so it has placeholders that the sync fills in:
- the header line "(course code · term)", filled from the course's properties
- empty cells in the *At a glance* table: Course code, Instructor, Lectures, Office hours and Links. A cell you've typed into is never changed.

If the template is missing, new course pages get the course details, the Important box and the At-a-glance table but no lists, and the sync report says so.

## Websites: courses/sites/

A course with a `website` needs a reader for that site here. A reader is a Python file with two things:

```python
MATCHES = r"cs3157\.github\.io"          # a regular expression the website URL must match

def read(html, page_url, now, tz):        # the site's HTML → SiteContent
    ...
```

`read()` returns a `scheduleaid.websites.SiteContent`:
- `items`: assignments and exams (`WebItem`)
- `lectures`: lecture notes (`WebLecture`)
- optionally `code_dirs`: example-code pages (`WebCodeDir`)

Shared helpers for dates and times (`resolve_date`, `parse_time_range`, `parse_single_time`, `at`, `squash`) are in `scheduleaid/websites.py`. Copy `discrete.py` as a starting point, and add a test with a saved copy of the page next to the ones in `tests/test_websites.py`.

A course whose website has no reader still syncs everything else; the sync report says which site it couldn't read. Courses that keep everything on Canvas don't need a website.

## Also school-specific, outside this folder

- `apps_script/Code.gs`: the `TZ` constant (time zone of calendar events).
- `prompts/email_summary.system.md`: says dates are in `America/New_York` and describes `[ANN]` mailing-list posts.
- The repository secrets: `COURSEWORKTOK` (Canvas token), `NOTION_TOKEN`, `ANTHROPIC_API_KEY`.
