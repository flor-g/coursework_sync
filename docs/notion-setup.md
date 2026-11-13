# Setting up Notion

coursework_sync writes to six Notion databases. Make them once, on any page you like. Then put their ids under `[notion]` in `courses/courses.toml` (or in the Apps Script's `DATA_SOURCES`, for the Google side).

Property names must match exactly. Types are Notion's.

## 1. Create an integration

1. Go to <https://www.notion.so/profile/integrations> → **New integration** (internal).
2. Copy its secret. That's the `NOTION_TOKEN` repository secret, and the `NOTION_TOKEN` Script Property in the Apps Script.
3. On the page that holds the databases, open **⋯ → Connections** and add the integration.

## 2. The databases

### Courses
One page per course. The sync fills these from `courses/courses.toml` and from CourseWorks and Ed.

| Property | Type | Notes |
|---|---|---|
| Name | Title | |
| Code | Text | e.g. `CS 3134` |
| Status | Select | `Active`, `Archived` |
| Term | Select | |
| Instructor, Meeting times, Office hours | Text | |
| Website, CourseWorks URL, Gradescope URL, Ed URL | URL | |
| CourseWorks ID, Ed ID, Gmail query | Text | |
| Assignments, Lecture Notes, Updates | Relation | two-way, to the databases below (the other side is each one's **Course**) |

### Assignments
Homework, projects, exams and to-dos. Also the To-do list.

| Property | Type | Notes |
|---|---|---|
| Name | Title | |
| Course | Relation | to Courses (two-way) |
| Due | Date | an exam's end time is the date's end |
| Status | Status | `Not started` (To-do group), `In progress` (In progress), `Submitted` and `Done` (Complete) |
| Type | Select | `Homework`, `Project`, `Exam`, `Quiz`, `Reading`, `Action item` |
| Points | Number | |
| Important, Due in 14 days | Checkbox | |
| Source | Select | `CourseWorks`, `Website`, `Gmail`, `Ed`, `Course list`, `Manual` |
| Source URL | URL | |
| Source ID, Content hash, Ed hash, Calendar event ID | Text | used by the sync; hide them in the page layout |
| Last synced | Date | |

### Lecture Notes

| Property | Type | Notes |
|---|---|---|
| Name | Title | |
| Course | Relation | to Courses (two-way) |
| Lecture # | Number | |
| Date | Date | |
| Format | Select | `PDF`, `Slides`, `Other` |
| Conversion status | Select | `Pending`, `Requested`, `Converting`, `Converted`, `Needs review` |
| Conversion | Button | action: **Edit property → Conversion status → Requested** |
| Original URL | URL | |
| Original file | Files & media | optional |
| Source, Source URL, Source ID, Content hash, Last synced | as in Assignments | |

### Updates
Announcements, emails and deadline changes.

| Property | Type | Notes |
|---|---|---|
| Name | Title | |
| Course | Relation | to Courses (two-way) |
| Date | Date | |
| Category | Select | `Assignment`, `Deadline change`, `Exam`, `Logistics`, `Announcement`, `Grades`, `Other` (the list in `prompts/email_summary.schema.json`) |
| Important | Checkbox | |
| Related assignment | Relation | to Assignments |
| Source, Source URL, Source ID, Content hash, Last synced | as in Assignments | |

### Code

| Property | Type | Notes |
|---|---|---|
| Name | Title | |
| Course | Relation | to Courses |
| Lecture | Relation | to Lecture Notes |
| Group | Text | |
| Language | Select | |
| Date | Date | |
| Source, Source URL, Source ID, Content hash, Last synced | as in Assignments | |

### Files (optional)
A place for files you add yourself: Name (title), Course (relation), File (files & media), Type (select). The sync puts instruction files into the assignment pages instead.

## 3. The course page template

Course pages show their To-do, Assignments, Updates, Lecture notes and Code as linked views filtered to the course. Notion's API can't create views, so make a **database template** once:

1. In Courses, choose **▾** next to **New** → **+ New template**, and name it **Course page**.
2. Add, in this order:
   - a gray line with the text `(course code · term)`, which the sync replaces
   - a callout whose text is exactly **Important**
   - a heading **At a glance**, then a two-column table with rows Course code, Instructor, Lectures, Office hours, Exams, Grading and Links
   - a heading and a linked view for each list
3. Filter each linked view by **Course → contains → Course page** (the template itself). Notion re-points that filter at each new course page.

Each sync adds the courses in `courses/courses.toml` from this template and keeps the header, the Important box and the At-a-glance table filled in.

## 4. Copy the ids

For each database, open it as a full page → **⋯ → Copy link**. The 32-character id in the link is the database id. The sync needs the **data source** id: in Notion, open the database's **⋯ → Manage data sources → Copy data source ID**. Put them in `courses/courses.toml`:

```toml
[notion]
courses = "…"
assignments = "…"
lectures = "…"
updates = "…"
code = "…"
```
