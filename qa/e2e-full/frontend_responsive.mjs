import { chromium } from '@playwright/test';
import fs from 'fs';
const BASE = process.env.WEB_BASE || 'http://127.0.0.1:3000';
const ROUTES = ['/', ...fs.readdirSync('apps/web/app',{withFileTypes:true})
  .filter(d=>d.isDirectory()&&!d.name.startsWith('_')&&!d.name.startsWith('('))
  .map(d=>'/'+d.name)];
const VIEWPORTS = [['mobile',375,812],['tablet',768,1024],['desktop',1440,900]];
const b = await chromium.launch();
const problems = [];

for (const [vpName,w,h] of VIEWPORTS) {
  for (const scheme of (vpName==='desktop' ? ['light','dark'] : ['light'])) {
    const ctx = await b.newContext({ viewport:{width:w,height:h}, colorScheme: scheme });
    const page = await ctx.newPage();
    for (const route of ROUTES) {
      try {
        await page.goto(BASE+route,{waitUntil:'networkidle',timeout:40000});
        await page.waitForTimeout(500);
        const r = await page.evaluate(() => {
          const body = document.body;
          const overflow = document.documentElement.scrollWidth - window.innerWidth;
          // Elements past the right edge — but ONLY those with no horizontally
          // scrollable ancestor. A wide table inside overflow-x:auto legitimately
          // extends beyond the viewport; counting those reported 54 "problems" on
          // /simulation alone, every one of them correct behaviour.
          const scrollableAncestor = el => {
            let n = el.parentElement;
            while (n && n !== document.documentElement) {
              const cs = getComputedStyle(n);
              if ((cs.overflowX==='auto'||cs.overflowX==='scroll') && n.scrollWidth > n.clientWidth + 1) return n;
              n = n.parentElement;
            }
            return null;
          };
          const spill = [...document.querySelectorAll('*')].filter(el=>{
            const b = el.getBoundingClientRect();
            if (!(b.width>0 && b.right > window.innerWidth + 2)) return false;
            if (getComputedStyle(el).position==='fixed') return false;
            return !scrollableAncestor(el);
          }).length;
          const bg = getComputedStyle(body).backgroundColor;
          const fg = getComputedStyle(body).color;
          // any text that matches its own background exactly
          const invisible = [...document.querySelectorAll('*')].filter(el=>{
            if (el.children.length || !el.textContent?.trim()) return false;
            const s = getComputedStyle(el);
            return s.color===s.backgroundColor && s.backgroundColor!=='rgba(0, 0, 0, 0)';
          }).length;
          return { overflow, spill, bg, fg, invisible, textLen:(body.innerText||'').trim().length };
        });
        const flags=[];
        if (r.overflow > 2) flags.push(`h-scroll:+${r.overflow}px`);
        if (r.spill > 0) flags.push(`spill:${r.spill}`);
        if (r.invisible > 0) flags.push(`invisible-text:${r.invisible}`);
        if (r.textLen < 60) flags.push('near-blank');
        if (flags.length) { problems.push({vpName,scheme,route,...r,flags}); console.log(`⚠️  ${vpName}/${scheme} ${route.padEnd(20)} ${flags.join(' ')}`); }
      } catch(e){ problems.push({vpName,scheme,route,error:String(e).slice(0,90)}); console.log(`❌ ${vpName}/${scheme} ${route} ${String(e).split('\n')[0].slice(0,70)}`); }
    }
    // one representative dark-mode colour sanity check
    if (scheme==='dark') {
      // The app uses an explicit toggle (persisted in localStorage) rather than
      // following prefers-color-scheme, so drive the real control.
      await page.goto(BASE+'/',{waitUntil:'networkidle'});
      const before = await page.evaluate(()=>getComputedStyle(document.body).backgroundColor);
      await page.click('[aria-label="Toggle dark mode"]').catch(()=>{});
      await page.waitForTimeout(700);
      const after = await page.evaluate(()=>({bg:getComputedStyle(document.body).backgroundColor, fg:getComputedStyle(document.body).color}));
      console.log(`   dark toggle: ${before} -> bg=${after.bg} fg=${after.fg} (${before!==after.bg ? 'applied' : 'NO EFFECT'})`);
    }
    await ctx.close();
    console.log(`— ${vpName}/${scheme}: swept ${ROUTES.length} routes`);
  }
}
await b.close();
console.log(`\n${problems.length} layout/theme problems across ${VIEWPORTS.length} viewports`);
