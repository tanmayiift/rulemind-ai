import { chromium } from '@playwright/test';
import fs from 'fs';

const BASE = process.env.WEB_BASE || 'http://127.0.0.1:3000';
const ROUTES = ['/', ...fs.readdirSync('apps/web/app', { withFileTypes: true })
  .filter(d => d.isDirectory() && !d.name.startsWith('_') && !d.name.startsWith('('))
  .map(d => '/' + d.name)];

const browser = await chromium.launch();
const results = [];

for (const route of ROUTES) {
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  const page = await ctx.newPage();
  const consoleErrors = [], pageErrors = [], netFails = [];
  page.on('console', m => { if (m.type() === 'error') consoleErrors.push(m.text().slice(0, 160)); });
  page.on('pageerror', e => pageErrors.push(String(e).slice(0, 160)));
  page.on('requestfailed', r => netFails.push(`${r.method()} ${r.url().slice(0,70)}`));
  page.on('response', r => { if (r.status() >= 500) netFails.push(`HTTP ${r.status()} ${r.url().slice(0,70)}`); });

  let audit = { route, error: null };
  try {
    await page.goto(BASE + route, { waitUntil: 'networkidle', timeout: 45000 });
    await page.waitForTimeout(1200);
    const measured = await page.evaluate(() => {
      const vis = el => { const r = el.getBoundingClientRect(); const s = getComputedStyle(el);
        return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none'; };
      const name = el => (el.getAttribute('aria-label') || el.getAttribute('title') ||
        el.textContent?.trim() || el.getAttribute('placeholder') ||
        (el.labels?.length ? [...el.labels].map(l=>l.textContent.trim()).join(' ') : '') ||
        (el.getAttribute('aria-labelledby') ? 'labelledby' : '')).trim();

      const buttons = [...document.querySelectorAll('button')].filter(vis);
      const links   = [...document.querySelectorAll('a[href]')].filter(vis);
      const inputs  = [...document.querySelectorAll('input,select,textarea')].filter(vis);
      const images  = [...document.querySelectorAll('img')].filter(vis);

      return {
        bodyTextLen: (document.body.innerText || '').trim().length,
        horizontalOverflow: document.documentElement.scrollWidth > window.innerWidth + 1,
        counts: { buttons: buttons.length, links: links.length, inputs: inputs.length },
        unnamedButtons: buttons.filter(b => !name(b)).length,
        unnamedLinks:   links.filter(a => !name(a)).length,
        unlabelledInputs: inputs.filter(i => !name(i) && i.type !== 'hidden').length,
        imagesNoAlt: images.filter(i => !i.getAttribute('alt') && i.getAttribute('role') !== 'presentation').length,
        emptyHrefLinks: links.filter(a => { const h=a.getAttribute('href'); return h==='#'||h===''||h==='javascript:void(0)'; }).length,
        disabledButtons: buttons.filter(b => b.disabled).length,
        // Any element whose text is the same colour as its own background
        invisibleText: [...document.querySelectorAll('*')].filter(el => {
          if (!el.children.length && el.textContent?.trim() && vis(el)) {
            const s = getComputedStyle(el);
            return s.color === s.backgroundColor && s.backgroundColor !== 'rgba(0, 0, 0, 0)';
          }
          return false;
        }).length,
      };
    });
    audit = { ...audit, ...measured };
  } catch (e) { audit.error = String(e).split('\n')[0].slice(0, 130); }

  audit.consoleErrors = [...new Set(consoleErrors)].slice(0, 3);
  audit.pageErrors = [...new Set(pageErrors)].slice(0, 2);
  audit.netFails = [...new Set(netFails)].slice(0, 3);
  results.push(audit);

  const flags = [];
  if (audit.error) flags.push('LOAD:' + audit.error);
  if (audit.pageErrors?.length) flags.push(`pageerr:${audit.pageErrors.length}`);
  if (audit.consoleErrors?.length) flags.push(`console:${audit.consoleErrors.length}`);
  if (audit.netFails?.length) flags.push(`net:${audit.netFails.length}`);
  if (audit.horizontalOverflow) flags.push('H-OVERFLOW');
  if (audit.unnamedButtons) flags.push(`btn-noname:${audit.unnamedButtons}`);
  if (audit.unnamedLinks) flags.push(`link-noname:${audit.unnamedLinks}`);
  if (audit.unlabelledInputs) flags.push(`input-nolabel:${audit.unlabelledInputs}`);
  if (audit.imagesNoAlt) flags.push(`img-noalt:${audit.imagesNoAlt}`);
  if (audit.emptyHrefLinks) flags.push(`deadlink:${audit.emptyHrefLinks}`);
  if (audit.invisibleText) flags.push(`invisible-text:${audit.invisibleText}`);
  if ((audit.bodyTextLen ?? 0) < 60) flags.push('NEARLY-BLANK');
  console.log(`${flags.length ? '⚠️ ' : '✅'} ${route.padEnd(20)} ${audit.counts ? `b:${audit.counts.buttons} l:${audit.counts.links} i:${audit.counts.inputs}` : ''}  ${flags.join(' ')}`);
  await ctx.close();
}
await browser.close();
fs.writeFileSync('qa/e2e-full/frontend_audit.json', JSON.stringify(results, null, 2));
const bad = results.filter(r => r.error || r.pageErrors?.length || r.unnamedButtons || r.unnamedLinks || r.unlabelledInputs || r.horizontalOverflow || r.invisibleText);
console.log(`\n${results.length} routes · ${results.length - bad.length} clean · ${bad.length} with element findings`);
