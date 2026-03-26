import { randomUUID } from "node:crypto";
import type {
  ApiKeyRecord,
  AssetKind,
  AuditLogRecord,
  BatchJobRecord,
  DashboardSummary,
  EnvironmentName,
  ManagedAsset,
  OutcomeImportRecord,
  PendingApproval,
  RuleDefinition,
  RuleRecord,
  RuleVersionRecord
} from "@rulemind/shared";

export interface RuleFilters {
  environment?: EnvironmentName;
  tag?: string;
  active?: boolean;
}

export interface AssetFilters {
  environment?: EnvironmentName;
  tag?: string;
  status?: string;
}

export interface PlatformStore {
  createRule(input: Omit<RuleRecord, "id" | "createdAt" | "updatedAt">): Promise<RuleRecord>;
  getRule(id: string): Promise<RuleRecord | null>;
  updateRule(id: string, patch: Partial<RuleRecord>): Promise<RuleRecord | null>;
  deleteRule(id: string): Promise<RuleRecord | null>;
  listRules(filters?: RuleFilters): Promise<RuleRecord[]>;
  createVersion(input: Omit<RuleVersionRecord, "id" | "createdAt">): Promise<RuleVersionRecord>;
  getVersion(ruleId: string, version: number): Promise<RuleVersionRecord | null>;
  listVersions(ruleId: string): Promise<RuleVersionRecord[]>;
  logEvaluation(entry: AuditLogRecord): Promise<void>;
  getAuditLog(ruleId: string): Promise<AuditLogRecord[]>;
  createAsset<T extends Record<string, unknown>>(kind: AssetKind, asset: Omit<ManagedAsset<T>, "id" | "createdAt" | "updatedAt">): Promise<ManagedAsset<T>>;
  getAsset<T extends Record<string, unknown>>(kind: AssetKind, id: string): Promise<ManagedAsset<T> | null>;
  updateAsset<T extends Record<string, unknown>>(kind: AssetKind, id: string, patch: Partial<ManagedAsset<T>>): Promise<ManagedAsset<T> | null>;
  deleteAsset(kind: AssetKind, id: string): Promise<ManagedAsset | null>;
  listAssets<T extends Record<string, unknown>>(kind: AssetKind, filters?: AssetFilters): Promise<Array<ManagedAsset<T>>>;
  createApproval(input: PendingApproval): Promise<PendingApproval>;
  getApproval(id: string): Promise<PendingApproval | null>;
  updateApproval(id: string, patch: Partial<PendingApproval>): Promise<PendingApproval | null>;
  listApprovals(environment?: EnvironmentName): Promise<PendingApproval[]>;
  createApiKey(record: ApiKeyRecord): Promise<ApiKeyRecord>;
  listApiKeys(environment?: EnvironmentName): Promise<ApiKeyRecord[]>;
  getApiKeyByHash(hash: string): Promise<ApiKeyRecord | null>;
  updateApiKey(id: string, patch: Partial<ApiKeyRecord>): Promise<ApiKeyRecord | null>;
  createBatchJob(record: BatchJobRecord): Promise<BatchJobRecord>;
  getBatchJob(id: string): Promise<BatchJobRecord | null>;
  updateBatchJob(id: string, patch: Partial<BatchJobRecord>): Promise<BatchJobRecord | null>;
  listBatchJobs(): Promise<BatchJobRecord[]>;
  createOutcomeImport(record: OutcomeImportRecord): Promise<OutcomeImportRecord>;
  listOutcomeImports(): Promise<OutcomeImportRecord[]>;
  dashboardSummary(environment?: EnvironmentName): Promise<DashboardSummary>;
  ping(): Promise<boolean>;
  migrate(): Promise<void>;
}

export interface PlatformState {
  rules: RuleRecord[];
  ruleVersions: RuleVersionRecord[];
  auditLog: AuditLogRecord[];
  assets: ManagedAsset[];
  approvals: PendingApproval[];
  apiKeys: ApiKeyRecord[];
  batchJobs: BatchJobRecord[];
  outcomeImports: OutcomeImportRecord[];
}

export function emptyState(): PlatformState {
  return {
    rules: [],
    ruleVersions: [],
    auditLog: [],
    assets: [],
    approvals: [],
    apiKeys: [],
    batchJobs: [],
    outcomeImports: []
  };
}

abstract class StateStore implements PlatformStore {
  protected abstract loadState(): Promise<PlatformState>;
  protected abstract saveState(state: PlatformState): Promise<void>;

  async createRule(input: Omit<RuleRecord, "id" | "createdAt" | "updatedAt">): Promise<RuleRecord> {
    const now = new Date().toISOString();
    const record: RuleRecord = {
      ...input,
      id: randomUUID(),
      createdAt: now,
      updatedAt: now
    };
    const state = await this.loadState();
    state.rules.push(record);
    await this.saveState(state);
    return record;
  }

  async getRule(id: string): Promise<RuleRecord | null> {
    const state = await this.loadState();
    return state.rules.find((rule) => rule.id === id) ?? null;
  }

  async updateRule(id: string, patch: Partial<RuleRecord>): Promise<RuleRecord | null> {
    const state = await this.loadState();
    const index = state.rules.findIndex((rule) => rule.id === id);

    if (index === -1) {
      return null;
    }

    state.rules[index] = {
      ...state.rules[index],
      ...patch,
      updatedAt: new Date().toISOString()
    };
    await this.saveState(state);
    return state.rules[index];
  }

  async deleteRule(id: string): Promise<RuleRecord | null> {
    return this.updateRule(id, {
      isActive: false,
      status: "deleted"
    });
  }

  async listRules(filters: RuleFilters = {}): Promise<RuleRecord[]> {
    const state = await this.loadState();
    return state.rules.filter((rule) => {
      if (filters.environment && rule.environment !== filters.environment) {
        return false;
      }

      if (filters.tag && !rule.tags.includes(filters.tag)) {
        return false;
      }

      if (filters.active !== undefined && rule.isActive !== filters.active) {
        return false;
      }

      return true;
    });
  }

  async createVersion(input: Omit<RuleVersionRecord, "id" | "createdAt">): Promise<RuleVersionRecord> {
    const state = await this.loadState();
    const record: RuleVersionRecord = {
      ...input,
      id: randomUUID(),
      createdAt: new Date().toISOString()
    };
    state.ruleVersions.push(record);
    await this.saveState(state);
    return record;
  }

  async getVersion(ruleId: string, version: number): Promise<RuleVersionRecord | null> {
    const state = await this.loadState();
    return state.ruleVersions.find((entry) => entry.ruleId === ruleId && entry.version === version) ?? null;
  }

  async listVersions(ruleId: string): Promise<RuleVersionRecord[]> {
    const state = await this.loadState();
    return state.ruleVersions.filter((entry) => entry.ruleId === ruleId).sort((left, right) => right.version - left.version);
  }

  async logEvaluation(entry: AuditLogRecord) {
    const state = await this.loadState();
    state.auditLog.unshift(entry);
    await this.saveState(state);
  }

  async getAuditLog(ruleId: string): Promise<AuditLogRecord[]> {
    const state = await this.loadState();
    return state.auditLog.filter((entry) => entry.ruleId === ruleId);
  }

  async createAsset<T extends Record<string, unknown>>(kind: AssetKind, asset: Omit<ManagedAsset<T>, "id" | "createdAt" | "updatedAt">): Promise<ManagedAsset<T>> {
    const state = await this.loadState();
    const now = new Date().toISOString();
    const record: ManagedAsset<T> = {
      ...asset,
      kind,
      id: randomUUID(),
      createdAt: now,
      updatedAt: now
    };
    state.assets.push(record as ManagedAsset);
    await this.saveState(state);
    return record;
  }

  async getAsset<T extends Record<string, unknown>>(kind: AssetKind, id: string): Promise<ManagedAsset<T> | null> {
    const state = await this.loadState();
    return (state.assets.find((asset) => asset.kind === kind && asset.id === id) as ManagedAsset<T> | undefined) ?? null;
  }

  async updateAsset<T extends Record<string, unknown>>(kind: AssetKind, id: string, patch: Partial<ManagedAsset<T>>): Promise<ManagedAsset<T> | null> {
    const state = await this.loadState();
    const index = state.assets.findIndex((asset) => asset.kind === kind && asset.id === id);

    if (index === -1) {
      return null;
    }

    state.assets[index] = {
      ...state.assets[index],
      ...patch,
      kind,
      updatedAt: new Date().toISOString()
    };
    await this.saveState(state);
    return state.assets[index] as ManagedAsset<T>;
  }

  async deleteAsset(kind: AssetKind, id: string): Promise<ManagedAsset | null> {
    return this.updateAsset(kind, id, { status: "deleted" });
  }

  async listAssets<T extends Record<string, unknown>>(kind: AssetKind, filters: AssetFilters = {}): Promise<Array<ManagedAsset<T>>> {
    const state = await this.loadState();
    return state.assets.filter((asset) => {
      if (asset.kind !== kind) {
        return false;
      }

      if (filters.environment && asset.environment !== filters.environment) {
        return false;
      }

      if (filters.tag && !asset.tags.includes(filters.tag)) {
        return false;
      }

      if (filters.status && asset.status !== filters.status) {
        return false;
      }

      return true;
    }) as Array<ManagedAsset<T>>;
  }

  async createApproval(input: PendingApproval): Promise<PendingApproval> {
    const state = await this.loadState();
    state.approvals.unshift(input);
    await this.saveState(state);
    return input;
  }

  async getApproval(id: string): Promise<PendingApproval | null> {
    const state = await this.loadState();
    return state.approvals.find((approval) => approval.id === id) ?? null;
  }

  async updateApproval(id: string, patch: Partial<PendingApproval>): Promise<PendingApproval | null> {
    const state = await this.loadState();
    const index = state.approvals.findIndex((approval) => approval.id === id);

    if (index === -1) {
      return null;
    }

    state.approvals[index] = {
      ...state.approvals[index],
      ...patch,
      updatedAt: new Date().toISOString()
    };
    await this.saveState(state);
    return state.approvals[index];
  }

  async listApprovals(environment?: EnvironmentName): Promise<PendingApproval[]> {
    const state = await this.loadState();
    return environment ? state.approvals.filter((approval) => approval.environment === environment) : state.approvals;
  }

  async createApiKey(record: ApiKeyRecord): Promise<ApiKeyRecord> {
    const state = await this.loadState();
    state.apiKeys.unshift(record);
    await this.saveState(state);
    return record;
  }

  async listApiKeys(environment?: EnvironmentName): Promise<ApiKeyRecord[]> {
    const state = await this.loadState();
    return environment ? state.apiKeys.filter((apiKey) => apiKey.environment === environment) : state.apiKeys;
  }

  async getApiKeyByHash(hash: string): Promise<ApiKeyRecord | null> {
    const state = await this.loadState();
    return state.apiKeys.find((apiKey) => apiKey.keyHash === hash) ?? null;
  }

  async updateApiKey(id: string, patch: Partial<ApiKeyRecord>): Promise<ApiKeyRecord | null> {
    const state = await this.loadState();
    const index = state.apiKeys.findIndex((apiKey) => apiKey.id === id);

    if (index === -1) {
      return null;
    }

    state.apiKeys[index] = {
      ...state.apiKeys[index],
      ...patch
    };
    await this.saveState(state);
    return state.apiKeys[index];
  }

  async createBatchJob(record: BatchJobRecord): Promise<BatchJobRecord> {
    const state = await this.loadState();
    state.batchJobs.unshift(record);
    await this.saveState(state);
    return record;
  }

  async getBatchJob(id: string): Promise<BatchJobRecord | null> {
    const state = await this.loadState();
    return state.batchJobs.find((job) => job.id === id) ?? null;
  }

  async updateBatchJob(id: string, patch: Partial<BatchJobRecord>): Promise<BatchJobRecord | null> {
    const state = await this.loadState();
    const index = state.batchJobs.findIndex((job) => job.id === id);

    if (index === -1) {
      return null;
    }

    state.batchJobs[index] = {
      ...state.batchJobs[index],
      ...patch,
      updatedAt: new Date().toISOString()
    };
    await this.saveState(state);
    return state.batchJobs[index];
  }

  async listBatchJobs(): Promise<BatchJobRecord[]> {
    const state = await this.loadState();
    return state.batchJobs;
  }

  async createOutcomeImport(record: OutcomeImportRecord): Promise<OutcomeImportRecord> {
    const state = await this.loadState();
    state.outcomeImports.unshift(record);
    await this.saveState(state);
    return record;
  }

  async listOutcomeImports(): Promise<OutcomeImportRecord[]> {
    const state = await this.loadState();
    return state.outcomeImports;
  }

  async dashboardSummary(environment?: EnvironmentName): Promise<DashboardSummary> {
    const state = await this.loadState();
    const rules = environment ? state.rules.filter((rule) => rule.environment === environment) : state.rules;
    const assets = environment ? state.assets.filter((asset) => asset.environment === environment) : state.assets;
    const approvals = environment ? state.approvals.filter((approval) => approval.environment === environment) : state.approvals;
    const today = new Date().toISOString().slice(0, 10);

    return {
      activeRules: rules.filter((rule) => rule.isActive).length,
      pendingApprovals: approvals.filter((approval) => approval.status === "pending").length,
      evaluationsToday: state.auditLog.filter((entry) => entry.evaluatedAt.startsWith(today)).length,
      auditEvents: state.auditLog.length,
      variables: assets.filter((asset) => asset.kind === "variable").length,
      connectors: assets.filter((asset) => asset.kind === "connector").length,
      batchJobs: state.batchJobs.length
    };
  }

  async ping(): Promise<boolean> {
    await this.loadState();
    return true;
  }

  async migrate() {
    const state = await this.loadState();
    await this.saveState(state);
  }
}

export { StateStore };

export function createRuleVersionSnapshot(
  ruleId: string,
  definition: RuleDefinition,
  expression: string,
  version: number,
  environment: EnvironmentName,
  createdBy: string,
  summary?: string
): Omit<RuleVersionRecord, "id" | "createdAt"> {
  return {
    ruleId,
    definition,
    expression,
    version,
    environment,
    createdBy,
    summary
  };
}
