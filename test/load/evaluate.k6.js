import http from "k6/http";
import { check, sleep } from "k6";

export const options = {
  vus: 50,
  duration: "30s",
  thresholds: {
    http_req_duration: ["p(95)<500"],
    http_req_failed: ["rate<0.01"],
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
    "has outcome": (value) => {
      const body = JSON.parse(value.body);
      return body.outcome !== undefined;
    },
  });

  sleep(0.1);
}
