import "dart:math";

import "decision_table_evaluator.dart";
import "models.dart";
import "rule_evaluator.dart";
import "scorecard_evaluator.dart";
import "variable_vm.dart";

/// Raised by a step the offline engine deliberately refuses to fake. Caught by the step loop and
/// surfaced as the step's `error`, exactly as the Python executor surfaces a raised step.
class UnsupportedStepException implements Exception {
  UnsupportedStepException(this.message);

  final String message;

  @override
  String toString() => message;
}

/// Everything a single `evaluate()` mutates. Held in one object because control-flow steps
/// (`loop`, `branch`, `workflow`) re-enter the step loop with the SAME live context, exactly as
/// the Python executor does with its `ExecutionContext`.
class _RunState {
  _RunState(this.bundle, this.payload, this.executionId);

  final Bundle bundle;
  final Map<String, dynamic> payload;
  final String executionId;

  final Map<String, dynamic> variables = <String, dynamic>{};
  final List<Map<String, dynamic>> ruleResults = <Map<String, dynamic>>[];
  final Map<String, Map<String, dynamic>> scorecardResults = <String, Map<String, dynamic>>{};
  final Map<String, Map<String, dynamic>> transformOutputs = <String, Map<String, dynamic>>{};
  final List<Map<String, dynamic>> actionResults = <Map<String, dynamic>>[];
  final List<Map<String, dynamic>> pendingOperations = <Map<String, dynamic>>[];
  final List<Map<String, dynamic>> trace = <Map<String, dynamic>>[];
  final List<String> skipped = <String>[];
  final Map<String, dynamic> reviewResponse = <String, dynamic>{};

  /// Loop bindings for the innermost `loop` step, visible to sub-step conditions/templates.
  Map<String, dynamic> loopScope = const <String, dynamic>{};

  /// Sub-workflow cycle guard — seeded with the root policy id, like the Python executor.
  final Set<String> visitedWorkflows = <String>{};

  String outcome = "pending";
  double? score;
  String status = "running";
  int currentStepIndex = 0;
  int? pausedAtStep;
  Map<String, dynamic>? reviewTask;
}

class PolicyExecutor {
  PolicyExecutor({
    VariableVm? variableVm,
    RuleEvaluator? ruleEvaluator,
    ScorecardEvaluator? scorecardEvaluator,
    DecisionTableEvaluator? decisionTableEvaluator,
  })  : _variableVm = variableVm ?? VariableVm(),
        _ruleEvaluator = ruleEvaluator ?? RuleEvaluator(),
        _scorecardEvaluator = scorecardEvaluator ?? ScorecardEvaluator(),
        _decisionTableEvaluator = decisionTableEvaluator ?? DecisionTableEvaluator(ruleEvaluator: ruleEvaluator);

  /// Matches `_MAX_WORKFLOW_DEPTH` in the Python executor.
  static const int maxWorkflowDepth = 10;
  static const int _loopHardCap = 10000;

  final VariableVm _variableVm;
  final RuleEvaluator _ruleEvaluator;
  final ScorecardEvaluator _scorecardEvaluator;
  final DecisionTableEvaluator _decisionTableEvaluator;

  String _mergeOutcome(String? current, String? candidate) {
    final currentValue = current ?? "pending";
    final candidateValue = candidate ?? currentValue;
    const precedence = <String, int>{
      "pending": 0,
      "pass": 1,
      "approve": 2,
      "review": 3,
      "reject": 4,
    };
    final currentRank = precedence[currentValue] ?? 0;
    final candidateRank = precedence[candidateValue] ?? 0;
    if (candidateRank > currentRank) {
      return candidateValue;
    }
    if (candidateRank < currentRank) {
      return currentValue;
    }
    if (currentValue == "pass" && candidateValue == "approve") {
      return candidateValue;
    }
    return currentValue;
  }

  Decision evaluate(Bundle bundle, Policy policy, Map<String, dynamic> payload, {Decision? resumeFrom}) {
    final startedAt = DateTime.now().millisecondsSinceEpoch;
    final executionId = resumeFrom?.executionId ?? _executionId();
    final effectivePayload = resumeFrom != null && resumeFrom.payload.isNotEmpty ? resumeFrom.payload : payload;

    final state = _RunState(bundle, effectivePayload, executionId);
    if (resumeFrom != null) {
      state.variables.addAll(resumeFrom.variables);
      state.ruleResults.addAll(resumeFrom.ruleResults.map(Map<String, dynamic>.from));
      state.scorecardResults.addAll(
        resumeFrom.scorecardResults.map((key, value) => MapEntry(key, Map<String, dynamic>.from(value))),
      );
      state.transformOutputs.addAll(
        resumeFrom.transformOutputs.map((key, value) => MapEntry(key, Map<String, dynamic>.from(value))),
      );
      state.actionResults.addAll(resumeFrom.actionResults.map(Map<String, dynamic>.from));
      state.pendingOperations.addAll(resumeFrom.pendingOperations.map(Map<String, dynamic>.from));
      state.trace.addAll(resumeFrom.trace.map(Map<String, dynamic>.from));
      state.skipped.addAll(resumeFrom.serverOnlyStepsSkipped);
      state.reviewResponse.addAll(resumeFrom.reviewResponse);
      state.outcome = resumeFrom.outcome;
      state.score = resumeFrom.score;
      state.status = resumeFrom.status;
      state.currentStepIndex = resumeFrom.currentStepIndex;
      state.pausedAtStep = resumeFrom.pausedAtStep;
      state.reviewTask = resumeFrom.reviewTask == null ? null : Map<String, dynamic>.from(resumeFrom.reviewTask!);
    }
    state.visitedWorkflows.add(policy.id);

    if (resumeFrom != null && state.reviewResponse.isNotEmpty) {
      state.reviewResponse.forEach((key, value) {
        if (key != "decision") {
          state.variables[key] = value;
        }
      });
      switch (state.reviewResponse["decision"]?.toString()) {
        case "approve":
          state.outcome = "approve";
        case "reject":
          state.outcome = "reject";
      }
    }

    if (resumeFrom == null) {
      for (final variable in bundle.variables.where((item) => item.compilable && !bundle.serverOnlyVariables.contains(item.id))) {
        state.variables[variable.id] = _variableVm.evaluate(variable, effectivePayload, state.variables);
      }
    }

    _runSteps(policy.steps, state, state.currentStepIndex, 0);

    if (state.status == "running") {
      state.status = "completed";
      state.currentStepIndex = policy.steps.length;
    }
    if (state.outcome == "pending") {
      state.outcome = policy.defaultOutcome ?? "review";
    }

    return Decision(
      outcome: state.outcome,
      score: state.score,
      variables: state.variables,
      ruleResults: state.ruleResults,
      latencyMs: DateTime.now().millisecondsSinceEpoch - startedAt,
      serverOnlyStepsSkipped: state.skipped.toSet().toList(),
      executionId: executionId,
      status: state.status,
      trace: state.trace,
      scorecardResults: state.scorecardResults,
      actionResults: state.actionResults,
      pendingOperations: state.pendingOperations,
      reviewTask: state.reviewTask,
      explainability: <String, dynamic>{
        "rules": state.ruleResults,
        "scorecards": state.scorecardResults,
        "trace": state.trace,
      },
      auditSummary: <String, dynamic>{
        "traceSteps": state.trace.length,
        "pendingOperationCount": state.pendingOperations.where((item) => item["status"] != "delivered").length,
        "actionCount": state.actionResults.length,
        "reviewTaskId": state.reviewTask?["id"],
      },
      policyId: policy.id,
      payload: effectivePayload,
      transformOutputs: state.transformOutputs,
      currentStepIndex: state.currentStepIndex,
      pausedAtStep: state.pausedAtStep,
      reviewResponse: state.reviewResponse,
    );
  }

  /// Run a list of steps against the live context. Re-entered by `loop`, `branch` and `workflow`
  /// at `depth + 1`; only the top level tracks `currentStepIndex` for durable resume.
  void _runSteps(List<PolicyStep> steps, _RunState state, int startIndex, int depth) {
    for (var index = startIndex; index < steps.length; index += 1) {
      final step = steps[index];
      if (depth == 0) {
        state.currentStepIndex = index;
      }
      final condition = step.config["condition"]?.toString();
      if (condition != null && condition.isNotEmpty && !_evaluateCondition(condition, _contextView(state))) {
        state.trace.add(<String, dynamic>{"step": _traceStep(step), "skipped": true, "reason": "Condition not met: $condition"});
        continue;
      }

      final stepStarted = DateTime.now().millisecondsSinceEpoch;
      Map<String, dynamic>? result;
      String? error;
      try {
        final body = _executeStepBody(step, state, depth);
        result = body.result;
        error = body.error;
      } catch (failure) {
        // A step that throws must not take the whole decision down, and must not vanish.
        error = failure is UnsupportedStepException ? failure.message : failure.toString();
        // Fail CLOSED on a decision GATE: a rule/scorecard/table that errored must never be
        // treated as passed, so escalate to human review (parity with the Python core).
        if (step.type == "rule" || step.type == "scorecard" || step.type == "decision_table") {
          state.outcome = _mergeOutcome(state.outcome, "review");
        }
        if (step.config["onFailure"]?.toString() == "abort") {
          state.status = "failed";
        }
      }

      state.trace.add(<String, dynamic>{
        "step": _traceStep(step),
        "result": result,
        "error": error,
        "duration_ms": DateTime.now().millisecondsSinceEpoch - stepStarted,
      });

      if (state.status == "failed" || state.status == "paused" || state.status == "pending_sync") {
        return;
      }
    }
  }

  /// Dispatch one step. Mirrors `_execute_step_body` in the Python core, including its final line:
  /// an unrecognised step type is an ERROR, not a silent skip.
  _StepOutcome _executeStepBody(PolicyStep step, _RunState state, int depth) {
    final refId = step.refId ?? step.ref;
    switch (step.type) {
      case "connector":
        final payloadKeys = ((state.payload[refId] as Map?) ?? const <String, dynamic>{})
            .keys
            .map((item) => item.toString())
            .toList()
          ..sort();
        return _StepOutcome(<String, dynamic>{"status": "ok", "payloadKeys": payloadKeys});

      case "rule":
        final rule = _firstOrNull(state.bundle.rules, (item) => item.id == refId);
        if (rule == null) {
          state.skipped.add(step.id ?? refId ?? step.type);
          return _StepOutcome(null, "Unknown rule: $refId");
        }
        final result = _ruleEvaluator.evaluate(rule, state.variables);
        state.ruleResults.add(result);
        state.outcome = _mergeOutcome(state.outcome, result["outcome"]?.toString());
        return _StepOutcome(result);

      case "decision_table":
        final table = _firstOrNull(state.bundle.decisionTables, (item) => item["id"] == refId);
        if (table == null) {
          state.skipped.add(step.id ?? refId ?? step.type);
          return _StepOutcome(null, "Unknown decision table: $refId");
        }
        final result = _decisionTableEvaluator.evaluate(table, state.variables);
        state.outcome = _mergeOutcome(state.outcome, result["outcome"]?.toString());
        return _StepOutcome(result);

      case "scorecard":
        final scorecard = _firstOrNull(state.bundle.scorecards, (item) => item.id == refId);
        if (scorecard == null) {
          state.skipped.add(step.id ?? refId ?? step.type);
          return _StepOutcome(null, "Unknown scorecard: $refId");
        }
        final result = _scorecardEvaluator.evaluate(scorecard, state.variables, _variableLookup(state));
        if (refId != null) {
          state.scorecardResults[refId] = result;
        }
        state.score = (result["score"] as num?)?.toDouble();
        return _StepOutcome(result);

      case "transform":
        final result = _executeTransform(step, _contextView(state));
        state.transformOutputs[step.config["outputKey"]?.toString() ?? "transformed"] = result;
        return _StepOutcome(result);

      case "action":
        final pending = _queueAction(step, _contextView(state));
        state.pendingOperations.add(pending);
        final result = <String, dynamic>{
          "operationId": pending["id"],
          "stepId": step.id ?? step.refId ?? step.ref ?? step.type,
          "queued": true,
          "success": false,
          "status": "queued",
          "blocking": pending["blocking"] == true,
          "url": pending["url"],
          "method": pending["method"],
          "requestBody": pending["requestBody"],
        };
        state.actionResults.add(result);
        if (pending["blocking"] == true) {
          state.status = "pending_sync";
          if (depth == 0) {
            state.currentStepIndex += 1;
          }
        }
        return _StepOutcome(result);

      case "review_gate":
        final task = _createReviewTask(step, state);
        state.reviewTask = task;
        state.status = "paused";
        state.pausedAtStep = state.currentStepIndex;
        return _StepOutcome(<String, dynamic>{"paused": true, "reviewTaskId": task["id"]});

      case "outcome":
        state.outcome = _mergeOutcome(
          state.outcome,
          step.refId ?? step.ref ?? step.config["outcome"]?.toString() ?? step.label ?? state.outcome,
        );
        return _StepOutcome(<String, dynamic>{"outcome": state.outcome});

      case "loop":
        return _StepOutcome(_executeLoop(step, state, depth));

      case "branch":
        return _StepOutcome(_executeBranch(step, state, depth));

      case "workflow":
        return _StepOutcome(_executeSubWorkflow(step, state, depth));

      // `model` needs the model blob and an ML runtime that only the server has; `monitor` is
      // post-decision server telemetry (audit events + alert webhooks + scheduled re-evaluation).
      // Neither can be honoured offline. They FAIL LOUDLY here rather than being skipped, because
      // a policy that silently drops its model step still returns a decision — one that was never
      // scored by the model it was authored around.
      case "model":
        throw UnsupportedStepException(
          "Model steps require the server runtime and cannot run offline: ${refId ?? step.id ?? "model"}",
        );

      case "monitor":
        throw UnsupportedStepException(
          "Monitor steps are server-side telemetry and cannot run offline: ${step.id ?? step.label ?? "monitor"}",
        );

      // Parity with `_execute_step_body`'s final line. Previously this branch marked the step
      // "skipped" and moved on, so a policy carrying a step type this SDK version does not know
      // produced a full decision with that step silently missing.
      default:
        return _StepOutcome(null, "Unknown step type: ${step.type}");
    }
  }

  // ── Control flow ──

  Map<String, dynamic> _executeLoop(PolicyStep step, _RunState state, int depth) {
    if (depth >= maxWorkflowDepth) {
      throw UnsupportedStepException("Max loop/workflow nesting depth ($maxWorkflowDepth) exceeded");
    }
    final config = step.config;
    final asName = config["as"]?.toString() ?? "item";
    final indexName = config["indexAs"]?.toString() ?? "index";
    final subSteps = _toSteps(config["steps"]);
    final maxIterations = min(_intConfig(config["maxIterations"], 1000), _loopHardCap);

    final collection = _resolveCollection(config["over"] ?? config["items"], state);
    final List<dynamic> items;
    if (collection is Map) {
      // A map iterates as {key, value} pairs; anything non-iterable is a no-op.
      items = collection.entries
          .map((entry) => <String, dynamic>{"key": entry.key?.toString(), "value": entry.value})
          .toList();
    } else if (collection is List) {
      items = List<dynamic>.from(collection);
    } else {
      items = const <dynamic>[];
    }

    final outerScope = state.loopScope;
    final truncated = items.length > maxIterations;
    final sliced = items.take(maxIterations).toList();
    final parallel = (config["mode"]?.toString().toLowerCase() ?? "sequential") == "parallel";
    final entryOutcome = state.outcome;

    final iterations = <Map<String, dynamic>>[];
    var cumulative = entryOutcome;
    var anyFailed = false;
    for (var index = 0; index < sliced.length; index += 1) {
      final item = sliced[index];
      state.loopScope = <String, dynamic>{...outerScope, asName: item, indexName: index};
      final outcomeBefore = parallel ? cumulative : state.outcome;
      // The server runs a parallel loop on isolated per-iteration contexts that each START from
      // the entry outcome and are merged back in index order. Reproducing that merge is what keeps
      // the OUTCOME identical; there is no concurrency to win on-device.
      if (parallel) {
        state.outcome = entryOutcome;
      }
      final traceStart = state.trace.length;
      _runSteps(subSteps, state, 0, depth + 1);
      if (parallel) {
        cumulative = _mergeOutcome(cumulative, state.outcome);
        state.outcome = cumulative;
      }
      final failed = state.status == "failed";
      anyFailed = anyFailed || failed;
      iterations.add(<String, dynamic>{
        "index": index,
        "item": item,
        "outcome_before": outcomeBefore,
        "outcome_after": parallel ? cumulative : state.outcome,
        "steps": state.trace.sublist(traceStart),
        "paused": state.status == "paused",
        "failed": failed,
      });
      if (parallel && state.status == "paused") {
        // Same refusal as the server: a durable pause cannot be represented across concurrent
        // iterations, so it is rejected rather than losing the resume point.
        state.loopScope = outerScope;
        throw UnsupportedStepException(
          "Review gates / durable pauses are not supported inside a parallel loop; use sequential mode.",
        );
      }
      if (!parallel && (state.status == "paused" || state.status == "failed")) {
        break;
      }
      if (parallel && state.status == "failed") {
        // A parallel loop runs every iteration; a failure is reported after the fact.
        state.status = "running";
      }
    }
    state.loopScope = outerScope;
    if (parallel && anyFailed) {
      state.status = "failed";
    }

    return <String, dynamic>{
      "loop": step.id ?? step.label ?? "loop",
      "over": config["over"],
      "count": items.length,
      "iterations_run": iterations.length,
      "truncated": truncated,
      "mode": parallel ? "parallel" : "sequential",
      "iterations": iterations,
    };
  }

  Map<String, dynamic> _executeBranch(PolicyStep step, _RunState state, int depth) {
    final branches = (step.config["branches"] as List<dynamic>?) ?? const <dynamic>[];
    for (var index = 0; index < branches.length; index += 1) {
      final branch = branches[index];
      if (branch is! Map) continue;
      final condition = branch["condition"]?.toString();
      if (condition == null || condition.isEmpty || _evaluateCondition(condition, _contextView(state))) {
        _runSteps(_toSteps(branch["steps"]), state, 0, depth + 1);
        return <String, dynamic>{"branch": index, "label": branch["label"] ?? index, "matched": true};
      }
    }
    final defaultSteps = _toSteps(step.config["default"]);
    if (defaultSteps.isNotEmpty) {
      _runSteps(defaultSteps, state, 0, depth + 1);
      return <String, dynamic>{"branch": "default", "matched": true};
    }
    return <String, dynamic>{"branch": null, "matched": false};
  }

  Map<String, dynamic> _executeSubWorkflow(PolicyStep step, _RunState state, int depth) {
    if (depth >= maxWorkflowDepth) {
      throw UnsupportedStepException("Max sub-workflow depth ($maxWorkflowDepth) exceeded");
    }
    final refId = step.refId ?? step.ref ?? "";
    if (refId.isEmpty) {
      throw UnsupportedStepException("Sub-workflow step missing ref_id");
    }
    if (state.visitedWorkflows.contains(refId)) {
      throw UnsupportedStepException("Sub-workflow cycle detected: $refId");
    }
    final subPolicy = _firstOrNull(state.bundle.policies, (item) => item.id == refId);
    if (subPolicy == null) {
      throw UnsupportedStepException("Unknown sub-workflow: $refId");
    }
    state.visitedWorkflows.add(refId);
    _runSteps(subPolicy.steps, state, 0, depth + 1);
    return <String, dynamic>{"workflow": refId, "outcome": state.outcome};
  }

  /// Loop iterables: a literal list, a `$.`-path, or a dotted path into the context view.
  dynamic _resolveCollection(dynamic over, _RunState state) {
    if (over is List) return over;
    if (over is! String || over.isEmpty) return null;
    return _resolvePath(_contextView(state), over.startsWith(r"$.") ? over.substring(2) : over);
  }

  List<PolicyStep> _toSteps(dynamic raw) {
    if (raw is! List) return const <PolicyStep>[];
    return raw.whereType<Map>().map((item) {
      final config = item["config"];
      return PolicyStep(
        id: item["id"]?.toString(),
        type: item["type"]?.toString() ?? "",
        ref: item["ref"]?.toString(),
        refId: (item["refId"] ?? item["ref_id"])?.toString(),
        label: (item["label"] ?? item["name"])?.toString(),
        config: config is Map
            ? config.map((key, value) => MapEntry(key.toString(), value))
            : const <String, dynamic>{},
      );
    }).toList();
  }

  int _intConfig(dynamic value, int fallback) {
    if (value is num) return value.toInt();
    if (value is String) return int.tryParse(value.trim()) ?? fallback;
    return fallback;
  }

  Map<String, Map<String, dynamic>> _variableLookup(_RunState state) => <String, Map<String, dynamic>>{
        for (final variable in state.bundle.variables)
          variable.id: <String, dynamic>{"name": variable.name, "source_id": variable.sourceId},
      };

  static T? _firstOrNull<T>(List<T> items, bool Function(T) test) {
    for (final item in items) {
      if (test(item)) return item;
    }
    return null;
  }

  // ── Step helpers ──

  Map<String, dynamic> _executeTransform(PolicyStep step, Map<String, dynamic> context) {
    final mapping = (step.config["mapping"] as Map<dynamic, dynamic>?) ?? const <dynamic, dynamic>{};
    final result = <String, dynamic>{};
    for (final entry in mapping.entries) {
      final target = entry.key.toString();
      final value = entry.value;
      if (value is String) {
        result[target] = value.startsWith(r"$.") ? _resolvePath(context, value.substring(2)) : _resolveTemplate(value, context);
      } else if (value is Map<dynamic, dynamic>) {
        final expr = value["expr"]?.toString();
        if (expr != null && expr.isNotEmpty) {
          result[target] = _evaluateExpression(expr, context);
        } else {
          result[target] = value.map((key, nestedValue) => MapEntry(key.toString(), _resolveTemplate(nestedValue, context)));
        }
      } else {
        result[target] = value;
      }
    }
    return result;
  }

  Map<String, dynamic> _queueAction(PolicyStep step, Map<String, dynamic> context) {
    final blocking = step.config["onFailure"]?.toString() == "abort";
    return <String, dynamic>{
      "id": _operationId(),
      "stepId": step.id ?? step.refId ?? step.ref ?? step.type,
      "actionName": step.label ?? step.config["name"]?.toString() ?? step.type,
      "url": _resolveTemplate(step.config["url"]?.toString() ?? "", context),
      "method": (step.config["method"]?.toString() ?? "POST").toUpperCase(),
      "headers": _resolveTemplate(step.config["headers"] ?? const <String, dynamic>{}, context),
      "requestBody": _resolveTemplate(step.config["bodyTemplate"] ?? const <String, dynamic>{}, context),
      "blocking": blocking,
      "status": "queued",
      "createdAt": DateTime.now().millisecondsSinceEpoch,
    };
  }

  Map<String, dynamic> _createReviewTask(PolicyStep step, _RunState state) {
    return <String, dynamic>{
      "id": "local_review_${_operationId().substring(3)}",
      "execution_id": state.executionId,
      "step_id": step.id ?? step.refId ?? step.ref ?? step.type,
      "queue": step.config["assignTo"]?.toString() ?? "mobile_review",
      "status": "pending",
      "required_fields": ((step.config["requiredFields"] as List<dynamic>?) ?? const <dynamic>[]).map((item) => item.toString()).toList(),
      "context_snapshot": <String, dynamic>{
        "variables": state.variables,
        "rule_results": state.ruleResults,
        "scorecard_results": state.scorecardResults,
        "outcome_before_review": state.outcome,
      },
    };
  }

  Map<String, dynamic> _contextView(_RunState state) {
    final computed = state.transformOutputs["computed"] ??
        state.transformOutputs.values.fold<Map<String, dynamic>>(<String, dynamic>{}, (acc, item) {
          acc.addAll(item);
          return acc;
        });
    return <String, dynamic>{
      "payload": state.payload,
      "variables": state.variables,
      "scorecard": state.scorecardResults,
      // Loop bindings live at the top level (referenceable by their `as`/`indexAs` names) plus
      // under a stable `loop` key, exactly as the Python context view exposes them.
      "loop": state.loopScope,
      ...state.loopScope,
      "computed": computed,
      "transforms": state.transformOutputs,
      "outcome": state.outcome,
      "execution_id": state.executionId,
      "review": state.reviewResponse,
    };
  }

  dynamic _resolveTemplate(dynamic value, Map<String, dynamic> context) {
    if (value is String) {
      final matches = RegExp(r"\{\{\s*([^}]+)\s*}}").allMatches(value).toList();
      if (matches.isEmpty) {
        return value;
      }
      if (matches.length == 1 && matches.first.group(0) == value) {
        return _resolvePath(context, matches.first.group(1)!.trim());
      }
      var resolved = value;
      for (final match in matches) {
        final replacement = _resolvePath(context, match.group(1)!.trim());
        resolved = resolved.replaceFirst(match.group(0)!, replacement?.toString() ?? "");
      }
      return resolved;
    }
    if (value is Map<dynamic, dynamic>) {
      return value.map((key, nested) => MapEntry(key.toString(), _resolveTemplate(nested, context)));
    }
    if (value is List<dynamic>) {
      return value.map((item) => _resolveTemplate(item, context)).toList();
    }
    return value;
  }

  bool _evaluateCondition(String expression, Map<String, dynamic> context) {
    final trimmed = expression.trim();
    if (trimmed.contains(" and ")) {
      return trimmed.split(" and ").every((item) => _evaluateCondition(item, context));
    }
    if (trimmed.contains(" or ")) {
      return trimmed.split(" or ").any((item) => _evaluateCondition(item, context));
    }
    final match = RegExp(r"^([A-Za-z0-9_\.]+)\s*(==|!=|>=|<=|>|<)\s*(.+)$").firstMatch(trimmed);
    if (match == null) {
      return false;
    }
    final left = _resolvePath(context, match.group(1)!);
    final right = _parseLiteral(match.group(3)!);
    return _compare(left, match.group(2)!, right);
  }

  dynamic _evaluateExpression(String expression, Map<String, dynamic> context) {
    final match = RegExp(r"^(.+)\s*([+\-*/])\s*(.+)$").firstMatch(expression.trim());
    if (match == null) {
      return _resolvePath(context, expression.trim().replaceFirst(r"$.", ""));
    }
    final left = _numeric(_parseOperand(match.group(1)!, context));
    final right = _numeric(_parseOperand(match.group(3)!, context));
    switch (match.group(2)) {
      case "+":
        return left + right;
      case "-":
        return left - right;
      case "*":
        return left * right;
      case "/":
        return right == 0 ? 0.0 : left / right;
      default:
        return left;
    }
  }

  dynamic _parseOperand(String value, Map<String, dynamic> context) {
    final trimmed = value.trim();
    return trimmed.startsWith(r"$.") ? _resolvePath(context, trimmed.substring(2)) : _parseLiteral(trimmed);
  }

  dynamic _parseLiteral(String value) {
    final trimmed = value.trim();
    if ((trimmed.startsWith("'") && trimmed.endsWith("'")) || (trimmed.startsWith('"') && trimmed.endsWith('"'))) {
      return trimmed.substring(1, trimmed.length - 1);
    }
    return double.tryParse(trimmed) ??
        switch (trimmed.toLowerCase()) {
          "true" => true,
          "false" => false,
          "null" => null,
          _ => trimmed,
        };
  }

  bool _compare(dynamic left, String operator, dynamic right) {
    switch (operator) {
      case ">":
        return _numeric(left) > _numeric(right);
      case ">=":
        return _numeric(left) >= _numeric(right);
      case "<":
        return _numeric(left) < _numeric(right);
      case "<=":
        return _numeric(left) <= _numeric(right);
      case "!=":
        return _normalize(left) != _normalize(right);
      default:
        return _normalize(left) == _normalize(right);
    }
  }

  dynamic _normalize(dynamic value) {
    if (value is num) {
      return value.toDouble();
    }
    if (value is String) {
      return value.trim();
    }
    return value;
  }

  double _numeric(dynamic value) {
    if (value is num) {
      return value.toDouble();
    }
    if (value is String) {
      return double.tryParse(value) ?? 0.0;
    }
    if (value is bool) {
      return value ? 1.0 : 0.0;
    }
    return 0.0;
  }

  dynamic _resolvePath(dynamic root, String rawPath) {
    final path = rawPath.startsWith(r"$.") ? rawPath.substring(2) : rawPath;
    if (root == null || path.isEmpty) {
      return root;
    }
    dynamic current = root;
    for (final segment in path.split(".")) {
      if (current == null) {
        return null;
      }
      final match = RegExp(r"([A-Za-z0-9_\-]+)(\[(\-?\d+)\])?").firstMatch(segment);
      if (match == null) {
        return null;
      }
      final key = match.group(1)!;
      current = current is Map<String, dynamic> ? current[key] : (current is Map ? current[key] : null);
      final indexGroup = match.group(3);
      if (indexGroup != null) {
        final list = current is List<dynamic> ? current : null;
        if (list == null) {
          return null;
        }
        final requested = int.parse(indexGroup);
        final resolved = requested < 0 ? list.length + requested : requested;
        current = resolved >= 0 && resolved < list.length ? list[resolved] : null;
      }
    }
    return current;
  }

  Map<String, dynamic> _traceStep(PolicyStep step) => <String, dynamic>{
        if (step.id != null) "id": step.id,
        "type": step.type,
        if (step.ref != null) "ref": step.ref,
        if (step.refId != null) "refId": step.refId,
        if (step.label != null) "label": step.label,
        if (step.config.isNotEmpty) "config": step.config,
      };

  String _executionId() => "exec_${DateTime.now().millisecondsSinceEpoch}_${Random().nextInt(100000)}";
  String _operationId() => "op_${DateTime.now().millisecondsSinceEpoch}_${Random().nextInt(100000)}";
}

class _StepOutcome {
  _StepOutcome(this.result, [this.error]);

  final Map<String, dynamic>? result;
  final String? error;
}
