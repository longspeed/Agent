// Puppeteer REPL driver for Agent Hub. Reads line-delimited commands from
// stdin (pipe a heredoc), drives one headless Chromium session per run.
// Command vocabulary intentionally mirrors chromium-cli (nav/wait-for/click/
// fill/press/screenshot/console/eval) since that's the standard driver
// shape for browser-driven apps — this one exists because chromium-cli
// isn't installed on this (Windows) machine.
//
// Usage:
//   node .claude/skills/run-agent-hub/driver.mjs <<'EOF'
//   nav http://localhost:8000/login
//   fill #password hunter2
//   click button[type=submit]
//   wait-nav
//   screenshot home
//   EOF
//
// Screenshots land in ./temporary screenshots/driver-<label>.png (repo root,
// matches this project's existing screenshot.mjs convention).

import puppeteer from 'puppeteer';
import fs from 'node:fs';
import path from 'node:path';
import readline from 'node:readline';

const SHOT_DIR = './temporary screenshots';
if (!fs.existsSync(SHOT_DIR)) fs.mkdirSync(SHOT_DIR, { recursive: true });

const browser = await puppeteer.launch();
const page = await browser.newPage();
await page.setViewport({ width: 1440, height: 900 });

const consoleErrors = [];
page.on('console', (msg) => {
  if (msg.type() === 'error') consoleErrors.push(msg.text());
});
page.on('pageerror', (err) => consoleErrors.push(String(err)));

function splitArgs(rest) {
  // selector/first-arg + free-text remainder (for `fill <sel> <value with spaces>`)
  const m = rest.match(/^(\S+)\s*(.*)$/);
  return [m?.[1] ?? '', m?.[2] ?? ''];
}

async function run(line) {
  const trimmed = line.trim();
  if (!trimmed || trimmed.startsWith('#')) return;
  const [cmd, rest] = splitArgs(trimmed).length ? [trimmed.split(/\s+/, 1)[0], trimmed.slice(trimmed.split(/\s+/, 1)[0].length).trim()] : [trimmed, ''];

  try {
    switch (cmd) {
      case 'nav': {
        await page.goto(rest, { waitUntil: 'domcontentloaded', timeout: 20000 });
        console.log(`ok: nav ${rest}`);
        break;
      }
      case 'wait-nav': {
        await page.waitForNavigation({ waitUntil: 'domcontentloaded', timeout: 20000 });
        console.log('ok: wait-nav');
        break;
      }
      case 'wait-for': {
        if (rest.startsWith('text=')) {
          const text = rest.slice(5);
          await page.waitForFunction(
            (t) => document.body.innerText.includes(t),
            { timeout: 15000 },
            text,
          );
        } else {
          const sel = rest.replace(/^css=/, '');
          await page.waitForSelector(sel, { timeout: 15000 });
        }
        console.log(`ok: wait-for ${rest}`);
        break;
      }
      case 'click': {
        await page.click(rest);
        console.log(`ok: click ${rest}`);
        break;
      }
      case 'fill': {
        const [sel, value] = splitArgs(rest);
        await page.evaluate((s) => { document.querySelector(s).value = ''; }, sel);
        await page.type(sel, value);
        console.log(`ok: fill ${sel}`);
        break;
      }
      case 'press': {
        await page.keyboard.press(rest);
        console.log(`ok: press ${rest}`);
        break;
      }
      case 'screenshot': {
        const label = rest || 'shot';
        const file = path.join(SHOT_DIR, `driver-${label}.png`);
        await page.screenshot({ path: file, fullPage: true });
        console.log(`ok: screenshot -> ${file}`);
        break;
      }
      case 'eval': {
        const result = await page.evaluate(rest);
        console.log(`ok: eval -> ${JSON.stringify(result)}`);
        break;
      }
      case 'console': {
        console.log(consoleErrors.length ? consoleErrors.join('\n') : 'ok: no console errors captured');
        break;
      }
      case 'sleep': {
        await new Promise((r) => setTimeout(r, Number(rest) || 500));
        console.log(`ok: sleep ${rest}`);
        break;
      }
      default:
        console.log(`error: unknown command "${cmd}"`);
    }
  } catch (e) {
    console.log(`error: ${cmd} -> ${e.message}`);
  }
}

const rl = readline.createInterface({ input: process.stdin, terminal: false });
for await (const line of rl) {
  await run(line);
}

await browser.close();
