// Ad-hoc route sweep. THE GATE'S VERSION OF THIS IS e2e/route-health.spec.ts, which runs every
// route on every regression and asserts the same properties plus a few this cannot (no 5xx, no
// uncaught page error, content beyond the app shell).
//
// Keep this script for exploration: it writes web_sweep_results.json, which is useful when you
// want a table of every route's console output in one place rather than a pass/fail. Do not
// treat it as coverage -- its checked-in results were stale at 36 routes while the app had 38,
// which is precisely why the assertions moved into the gate.

import { chromium } from '@playwright/test';
import fs from 'fs';

const BASE = process.env.WEB_BASE || 'http://localhost:3000';
const ROUTES = fs.readdirSync('apps/web/app', { withFileTypes: true })
  .filter(d => d.isDirectory() && !d.name.startsWith('_') && !d.name.startsWith('('))
  .map(d => '/' + d.name);
ROUTES.unshift('/');

const browser = await chromium.launch();
const results = [];
for (const route of ROUTES) {
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  const page = await ctx.newPage();
  const consoleErrors = [], pageErrors = [], failedReqs = [];
  page.on('console', m => { if (m.type() === 'error') consoleErrors.push(m.text().slice(0, 200)); });
  page.on('pageerror', e => pageErrors.push(String(e).slice(0, 200)));
  page.on('requestfailed', r => failedReqs.push(`${r.method()} ${r.url().slice(0, 90)} — ${r.failure()?.errorText}`));
  page.on('response', r => { if (r.status() >= 500) failedReqs.push(`HTTP ${r.status()} ${r.url().slice(0, 90)}`); });

  let status = 'ok', detail = '';
  try {
    const resp = await page.goto(BASE + route, { waitUntil: 'networkidle', timeout: 45000 });
    if (!resp || resp.status() >= 400) { status = 'http'; detail = `HTTP ${resp?.status()}`; }
    await page.waitForTimeout(900);
    // Did anything actually render?
    const text = (await page.locator('body').innerText().catch(() => '')).trim();
    const hasNextError = await page.locator('text=/Application error|Unhandled Runtime Error|This page could not be found/i').count();
    if (hasNextError) { status = 'crash'; detail = 'Next.js error overlay'; }
    else if (text.length < 40) { status = 'blank'; detail = `only ${text.length} chars rendered`; }
  } catch (e) {
    status = 'timeout'; detail = String(e).split('\n')[0].slice(0, 160);
  }
  if (status === 'ok' && (pageErrors.length || consoleErrors.length)) status = 'warn';
  results.push({ route, status, detail, pageErrors, consoleErrors: [...new Set(consoleErrors)].slice(0, 3), failedReqs: [...new Set(failedReqs)].slice(0, 3) });
  const icon = { ok: '✅', warn: '⚠️ ', blank: '⬜', crash: '❌', http: '❌', timeout: '⏱' }[status];
  console.log(`${icon} ${route.padEnd(22)} ${detail}${consoleErrors.length ? ` | console:${consoleErrors.length}` : ''}${pageErrors.length ? ` | pageerr:${pageErrors.length}` : ''}${failedReqs.length ? ` | netfail:${failedReqs.length}` : ''}`);
  await ctx.close();
}
await browser.close();
fs.writeFileSync('qa/e2e-full/web_sweep_results.json', JSON.stringify(results, null, 2));
const bad = results.filter(r => r.status !== 'ok');
console.log(`\n${results.length} routes · ${results.length - bad.length} clean · ${bad.length} with findings`);
