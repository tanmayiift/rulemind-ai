// Saturate the sandbox pool and assert no decision is derived from a null.
//
// WHY THIS EXISTS, and why no existing lens catches it.
//
// Two defects compounded into a single failure mode that only appears under load:
//
//   T1.2  A variable that times out returns {"value": None, "error": ...} rather than raising.
//         The executor discarded the error, so the variable became a null indistinguishable
//         from a legitimately-null value -- and logic.py returns False for every comparison
//         against None. So the direction a decision failed was decided by how the rule author
//         happened to phrase it: "reject if fraud_score >= 90" simply did not reject.
//
//   T2.2  future.result(timeout=) times out the WAIT, not the task. A wedged worker kept its
//         slot forever, a broken pool was never rebuilt, and SANDBOX_FALLBACK_MODE defaulted to
//         running customer Python in the API process with no timeout at all.
//
// Together: at peak load -- exactly when fraud volume is highest -- the system approved the most
// and logged nothing unusual. The unit tests now cover each half, but a unit test cannot
// reproduce the trigger, because the trigger is CONTENTION. `future.result(timeout=)` times out
// on WAITING, not on executing, so a saturated pool manufactures nulls out of perfectly healthy
// variables. You need real concurrent load to see it.
//
// This test therefore does something the throughput test (evaluate.k6.js) deliberately does not:
// it drives the pool past its capacity on purpose and then asserts a CORRECTNESS property, not a
// latency one. A 504 is a pass here. A slow response is a pass. An `approve` that came back with
// a variable error attached is a failure, and so is any 200 whose outcome was computed from an
// input that did not compute.
//
// Run:
//   k6 run -e BASE_URL=http://localhost:8080 -e API_KEY=... -e POLICY_ID=... \
//          test/load/saturation.k6.js
//
// Expect to run it against a pool deliberately smaller than the concurrency, e.g.
// SANDBOX_POOL_SIZE=2 with 40 VUs. That is the point.

import http from "k6/http";
import { check } from "k6";
import { Counter, Rate } from "k6/metrics";

// The metric that matters. Everything else here is diagnostic.
const unsoundDecisions = new Counter("unsound_decisions");
const failedVariables = new Rate("decisions_with_a_failed_variable");
const guardFired = new Counter("fail_closed_guard_fired");
const budgetExceeded = new Counter("evaluation_budget_exceeded_504");

export const options = {
  // Ramp well past the sandbox pool so requests queue and time out on the WAIT. Holding at the
  // peak matters more than reaching it: the wedge only shows once the queue is persistently deep.
  stages: [
    { duration: "10s", target: 20 },
    { duration: "40s", target: 60 },
    { duration: "20s", target: 60 },
    { duration: "10s", target: 0 },
  ],
  thresholds: {
    // The whole point of the run. Any unsound decision fails the test outright.
    unsound_decisions: ["count==0"],
    // Deliberately NOT a latency threshold. Under saturation, slow is correct behaviour --
    // shedding load with a 504 is the designed outcome, and gating on p(95) here would fail the
    // run for doing the right thing. evaluate.k6.js owns the latency contract.
    //
    // 5xx other than 504 is still a real failure, so it is checked per-response below.
    checks: ["rate>0.99"],
  },
};

const baseUrl = __ENV.BASE_URL || "http://localhost:8080";
const apiKey = __ENV.API_KEY || "rm_live_devlocaltenantkey000000000000";
const policyId = __ENV.POLICY_ID || "";

const headers = { "Content-Type": "application/json", "x-api-key": apiKey };

// Resolve a policy once, so the run does not depend on a hardcoded id the way evaluate.k6.js
// does (its RULE_ID defaults to the literal "replace-me", which 404s every request).
export function setup() {
  if (policyId) return { policyId };
  const res = http.get(`${baseUrl}/api/v1/policies`, { headers });
  if (res.status !== 200) {
    throw new Error(`cannot list policies: ${res.status} ${res.body}`);
  }
  const policies = JSON.parse(res.body);
  const prod = policies.filter((p) => p.status === "prod");
  const chosen = (prod.length ? prod : policies)[0];
  if (!chosen) throw new Error("no policy to decide against");
  return { policyId: chosen.id };
}

export default function (data) {
  const res = http.post(
    `${baseUrl}/api/v1/decide`,
    JSON.stringify({
      policyId: data.policyId,
      payload: { age: 34, bureau: { score: 720 } },
      userId: `k6-saturation-${__VU}-${__ITER}`,
    }),
    { headers, timeout: "60s" }
  );

  // 504 is the evaluation budget (T1.3) shedding load. That is the system working, not failing.
  if (res.status === 504) {
    budgetExceeded.add(1);
    check(res, { "504 is a clean budget rejection": (r) => r.body.includes("evaluation_budget") });
    return;
  }

  // 429 is the rate limiter. Also correct under load.
  if (res.status === 429) return;

  const ok = check(res, {
    "no 5xx": (r) => r.status < 500,
    "status 200": (r) => r.status === 200,
  });
  if (!ok || res.status !== 200) return;

  let body;
  try {
    body = JSON.parse(res.body);
  } catch (e) {
    unsoundDecisions.add(1);
    return;
  }

  // T1.2's contract, asserted from the outside. /decide now returns variable_errors alongside
  // variables precisely so a caller can tell a real null from a failed computation -- before,
  // the response hardcoded error:None and passed:True for every variable and the API simply lied.
  const errors = body.variable_errors || {};
  const hadFailure = Object.keys(errors).length > 0;
  failedVariables.add(hadFailure);

  const trace = body.trace || [];
  const guard = trace.filter((s) => s && s.type === "variable_error_guard");
  if (guard.length) guardFired.add(1);

  // THE ASSERTION. If any input failed to compute, the decision must not have landed on a
  // terminal outcome derived from it. The guard sends it to review; anything else means a null
  // reached a gate and decided something.
  if (hadFailure) {
    const sound = body.outcome === "review" && guard.length > 0;
    if (!sound) {
      unsoundDecisions.add(1);
      console.error(
        `UNSOUND: outcome=${body.outcome} with failed variables ${Object.keys(errors).join(",")} ` +
          `and ${guard.length} guard entries -- a null-derived decision escaped`
      );
    }
  }

  // The inverse also has to hold, or the guard could "pass" by firing on everything and turning
  // the product into a review queue.
  if (!hadFailure && guard.length > 0) {
    unsoundDecisions.add(1);
    console.error("UNSOUND: fail-closed guard fired with no failed variable");
  }

  // No sleep. Saturation is the experiment.
}

export function handleSummary(data) {
  const counter = (name) => (data.metrics[name] ? data.metrics[name].values.count : 0);
  const unsound = counter("unsound_decisions");
  const lines = [
    "",
    "  sandbox saturation run",
    "  ----------------------",
    `  decisions with a failed variable : ${
      data.metrics.decisions_with_a_failed_variable
        ? (data.metrics.decisions_with_a_failed_variable.values.rate * 100).toFixed(1) + "%"
        : "n/a"
    }`,
    `  fail-closed guard fired          : ${counter("fail_closed_guard_fired")}`,
    `  504 budget rejections            : ${counter("evaluation_budget_exceeded_504")}`,
    `  UNSOUND decisions                : ${unsound}`,
    "",
    unsound === 0
      ? "  PASS - no decision was derived from an input that failed to compute."
      : `  FAIL - ${unsound} decision(s) were derived from a null. This is the T1.2 + T2.2 regression.`,
    "",
  ];
  return { stdout: lines.join("\n") };
}
