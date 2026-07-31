import "dart:convert";

import "package:flutter/foundation.dart";
import "package:hive/hive.dart";
import "package:http/http.dart" as http;

import "src/bundle_manager.dart";
import "src/decision_cache.dart";
import "src/decision_codec.dart";
import "src/decision_outbox.dart";
import "src/decision_syncer.dart";
import "src/event_logger.dart";
import "src/execution_store.dart";
import "src/models.dart";
import "src/rulemind_engine.dart";
import "src/sqflite_decision_outbox.dart";
import "src/sync_service.dart";

export "src/models.dart";

class RuleMind {
  RuleMind._();

  static RuleMindConfig? _config;
  static BundleManager? _bundleManager;
  static EventLogger? _eventLogger;
  static RuleMindEngine _engine = RuleMindEngine();
  static DecisionCache? _decisionCache;
  static ExecutionStore? _executionStore;
  static DecisionOutbox? _decisionOutbox;
  static DecisionSyncer? _decisionSyncer;
  static http.Client _httpClient = http.Client();

  static Future<void> initialize(RuleMindConfig config) async {
    _config = config;
    _bundleManager = BundleManager(config: config, httpClient: _httpClient);
    _eventLogger = EventLogger(config: config, httpClient: _httpClient);
    _executionStore = ExecutionStore();
    // Durable on-device decision outbox (SQLite). Decisions made offline are queued here
    // and drained to the backend in batches by the background sync; on a device without a
    // SQLite binding (tests/desktop) fall back to an in-memory queue.
    try {
      _decisionOutbox = await SqfliteDecisionOutbox.open();
    } catch (_) {
      _decisionOutbox = InMemoryDecisionOutbox();
    }
    _decisionSyncer = DecisionSyncer(outbox: _decisionOutbox!, uploader: _uploadDecisions);
    SyncService(bundleManager: _bundleManager!, eventLogger: _eventLogger!, decisionSyncer: _decisionSyncer);
    _decisionCache = DecisionCache(
      boxName: "rulemind.decisions",
      ttlMs: config.decisionCacheTtlMs,
      maxEntries: config.decisionCacheMaxEntries,
    );
    await _decisionCache!.initialize();
    await _eventLogger!.initialize();
    await _executionStore!.initialize();
    await SyncService.registerPeriodicSync(intervalMinutes: config.bundleSyncIntervalMinutes);
  }

  /// Drain the durable decision outbox now (e.g. on app foreground / reconnect).
  static Future<void> syncPendingDecisions() async {
    await _decisionSyncer?.sync();
  }

  static Future<Bundle?> syncNow() async {
    return _bundleManager?.syncNow();
  }

  static Future<Map<String, dynamic>> health() async {
    final config = _requireConfig();
    final response = await _httpClient.get(
      Uri.parse("${config.baseUrl.replaceAll(RegExp(r"/+$"), "")}/sdk/v1/health"),
      headers: <String, String>{
        "X-API-Key": config.apiKey,
        "X-SDK-Version": config.sdkVersion,
      },
    );
    if (response.statusCode < 200 || response.statusCode >= 300) {
      throw StateError("Health check failed with HTTP ${response.statusCode}");
    }
    return jsonDecode(response.body) as Map<String, dynamic>;
  }

  static Future<Decision> evaluate(String policyId, Map<String, dynamic> payload, {String? userId}) async {
    final config = _requireConfig();
    Bundle? bundle = await _bundleManager?.currentBundle();
    bundle ??= await syncNow();
    if (bundle == null && config.enableServerFallback) {
      return _serverDecide(policyId, payload, userId: userId);
    }
    final activeBundle = bundle!;
    Experiment? experiment;
    for (final item in activeBundle.experiments) {
      if (item.status == "running" && item.targetPolicyId == policyId) {
        experiment = item;
        break;
      }
    }
    final variant = experiment == null ? null : _engine.getExperiment(activeBundle, experiment.id, userId);
    final cacheKey = _decisionCache!.buildKey(policyId, payload, variant?.id);
    final cached = await _decisionCache!.get(cacheKey);
    if (cached != null && cached.status == "completed" && cached.pendingOperations.isEmpty) {
      return cached;
    }
    final decision = _engine.evaluate(activeBundle, policyId, payload, userId: userId).copyWith(
      source: "edge_bundle",
      policyId: policyId,
      userId: userId,
    );
    await _executionStore?.persist(decision);
    if (decision.status == "completed" && decision.pendingOperations.isEmpty) {
      await _decisionCache!.put(cacheKey, decision);
    }
    // A completed decision goes to the durable outbox (batched, deduped, retried by the
    // background sync). A paused/resumable execution keeps the execution-state sync so it
    // can be resumed. Routing completed decisions through the outbox instead of the old
    // fire-and-forget avoids double-logging (the outbox path is deduped server-side).
    if (decision.status == "completed" && _decisionOutbox != null && decision.executionId != null && decision.policyId != null) {
      await _decisionOutbox!.enqueue(_toPending(decision));
    } else {
      await _syncDecision(decision);
    }
    return decision;
  }

  static PendingDecision _toPending(Decision d) {
    final now = DateTime.now().toUtc().toIso8601String();
    final record = <String, dynamic>{
      "id": d.executionId,
      "policy_id": d.policyId,
      "outcome": d.outcome,
      "payload": d.payload,
      "computed_variables": d.variables,
      "rule_results": d.ruleResults,
      "scorecard_result": d.scorecardResults.values.isNotEmpty ? d.scorecardResults.values.first : null,
      "latency_ms": d.latencyMs,
      "source": "on_device",
      "sdk_version": _config?.sdkVersion,
      "experiment_variant": d.experimentVariant,
      "created_at": now,
    };
    return PendingDecision(
      id: d.executionId!,
      policyId: d.policyId,
      outcome: d.outcome,
      payloadJson: jsonEncode(record),
      createdAt: now,
    );
  }

  /// Uploader the DecisionSyncer calls: POST a batch to /sdk/v1/decisions and return the
  /// server-acked ids (idempotent — a retry after a lost ack never double-counts).
  static Future<UploadResult> _uploadDecisions(List<PendingDecision> batch) async {
    final config = _config;
    if (config == null) return UploadResult.failure();
    try {
      final response = await _httpClient.post(
        Uri.parse("${config.baseUrl.replaceAll(RegExp(r"/+$"), "")}/sdk/v1/decisions"),
        headers: <String, String>{
          "Content-Type": "application/json",
          "X-API-Key": config.apiKey,
          "X-SDK-Version": config.sdkVersion,
        },
        body: jsonEncode(<String, dynamic>{
          "decisions": batch.map((d) => jsonDecode(d.payloadJson)).toList(),
        }),
      );
      if (response.statusCode < 200 || response.statusCode >= 300) {
        return UploadResult.failure();
      }
      final body = jsonDecode(response.body) as Map<String, dynamic>;
      final acked = (body["acked"] as List?)?.map((e) => e.toString()).toList() ?? const <String>[];
      return UploadResult.success(acked);
    } catch (_) {
      return UploadResult.failure();
    }
  }

  static Future<List<Decision>> flushPendingOperations() async {
    final executions = await _executionStore?.list() ?? const <Decision>[];
    if (executions.isEmpty) {
      return const <Decision>[];
    }
    final bundle = await _bundleManager?.currentBundle() ?? await syncNow();
    final updated = <Decision>[];
    for (final execution in executions.where((item) => item.pendingOperations.any((op) => op["status"] != "delivered"))) {
      var next = execution;
      final pending = <Map<String, dynamic>>[];
      final actionResults = execution.actionResults.map((item) => Map<String, dynamic>.from(item)).toList();
      for (final operation in execution.pendingOperations) {
        if (operation["status"] == "delivered") {
          pending.add(Map<String, dynamic>.from(operation));
          continue;
        }
        final result = await _dispatchPendingOperation(operation);
        final updatedOperation = Map<String, dynamic>.from(operation)
          ..["status"] = result["success"] == true ? "delivered" : "failed"
          ..["lastResult"] = result;
        pending.add(updatedOperation);
        final index = actionResults.indexWhere((item) => item["operationId"] == operation["id"]);
        if (index >= 0) {
          actionResults[index] = <String, dynamic>{...actionResults[index], ...result};
        } else {
          actionResults.add(result);
        }
      }
      next = next.copyWith(
        pendingOperations: pending,
        actionResults: actionResults,
        source: "edge_bundle",
      );
      final blockingPending = next.pendingOperations.any((item) => item["blocking"] == true && item["status"] != "delivered");
      if (next.status == "pending_sync" && !blockingPending && bundle != null && next.policyId != null) {
        final resumedSeed = next.copyWith(status: "running");
        next = _engine.evaluate(bundle, next.policyId!, next.payload, userId: next.userId, resumeFrom: resumedSeed).copyWith(
          source: "edge_resume",
          policyId: next.policyId,
          userId: next.userId,
        );
      }
      await _executionStore?.persist(next);
      await _syncDecision(next);
      updated.add(next);
    }
    return updated;
  }

  static Future<Decision?> getExecution(String executionId) async {
    final local = await _executionStore?.get(executionId);
    if (local != null) {
      return local;
    }
    final config = _requireConfig();
    final response = await _httpClient.get(
      Uri.parse("${config.baseUrl.replaceAll(RegExp(r"/+$"), "")}/sdk/v1/executions/$executionId"),
      headers: <String, String>{
        "X-API-Key": config.apiKey,
        "X-SDK-Version": config.sdkVersion,
      },
    );
    if (response.statusCode == 404) {
      return null;
    }
    if (response.statusCode < 200 || response.statusCode >= 300) {
      throw StateError("Execution lookup failed with HTTP ${response.statusCode}");
    }
    return decisionFromMap(jsonDecode(response.body) as Map<String, dynamic>);
  }

  static Future<Decision> resumeExecution(String executionId, ReviewDecision reviewDecision) async {
    final local = await _executionStore?.get(executionId);
    if (local != null && local.status == "paused" && local.policyId != null) {
      final bundle = await _bundleManager?.currentBundle() ?? await syncNow();
      if (bundle == null) {
        throw StateError("No cached bundle available to resume execution.");
      }
      final resumedSeed = local.copyWith(
        status: "running",
        outcome: reviewDecision.decision == "approve" ? "approve" : "reject",
        reviewResponse: <String, dynamic>{...local.reviewResponse, ...reviewDecision.response},
        pausedAtStep: null,
        currentStepIndex: (local.pausedAtStep ?? local.currentStepIndex) + 1,
        reviewTask: <String, dynamic>{
          ...?local.reviewTask,
          "status": reviewDecision.decision == "approve" ? "approved" : "rejected",
          "reviewer_response": reviewDecision.response,
          "reviewed_by": reviewDecision.reviewerId,
        },
      );
      final resumed = _engine.evaluate(bundle, local.policyId!, local.payload, userId: local.userId, resumeFrom: resumedSeed).copyWith(
        source: "edge_resume",
        policyId: local.policyId,
        userId: local.userId,
      );
      await _executionStore?.persist(resumed);
      await _syncDecision(resumed);
      return resumed;
    }
    final config = _requireConfig();
    final response = await _httpClient.post(
      Uri.parse("${config.baseUrl.replaceAll(RegExp(r"/+$"), "")}/sdk/v1/executions/$executionId/resume"),
      headers: <String, String>{
        "Content-Type": "application/json",
        "X-API-Key": config.apiKey,
        "X-SDK-Version": config.sdkVersion,
      },
      body: jsonEncode(<String, dynamic>{
        "decision": reviewDecision.decision,
        "reviewerId": reviewDecision.reviewerId,
        "response": reviewDecision.response,
      }),
    );
    if (response.statusCode < 200 || response.statusCode >= 300) {
      throw StateError("Execution resume failed with HTTP ${response.statusCode}");
    }
    final resumed = decisionFromMap(jsonDecode(response.body) as Map<String, dynamic>);
    await _executionStore?.persist(resumed);
    return resumed;
  }

  static Future<ExperimentVariant?> getExperiment(String experimentId, String? userId) async {
    final bundle = await _bundleManager?.currentBundle();
    if (bundle == null) {
      return null;
    }
    return _engine.getExperiment(bundle, experimentId, userId);
  }

  static Future<void> logEvent(String type, Map<String, dynamic> data) async {
    await _eventLogger?.queue(type, data);
  }

  static Future<void> clearCache() async {
    await _decisionCache?.clearCache();
  }

  /// Reset all static state. Intended for integration tests.
  @visibleForTesting
  static Future<void> resetForTest() async {
    try { await Hive.close(); } catch (_) {}
    _config = null;
    _bundleManager = null;
    _eventLogger = null;
    _engine = RuleMindEngine();
    _decisionCache = null;
    _executionStore = null;
    _httpClient = http.Client();
  }

  static RuleMindConfig _requireConfig() {
    final config = _config;
    if (config == null) {
      throw StateError("RuleMind.initialize() must be called first.");
    }
    return config;
  }

  static Future<void> _syncDecision(Decision decision) async {
    final config = _config;
    if (config == null || decision.executionId == null || decision.policyId == null) {
      return;
    }
    try {
      await _httpClient.post(
        Uri.parse("${config.baseUrl.replaceAll(RegExp(r"/+$"), "")}/sdk/v1/executions/sync"),
        headers: <String, String>{
          "Content-Type": "application/json",
          "X-API-Key": config.apiKey,
          "X-SDK-Version": config.sdkVersion,
        },
        body: jsonEncode(decisionToMap(decision)),
      );
    } catch (_) {
      // Best-effort sync so offline flows remain usable.
    }
  }

  static Future<Map<String, dynamic>> _dispatchPendingOperation(Map<String, dynamic> operation) async {
    final startedAt = DateTime.now().millisecondsSinceEpoch;
    final url = operation["url"]?.toString() ?? "";
    final method = (operation["method"]?.toString() ?? "POST").toUpperCase();
    if (url.startsWith("rulemind://simulate/")) {
      return <String, dynamic>{
        "operationId": operation["id"],
        "stepId": operation["stepId"],
        "url": url,
        "method": method,
        "requestBody": operation["requestBody"],
        "responseStatus": 202,
        "responseBody": '{"simulated":true}',
        "latencyMs": DateTime.now().millisecondsSinceEpoch - startedAt,
        "success": true,
        "status": "delivered",
      };
    }
    final headers = ((operation["headers"] as Map?) ?? const <String, dynamic>{}).map((key, value) => MapEntry(key.toString(), value?.toString() ?? ""));
    final requestBody = operation["requestBody"];
    try {
      late final http.Response response;
      switch (method) {
        case "GET":
          response = await _httpClient.get(Uri.parse(url), headers: headers);
          break;
        case "PUT":
          response = await _httpClient.put(Uri.parse(url), headers: {...headers, "Content-Type": "application/json"}, body: jsonEncode(requestBody));
          break;
        case "PATCH":
          response = await _httpClient.patch(Uri.parse(url), headers: {...headers, "Content-Type": "application/json"}, body: jsonEncode(requestBody));
          break;
        default:
          response = await _httpClient.post(Uri.parse(url), headers: {...headers, "Content-Type": "application/json"}, body: jsonEncode(requestBody));
      }
      return <String, dynamic>{
        "operationId": operation["id"],
        "stepId": operation["stepId"],
        "url": url,
        "method": method,
        "requestBody": requestBody,
        "responseStatus": response.statusCode,
        "responseBody": response.body,
        "latencyMs": DateTime.now().millisecondsSinceEpoch - startedAt,
        "success": response.statusCode >= 200 && response.statusCode < 300,
        "status": response.statusCode >= 200 && response.statusCode < 300 ? "delivered" : "failed",
      };
    } catch (error) {
      return <String, dynamic>{
        "operationId": operation["id"],
        "stepId": operation["stepId"],
        "url": url,
        "method": method,
        "requestBody": requestBody,
        "latencyMs": DateTime.now().millisecondsSinceEpoch - startedAt,
        "success": false,
        "status": "failed",
        "error": error.toString(),
      };
    }
  }

  static Future<Decision> _serverDecide(String policyId, Map<String, dynamic> payload, {String? userId}) async {
    final config = _requireConfig();
    final response = await _httpClient.post(
      Uri.parse("${config.baseUrl.replaceAll(RegExp(r"/+$"), "")}/sdk/v1/decide"),
      headers: <String, String>{
        "Content-Type": "application/json",
        "X-API-Key": config.apiKey,
        "X-SDK-Version": config.sdkVersion,
      },
      body: jsonEncode(<String, dynamic>{
        "policyId": policyId,
        "payload": payload,
        "userId": userId,
        "sdkVersion": config.sdkVersion,
      }),
    );
    final decision = decisionFromMap(jsonDecode(response.body) as Map<String, dynamic>).copyWith(
      source: "sdk_server",
      policyId: policyId,
      userId: userId,
      payload: payload,
    );
    await _executionStore?.persist(decision);
    return decision;
  }
}

extension on Decision {
  Decision copyWith({
    String? outcome,
    double? score,
    Map<String, dynamic>? variables,
    List<Map<String, dynamic>>? ruleResults,
    String? experimentId,
    String? experimentVariant,
    int? latencyMs,
    String? requestId,
    List<String>? serverOnlyStepsSkipped,
    String? executionId,
    String? status,
    List<Map<String, dynamic>>? trace,
    Map<String, Map<String, dynamic>>? scorecardResults,
    List<Map<String, dynamic>>? actionResults,
    List<Map<String, dynamic>>? pendingOperations,
    Map<String, dynamic>? reviewTask,
    Map<String, dynamic>? explainability,
    Map<String, dynamic>? auditSummary,
    String? source,
    String? policyId,
    String? userId,
    Map<String, dynamic>? payload,
    Map<String, Map<String, dynamic>>? transformOutputs,
    int? currentStepIndex,
    int? pausedAtStep,
    Map<String, dynamic>? reviewResponse,
  }) {
    return Decision(
      outcome: outcome ?? this.outcome,
      score: score ?? this.score,
      variables: variables ?? this.variables,
      ruleResults: ruleResults ?? this.ruleResults,
      experimentId: experimentId ?? this.experimentId,
      experimentVariant: experimentVariant ?? this.experimentVariant,
      latencyMs: latencyMs ?? this.latencyMs,
      requestId: requestId ?? this.requestId,
      serverOnlyStepsSkipped: serverOnlyStepsSkipped ?? this.serverOnlyStepsSkipped,
      executionId: executionId ?? this.executionId,
      status: status ?? this.status,
      trace: trace ?? this.trace,
      scorecardResults: scorecardResults ?? this.scorecardResults,
      actionResults: actionResults ?? this.actionResults,
      pendingOperations: pendingOperations ?? this.pendingOperations,
      reviewTask: reviewTask ?? this.reviewTask,
      explainability: explainability ?? this.explainability,
      auditSummary: auditSummary ?? this.auditSummary,
      source: source ?? this.source,
      policyId: policyId ?? this.policyId,
      userId: userId ?? this.userId,
      payload: payload ?? this.payload,
      transformOutputs: transformOutputs ?? this.transformOutputs,
      currentStepIndex: currentStepIndex ?? this.currentStepIndex,
      pausedAtStep: pausedAtStep ?? this.pausedAtStep,
      reviewResponse: reviewResponse ?? this.reviewResponse,
    );
  }
}
