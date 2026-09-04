import fs from 'node:fs/promises';
import puppeteer from 'puppeteer';

const base = 'http://127.0.0.1:8000';
const out = 'qa-reports/local-demo-2026-09-01';
await fs.mkdir(`${out}/screenshots`, {recursive: true});

const results = [];
const consoleErrors = [];
const browser = await puppeteer.launch({headless: true});
const page = await browser.newPage({viewport: {width: 1440, height: 1000}, deviceScaleFactor: 1});
page.on('console', msg => { if (msg.type() === 'error') consoleErrors.push(msg.text()); });
page.on('pageerror', error => consoleErrors.push(`pageerror: ${error.message}`));

async function check(name, fn) {
  try {
    const detail = await fn();
    results.push({name, status: 'PASS', detail});
  } catch (error) {
    results.push({name, status: 'FAIL', detail: error instanceof Error ? error.message : String(error)});
  }
}

async function jsonRequest(path, options = {}) {
  const response = await fetch(`${base}${path}`, options);
  let body;
  try { body = await response.json(); } catch { body = await response.text(); }
  return {status: response.status, headers: Object.fromEntries(response.headers), body};
}

await check('Health endpoint reports safe local mode', async () => {
  const response = await jsonRequest('/health');
  if (response.status !== 200 || response.body.ok !== true || response.body.safe_to_send !== false) throw new Error(JSON.stringify(response));
  return JSON.stringify(response.body);
});

await check('Demo page loads with seeded queue', async () => {
  const response = await page.goto(`${base}/demo`, {waitUntil: 'networkidle0'});
  const text = await page.$eval('body', el => el.innerText);
  const cards = await page.$$('.card');
  if (response?.status() !== 200 || cards.length !== 3 || !text.includes('Maya Chen') || !text.includes('Thomas Lee') || !text.includes('Priya Shah')) {
    throw new Error(`status=${response?.status()} cards=${cards.length}`);
  }
  await page.screenshot({path: `${out}/screenshots/01-initial-desktop.png`, fullPage: true});
  return '3 seeded queue cards rendered';
});

await check('Initial summary metrics are correct', async () => {
  const metrics = await page.$$eval('.metric', els => els.map(el => el.innerText.trim()));
  const expected = ['3\nNeed review', '1\nPromised follow-ups', '0\nExternal emails sent'];
  if (JSON.stringify(metrics) !== JSON.stringify(expected)) throw new Error(JSON.stringify(metrics));
  return metrics.join(' | ');
});

await check('Mark reviewed updates queue and summary', async () => {
  await page.click('.review');
  await page.waitForFunction(() => document.querySelector('#message')?.textContent?.includes('Saved locally'));
  const metrics = await page.$$eval('.metric', els => els.map(el => el.innerText.trim()));
  const firstButton = await page.$eval('.review', el => ({text: el.textContent, disabled: el.disabled}));
  if (metrics[0] !== '2\nNeed review' || firstButton.text !== 'Reviewed' || !firstButton.disabled) throw new Error(JSON.stringify({metrics, firstButton}));
  await page.screenshot({path: `${out}/screenshots/02-after-review.png`, fullPage: true});
  return 'review action persisted locally; needs_review changed 3 → 2';
});

await check('Protected send is rejected and does not send', async () => {
  await page.click('#send');
  await page.waitForFunction(() => document.querySelector('#message')?.textContent?.startsWith('409:'));
  const message = await page.$eval('#message', el => el.textContent);
  if (!message.includes('never sends email')) throw new Error(message);
  await page.screenshot({path: `${out}/screenshots/03-protected-send.png`, fullPage: true});
  return message;
});

await check('Reset restores seed data', async () => {
  await page.click('#reset');
  await page.waitForFunction(() => document.querySelector('#message')?.textContent?.includes('Demo data restored'));
  const metrics = await page.$$eval('.metric', els => els.map(el => el.innerText.trim()));
  if (metrics[0] !== '3\nNeed review' || metrics[1] !== '1\nPromised follow-ups') throw new Error(JSON.stringify(metrics));
  return 'summary restored to 3 reviews and 1 promised follow-up';
});

await check('Invalid queue item returns 404', async () => {
  const response = await jsonRequest('/api/demo/queue/not-a-real-item/review', {method: 'POST'});
  if (response.status !== 404) throw new Error(JSON.stringify(response));
  return JSON.stringify(response.body);
});

await check('Plan endpoint returns local limits', async () => {
  const response = await jsonRequest('/api/plan');
  if (response.status !== 200 || response.body.name !== 'Local demo' || response.body.daily_send_limit !== 25) throw new Error(JSON.stringify(response));
  return JSON.stringify(response.body);
});

await check('Safe sheet template downloads work', async () => {
  for (const path of ['/api/lead-sheet-template.csv', '/api/lead-sheet-template.xlsx']) {
    const response = await fetch(`${base}${path}`);
    if (response.status !== 200 || !(response.headers.get('content-disposition') || '').includes('attachment')) throw new Error(`${path}: ${response.status}`);
    if ((await response.arrayBuffer()).byteLength < 100) throw new Error(`${path}: empty download`);
  }
  return 'CSV and XLSX downloads returned non-empty attachments';
});

await check('Mobile layout renders without horizontal overflow', async () => {
  await page.setViewport({width: 390, height: 844, deviceScaleFactor: 1});
  await page.goto(`${base}/demo`, {waitUntil: 'networkidle0'});
  const dimensions = await page.evaluate(() => ({scrollWidth: document.documentElement.scrollWidth, clientWidth: document.documentElement.clientWidth}));
  await page.screenshot({path: `${out}/screenshots/04-mobile.png`, fullPage: true});
  if (dimensions.scrollWidth > dimensions.clientWidth + 1) throw new Error(JSON.stringify(dimensions));
  return JSON.stringify(dimensions);
});

for (const path of ['/login', '/signup']) {
  await check(`${path} redirects safely to demo`, async () => {
    const response = await fetch(`${base}${path}`, {redirect: 'manual'});
    const location = response.headers.get('location');
    if (response.status !== 303 || location !== '/demo') throw new Error(`status=${response.status} location=${location}`);
    return `303 → ${location}`;
  });
}

await browser.close();

const passed = results.filter(result => result.status === 'PASS').length;
const failed = results.filter(result => result.status === 'FAIL').length;
const report = {
  target: `${base}/demo`,
  generated_at: new Date().toISOString(),
  results,
  passed,
  failed,
  console_errors: consoleErrors,
};
await fs.writeFile(`${out}/results.json`, JSON.stringify(report, null, 2));
console.log(JSON.stringify(report, null, 2));
process.exitCode = failed ? 1 : 0;
