# Prompts

Every prompt the workflow sends to Claude. The code reads these files at run time, so editing one changes the next run; no code change is needed.

| File | Used for | Model | Sent by |
|---|---|---|---|
| `email_summary.system.md` | Instructions for reading one course email: title, summary, category, staff or not, importance, related assignments, new to-dos | `claude-haiku-5-5` (`EMAIL_MODEL`) | `scheduleaid/email_analysis.py`, every sync, once per new email |
| `email_summary.user.md` | The email itself, with the course and its known assignments | same | same |
| `email_summary.schema.json` | The JSON format the email reply must follow (sent as structured output) | same | same |
| `lecture_notes.system.md` | Instructions for rewriting slides as lecture notes | `claude-opus-5-5` (`CONVERT_MODEL`) | `scheduleaid/convert.py`, when you click Conversion |
| `lecture_notes.user.md` | The request that goes with the PDF | same | same |

## Editing

- **System prompts** (`*.system.md`) are sent exactly as written.
- **User templates** (`*.user.md`) have `{fields}` that are filled in for each email or lecture. Keep the field names; you can move them, add text around them, or drop ones you don't want.
  - `email_summary.user.md`: `{course}`, `{today}`, `{known_assignments}`, `{sender}`, `{sender_email}`, `{sent}`, `{subject}`, `{attachments}`, `{body}`
  - `lecture_notes.user.md`: `{lecture}` (the lecture name, with the course and instructor)
- A literal `{` or `}` in a user template must be doubled: `{{` or `}}`. System prompts need no escaping.
- **Reply schema** (`email_summary.schema.json`): Claude's email reply must match it exactly. The field descriptions live in `email_summary.system.md`, so when you add, rename or remove a field, change both files.
  - The `category` list is the set of email categories, and the `new_tasks` → `type` list is the set of to-do types. The code reads both lists from this file. A new category also needs to exist as an option of *Category* in the Notion **Updates** database (Notion adds it automatically the first time it's used).
  - Renaming or removing a field the code reads (`title`, `summary`, `category`, `important`, `from_staff`, `related_assignments`, `new_tasks`) needs a code change in `scheduleaid/email_analysis.py`.

`python -m pytest tests/test_prompts.py` checks that every prompt loads, every template has its fields, and the schema is valid.
