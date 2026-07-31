class Instruction {
  Instruction({
    required this.op,
    this.target,
    this.source,
    this.sources = const <String>[],
    this.left,
    this.right,
    this.args = const <String>[],
    this.path,
    this.key,
    this.defaultValue,
    this.type,
    this.value,
    this.values = const <dynamic>[],
    this.predicatePath,
    this.predicateOperator,
    this.predicateValue,
    this.thenSource,
    this.elseSource,
    this.thenValue,
    this.elseValue,
  });

  factory Instruction.fromJson(Map<String, dynamic> json) => Instruction(
        op: json["op"] as String,
        target: json["target"] as String?,
        source: json["source"] as String?,
        sources: (json["sources"] as List<dynamic>? ?? const <dynamic>[]).map((item) => item.toString()).toList(),
        left: json["left"] as String?,
        right: json["right"] as String?,
        args: (json["args"] as List<dynamic>? ?? const <dynamic>[]).map((item) => item.toString()).toList(),
        path: json["path"] as String?,
        key: json["key"] as String?,
        defaultValue: json["defaultValue"],
        type: json["type"] as String?,
        value: json["value"],
        values: (json["values"] as List<dynamic>? ?? const <dynamic>[]).toList(),
        predicatePath: (json["predicatePath"] ?? json["predicate_path"]) as String?,
        predicateOperator: (json["predicateOperator"] ?? json["predicate_operator"]) as String?,
        predicateValue: json["predicateValue"],
        thenSource: json["thenSource"] as String?,
        elseSource: json["elseSource"] as String?,
        thenValue: json["thenValue"],
        elseValue: json["elseValue"],
      );

  final String op;
  final String? target;
  final String? source;
  final List<String> sources;
  final String? left;
  final String? right;
  final List<String> args;
  final String? path;
  final String? key;
  final dynamic defaultValue;
  final String? type;
  final dynamic value;
  final List<dynamic> values;
  final String? predicatePath;
  final String? predicateOperator;
  final dynamic predicateValue;
  final String? thenSource;
  final String? elseSource;
  final dynamic thenValue;
  final dynamic elseValue;
}

class CompiledVariable {
  CompiledVariable({
    required this.id,
    required this.sourceId,
    required this.name,
    required this.instructions,
    this.returnType,
    this.compilable = true,
  });

  factory CompiledVariable.fromJson(Map<String, dynamic> json) => CompiledVariable(
        id: json["id"] as String,
        sourceId: (json["sourceId"] ?? json["source_id"]) as String,
        name: json["name"] as String,
        returnType: (json["returnType"] ?? json["return_type"]) as String?,
        instructions: (json["instructions"] as List<dynamic>? ?? const <dynamic>[])
            .whereType<Map<String, dynamic>>()
            .map(Instruction.fromJson)
            .toList(),
        compilable: json["compilable"] as bool? ?? true,
      );

  final String id;
  final String sourceId;
  final String name;
  final String? returnType;
  final List<Instruction> instructions;
  final bool compilable;
}

class CompiledRule {
  CompiledRule({
    required this.id,
    required this.name,
    required this.ruleFormat,
    this.tree,
    this.nodes = const <Map<String, dynamic>>[],
    this.expression,
  });

  factory CompiledRule.fromJson(Map<String, dynamic> json) => CompiledRule(
        id: json["id"] as String,
        name: json["name"] as String,
        ruleFormat: (json["ruleFormat"] ?? json["rule_format"] ?? "v1") as String,
        tree: json["tree"] as Map<String, dynamic>?,
        nodes: (json["nodes"] as List<dynamic>? ?? const <dynamic>[]).whereType<Map<String, dynamic>>().toList(),
        expression: json["expression"] as String?,
      );

  final String id;
  final String name;
  final String ruleFormat;
  final Map<String, dynamic>? tree;
  final List<Map<String, dynamic>> nodes;
  final String? expression;
}

class ScoreRange {
  ScoreRange({this.min, this.max, required this.points});

  factory ScoreRange.fromJson(Map<String, dynamic> json) => ScoreRange(
        min: (json["min"] as num?)?.toDouble(),
        max: (json["max"] as num?)?.toDouble(),
        points: (json["points"] as num?)?.toInt() ?? 0,
      );

  final double? min;
  final double? max;
  final int points;
}

class ScoreFactor {
  ScoreFactor({required this.variableId, required this.ranges});

  factory ScoreFactor.fromJson(Map<String, dynamic> json) => ScoreFactor(
        variableId: (json["variableId"] ?? json["variable_id"]) as String,
        ranges: (json["ranges"] as List<dynamic>? ?? const <dynamic>[])
            .whereType<Map<String, dynamic>>()
            .map(ScoreRange.fromJson)
            .toList(),
      );

  final String variableId;
  final List<ScoreRange> ranges;
}

class Scorecard {
  Scorecard({
    required this.id,
    required this.name,
    required this.bins,
    this.baseScore = 300,
    this.maxScore = 900,
  });

  factory Scorecard.fromJson(Map<String, dynamic> json) => Scorecard(
        id: json["id"] as String,
        name: json["name"] as String,
        baseScore: ((json["baseScore"] ?? json["base_score"]) as num?)?.toInt() ?? 300,
        maxScore: ((json["maxScore"] ?? json["max_score"]) as num?)?.toInt() ?? 900,
        bins: (json["bins"] as List<dynamic>? ?? const <dynamic>[]).whereType<Map<String, dynamic>>().map(ScoreFactor.fromJson).toList(),
      );

  final String id;
  final String name;
  final int baseScore;
  final int maxScore;
  final List<ScoreFactor> bins;
}

class PolicyStep {
  PolicyStep({
    required this.type,
    this.id,
    this.ref,
    this.refId,
    this.label,
    this.config = const <String, dynamic>{},
  });

  factory PolicyStep.fromJson(Map<String, dynamic> json) => PolicyStep(
        type: json["type"] as String,
        id: json["id"] as String?,
        ref: json["ref"] as String?,
        refId: (json["refId"] ?? json["ref_id"]) as String?,
        label: (json["label"] ?? json["name"]) as String?,
        config: (json["config"] as Map<String, dynamic>?) ?? const <String, dynamic>{},
      );

  final String type;
  final String? id;
  final String? ref;
  final String? refId;
  final String? label;
  final Map<String, dynamic> config;
}

class Policy {
  Policy({
    required this.id,
    required this.name,
    required this.steps,
    this.serverOnlySteps = const <String>[],
    this.defaultOutcome,
  });

  factory Policy.fromJson(Map<String, dynamic> json) => Policy(
        id: json["id"] as String,
        name: json["name"] as String,
        steps: (json["steps"] as List<dynamic>? ?? const <dynamic>[]).whereType<Map<String, dynamic>>().map(PolicyStep.fromJson).toList(),
        serverOnlySteps: (json["serverOnlySteps"] as List<dynamic>? ?? const <dynamic>[]).map((item) => item.toString()).toList(),
        defaultOutcome: (json["defaultOutcome"] ?? json["default_outcome"]) as String?,
      );

  final String id;
  final String name;
  final List<PolicyStep> steps;
  final List<String> serverOnlySteps;
  final String? defaultOutcome;
}

class ExperimentVariant {
  ExperimentVariant({required this.id, required this.weight, this.overrides = const <String, dynamic>{}});

  factory ExperimentVariant.fromJson(Map<String, dynamic> json) => ExperimentVariant(
        id: json["id"] as String,
        weight: (json["weight"] as num).toInt(),
        overrides: (json["overrides"] as Map<String, dynamic>?) ?? const <String, dynamic>{},
      );

  final String id;
  final int weight;
  final Map<String, dynamic> overrides;
}

class Experiment {
  Experiment({
    required this.id,
    required this.name,
    required this.status,
    required this.variants,
    this.targetPolicyId,
  });

  factory Experiment.fromJson(Map<String, dynamic> json) => Experiment(
        id: json["id"] as String,
        name: json["name"] as String,
        status: json["status"] as String,
        variants: (json["variants"] as List<dynamic>? ?? const <dynamic>[]).whereType<Map<String, dynamic>>().map(ExperimentVariant.fromJson).toList(),
        targetPolicyId: (json["targetPolicyId"] ?? json["target_policy_id"]) as String?,
      );

  final String id;
  final String name;
  final String status;
  final List<ExperimentVariant> variants;
  final String? targetPolicyId;
}

class Bundle {
  Bundle({
    required this.bundleVersion,
    required this.bundleId,
    required this.tenantId,
    required this.compiledAt,
    required this.expiresAt,
    required this.variables,
    required this.rules,
    required this.scorecards,
    this.decisionTables = const <Map<String, dynamic>>[],
    required this.policies,
    required this.experiments,
    required this.serverOnlyVariables,
    required this.checksum,
  });

  factory Bundle.fromJson(Map<String, dynamic> json) => Bundle(
        bundleVersion: (json["bundleVersion"] as num).toInt(),
        bundleId: json["bundleId"] as String,
        tenantId: json["tenantId"] as String,
        compiledAt: json["compiledAt"] as String,
        expiresAt: json["expiresAt"] as String,
        variables: (json["variables"] as List<dynamic>? ?? const <dynamic>[]).whereType<Map<String, dynamic>>().map(CompiledVariable.fromJson).toList(),
        rules: (json["rules"] as List<dynamic>? ?? const <dynamic>[]).whereType<Map<String, dynamic>>().map(CompiledRule.fromJson).toList(),
        scorecards: (json["scorecards"] as List<dynamic>? ?? const <dynamic>[]).whereType<Map<String, dynamic>>().map(Scorecard.fromJson).toList(),
        // Decision tables travel as raw maps (rows/cells are keyed by dynamic input
        // ids) — DecisionTableEvaluator reads them exactly like the Python core.
        decisionTables: (json["decisionTables"] as List<dynamic>? ?? const <dynamic>[]).whereType<Map<String, dynamic>>().toList(),
        policies: (json["policies"] as List<dynamic>? ?? const <dynamic>[]).whereType<Map<String, dynamic>>().map(Policy.fromJson).toList(),
        experiments: (json["experiments"] as List<dynamic>? ?? const <dynamic>[]).whereType<Map<String, dynamic>>().map(Experiment.fromJson).toList(),
        serverOnlyVariables: (json["serverOnlyVariables"] as List<dynamic>? ?? const <dynamic>[]).map((item) => item.toString()).toList(),
        checksum: json["checksum"] as String,
      );

  final int bundleVersion;
  final String bundleId;
  final String tenantId;
  final String compiledAt;
  final String expiresAt;
  final List<CompiledVariable> variables;
  final List<CompiledRule> rules;
  final List<Scorecard> scorecards;
  final List<Map<String, dynamic>> decisionTables;
  final List<Policy> policies;
  final List<Experiment> experiments;
  final List<String> serverOnlyVariables;
  final String checksum;
}

class BundleEnvelope {
  BundleEnvelope({
    required this.bundleVersion,
    required this.encryptedBundle,
    required this.encryptedKey,
    required this.signature,
    required this.checksum,
    required this.compiledAt,
    required this.expiresAt,
  });

  factory BundleEnvelope.fromJson(Map<String, dynamic> json) => BundleEnvelope(
        bundleVersion: (json["bundleVersion"] as num).toInt(),
        encryptedBundle: json["encryptedBundle"] as String,
        encryptedKey: json["encryptedKey"] as String,
        signature: json["signature"] as String,
        checksum: json["checksum"] as String,
        compiledAt: json["compiledAt"] as String,
        expiresAt: json["expiresAt"] as String,
      );

  final int bundleVersion;
  final String encryptedBundle;
  final String encryptedKey;
  final String signature;
  final String checksum;
  final String compiledAt;
  final String expiresAt;
}

class Decision {
  Decision({
    required this.outcome,
    required this.variables,
    required this.ruleResults,
    this.score,
    this.experimentId,
    this.experimentVariant,
    this.latencyMs = 0,
    this.requestId,
    this.serverOnlyStepsSkipped = const <String>[],
    this.executionId,
    this.status = "completed",
    this.trace = const <Map<String, dynamic>>[],
    this.scorecardResults = const <String, Map<String, dynamic>>{},
    this.actionResults = const <Map<String, dynamic>>[],
    this.pendingOperations = const <Map<String, dynamic>>[],
    this.reviewTask,
    this.explainability = const <String, dynamic>{},
    this.auditSummary = const <String, dynamic>{},
    this.source,
    this.policyId,
    this.userId,
    this.payload = const <String, dynamic>{},
    this.transformOutputs = const <String, Map<String, dynamic>>{},
    this.currentStepIndex = 0,
    this.pausedAtStep,
    this.reviewResponse = const <String, dynamic>{},
  });

  final String outcome;
  final double? score;
  final Map<String, dynamic> variables;
  final List<Map<String, dynamic>> ruleResults;
  final String? experimentId;
  final String? experimentVariant;
  final int latencyMs;
  final String? requestId;
  final List<String> serverOnlyStepsSkipped;
  final String? executionId;
  final String status;
  final List<Map<String, dynamic>> trace;
  final Map<String, Map<String, dynamic>> scorecardResults;
  final List<Map<String, dynamic>> actionResults;
  final List<Map<String, dynamic>> pendingOperations;
  final Map<String, dynamic>? reviewTask;
  final Map<String, dynamic> explainability;
  final Map<String, dynamic> auditSummary;
  final String? source;
  final String? policyId;
  final String? userId;
  final Map<String, dynamic> payload;
  final Map<String, Map<String, dynamic>> transformOutputs;
  final int currentStepIndex;
  final int? pausedAtStep;
  final Map<String, dynamic> reviewResponse;
}

class ReviewDecision {
  ReviewDecision({
    required this.decision,
    this.response = const <String, dynamic>{},
    this.reviewerId,
  });

  final String decision;
  final Map<String, dynamic> response;
  final String? reviewerId;
}

class RuleMindConfig {
  RuleMindConfig({
    required this.baseUrl,
    required this.apiKey,
    this.sdkVersion = "4.1.0",
    this.bundleSyncIntervalMinutes = 15,
    this.decisionCacheTtlMs = 300000,
    this.decisionCacheMaxEntries = 256,
    this.connectTimeoutMs = 5000,
    this.readTimeoutMs = 10000,
    this.retryCount = 2,
    this.eventFlushBatchSize = 50,
    this.eventFlushIntervalMs = 60000,
    this.enableServerFallback = true,
    this.serverPublicKeyPem,
  });

  final String baseUrl;
  final String apiKey;
  final String sdkVersion;
  final int bundleSyncIntervalMinutes;
  final int decisionCacheTtlMs;
  final int decisionCacheMaxEntries;
  final int connectTimeoutMs;
  final int readTimeoutMs;
  final int retryCount;
  final int eventFlushBatchSize;
  final int eventFlushIntervalMs;
  final bool enableServerFallback;
  final String? serverPublicKeyPem;
}

class SdkEvent {
  SdkEvent({
    required this.type,
    required this.timestamp,
    this.data = const <String, dynamic>{},
  });

  factory SdkEvent.fromJson(Map<String, dynamic> json) => SdkEvent(
        type: json["type"] as String,
        timestamp: json["timestamp"] as String,
        data: (json["data"] as Map<String, dynamic>?) ?? const <String, dynamic>{},
      );

  Map<String, dynamic> toJson() => <String, dynamic>{
        "type": type,
        "timestamp": timestamp,
        "data": data,
      };

  final String type;
  final String timestamp;
  final Map<String, dynamic> data;
}
