// Tests for apps_script/Code.gs with in-memory stand-ins for Gmail, Calendar and Notion.
// Run: node --test tests/apps_script.test.js
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const CODE = fs.readFileSync(path.join(__dirname, '..', 'apps_script', 'Code.gs'), 'utf8');
// The Notion data source ids the tests fill into Code.gs's DATA_SOURCES.
const DS = { courses: 'ds-courses', assignments: 'ds-assignments', updates: 'ds-updates', lectures: 'ds-lectures' };
const DAY = 24 * 3600 * 1000;

// --- fake Notion behind UrlFetchApp -----------------------------------------------------------
function makeNotion() {
  let n = 0;
  const pages = {};
  const uploads = [];
  const calls = [];
  const plain = (items) => (items || []).map((i) => i.text.content).join('');
  const matches = (page, f) => {
    if (!f) return true;
    if (f.and) return f.and.every((x) => matches(page, x));
    const p = page.properties[f.property] || {};
    if (f.select) {
      const v = p.select && p.select.name;
      if ('equals' in f.select) return v === f.select.equals;
      if ('does_not_equal' in f.select) return v !== f.select.does_not_equal;
    }
    if (f.date) {
      const v = p.date && p.date.start;
      if (f.date.is_empty) return !v;
      if (f.date.on_or_after) return !!v && v.slice(0, 10) >= f.date.on_or_after;
    }
    if (f.rich_text && f.rich_text.is_not_empty) return plain(p.rich_text).length > 0;
    if (f.relation) return (p.relation || []).some((r) => r.id === f.relation.contains);
    throw new Error('unsupported filter ' + JSON.stringify(f));
  };
  const respond = (code, body) => ({ getResponseCode: () => code, getContentText: () => JSON.stringify(body) });
  const api = {
    pages, uploads, calls,
    add(ds, properties) {
      const id = `page-${++n}`;
      pages[id] = { id, url: `https://notion.so/${id}`, ds, properties: JSON.parse(JSON.stringify(properties)) };
      return pages[id];
    },
    inDs: (ds) => Object.values(pages).filter((p) => p.ds === ds),
    fetch(url, opts) {
      const route = url.replace('https://api.notion.com/v1', '');
      const method = (opts.method || 'get').toLowerCase();
      calls.push(`${method} ${route}`);
      assert.equal(opts.headers['Notion-Version'], '2025-09-03');
      assert.equal(opts.headers.Authorization, 'Bearer secret-token');
      let m;
      if (method === 'post' && (m = route.match(/^\/data_sources\/(.+)\/query$/))) {
        const body = JSON.parse(opts.payload);
        return respond(200, { results: api.inDs(m[1]).filter((p) => matches(p, body.filter)), has_more: false });
      }
      if (method === 'post' && route === '/pages') {
        const body = JSON.parse(opts.payload);
        const page = api.add(body.parent.data_source_id, body.properties);
        page.children = body.children;
        return respond(200, { id: page.id, url: page.url });
      }
      if (method === 'patch' && (m = route.match(/^\/pages\/(.+)$/))) {
        Object.assign(pages[m[1]].properties, JSON.parse(opts.payload).properties);
        return respond(200, { id: m[1] });
      }
      if (method === 'post' && route === '/file_uploads') {
        const body = JSON.parse(opts.payload);
        uploads.push({ id: `upload-${uploads.length + 1}`, ...body });
        return respond(200, { id: `upload-${uploads.length}` });
      }
      if (method === 'post' && (m = route.match(/^\/file_uploads\/(.+)\/send$/))) {
        assert.ok(opts.payload.file, 'multipart file part');
        uploads.find((u) => u.id === m[1]).sent = true;
        return respond(200, {});
      }
      throw new Error(`unexpected ${method} ${route}`);
    },
  };
  return api;
}

// --- fake Gmail / Calendar / Apps Script services --------------------------------------------------
function attachment(name, type, size) {
  return { getName: () => name, getContentType: () => type, getSize: () => size,
           copyBlob() { return { setName() { return this; } }; } };
}
function message(id, { subject, from = 'Pat Lee <plee@cs.example.edu>', date, body = '', attachments = [] }) {
  return { getId: () => id, getSubject: () => subject, getFrom: () => from, getDate: () => date,
           getPlainBody: () => body, isDraft: () => false, getAttachments: () => attachments };
}

function makeCalendar() {
  let n = 0;
  const events = {};
  const make = (title, start, end, allDay, options) => {
    const id = `evt-${++n}@google.com`;
    const ev = {
      id, title, start, end, allDay, options, reminders: [], deleted: false,
      getId: () => id, getTitle: () => ev.title, isAllDayEvent: () => ev.allDay,
      getStartTime: () => ev.start, getEndTime: () => ev.end, getAllDayStartDate: () => ev.start,
      setTitle(t) { ev.title = t; }, setTime(s, e) { ev.start = s; ev.end = e; ev.allDay = false; },
      setAllDayDate(d) { ev.start = d; ev.allDay = true; },
      removeAllReminders() { ev.reminders = []; }, addPopupReminder(m) { ev.reminders.push(m); },
      deleteEvent() { ev.deleted = true; },
    };
    events[id] = ev;
    return ev;
  };
  return {
    events,
    createEvent: (t, s, e, o) => make(t, s, e, false, o),
    createAllDayEvent: (t, d, o) => make(t, d, null, true, o),
    getEventById: (id) => (events[id] && !events[id].deleted ? events[id] : null),
  };
}

function load({ now = new Date('2026-11-02T17:00:00Z'), threads = [], token = 'secret-token',
                github = null, githubStatus = 204, activeEmail = 'ab1234@example.edu',
                effectiveEmail = 'ab1234@example.edu' } = {}) {
  const notion = makeNotion();
  const dispatches = [];
  const fetch = (url, opts) => {
    if (!url.startsWith('https://api.github.com/')) return notion.fetch(url, opts);
    assert.equal(opts.headers.Authorization, 'Bearer ' + github);
    dispatches.push({ url, body: JSON.parse(opts.payload) });
    return { getResponseCode: () => githubStatus, getContentText: () => (githubStatus < 300 ? '' : '{"message":"Bad credentials"}') };
  };
  const calendar = makeCalendar();
  const searches = [];
  const RealDate = Date;
  class FakeDate extends RealDate {
    constructor(...args) { super(...(args.length ? args : [now.getTime()])); }
    static now() { return now.getTime(); }
  }
  const pad = (x) => String(x).padStart(2, '0');
  const context = {
    console: { log() {}, warn() {} },
    Date: FakeDate,
    Session: { getActiveUser: () => ({ getEmail: () => activeEmail }),
               getEffectiveUser: () => ({ getEmail: () => effectiveEmail }) },
    PropertiesService: { getScriptProperties: () => ({
      getProperty: (k) => ({ NOTION_TOKEN: token, GITHUB_TOKEN: github })[k] || null }) },
    UrlFetchApp: { fetch },
    Utilities: {
      sleep() {},
      formatDate(date, tz, pattern) {
        // New York is UTC-5 after Nov 1 2026 and UTC-4 before: enough for these tests.
        const offset = date < new RealDate('2026-11-01T06:00:00Z') ? -4 : -5;
        const local = new RealDate(date.getTime() + offset * 3600 * 1000);
        const day = `${local.getUTCFullYear()}-${pad(local.getUTCMonth() + 1)}-${pad(local.getUTCDate())}`;
        if (pattern === 'yyyy-MM-dd') return day;
        return `${day}T${pad(local.getUTCHours())}:${pad(local.getUTCMinutes())}:${pad(local.getUTCSeconds())}-0${-offset}:00`;
      },
    },
    GmailApp: {
      search(q) {
        searches.push(q);
        return threads.filter((t) => q.includes(t.matches)).map((t) => ({ getId: () => t.id, getMessages: () => t.messages }));
      },
    },
    CalendarApp: {
      getOwnedCalendarsByName: (name) => (name === 'Coursework' ? [calendar] : []),
      createCalendar() { throw new Error('should reuse the existing calendar'); },
    },
    ScriptApp: { getProjectTriggers: () => [], deleteTrigger() {}, newTrigger() { throw new Error('not in tests'); } },
  };
  vm.createContext(context);
  vm.runInContext(CODE, context);
  vm.runInContext(`Object.assign(DATA_SOURCES, ${JSON.stringify(DS)}); GITHUB_REPO = 'me/coursework_sync';`, context);
  return { gs: context, notion, calendar, searches, dispatches };
}

function seedCourses(notion) {
  const ap = notion.add(DS.courses, { Name: { title: [{ text: { content: 'Advanced Programming' } }] },
                                      Code: { rich_text: [{ text: { content: 'COMS W3157' } }] },
                                      Status: { select: { name: 'Active' } },
                                      'Gmail query': { rich_text: [{ text: { content: 'subject:cs3157' } }] } });
  const old = notion.add(DS.courses, { Name: { title: [{ text: { content: 'Old' } }] },
                                       Status: { select: { name: 'Archived' } },
                                       'Gmail query': { rich_text: [{ text: { content: 'subject:old' } }] } });
  return { ap, old };
}

const plainOf = (items) => (items || []).map((i) => i.text.content).join('');

// --- email tests ---------------------------------------------------------------------------------
test('syncEmails copies new course emails into Updates with body, attachments and link', () => {
  const lab = message('m1', {
    subject: '[cs3157] [ANN] [LAB4] Lab 4 released',
    date: new Date('2026-10-30T14:00:00Z'),
    body: 'Hi all,\n\nLab 4 is out.\nDue Sunday.\n\n-- Pat',
    attachments: [attachment('lab4.pdf', 'application/pdf', 2000), attachment('demo.mp4', 'video/mp4', 9 * 1024 * 1024)],
  });
  const mine = message('m2', { subject: 'Re: lab', from: 'Student <AB1234@example.edu>', date: new Date('2026-10-31T14:00:00Z') });
  const old = message('m0', { subject: 'old', date: new Date('2026-09-01T14:00:00Z') });
  const { gs, notion, searches } = load({ threads: [{ id: 't1', matches: 'subject:cs3157', messages: [old, lab, mine] }] });
  seedCourses(notion);

  gs.syncEmails();

  assert.deepEqual(searches, ['(subject:cs3157) newer_than:30d -from:instructure.com']);  // archived course skipped
  const [page] = notion.inDs(DS.updates);
  assert.equal(notion.inDs(DS.updates).length, 1);  // own reply and too-old message skipped
  assert.equal(plainOf(page.properties.Name.title), '[cs3157] [ANN] [LAB4] Lab 4 released');
  assert.equal(plainOf(page.properties['Source ID'].rich_text), 'gmail:m1');
  assert.equal(page.properties.Source.select.name, 'Gmail');
  assert.equal(page.properties.Date.date.start, '2026-10-30T10:00:00-04:00');
  assert.equal(page.properties['Source URL'].url, 'https://mail.google.com/mail/?authuser=ab1234%40example.edu#all/t1');

  const headings = page.children.filter((b) => b.type === 'heading_2').map((b) => plainOf(b.heading_2.rich_text));
  assert.deepEqual(headings, ['Summary', 'Email', 'Attachments', 'Source', 'My notes']);
  const toggle = (name) => page.children.find((b) => b.type === 'heading_2' && plainOf(b.heading_2.rich_text) === name);
  assert.equal(toggle('Email').heading_2.is_toggleable, true);
  assert.equal(toggle('Source').heading_2.is_toggleable, false);
  const email = toggle('Email').heading_2.children.map((b) => plainOf(b.paragraph.rich_text));
  assert.deepEqual(email, ['From: Pat Lee <plee@cs.example.edu>', 'Hi all,', 'Lab 4 is out.\nDue Sunday.', '-- Pat']);
  const files = toggle('Attachments').heading_2.children;
  assert.equal(files[0].type, 'pdf');
  assert.equal(files[0].pdf.file_upload.id, 'upload-1');
  assert.match(plainOf(files[1].paragraph.rich_text), /demo\.mp4 \(9\.0 MB\) is over Notion's 5 MB limit/);
  assert.deepEqual(notion.uploads.map((u) => [u.filename, u.content_type, u.sent]), [['lab4.pdf', 'application/pdf', true]]);

  // second run: nothing new
  const before = notion.calls.length;
  gs.syncEmails();
  assert.equal(notion.inDs(DS.updates).length, 1);
  assert.ok(notion.calls.slice(before).every((c) => c.startsWith('post /data_sources/')));
});

test('syncEmails explains a missing token', () => {
  const { gs } = load({ token: null });
  assert.throws(() => gs.syncEmails(), /Add NOTION_TOKEN under Project Settings/);
});

test('long emails are cut at 95 paragraphs with a note', () => {
  const body = Array.from({ length: 120 }, (_, i) => `Paragraph ${i}`).join('\n\n');
  const msg = message('m9', { subject: '[cs3157] long', date: new Date('2026-11-01T14:00:00Z'), body });
  const { gs, notion } = load({ threads: [{ id: 't9', matches: 'subject:cs3157', messages: [msg] }] });
  seedCourses(notion);
  gs.syncEmails();
  const emailToggle = notion.inDs(DS.updates)[0].children[1].heading_2.children;
  assert.equal(emailToggle.length, 1 + 95 + 1);
  assert.match(plainOf(emailToggle.at(-1).paragraph.rich_text), /continues/);
});

// --- calendar tests ------------------------------------------------------------------------------
function seedAssignments(notion, courseId) {
  const a = (name, due, extra = {}) => notion.add(DS.assignments, {
    Name: { title: [{ text: { content: name } }] }, Course: { relation: [{ id: courseId }] },
    Due: { date: due }, Source: { select: { name: 'Website' } },
    'Source URL': { url: 'https://cs3157.github.io/www/2026-9/' }, ...extra });
  return {
    lab4: a('Lab 4', { start: '2026-11-08T23:59:00-05:00' }, { Type: { select: { name: 'Homework' } } }),
    exam2: a('Exam 2', { start: '2026-11-17T16:10:00-05:00', end: '2026-11-17T17:25:00-05:00' }, { Type: { select: { name: 'Exam' } } }),
    midterm: a('Midterm 2', { start: '2026-11-11' }, { Type: { select: { name: 'Exam' } } }),
    lab3: a('Lab 3', { start: '2026-10-25T23:59:00-04:00' }),           // past: no new event
  };
}

test('syncCalendar creates deadline, exam and all-day events and saves their ids', () => {
  const { gs, notion, calendar } = load();
  const { ap } = seedCourses(notion);
  const pages = seedAssignments(notion, ap.id);

  gs.syncCalendar();

  const events = Object.values(calendar.events);
  assert.equal(events.length, 3);
  const byTitle = Object.fromEntries(events.map((e) => [e.title, e]));
  const lab = byTitle['Due: Lab 4 (COMS W3157)'];
  assert.equal(lab.end.toISOString(), '2026-11-09T04:59:00.000Z');
  assert.equal(lab.start.toISOString(), '2026-11-09T04:29:00.000Z');   // 30 minutes ending at the deadline
  assert.deepEqual(lab.reminders, [1440, 120]);
  assert.match(lab.options.description, /Notion: https:\/\/notion\.so\/page-/);
  const exam = byTitle['Exam: Exam 2 (COMS W3157)'];
  assert.equal(exam.start.toISOString(), '2026-11-17T21:10:00.000Z');
  assert.equal(exam.end.toISOString(), '2026-11-17T22:25:00.000Z');
  const midterm = byTitle['Exam: Midterm 2 (COMS W3157)'];
  assert.equal(midterm.allDay, true);
  assert.deepEqual([midterm.start.getFullYear(), midterm.start.getMonth(), midterm.start.getDate()], [2026, 10, 11]);
  assert.equal(plainOf(notion.pages[pages.lab4.id].properties['Calendar event ID'].rich_text), lab.id);
  assert.equal(notion.pages[pages.lab3.id].properties['Calendar event ID'], undefined);

  // running again changes nothing
  gs.syncCalendar();
  assert.equal(Object.values(calendar.events).length, 3);
});

test('syncCalendar moves an event when the due date changes and deletes it when removed', () => {
  const { gs, notion, calendar } = load();
  const { ap } = seedCourses(notion);
  const pages = seedAssignments(notion, ap.id);
  gs.syncCalendar();
  const id = plainOf(notion.pages[pages.lab4.id].properties['Calendar event ID'].rich_text);

  notion.pages[pages.lab4.id].properties.Due = { date: { start: '2026-11-10T23:59:00-05:00' } };
  gs.syncCalendar();
  assert.equal(calendar.events[id].end.toISOString(), '2026-11-11T04:59:00.000Z');
  assert.equal(Object.values(calendar.events).length, 3);

  notion.pages[pages.lab4.id].properties.Due = { date: null };
  gs.syncCalendar();
  assert.equal(calendar.events[id].deleted, true);
  assert.deepEqual(notion.pages[pages.lab4.id].properties['Calendar event ID'].rich_text, []);
});

// --- conversion button -------------------------------------------------------------------------
function seedLecture(notion, conversion) {
  return notion.add(DS.lectures, { Name: { title: [{ text: { content: 'Lecture 14: Graphs' } }] },
                                   'Conversion status': { select: { name: conversion } } });
}

test('checkConversions starts the GitHub workflow for requested lectures', () => {
  const { gs, notion, dispatches } = load({ github: 'gh-token' });
  const requested = seedLecture(notion, 'Requested');
  const pending = seedLecture(notion, 'Pending');
  gs.checkConversions();
  assert.equal(dispatches.length, 1);
  assert.equal(dispatches[0].url, 'https://api.github.com/repos/me/coursework_sync/actions/workflows/convert.yml/dispatches');
  assert.deepEqual(dispatches[0].body, { ref: 'main', inputs: { page_id: requested.id } });
  assert.equal(notion.pages[requested.id].properties['Conversion status'].select.name, 'Converting');
  assert.equal(notion.pages[pending.id].properties['Conversion status'].select.name, 'Pending');
  gs.checkConversions();                       // already converting: not started twice
  assert.equal(dispatches.length, 1);
});

test('checkConversions puts the request back if GitHub refuses', () => {
  const { gs, notion } = load({ github: 'bad-token', githubStatus: 401 });
  const requested = seedLecture(notion, 'Requested');
  gs.checkConversions();
  assert.equal(notion.pages[requested.id].properties['Conversion status'].select.name, 'Requested');
});

test('checkConversions does nothing without a GitHub token', () => {
  const { gs, notion, dispatches } = load();
  const requested = seedLecture(notion, 'Requested');
  const before = notion.calls.length;
  gs.checkConversions();
  assert.equal(dispatches.length, 0);
  assert.equal(notion.calls.length, before);   // not even a Notion query
  assert.equal(notion.pages[requested.id].properties['Conversion status'].select.name, 'Requested');
});

test('when Google returns no address, emails are still imported', () => {
  const msg = message('m5', { subject: '[cs3157] [ANN] Lab 5 released', date: new Date('2026-11-01T14:00:00Z') });
  const { gs, notion } = load({ activeEmail: '', effectiveEmail: '',
                                threads: [{ id: 't5', matches: 'subject:cs3157', messages: [msg] }] });
  seedCourses(notion);
  gs.syncEmails();
  const pages = notion.inDs(DS.updates);
  assert.equal(pages.length, 1);
  assert.equal(pages[0].properties['Source URL'].url, 'https://mail.google.com/mail/#all/t5');
});

test('falls back to the effective user to skip your own replies', () => {
  const mine = message('m6', { subject: 'Re: lab', from: 'Me <ab1234@example.edu>', date: new Date('2026-11-01T14:00:00Z') });
  const { gs, notion } = load({ activeEmail: '', threads: [{ id: 't6', matches: 'subject:cs3157', messages: [mine] }] });
  seedCourses(notion);
  gs.syncEmails();
  assert.equal(notion.inDs(DS.updates).length, 0);
});
