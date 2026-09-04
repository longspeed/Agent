import puppeteer from 'puppeteer';

const browser = await puppeteer.launch({headless: true});
const page = await browser.newPage({viewport: {width: 1440, height: 1000}, deviceScaleFactor: 1});
for (const url of ['http://127.0.0.1:8000/health', 'http://127.0.0.1:8000/demo']) {
  const response = await page.goto(url, {waitUntil: 'networkidle0'});
  console.log('URL', url, 'STATUS', response?.status(), 'TITLE', await page.title());
  console.log((await page.$eval('body', el => el.innerText)).slice(0, 12000));
  console.log('LINKS', await page.$$eval('a', els => els.map(e => ({text: e.innerText, href: e.href}))));
  console.log('BUTTONS', await page.$$eval('button', els => els.map(e => ({text: e.innerText, type: e.type, disabled: e.disabled}))));
  console.log('FORMS', await page.$$eval('form', els => els.map(e => ({action: e.action, method: e.method, text: e.innerText}))));
  if (url.endsWith('/demo')) await page.screenshot({path: 'qa-reports/local-demo-2026-09-01/initial-demo.png', fullPage: true});
}
await browser.close();
