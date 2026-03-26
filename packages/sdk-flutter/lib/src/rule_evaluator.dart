class RuleEvaluator {
  Map<String, dynamic> evaluate(dynamic rule, Map<String, dynamic> variables) {
    final conditions = <Map<String, dynamic>>[];
    final groups = <Map<String, dynamic>>[];
    final evaluation = rule.ruleFormat == "v2" && rule.tree != null
        ? _evaluateTree(rule.tree!, variables, conditions, groups)
        : _evaluateNodes(rule.nodes, variables, conditions, groups);
    return <String, dynamic>{
      "ruleId": rule.id,
      "passed": evaluation.passed,
      "outcome": evaluation.outcome,
      "logic": evaluation.logic,
      "conditions": conditions,
      "groupResults": groups,
    };
  }

  _RuleEvaluation _evaluateNodes(
    List<Map<String, dynamic>> nodes,
    Map<String, dynamic> variables,
    List<Map<String, dynamic>> conditions,
    List<Map<String, dynamic>> groups,
  ) {
    final logic = nodes.any((node) => node["type"] == "or") ? "OR" : "AND";
    final results = nodes.where((node) => node["type"] == "condition").map((condition) {
      final variableId = condition["variable"]?.toString() ?? "";
      final actual = variables[variableId] ?? 0;
      final operator = condition["operator"]?.toString() ?? "==";
      final expected = condition["value"];
      final passed = _compare(actual, operator, expected);
      conditions.add(<String, dynamic>{
        "variable_id": variableId,
        "variable_name": variableId,
        "operator": operator,
        "threshold": expected,
        "value": actual,
        "passed": passed,
        "group": logic,
      });
      return passed;
    }).toList();
    final passed = logic == "OR" ? results.any((value) => value) : results.every((value) => value);
    groups.add(<String, dynamic>{"id": "group", "logic": logic, "passed": passed, "childCount": results.length});
    return _RuleEvaluation(passed: passed, outcome: passed ? "approve" : "review", logic: logic);
  }

  _RuleEvaluation _evaluateTree(
    Map<String, dynamic> node,
    Map<String, dynamic> variables,
    List<Map<String, dynamic>> conditions,
    List<Map<String, dynamic>> groups,
  ) {
    final passed = _evaluateNode(node, variables, conditions, groups);
    return _RuleEvaluation(
      passed: passed,
      outcome: passed ? (node["onPass"]?.toString() ?? "approve") : (node["onFail"]?.toString() ?? "review"),
      logic: node["logic"]?.toString() ?? "AND",
    );
  }

  bool _evaluateNode(
    Map<String, dynamic> node,
    Map<String, dynamic> variables,
    List<Map<String, dynamic>> conditions,
    List<Map<String, dynamic>> groups,
  ) {
    switch (node["type"]) {
      case "condition":
        final variableId = node["variable"]?.toString() ?? "";
        final actual = variables[variableId] ?? 0;
        final operator = node["operator"]?.toString() ?? "==";
        final expected = node["value"];
        final passed = _compare(actual, operator, expected);
        conditions.add(<String, dynamic>{
          "variable_id": variableId,
          "variable_name": variableId,
          "operator": operator,
          "threshold": expected,
          "value": actual,
          "passed": passed,
          "group": node["logic"]?.toString() ?? "AND",
        });
        return passed;
      case "not":
        final child = (node["child"] as Map<String, dynamic>?) ??
            ((node["children"] as List<dynamic>? ?? const <dynamic>[]).whereType<Map<String, dynamic>>().firstOrNull);
        final passed = child == null ? false : !_evaluateNode(child, variables, conditions, groups);
        groups.add(<String, dynamic>{"id": node["id"] ?? "not", "logic": "NOT", "passed": passed, "childCount": 1});
        return passed;
      default:
        final children = (node["children"] as List<dynamic>? ?? const <dynamic>[]).whereType<Map<String, dynamic>>().toList();
        final logic = node["logic"]?.toString() ?? "AND";
        final results = children.map((child) => _evaluateNode(child, variables, conditions, groups)).toList();
        final passed = logic == "OR" ? results.any((value) => value) : results.every((value) => value);
        groups.add(<String, dynamic>{
          "id": node["id"] ?? "group",
          "logic": logic,
          "passed": passed,
          "childCount": results.length,
        });
        return passed;
    }
  }

  bool _compare(dynamic actual, String operator, dynamic expected) {
    switch (operator) {
      case ">":
        return _numeric(actual) > _numeric(expected);
      case ">=":
        return _numeric(actual) >= _numeric(expected);
      case "<":
        return _numeric(actual) < _numeric(expected);
      case "<=":
        return _numeric(actual) <= _numeric(expected);
      case "!=":
        return actual != expected;
      default:
        return actual == expected || _numeric(actual) == _numeric(expected);
    }
  }

  double _numeric(dynamic value) {
    if (value is num) {
      return value.toDouble();
    }
    if (value is String) {
      return double.tryParse(value) ?? 0.0;
    }
    return 0.0;
  }
}

class _RuleEvaluation {
  const _RuleEvaluation({required this.passed, required this.outcome, required this.logic});

  final bool passed;
  final String outcome;
  final String logic;
}

extension<E> on Iterable<E> {
  E? get firstOrNull => isEmpty ? null : first;
}
