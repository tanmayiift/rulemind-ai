import "dart:math";

import "decision_table_evaluator.dart";
import "models.dart";
import "rule_evaluator.dart";
import "scorecard_evaluator.dart";
import "variable_vm.dart";

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
    final variables = Map<String, dynamic>.from(resumeFrom?.variables ?? const <String, dynamic>{});
    final ruleResults = (resumeFrom?.ruleResults ?? const <Map<String, dynamic>>[]).map((item) => Map<String, dynamic>.from(item)).toList();
    final scorecardResults = (resumeFrom?.scorecardResults ?? const <String, Map<String, dynamic>>{})
        .map((key, value) => MapEntry(key, Map<String, dynamic>.from(value)));
    final transformOutputs = (resumeFrom?.transformOutputs ?? const <String, Map<String, dynamic>>{})
        .map((key, value) => MapEntry(key, Map<String, dynamic>.from(value)));
    final actionResults = (resumeFrom?.actionResults ?? const <Map<String, dynamic>>[]).map((item) => Map<String, dynamic>.from(item)).toList();
    final pendingOperations = (resumeFrom?.pendingOperations ?? const <Map<String, dynamic>>[]).map((item) => Map<String, dynamic>.from(item)).toList();
    final trace = (resumeFrom?.trace ?? const <Map<String, dynamic>>[]).map((item) => Map<String, dynamic>.from(item)).toList();
    final skipped = (resumeFrom?.serverOnlyStepsSkipped ?? const <String>[]).toList();
    final reviewResponse = Map<String, dynamic>.from(resumeFrom?.reviewResponse ?? const <String, dynamic>{});
    var reviewTask = resumeFrom?.reviewTask == null ? null : Map<String, dynamic>.from(resumeFrom!.reviewTask!);
    var currentStepIndex = resumeFrom?.currentStepIndex ?? 0;
    var pausedAtStep = resumeFrom?.pausedAtStep;
    var outcome = resumeFrom?.outcome ?? "pending";
    var score = resumeFrom?.score;
    var status = resumeFrom?.status ?? "running";

    if (resumeFrom != null && reviewResponse.isNotEmpty) {
      reviewResponse.forEach((key, value) {
        if (key != "decision") {
          variables[key] = value;
        }
      });
      switch (reviewResponse["decision"]?.toString()) {
        case "approve":
          outcome = "approve";
          break;
        case "reject":
          outcome = "reject";
          break;
      }
    }

    if (resumeFrom == null) {
      for (final variable in bundle.variables.where((item) => item.compilable && !bundle.serverOnlyVariables.contains(item.id))) {
        variables[variable.id] = _variableVm.evaluate(variable, effectivePayload, variables);
      }
    }

    for (var index = currentStepIndex; index < policy.steps.length; index += 1) {
      final step = policy.steps[index];
      currentStepIndex = index;
      final condition = step.config["condition"]?.toString();
      final context = _contextView(effectivePayload, variables, scorecardResults, transformOutputs, outcome, executionId, reviewResponse);
      if (condition != null && condition.isNotEmpty && !_evaluateCondition(condition, context)) {
        trace.add(<String, dynamic>{"step": _traceStep(step), "skipped": true, "reason": "Condition not met: $condition"});
        continue;
      }

      final stepStarted = DateTime.now().millisecondsSinceEpoch;
      Map<String, dynamic>? result;
      String? error;

      switch (step.type) {
        case "connector":
          final refId = step.refId ?? step.ref;
          final payloadKeys = ((effectivePayload[refId] as Map?) ?? const <String, dynamic>{}).keys.map((item) => item.toString()).toList()..sort();
          result = <String, dynamic>{
            "status": "ok",
            "payloadKeys": payloadKeys,
          };
          break;
        case "rule":
          final refId = step.refId ?? step.ref;
          final rule = bundle.rules.firstWhere((item) => item.id == refId, orElse: () => _MissingRule.instance);
          if (rule is _MissingRule) {
            skipped.add(step.id ?? refId ?? step.type);
            error = "Unknown rule: $refId";
          } else {
            result = _ruleEvaluator.evaluate(rule, variables);
            ruleResults.add(result);
            outcome = _mergeOutcome(outcome, result["outcome"]?.toString());
          }
          break;
        case "decision_table":
          final refId = step.refId ?? step.ref;
          final table = bundle.decisionTables.cast<Map<String, dynamic>?>().firstWhere((item) => item?["id"] == refId, orElse: () => null);
          if (table == null) {
            skipped.add(step.id ?? refId ?? step.type);
            error = "Unknown decision table: $refId";
          } else {
            result = _decisionTableEvaluator.evaluate(table, variables);
            outcome = _mergeOutcome(outcome, result["outcome"]?.toString());
          }
          break;
        case "scorecard":
          final refId = step.refId ?? step.ref;
          final scorecard = bundle.scorecards.firstWhere((item) => item.id == refId, orElse: () => _MissingScorecard.instance);
          if (scorecard is _MissingScorecard) {
            skipped.add(step.id ?? refId ?? step.type);
            error = "Unknown scorecard: $refId";
          } else {
            result = _scorecardEvaluator.evaluate(scorecard, variables);
            if (refId != null) {
              scorecardResults[refId] = result;
            }
            score = (result["score"] as num?)?.toDouble();
          }
          break;
        case "transform":
          result = _executeTransform(step, context);
          transformOutputs[step.config["outputKey"]?.toString() ?? "transformed"] = result;
          break;
        case "action":
          final pending = _queueAction(step, context);
          pendingOperations.add(pending);
          result = <String, dynamic>{
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
          actionResults.add(result);
          if (pending["blocking"] == true) {
            status = "pending_sync";
            currentStepIndex = index + 1;
          }
          break;
        case "review_gate":
          reviewTask = _createReviewTask(step, variables, ruleResults, scorecardResults, outcome, executionId);
          result = <String, dynamic>{"paused": true, "reviewTaskId": reviewTask["id"]};
          status = "paused";
          pausedAtStep = index;
          break;
        case "outcome":
          outcome = _mergeOutcome(outcome, step.refId ?? step.ref ?? step.config["outcome"]?.toString() ?? step.label ?? outcome);
          result = <String, dynamic>{"outcome": outcome};
          break;
        default:
          skipped.add(step.id ?? step.refId ?? step.ref ?? step.type);
          result = <String, dynamic>{"skipped": true};
      }

      trace.add(<String, dynamic>{
        "step": _traceStep(step),
        "result": result,
        "error": error,
        "duration_ms": DateTime.now().millisecondsSinceEpoch - stepStarted,
      });

      if (status == "paused" || status == "pending_sync") {
        break;
      }
    }

    if (status == "running") {
      status = "completed";
      currentStepIndex = policy.steps.length;
    }
    if (outcome == "pending") {
      outcome = policy.defaultOutcome ?? "review";
    }

    return Decision(
      outcome: outcome,
      score: score,
      variables: variables,
      ruleResults: ruleResults,
      latencyMs: DateTime.now().millisecondsSinceEpoch - startedAt,
      serverOnlyStepsSkipped: skipped.toSet().toList(),
      executionId: executionId,
      status: status,
      trace: trace,
      scorecardResults: scorecardResults,
      actionResults: actionResults,
      pendingOperations: pendingOperations,
      reviewTask: reviewTask,
      explainability: <String, dynamic>{
        "rules": ruleResults,
        "scorecards": scorecardResults,
        "trace": trace,
      },
      auditSummary: <String, dynamic>{
        "traceSteps": trace.length,
        "pendingOperationCount": pendingOperations.where((item) => item["status"] != "delivered").length,
        "actionCount": actionResults.length,
        "reviewTaskId": reviewTask?["id"],
      },
      policyId: policy.id,
      payload: effectivePayload,
      transformOutputs: transformOutputs,
      currentStepIndex: currentStepIndex,
      pausedAtStep: pausedAtStep,
      reviewResponse: reviewResponse,
    );
  }

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

  Map<String, dynamic> _createReviewTask(
    PolicyStep step,
    Map<String, dynamic> variables,
    List<Map<String, dynamic>> ruleResults,
    Map<String, Map<String, dynamic>> scorecardResults,
    String outcome,
    String executionId,
  ) {
    return <String, dynamic>{
      "id": "local_review_${_operationId().substring(3)}",
      "execution_id": executionId,
      "step_id": step.id ?? step.refId ?? step.ref ?? step.type,
      "queue": step.config["assignTo"]?.toString() ?? "mobile_review",
      "status": "pending",
      "required_fields": ((step.config["requiredFields"] as List<dynamic>?) ?? const <dynamic>[]).map((item) => item.toString()).toList(),
      "context_snapshot": <String, dynamic>{
        "variables": variables,
        "rule_results": ruleResults,
        "scorecard_results": scorecardResults,
        "outcome_before_review": outcome,
      },
    };
  }

  Map<String, dynamic> _contextView(
    Map<String, dynamic> payload,
    Map<String, dynamic> variables,
    Map<String, Map<String, dynamic>> scorecardResults,
    Map<String, Map<String, dynamic>> transformOutputs,
    String outcome,
    String executionId,
    Map<String, dynamic> reviewResponse,
  ) {
    final computed = transformOutputs["computed"] ??
        transformOutputs.values.fold<Map<String, dynamic>>(<String, dynamic>{}, (acc, item) {
          acc.addAll(item);
          return acc;
        });
    return <String, dynamic>{
      "payload": payload,
      "variables": variables,
      "scorecard": scorecardResults,
      "computed": computed,
      "transforms": transformOutputs,
      "outcome": outcome,
      "execution_id": executionId,
      "review": reviewResponse,
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
      current = current is Map<String, dynamic> ? current[key] : null;
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

class _MissingRule implements CompiledRule {
  _MissingRule._();
  static final _MissingRule instance = _MissingRule._();

  @override
  String get id => "";
  @override
  String get name => "";
  @override
  List<Map<String, dynamic>> get nodes => const <Map<String, dynamic>>[];
  @override
  String? get expression => null;
  @override
  String get ruleFormat => "v1";
  @override
  Map<String, dynamic>? get tree => null;
}

class _MissingScorecard implements Scorecard {
  _MissingScorecard._();
  static final _MissingScorecard instance = _MissingScorecard._();

  @override
  int get baseScore => 300;
  @override
  List<ScoreFactor> get bins => const <ScoreFactor>[];
  @override
  String get id => "";
  @override
  int get maxScore => 900;
  @override
  String get name => "";
}
