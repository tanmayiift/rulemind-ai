import 'dart:convert';
import 'dart:io';

import 'package:flutter_test/flutter_test.dart';
import 'package:rulemind/src/rule_evaluator.dart';

/// Dart arm of the cross-engine operator conformance suite. Loads the same
/// fixture file as the TS, Python, and Kotlin engines
/// (packages/shared/operators.spec.json) and asserts the offline engine's
/// operator semantics match exactly.
void main() {
  final specFile = _locateSpec();
  final spec = jsonDecode(specFile.readAsStringSync()) as Map<String, dynamic>;
  final cases = (spec['cases'] as List).cast<Map<String, dynamic>>();
  final operators = (spec['operators'] as List).cast<Map<String, dynamic>>();
  final evaluator = RuleEvaluator();

  test('every operator has at least one fixture', () {
    final covered = cases.map((c) => c['operator']).toSet();
    for (final entry in operators) {
      expect(covered.contains(entry['op']), isTrue,
          reason: 'no fixture for operator ${entry['op']}');
    }
  });

  for (final testCase in cases) {
    test('${testCase['operator']}: ${testCase['name']}', () {
      final result = evaluator.compareCondition(
        testCase['actual'],
        testCase['operator'] as String,
        testCase['value'],
        expected2: testCase['value2'],
        fieldType: testCase['fieldType'] as String?,
      );
      expect(result, testCase['expected'],
          reason: '${testCase['name']}: ${testCase['operator']}');
    });
  }
}

File _locateSpec() {
  // flutter test runs with cwd at the package root (packages/sdk-flutter).
  for (final candidate in <String>[
    '../shared/operators.spec.json',
    '../../packages/shared/operators.spec.json',
  ]) {
    final file = File(candidate);
    if (file.existsSync()) return file;
  }
  throw StateError('Could not locate packages/shared/operators.spec.json');
}
