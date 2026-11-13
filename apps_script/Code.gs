/**
 * coursework_sync: the Google side. Paste this into a Google Apps Script project
 * (script.google.com) owned by the Google account that gets your course email. See apps_script/README.md.
 *
 * Every hour it:
 *   1. syncEmails:   copies new course emails into the Notion "Updates" database: subject,
 *                    sender, date, the email text, attachments (up to 5 MB each), and a link
 *                    back to Gmail. The GitHub sync then summarizes them.
 *   2. syncCalendar: creates or moves an event in a "Coursework" Google Calendar for every
 *                    upcoming due date and exam in the Notion "Assignments" database.
 * Every minute:
 *   3. checkConversions: when you click "Convert to Markdown" on a lecture in Notion, starts the
 *                    GitHub "Convert lecture" workflow for it.
 *
 * It only reads your mail. It never sends, changes or deletes email.
 * Script Properties: NOTION_TOKEN (the same token as the GitHub secret), and GITHUB_TOKEN for
 * the convert button (a fine-grained GitHub token for your repository with Actions: read and write).
 */

const NOTION_VERSION = '2025-09-03';
const NOTION_API = 'https://api.notion.com/v1';
const DATA_SOURCES = {
  courses: '',
  assignments: '',
  updates: '',
  lectures: '',
};
let GITHUB_REPO = '';     // your copy of this repository, e.g. 'your-name/coursework_sync'
const CONVERT_WORKFLOW = 'convert.yml';
const LOOKBACK_DAYS = 30;                  // how far back to look for course emails
const EXCLUDE = '-from:instructure.com';   // CourseWorks notifications: already synced directly
const MAX_UPLOAD_BYTES = 5 * 1024 * 1024;  // Notion free plan limit per file
const MAX_ATTACHMENTS = 10;
const MAX_BODY_PARAGRAPHS = 95;            // Notion allows 100 blocks per section
const MAX_RUN_MS = 4.5 * 60 * 1000;        // stop early; Apps Script runs are capped at 6 minutes
const CALENDAR_NAME = 'Coursework';
const TZ = 'America/New_York';
const DAY_MS = 24 * 60 * 60 * 1000;

/** Run this once by hand: checks access, schedules the hourly runs, and runs both syncs. */
function setup() {
  notionToken_();
  ScriptApp.getProjectTriggers().forEach((t) => ScriptApp.deleteTrigger(t));
  ScriptApp.newTrigger('syncEmails').timeBased().everyHours(1).create();
  ScriptApp.newTrigger('syncCalendar').timeBased().everyHours(1).create();
  ScriptApp.newTrigger('checkConversions').timeBased().everyMinutes(1).create();
  syncEmails();
  syncCalendar();
  console.log('Setup done: emails and calendar sync every hour; convert requests are checked every minute.');
}

// --- emails ------------------------------------------------------------------------------------

function syncEmails() {
  const started = Date.now();
  const me = myAddress_();
  let skipped = 0;
  const cutoff = new Date(Date.now() - LOOKBACK_DAYS * DAY_MS);
  const imported = alreadyImported_(cutoff);
  let created = 0;

  for (const course of activeCourses_()) {
    const query = propText_(course, 'Gmail query').trim();
    if (!query) continue;
    const threads = GmailApp.search(`(${query}) newer_than:${LOOKBACK_DAYS}d ${EXCLUDE}`, 0, 100);
    for (const thread of threads) {
      for (const msg of thread.getMessages()) {
        if (Date.now() - started > MAX_RUN_MS) {
          console.log(`Imported ${created} email(s); stopping early, the rest come next hour.`);
          return;
        }
        const id = msg.getId();
        if (imported.has(id) || msg.isDraft() || msg.getDate() < cutoff) continue;
        if (me && msg.getFrom().toLowerCase().includes(me)) { skipped++; continue; }  // your own replies
        createUpdate_(course, thread, msg, me);
        imported.add(id);
        created++;
      }
    }
  }
  console.log(`Imported ${created} email(s)` + (skipped ? `; skipped ${skipped} sent by you.` : '.'));
}

/** Your address, used to skip your own replies. Empty if Google won't say (then nothing is skipped). */
function myAddress_() {
  const address = Session.getActiveUser().getEmail() || Session.getEffectiveUser().getEmail() || '';
  return address.toLowerCase();
}

/** Message ids already in Notion, so each email is copied once. */
function alreadyImported_(cutoff) {
  const pages = queryAll_(DATA_SOURCES.updates, {
    and: [
      { property: 'Source', select: { equals: 'Gmail' } },
      { property: 'Date', date: { on_or_after: day_(new Date(cutoff.getTime() - DAY_MS)) } },
    ],
  });
  return new Set(pages.map((p) => propText_(p, 'Source ID').replace(/^gmail:/, '')));
}

function createUpdate_(course, thread, msg, me) {
  const link = me
    ? `https://mail.google.com/mail/?authuser=${encodeURIComponent(me)}#all/${thread.getId()}`
    : `https://mail.google.com/mail/#all/${thread.getId()}`;
  const subject = msg.getSubject() || '(no subject)';
  const paragraphs = (msg.getPlainBody() || '').split(/\n\s*\n/).map((s) => s.trim()).filter(Boolean);
  const body = paragraphs.slice(0, MAX_BODY_PARAGRAPHS).map((p) => para_(chunks_(p)));
  if (paragraphs.length > MAX_BODY_PARAGRAPHS) {
    body.push(para_([text_('The email continues; open the original for the rest.', { color: 'gray' })]));
  }

  notion_('post', '/pages', {
    parent: { type: 'data_source_id', data_source_id: DATA_SOURCES.updates },
    properties: {
      Name: { title: [text_(subject.slice(0, 2000))] },
      Course: { relation: [{ id: course.id }] },
      Date: { date: { start: Utilities.formatDate(msg.getDate(), TZ, "yyyy-MM-dd'T'HH:mm:ssXXX") } },
      Source: { select: { name: 'Gmail' } },
      'Source ID': { rich_text: [text_('gmail:' + msg.getId())] },
      'Source URL': { url: link },
    },
    children: [
      heading_('Summary', [para_([text_('Not summarized yet. The next GitHub sync fills this in.', { color: 'gray' })])]),
      heading_('Email', [para_([text_('From: ' + msg.getFrom(), { color: 'gray' })]), ...body]),
      heading_('Attachments', attachmentBlocks_(msg, link)),
      heading_('Source'),
      para_([text_('Gmail · ', { color: 'gray' }), text_('Open original', { link })]),
      heading_('My notes'),
      para_([]),
    ],
  });
}

function attachmentBlocks_(msg, link) {
  const attachments = msg.getAttachments({ includeInlineImages: false });
  if (!attachments.length) return [para_([text_('No attachments.', { color: 'gray' })])];
  const blocks = attachments.slice(0, MAX_ATTACHMENTS).map((a) => {
    const name = a.getName();
    const type = a.getContentType() || 'application/octet-stream';
    const size = a.getSize();
    if (size > MAX_UPLOAD_BYTES) {
      return para_([text_(`${name} (${(size / 1048576).toFixed(1)} MB) is over Notion's 5 MB limit. `),
                    text_('Open it in Gmail', { link })]);
    }
    try {
      return fileBlock_(upload_(a.copyBlob().setName(name), name, type), name, type);
    } catch (e) {
      console.warn(`Could not upload ${name}: ${e.message}`);
      return para_([text_(`${name} could not be uploaded to Notion. `), text_('Open it in Gmail', { link })]);
    }
  });
  if (attachments.length > MAX_ATTACHMENTS) {
    blocks.push(para_([text_(`${attachments.length - MAX_ATTACHMENTS} more attachment(s) in Gmail.`, { color: 'gray' })]));
  }
  return blocks;
}

function upload_(blob, name, type) {
  const created = notion_('post', '/file_uploads', { mode: 'single_part', filename: name, content_type: type });
  const res = UrlFetchApp.fetch(`${NOTION_API}/file_uploads/${created.id}/send`, {
    method: 'post',
    headers: { Authorization: 'Bearer ' + notionToken_(), 'Notion-Version': NOTION_VERSION },
    payload: { file: blob },  // sent as multipart/form-data
    muteHttpExceptions: true,
  });
  if (res.getResponseCode() >= 400) {
    throw new Error(`Notion upload ${res.getResponseCode()}: ${res.getContentText().slice(0, 200)}`);
  }
  return created.id;
}

function fileBlock_(uploadId, name, type) {
  const kind = type === 'application/pdf' ? 'pdf' : type.startsWith('image/') ? 'image' : 'file';
  const body = { type: 'file_upload', file_upload: { id: uploadId } };
  if (kind === 'file') body.name = name; else body.caption = [text_(name)];
  return { object: 'block', type: kind, [kind]: body };
}

// --- calendar ----------------------------------------------------------------------------------

function syncCalendar() {
  const calendar = courseworkCalendar_();
  const codes = {};
  queryAll_(DATA_SOURCES.courses).forEach((c) => { codes[c.id] = propText_(c, 'Code') || propText_(c, 'Name'); });

  const upcoming = queryAll_(DATA_SOURCES.assignments,
                             { property: 'Due', date: { on_or_after: day_(new Date(Date.now() - DAY_MS)) } });
  let changed = 0;
  for (const page of upcoming) {
    if (syncEvent_(calendar, page, codes)) changed++;
  }

  // Assignments whose due date was removed: delete their event.
  const orphaned = queryAll_(DATA_SOURCES.assignments, {
    and: [{ property: 'Due', date: { is_empty: true } },
          { property: 'Calendar event ID', rich_text: { is_not_empty: true } }],
  });
  for (const page of orphaned) {
    const event = findEvent_(calendar, propText_(page, 'Calendar event ID'));
    if (event) event.deleteEvent();
    setEventId_(page.id, '');
    changed++;
  }
  console.log(`Calendar: ${changed} event(s) created, moved or removed.`);
}

/** Creates or updates the event for one assignment. Returns true if anything changed. */
function syncEvent_(calendar, page, codes) {
  const due = page.properties.Due && page.properties.Due.date;
  if (!due || !due.start) return false;
  const name = propText_(page, 'Name');
  const type = (page.properties.Type && page.properties.Type.select || {}).name;
  const courseIds = (page.properties.Course && page.properties.Course.relation) || [];
  const code = courseIds.length ? codes[courseIds[0].id] || '' : '';
  const title = `${type === 'Exam' ? 'Exam' : 'Due'}: ${name}${code ? ` (${code})` : ''}`;
  const times = eventTimes_(due);

  const existing = findEvent_(calendar, propText_(page, 'Calendar event ID'));
  if (existing) {
    let changed = false;
    if (existing.getTitle() !== title) { existing.setTitle(title); changed = true; }
    if (times.allDay) {
      if (!existing.isAllDayEvent() || day_(existing.getAllDayStartDate()) !== day_(times.start)) {
        existing.setAllDayDate(times.start); changed = true;
      }
    } else if (existing.isAllDayEvent() || existing.getStartTime().getTime() !== times.start.getTime()
               || existing.getEndTime().getTime() !== times.end.getTime()) {
      existing.setTime(times.start, times.end); changed = true;
    }
    return changed;
  }

  const source = (page.properties.Source && page.properties.Source.select || {}).name || '';
  const sourceUrl = (page.properties['Source URL'] || {}).url || '';
  const options = { description: `${code} · ${source}\n${sourceUrl}\n\nNotion: ${page.url}` };
  const event = times.allDay
    ? calendar.createAllDayEvent(title, times.start, options)
    : calendar.createEvent(title, times.start, times.end, options);
  event.removeAllReminders();
  event.addPopupReminder(24 * 60);
  event.addPopupReminder(120);
  setEventId_(page.id, event.getId());
  return true;
}

/**
 * Due dates without a time are all-day events. Exams store their end time as the end of the
 * Due date and become events spanning start to end. Other deadlines become a 30-minute block
 * that ends exactly at the deadline.
 */
function eventTimes_(due) {
  if (!due.start.includes('T')) {
    const [y, m, d] = due.start.split('-').map(Number);
    return { allDay: true, start: new Date(y, m - 1, d) };
  }
  if (due.end) return { allDay: false, start: new Date(due.start), end: new Date(due.end) };
  const end = new Date(due.start);
  return { allDay: false, start: new Date(end.getTime() - 30 * 60 * 1000), end };
}

function courseworkCalendar_() {
  const found = CalendarApp.getOwnedCalendarsByName(CALENDAR_NAME);
  return found.length ? found[0] : CalendarApp.createCalendar(CALENDAR_NAME, { timeZone: TZ });
}

function findEvent_(calendar, id) {
  if (!id) return null;
  try {
    return calendar.getEventById(id);
  } catch (e) {
    return null;
  }
}

function setEventId_(pageId, eventId) {
  notion_('patch', `/pages/${pageId}`, {
    properties: { 'Calendar event ID': { rich_text: eventId ? [text_(eventId)] : [] } },
  });
}

// --- lecture conversion button ---------------------------------------------------------------

/**
 * The Conversion button sets a lecture's Conversion status to "Requested". This marks it
 * "Converting" and starts the GitHub workflow that does the conversion. Without a GITHUB_TOKEN the
 * request simply waits for the next 3-hourly GitHub sync.
 */
function checkConversions() {
  const github = PropertiesService.getScriptProperties().getProperty('GITHUB_TOKEN');
  if (!github || !GITHUB_REPO) return;   // without both, requests wait for the 3-hourly sync
  const requested = queryAll_(DATA_SOURCES.lectures, { property: 'Conversion status', select: { equals: 'Requested' } });
  for (const page of requested) {
    setConversion_(page.id, 'Converting');
    const res = UrlFetchApp.fetch(
      `https://api.github.com/repos/${GITHUB_REPO}/actions/workflows/${CONVERT_WORKFLOW}/dispatches`, {
        method: 'post',
        contentType: 'application/json',
        headers: { Authorization: 'Bearer ' + github, Accept: 'application/vnd.github+json',
                   'X-GitHub-Api-Version': '2022-11-28' },
        payload: JSON.stringify({ ref: 'main', inputs: { page_id: page.id } }),
        muteHttpExceptions: true,
      });
    if (res.getResponseCode() >= 300) {
      console.warn(`GitHub refused to start the conversion (${res.getResponseCode()}): ${res.getContentText().slice(0, 200)}`);
      setConversion_(page.id, 'Requested');  // try again next minute
    } else {
      console.log(`Started conversion of ${propText_(page, 'Name')}.`);
    }
  }
}

function setConversion_(pageId, value) {
  notion_('patch', `/pages/${pageId}`, { properties: { 'Conversion status': { select: { name: value } } } });
}

// --- Notion helpers ----------------------------------------------------------------------------

function notionToken_() {
  const token = PropertiesService.getScriptProperties().getProperty('NOTION_TOKEN');
  if (!token) throw new Error('Add NOTION_TOKEN under Project Settings → Script Properties first.');
  if (!DATA_SOURCES.courses || !DATA_SOURCES.assignments || !DATA_SOURCES.updates || !DATA_SOURCES.lectures) {
    throw new Error('Fill in DATA_SOURCES at the top of Code.gs with your Notion data source ids (docs/notion-setup.md).');
  }
  return token;
}

function notion_(method, path, body) {
  for (let attempt = 0; attempt < 5; attempt++) {
    const options = {
      method,
      headers: { Authorization: 'Bearer ' + notionToken_(), 'Notion-Version': NOTION_VERSION },
      muteHttpExceptions: true,
    };
    if (body) {
      options.contentType = 'application/json';
      options.payload = JSON.stringify(body);
    }
    const res = UrlFetchApp.fetch(NOTION_API + path, options);
    const code = res.getResponseCode();
    if (code === 429 || code >= 500) {
      Utilities.sleep(1000 * Math.pow(2, attempt));
      continue;
    }
    if (code >= 400) {
      throw new Error(`Notion ${code} on ${method.toUpperCase()} ${path}: ${res.getContentText().slice(0, 300)}`);
    }
    const textBody = res.getContentText();
    return textBody ? JSON.parse(textBody) : {};
  }
  throw new Error(`Notion kept failing on ${path}`);
}

function queryAll_(dataSourceId, filter) {
  const results = [];
  let cursor = null;
  do {
    const body = { page_size: 100 };
    if (filter) body.filter = filter;
    if (cursor) body.start_cursor = cursor;
    const res = notion_('post', `/data_sources/${dataSourceId}/query`, body);
    results.push(...res.results);
    cursor = res.has_more ? res.next_cursor : null;
  } while (cursor);
  return results;
}

function activeCourses_() {
  return queryAll_(DATA_SOURCES.courses).filter((c) => {
    const status = c.properties.Status && c.properties.Status.select;
    return !(status && status.name === 'Archived');
  });
}

function propText_(page, name) {
  const prop = page.properties[name] || {};
  return (prop.title || prop.rich_text || [])
    .map((i) => i.plain_text || (i.text && i.text.content) || '').join('');
}

function text_(content, opts) {
  const item = { type: 'text', text: { content } };
  if (opts && opts.link) item.text.link = { url: opts.link };
  if (opts && opts.color) item.annotations = { color: opts.color };
  return item;
}

function chunks_(s) {
  const items = [];
  for (let i = 0; i < s.length && items.length < 100; i += 2000) items.push(text_(s.slice(i, i + 2000)));
  return items;
}

function para_(items) {
  return { object: 'block', type: 'paragraph', paragraph: { rich_text: items } };
}

/** A section heading; with children it is a collapsible (toggle) heading. */
function heading_(title, children) {
  const block = { object: 'block', type: 'heading_2', heading_2: { rich_text: [text_(title)], is_toggleable: !!children } };
  if (children) block.heading_2.children = children;
  return block;
}

function day_(date) {
  return Utilities.formatDate(date, TZ, 'yyyy-MM-dd');
}
