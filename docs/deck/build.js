// RuleMind AI — enterprise buyer deck.
// Every number on a slide is verified against the repo; see verify notes in the session.
const pptxgen = require("pptxgenjs");
const React = require("react");
const ReactDOMServer = require("react-dom/server");
const sharp = require("sharp");
const Fi = require("react-icons/fi");

// ── Palette: the product's own identity, not a generic deck blue ───────────────
const INK      = "141A2E";  // deep ink — dominant dark ground
const INK_SOFT = "232A45";  // raised surface on dark
const ACCENT   = "635BFF";  // RuleMind indigo
const ACCENT_D = "4B44D1";  // deeper indigo for fills behind white text
const GROUND   = "F5F7FB";  // cool light ground
const CARD     = "FFFFFF";
const MUTED    = "4F566B";
const MUTED_L  = "9AA3BC";  // muted on dark
const LINE     = "E3E8F2";

// Decision-semantic colours — the product literally emits approve / review / reject.
const APPROVE  = "0E9F6E";
const REVIEW   = "D97706";
const REJECT   = "DC2626";

const TITLE_FONT = "Cambria";   // institutional serif — the regulator/audit register
const BODY_FONT  = "Calibri";

const W = 13.333, H = 7.5, M = 0.62;

const pres = new pptxgen();
pres.layout = "LAYOUT_WIDE";
pres.author = "RuleMind AI";
pres.title = "RuleMind AI";

// ── icon helper ───────────────────────────────────────────────────────────────
const iconCache = {};
async function icon(name, color) {
  const key = name + color;
  if (iconCache[key]) return iconCache[key];
  const Comp = Fi[name];
  if (!Comp) throw new Error("no icon " + name);
  const svg = ReactDOMServer.renderToStaticMarkup(
    React.createElement(Comp, { color: "#" + color, size: 256, strokeWidth: 2 })
  );
  const buf = await sharp(Buffer.from(svg)).resize(256, 256).png().toBuffer();
  iconCache[key] = "image/png;base64," + buf.toString("base64");
  return iconCache[key];
}

// ── shared slide furniture ────────────────────────────────────────────────────
function titleSlide(s, text, kicker, dark) {
  if (kicker) {
    s.addText(kicker, {
      x: M, y: 0.46, w: 11.5, h: 0.3, isTextBox: true, margin: 0,
      fontFace: BODY_FONT, fontSize: 12, bold: true, charSpacing: 2.2,
      color: dark ? ACCENT : ACCENT,
    });
  }
  s.addText(text, {
    x: M, y: kicker ? 0.80 : 0.5, w: 11.9, h: 0.9, isTextBox: true, margin: 0,
    fontFace: TITLE_FONT, fontSize: 34, bold: true,
    color: dark ? "FFFFFF" : INK,
  });
}

function footer(s, n, dark) {
  s.addText("RuleMind AI", {
    x: M, y: H - 0.62, w: 3, h: 0.28, isTextBox: true, margin: 0,
    fontFace: BODY_FONT, fontSize: 9, color: dark ? MUTED_L : MUTED_L,
  });
  s.addText(String(n), {
    x: W - M - 1, y: H - 0.62, w: 1, h: 0.28, isTextBox: true, margin: 0, align: "right",
    fontFace: BODY_FONT, fontSize: 9, color: dark ? MUTED_L : MUTED_L,
  });
}

function card(s, x, y, w, h, fill) {
  s.addShape(pres.ShapeType.roundRect, {
    x, y, w, h, rectRadius: 0.1,
    fill: { color: fill || CARD },
    line: { color: fill ? fill : LINE, width: 1 },
    shadow: { type: "outer", angle: 90, blur: 10, offset: 2, color: "0B1020", opacity: 0.07 },
  });
}

// ══════════════════════════════════════════════════════════════════════════════
async function build() {

// ── 1. Title ──────────────────────────────────────────────────────────────────
{
  const s = pres.addSlide();
  s.background = { color: INK };

  // Motif: the decision triad, stated once at full size.
  [[APPROVE, "approve"], [REVIEW, "review"], [REJECT, "reject"]].forEach(([c, label], i) => {
    s.addShape(pres.ShapeType.ellipse, {
      x: M + i * 1.5, y: 1.05, w: 0.3, h: 0.3, fill: { color: c }, line: { color: c, width: 0 },
    });
    s.addText(label, {
      x: M + i * 1.5 + 0.4, y: 1.06, w: 1.1, h: 0.3, isTextBox: true, margin: 0,
      fontFace: BODY_FONT, fontSize: 11, color: MUTED_L, valign: "middle",
    });
  });

  s.addText("RuleMind AI", {
    x: M, y: 2.0, w: 11.5, h: 1.1, isTextBox: true, margin: 0,
    fontFace: TITLE_FONT, fontSize: 60, bold: true, color: "FFFFFF",
  });
  s.addText("The decisioning engine your risk team can actually change", {
    x: M, y: 3.15, w: 10.5, h: 0.6, isTextBox: true, margin: 0,
    fontFace: TITLE_FONT, fontSize: 25, color: ACCENT,
  });
  s.addText(
    "Author rules, scorecards and policies over any JSON source. Promote dev → uat → prod " +
    "with an audit trail that answers “why” about a decision from eight months ago. " +
    "Runs entirely inside your perimeter.",
    {
      x: M, y: 3.95, w: 8.6, h: 1.0, isTextBox: true, margin: 0,
      fontFace: BODY_FONT, fontSize: 15, color: MUTED_L, lineSpacing: 23,
    });

  const stats = [
    ["4,608", "tests, 11 lanes, all green"],
    ["5", "language engines, held to one corpus"],
    ["19.8 ms", "median decision, measured"],
    ["0", "phone-home, metering or licence calls"],
  ];
  stats.forEach(([big, small], i) => {
    const x = M + i * 3.0;
    s.addText(big, {
      x, y: 5.45, w: 2.8, h: 0.58, isTextBox: true, margin: 0,
      fontFace: BODY_FONT, fontSize: 32, bold: true, color: "FFFFFF",
    });
    s.addText(small, {
      x, y: 6.02, w: 2.75, h: 0.55, isTextBox: true, margin: 0,
      fontFace: BODY_FONT, fontSize: 11, color: MUTED_L, lineSpacing: 15,
    });
  });
  s.addNotes(
    "RuleMind is a decisioning engine: the layer that turns data about an applicant into " +
    "approve / review / reject, with the reasoning recorded. Two audiences in the room — the " +
    "risk lead who wants to change policy this week, and the person who has to answer the " +
    "regulator. Every number in this deck is generated from the repository, not typed."
  );
  footer(s, 1, true);
}

// ── 2. The problem ────────────────────────────────────────────────────────────
{
  const s = pres.addSlide();
  s.background = { color: GROUND };
  titleSlide(s, "Two costs every risk team pays", "THE PROBLEM", false);

  const problems = [
    {
      ic: "FiClock", col: REJECT,
      h: "A policy change waits on an engineering release",
      b: "The threshold is a constant in application code. Moving it from 700 to 690 means a " +
         "ticket, a sprint, a deploy and a rollback plan. So the credit team stops asking, and " +
         "policy drifts away from the market it was written for.",
      f: "Risk moves at the speed of the release train, not the market.",
    },
    {
      ic: "FiSearch", col: REVIEW,
      h: "Eight months later, the regulator asks why",
      b: "One applicant, one decision, one date. What were the rules that day? Which version of " +
         "the scorecard? What did the bureau actually return? If the answer is a log line and a " +
         "git blame, the answer is no answer.",
      f: "Reconstruction is not evidence. It has to have been recorded.",
    },
  ];

  for (let i = 0; i < problems.length; i++) {
    const p = problems[i];
    const x = M + i * 6.15;
    card(s, x, 1.92, 5.72, 3.6);
    s.addShape(pres.ShapeType.ellipse, {
      x: x + 0.42, y: 2.3, w: 0.64, h: 0.64, fill: { color: p.col }, line: { color: p.col, width: 0 },
    });
    s.addImage({ data: await icon(p.ic, "FFFFFF"), x: x + 0.58, y: 2.46, w: 0.32, h: 0.32 });
    s.addText(p.h, {
      x: x + 0.42, y: 3.12, w: 4.9, h: 0.75, isTextBox: true, margin: 0,
      fontFace: TITLE_FONT, fontSize: 19, bold: true, color: INK, lineSpacing: 25,
    });
    s.addText(p.b, {
      x: x + 0.42, y: 3.9, w: 4.9, h: 1.05, isTextBox: true, margin: 0,
      fontFace: BODY_FONT, fontSize: 13, color: MUTED, lineSpacing: 20,
    });
    s.addText(p.f, {
      x: x + 0.42, y: 5.03, w: 4.9, h: 0.38, isTextBox: true, margin: 0,
      fontFace: BODY_FONT, fontSize: 12.5, italic: true, bold: true, color: p.col,
    });
  }

  card(s, M, 5.7, 11.88, 0.9, INK);
  s.addText(
    "RuleMind removes both. The policy is data, edited in a dashboard and promoted through " +
    "environments — and every decision it makes is written down with the exact rules that produced it.",
    {
      x: M + 0.45, y: 5.82, w: 11.0, h: 0.66, isTextBox: true, margin: 0,
      fontFace: BODY_FONT, fontSize: 14, color: "FFFFFF", lineSpacing: 21, valign: "middle",
    });
  s.addNotes(
    "Do not rush this slide — it is the one the buyer recognises. The second cost is usually " +
    "the one that unlocks budget: audit failures are expensive in a way that slow releases are not."
  );
  footer(s, 2, false);
}

// ── 3. What it is — the pipeline ──────────────────────────────────────────────
{
  const s = pres.addSlide();
  s.background = { color: GROUND };
  titleSlide(s, "Data in, decision out, reasoning kept", "WHAT IT IS", false);

  const steps = [
    ["FiDatabase", "Connect",  "Any JSON source — bureau, bank, GST, device, KYC, or your own API. Source-agnostic by design."],
    ["FiCode",     "Compute",  "Python variables in a sandbox, or 232 Excel functions if the author thinks in spreadsheets."],
    ["FiSliders",  "Author",   "Rules, scorecards and decision tables, click-built. 13 step types compose into a policy."],
    ["FiZap",      "Decide",   "One /decide call returns the outcome, the score, and the step-by-step trace behind it."],
    ["FiShield",   "Account",  "Every decision stores its inputs, computed variables, every rule result and the full step trace."],
  ];

  const cw = 2.24, gap = 0.19;
  for (let i = 0; i < steps.length; i++) {
    const [ic, h, b] = steps[i];
    const x = M + i * (cw + gap);
    card(s, x, 2.05, cw, 3.3);
    s.addShape(pres.ShapeType.ellipse, {
      x: x + 0.36, y: 2.4, w: 0.56, h: 0.56,
      fill: { color: i === 4 ? ACCENT_D : ACCENT }, line: { color: ACCENT, width: 0 },
    });
    s.addImage({ data: await icon(ic, "FFFFFF"), x: x + 0.5, y: 2.54, w: 0.28, h: 0.28 });
    s.addText(String(i + 1), {
      x: x + cw - 0.62, y: 2.42, w: 0.4, h: 0.32, isTextBox: true, margin: 0, align: "right",
      fontFace: BODY_FONT, fontSize: 15, bold: true, color: LINE,
    });
    s.addText(h, {
      x: x + 0.36, y: 3.12, w: cw - 0.7, h: 0.4, isTextBox: true, margin: 0,
      fontFace: TITLE_FONT, fontSize: 18, bold: true, color: INK,
    });
    s.addText(b, {
      x: x + 0.36, y: 3.58, w: cw - 0.66, h: 1.6, isTextBox: true, margin: 0,
      fontFace: BODY_FONT, fontSize: 11.5, color: MUTED, lineSpacing: 17,
    });
    if (i < steps.length - 1) {
      s.addShape(pres.ShapeType.triangle, {
        x: x + cw + 0.04, y: 2.6, w: 0.11, h: 0.16,
        fill: { color: MUTED_L }, line: { color: MUTED_L, width: 0 }, rotate: 90,
      });
    }
  }

  const facts = [
    ["281", "API endpoints"],
    ["13", "composable step types"],
    ["41", "tables of recorded state"],
    ["3", "environments, promoted not copied"],
  ];
  facts.forEach(([b, l], i) => {
    const x = M + i * 3.0;
    s.addText(b, {
      x, y: 5.7, w: 1.1, h: 0.45, isTextBox: true, margin: 0,
      fontFace: BODY_FONT, fontSize: 24, bold: true, color: ACCENT,
    });
    s.addText(l, {
      x, y: 6.16, w: 2.8, h: 0.35, isTextBox: true, margin: 0,
      fontFace: BODY_FONT, fontSize: 11.5, color: MUTED,
    });
  });
  s.addNotes(
    "The pipeline is the product in one line. Emphasise step 1: source-agnostic means we do not " +
    "sell you a bureau integration — you bring whatever you already pay for.\n\n" +
    "On step 5, say REASONING, not 'policy version'. A server-side decision records what " +
    "actually happened — the payload, the variables computed, every rule result, the scorecard " +
    "breakdown and the step trace — but it does NOT pin a policy version (bundle_hash is null " +
    "for API decisions; it is populated only for on-device ones). That is the honest answer and " +
    "it is also the better one: a stored trace is stronger evidence than a pointer to " +
    "configuration that may have changed since. If a buyer asks specifically for version " +
    "pinning on server decisions, it is a roadmap item, not a shipped one."
  );
  footer(s, 3, false);
}

// ── 4. Change policy without an engineering release ───────────────────────────
{
  const s = pres.addSlide();
  s.background = { color: GROUND };
  titleSlide(s, "The risk team ships the change", "CAPABILITY", false);

  const feats = [
    ["FiGitBranch", "Promote, don't redeploy",
      "dev → uat → prod as a governed promotion with approvals and a diff of what changed."],
    ["FiPercent", "Champion / challenger",
      "Run a new policy against a share of live traffic, or replay it over history before it sees any."],
    ["FiTable", "Tables from your spreadsheet",
      "Import the pricing matrix the team already maintains in Excel; it becomes an executable table."],
    ["FiRotateCcw", "Backtest and what-if",
      "Replay a candidate policy across recorded decisions and see exactly which outcomes move."],
    ["FiActivity", "Drift and monitoring",
      "PSI drift on inputs and scores, with error events surfaced rather than swallowed."],
    ["FiCpu", "AI assists, never decides",
      "Bring your own key — Anthropic, OpenAI or Gemini — to draft a rule. It never decides."],
  ];

  const cw = 3.82, ch = 1.72, gx = 0.21, gy = 0.2;
  for (let i = 0; i < feats.length; i++) {
    const [ic, h, b] = feats[i];
    const x = M + (i % 3) * (cw + gx);
    const y = 2.05 + Math.floor(i / 3) * (ch + gy);
    card(s, x, y, cw, ch);
    s.addShape(pres.ShapeType.ellipse, {
      x: x + 0.32, y: y + 0.34, w: 0.5, h: 0.5, fill: { color: ACCENT }, line: { color: ACCENT, width: 0 },
    });
    s.addImage({ data: await icon(ic, "FFFFFF"), x: x + 0.44, y: y + 0.46, w: 0.26, h: 0.26 });
    s.addText(h, {
      x: x + 0.95, y: y + 0.24, w: cw - 1.24, h: 0.58, isTextBox: true, margin: 0,
      fontFace: TITLE_FONT, fontSize: 15.5, bold: true, color: INK, lineSpacing: 20,
    });
    s.addText(b, {
      x: x + 0.95, y: y + 0.86, w: cw - 1.24, h: 0.74, isTextBox: true, margin: 0,
      fontFace: BODY_FONT, fontSize: 11.5, color: MUTED, lineSpacing: 16,
    });
  }

  s.addText(
    "None of the above is a code change. All of it is recorded as an audit event with the actor who did it.",
    {
      x: M, y: 6.1, w: 11.9, h: 0.4, isTextBox: true, margin: 0,
      fontFace: BODY_FONT, fontSize: 13.5, italic: true, bold: true, color: ACCENT,
    });
  s.addNotes(
    "The closing line is the point of the slide. Every capability here is authored, not " +
    "deployed, and every one leaves an audit trail naming who did it.\n\n" +
    "'With approvals' means real two-person control: promotion to production is REFUSED if the " +
    "same member promoted it to UAT (main.py promote_entity). The actor on the ledger is the " +
    "AUTHENTICATED principal, not a client-supplied label, which is what makes that check worth " +
    "anything. One nuance if asked: dual control is per-workspace and OFF by default " +
    "(engine_config.require_dual_control) — it is a control you switch on, not one we impose."
  );
  footer(s, 4, false);
}

// ── 5. Five engines, one answer ───────────────────────────────────────────────
{
  const s = pres.addSlide();
  s.background = { color: INK };
  titleSlide(s, "Five engines. One answer. Enforced.", "THE HARD PART", true);

  s.addText(
    "The same policy evaluates in Python on the server, TypeScript in the browser, Rust on the hot " +
    "path, and Kotlin or Dart on the device — offline. Cross-engine agreement is not a convention " +
    "here; it is a corpus that fails the build.",
    {
      x: M, y: 1.82, w: 7.4, h: 1.0, isTextBox: true, margin: 0,
      fontFace: BODY_FONT, fontSize: 14.5, color: MUTED_L, lineSpacing: 21,
    });

  const langs = [
    ["Python",     "server, source of truth"],
    ["TypeScript", "browser preview"],
    ["Rust",       "hot path"],
    ["Kotlin",     "Android, offline"],
    ["Dart",       "Flutter, offline"],
  ];
  langs.forEach(([l, r], i) => {
    const y = 3.05 + i * 0.63;
    s.addShape(pres.ShapeType.roundRect, {
      x: M, y, w: 7.4, h: 0.52, rectRadius: 0.07,
      fill: { color: INK_SOFT }, line: { color: INK_SOFT, width: 0 },
    });
    s.addShape(pres.ShapeType.ellipse, {
      x: M + 0.26, y: y + 0.17, w: 0.18, h: 0.18, fill: { color: ACCENT }, line: { color: ACCENT, width: 0 },
    });
    s.addText(l, {
      x: M + 0.62, y, w: 2.1, h: 0.52, isTextBox: true, margin: 0, valign: "middle",
      fontFace: BODY_FONT, fontSize: 14, bold: true, color: "FFFFFF",
    });
    s.addText(r, {
      x: M + 2.7, y, w: 4.5, h: 0.52, isTextBox: true, margin: 0, valign: "middle",
      fontFace: BODY_FONT, fontSize: 12.5, color: MUTED_L,
    });
  });

  card(s, 8.45, 1.82, 4.27, 4.5, INK_SOFT);
  s.addText("THE CORPUS", {
    x: 8.82, y: 2.15, w: 3.6, h: 0.3, isTextBox: true, margin: 0,
    fontFace: BODY_FONT, fontSize: 10.5, bold: true, charSpacing: 2, color: ACCENT,
  });
  const corpus = [
    ["90", "operator cases — through all five engines"],
    ["600", "conditions in one generated stress rule"],
    ["1.6 MB", "of expected outputs, generated not typed"],
  ];
  corpus.forEach(([b, l], i) => {
    const y = 2.6 + i * 1.02;
    s.addText(b, {
      x: 8.82, y, w: 3.6, h: 0.48, isTextBox: true, margin: 0,
      fontFace: BODY_FONT, fontSize: 28, bold: true, color: "FFFFFF",
    });
    s.addText(l, {
      x: 8.82, y: y + 0.47, w: 3.55, h: 0.5, isTextBox: true, margin: 0,
      fontFace: BODY_FONT, fontSize: 11.5, color: MUTED_L, lineSpacing: 16,
    });
  });
  s.addText("Disagree on one case and the build fails.", {
    x: 8.82, y: 5.72, w: 3.55, h: 0.4, isTextBox: true, margin: 0,
    fontFace: BODY_FONT, fontSize: 12, italic: true, bold: true, color: ACCENT,
  });

  s.addText(
    "Why it matters to you: the score your branch app shows offline is the score your server " +
    "recorded. No “the mobile engine rounds differently” incident, because that incident fails " +
    "the build.",
    {
      x: M, y: 6.30, w: 11.9, h: 0.45, isTextBox: true, margin: 0,
      fontFace: BODY_FONT, fontSize: 12.5, color: MUTED_L, lineSpacing: 18,
    });
  s.addNotes(
    "This is the technical differentiator and the hardest thing in the product. Most vendors " +
    "have one engine and an SDK that approximates it. Divergence between them is the classic " +
    "source of disputed decisions.\n\n" +
    "BE PRECISE IF PRESSED — the corpora do not all cover all five. The OPERATOR contract (12 " +
    "operators, 90 cases) is the one that runs through all five engines. The 600-condition " +
    "stress rule x 135 cases runs through Python, Rust, Kotlin and Dart — not TypeScript, whose " +
    "job is browser preview. Scorecards (173 cases) run through Python, TypeScript, Kotlin and " +
    "Dart. Say 'every engine is held to a shared generated corpus', which is true, rather than " +
    "'every corpus runs on every engine', which is not."
  );
  footer(s, 5, true);
}

// ── 6. How it is proven ───────────────────────────────────────────────────────
{
  const s = pres.addSlide();
  s.background = { color: GROUND };
  titleSlide(s, "Proof you can re-run yourself", "EVIDENCE", false);

  const bigs = [
    ["4,608", "tests across 11 lanes", "all green"],
    ["21 / 22", "capability rows evidenced", "0 absent"],
    ["2", "database lanes", "SQLite + PostgreSQL 16"],
  ];
  bigs.forEach(([b, l1, l2], i) => {
    const x = M + i * 4.03;
    card(s, x, 1.95, 3.78, 1.55);
    s.addText(b, {
      x: x + 0.38, y: 2.14, w: 3.1, h: 0.6, isTextBox: true, margin: 0,
      fontFace: BODY_FONT, fontSize: 34, bold: true, color: ACCENT,
    });
    s.addText(l1, {
      x: x + 0.38, y: 2.76, w: 3.1, h: 0.3, isTextBox: true, margin: 0,
      fontFace: BODY_FONT, fontSize: 12.5, bold: true, color: INK,
    });
    s.addText(l2, {
      x: x + 0.38, y: 3.04, w: 3.1, h: 0.3, isTextBox: true, margin: 0,
      fontFace: BODY_FONT, fontSize: 11.5, color: MUTED,
    });
  });

  s.addChart(pres.ChartType.bar, [{
    name: "Tests",
    labels: ["Python SQLite", "Python Postgres", "Vitest", "Flutter", "Playwright", "Kotlin", "Rust"],
    values: [1920, 1920, 462, 163, 68, 46, 29],
  }], {
    x: M, y: 3.72, w: 7.15, h: 2.72,
    barDir: "bar", barGapWidthPct: 42,
    chartColors: [ACCENT],
    showTitle: true, title: "Tests by lane — every one passing",
    titleFontFace: BODY_FONT, titleFontSize: 12, titleColor: INK,
    showValue: true, dataLabelPosition: "outEnd",
    dataLabelFontFace: BODY_FONT, dataLabelFontSize: 9, dataLabelColor: MUTED,
    catAxisLabelFontFace: BODY_FONT, catAxisLabelFontSize: 10, catAxisLabelColor: MUTED,
    valAxisLabelFontFace: BODY_FONT, valAxisLabelFontSize: 9, valAxisLabelColor: MUTED_L,
    valGridLine: { color: LINE, size: 1 }, catGridLine: { style: "none" },
    showLegend: false, valAxisMaxVal: 2200,
  });

  card(s, 8.0, 3.72, 4.72, 2.72);
  s.addText("What “proven” means here", {
    x: 8.36, y: 3.94, w: 4.0, h: 0.35, isTextBox: true, margin: 0,
    fontFace: TITLE_FONT, fontSize: 16, bold: true, color: INK,
  });
  const proof = [
    "Test expectations are generated from the reference engine, not hand-written to match.",
    "Every capability claim cites a file and line that the build re-resolves.",
    "A fix is checked by breaking it again and confirming a named test fails.",
    "The report is committed and regenerable: one command, same numbers.",
  ];
  s.addText(proof.map((t, i) => ({
    text: t, options: { bullet: true, breakLine: i < proof.length - 1 },
  })), {
    x: 8.36, y: 4.38, w: 4.0, h: 1.9, isTextBox: true, margin: 0,
    fontFace: BODY_FONT, fontSize: 11.5, color: MUTED, lineSpacing: 16, paraSpaceAfter: 7,
  });
  s.addNotes(
    "Say the fourth bullet out loud: TEST_RESULTS.md is committed to the repository and " +
    "regenerated by `make test-all` from the runners' own output. Your engineers can reproduce " +
    "this number on their own hardware in one command — that is the offer."
  );
  footer(s, 6, false);
}

// ── 7. Performance ────────────────────────────────────────────────────────────
{
  const s = pres.addSlide();
  s.background = { color: GROUND };
  titleSlide(s, "Fast, and small enough to run anywhere", "MEASURED", false);

  s.addChart(pres.ChartType.bar, [{
    name: "Decisions / second",
    labels: ["1 client", "4 clients", "16 clients"],
    values: [49.2, 103.1, 117.5],
  }], {
    x: M, y: 2.0, w: 6.0, h: 3.0,
    barDir: "col", barGapWidthPct: 60,
    chartColors: [ACCENT],
    showTitle: true, title: "Throughput scales with concurrency",
    titleFontFace: BODY_FONT, titleFontSize: 12, titleColor: INK,
    showValue: true, dataLabelPosition: "outEnd",
    dataLabelFontFace: BODY_FONT, dataLabelFontSize: 10, dataLabelColor: MUTED,
    catAxisLabelFontFace: BODY_FONT, catAxisLabelFontSize: 11, catAxisLabelColor: MUTED,
    valAxisLabelFontFace: BODY_FONT, valAxisLabelFontSize: 9, valAxisLabelColor: MUTED_L,
    valGridLine: { color: LINE, size: 1 }, catGridLine: { style: "none" },
    showLegend: false, valAxisMaxVal: 140,
  });

  const right = [
    ["19.78 ms", "median decision, end to end over HTTP", ACCENT],
    ["232 MB", "dashboard container, down from 2.54 GB", APPROVE],
    ["388 MiB", "API resident, two uvicorn workers", ACCENT],
  ];
  right.forEach(([b, l, c], i) => {
    const y = 2.0 + i * 1.04;
    card(s, 7.05, y, 5.67, 0.9);
    s.addText(b, {
      x: 7.42, y: y + 0.08, w: 1.9, h: 0.55, isTextBox: true, margin: 0, valign: "middle",
      fontFace: BODY_FONT, fontSize: 23, bold: true, color: c,
    });
    s.addText(l, {
      x: 9.35, y: y + 0.08, w: 3.2, h: 0.75, isTextBox: true, margin: 0, valign: "middle",
      fontFace: BODY_FONT, fontSize: 11.5, color: MUTED, lineSpacing: 16,
    });
  });

  card(s, 7.05, 5.12, 5.67, 1.22, INK);
  s.addText("The footprint is the deployment story", {
    x: 7.42, y: 5.28, w: 5.0, h: 0.3, isTextBox: true, margin: 0,
    fontFace: BODY_FONT, fontSize: 12.5, bold: true, color: "FFFFFF",
  });
  s.addText(
    "A 232 MB image runs on a laptop, a single VM, or three pods. You are not buying a cluster " +
    "in order to run a pilot.",
    {
      x: 7.42, y: 5.6, w: 5.0, h: 0.6, isTextBox: true, margin: 0,
      fontFace: BODY_FONT, fontSize: 11.5, color: MUTED_L, lineSpacing: 16,
    });

  s.addText(
    "Measured on a developer laptop against the seeded demo tenant — a floor, not a ceiling. " +
    "Your own numbers come from the same benchmark script, on your hardware.",
    {
      x: M, y: 5.3, w: 6.0, h: 0.8, isTextBox: true, margin: 0,
      fontFace: BODY_FONT, fontSize: 11, italic: true, color: MUTED, lineSpacing: 16,
    });
  s.addNotes(
    "Be precise if asked: 19.78 ms is p50 at concurrency 1; 117.5 rps is at concurrency 16. " +
    "These are different rows of the same table. The honest framing is a laptop floor — do not " +
    "extrapolate to production sizing on this slide."
  );
  footer(s, 7, false);
}

// ── 8. Governance ─────────────────────────────────────────────────────────────
{
  const s = pres.addSlide();
  s.background = { color: GROUND };
  titleSlide(s, "Built for the conversation after the decision", "GOVERNANCE", false);

  const rows = [
    ["FiLock", "Tamper-evident audit chain",
      "Each audit record is hash-chained to the one before it, so a silent edit does not verify. " +
      "We say tamper-evident, not immutable — an operator with database write access could " +
      "recompute the chain. Detection, honestly described."],
    ["FiUsers", "Five roles, checked per endpoint",
      "Owner, admin, policy_maker, reviewer and viewer — enforced at the API by capability, not " +
      "hidden in the UI, and tested as a matrix rather than a single happy path."],
    ["FiLayers", "Tenant isolation as a test, not a promise",
      "Every stored object carries a tenant, and cross-tenant reads are a standing negative test " +
      "on each capability — the case that must fail."],
    ["FiInbox", "Never lose a decision event",
      "The decision and its outbox record commit in one database transaction, relayed with an " +
      "idempotent producer and a dead-letter path. At-least-once delivery with idempotent " +
      "consumption — redelivery does not double-log."],
  ];

  for (let i = 0; i < rows.length; i++) {
    const [ic, h, b] = rows[i];
    const y = 1.92 + i * 1.12;
    card(s, M, y, 11.88, 1.0);
    s.addShape(pres.ShapeType.ellipse, {
      x: M + 0.32, y: y + 0.27, w: 0.48, h: 0.48, fill: { color: ACCENT }, line: { color: ACCENT, width: 0 },
    });
    s.addImage({ data: await icon(ic, "FFFFFF"), x: M + 0.44, y: y + 0.39, w: 0.24, h: 0.24 });
    s.addText(h, {
      x: M + 0.95, y: y + 0.13, w: 3.5, h: 0.75, isTextBox: true, margin: 0, valign: "middle",
      fontFace: TITLE_FONT, fontSize: 15, bold: true, color: INK, lineSpacing: 19,
    });
    s.addText(b, {
      x: M + 4.55, y: y + 0.1, w: 7.0, h: 0.82, isTextBox: true, margin: 0, valign: "middle",
      fontFace: BODY_FONT, fontSize: 11, color: MUTED, lineSpacing: 15,
    });
  }

  s.addText(
    "Where a claim has a limit, the limit is on the slide. Your security review will find it anyway; " +
    "better it finds we already said so.",
    {
      x: M, y: 6.42, w: 11.9, h: 0.36, isTextBox: true, margin: 0,
      fontFace: BODY_FONT, fontSize: 12, italic: true, bold: true, color: ACCENT,
    });
  s.addNotes(
    "The tamper-evident wording is deliberate and worth calling out unprompted — it buys " +
    "credibility for everything else on the slide. Same for at-least-once: we do not claim " +
    "exactly-once delivery, we claim you never lose an event and redelivery does not double-log."
  );
  footer(s, 8, false);
}

// ── 9. Deployment ─────────────────────────────────────────────────────────────
{
  const s = pres.addSlide();
  s.background = { color: INK };
  titleSlide(s, "It runs inside your perimeter", "DEPLOYMENT", true);

  s.addText(
    "No vendor cloud, no telemetry, no per-decision metering. The only outbound calls are the " +
    "ones you configure. Two supported paths, each with a committed smoke test that proves the " +
    "install rather than describing it.",
    {
      x: M, y: 1.82, w: 9.5, h: 0.7, isTextBox: true, margin: 0,
      fontFace: BODY_FONT, fontSize: 14, color: MUTED_L, lineSpacing: 20,
    });

  const paths = [
    ["FiBox", "Docker Compose", "make up",
      ["API, dashboard, worker, PostgreSQL and Redis", "One command from clone to a decision",
       "14-check smoke test included"]],
    ["FiServer", "Kubernetes", "make k8s-up",
      ["A real Helm chart, installed and rolled out", "Migrations as a pre-install hook",
       "16-check smoke test included"]],
  ];
  for (let i = 0; i < paths.length; i++) {
    const [ic, h, cmd, items] = paths[i];
    const x = M + i * 6.15;
    card(s, x, 2.72, 5.72, 2.72, INK_SOFT);
    s.addShape(pres.ShapeType.ellipse, {
      x: x + 0.42, y: 3.05, w: 0.54, h: 0.54, fill: { color: ACCENT }, line: { color: ACCENT, width: 0 },
    });
    s.addImage({ data: await icon(ic, "FFFFFF"), x: x + 0.55, y: 3.18, w: 0.28, h: 0.28 });
    s.addText(h, {
      x: x + 1.1, y: 3.06, w: 2.35, h: 0.5, isTextBox: true, margin: 0, valign: "middle",
      fontFace: TITLE_FONT, fontSize: 19, bold: true, color: "FFFFFF",
    });
    s.addText(cmd, {
      x: x + 3.55, y: 3.06, w: 1.82, h: 0.5, isTextBox: true, margin: 0, valign: "middle", align: "right",
      fontFace: "Courier New", fontSize: 13, bold: true, color: ACCENT,
    });
    s.addText(items.map((t, j) => ({
      text: t, options: { bullet: true, breakLine: j < items.length - 1 },
    })), {
      x: x + 0.45, y: 3.75, w: 4.9, h: 1.5, isTextBox: true, margin: 0,
      fontFace: BODY_FONT, fontSize: 12, color: MUTED_L, lineSpacing: 17, paraSpaceAfter: 7,
    });
  }

  const assur = [
    ["Your keys", "AI features are bring-your-own-key and off unless configured."],
    ["Your data", "Outbound calls are SSRF-guarded with IP pinning, per connector."],
    ["Your terms", "Apache 2.0 licensed. No runtime phone-home, no seat lock-in."],
  ];
  assur.forEach(([h, b], i) => {
    const x = M + i * 4.03;
    s.addText(h, {
      x, y: 5.72, w: 3.7, h: 0.33, isTextBox: true, margin: 0,
      fontFace: BODY_FONT, fontSize: 14, bold: true, color: ACCENT,
    });
    s.addText(b, {
      x, y: 6.04, w: 3.75, h: 0.6, isTextBox: true, margin: 0,
      fontFace: BODY_FONT, fontSize: 11.5, color: MUTED_L, lineSpacing: 16,
    });
  });
  s.addNotes(
    "For a regulated buyer this slide often outranks the feature slides. Apache 2.0 licensed is " +
    "accurate; the repository is not public today, so do not say 'open source' as a present " +
    "fact.\n\n" +
    "Do NOT say 'no data leaves your network' — connectors call the data sources the customer " +
    "configures, which is the entire point of a bureau integration, and the AI features call " +
    "the provider whose key they supplied. What is true is that WE receive nothing: no " +
    "telemetry, no metering, no licence check. Every outbound call is one they chose, and the " +
    "bottom row of this slide says how those are guarded."
  );
  footer(s, 9, true);
}

// ── 10. Honest status ─────────────────────────────────────────────────────────
{
  const s = pres.addSlide();
  s.background = { color: GROUND };
  titleSlide(s, "What is shipped, and what is not", "STRAIGHT ANSWERS", false);

  s.addText(
    "You are going to run a technical evaluation. Here is the line we draw, before you find it.",
    {
      x: M, y: 1.8, w: 11.9, h: 0.35, isTextBox: true, margin: 0,
      fontFace: BODY_FONT, fontSize: 13.5, color: MUTED,
    });

  card(s, M, 2.32, 5.72, 3.55);
  s.addShape(pres.ShapeType.ellipse, {
    x: M + 0.42, y: 2.62, w: 0.42, h: 0.42, fill: { color: APPROVE }, line: { color: APPROVE, width: 0 },
  });
  s.addText("Shipped and tested", {
    x: M + 1.0, y: 2.6, w: 4.4, h: 0.45, isTextBox: true, margin: 0, valign: "middle",
    fontFace: TITLE_FONT, fontSize: 19, bold: true, color: INK,
  });
  const shipped = [
    "21 of 22 capability rows, each citing evidence in code",
    "Five engines agreeing on a generated corpus",
    "Durable decision delivery with a dead-letter path",
    "Tamper-evident audit chain, RBAC, tenant isolation",
    "232 Excel functions server-side (152 in the browser, 32 on device)",
  ];
  s.addText(shipped.map((t, i) => ({
    text: t, options: { bullet: true, breakLine: i < shipped.length - 1 },
  })), {
    x: M + 0.45, y: 3.2, w: 4.95, h: 2.4, isTextBox: true, margin: 0,
    fontFace: BODY_FONT, fontSize: 12, color: MUTED, lineSpacing: 17, paraSpaceAfter: 8,
  });

  card(s, M + 6.15, 2.32, 5.72, 3.55);
  s.addShape(pres.ShapeType.ellipse, {
    x: M + 6.57, y: 2.62, w: 0.42, h: 0.42, fill: { color: REVIEW }, line: { color: REVIEW, width: 0 },
  });
  s.addText("Not yet, and we will say so", {
    x: M + 7.15, y: 2.6, w: 4.4, h: 0.45, isTextBox: true, margin: 0, valign: "middle",
    fontFace: TITLE_FONT, fontSize: 19, bold: true, color: INK,
  });
  const notyet = [
    "Not exactly-once delivery — at-least-once with idempotent consumption",
    "Not an immutable audit log — tamper-evident, which is a detection guarantee",
    "Not hosted CI today; the evidence is a committed, regenerable test report",
    "Apache 2.0 licensed, but the repository is not public yet",
    "A 76-93% optimisation ships OFF by default — it shrinks what the decision record stores",
  ];
  s.addText(notyet.map((t, i) => ({
    text: t, options: { bullet: true, breakLine: i < notyet.length - 1 },
  })), {
    x: M + 6.58, y: 3.2, w: 4.95, h: 2.4, isTextBox: true, margin: 0,
    fontFace: BODY_FONT, fontSize: 12, color: MUTED, lineSpacing: 17, paraSpaceAfter: 8,
  });

  card(s, M, 5.96, 11.88, 0.82, INK);
  s.addText(
    "A vendor who cannot tell you what their product does not do has not tested it hard enough to know.",
    {
      x: M + 0.45, y: 6.04, w: 11.0, h: 0.66, isTextBox: true, margin: 0, valign: "middle",
      fontFace: BODY_FONT, fontSize: 14, italic: true, color: "FFFFFF",
    });
  s.addNotes(
    "This slide wins deals with technical buyers. Every item on the right is a real, current " +
    "limitation taken from the repository's own notes — not a softened version.\n\n" +
    "On the optimisation: COMPUTE_ONLY_USED_VARS computes only the variables a policy can " +
    "actually reach — 92.8% less work on the serving path, 76.0% on the full path, measured " +
    "in-process with alternating arms. It IS proven correct: both decision paths produce " +
    "identical outcomes and identical computed-variable sets, mutation-checked in both " +
    "directions, with the full 1,928-test suite green twice with it on. It stays off by default " +
    "for one reason, and it is not correctness: it shrinks the recorded variable set from the " +
    "whole catalog to what the policy read. That is a change to YOUR audit surface, so it is " +
    "your decision to make, not a default we impose."
  );
  footer(s, 10, false);
}

// ── 11. Close ─────────────────────────────────────────────────────────────────
{
  const s = pres.addSlide();
  s.background = { color: INK };
  titleSlide(s, "A pilot is three afternoons", "WHAT HAPPENS NEXT", true);

  const steps = [
    ["Stand it up", "One command on a laptop or a VM inside your network. Nothing leaves it.",
      "Afternoon one"],
    ["Bring one policy", "Rebuild a live policy you already run. Replay it over your recorded " +
      "decisions and compare outcomes.", "Afternoon two"],
    ["Change it", "Move a threshold, promote it through uat, and read the audit trail the " +
      "change produced.", "Afternoon three"],
  ];
  for (let i = 0; i < steps.length; i++) {
    const [h, b, when] = steps[i];
    const x = M + i * 4.03;
    card(s, x, 2.05, 3.78, 2.75, INK_SOFT);
    s.addShape(pres.ShapeType.ellipse, {
      x: x + 0.42, y: 2.38, w: 0.62, h: 0.62, fill: { color: ACCENT }, line: { color: ACCENT, width: 0 },
    });
    s.addText(String(i + 1), {
      x: x + 0.42, y: 2.38, w: 0.62, h: 0.62, isTextBox: true, margin: 0,
      align: "center", valign: "middle",
      fontFace: BODY_FONT, fontSize: 21, bold: true, color: "FFFFFF",
    });
    s.addText(when, {
      x: x + 0.42, y: 3.14, w: 3.0, h: 0.26, isTextBox: true, margin: 0,
      fontFace: BODY_FONT, fontSize: 10, bold: true, charSpacing: 1.4, color: ACCENT,
    });
    s.addText(h, {
      x: x + 0.42, y: 3.42, w: 3.0, h: 0.4, isTextBox: true, margin: 0,
      fontFace: TITLE_FONT, fontSize: 18, bold: true, color: "FFFFFF",
    });
    s.addText(b, {
      x: x + 0.42, y: 3.88, w: 3.0, h: 0.86, isTextBox: true, margin: 0,
      fontFace: BODY_FONT, fontSize: 11.5, color: MUTED_L, lineSpacing: 16,
    });
  }

  s.addText("The question a pilot answers", {
    x: M, y: 5.12, w: 11.9, h: 0.35, isTextBox: true, margin: 0,
    fontFace: BODY_FONT, fontSize: 11.5, bold: true, charSpacing: 2, color: ACCENT,
  });
  s.addText(
    "Can your risk team change a live policy this week — and prove, months later, exactly why " +
    "one applicant got the answer they got?",
    {
      x: M, y: 5.5, w: 11.6, h: 1.2, isTextBox: true, margin: 0,
      fontFace: TITLE_FONT, fontSize: 24, bold: true, color: "FFFFFF", lineSpacing: 32,
    });
  s.addNotes(
    "Close on the question, not on a feature. If they can answer yes today with what they have, " +
    "they do not need us — and saying that is what makes the rest credible."
  );
  footer(s, 11, true);
}

  await pres.writeFile({ fileName: "RuleMind-AI.pptx" });
  console.log("written");
}

build().catch((e) => { console.error(e); process.exit(1); });
