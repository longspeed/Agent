import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { readFile } from 'node:fs/promises';
import path from 'node:path';
import puppeteer from 'puppeteer';

// Isolated browser fixture: no requests can reach Gmail or Supabase.
const server = createServer(async (req, res) => {
  const pathname = new URL(req.url, 'http://localhost').pathname;
  const file = pathname === '/outreach' ? 'static/outreach.html' : pathname.slice(1);
  if (!file.startsWith('static/') || file.includes('..')) return res.writeHead(404).end();
  try {
    res.setHeader('Content-Type', file.endsWith('.css') ? 'text/css' : file.endsWith('.js') ? 'text/javascript' : 'text/html');
    res.end(await readFile(path.resolve(file)));
  } catch { res.writeHead(404).end(); }
});
await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
const base = `http://127.0.0.1:${server.address().port}`;
const browser = await puppeteer.launch({ headless: true, args: ['--no-sandbox'] });
let row = {
  id: 71, actor: 'you', status: 'scheduled', action_text: 'Send the example deck',
  thread_id: 'test-thread', evidence: 'I will send the deck Friday.',
  due_at: '2026-12-12T02:00:00Z', reminder_at: '2026-12-12T02:00:00Z', timezone: 'Asia/Bangkok',
  audit_events: [{ from_status: 'detected', to_status: 'scheduled', created_at: '2026-09-05T02:00:00Z' }],
};
let mutationFails = true;
let accountFails = false;
let ledgerFails = false;
let mutations = 0;
try {
  const page = await browser.newPage();
  const pageErrors = [];
  page.on('pageerror', error => pageErrors.push(error.message));
  await page.emulateTimezone('Asia/Bangkok');
  await page.setRequestInterception(true);
  page.on('request', request => {
    const url = new URL(request.url());
    if (url.origin !== base) return request.abort();
    if (!url.pathname.startsWith('/api/')) return request.continue();
    let body = [], status = 200;
    if (url.pathname === '/api/me') {
      status = accountFails ? 503 : 200;
      body = accountFails ? { detail: 'Account storage unavailable' } : { monitoring: { status: 'healthy' }, settings: {} };
    } else if (/\/commitments\/71\/(dismiss|confirm|complete)$/.test(url.pathname)) {
      mutations++;
      if (mutationFails) { status = 503; body = { detail: 'Try again shortly' }; }
      else {
        const next = url.pathname.endsWith('/confirm') ? 'scheduled' : url.pathname.endsWith('/complete') ? 'completed' : 'dismissed';
        row.audit_events.push({ from_status: row.status, to_status: next, created_at: '2026-09-06T02:00:00Z' });
        row = { ...row, status: next };
        body = { ...row, receipt: { status: next, message: `Promise ${next}`, reminder_at: row.reminder_at, timezone: row.timezone } };
      }
    } else if (url.pathname === '/api/outreach/commitments/ledger') {
      status = ledgerFails ? 503 : 200;
      body = ledgerFails ? { detail: 'Unavailable' } : [row];
    } else if (url.pathname === '/api/outreach/commitments') {
      body = ['detected', 'due'].includes(row.status) ? [row] : [];
    } else if (url.pathname.endsWith('/status') || url.pathname.endsWith('/summary')) body = { due: 0 };
    return request.respond({ status, contentType: 'application/json', body: JSON.stringify(body) });
  });
  await page.goto(`${base}/outreach?tab=promises`, { waitUntil: 'networkidle0' });
  await page.waitForSelector('.ledger-dismiss');
  assert.equal(await page.$eval('#commitment-count-badge', el => el.textContent), '0 need review');
  await page.click('.promise-audit summary');
  assert.match(await page.$eval('.promise-audit', el => el.textContent), /detected → scheduled/);
  await page.click('.ledger-dismiss');
  await page.waitForFunction(() => document.querySelector('.ledger-action-status')?.textContent.includes('Try again'));
  assert.equal(await page.$eval('.ledger-dismiss', el => el.disabled), false);
  mutationFails = false;
  await page.click('.ledger-dismiss');
  await page.waitForFunction(() => !document.querySelector('.ledger-dismiss'));
  assert.equal(mutations, 2);
  assert.equal(row.status, 'dismissed');
  assert.match(await page.$eval('#dismissed-commitments-list', el => el.textContent), /scheduled → dismissed/);

  // Exercise a safe, fictional confirm -> due -> complete lifecycle.
  row.status = 'detected';
  await page.reload({ waitUntil: 'networkidle0' });
  await page.waitForSelector('#commitments-list .commitment-confirm', { visible: true });
  assert.equal(await page.$eval('#commitments-list .commitment-due', el => el.value), '2026-12-12T09:00');
  await page.click('#commitments-list .commitment-confirm');
  await page.waitForSelector('.ledger-dismiss');
  assert.equal(row.status, 'scheduled');
  row.status = 'due'; // Worker transition simulated, not time travel in production.
  await page.reload({ waitUntil: 'networkidle0' });
  await page.waitForSelector('#due-commitments-list .commitment-complete', { visible: true });
  // Reload restores scroll position. Move the control clear of the sticky header.
  await page.$eval('#due-commitments-list .commitment-complete', el => el.scrollIntoView({ block: 'center', behavior: 'instant' }));
  await page.locator('#due-commitments-list .commitment-complete').click();
  await page.waitForFunction(() => document.querySelector('#completed-commitments-list')?.textContent.includes('Send the example deck')).catch(async error => {
    console.error({ row, mutations, ui: await page.$eval('#tab-promises-panel', el => el.textContent) });
    throw error;
  });
  assert.equal(row.status, 'completed');

  accountFails = true;
  ledgerFails = true;
  await page.reload({ waitUntil: 'networkidle0' });
  await page.waitForFunction(() => document.querySelector('#nav-monitoring-copy')?.textContent === 'Status unavailable');
  await page.waitForFunction(() => document.querySelector('#commitment-count-badge')?.textContent === 'Unavailable');
  assert.match(await page.$eval('#scheduled-you-list', el => el.textContent), /unavailable/i);
  assert.doesNotMatch(await page.$eval('#promise-ledger-summary', el => el.textContent), /What do I owe/);
  accountFails = false;
  ledgerFails = false;
  await page.reload({ waitUntil: 'networkidle0' });
  await page.waitForFunction(() => document.querySelector('#nav-monitoring-copy')?.textContent === 'Gmail active');
  for (const width of [375, 768]) {
    await page.setViewport({ width, height: 900 });
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
  }
  assert.deepEqual(pageErrors, []);
  console.log('promise ledger: dismissal failure/retry, audit, confirm/due/complete, outage/recovery, responsive checks passed');
} finally {
  await browser.close();
  await new Promise(resolve => server.close(resolve));
}
