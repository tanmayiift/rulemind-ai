import client from "prom-client";

export class RuleMindMetrics {
  private readonly registry = new client.Registry();
  private readonly evaluations = new client.Counter({
    name: "rule_evaluations_total",
    help: "Total rule evaluations",
    labelNames: ["rule_id", "outcome", "environment"],
    registers: [this.registry]
  });
  private readonly evaluationDuration = new client.Histogram({
    name: "rule_evaluations_duration_ms",
    help: "Rule evaluation duration in milliseconds",
    labelNames: ["rule_id", "environment"],
    buckets: [1, 5, 10, 25, 50, 100, 250, 500, 1000],
    registers: [this.registry]
  });
  private readonly evaluationOutcome = new client.Counter({
    name: "rule_evaluations_outcome",
    help: "Rule evaluation outcome count",
    labelNames: ["rule_id", "outcome", "environment"],
    registers: [this.registry]
  });
  private readonly cacheHits = new client.Counter({
    name: "rule_cache_hits",
    help: "Compiled rule cache hits",
    labelNames: ["rule_id"],
    registers: [this.registry]
  });
  private readonly cacheMisses = new client.Counter({
    name: "rule_cache_misses",
    help: "Compiled rule cache misses",
    labelNames: ["rule_id"],
    registers: [this.registry]
  });
  private readonly activeRules = new client.Gauge({
    name: "rule_active_count",
    help: "Active rule count",
    labelNames: ["environment"],
    registers: [this.registry]
  });

  constructor() {
    client.collectDefaultMetrics({ register: this.registry });
  }

  recordEvaluation(ruleId: string, environment: string, outcome: string, durationMs: number) {
    this.evaluations.inc({ rule_id: ruleId, outcome, environment });
    this.evaluationOutcome.inc({ rule_id: ruleId, outcome, environment });
    this.evaluationDuration.observe({ rule_id: ruleId, environment }, durationMs);
  }

  markCacheHit(ruleId: string) {
    this.cacheHits.inc({ rule_id: ruleId });
  }

  markCacheMiss(ruleId: string) {
    this.cacheMisses.inc({ rule_id: ruleId });
  }

  setActiveRules(environment: string, count: number) {
    this.activeRules.set({ environment }, count);
  }

  async render() {
    return this.registry.metrics();
  }
}
