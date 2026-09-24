"""The typed response contract for the endpoints an integration depends on (plan 5.1).

THE GAP. Every operation was annotated `-> Dict[str, Any]`, so `openapi.json` described 291
operations and ZERO typed 2xx responses. Three consequences, all real:

  * typed client generation was impossible, which is why all five SDKs are hand-written;
  * `scripts/openapi_gate.py` advertised "fails when a response field disappears" and had no
    response fields to compare, so that rule was inert;
  * nothing could notice two code paths returning different shapes for the same field -- and
    two did. `POST /api/v1/decide` returned `rule_results` as trace entries
    (`{step, result: {passed, ...}}`) on the executor path and as rule results
    (`{rule_id, passed, ...}`) on the FAST_DECIDE path. Writing `RuleResult` below is what
    found it: one schema cannot describe both.

HOW THESE ARE USED. Routes document them with `responses={200: {"model": X}}` and keep their
runtime serialisation unchanged. `response_model=X` would validate every response against the
model at request time -- on /decide that is a 500 for a real applicant the day a field the model
calls a string comes back null. The contract is enforced by tests instead:
`tests/test_api_contract.py` validates real responses from every path against these models AND
fails on any field the server sends that a model does not declare, so the document cannot fall
behind the code.

OPEN BY DESIGN. Every model allows extra fields (`additionalProperties: true` in the spec):
adding a response field is how this API grows without a version bump, so clients must ignore
keys they do not know. Completeness is the test's job, not the parser's. A model whose content
is genuinely open-ended -- the authored step inside a trace entry, an error's detail -- sets
`OPEN = True`, and the completeness check stops at it.
"""
from __future__ import annotations

from typing import Any, ClassVar, Dict, List, Optional, Union

from pydantic import BaseModel, ConfigDict, Field


class ApiModel(BaseModel):
    model_config = ConfigDict(extra="allow")
    # True where the content is open-ended by design; the completeness check does not descend.
    OPEN: ClassVar[bool] = False


# ── Errors ─────────────────────────────────────────────────────────────────


class ValidationIssue(ApiModel):
    loc: List[Union[str, int]] = Field(description="Where the problem is, e.g. [\"body\", \"policy_id\"].")
    msg: str
    type: str
    input: Any = None


class ErrorResponse(ApiModel):
    """Every error, from every layer. `error` is always present and always a short code to switch
    on; `detail` is context whose shape depends on the error -- a sentence, the list of
    validation issues on a 422, or an object for the few structured errors."""

    OPEN: ClassVar[bool] = True
    error: str = Field(description="A stable code, e.g. not_found, unauthorized, invalid_input.")
    detail: Any = Field(None, description="A human sentence, a list of ValidationIssue (422), or an object.")


class ValidationErrorResponse(ErrorResponse):
    error: str = Field(description="validation_error when the request itself is malformed (detail is a "
                                   "list of ValidationIssue); invalid_input when the decision payload "
                                   "fails the policy's input checks (detail is an object with `issues`).")
    detail: Union[List[ValidationIssue], str, Dict[str, Any]] = None


# ── Decisions ──────────────────────────────────────────────────────────────


class ConditionResult(ApiModel):
    variable_id: str
    variable_name: Optional[str]
    source_id: Optional[str]
    operator: str
    threshold: Any = Field(description="What the condition compared against.")
    value: Any = Field(description="The value the decision saw.")
    passed: bool
    group: str = Field(description="Logic of the group the condition sits in: AND or OR.")


class GroupResult(ApiModel):
    id: str
    logic: str
    passed: bool
    childCount: int


class RuleResult(ApiModel):
    """One rule's evaluation. The same shape on every path that decides: the executor, the
    FAST_DECIDE path, the stored decision row, and the on-device engines."""

    rule_id: str
    ruleId: str = Field(description="Same as rule_id; the spelling the on-device engines and /sdk/v1 use.")
    passed: bool
    outcome: str = Field(description="The outcome this rule voted for.")
    logic: str
    conditions: List[ConditionResult]
    groupResults: List[GroupResult]


class ScorecardLine(ApiModel):
    variable_id: str
    variable_name: Optional[str]
    source_id: Optional[str]
    value: Any
    active_range: Optional[Dict[str, Any]] = Field(description="The authored band the value fell in, if any.")
    points: Optional[float]
    weighted_points: Optional[float]
    weight: Optional[float]
    woe: Optional[float]
    coefficient: Optional[float]


class WoeDetail(ApiModel):
    variable_id: str
    variable_name: Optional[str]
    value: Any
    woe: Optional[float]
    iv: Optional[float]
    coefficient: Optional[float]
    contribution: Optional[float]
    event_rate: Optional[float]
    non_event_rate: Optional[float]


class ScorecardMetric(ApiModel):
    value: Any
    unit: str
    category: str


class ScorecardResult(ApiModel):
    score: float
    base_score: float
    max_score: float
    scoring_method: str
    breakdown: List[ScorecardLine]
    woe_details: Optional[List[WoeDetail]] = Field(None, description="Weight-of-evidence scorecards only.")
    information_value: Optional[float] = Field(None, description="Weight-of-evidence scorecards only.")
    metrics: Optional[Dict[str, ScorecardMetric]] = Field(None, description="Authored metrics, by name.")


class TraceEntry(ApiModel):
    """One executed (or skipped, or deferred) policy step. `result` is the step's own output and
    its shape depends on the step type."""

    step: Dict[str, Any] = Field(description="The step as authored: id, type, label, ref_id, config.")
    result: Optional[Dict[str, Any]] = Field(None, description="The step's output. Absent when it did not run.")
    duration_ms: Optional[float] = None
    error: Optional[str] = None
    skipped: Optional[bool] = Field(None, description="The step's condition was not met.")
    reason: Optional[str] = Field(None, description="Why the step was skipped.")
    deferred: Optional[bool] = Field(None, description="Left to the host by the portable core (FAST_DECIDE).")


class DecideResponse(ApiModel):
    policy_id: str
    decision_id: Optional[str] = Field(
        description="The decision this call recorded; fetch it, its reason codes or a replay by this id. "
                    "Null when the decision paused at a review gate -- no decision exists until a "
                    "reviewer completes it; follow execution_id.")
    execution_id: Optional[str] = Field(
        description="The paused execution a reviewer will complete (GET /api/v1/executions/{id}). "
                    "Null when the decision completed in this call.")
    status: str = Field(description="completed; paused at a review gate; or failed.")
    policy_version: Optional[int] = Field(description="Which version of the policy decided.")
    outcome: str
    score: Optional[float] = Field(description="The scorecard score, when the policy has one.")
    variables: Dict[str, Any] = Field(description="Every variable the decision computed, by id.")
    variable_errors: Dict[str, str] = Field(
        description="Variables that failed to compute, by id. Their value in `variables` is null; "
                    "this map is how to tell a real null from a failure. Empty when healthy.")
    rule_results: List[RuleResult]
    scorecard_result: Optional[ScorecardResult]
    trace: List[TraceEntry]
    latency_ms: float


class BatchRowResult(ApiModel):
    policy_id: str
    outcome: str = Field(description="The decision's outcome, or `error` when the row failed.")
    latency_ms: Optional[float] = None
    error: Optional[str] = Field(None, description="Why the row failed; the rest of the batch still ran.")


class BatchRow(ApiModel):
    index: int = Field(description="Position of the payload in the request.")
    result: BatchRowResult


class BatchPerformance(ApiModel):
    server_ms: float
    throughput_tps: Optional[float]
    avg_ms: Optional[float]
    path: str = Field(description="fast or full_executor.")
    workers: int


class BatchDecideResponse(ApiModel):
    targetType: str
    targetId: str
    rows: List[BatchRow]
    count: int
    performance: BatchPerformance


class ReasonCode(ApiModel):
    code: str = Field(description="Stable for a given failing condition.")
    variable_id: Optional[str]
    variable_name: Optional[str]
    operator: Optional[str]
    threshold: Any
    value: Any = Field(None, description="The applicant's value. Absent once the subject's data is erased.")
    reason: str = Field(description="A sentence suitable for a decline notice.")


class ErasedRuleResults(ApiModel):
    """What `rule_results` holds after the subject's data was erased: the reason codes a decline
    notice needs, without the applicant's values."""

    erased: bool
    reason_codes: List[ReasonCode]


class ErasedTraceMarker(ApiModel):
    """The whole of an erased decision's trace: one marker in place of the steps."""

    type: str = Field(description="erased.")
    status: str = Field(description="erased.")


class DecisionRecord(ApiModel):
    id: str
    client_id: Optional[str]
    user_id: Optional[str] = Field(description="The data subject. A keyed pseudonym once erased.")
    policy_id: Optional[str]
    policy_version: Optional[int]
    outcome: str
    payload: Dict[str, Any] = Field(description="The request payload, with configured PII keys redacted.")
    payload_hash: Optional[str]
    computed_variables: Dict[str, Any]
    rule_results: List[Union[RuleResult, ErasedRuleResults]]
    scorecard_result: Optional[ScorecardResult]
    trace: List[Union[TraceEntry, ErasedTraceMarker]]
    latency_ms: Optional[float]
    source: Optional[str] = Field(description="api, api_fast, sdk_server, sdk_device, ...")
    sdk_version: Optional[str]
    experiment_id: Optional[str]
    experiment_variant: Optional[str]
    device_id: Optional[str]
    bundle_hash: Optional[str] = Field(description="The compiled bundle an on-device decision used. Null for server decisions.")
    created_at: Optional[str]
    received_at: Optional[str]
    erased_at: Optional[str] = Field(description="Set when the subject's personal data was erased.")
    undecryptable: bool = Field(description="True when encrypted fields could not be decrypted with the current key.")


class ReasonCodesResponse(ApiModel):
    decision_id: str
    outcome: Optional[str]
    reason_codes: List[ReasonCode]
    erased_at: Optional[str] = Field(None, description="Present when the codes were preserved at erasure.")


class ReplayResponse(ApiModel):
    decision_id: str
    policy_id: Optional[str]
    original_outcome: Optional[str]
    replayed_outcome: str
    changed: bool
    bundle_version: int
    replayed_at: str


class ExecutionSummary(ApiModel):
    id: str
    tenant_id: str
    policy_id: Optional[str]
    status: str = Field(description="running, paused, completed or failed.")
    current_step_index: Optional[int]
    trigger_type: Optional[str]
    trigger_metadata: Dict[str, Any]
    started_at: Optional[str]
    paused_at: Optional[str]
    completed_at: Optional[str]
    created_at: Optional[str]
    updated_at: Optional[str]


# ── OAuth2 (RFC 6749 §4.4, §5.1, §5.2) ────────────────────────────────────


class OAuthTokenResponse(ApiModel):
    access_token: str
    token_type: str = Field(description="Bearer.")
    expires_in: int = Field(description="Seconds.")


class OAuthErrorResponse(ErrorResponse):
    error: str = Field(description="An RFC 6749 §5.2 code: invalid_client, unsupported_grant_type, ...")
    error_description: Optional[str] = None


# ── Privacy (plan 6.7) ─────────────────────────────────────────────────────


class SubjectExecution(ApiModel):
    OPEN: ClassVar[bool] = True
    id: str
    policy_id: Optional[str]
    status: str
    context: Dict[str, Any]


class SubjectCounts(ApiModel):
    decisions: int
    executions: int


class DataSubjectExport(ApiModel):
    subject_id: str
    decisions: List[DecisionRecord]
    executions: List[SubjectExecution]
    counts: SubjectCounts
    not_covered: List[str] = Field(description="Stores outside this database that the export cannot see.")


class ErasureCounts(ApiModel):
    decisions: int
    executions: int
    review_tasks: int
    webhook_deliveries: int
    outbox_events: int
    idempotency_records: int
    error_events: int


class ErasureReceipt(ApiModel):
    subject: str = Field(description="The keyed pseudonym that replaced the subject id.")
    erased: ErasureCounts
    not_covered: List[str] = Field(description="Stores erasure cannot reach, and what to do about each.")


class ResidencyDatabase(ApiModel):
    engine: str
    host: Optional[str]
    database: Optional[str] = None
    note: Optional[str] = None


class EncryptionAtRest(ApiModel):
    decisions: bool
    audit_log: bool


class EgressDestination(ApiModel):
    kind: str = Field(description="connector, connector_token_endpoint, webhook_subscription, ai_provider, "
                                  "decision_archive, kafka or tracing.")
    id: Optional[str]
    host: Optional[str]
    sends: str
    at_decision_time: Optional[bool] = None
    region: Optional[str] = None


class SubjectEndpoints(ApiModel):
    access: str
    erasure: str


class ResidencyReport(ApiModel):
    install_region: Optional[str] = Field(description="RULEMIND_DATA_REGION, as declared by the operator.")
    statement: str
    database: ResidencyDatabase
    encryption_at_rest: EncryptionAtRest
    egress: List[EgressDestination]
    data_subject_endpoints: SubjectEndpoints


# ── SDK ────────────────────────────────────────────────────────────────────


class SigningKeyResponse(ApiModel):
    algorithm: str
    publicKeyPem: str
    ephemeral: bool = Field(description="True when no RULEMIND_SIGNING_PRIVATE_KEY is configured: the key "
                                        "changes per process, so do not pin it.")


class SdkBundleResponse(ApiModel):
    bundleVersion: int
    encryptedBundle: str
    encryptedKey: Optional[str]
    signature: str
    checksum: str
    compiledAt: str
    expiresAt: str


class SdkDecideResponse(ApiModel):
    """/sdk/v1 speaks camelCase. The nested objects below the rule results are the dashboard's
    explanation views and are documented as objects, not field by field."""

    executionId: str
    status: str
    outcome: str
    score: Optional[float]
    variables: Dict[str, Any]
    ruleResults: List[RuleResult]
    scorecardResults: Dict[str, ScorecardResult] = Field(description="By scorecard id.")
    trace: List[TraceEntry]
    latencyMs: float
    requestId: Optional[str]
    startedAt: Optional[str]
    completedAt: Optional[str]
    experiment: Optional[Dict[str, Any]]
    experimentId: Optional[str]
    experimentVariant: Optional[str]
    serverOnlyStepsSkipped: List[str] = Field(description="Steps only the server can run, skipped on this path.")
    pendingOperations: List[Dict[str, Any]]
    actionResults: List[Dict[str, Any]]
    transformOutputs: Dict[str, Any]
    reviewTask: Optional[Dict[str, Any]]
    reviewResponse: Dict[str, Any]
    auditSummary: Dict[str, Any]
    explainability: Dict[str, Any]


# ── Liveness and readiness ─────────────────────────────────────────────────


class HealthResponse(ApiModel):
    status: str
    version: str
    check: str


class ReadinessResponse(ApiModel):
    ready: bool = Field(description="False (with a 503) when the API must not take traffic.")
    status: str = Field(description="ok, degraded or unavailable.")
    db: str
    redis: str
    sandbox: str
    sandbox_mode: str
    sandbox_detail: Optional[str] = None
    version: str
