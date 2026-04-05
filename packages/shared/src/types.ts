export const environments = ["dev", "uat", "prod"] as const;
export type EnvironmentName = (typeof environments)[number];

export const nodeTypes = [
  "trigger",
  "condition",
  "score",
  "and",
  "or",
  "not",
  "group",
  "approve",
  "review",
  "reject"
] as const;
export type NodeType = (typeof nodeTypes)[number];

export const fieldTypes = ["number", "string", "boolean", "date", "enum"] as const;
export type FieldType = (typeof fieldTypes)[number];

export const operators = [
  "==",
  "!=",
  ">",
  ">=",
  "<",
  "<=",
  "between",
  "in",
  "not_in",
  "regex",
  "exists",
  "!exists"
] as const;
export type Operator = (typeof operators)[number];

export type OutcomeType = "approve" | "review" | "reject";
export type GroupOperator = "AND" | "OR";
export type AssetStatus =
  | "draft"
  | "pending_approval"
  | "approved"
  | "active"
  | "archived"
  | "deleted";

export type Role = "admin" | "editor" | "viewer" | "api";
export type AuthMode = "none" | "apikey" | "jwt";

export type Permission =
  | "rule:create"
  | "rule:read"
  | "rule:update"
  | "rule:delete"
  | "rule:evaluate"
  | "rule:promote"
  | "asset:create"
  | "asset:read"
  | "asset:update"
  | "asset:delete"
  | "asset:test"
  | "approval:read"
  | "approval:decide"
  | "api-key:manage"
  | "dashboard:read"
  | "audit:read"
  | "batch:manage";

export const rolePermissions: Record<Role, Permission[]> = {
  admin: [
    "rule:create",
    "rule:read",
    "rule:update",
    "rule:delete",
    "rule:evaluate",
    "rule:promote",
    "asset:create",
    "asset:read",
    "asset:update",
    "asset:delete",
    "asset:test",
    "approval:read",
    "approval:decide",
    "api-key:manage",
    "dashboard:read",
    "audit:read",
    "batch:manage"
  ],
  editor: [
    "rule:create",
    "rule:read",
    "rule:update",
    "rule:evaluate",
    "asset:create",
    "asset:read",
    "asset:update",
    "asset:test",
    "approval:read",
    "dashboard:read",
    "audit:read",
    "batch:manage"
  ],
  viewer: ["rule:read", "rule:evaluate", "asset:read", "approval:read", "dashboard:read", "audit:read"],
  api: ["rule:evaluate", "rule:read", "asset:read"]
};

export interface RuleNode {
  id: string;
  type: NodeType;
  label: string;
  x: number;
  y: number;
  config: {
    field?: string;
    fieldType?: FieldType;
    operator?: Operator;
    value?: unknown;
    value2?: unknown;
    event?: string;
    reason?: string;
    groupOp?: GroupOperator;
    [key: string]: unknown;
  };
}

export interface RuleConnection {
  from: string;
  to: string;
}

export interface RuleMetadata {
  name?: string;
  description?: string;
  environment?: EnvironmentName;
  tags?: string[];
  version?: number;
  createdAt?: string;
  updatedAt?: string;
}

export interface RuleDefinition {
  nodes: RuleNode[];
  connections: RuleConnection[];
  metadata?: RuleMetadata;
}

export interface ValidationIssue {
  level: "error" | "warn";
  message: string;
  nodeId?: string;
  connection?: RuleConnection;
}

export interface ConditionResult {
  nodeId: string;
  nodeLabel: string;
  type: NodeType;
  pass: boolean;
  field?: string;
  operator?: string;
  expected?: unknown;
  actual?: unknown;
  reason?: string;
}

export interface OutcomeDecision {
  type: OutcomeType;
  reason?: string;
  nodeId: string;
}

export interface ExplanationNode {
  nodeId: string;
  nodeType: NodeType;
  label: string;
  pass: boolean;
  detail?: string;
  children: ExplanationNode[];
  reachedOutcomes?: OutcomeDecision[];
}

export interface EvaluationResult {
  outcome: OutcomeType;
  passed: boolean;
  conditionResults: Record<string, ConditionResult>;
  variableResults: Record<string, unknown>;
  explanationTree: ExplanationNode[];
  executionTimeMs: number;
  traceId: string;
  version?: number;
  reachedOutcomes: OutcomeDecision[];
}

export interface RuleRecord {
  id: string;
  name: string;
  environment: EnvironmentName;
  tags: string[];
  isActive: boolean;
  currentVersion: number;
  createdBy: string;
  createdAt: string;
  updatedAt: string;
  expression: string;
  status: AssetStatus;
  definition: RuleDefinition;
}

export interface RuleVersionRecord {
  id: string;
  ruleId: string;
  version: number;
  definition: RuleDefinition;
  expression: string;
  createdAt: string;
  createdBy: string;
  environment: EnvironmentName;
  summary?: string;
}

export interface AuditLogRecord {
  id: string;
  ruleId: string;
  ruleVersion: number;
  inputData: Record<string, unknown>;
  outcome: OutcomeType;
  passed: boolean;
  conditionResults: Record<string, ConditionResult>;
  variableResults: Record<string, unknown>;
  executionMs: number;
  traceId: string;
  evaluatedAt: string;
  evaluatedBy: string;
}

export interface VariableDefinition {
  code: string;
  returnType?: string;
  cacheTtl?: number;
  timeoutMs?: number;
  dependencies?: string[];
  inputSchema?: Record<string, unknown>;
  fallbackValue?: unknown;
}

export interface ConnectorDefinition {
  baseUrl: string;
  authType?: "apiKey" | "oauth2" | "basic" | "bearer" | "custom" | "none";
  authSecretRef?: string;
  mappings?: Record<string, string>;
  timeoutMs?: number;
  cacheTtl?: number;
  fallbackStrategy?: "none" | "static" | "last_success";
  retryCount?: number;
  published?: boolean;
}

export interface DecisionTableDefinition {
  hitPolicy: "First" | "Unique" | "Any" | "All" | "RuleOrder";
  inputs: string[];
  outputs: string[];
  rows: Array<Record<string, unknown>>;
}

export const runtimeEnvironments = environments;
export type RuntimeEnvironment = EnvironmentName;

export type WorkflowStepType = "connector" | "rule" | "scorecard" | "outcome" | "transform" | "action" | "review_gate";
export type WorkflowTriggerType = "api" | "webhook" | "cron";

export interface RuleTreeCondition {
  type: "condition";
  variable: string;
  operator: string;
  value: unknown;
}

export interface RuleTreeGroup {
  type: "group";
  logic: "AND" | "OR" | "NOT";
  children: Array<RuleTreeCondition | RuleTreeGroup>;
  onPass?: string;
  onFail?: string;
}

export interface SdkCompiledVariable {
  id: string;
  sourceId: string;
  name: string;
  returnType?: string;
  instructions: Array<Record<string, unknown>>;
}

export interface SdkCompiledRule {
  id: string;
  name: string;
  ruleFormat: "v1" | "v2";
  tree?: RuleTreeGroup | null;
  nodes?: Array<Record<string, unknown>> | null;
  expression?: string | null;
  status?: string | null;
}

export interface SdkScorecard {
  id: string;
  name: string;
  base_score?: number;
  max_score?: number;
  bins: Array<Record<string, unknown>>;
  status?: string | null;
}

export interface WorkflowPolicyStep {
  id?: string;
  type: WorkflowStepType;
  ref?: string;
  ref_id?: string;
  label?: string;
  name?: string;
  config?: Record<string, unknown>;
}

export interface SdkPolicy {
  id: string;
  name: string;
  steps: WorkflowPolicyStep[];
  serverOnlySteps?: string[];
  defaultOutcome?: string | null;
  trigger?: {
    type?: WorkflowTriggerType;
    config?: Record<string, unknown>;
  } | null;
}

export interface ExperimentVariant {
  id: string;
  weight: number;
  overrides?: Record<string, unknown>;
}

export interface SdkExperiment {
  id: string;
  name: string;
  status: "draft" | "running" | "paused" | "completed";
  variants: ExperimentVariant[];
  target_policy_id?: string | null;
}

export interface ExecutionTraceStep {
  step: WorkflowPolicyStep;
  result?: unknown;
  error?: string | null;
  skipped?: boolean;
  reason?: string;
  duration_ms?: number;
}

export interface ReviewTaskRecord {
  id: string;
  execution_id: string;
  policy_id: string;
  queue: string;
  status: "pending" | "approved" | "rejected" | "escalated" | "timed_out";
  required_fields?: string[];
  context_snapshot?: Record<string, unknown>;
  reviewer_response?: Record<string, unknown> | null;
  reviewed_by?: string | null;
  timeout_at?: string | null;
}

export interface WebhookRegistration {
  id: string;
  policy_id: string;
  endpoint_path: string;
  is_active: boolean;
  payload_mapping?: Record<string, unknown>;
}

export interface CronScheduleRecord {
  id: string;
  policy_id: string;
  cron_expression: string;
  is_active: boolean;
  payload_source?: Record<string, unknown>;
  config?: Record<string, unknown>;
  last_run_at?: string | null;
}

export interface ActionLogRecord {
  id: string;
  execution_id: string;
  step_id?: string | null;
  action_name?: string | null;
  url: string;
  method: string;
  request_body?: Record<string, unknown> | null;
  response_status?: number | null;
  response_body?: string | null;
  latency_ms?: number | null;
  success: boolean;
  retry_count?: number;
  error?: string | null;
}

export interface TenantAdminRecord {
  id: string;
  name: string;
  plan: "free" | "standard" | "enterprise";
  is_active: boolean;
  config?: Record<string, unknown>;
}

export interface TenantApiKeyRecord {
  id: string;
  kid: string;
  masked_key: string;
  created_at?: string | null;
  last_used_at?: string | null;
}

export interface SdkBundle {
  bundleVersion: number;
  bundleId: string;
  tenantId: string;
  compiledAt: string;
  expiresAt: string;
  variables: SdkCompiledVariable[];
  rules: SdkCompiledRule[];
  scorecards: SdkScorecard[];
  policies: SdkPolicy[];
  experiments: SdkExperiment[];
  serverOnlyVariables: string[];
  compilationWarnings?: Array<Record<string, unknown>>;
  checksum: string;
}

export interface SdkBundleEnvelope {
  bundleVersion: number;
  encryptedBundle: string;
  encryptedKey?: string | null;
  signature: string;
  checksum: string;
  compiledAt: string;
  expiresAt: string;
}

export interface SdkDecideRequestContract {
  policyId: string;
  payload: Record<string, unknown>;
  userId?: string;
  requestId?: string;
  sdkVersion?: string;
}

export interface SdkDecisionExperiment {
  id?: string | null;
  variant?: string | null;
}

export interface SdkDecideResponseContract {
  outcome: string;
  score?: number | null;
  variables: Record<string, unknown>;
  ruleResults: Array<Record<string, unknown>>;
  experiment?: SdkDecisionExperiment | null;
  experimentId?: string | null;
  experimentVariant?: string | null;
  latencyMs: number;
  requestId?: string | null;
  executionId?: string | null;
  status?: string;
  trace?: ExecutionTraceStep[];
  scorecardResults?: Record<string, unknown>;
  actionResults?: Array<Record<string, unknown>>;
  reviewTask?: ReviewTaskRecord | null;
  pendingOperations?: Array<Record<string, unknown>>;
  explainability?: Record<string, unknown>;
  auditSummary?: Record<string, unknown>;
  serverOnlyStepsSkipped: string[];
}

export interface SdkEventRecordContract {
  type: string;
  timestamp?: string;
  data?: Record<string, unknown>;
}

export interface ScorecardDefinition {
  factors: Array<{
    name: string;
    field: string;
    operator: Operator;
    value?: unknown;
    value2?: unknown;
    score: number;
  }>;
  bands: Array<{
    min: number;
    max: number;
    label: string;
    decision?: OutcomeType;
  }>;
}

export interface SegmentDefinition {
  priority: number;
  criteria: RuleDefinition;
}

export interface PolicyDefinition {
  ruleIds: string[];
  requiredRuleIds?: string[];
  overrideRuleIds?: string[];
  scorecardId?: string;
  segmentIds?: string[];
}

export interface ExperimentDefinition {
  variants: Array<{
    id: string;
    label: string;
    ruleId?: string;
    policyId?: string;
    traffic: number;
  }>;
  confidenceTarget?: number;
}

export interface WorkflowNode {
  id: string;
  type: "start" | "task" | "rule_gate" | "human_task" | "gateway" | "timer" | "end";
  label: string;
  config: Record<string, unknown>;
}

export interface WorkflowDefinition {
  nodes: WorkflowNode[];
  connections: RuleConnection[];
}

export const assetKinds = [
  "variable",
  "connector",
  "decision_table",
  "scorecard",
  "policy",
  "segment",
  "ab_test",
  "workflow",
  "export",
  "outcome_import"
] as const;
export type AssetKind = (typeof assetKinds)[number];

export interface ManagedAsset<T = Record<string, unknown>> {
  id: string;
  kind: AssetKind;
  name: string;
  description?: string;
  environment: EnvironmentName;
  tags: string[];
  status: AssetStatus;
  version: number;
  createdBy: string;
  createdAt: string;
  updatedAt: string;
  metadata: Record<string, unknown>;
  definition: T;
}

export interface PendingApproval {
  id: string;
  entityKind: "rule" | AssetKind | "api_key";
  entityId: string;
  environment: EnvironmentName;
  status: "pending" | "approved" | "rejected";
  makerId: string;
  checkerId?: string;
  reason?: string;
  comment?: string;
  beforeState?: unknown;
  afterState?: unknown;
  slaDueAt?: string;
  createdAt: string;
  updatedAt: string;
}

export interface ApiKeyRecord {
  id: string;
  name: string;
  keyHash: string;
  environment: EnvironmentName;
  permissions: Permission[];
  createdBy: string;
  createdAt: string;
  expiresAt?: string;
  lastUsedAt?: string;
  rateLimit: number;
}

export interface BatchJobRecord {
  id: string;
  kind: "batch-evaluate" | "export" | "outcome-import";
  status: "queued" | "running" | "completed" | "failed";
  environment: EnvironmentName;
  createdBy: string;
  createdAt: string;
  updatedAt: string;
  payload: Record<string, unknown>;
  result?: Record<string, unknown>;
  error?: string;
}

export interface OutcomeImportRecord {
  id: string;
  source: string;
  status: "queued" | "processed" | "failed";
  recordsImported: number;
  createdAt: string;
  createdBy: string;
  payload: Record<string, unknown>;
}

export interface DashboardSummary {
  activeRules: number;
  pendingApprovals: number;
  evaluationsToday: number;
  auditEvents: number;
  variables: number;
  connectors: number;
  batchJobs: number;
}

export interface UserContext {
  id: string;
  name: string;
  email?: string;
  roles: Role[];
  permissions: Permission[];
  environment?: EnvironmentName;
  authMode: AuthMode;
  apiKeyId?: string;
}
