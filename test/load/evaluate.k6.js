import http from "k6/http";
import { check, sleep } from "k6";

export const options = {
  vus: 50,
  duration: "30s",
  thresholds: {
    // CORRECTNESS FIRST. Without this the script can be completely broken and still "pass":
    // `has outcome` below failed on 100% of requests for the life of this file (it read
    // `body.outcome`, but the rule-test endpoint nests it under `result`), and nobody saw it
    // because the workflow step carried continue-on-error.
    checks: ["rate>0.99"],
    http_req_failed: ["rate<0.01"],
    // p(95) measured HERE, against what this workflow actually starts: a single-worker uvicorn
    // on SQLite, from qa/mobile_e2e/start_fastapi.sh. Observed p(95)=1.64s at 50 VUs.
    //
    // The 500ms this used to assert came from a DIFFERENT configuration -- PostgreSQL, 4
    // workers, rate limiter off (E2E_TEST_PLAN.md section 11) -- so it was never a threshold
    // this job could meet, and failing it told you nothing except that the two setups differ.
    // 3s asserts the thing this job CAN assert: that it degrades rather than collapsing.
    // The real SLO number lives in docs/PERFORMANCE.md, measured where the configuration matches.
    http_req_duration: [`p(95)<${__ENV.P95_MS || 3000}`],
  },
};

const ruleId = __ENV.RULE_ID || "replace-me";
const baseUrl = __ENV.BASE_URL || "http://localhost:8080";
const apiKey = __ENV.API_KEY || "rm_live_devlocaltenantkey000000000000";

export default function () {
  const response = http.post(
    `${baseUrl}/api/v1/rules/${ruleId}/test`,
    JSON.stringify({
      bank: { summary: { avgBalance: 45200 } },
      bureau: { score: 720 },
      device: { risk_score: 0 },
    }),
    {
      headers: {
        "Content-Type": "application/json",
        "x-api-key": apiKey,
      },
    }
  );

  check(response, {
    "status 200": (value) => value.status === 200,
    // `result.outcome`, not `outcome`. POST /api/v1/rules/{id}/test returns
    // { rule, result: { outcome, passed, ... }, values, variable_results, expression }.
    "has outcome": (value) => {
      try {
        return JSON.parse(value.body)?.result?.outcome !== undefined;
      } catch {
        return false;
      }
    },
  });

  sleep(0.1);
}
