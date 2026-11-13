# TODO

## Automated setup

Setting up a copy today is a series of manual steps across five services:
- making the Notion databases and the course page template
- copying ids into `courses/courses.toml` and `apps_script/Code.gs`
- adding GitHub secrets
- pasting and authorizing the Apps Script
- listing the courses

A single guided command should do as much of this as each service allows, and walk the person through the rest:

```bash
python -m scheduleaid setup
```

### What it should automate

- **Check the tokens first.** Confirm the Canvas token, Notion token and (optional) Ed and Anthropic keys work before changing anything, and say which one fails and how to fix it.
- **Create the Notion databases** under a page the user picks: Courses, Assignments, Lecture Notes, Updates and Code, with every property, option and two-way relation in [`docs/notion-setup.md`](docs/notion-setup.md). Do this through the Notion API from one schema definition in the code, so the docs and the setup can't drift apart.
- **Write `courses/courses.toml`:**
  - the `[notion]` ids of the new databases
  - `[school]` settings (the Canvas URL, time zone and term), guessed from the Canvas account
  - a draft `[[course]]` list from the user's active Canvas courses, with codes, instructors and Canvas ids, for the user to trim
  - Ed course ids, matched by code when an Ed token is given
- **Detect course websites** linked from each Canvas course. Say which have a reader in `courses/sites/` and which would need one.
- **Set the GitHub repository secrets** (`COURSEWORKTOK`, `NOTION_TOKEN`, `ED_TOKEN`, `ANTHROPIC_API_KEY`) with the GitHub CLI when it's installed and signed in. Otherwise print exactly what to add where.
- **Produce a ready-to-paste `Code.gs`** with `DATA_SOURCES` and `GITHUB_REPO` filled in.
- **Finish with `check` and a `dry-run`**, then report what's done and what's left.
- **Be safe to run again.** A second run finds what already exists, fills only the gaps, and never duplicates databases or overwrites edits to `courses.toml`.

### What stays manual (and the setup should guide)

The setup can't do these steps; it should give clear, numbered instructions for each, at the moment they're needed:
- **Notion's API can't create database views, database templates or button properties.** That covers the **Course page** template with its filtered linked views, and the **Conversion** button on Lecture Notes. A Notion template page to duplicate could cover most of it.
- **The Apps Script** must be pasted into script.google.com, authorized by the user, and given its Script Properties.
- **Tokens** are created by the user in Canvas, Notion, Ed, Anthropic and GitHub.

### Done when

Someone who has never seen the project can go from a fork to a first successful sync by following the command's output alone, in about 15 minutes.
