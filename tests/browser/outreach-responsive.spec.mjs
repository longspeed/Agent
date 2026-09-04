import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { readFile } from 'node:fs/promises';
import path from 'node:path';
import process from 'node:process';
import puppeteer from 'puppeteer';

const root = process.cwd();
const staticRoot = path.join(root, 'static');
const mime = { '.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css' };

const server = createServer(async (request, response) => {
  try {
    const url = new URL(request.url, 'http://localhost');
    let file;
    if (url.pathname === '/outreach') file = path.join(staticRoot, 'outreach.html');
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

try {
  const page = await browser.newPage();
  await page.setRequestInterception(true);
  page.on('request', async (request) => {
    const url = new URL(request.url());
    if (url.origin !== baseUrl) return request.abort().catch(() => {});
    if (!url.pathname.startsWith('/api/')) return request.continue().catch(() => {});

    let body = {};
    if (url.pathname === '/api/me') body = { email: 'pilot@example.com' };
    else if (url.pathname === '/api/outreach/replies') body = [{ id: 1, kind: 'reply', status: 'pending' }];
    else if (url.pathname === '/api/outreach/commitments') body = [{ id: 2, status: 'detected' }];
    else if (url.pathname === '/api/outreach/commitments/summary') body = { detected: 1, confirmed: 0, due: 0 };
    else if (url.pathname === '/api/outreach/follow-ups/status') body = { due: 1, waiting: 0, queued: 0, sent: 0 };
    else if (url.pathname === '/api/outreach/campaigns/preview') body = {
      send_mode: 'manual', bounces: {}, eligible: 0, eligible_total: 0,
      sent_today: 0, daily_limit: 50, remaining_today: 50, capped: 0,
      blockers: [], deliverability: { status: 'ready', findings: [] }, auto_send_enabled: false,
    };
    else if (url.pathname === '/api/outreach/evidence') body = {};
    else if (url.pathname === '/api/outreach/drafts' ||
             url.pathname === '/api/outreach/campaigns' ||
             url.pathname === '/api/outreach/commitments/resolved' ||
             url.pathname === '/api/outreach/outcomes' ||
             url.pathname === '/api/outreach/dogfood-log') body = [];

    return request.respond({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(body),
    }).catch(() => {});
  });

  for (const width of [375, 768]) {
    await page.setViewport({ width, height: 900, deviceScaleFactor: 1 });
    await page.goto(`${baseUrl}/outreach?tab=inbox`, { waitUntil: 'domcontentloaded' });
    await page.waitForSelector('#guided-filters [data-guided-filter="verify"]');

    const geometry = await page.evaluate(() => {
      const filters = document.querySelector('#guided-filters');
      const last = filters?.querySelector('[data-guided-filter="verify"]');
      const filterRect = filters?.getBoundingClientRect();
      const lastRect = last?.getBoundingClientRect();
      return {
        pageOverflow: document.documentElement.scrollWidth - document.documentElement.clientWidth,
        filterOverflow: filters ? filters.scrollWidth - filters.clientWidth : 999,
        lastInsideViewport: !!lastRect && lastRect.left >= 0 && lastRect.right <= window.innerWidth,
        lastInsideFilter: !!filterRect && !!lastRect &&
          lastRect.left >= filterRect.left && lastRect.right <= filterRect.right + 1,
      };
    });

    assert.ok(geometry.pageOverflow <= 1, `${width}px page overflowed by ${geometry.pageOverflow}px`);
    assert.ok(geometry.filterOverflow <= 1, `${width}px filters overflowed by ${geometry.filterOverflow}px`);
    assert.equal(geometry.lastInsideViewport, true, `${width}px clipped the Verify filter`);
    assert.equal(geometry.lastInsideFilter, true, `${width}px put Verify outside its filter row`);
  }

  console.log('outreach responsive filters: passed at 375px and 768px');
} finally {
  await browser.close();
  await new Promise((resolve) => server.close(resolve));
}
