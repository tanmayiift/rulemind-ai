import { chromium } from '@playwright/test';
const b = await chromium.launch();
for (const [w, h] of [[1440, 900], [1280, 720], [1366, 768]]) {
  const p = await (await b.newContext({ viewport: { width: w, height: h } })).newPage();
  await p.goto('http://localhost:3000/', { waitUntil: 'networkidle' });
  await p.waitForSelector('aside', { timeout: 15000 });
  const r = await p.evaluate(() => {
    const aside = document.querySelector('aside');
    const nav = aside.querySelector('nav');
    const links = [...aside.querySelectorAll('a')];
    const unlabelled = links.filter(a => !(a.getAttribute('aria-label') || a.textContent.trim()));
    const audit = links.find(a => a.getAttribute('href') === '/audit');
    const ab = audit ? audit.getBoundingClientRect() : null;
    return {
      docScrolls: document.documentElement.scrollHeight > innerHeight + 1,
      asideH: Math.round(aside.getBoundingClientRect().height),
      navScrollsInternally: nav.scrollHeight > nav.clientHeight,
      links: links.length,
      unlabelled: unlabelled.length,
      auditReachableByScrollingNav: !!audit && ab.top < nav.getBoundingClientRect().bottom + nav.scrollHeight,
    };
  });
  console.log(`${w}x${h}`, JSON.stringify(r));
  await p.context().close();
}
await b.close();
