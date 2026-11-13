# coursework_sync

Collects a student's coursework from Canvas, course websites, Ed Discussion and Gmail, and organizes it in Notion and Google Calendar:
- assignments and exams, with their instructions and files, on a to-do list and a calendar
- announcements and course emails, summarized by Claude
- lecture slides, rewritten as readable lecture notes on request
- course code in its own database
- homework and exam clarifications from Ed on each assignment's page

It was built for one student's Columbia courses (Columbia's Canvas is called **CourseWorks**, which is why the code uses that name) and works with any Canvas school. Your school, courses and Notion ids go in [`courses/`](courses/README.md).

Two parts work together, with Notion in the middle:
- **GitHub Actions** (this repo's Python code) syncs CourseWorks and course websites into Notion, and summarizes emails.
- **An Apps Script** in your Google account (`apps_script/`) copies course emails into Notion and keeps a Google Calendar in step with Notion's due dates. No Google Cloud project is needed.

## Your courses

The courses coursework_sync tracks, and the school's settings, are in **`courses/courses.toml`**. Each sync starts by making Notion's **Courses** database match it: new courses get a page, listed courses get the file's values, and removed courses are archived. Course website readers are in `courses/sites/`, one file per site. To adapt coursework_sync to other courses, edit that folder; see [`courses/README.md`](courses/README.md).

## What the CourseWorks sync does

Runs every 3 hours on GitHub Actions. For each active course in the Notion **Courses** database it:

1. **Links the course.** It finds the matching CourseWorks course by its code (e.g. `CS 3134`) and fills in *CourseWorks ID* and *CourseWorks URL*. If it picks the wrong one, set `canvas_id` for the course in `courses/courses.toml`.
2. **Imports assignments** into **Assignments**:
   - Copies the name, due date (Eastern time), points and a link to the original.
   - Converts the instructions into Notion text inside a collapsible *Instructions* section, and uploads the files the description links to (specs, starter code) under *Files* there. PDFs show as inline viewers. Files over 5 MB, or ones your account can't download, stay as links.
   - Adds a *Source* section and an empty *My notes* section. When instructions change on CourseWorks, only the *Instructions* section is replaced; your notes are left alone.
   - Updates the status from your submission: *Not started* → *Submitted* (*Done* if excused). It never moves a status backwards, so anything you set yourself stays.
   - On the very first import, unsubmitted assignments that were due more than 14 days earlier are marked *Done* so the to-do list starts clean. Assignments Canvas flags as missing stay *Not started*.
3. **Logs deadline changes.** When a due date moves, it adds an *Important* entry to **Updates** (category *Deadline change*), linked to the assignment.
4. **Imports announcements** from the last 120 days into **Updates**.
5. **Imports lecture slides** into **Lecture Notes** and **source code** into **Code** (see *Course code* below). It looks in two places:
   - the Files area, when students can open it: slides directly inside a `Lecture N` folder become *Lecture N*, and code anywhere becomes Code pages. Intro to Java works this way.
   - the files the syllabus links, row by row: slide decks and PDFs become lectures dated by their syllabus row, and code files become Code pages grouped by week. Solutions, exercises, worksheets, annotations and installation guides are left out. Data Structures works this way, because its Files area is closed to students.
   Files that aren't published yet are picked up once they are. When the professor uploads a new version of a file, CourseWorks gives it a new id. The existing lecture or code page then switches to the new file instead of a second page appearing. A lecture already converted goes back to *Pending*, with a red note that the notes are from the earlier version.

Calendar events come from the Apps Script, which reads due dates from Notion; see `apps_script/README.md`.
   Gradebook-only entries that CourseWorks lists as assignments (scores, class rank, late days, attendance, undated items with no online submission) are skipped.

Every Notion entry stores a *Source ID* (e.g. `canvas:assignment:12345`), so re-running the sync never creates duplicates.

## What the website sync does

For each course with a `website` in `courses/courses.toml`, the reader for that site in `courses/sites/` collects:

- **Assignments** and **exams** into **Assignments**. Handouts (e.g. problem-set PDFs) are uploaded into the assignment's *Instructions*, and solutions stay as links. An exam's end time is stored as the end of its *Due* date, so its calendar event covers the whole exam. Due dates without a time are set to 11:59 PM, and the page says so.
- **Lecture notes**, into **Lecture Notes**, with links to the PDFs and slides. Their *Conversion status* starts as *Pending*; see *Converting slides to lecture notes* below. Example code from `code/NN/` pages goes into **Code**.

Readers exist for:
- COMS 3157, `cs3157.github.io`
- COMS 3203, `cs.columbia.edu/~gtchuang/discrete`

Each is one file in `courses/sites/`; add a file there for a new site (see `courses/README.md`).

Websites can't tell whether you've submitted something. Items already past due at first import are marked *Done*, and after that the status is yours to change. The sync never overwrites it, except to mark an exam *Done* once it has happened. Moved due dates are logged under **Updates** the same way as CourseWorks changes.

## What the Gmail sync does

**Step 1, the Apps Script (every hour).** For each course with a *Gmail query* in Notion (e.g. `subject:cs3157`), it copies emails from the last 30 days into **Updates**:
- the subject, date and a Gmail link
- the email text, in a collapsible *Email* section
- attachments, uploaded in an *Attachments* section; files over 5 MB (the Notion free-plan limit) link to Gmail instead

CourseWorks notification emails and your own replies are skipped.

**Step 2, the GitHub sync (every 3 hours).** For each email not yet summarized, it fills in:
- **A title, summary, category** (*Assignment*, *Deadline change*, *Exam*, *Logistics*, *Grades*, *Announcement*, *Other*) and an **Important** flag.
- **Links to the assignments it's about**, e.g. a `[cs3157] [ANN] [LAB4]` email links to *Lab 4*. Each assignment then shows its emails under *Related updates*.
- **Flagged date conflicts.** If the email gives a different due date than Notion has, the update is marked *Important* with a red note. Emails never overwrite due dates.
- **New to-dos.** Work a staff email asks for that isn't tracked yet (a form, a reading) becomes a new assignment with source *Gmail*. An undated to-do from an email more than a week old is filed as *Done*.
- **Staff only.** Only emails from course staff can be *Important*, flag a date conflict, create to-dos or be copied into an assignment. Digests, student threads, ads and receipts are filed as *Other*, which course pages hide.
- **Gradescope receipts.** "Successfully submitted to X" emails mark assignment X *Submitted*.
- **Assignment emails go into the assignment.** An email about an assignment, like `[cs3157] [ANN] [LAB4] Lab 4 released`, is copied with its attachments into that assignment's *Instructions*, under "From email: …". Later refreshes from CourseWorks or the website leave these email sections alone.

**How emails are read:** with `ANTHROPIC_API_KEY` set, Claude reads each email and returns those fields in a fixed JSON format. It uses `claude-haiku-5-5` at low effort; change it with an `EMAIL_MODEL` repository variable (Settings → Secrets and variables → Actions → Variables). Without the key, or when a call fails, subject-line rules are used. The rules give simpler titles and summaries and don't create new to-dos. Each email is summarized once.

## What the Ed sync does

Every sync, for each course on Ed Discussion, it reads the threads from the last 120 days and matches them to the course's assignments and exams. It matches on the thread's Ed category, subcategory and title: "HW3", "Homework 3", "hw 3 part b", "Midterm 1" for "Midterm Exam 1", "pset 5" and so on.

A matched thread counts as a **clarification** when course staff wrote it, or when staff answered or commented on it, or endorsed an answer. It goes into a **Clarifications from Ed** section on that assignment's page, just below *Instructions*:
- newest thread first
- the post or question, then the staff replies
- a link to the thread on Ed

Discussion among students only is left out. A page is rewritten only when one of its threads changes on Ed, so your own notes elsewhere on the page are never touched.

The course's Ed id is found by matching its code against your Ed courses, then saved in *Ed ID*; set *Ed ID* yourself if it picks the wrong one. A course that isn't on Ed is skipped.

Ed has no official API. The sync reads the one Ed's own web app uses, with your personal Ed token.

## Course pages

Each course in **Courses** has its own page, laid out like the course page template:
- the course code, section and term, with the instructor and lecture times
- an **Important** box
- **At a glance**: instructor, lectures, office hours, exams, grading and links. Edit this table directly.
- **To-do**, **Assignments**, **Updates**, **Lecture notes**, **Files** and **Code**, each filtered to the course

The *At a glance* table fills in from the course's details. Two rows are kept current by the sync. **Exams** lists every exam page of the course with its date and time, each name linking to its page. **Links** is refreshed while it holds only the sync's own links (Website, CourseWorks, Gradescope, Ed). Rows you've typed into are left alone.

**To-do** has two tabs: **Next 14 days** (the default) and **All pending**. Both hide anything *Done* or *Submitted* the moment you change its status. The 14-day window comes from a *Due in 14 days* checkbox that each sync updates.

Every sync refreshes the Important box: important updates from the last two weeks and exams in the next ten days, with deadline changes and exams in red.

## Converting slides to lecture notes

Conversion only happens when you ask for it.
1. On a lecture in **Lecture Notes** (or a course page's *Lecture notes*), click the **Conversion** button. *Conversion status* changes to *Requested*.
2. Within a minute the Apps Script starts the **Convert lecture** workflow on GitHub, and *Conversion status* shows *Converting*.
3. A few minutes later the notes appear in the lecture page's *Notes* section and *Conversion status* shows *Converted*.

The original stays linked under *Files* and *Source*.

**What you get:** Claude rewrites the slides as written lecture notes, not a slide-by-slide copy. The notes have:
- a title, course and instructor
- numbered sections and subsections ("1 Exponents", "2.1 Proof of the Product Rule")
- full sentences in place of bullet fragments
- proofs as numbered steps, each with a "Reason:" line
- code listings followed by an explanation
- warnings in *Note* boxes
- diagrams of data structures redrawn as text or tables

Nothing the slides teach is dropped, and no new topics are added. This uses `claude-opus-5-5`; change it with `CONVERT_MODEL`.

**File types:** PDFs go straight to Claude. PowerPoint (`.ppt`, `.pptx`) and Word files are turned into PDFs with LibreOffice first; the Convert workflow installs it. If the 3-hourly sync finds a requested PowerPoint, it hands it back (*Requested*) for the Convert workflow.

If a conversion is cut off or fails, *Conversion status* shows *Needs review* with the reason in *Notes*. Click the button again to retry.

## Course code

The **Code** database has one page per source file: `.java`, `.c`, `.h`, `.py`, Makefiles and so on, including the files inside zips. Each page has:
- **Code**: the file in a code block, followed by a copy of the file uploaded as `.txt`. Notion's upload refuses `.java`, `.c` and similar files, so the copy is plain text; rename it after downloading if you need the original extension.
- *Group*: the lecture or week it belongs to (`Lecture 03`, `Week 04 · Sep 28: Array Lists…`, `Lecture notes 02: Separate Compilation and Make`).
- *Lecture*: a link to the lecture it goes with, when known.
- *Source*: a link to the original.

Sources:
- Advanced Programming: the website's `code/NN/` pages, which go with lecture notes NN.
- Intro to Java: CourseWorks lecture folders. The same file uploaded for each section is kept once; a file that differs between sections gets the section in its name.
- Data Structures: the code the syllabus links.

A file is downloaded again only when it changes.

**Known limit:** assignments submitted on Gradescope don't show as submitted on CourseWorks until grades are posted. Mark those *Submitted* yourself.

## Setup

### 1. GitHub repository secrets

Add these under Settings → Secrets and variables → Actions:

| Secret | Required | Where to get it |
|---|---|---|
| `COURSEWORKTOK` | yes | Canvas → Account → Settings → *New access token* |
| `NOTION_TOKEN` | yes | See step 2 |
| `ED_TOKEN` | optional | edstem.org → your avatar → *Settings* → *API Tokens* → *Create token*; lets the sync read Ed Discussion |
| `ANTHROPIC_API_KEY` | optional | <https://platform.claude.com/settings/keys>; Claude reads emails and converts lecture PDFs |

### 2. Notion integration

1. Go to <https://www.notion.so/profile/integrations> and click **New integration**.
   - Type: *Internal*.
   - Capabilities: read content, update content, insert content.
2. Copy the *Internal Integration Secret* into the `NOTION_TOKEN` secret.
3. Create the databases and the course page template, connect the integration, and put the databases' ids under `[notion]` in `courses/courses.toml`: [`docs/notion-setup.md`](docs/notion-setup.md) has the details.

### 3. Your courses

Replace the example in `courses/courses.toml` with your school and courses ([`courses/README.md`](courses/README.md)).

### 4. Gmail and Google Calendar: Apps Script

Paste two files into script.google.com and run `setup` once. Step-by-step instructions: [`apps_script/README.md`](apps_script/README.md).

## Running it

From the **Actions** tab, open **Sync** and click **Run workflow**. Pick a mode:

- **preview**: lists your CourseWorks courses and upcoming assignments. It only needs `COURSEWORKTOK`, so it's a good first check.
- **dry-run**: reads everything and logs what it would change, without writing anything.
- **sync**: the real sync of the course list, CourseWorks, the course websites, and email summaries. This also runs automatically every 3 hours.

Each run's summary appears on the run page.

To run it on your own computer:

```bash
pip install -r requirements.txt -r requirements-dev.txt
```
```bash
python -m pytest
```
```bash
node --test tests/apps_script.test.js
```
```bash
CANVAS_TOKEN=... NOTION_TOKEN=... python -m scheduleaid sync --dry-run
```

## Layout

```
scheduleaid/
  canvas.py            CourseWorks (Canvas) API client, read-only
  notion.py            Notion API client + property helpers
  blocks.py            CourseWorks HTML → Notion blocks
  course_list.py       reads courses/ and keeps Notion's Courses database in step with it
  websites.py          what a website reader returns, shared parsing helpers, reader lookup
  sync_common.py       shared writes: assignments, deadline changes, lecture notes, calendar
  sync_courseworks.py  CourseWorks sync
  sync_websites.py     course website sync
  email_analysis.py    reads an email: Claude (structured JSON) or subject-line rules
  convert.py           on-demand slides → lecture notes (Claude; LibreOffice for PowerPoint/Word)
  materials.py         finds slides and code in CourseWorks folders and syllabus links
  markdown.py          Markdown → Notion blocks
  prompts.py           loads the prompts in prompts/
  course_pages.py      keeps each course page's Important box current
  claude.py            shared Claude settings (refusal fallback where supported)
  sync_gmail.py        summarizes the emails the Apps Script copied into Notion
  ed.py                Ed Discussion client (read-only) and Ed's post format → Notion blocks
  sync_ed.py           Ed clarifications → the assignment and exam pages they're about
  __main__.py          command line: sync | courses | courseworks | websites | gmail | ed | convert | preview
courses/               the school, the course list and the website readers (see courses/README.md)
prompts/               every prompt sent to Claude, one file each (see prompts/README.md)
apps_script/           Google side: Gmail → Notion, Notion → Google Calendar
tests/                 unit tests with in-memory fakes
```

## License

MIT; see [LICENSE](LICENSE).
