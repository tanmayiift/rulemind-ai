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
        // The compiler emits "default"; "defaultValue" is only the legacy spelling.
        defaultValue: json["default"] ?? json["defaultValue"],
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

/// A bin bound as the Python core reads it.
///
/// `float(candidate.get("min", 0))` treats an ABSENT key as **0.0**, not an open bound — but
/// raises on an explicit null or a non-numeric value, and the `except (TypeError, ValueError):
/// continue` around it means such a candidate can never match. [matchable] carries that second
/// case, which a plain nullable double cannot express.
class ScoreBound {
  const ScoreBound(this.value, this.matchable);

  factory ScoreBound.fromJson(Map<String, dynamic> json, String key) {
    if (!json.containsKey(key)) return const ScoreBound(null, true);
    final raw = json[key];
    if (raw is num) return ScoreBound(raw.toDouble(), true);
    if (raw is String) {
      final parsed = double.tryParse(raw.trim());
      return parsed == null ? const ScoreBound(null, false) : ScoreBound(parsed, true);
    }
    return const ScoreBound(null, false);
  }

  final double? value;
  final bool matchable;

  double get bound => value ?? 0.0;
}

class ScoreRange {
  ScoreRange({this.min, this.max, required this.points, this.matchable = true});

  factory ScoreRange.fromJson(Map<String, dynamic> json) {
    final lower = ScoreBound.fromJson(json, "min");
    final upper = ScoreBound.fromJson(json, "max");
    return ScoreRange(
      min: lower.value,
      max: upper.value,
      points: _asInt(json["points"]) ?? 0,
      matchable: lower.matchable && upper.matchable,
    );
  }

  final double? min;
  final double? max;
  final int points;
  final bool matchable;

  double get lowerBound => min ?? 0.0;
  double get upperBound => max ?? 0.0;
}

/// A weight-of-evidence bin: the same bound semantics as [ScoreRange], carrying woe/iv not points.
class WoeRange {
  WoeRange({
    this.min,
    this.max,
    this.woe = 0.0,
    this.iv = 0.0,
    this.eventRate,
    this.nonEventRate,
    this.matchable = true,
  });

  factory WoeRange.fromJson(Map<String, dynamic> json) {
    final lower = ScoreBound.fromJson(json, "min");
    final upper = ScoreBound.fromJson(json, "max");
    return WoeRange(
      min: lower.value,
      max: upper.value,
      woe: _asDouble(json["woe"]) ?? 0.0,
      iv: _asDouble(json["iv"]) ?? 0.0,
      eventRate: _asDouble(json["eventRate"] ?? json["event_rate"]),
      nonEventRate: _asDouble(json["nonEventRate"] ?? json["non_event_rate"]),
      matchable: lower.matchable && upper.matchable,
    );
  }

  final double? min;
  final double? max;
  final double woe;
  final double iv;
  final double? eventRate;
  final double? nonEventRate;
  final bool matchable;

  double get lowerBound => min ?? 0.0;
  double get upperBound => max ?? 0.0;
}

/// A derived metric computed from the scored result by a formula (see `safe_formula.dart`).
class ScorecardMetric {
  ScorecardMetric({
    this.name = "unnamed",
    this.formula = "0",
    this.unit = "",
    this.category = "financial",
  });

  factory ScorecardMetric.fromJson(Map<String, dynamic> json) => ScorecardMetric(
        name: json["name"] as String? ?? "unnamed",
        formula: json["formula"] as String? ?? "0",
        unit: json["unit"] as String? ?? "",
        category: json["category"] as String? ?? "financial",
      );

  final String name;
  final String formula;
  final String unit;
  final String category;
}

class ScoreFactor {
  ScoreFactor({
    required this.variableId,
    required this.ranges,
    this.weight = 1.0,
    this.coefficient = 0.0,
    this.woeValues = const <WoeRange>[],
  });

  factory ScoreFactor.fromJson(Map<String, dynamic> json) => ScoreFactor(
        // Null when the bin omits `variable_id`: the Python core then looks up the key `None`,
        // finds nothing, and scores the factor against a value of 0.
        variableId: (json["variableId"] ?? json["variable_id"]) as String?,
        weight: _asDouble(json["weight"]) ?? 1.0,
        coefficient: _asDouble(json["coefficient"]) ?? 0.0,
        ranges: (json["ranges"] as List<dynamic>? ?? const <dynamic>[])
            .whereType<Map<String, dynamic>>()
            .map(ScoreRange.fromJson)
            .toList(),
        woeValues: ((json["woeValues"] ?? json["woe_values"]) as List<dynamic>? ?? const <dynamic>[])
            .whereType<Map<String, dynamic>>()
            .map(WoeRange.fromJson)
            .toList(),
      );

  final String? variableId;
  final List<ScoreRange> ranges;

  /// Multiplies this factor's points before they are summed (points / weighted methods).
  final double weight;

  /// The logistic-regression coefficient this factor's WoE is multiplied by.
  final double coefficient;
  final List<WoeRange> woeValues;
}

class Scorecard {
  Scorecard({
    required this.id,
    required this.name,
    required this.bins,
    this.baseScore = 300,
    this.maxScore = 900,
    this.scoringMethod = "points",
    this.intercept = 0.0,
    this.pdo = 20.0,
    this.targetScore = 600.0,
    this.targetOdds = 50.0,
    this.formula,
    this.metrics = const <ScorecardMetric>[],
  });

  factory Scorecard.fromJson(Map<String, dynamic> json) => Scorecard(
        // A scorecard travelling as a bare definition (a conformance fixture, an inline authoring
        // payload) carries no id/name; the scoring maths never reads them.
        id: json["id"] as String? ?? "",
        name: json["name"] as String? ?? "",
        baseScore: _asInt(json["baseScore"] ?? json["base_score"]) ?? 300,
        maxScore: _asInt(json["maxScore"] ?? json["max_score"]) ?? 900,
        scoringMethod: (json["scoringMethod"] ?? json["scoring_method"]) as String? ?? "points",
        intercept: _asDouble(json["intercept"]) ?? 0.0,
        pdo: _asDouble(json["pdo"]) ?? 20.0,
        targetScore: _asDouble(json["targetScore"] ?? json["target_score"]) ?? 600.0,
        targetOdds: _asDouble(json["targetOdds"] ?? json["target_odds"]) ?? 50.0,
        formula: json["formula"] as String?,
        metrics: (json["metrics"] as List<dynamic>? ?? const <dynamic>[])
            .whereType<Map<String, dynamic>>()
            .map(ScorecardMetric.fromJson)
            .toList(),
        bins: (json["bins"] as List<dynamic>? ?? const <dynamic>[]).whereType<Map<String, dynamic>>().map(ScoreFactor.fromJson).toList(),
      );

  final String id;
  final String name;
  final int baseScore;
  final int maxScore;
  final List<ScoreFactor> bins;

  /// "points" (default), "woe" or "formula". Anything else falls through to points, as in Python.
  final String scoringMethod;

  // ── WoE (points-to-double-the-odds) calibration ──
  final double intercept;
  final double pdo;
  final double targetScore;
  final double targetOdds;

  /// Custom scoring expression for `scoringMethod == "formula"`.
  final String? formula;
  final List<ScorecardMetric> metrics;
}

int? _asInt(dynamic value) {
  if (value is num) return value.toInt();
  if (value is String) return int.tryParse(value.trim());
  return null;
}

double? _asDouble(dynamic value) {
  if (value is num) return value.toDouble();
  if (value is String) return double.tryParse(value.trim());
  return null;
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
        // The server omits this (JSON null) whenever the client sent no
        // X-Client-Public-Key, and CryptoManager.decryptBundle already branches on
        // an EMPTY key. A hard non-null cast threw before that branch could run, so
        // every RuleMind.evaluate() died at bundle parse. Normalise null -> "".
        encryptedKey: (json["encryptedKey"] as String?) ?? "",
        signature: (json["signature"] as String?) ?? "",
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
    this.clientPublicKeyBase64,
    this.initializeHive = true,
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

  /// The server's bundle-SIGNING public key. Used only to verify a downloaded
  /// bundle; never sent to the server.
  final String? serverPublicKeyPem;

  /// This device's own public key (base64 DER or PEM), sent as X-Client-Public-Key
  /// so the server can RSA-wrap the bundle's AES key to it. Leave null to use the
  /// server's keyless bundle mode.
  final String? clientPublicKeyBase64;

  /// Set false when the host app already called `Hive.init`/`Hive.initFlutter`.
  final bool initializeHive;
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
