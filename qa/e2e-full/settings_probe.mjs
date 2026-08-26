import { chromium } from '@playwright/test';
const b = await chromium.launch();
const p = await (await b.newContext({viewport:{width:1440,height:900}})).newPage();
p.on('console', async m => {
  if (m.type() !== 'error') return;
  const args = await Promise.all(m.args().map(a => a.jsonValue().catch(() => '?')));
  console.log('CONSOLE:', args.map(String).join(' | ').slice(0, 400));
});
await p.goto('http://localhost:3000/settings', { waitUntil: 'networkidle' });
await p.waitForTimeout(1500);
await b.close();
