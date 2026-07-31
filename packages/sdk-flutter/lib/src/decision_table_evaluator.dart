import "rule_evaluator.dart";

/// On-device decision-table evaluation. A direct port of the Python core
/// (app/decision_tables.py evaluate_decision_table) so the offline SDK matches the
/// server exactly — validated by packages/shared/decision-tables.spec.json (see
/// test/decision_table_conformance_test.dart). Cell matching reuses
/// [RuleEvaluator.compareCondition] (the same 12-operator contract as rules).
///
/// Tables are raw maps (rows/cells are keyed by arbitrary input ids), mirroring the
/// dynamic shape the compiler emits into the bundle.
class DecisionTableEvaluator {
  DecisionTableEvaluator({RuleEvaluator? ruleEvaluator})
      : _ruleEvaluator = ruleEvaluator ?? RuleEvaluator();

  final RuleEvaluator _ruleEvaluator;
  static const _wildcardOperators = {"", "any", "*", "-"};
  static const _hitPolicies = {"first", "priority", "unique", "collect"};

  Map<String, dynamic> evaluate(Map<String, dynamic> table, Map<String, dynamic> variables) {
    final inputs = (table["inputs"] as List<dynamic>? ?? const <dynamic>[]).whereType<Map<String, dynamic>>().toList();
    final rows = (table["rows"] as List<dynamic>? ?? const <dynamic>[]).whereType<Map<String, dynamic>>().toList();
    var hitPolicy = (table["hit_policy"] as String? ?? "first").toLowerCase();
    if (!_hitPolicies.contains(hitPolicy)) hitPolicy = "first";
    final outcomeOutputId = _outcomeOutputId(table);

    final matched = rows.where((row) => _rowMatches(row, inputs, variables)).toList();

    Map<String, dynamic> outputs = <String, dynamic>{};
    dynamic winningRowId;
    if (matched.isNotEmpty) {
      if (hitPolicy == "collect") {
        final collected = <String, dynamic>{};
        for (final out in (table["outputs"] as List<dynamic>? ?? const <dynamic>[])) {
          final oid = (out as Map)["id"]?.toString();
          if (oid == null) continue;
          collected[oid] = matched.map((row) => (row["outputs"] as Map?)?[oid]).toList();
        }
        outputs = collected;
        winningRowId = matched.map((row) => row["id"]).toList();
      } else if (hitPolicy == "priority") {
        final winner = matched.reduce((a, b) => _priority(b) > _priority(a) ? b : a);
        outputs = Map<String, dynamic>.from(winner["outputs"] as Map? ?? const <String, dynamic>{});
        winningRowId = winner["id"];
      } else {
        // first / unique both take the first match at eval time
        final winner = matched.first;
        outputs = Map<String, dynamic>.from(winner["outputs"] as Map? ?? const <String, dynamic>{});
        winningRowId = winner["id"];
      }
    } else {
      final defaultRow = table["default_row"] as Map<String, dynamic>?;
      outputs = Map<String, dynamic>.from(defaultRow?["outputs"] as Map? ?? const <String, dynamic>{});
      winningRowId = outputs.isNotEmpty ? "default" : null;
    }

    dynamic outcome;
    if (outcomeOutputId != null) {
      final raw = outputs[outcomeOutputId];
      outcome = raw is List && raw.isNotEmpty ? raw.first : raw;
      if (outcome is String) outcome = outcome.toLowerCase();
    }

    return {
      "outputs": outputs,
      "outcome": outcome,
      "matchedRowIds": matched.map((row) => row["id"]).toList(),
      "winningRowId": winningRowId,
      "hitPolicy": hitPolicy,
      "ambiguous": hitPolicy == "unique" && matched.length > 1,
    };
  }

  bool _rowMatches(Map<String, dynamic> row, List<Map<String, dynamic>> inputs, Map<String, dynamic> variables) {
    final cells = row["cells"] as Map<String, dynamic>? ?? const <String, dynamic>{};
    for (final input in inputs) {
      final inputId = input["id"]?.toString();
      if (inputId == null) continue;
      final cell = cells[inputId] as Map<String, dynamic>?;
      if (_isWildcard(cell)) continue;
      final variableId = (input["variable_id"] ?? input["variable"] ?? inputId).toString();
      final actual = variables[variableId];
      final operator = cell?["operator"]?.toString() ?? "==";
      final expected = cell?["value"];
      final value2 = cell?["value2"];
      final expected2 = (value2 == null || value2 == "") ? null : value2;
      final fieldType = (input["field_type"] ?? input["fieldType"])?.toString();
      if (!_ruleEvaluator.compareCondition(actual, operator, expected, expected2: expected2, fieldType: fieldType)) {
        return false;
      }
    }
    return true;
  }

  bool _isWildcard(Map<String, dynamic>? cell) {
    if (cell == null) return true;
    final operator = cell["operator"]?.toString();
    if (operator == null || _wildcardOperators.contains(operator)) return true;
    if (operator != "exists" && operator != "!exists") {
      final value = cell["value"];
      if (value == null || value == "") return true;
    }
    return false;
  }

  double _priority(Map<String, dynamic> row) {
    final p = row["priority"];
    if (p is num) return p.toDouble();
    if (p is String) return double.tryParse(p) ?? 0.0;
    return 0.0;
  }

  String? _outcomeOutputId(Map<String, dynamic> table) {
    for (final out in (table["outputs"] as List<dynamic>? ?? const <dynamic>[])) {
      final map = out as Map?;
      if ((map?["type"] as String?)?.toLowerCase() == "outcome") return map?["id"]?.toString();
    }
    return null;
  }
}
