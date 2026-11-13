# Apps Script: Gmail and Google Calendar

This script runs inside the Google account that receives your course email. Every hour it:

- **Copies new course emails into Notion.** It looks at the last 30 days, using each course's *Gmail query*. Each email lands in **Updates** with its text, attachments (up to 5 MB each) and a Gmail link. The GitHub sync then adds a summary and links it to assignments.
- **Keeps a "Coursework" Google Calendar up to date.** It's created the first time. Each upcoming deadline is a 30-minute block ending at the due time, exams cover their real time, and dates without a time are all-day events. Each event reminds you 1 day and 2 hours before. Moving a due date in Notion moves the event; removing it deletes the event.

It also checks **every minute** for lectures you asked to convert with the **Convert to Markdown** button, and starts the GitHub conversion for each one.

It only reads your mail. It never sends, changes or deletes anything there.

## Install (about 5 minutes)

1. Go to <https://script.google.com>, **signed in as the account that gets your course email**, and click **New project**. Rename it "coursework_sync".
2. **Code.gs:** delete the sample code in the editor and paste in all of [`Code.gs`](Code.gs). At the top, fill in:
   - `DATA_SOURCES`: your Notion data source ids, the same ones as under `[notion]` in `courses/courses.toml`
   - `GITHUB_REPO`: your copy of this repository, e.g. `your-name/coursework_sync`
3. **appsscript.json:**
   1. Click **Project Settings** (the gear) and tick **Show "appsscript.json" manifest file in editor**.
   2. Back in **Editor**, open `appsscript.json` and replace its contents with [`appsscript.json`](appsscript.json).
4. **Tokens:** in **Project Settings → Script Properties**, click **Add script property** and add:
   - `NOTION_TOKEN`: the same Notion token you saved as the GitHub secret.
   - `GITHUB_TOKEN`: lets the convert button start the conversion on GitHub. Create it at <https://github.com/settings/personal-access-tokens/new>:
     - Choose *Only select repositories →* your copy of this repository.
     - Under Repository permissions, set **Actions** to *Read and write*.
     - Without this token, convert requests wait for the next 3-hourly sync instead.
   - Click **Save**.
5. **Run setup:** in the editor, choose `setup` in the function menu and click **Run**.
   - Google asks you to authorize. You may see "Google hasn't verified this app": click **Advanced → Go to coursework_sync**. It's your own script.
   - The Gmail permission is worded broadly ("read, compose, send, and permanently delete"). That's the only option Apps Script gives for reading mail. The script itself only searches and reads.
6. **Check the log:** it should end with `Setup done`. Your first course emails are now in Notion under **Updates** (marked "Not summarized yet"), and a **Coursework** calendar appears in Google Calendar.

The next GitHub sync (every 3 hours, or run it from the Actions tab) fills in the summaries.

## If something goes wrong

- **Column or permission errors from Notion:** the token is wrong, a `DATA_SOURCES` id is wrong, or the integration isn't connected to the page that holds your databases.
- **"This app is blocked" or "admin policy":** your school's Google Workspace doesn't allow Apps Script to read mail. Forward course emails to a personal Gmail account and install the script there.
- **To see what it did:** **Executions** (left sidebar) lists every hourly run with its log.
- **To stop it:** **Triggers** (left sidebar), then delete the triggers.

When `Code.gs` changes in this repo, paste the new version over the old one. Run `setup` again only when the change adds a new scheduled job; the commit message will say so.
