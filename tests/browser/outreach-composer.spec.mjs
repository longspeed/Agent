import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { readFile } from 'node:fs/promises';
import path from 'node:path';
import process from 'node:process';
import puppeteer from 'puppeteer';

const root = process.cwd();
const staticRoot = path.join(root, 'static');
const mime = { '.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css', '.svg': 'image/svg+xml' };

const server = createServer(async (request, response) => {
  try {
    const url = new URL(request.url, 'http://localhost');
    let file;
    if (url.pathname === '/' || url.pathname === '/outreach') file = path.join(staticRoot, 'outreach.html');
    else if (url.pathname.startsWith('/static/')) file = path.join(root, url.pathname.slice(1));
    else {
      response.writeHead(404).end('not found');
      return;
    }
    const bytes = await readFile(file);
    response.writeHead(200, { 'Content-Type': mime[path.extname(file)] || 'application/octet-stream' });
    response.end(bytes);
  } catch {
    response.writeHead(404).end('not found');
  }
});

await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
const { port } = server.address();
const baseUrl = `http://127.0.0.1:${port}`;

const browser = await puppeteer.launch({ headless: true, args: ['--no-sandbox'] });
const page = await browser.newPage();
await page.evaluateOnNewDocument(() => {
  window.__SENDKEEP_TEST_POLL_TIMEOUT_MS__ = 400;
  window.__SENDKEEP_TEST_AUTOCLOSE_MS__ = 300;
});

const counters = new Map();
const jobs = new Map();
let heldRewrite = false;
let sendMode = 'success';
let releaseSend = null;

const count = (key) => {
  counters.set(key, (counters.get(key) || 0) + 1);
  return counters.get(key);
};
const json = (request, body, status = 200) => request.respond({
  status,
  contentType: 'application/json',
  body: JSON.stringify(body),
}).catch(() => {});

const campaigns = [
  { name: 'Ada', email: 'ada@example.com', company: 'Analytical', status: 'Replied', hasThread: true, trackedId: 1, replyActionable: true },
  { name: 'Grace', email: 'grace@example.com', company: 'Compiler', status: 'Replied', hasThread: true, trackedId: 2, replyActionable: true },
  { name: 'Linus', email: 'linus@example.com', company: 'Kernel', status: 'Sent', hasThread: true, trackedId: 3, replyActionable: false },
];

await page.setRequestInterception(true);
page.on('request', async (request) => {
  const url = new URL(request.url());
  if (url.origin !== baseUrl) {
    await request.abort().catch(() => {});
    return;
  }
  if (!url.pathname.startsWith('/api/')) {
    await request.continue().catch(() => {});
    return;
  }

  count(`${request.method()} ${url.pathname}`);
  if (url.pathname === '/api/outreach/campaigns') return json(request, campaigns);
  if (url.pathname === '/api/outreach/campaigns/preview') return json(request, {
    send_mode: 'manual', bounces: {}, eligible: 0, eligible_total: 0,
    sent_today: 0, daily_limit: 50, remaining_today: 50, capped: 0,
    blockers: [], deliverability: { status: 'ready', findings: [] }, auto_send_enabled: false,
  });
  if (url.pathname === '/api/outreach/drafts') return json(request, []);
  if (url.pathname === '/api/outreach/replies') return json(request, []);
  if (url.pathname === '/api/outreach/commitments') return json(request, []);
  if (url.pathname === '/api/outreach/commitments/summary') return json(request, { detected: 0, confirmed: 0, due: 0 });
  if (url.pathname === '/api/outreach/commitments/resolved') return json(request, []);
  if (url.pathname === '/api/outreach/follow-ups/status') return json(request, { due: 0, waiting: 0, queued: 0, sent: 0 });
  if (url.pathname === '/api/outreach/outcomes') return json(request, []);
  if (url.pathname === '/api/outreach/evidence') return json(request, {});
  if (url.pathname === '/api/outreach/dogfood-log') return json(request, []);
  if (url.pathname === '/api/me') return json(request, { email: 'pilot@example.com' });

  const draftMatch = url.pathname.match(/^\/api\/outreach\/threads\/(\d+)\/draft-reply$/);
  if (draftMatch && request.method() === 'POST') {
    const id = Number(draftMatch[1]);
    const jobId = `load-${id}-${Date.now()}`;
    jobs.set(jobId, {
      status: 'complete',
      result: {
        review_id: 100 + id,
        contact: { name: campaigns[id - 1].name, email: campaigns[id - 1].email },
        subject: `Re: conversation ${id}`,
        latest_message: `Latest message ${id}`,
        draft: `Original draft ${id}`,
        existing: true,
      },
    });
    return json(request, { job_id: jobId }, 202);
  }

  const rewriteMatch = url.pathname.match(/^\/api\/outreach\/replies\/(\d+)\/rewrite$/);
  if (rewriteMatch && request.method() === 'POST') {
    const jobId = `rewrite-${rewriteMatch[1]}-${Date.now()}`;
    jobs.set(jobId, { status: 'running', result: { body: `Late rewrite ${rewriteMatch[1]}` } });
    return json(request, { job_id: jobId }, 202);
  }

  const sendMatch = url.pathname.match(/^\/api\/outreach\/replies\/(\d+)\/send$/);
  if (sendMatch && request.method() === 'POST') {
    if (sendMode === 'delayed-success') {
      await new Promise((resolve) => { releaseSend = resolve; });
      return json(request, { ok: true });
    }
    if (sendMode === 'uncertain') return json(request, {
      detail: 'Gmail may have accepted this reply. Verify Gmail Sent.',
      code: 'send_uncertain', retryable: false,
    }, 502);
    if (sendMode === 'retryable') return json(request, {
      detail: 'Capacity is temporarily unavailable.',
      code: 'capacity_unavailable', retryable: true,
    }, 503);
    return json(request, { ok: true });
  }

  const jobMatch = url.pathname.match(/^\/api\/jobs\/(.+)$/);
  if (jobMatch) {
    const job = jobs.get(jobMatch[1]);
    if (!job) return json(request, { detail: 'Job not found' }, 404);
    if (jobMatch[1].startsWith('rewrite-') && !heldRewrite) job.status = 'complete';
    return json(request, job);
  }

  return json(request, {});
});

async function loadDesk() {
  await page.goto(`${baseUrl}/outreach?tab=threads`, { waitUntil: 'domcontentloaded' });
  await page.addStyleTag({ content: '.hidden{display:none!important}' });
  await page.waitForSelector('.draft-thread-reply-btn[data-tracked-id="1"]');
}

async function openComposer(id = 1) {
  await page.click(`.draft-thread-reply-btn[data-tracked-id="${id}"]`);
  await page.waitForFunction(() => {
    const send = document.getElementById('thread-reply-send');
    return document.getElementById('thread-reply-composer').open
      && send && !send.classList.contains('hidden') && !send.disabled;
  });
}

try {
  await loadDesk();

  // Regression: ISSUE-COMPOSER-001 — action buttons leaked onto reply-free threads.
  assert.equal(await page.$$eval('.draft-thread-reply-btn', (nodes) => nodes.length), 2);
  assert.equal(await page.$('.draft-thread-reply-btn[data-tracked-id="3"]'), null);

  await openComposer(1);
  heldRewrite = true;
  await page.click('#thread-reply-rewrite');
  await page.waitForFunction(() => document.getElementById('thread-reply-rewrite').disabled);
  await page.evaluate(() => document.getElementById('thread-reply-send').click());
  assert.equal(counters.get('POST /api/outreach/replies/101/send') || 0, 0, 'send must not start while rewrite is pending');

  const pollsBeforeClose = counters.get('GET /api/jobs/rewrite-101-' ) || 0;
  await page.click('[data-composer-close]');
  assert.equal(await page.$eval('#thread-reply-composer', (node) => node.open), false);
  const totalPollsBefore = [...counters.entries()].filter(([key]) => key.includes('/api/jobs/rewrite-101-')).reduce((sum, [, value]) => sum + value, 0);
  await new Promise((resolve) => setTimeout(resolve, 650));
  const totalPollsAfter = [...counters.entries()].filter(([key]) => key.includes('/api/jobs/rewrite-101-')).reduce((sum, [, value]) => sum + value, 0);
  assert.equal(totalPollsAfter, totalPollsBefore, 'closing must abort rewrite polling');
  void pollsBeforeClose;

  heldRewrite = false;
  await openComposer(2);
  assert.equal(await page.$eval('#thread-reply-draft', (node) => node.value), 'Original draft 2', 'a late rewrite must not overwrite a reopened composer');
  await page.click('[data-composer-close]');

  await openComposer(1);
  sendMode = 'delayed-success';
  const sendCallsBefore = counters.get('POST /api/outreach/replies/101/send') || 0;
  const campaignsBefore = counters.get('GET /api/outreach/campaigns') || 0;
  const repliesBefore = counters.get('GET /api/outreach/replies') || 0;
  const draftsBefore = counters.get('GET /api/outreach/drafts') || 0;
  const rewritesBeforeSend = counters.get('POST /api/outreach/replies/101/rewrite') || 0;
  await page.evaluate(() => {
    const send = document.getElementById('thread-reply-send');
    send.click();
    send.click();
  });
  await page.waitForFunction(() => document.getElementById('thread-reply-send').disabled);
  assert.equal((counters.get('POST /api/outreach/replies/101/send') || 0) - sendCallsBefore, 1, 'double click must produce one send request');
  await page.evaluate(() => document.getElementById('thread-reply-rewrite').click());
  assert.equal(counters.get('POST /api/outreach/replies/101/rewrite') || 0, rewritesBeforeSend, 'rewrite must not start while sending');
  await page.evaluate(() => document.querySelector('[data-composer-close]').click());
  assert.equal(await page.$eval('#thread-reply-composer', (node) => node.open), true, 'composer must stay open while sending');
  releaseSend();
  await page.waitForFunction(() => document.getElementById('thread-reply-status').textContent.includes('audit log'));
  await new Promise((resolve) => setTimeout(resolve, 100));
  assert.ok((counters.get('GET /api/outreach/campaigns') || 0) > campaignsBefore, 'success refreshes Threads');
  assert.ok((counters.get('GET /api/outreach/replies') || 0) > repliesBefore, 'success refreshes Replies');
  assert.ok((counters.get('GET /api/outreach/drafts') || 0) > draftsBefore, 'success refreshes Drafts');
  assert.equal(await page.$eval('#thread-reply-send', (node) => node.classList.contains('hidden')), true);
  await page.waitForFunction(() => !document.getElementById('thread-reply-composer').open);

  await openComposer(1);
  sendMode = 'retryable';
  await page.$eval('#thread-reply-draft', (node) => { node.value = 'Keep my edited reply'; });
  await page.click('#thread-reply-send');
  await page.waitForFunction(() => !document.getElementById('thread-reply-send').disabled);
  assert.equal(await page.$eval('#thread-reply-draft', (node) => node.value), 'Keep my edited reply');
  await page.click('[data-composer-close]');

  await openComposer(1);
  sendMode = 'uncertain';
  await page.click('#thread-reply-send');
  await page.waitForFunction(() => document.getElementById('thread-reply-status').textContent.includes('Verify Gmail Sent'));
  assert.equal(await page.$eval('#thread-reply-send', (node) => node.classList.contains('hidden')), true);
  assert.equal(await page.$eval('#thread-reply-draft', (node) => node.readOnly), true);
  assert.equal(await page.$eval('#thread-reply-open-gmail', (node) => !node.classList.contains('hidden')), true);
  await new Promise((resolve) => setTimeout(resolve, 500));
  assert.equal(await page.$eval('#thread-reply-send', (node) => node.classList.contains('hidden')), true, 'uncertain send must stay locked');
  await page.click('[data-composer-close]');

  await openComposer(1);
  heldRewrite = true;
  await page.click('#thread-reply-rewrite');
  await page.waitForFunction(() => document.getElementById('thread-reply-status').textContent.includes('longer than 90 seconds'));
  const timeoutPolls = [...counters.entries()].filter(([key]) => key.includes('/api/jobs/rewrite-101-')).reduce((sum, [, value]) => sum + value, 0);
  await new Promise((resolve) => setTimeout(resolve, 650));
  const timeoutPollsAfter = [...counters.entries()].filter(([key]) => key.includes('/api/jobs/rewrite-101-')).reduce((sum, [, value]) => sum + value, 0);
  assert.equal(timeoutPollsAfter, timeoutPolls, 'timed-out polling must leave no timer behind');

  console.log('outreach composer browser tests passed');
} finally {
  await browser.close();
  await new Promise((resolve) => server.close(resolve));
}
