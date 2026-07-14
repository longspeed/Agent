import puppeteer from 'puppeteer';
import fs from 'node:fs';
import path from 'node:path';

const url = process.argv[2];
const label = process.argv[3];

if (!url) {
  console.error('Usage: node screenshot.mjs <url> [label]');
  process.exit(1);
}

const DIR = './temporary screenshots';
if (!fs.existsSync(DIR)) fs.mkdirSync(DIR, { recursive: true });

let n = 1;
while (fs.existsSync(path.join(DIR, `screenshot-${n}${label ? '-' + label : ''}.png`))) n++;
const outFile = path.join(DIR, `screenshot-${n}${label ? '-' + label : ''}.png`);

const browser = await puppeteer.launch();
const page = await browser.newPage();
await page.setViewport({ width: 1440, height: 900 });
await page.goto(url, { waitUntil: 'networkidle0' });
await page.screenshot({ path: outFile, fullPage: true });
await browser.close();

console.log(`Saved ${outFile}`);
