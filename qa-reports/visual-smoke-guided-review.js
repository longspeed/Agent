const puppeteer = require('puppeteer');
const assert = require('assert/strict');
const fs = require('fs');
const http = require('http');
const path = require('path');

(async () => {
  const root = path.resolve(__dirname, '..');
  const mockPayload = (url) => {
    if (url === '/api/outreach/replies') return [
      { id: 1, kind: 'reply', status: 'ready', name: 'Maya Chen', email: 'maya@example.com', customer_reply: 'Thanks, this makes sense. Can you send the revised scope by Thursday?', draft_reply: 'Hi Maya,\n\nAbsolutely. I will send the revised scope by Thursday morning.\n\nBest,\nNam' },
      { id: 2, kind: 'follow_up', status: 'ready', name: 'Priya Shah', email: 'priya@example.com', draft_reply: 'Hi Priya, just following up on the proposal. Would next week be a better time to reconnect?' },
      { id: 4, kind: 'reply', status: 'send_uncertain', name: 'Owen Brooks', email: 'owen@example.com', customer_reply: 'Can you send that again?', draft_reply: 'The result of the previous send is unknown.' },
    ];
    if (url === '/api/outreach/commitments') return [
      { id: 3, status: 'detected', actor: 'operator', contact_name: 'Jon Bell', contact_email: 'jon@example.com', action_text: 'Send the calendar invite', evidence: 'I will send the calendar invite this afternoon.', due_at: new Date(Date.now() + 86400000).toISOString() },
    ];
    if (url.startsWith('/api/outreach/commitments/summary')) return { open_count: 1, overdue_count: 0 };
    if (url.startsWith('/api/outreach/commitments/resolved')) return [];
    if (url.startsWith('/api/outreach/follow-ups/status')) return { due: 1 };
    if (url === '/api/outreach/campaigns') return [];
    if (url === '/api/me') return { settings: {}, monitoring: { status: 'healthy' } };
    if (url.includes('/api/outreach/monitoring')) return { status: 'healthy' };
    return [];
  };
  const server = http.createServer((request, response) => {
    const url = new URL(request.url, 'http://127.0.0.1').pathname;
    if (url === '/outreach') {
      response.writeHead(200, { 'Content-Type': 'text/html' });
      return response.end(fs.readFileSync(path.join(root, 'static', 'outreach.html')));
    }
    if (url === '/static/nav.js') {
      response.writeHead(200, { 'Content-Type': 'application/javascript' });
      return response.end(fs.readFileSync(path.join(root, 'static', 'nav.js')));
    }
    if (url === '/static/outreach-desk.js') {
      response.writeHead(200, { 'Content-Type': 'application/javascript' });
      return response.end(fs.readFileSync(path.join(root, 'static', 'outreach-desk.js')));
    }
    if (url === '/static/outreach-desk-renderers.js') {
      response.writeHead(200, { 'Content-Type': 'application/javascript' });
      return response.end(fs.readFileSync(path.join(root, 'static', 'outreach-desk-renderers.js')));
    }
    if (url === '/favicon.ico') {
      response.writeHead(204);
      return response.end();
    }
    if (url.startsWith('/api/')) {
      response.writeHead(200, { 'Content-Type': 'application/json' });
      return response.end(JSON.stringify(mockPayload(url)));
    }
    response.writeHead(404);
    response.end();
  });
  await new Promise((resolve) => server.listen(41733, '127.0.0.1', resolve));
  const browser = await puppeteer.launch({
    headless: true,
    executablePath: 'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe',
    args: ['--no-sandbox'],
  });
  const page = await browser.newPage();
  await page.setRequestInterception(true);
  page.on('request', (request) => {
    const requestUrl = request.url();
    if (requestUrl.startsWith('https://cdn.tailwindcss.com') || requestUrl.startsWith('https://cdn.jsdelivr.net')) {
      request.respond({ status: 200, contentType: 'application/javascript', body: '' });
      return;
    }
    if (requestUrl.startsWith('https://fonts.googleapis.com') || requestUrl.startsWith('https://fonts.gstatic.com')) {
      request.respond({ status: 200, contentType: 'text/css', body: '' });
      return;
    }
    request.continue();
  });
  await page.setViewport({ width: 1440, height: 900, deviceScaleFactor: 1 });
  const errors = [];
  page.on('pageerror', (error) => errors.push(error.message));
  page.on('console', (message) => {
    if (message.type() === 'error') errors.push(message.text());
  });
  await page.goto('http://127.0.0.1:41733/outreach?tab=inbox', { waitUntil: 'networkidle2' });
  await page.waitForSelector('.guided-workspace', { visible: true });
  await new Promise((resolve) => setTimeout(resolve, 1200));
  const result = await page.evaluate(() => ({
    title: document.querySelector('#guided-review-title')?.textContent,
    queueItems: document.querySelectorAll('.guided-queue-item').length,
    selected: document.querySelector('.guided-queue-item[aria-current="true"]')?.textContent.trim(),
    detail: document.querySelector('#guided-detail-body')?.textContent.trim().slice(0, 120),
    workspaceDisplay: getComputedStyle(document.querySelector('.guided-workspace')).display,
    navTabs: [...document.querySelectorAll('.outreach-top-tab')].map((node) => node.textContent.trim()),
    kinds: [...document.querySelectorAll('.guided-kind')].map((node) => node.textContent.trim()),
    minTarget: Math.min(...[...document.querySelectorAll('.guided-filter, .guided-detail-body button, .guided-gmail-link')].map((node) => node.getBoundingClientRect().height)),
    draftBackground: getComputedStyle(document.querySelector('.rc-draft')).backgroundColor,
    sessionCopy: document.querySelector('#guided-progress-copy')?.textContent.trim(),
  }));
  assert.equal(result.title, 'Today’s review');
  assert.equal(result.queueItems, 4);
  assert.deepEqual(result.navTabs, ['Today', 'Promises', 'Follow-ups', 'Threads']);
  assert.deepEqual(result.kinds, ['REPLY', 'VERIFY', 'PROMISE', 'DUE']);
  assert.ok(result.minTarget >= 44, `smallest action target was ${result.minTarget}px`);
  assert.notEqual(result.draftBackground, 'rgb(255, 255, 255)');
  assert.equal(result.sessionCopy, '4 open · replies first');
  assert.deepEqual(errors, []);
  await page.screenshot({ path: 'qa-reports/guided-review-desktop.png', fullPage: true });

  await page.focus('.guided-queue-item[aria-current="true"]');
  await page.keyboard.press('ArrowDown');
  const keyboardSelection = await page.$eval('.guided-queue-item[aria-current="true"]', (node) => node.textContent.trim());
  assert.match(keyboardSelection, /VERIFY/);
  await page.click('.outreach-top-tab[data-outreach-tab-link="promises"]');
  const promises = await page.evaluate(() => ({
    title: document.querySelector('#guided-review-title')?.textContent,
    queueItems: document.querySelectorAll('.guided-queue-item').length,
    activeTab: document.querySelector('.outreach-top-tab[aria-current="page"]')?.textContent,
  }));
  await page.click('.outreach-top-tab[data-outreach-tab-link="follow-ups"]');
  const followUps = await page.evaluate(() => ({
    title: document.querySelector('#guided-review-title')?.textContent,
    queueItems: document.querySelectorAll('.guided-queue-item').length,
    activeTab: document.querySelector('.outreach-top-tab[aria-current="page"]')?.textContent,
  }));
  assert.equal(promises.title, 'Promise review');
  assert.equal(promises.queueItems, 1);
  assert.equal(promises.activeTab, 'Promises');
  assert.equal(followUps.title, 'Follow-ups due');
  assert.equal(followUps.queueItems, 1);
  assert.equal(followUps.activeTab, 'Follow-ups');

  await page.setViewport({ width: 375, height: 812, deviceScaleFactor: 1 });
  await page.click('.outreach-top-tab[data-outreach-tab-link="inbox"]');
  assert.equal(await page.$$eval('.guided-queue-item', (nodes) => nodes.length), 4);
  await page.click('.guided-queue-item');
  const mobileDetail = await page.evaluate(() => ({
    queueDisplay: getComputedStyle(document.querySelector('.guided-queue-pane')).display,
    detailDisplay: getComputedStyle(document.querySelector('.guided-detail-pane')).display,
    backDisplay: getComputedStyle(document.querySelector('#guided-back')).display,
  }));
  assert.equal(mobileDetail.queueDisplay, 'none');
  assert.equal(mobileDetail.detailDisplay, 'flex');
  assert.notEqual(mobileDetail.backDisplay, 'none');
  await page.screenshot({ path: 'qa-reports/guided-review-mobile.png', fullPage: true });
  await page.click('#guided-back');
  assert.notEqual(await page.$eval('.guided-queue-pane', (node) => getComputedStyle(node).display), 'none');
  assert.deepEqual(errors, []);
  console.log(JSON.stringify({ today: result, promises, followUps, errors }, null, 2));
  await browser.close();
  server.close();
})().catch((error) => {
  console.error(error);
  process.exit(1);
});
