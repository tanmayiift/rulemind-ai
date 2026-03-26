from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Optional
from urllib import error, request


@dataclass
class SdkDecideResponse:
    outcome: str
    score: Optional[float]
    variables: dict[str, Any]
    rule_results: list[dict[str, Any]]
    experiment: Optional[dict[str, Any]]
    latency_ms: int
    request_id: Optional[str]
    server_only_steps_skipped: list[str]
    raw: dict[str, Any]


@dataclass
class SdkBundleResponse:
    bundle_version: int
    encrypted_bundle: str
    encrypted_key: Optional[str]
    signature: str
    checksum: str
    compiled_at: str
    expires_at: str
    raw: dict[str, Any]


class RuleMindServerClient:
    def __init__(self, base_url: str, api_key: str | None = None, timeout: float = 5.0, sdk_version: str = "4.0.0") -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        self.sdk_version = sdk_version

    def _request(
        self,
        path: str,
        method: str = "GET",
        body: dict[str, Any] | list[Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> Any:
        payload = None if body is None else json.dumps(body).encode("utf-8")
        request_headers = {"Content-Type": "application/json", **(headers or {})}
        if self.api_key:
            request_headers["x-api-key"] = self.api_key

        req = request.Request(f"{self.base_url}{path}", data=payload, method=method, headers=request_headers)
        try:
            with request.urlopen(req, timeout=self.timeout) as response:
                if response.status == 304:
                    return None
                raw = response.read().decode("utf-8")
                return json.loads(raw) if raw else None
        except error.HTTPError as exc:
            if exc.code == 304:
                return None
            message = exc.read().decode("utf-8") or exc.reason
            raise RuntimeError(f"RuleMind request failed with {exc.code}: {message}") from exc

    def decide(
        self,
        policy_id: str,
        payload: dict[str, Any],
        *,
        user_id: str | None = None,
        request_id: str | None = None,
        sdk_version: str | None = None,
    ) -> SdkDecideResponse:
        data = self._request(
            "/sdk/v1/decide",
            method="POST",
            body={
                "policyId": policy_id,
                "payload": payload,
                "userId": user_id,
                "requestId": request_id,
                "sdkVersion": sdk_version,
            },
        )
        return SdkDecideResponse(
            outcome=str(data.get("outcome", "")),
            score=float(data["score"]) if data.get("score") is not None else None,
            variables=dict(data.get("variables", {})),
            rule_results=list(data.get("ruleResults", [])),
            experiment=data.get("experiment"),
            latency_ms=int(data.get("latencyMs", 0)),
            request_id=data.get("requestId"),
            server_only_steps_skipped=list(data.get("serverOnlyStepsSkipped", [])),
            raw=data,
        )

    def send_events(self, events: list[dict[str, Any]]) -> dict[str, int]:
        data = self._request("/sdk/v1/events", method="POST", body={"events": events})
        return {"received": int(data.get("received", 0)), "processed": int(data.get("processed", 0))}

    def get_bundle(self, *, bundle_version: int | None = None, client_public_key: str | None = None) -> Optional[SdkBundleResponse]:
        headers: dict[str, str] = {"x-sdk-version": self.sdk_version}
        if bundle_version is not None:
            headers["x-bundle-version"] = str(bundle_version)
        if client_public_key is not None:
            headers["x-client-public-key"] = client_public_key
        data = self._request("/sdk/v1/bundle", method="GET", headers=headers)
        if data is None:
            return None
        return SdkBundleResponse(
            bundle_version=int(data.get("bundleVersion", 0)),
            encrypted_bundle=str(data.get("encryptedBundle", "")),
            encrypted_key=data.get("encryptedKey"),
            signature=str(data.get("signature", "")),
            checksum=str(data.get("checksum", "")),
            compiled_at=str(data.get("compiledAt", "")),
            expires_at=str(data.get("expiresAt", "")),
            raw=data,
        )


RuleEngineClient = RuleMindServerClient
