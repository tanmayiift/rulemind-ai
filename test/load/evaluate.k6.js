import http from "k6/http";
import { check, sleep } from "k6";

export const options = {
  vus: 50,
  duration: "30s"
};

const ruleId = __ENV.RULE_ID || "replace-me";
const baseUrl = __ENV.BASE_URL || "http://localhost:3001";

export default function () {
  const response = http.post(
    `${baseUrl}/api/v1/rules/${ruleId}/evaluate`,
    JSON.stringify({
      input: {
        event: "application_submitted",
        credit_score: 720
      }
    }),
    {
      headers: {
        "Content-Type": "application/json"
      }
    }
  );

  check(response, {
    "status 200": (value) => value.status === 200
  });

  sleep(1);
}
