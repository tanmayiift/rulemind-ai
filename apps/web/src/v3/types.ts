export type ThemeMode = "light" | "dark";
export type EnvironmentName = "dev" | "uat" | "prod";
export type StatusName = EnvironmentName;
export type ConnectorId = "bureau" | "bank" | "gst" | "device" | "kyc" | "custom";

export interface ConnectorRecord {
  id: string;
  name: string;
  icon: string;
  color: string;
  description: string;
  schema_paths: string[];
  sample_payload: Record<string, unknown>;
  is_active: boolean;
  config: Record<string, unknown>;
  created_at: string;
  updated_at: string;
}

export interface VariableTestResult {
  value: unknown;
  error: string | null;
  latency_ms: number;
  tested_at?: string;
  passed?: boolean;
}

export interface VariableRecord {
  id: string;
  name: string;
  category: string;
  source_id: string;
  code: string;
  description?: string | null;
  status: StatusName;
  last_test_result?: VariableTestResult | null;
  version: number;
  created_at: string;
  updated_at: string;
}

export type RuleOperator =
  | "=="
  | "!="
  | ">"
  | ">="
  | "<"
  | "<="
  | "between"
  | "in"
  | "not_in"
  | "regex"
  | "exists"
  | "!exists";

export type RuleFieldType = "number" | "string" | "boolean" | "date";

export interface RuleNodeRecord {
  id: string;
  type: "condition" | "and" | "or" | "approve" | "review" | "reject";
  variable?: string;
  operator?: RuleOperator;
  value?: string;
  value2?: string;
  fieldType?: RuleFieldType;
  label?: string;
}

export interface RuleTreeNodeRecord {
  id?: string;
  type: "group" | "condition" | "not";
  logic?: "AND" | "OR";
  variable?: string;
  operator?: RuleOperator;
  value?: string | number | boolean;
  value2?: string | number | boolean;
  fieldType?: RuleFieldType;
  children?: RuleTreeNodeRecord[];
  child?: RuleTreeNodeRecord;
  onPass?: "approve" | "review" | "reject";
  onFail?: "approve" | "review" | "reject";
}

export interface RuleRecord {
  id: string;
  name: string;
  nodes: RuleNodeRecord[];
  tree?: RuleTreeNodeRecord | null;
  rule_format?: "v1" | "v2";
  expression: string;
  status: StatusName;
  last_test_result?: {
    passed: boolean;
    outcome: string;
    conditions: RuleConditionResult[];
    groupResults?: Array<{ id?: string; logic: string; passed: boolean; childCount: number }>;
    latency_ms: number;
    tested_at: string;
  } | null;
  version: number;
  created_at: string;
  updated_at: string;
}

export interface RuleConditionResult {
  variable_id: string;
  variable_name: string;
  source_id?: string;
  operator: string;
  threshold: string | number | boolean | null;
  value: unknown;
  passed: boolean;
  group?: string;
}

export interface ScorecardRangeRecord {
  min: number;
  max: number;
  points: number;
}

export interface ScorecardBinRecord {
  variable_id: string;
  ranges: ScorecardRangeRecord[];
}

export interface ScorecardRecord {
  id: string;
  name: string;
  base_score: number;
  max_score: number;
  bins: ScorecardBinRecord[];
  status: StatusName;
  last_test_result?: {
    passed: boolean;
    score: number;
    latency_ms: number;
    tested_at: string;
  } | null;
  version: number;
  created_at: string;
  updated_at: string;
}

export interface PolicyStepRecord {
  type: "connector" | "rule" | "scorecard" | "outcome" | "transform" | "action" | "review_gate";
  ref_id?: string | null;
  ref?: string | null;
  id?: string | null;
  name?: string | null;
  label?: string | null;
  config?: Record<string, unknown> | null;
}

export interface PolicyRecord {
  id: string;
  name: string;
  trigger?: Record<string, unknown> | null;
  steps: PolicyStepRecord[];
  defaultOutcome?: string | null;
  status: StatusName;
  last_test_result?: {
    passed: boolean;
    outcome: string;
    latency_ms: number;
    tested_at: string;
  } | null;
  version: number;
  created_at: string;
  updated_at: string;
}

export interface SettingsRecord {
  api_base_url: string;
  auth_config: Record<string, unknown>;
  engine_config: Record<string, unknown>;
  source_defaults: Record<string, unknown>;
  audit_retention_days: number;
  theme_mode: ThemeMode;
  updated_at?: string;
}

export interface PromotionRecord {
  id: number;
  entity_type: "variable" | "rule" | "scorecard" | "policy";
  entity_id: string;
  from_status: StatusName;
  to_status: StatusName;
  promoted_by: string;
  reason: string;
  created_at: string;
}

export interface BootstrapPayload {
  connectors: ConnectorRecord[];
  variables: VariableRecord[];
  rules: RuleRecord[];
  scorecards: ScorecardRecord[];
  policies: PolicyRecord[];
  settings: SettingsRecord;
  promotions: PromotionRecord[];
}

export interface VariableGraphResponse {
  nodes: Array<{ id: string; type: "variable" | "rule" | "scorecard" | "policy"; label: string; status: StatusName }>;
  edges: Array<{ from: string; to: string; relation: string }>;
}

export interface AuditErrorRecord {
  id: number;
  scope: string;
  entity_type?: string | null;
  entity_id?: string | null;
  stage: string;
  message: string;
  details: Record<string, unknown>;
  created_at: string;
}

export interface VariableBatchTestRow {
  id: string;
  name: string;
  status: StatusName;
  category: string;
  source_id: string;
  source_icon: string;
  computed_value: unknown;
  latency_ms: number;
  passed: boolean;
  badge: string;
  error?: string | null;
}

export interface VariableBatchTestResponse {
  results: VariableBatchTestRow[];
  summary: string;
}

export interface RuleTestResponse {
  rule: RuleRecord;
  expression: string;
  variable_results: Array<{
    id: string;
    name: string;
    category: string;
    source_id: string;
    source_name?: string;
    source_icon?: string;
    source_active: boolean;
    value: unknown;
    error?: string | null;
    passed: boolean;
  }>;
  values: Record<string, unknown>;
  result: {
    passed: boolean;
    outcome: string;
    conditions: RuleConditionResult[];
    groupResults?: Array<{ id?: string; logic: string; passed: boolean; childCount: number }>;
    latency_ms: number;
    tested_at: string;
  };
}

export interface PolicyExecuteResponse {
  policy: PolicyRecord;
  values: Record<string, unknown>;
  variable_results: Array<{
    id: string;
    name: string;
    category: string;
    source_id: string;
    source_name?: string;
    source_icon?: string;
    source_active: boolean;
    value: unknown;
    error?: string | null;
    passed: boolean;
  }>;
  latency_ms: number;
  result: {
    policy_id: string;
    outcome: string;
    scorecard_result?: {
      score: number;
      base_score: number;
      max_score: number;
      breakdown: Array<{
        variable_id: string;
        variable_name: string;
        source_id?: string;
        value: unknown;
        points: number;
        active_range?: ScorecardRangeRecord | null;
      }>;
    } | null;
    trace: Array<Record<string, unknown>>;
  };
}

export interface ExecutionTraceStepRecord {
  step?: {
    name?: string | null;
    label?: string | null;
    type?: string | null;
  } | null;
  type?: string | null;
  status?: string | number | null;
  outcome?: string | null;
  duration_ms?: number | null;
  error?: string | null;
  skipped?: boolean | null;
  result?: {
    status?: string | number | null;
    outcome?: string | null;
  } | null;
  [key: string]: unknown;
}

export interface DeployStatusPayload {
  dev: {
    variables: VariableRecord[];
    rules: RuleRecord[];
    scorecards: ScorecardRecord[];
    policies: PolicyRecord[];
  };
  uat: {
    variables: VariableRecord[];
    rules: RuleRecord[];
    scorecards: ScorecardRecord[];
    policies: PolicyRecord[];
  };
  prod: {
    variables: VariableRecord[];
    rules: RuleRecord[];
    scorecards: ScorecardRecord[];
    policies: PolicyRecord[];
  };
}
