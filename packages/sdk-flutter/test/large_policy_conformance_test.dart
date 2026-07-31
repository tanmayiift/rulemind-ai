import 'dart:convert';
import 'dart:io';

import 'package:flutter_test/flutter_test.dart';
import 'package:rulemind/src/models.dart';
import 'package:rulemind/src/rule_evaluator.dart';

/// Dart arm of the cross-engine LARGE-policy conformance suite. Loads the shared
/// fixture (packages/shared/large-policy.spec.json) — one v2 rule of 600 conditions
/// across 750 variables — and asserts the offline engine reproduces BOTH the Python
/// core's outcome AND its number of passed conditions for every case, including
/// payloads that omit variables (missing-variable parity).
void main() {
  final spec = jsonDecode(_locateSpec().readAsStringSync()) as Map<String, dynamic>;
  final rule = CompiledRule.fromJson(spec['rule'] as Map<String, dynamic>);
  final cases = (spec['cases'] as List).cast<Map<String, dynamic>>();
  final evaluator = RuleEvaluator();

  test('every case matches the Python outcome and passed-condition count', () {
    for (var i = 0; i < cases.length; i++) {
      final variables = (cases[i]['variables'] as Map).cast<String, dynamic>();
      final result = evaluator.evaluate(rule, variables);
      expect(result['outcome'], cases[i]['expectedOutcome'], reason: 'case $i outcome');
      final passed = (result['conditions'] as List)
          .where((c) => (c as Map)['passed'] == true)
          .length;
      expect(passed, cases[i]['trueConditions'], reason: 'case $i passed-count');
    }
  });
}

File _locateSpec() {
  for (final candidate in <String>[
    '../shared/large-policy.spec.json',
    '../../packages/shared/large-policy.spec.json',
  ]) {
    final file = File(candidate);
    if (file.existsSync()) return file;
  }
  throw StateError('Could not locate packages/shared/large-policy.spec.json');
}
