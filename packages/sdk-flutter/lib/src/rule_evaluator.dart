/// Max rule-tree nesting depth. Mirrors app/logic.py MAX_RULE_TREE_DEPTH.
const int maxRuleTreeDepth = 200;

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
      // Missing variable stays null (not 0) so numeric comparisons return false,
      // matching the Python core (compare(None, ...) -> false). Defaulting to 0 made
      // `missing < 700` true on-device but false server-side — a real divergence.
      final actual = variables[variableId];
      final operator = condition["operator"]?.toString() ?? "==";
      final expected = condition["value"];
      final passed = _compare(actual, operator, expected,
          expected2: condition["value2"], fieldType: condition["fieldType"]?.toString());
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
    final passed = _evaluateNode(node, variables, conditions, groups, 0);
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
    int depth,
  ) {
    // Guard pathological nesting from a native stack overflow (matches the Python core).
    if (depth > maxRuleTreeDepth) {
      throw StateError("Rule tree nesting exceeds the maximum depth ($maxRuleTreeDepth)");
    }
    switch (node["type"]) {
      case "condition":
        final variableId = node["variable"]?.toString() ?? "";
        // Missing variable stays null (not 0), matching the Python core. See evaluateFlat above.
        final actual = variables[variableId];
        final operator = node["operator"]?.toString() ?? "==";
        final expected = node["value"];
        final passed = _compare(actual, operator, expected,
            expected2: node["value2"], fieldType: node["fieldType"]?.toString());
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
        final passed = child == null ? false : !_evaluateNode(child, variables, conditions, groups, depth + 1);
        groups.add(<String, dynamic>{"id": node["id"] ?? "not", "logic": "NOT", "passed": passed, "childCount": 1});
        return passed;
      default:
        final children = (node["children"] as List<dynamic>? ?? const <dynamic>[]).whereType<Map<String, dynamic>>().toList();
        final logic = node["logic"]?.toString() ?? "AND";
        final results = children.map((child) => _evaluateNode(child, variables, conditions, groups, depth + 1)).toList();
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

  /// Public entry point used by the cross-engine operator conformance suite
  /// (packages/shared/operators.spec.json). Delegates to the private [_compare].
  bool compareCondition(dynamic actual, String operator, dynamic expected,
          {dynamic expected2, String? fieldType}) =>
      _compare(actual, operator, expected, expected2: expected2, fieldType: fieldType);

  /// Evaluate a single condition. Mirrors the canonical operator contract
  /// (packages/shared/operators.spec.json) shared with the TS, Python, and
  /// Kotlin engines: == != > >= < <= between in not_in regex exists !exists.
  bool _compare(dynamic actual, String operator, dynamic expected,
      {dynamic expected2, String? fieldType}) {
    // Existence checks run before any missing-value handling.
    if (operator == "exists") {
      return actual != null && actual != "";
    }
    if (operator == "!exists") {
      return actual == null || actual == "";
    }

    // Set membership. `expected` may be a list or a comma-separated string.
    if (operator == "in" || operator == "not_in") {
      final options = _asOptionList(expected);
      final matched = options.any((option) => _looseEqual(actual, option));
      return operator == "in" ? matched : !matched;
    }

    // Regular-expression match against the string form of the value.
    if (operator == "regex") {
      if (actual == null) return false;
      try {
        return RegExp(expected.toString()).hasMatch(actual.toString());
      } catch (_) {
        return false;
      }
    }

    // Boolean-typed equality (only when the field is declared boolean).
    if ((fieldType ?? "").toLowerCase() == "boolean" &&
        (operator == "==" || operator == "!=")) {
      final matched = _toBool(actual) == _toBool(expected);
      return operator == "==" ? matched : !matched;
    }

    // Ordered comparisons and inclusive range operate on numbers.
    if (operator == ">=" ||
        operator == "<=" ||
        operator == ">" ||
        operator == "<" ||
        operator == "between") {
      final a = _numericOrNull(actual);
      final e = _numericOrNull(expected);
      if (a == null || e == null) return false;
      switch (operator) {
        case ">=":
          return a >= e;
        case "<=":
          return a <= e;
        case ">":
          return a > e;
        case "<":
          return a < e;
        case "between":
          final upper = _numericOrNull(expected2);
          if (upper == null) return false;
          return a >= e && a <= upper;
      }
    }

    if (operator == "==") return _looseEqual(actual, expected);
    if (operator == "!=") return !_looseEqual(actual, expected);
    return false;
  }

  double? _numericOrNull(dynamic value) {
    if (value is num) return value.toDouble();
    if (value is String) return double.tryParse(value);
    return null;
  }

  bool _toBool(dynamic value) {
    if (value is bool) return value;
    if (value is num) return value != 0;
    final s = value?.toString().trim().toLowerCase();
    return s == "true" || s == "1" || s == "yes";
  }

  List<dynamic> _asOptionList(dynamic expected) {
    if (expected is List) return expected;
    if (expected == null) return const <dynamic>[];
    return expected
        .toString()
        .split(",")
        .map((item) => item.trim())
        .where((item) => item.isNotEmpty)
        .toList();
  }

  bool _looseEqual(dynamic a, dynamic b) {
    if (a == b) return true;
    final an = _numericOrNull(a);
    final bn = _numericOrNull(b);
    if (an != null && bn != null) return an == bn;
    return a.toString() == b.toString();
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
