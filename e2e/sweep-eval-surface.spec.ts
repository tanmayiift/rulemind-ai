/**
 * The EVALUATIONS / MODELS / A-B surface, driven through a browser.
 *
 * Before this file, `/evaluations`, `/models`, `/ab-tests` and `/simulation` had ZERO browser
 * coverage -- no spec in e2e/ ever called `goto()` on any of them. They are the pages a risk team
 * reads a promote/hold decision off, so "it renders" is not the interesting claim; "the number on
 * screen is the number the backend computed" is.
 *
 * Each test pairs a UI assertion with an API control on the SAME entity, so a failure says which
 * half is wrong. Two of them deliberately assert that something is ABSENT from the page while the
 * API control proves the backend sent it -- those pin real display gaps, and they are written to
 * go red the moment the gap is closed, which is the point.
 *
 * Non-destructive: every fixture is created and deleted by the test that uses it. Nothing seeded
 * is modified.
 */
import { expect, test, type APIRequestContext } from "@playwright/test";

import { API_BASE_URL } from "./env";

const EVAL_ID = "zz_sweep_eval_surface";
const EXP_ID = "zz_sweep_eval_experiment";

/** A clean, strongly rank-ordered set large enough that the metrics are stable. */
function scoredRows(n = 400) {
  // Deterministic LCG -- no dependence on Math.random, so a failure reproduces.
  let seed = 42;
  const next = () => ((seed = (seed * 1103515245 + 12345) % 2147483648) / 2147483648);
  const rows: Array<{ score: number; label: number }> = [];
  for (let i = 0; i < n; i += 1) {
    const label = next() < 0.3 ? 1 : 0;
    const base = label ? 0.72 : 0.3;
    const score = Math.min(0.999, Math.max(0.001, base + (next() - 0.5) * 0.3));
    rows.push({ score: Number(score.toFixed(4)), label });
  }
  return rows;
}

async function deleteQuietly(request: APIRequestContext, path: string) {
  await request.delete(`${API_BASE_URL}${path}`).catch(() => undefined);
}

test.describe("evaluations surface", () => {
  test.afterEach(async ({ request }) => {
    await deleteQuietly(request, `/api/v1/evaluations/${EVAL_ID}`);
  });

  test("running an evaluation from the form shows a gate verdict that matches the API", async ({
    page,
    request
  }) => {
    await page.goto("/evaluations");
    await expect(page.getByText("Run an evaluation")).toBeVisible();

    await page.getByPlaceholder("Q3 PD model").fill(EVAL_ID);
    const rowsBox = page.locator("textarea").first();
    await rowsBox.fill(JSON.stringify(scoredRows()));
    await page.getByRole("button", { name: /Run evaluation/i }).click();

    // The verdict badge is the thing a user acts on.
    const badge = page.getByText(/^Gate: (PASS|FAIL|UNKNOWN)$/);
    await expect(badge).toBeVisible({ timeout: 20_000 });
    const shown = (await badge.innerText()).replace("Gate: ", "").toLowerCase();

    // API control: the persisted verdict must be the one on screen.
    const listed = await request.get(`${API_BASE_URL}/api/v1/evaluations`);
    expect(listed.ok()).toBeTruthy();
    const record = (await listed.json()).find((e: { name: string }) => e.name === EVAL_ID);
    expect(record, "the evaluation the form just ran should be listed").toBeTruthy();
    expect(record.gate_status).toBe(shown);
  });

  test("a 20,000-row CSV is uploaded and evaluated, never pasted into the textarea", async ({
    page,
    request
  }) => {
    // The plan's finding: a risk team scoring a real portfolio could not use this screen, because
    // the only input was a <textarea>. A portfolio arrives as a file.
    const rows = scoredRows(20_000);
    const csv = ["score,label", ...rows.map((r) => `${r.score},${r.label}`)].join("\n");
    await page.goto("/evaluations");
    await page.getByPlaceholder("Q3 PD model").fill(EVAL_ID);
    await page.locator("#eval-dataset-file").setInputFiles({
      name: "portfolio.csv", mimeType: "text/csv", buffer: Buffer.from(csv)
    });
    await expect(page.getByTestId("eval-dataset-summary")).toContainText("20,000 rows · 2 columns");
    await expect(page.locator("textarea"), "the file must not be dumped into a textarea").toHaveCount(0);

    await page.getByRole("button", { name: /Run evaluation/i }).click();
    await expect(page.getByText(/^Gate: (PASS|FAIL|UNKNOWN)$/)).toBeVisible({ timeout: 60_000 });
    const listed = await request.get(`${API_BASE_URL}/api/v1/evaluations`);
    const record = (await listed.json()).find((e: { name: string }) => e.name === EVAL_ID);
    const detail = await (await request.get(`${API_BASE_URL}/api/v1/evaluations/${record.id}`)).json();
    expect(detail.metrics.n, "every uploaded row was evaluated").toBe(20_000);
  });

  test("a column the file does not have is named before any request is sent", async ({ page }) => {
    await page.goto("/evaluations");
    await page.locator("#eval-dataset-file").setInputFiles({
      name: "portfolio.csv", mimeType: "text/csv", buffer: Buffer.from("pd,default\n0.2,0\n0.8,1\n")
    });
    let posted = false;
    page.on("request", (req) => {
      if (req.method() === "POST" && req.url().includes("/api/v1/evaluations")) posted = true;
    });
    await page.getByRole("button", { name: /Run evaluation/i }).click();
    await expect(page.getByText("Not in portfolio.csv: score, label. Its columns are: pd, default.")).toBeVisible();
    expect(posted, "a doomed evaluation was sent to the server").toBe(false);
  });

  test("every promotion-gate check on screen matches the API's value and threshold", async ({
    page,
    request
  }) => {
    const created = await request.post(`${API_BASE_URL}/api/v1/evaluations`, {
      data: {
        id: EVAL_ID,
        name: EVAL_ID,
        task: "binary",
        config: { score_col: "score", label_col: "label" },
        rows: scoredRows()
      }
    });
    expect(created.ok()).toBeTruthy();
    const detail = await created.json();

    await page.goto("/evaluations");
    await page.getByText(EVAL_ID, { exact: false }).first().click();
    await expect(page.getByText("Promotion gate")).toBeVisible({ timeout: 20_000 });

    for (const check of detail.gate_result.checks) {
      const row = page.locator("tr", { hasText: check.check });
      await expect(row, `gate row "${check.check}" should be rendered`).toBeVisible();
      await expect(row).toContainText(check.passed ? "pass" : "fail");
    }
  });

  test("the confusion matrix and accuracy the API computes are actually rendered", async ({
    page,
    request
  }) => {
    // This began as a GAP marker: the backend computed all four cells, an accuracy and a
    // calibration curve, persisted them, and the page rendered none of them -- while the left
    // nav's own subtitle for this page promised "accuracy, drift, and confusion matrix". It is
    // now a guard, because the page renders them.
    const created = await request.post(`${API_BASE_URL}/api/v1/evaluations`, {
      data: {
        id: EVAL_ID,
        name: EVAL_ID,
        task: "binary",
        config: { score_col: "score", label_col: "label" },
        rows: scoredRows()
      }
    });
    expect(created.ok()).toBeTruthy();
    const detail = await created.json();

    // Control: the backend really did compute all four cells and an accuracy.
    expect(detail.metrics.confusion).toMatchObject({
      tp: expect.any(Number),
      fp: expect.any(Number),
      tn: expect.any(Number),
      fn: expect.any(Number),
      accuracy: expect.any(Number)
    });

    await page.goto("/evaluations");
    await page.getByText(EVAL_ID, { exact: false }).first().click();
    await expect(page.getByText("Promotion gate")).toBeVisible({ timeout: 20_000 });

    // The panel exists and carries the four cells, each labelled, so a reader cannot mistake a
    // transposed matrix for a correct one.
    const matrix = page.getByTestId("eval-confusion");
    await expect(matrix).toBeVisible();
    await expect(matrix).toContainText("Accuracy");
    for (const cell of ["TP", "FP", "TN", "FN"]) {
      await expect(matrix).toContainText(cell);
    }
    // The numbers on screen must be the API's, not a recomputation that could drift from it.
    await expect(matrix).toContainText(String(detail.metrics.confusion.tp));
    await expect(matrix).toContainText(String(detail.metrics.confusion.fn));

    await expect(page.getByTestId("eval-calibration")).toBeVisible();

    // PSI stays absent, and that is now DELIBERATE rather than an omission: model_evaluation.psi
    // has no callers, because PSI needs a reference distribution a single labelled set does not
    // have. The claim was removed from the subtitle and the router docstring instead of being
    // faked, and drift lives on /predictors, which does have a baseline.
    const body = await page.locator("main").innerText();
    const metricsOnly = body.split("Run an evaluation")[1] ?? body;
    expect(metricsOnly).not.toMatch(/\bPSI\b/);
  });
});

test.describe("models surface", () => {
  test("the models page agrees with the API about whether any model exists", async ({
    page,
    request
  }) => {
    // Deliberately NOT `if (models.length) { ...assert names... }`. The seeded e2e database has
    // no hosted models, so that branch never executed and a mutation blanking every model name
    // survived the test. Asserting the correspondence in BOTH directions always runs.
    const listed = await request.get(`${API_BASE_URL}/api/v1/models`);
    expect(listed.ok()).toBeTruthy();
    const models: Array<{ name: string }> = await listed.json();

    await page.goto("/models");
    // Role-scoped: "Upload model" is both the card heading and the submit button.
    await expect(page.getByRole("heading", { name: "Upload model" })).toBeVisible();

    const emptyState = page.getByText("No models yet");
    if (models.length === 0) {
      await expect(emptyState, "API returned no models, so the empty state must show").toBeVisible();
    } else {
      await expect(emptyState, "API returned models, so the empty state must NOT show").toHaveCount(0);
      for (const model of models.slice(0, 5)) {
        await expect(page.getByText(model.name, { exact: false }).first()).toBeVisible();
      }
    }
  });
});

test.describe("model formats", () => {
  const NAME = "zz_sweep_onnx_model";

  test.afterEach(async ({ request }) => {
    const listed = await request.get(`${API_BASE_URL}/api/v1/models`);
    for (const model of (await listed.json()) as Array<{ id: string; name: string }>) {
      if (model.name === NAME) await deleteQuietly(request, `/api/v1/models/${model.id}`);
    }
  });

  test("an ONNX model uploads through the page and is labelled by its detected format", async ({
    page,
    request
  }) => {
    await page.goto("/models");
    await page.getByPlaceholder("Risk classifier v2").fill(NAME);
    await page.locator('input[type="file"]').setInputFiles(
      "apps/python-executor/tests/fixtures/models/logistic.onnx");
    await page.getByRole("button", { name: "Upload model" }).click();
    await expect(page.getByText(`Uploaded “${NAME}”.`)).toBeVisible({ timeout: 20_000 });
    const card = page.locator("div").filter({ hasText: NAME }).filter({ hasText: "ONNX" }).first();
    await expect(card, "the model list must say ONNX, from the bytes, not a declared type").toBeVisible();

    const listed = await request.get(`${API_BASE_URL}/api/v1/models`);
    const model = ((await listed.json()) as Array<{ name: string; model_format: string }>).find((m) => m.name === NAME);
    expect(model?.model_format).toBe("onnx");
  });
});

test.describe("a/b experiments surface", () => {
  test.afterEach(async ({ request }) => {
    await deleteQuietly(request, `/api/v1/experiments/${EXP_ID}`);
  });

  test("the challenger lift and the promote/hold recommendation reach the page", async ({
    page,
    request
  }) => {
    // Another former GAP marker. The page read `champion_challenger` and `recommendation.action`;
    // the API emits `championChallenger` and a plain STRING. Two independent mismatches hiding
    // the same block, on a page whose subtitle promises "watch approval lift and significance,
    // then promote the winner". Both are fixed; this now guards the binding.
    const policies = await (await request.get(`${API_BASE_URL}/api/v1/policies`)).json();
    const created = await request.post(`${API_BASE_URL}/api/v1/experiments`, {
      data: {
        id: EXP_ID,
        name: EXP_ID,
        status: "draft",
        target_policy_id: policies[0]?.id,
        hash_key: "user_id",
        variants: [
          { id: "champion", role: "champion", weight: 50 },
          { id: "challenger", role: "challenger", weight: 50 }
        ]
      }
    });
    expect(created.ok()).toBeTruthy();

    // Control: the analysis the page is supposed to show.
    const results = await (
      await request.get(`${API_BASE_URL}/api/v1/experiments/${EXP_ID}/results`)
    ).json();
    expect(results).toHaveProperty("championChallenger");
    expect(results).not.toHaveProperty("champion_challenger");
    const challenger = results.championChallenger.challengers[0];
    expect(typeof challenger.recommendation).toBe("string");
    expect(["promote", "hold", "rollback"]).toContain(challenger.recommendation);

    await page.goto("/ab-tests");
    await page.getByRole("button", { name: EXP_ID }).click();
    await expect(page.getByText("Variants")).toBeVisible({ timeout: 20_000 });

    // The p-value DOES render -- proof the analytics call succeeded and the page has data.
    await expect(page.getByText("P-VALUE")).toBeVisible();

    // ...and so do the recommendation and the lift figure.
    const recommendations = page.getByTestId("ab-recommendations");
    await expect(recommendations).toBeVisible();
    // The value is the API's own verdict, rendered verbatim rather than re-derived in the page.
    await expect(recommendations).toContainText(challenger.recommendation);

    const challengerRow = page.locator("tr", { hasText: "challenger" }).first();
    await expect(challengerRow).toBeVisible();
    // A working binding renders the approval cell as e.g. "0%(+0)"; the broken one rendered "0%".
    await expect(challengerRow).toContainText("(+");
  });

  test("the promote button honours the safety gate instead of bypassing it", async ({
    page,
    request
  }) => {
    // This was pinned at the API only -- and that made it UNFALSIFIABLE for the defect it was
    // named after. The API honours `force` either way, so posting force:true and asserting 200
    // passes whether or not the page still hardcodes it. The defect was in the REQUEST THE PAGE
    // SENDS, so the page is what has to be driven. The API assertions below are kept as the
    // controls that make the page assertion meaningful.
    const policies = await (await request.get(`${API_BASE_URL}/api/v1/policies`)).json();
    await request.post(`${API_BASE_URL}/api/v1/experiments`, {
      data: {
        id: EXP_ID,
        name: EXP_ID,
        status: "draft",
        target_policy_id: policies[0]?.id,
        hash_key: "user_id",
        variants: [
          { id: "champion", role: "champion", weight: 50 },
          { id: "challenger", role: "challenger", weight: 50 }
        ]
      }
    });

    // What the gate says when it is allowed to speak: a challenger with no traffic is "hold".
    const honest = await request.post(
      `${API_BASE_URL}/api/v1/experiments/${EXP_ID}/promote`,
      { data: { variant_id: "challenger", force: false, promoted_by: "e2e" } }
    );
    expect(honest.status()).toBe(422);
    expect(await honest.text()).toContain("Promotion blocked");

    // Control: force=true really is an override the API accepts, so the page assertion below
    // is about the page's CHOICE, not about the endpoint being broken.
    const forced = await request.post(
      `${API_BASE_URL}/api/v1/experiments/${EXP_ID}/promote`,
      { data: { variant_id: "challenger", force: true, promoted_by: "e2e-control" } }
    );
    expect(forced.status()).toBe(200);

    // THE ASSERTION THIS TEST EXISTS FOR. Clicking Promote must surface the gate's refusal
    // rather than silently overriding it. Previously every row's button sent force:true, so the
    // gate -- which also blocks promoting the champion at all, since both checks live inside
    // `if not request.force:` -- was permanently disabled from the UI.
    const fresh = `${EXP_ID}-ui`;
    await request.post(`${API_BASE_URL}/api/v1/experiments`, {
      data: {
        id: fresh, name: fresh, status: "draft", target_policy_id: policies[0]?.id,
        hash_key: "user_id",
        variants: [
          { id: "champion", role: "champion", weight: 50 },
          { id: "challenger", role: "challenger", weight: 50 }
        ]
      }
    });
    await page.goto("/ab-tests");
    await page.getByRole("button", { name: fresh }).click();
    await expect(page.getByText("Variants")).toBeVisible({ timeout: 20_000 });
    await page.locator("tr", { hasText: "challenger" }).first()
      .getByRole("button", { name: /Promote/ }).click();

    const blocked = page.getByTestId("ab-promote-blocked");
    await expect(blocked).toBeVisible({ timeout: 20_000 });
    await expect(blocked).toContainText("Promotion blocked");
    // Overriding is still possible -- it is now a second, deliberate click rather than the
    // default. A gate that cannot be overridden gets bypassed in the code, which is what
    // happened here the first time.
    await expect(page.getByTestId("ab-promote-anyway")).toBeVisible();

    await deleteQuietly(request, `/api/v1/experiments/${fresh}`);
  });
});
