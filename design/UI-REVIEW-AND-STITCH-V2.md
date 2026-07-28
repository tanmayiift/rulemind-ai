# RuleMind UI — Critical Review + Gap-Fixed Stitch Prompt (v2)

Reviewed the 14-screen Stitch sample (`stitch_rulemind_decision_platform.zip`). **This is a strong first pass — not the final flow.** Below: what's good, product gaps, UX/flow gaps, then a revised master prompt that bakes in every fix.

## Verdict
The sample nails the information architecture and hits ~90% of the product surface. The rule-builder canvas (node palette + inspector + live MECE diagnostics with "auto-generate fallback rule") and the experiments screen (champion/challenger, traffic slider, live significance, cumulative approval chart) are genuinely good and match the backend that already exists. Ship-quality direction. The gaps below are what separate "demo" from "product."

## Consistency gaps (fix first — they read as "different apps")
1. **Brand/logo drift.** Dashboard shows plain "RuleMind"; other screens add an "Enterprise Logic" subtitle and a *different* logo glyph on nearly every screen (compass, atom, shield, sprout). Pick ONE wordmark + mark and lock it.
2. **Environment switcher is different on every screen** — a "Production" pill (dashboard), "ENV: PROD" (policies), "Prod /" breadcrumb (experiments), "PROD" chip (monitoring). Standardize ONE env switcher component, top-right, that also gates writes.
3. **Top bar inconsistency.** Search placeholder, breadcrumbs vs. page-title-only, and the presence/position of notifications/settings/avatar shift screen to screen. Define one AppBar.
4. **Sidebar item set drifts** (Deploy/Settings pinned bottom on some, inline on others). Lock nav order + pinning.

## Product gaps (missing capability the platform needs)
1. **No Simulation/Backtest screen shown in my sample set** beyond the nav entry — this is core (run a policy version against a dataset, diff vs prod). Must be a first-class screen with dataset upload, outcome distribution, and a prod-vs-candidate diff.
2. **Explainability / adverse-action reason codes** — for lending this is a regulatory must. No decision-detail drawer showing the explanation tree (actual vs expected per condition) + reason codes.
3. **Monitoring has placeholder charts** ("[Interactive Area Chart Rendered Here]", "Shift Detected" empty box). PSI/drift table is good; the hero charts must be real. Add: alert → drill-through to the offending rule/variable.
4. **Champion/Challenger promotion loop is incomplete** — experiments show significance but no explicit **Promote Challenger → Prod** with guardrail confirmation + auto-rollback threshold. Close the loop.
5. **No connectors "resolve/latency/error-rate" health per source** on the connector screen (dashboard shows aggregate 98.5% only). Ops needs per-connector SLA.
6. **No RBAC / maker-checker surface** — deploy is destructive; needs approver ≠ author, visible on Deploy + Audit.
7. **No decision-search / single-decision lookup** ("why did req_8a92b1 get rejected?") — the most common support query. Needs a decision explorer.
8. **Bundle/version as a first-class object** — versions appear per-policy, but the immutable **bundle** (what actually deploys to edge/SDK) isn't surfaced; it's the unit of promotion, rollback, and A/B.

## UX / flow gaps
1. **Rule builder:** the drag-and-drop story is implied but there's no visible drag affordance (ghost node, drop target, snapping), no minimap for large graphs, no keyboard nav, and the MECE panel overlaps the canvas (should be a dockable/collapsible bottom sheet). "Auto-layout Active" needs a manual toggle. No undo/redo visible — mandatory for a canvas.
2. **Empty & loading states** are absent everywhere. Enterprise tools live in empty/loading/error far more than the happy path. Every list + chart needs skeletons + empty CTAs + error retry.
3. **Validation → action linkage:** Policies "Deploy (Disabled)" with 2 issues is good, but the issues should deep-link to the exact node (they hint at "Review Rule Matrix" — make it a real jump). Same for the dashboard experiments card → open experiment.
4. **Destructive actions** (Halt Experiment, Deploy, Archive) need confirm dialogs with blast-radius text ("affects 14,209 decisions/day").
5. **Accessibility:** several status pills rely on color alone (approve/review/reject). Add icon or text token so it's not color-only. Verify contrast on the light indigo pills.
6. **Density controls & responsive:** high-density tables are right for the persona, but there's no comfortable/compact toggle and no defined breakpoint behavior (drawers on top of a 1280px canvas will crush the graph on laptops).
7. **Dark mode** is specified in DESIGN.md but no dark screens were generated — must be designed, not derived, especially the "Logic Zone" dark editor which needs to stay distinct in dark mode.
8. **Time handling:** timestamps like `10:42:05.123` with no timezone; experiments show absolute dates only. Add relative + absolute + tz.

## Revised Stitch Master Prompt (v2 — paste this)

```
Design "RuleMind" — an enterprise no-code decisioning + rule-engine platform for
risk, credit, and fraud teams. Modern-corporate, data-first, calm, high-density.
Trustworthy fintech (Stripe Radar × visual workflow builder). Generate a COHERENT
multi-screen system — every screen must share the exact same shell.

LOCKED DESIGN SYSTEM (use identically on every screen):
- One wordmark: "RuleMind" + a single geometric mark (do NOT vary the glyph or add
  subtitles per screen).
- Accent indigo #5B5BD6; success emerald #10B981; warning amber #F59E0B; danger
  #DC2626. Light: #FFFFFF surfaces, #E2E8F0 borders. Dark: #0F172A surfaces, #1E293B
  borders. DESIGN BOTH LIGHT AND DARK for every screen.
- Inter for UI, JetBrains Mono for expressions/IDs. 8px grid. Sidebar 260px, right
  drawers 480px. Status pills carry an ICON + text (never color-only).
- ONE AppBar on every screen: left breadcrumbs, center global search, right = env
  switcher (Dev/UAT/Prod, gates writes) + notifications + settings + avatar.
- ONE left sidebar, fixed order: Dashboard, Connectors, Variables, Rule Builder,
  Scorecards, Policies, Test Console, Simulation, Experiments, Review Queue,
  Monitoring, Audit Log, Deploy; Settings pinned bottom. Active = 4px indigo rail.
- Every list/chart shows three states: skeleton loading, empty-with-CTA, error-retry.
- Every destructive action (Deploy, Halt, Archive, Delete) opens a confirm dialog
  stating blast radius ("affects 14,209 decisions/day").

SCREENS:
1. Dashboard — decision volume, outcome mix donut, p95 latency, per-connector health,
   recent decisions (clickable → decision detail), active experiments (click → open).
2. Connectors — per-source cards with resolve latency, error rate, last-sync, toggle,
   schema viewer, sample-payload tester.
3. Variables — feature list; dark code editor drawer (Python) with test-run + version
   history graph; "unused variable" lint chip.
4. Rule Builder — drag-and-drop node canvas with VISIBLE drag affordances (ghost node,
   highlighted drop target, edge snapping), minimap, undo/redo, manual/auto layout
   toggle; left node palette; right inspector; a COLLAPSIBLE bottom MECE panel whose
   findings deep-link to the offending node ("Rules 2 & 3 overlap on income 40k–60k →
   Go to node").
5. Scorecards — weighted factors + WoE bins + formula editor (dark logic zone).
6. Policies — pipeline builder (Inputs→Variables→Scorecard→Rules→Decision) with a
   validation panel whose issues jump to the exact node; version+bundle table with
   Deploy gated by validation.
7. Test Console — JSON input → decision + EXPANDABLE explanation tree (actual vs
   expected per condition, trace id) + adverse-action reason codes; batch tab.
8. Simulation & Backtest — pick policy/bundle version, upload/select dataset, run,
   show outcome distribution + a side-by-side DIFF vs current production.
9. Experiments (A/B + Champion/Challenger) — variant cards (Champion vs Challenger
   with bundle versions), traffic-ramp timeline (5→25→50%), live approval/FPR/latency
   with significance badge, cumulative chart, and a PROMOTE CHALLENGER → PROD action
   with guardrail confirm + auto-rollback threshold.
10. Review Queue — manual cases, case detail drawer, notes, amount, approve/reject.
11. Monitoring — REAL decision-volume area chart, score-distribution shift (current vs
    baseline), PSI/drift table, active-anomaly alerts that drill through to the rule.
12. Audit Log — immutable timeline, who/what/when, diffs, reason, maker-checker.
13. Deploy — Dev→UAT→Prod promotion with maker-checker (approver ≠ author), MECE gate,
    bundle version being promoted, rollback.
14. Decision Explorer — search a decision by id/subject → full trace + explanation.
15. Settings & Admin — tenants, members with RBAC roles, API keys, env config, SSO.

Include a Decision Detail drawer (reused by Dashboard, Test Console, Monitoring,
Decision Explorer): outcome, score, expandable explanation tree, reason codes,
experiment variant, latency, trace id.
```

## Follow-up per-screen deep-dive prompts
- *Dark mode pass:* "Regenerate all 15 screens in dark mode (#0F172A surfaces, #1E293B borders); keep the rule/scorecard editor a distinct near-black 'logic zone' that stays visually separate from the page even in dark mode."
- *Rule builder interactions:* "Show mid-drag: a ghost 'Condition' node under the cursor, a highlighted drop target on an AND gate, an edge snapping to a port, a minimap bottom-right, undo/redo in the toolbar, and the MECE panel collapsed to a single warning bar."
- *Promotion loop:* "Show the Promote Challenger dialog: challenger v2.5.0-rc1 → Prod, guardrail 'auto-rollback if FPR > 1.5%', blast radius '90k decisions/day', maker-checker approver field."
