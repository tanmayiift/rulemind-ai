import 'dart:convert';
import 'dart:io';

import 'package:flutter_test/flutter_test.dart';
import 'package:rulemind/src/decision_table_evaluator.dart';

/// Dart arm of the cross-engine DECISION-TABLE conformance suite. Loads the same
/// fixture as the Python and Kotlin engines (packages/shared/decision-tables.spec.json)
/// and asserts the offline evaluator resolves the SAME outcome as the Python core
/// (the source of truth) for every case.
void main() {
  final spec = jsonDecode(_locateSpec().readAsStringSync()) as Map<String, dynamic>;
  final tables = spec['tables'] as Map<String, dynamic>;
  final cases = (spec['cases'] as List).cast<Map<String, dynamic>>();
  final evaluator = DecisionTableEvaluator();

  for (final testCase in cases) {
    test('${testCase['table']}: ${testCase['name']}', () {
      final table = tables[testCase['table']] as Map<String, dynamic>;
      final variables = (testCase['variables'] as Map).cast<String, dynamic>();
      final result = evaluator.evaluate(table, variables);
      expect(result['outcome'], testCase['expectedOutcome'], reason: testCase['name'] as String);
    });
  }
}

File _locateSpec() {
  for (final candidate in <String>[
    '../shared/decision-tables.spec.json',
    '../../packages/shared/decision-tables.spec.json',
  ]) {
    final file = File(candidate);
    if (file.existsSync()) return file;
  }
  throw StateError('Could not locate packages/shared/decision-tables.spec.json');
}
