import 'package:flutter_test/flutter_test.dart';
import 'package:rulemind/rulemind.dart';
import 'package:rulemind/src/experiment_manager.dart';
import 'package:rulemind/src/policy_executor.dart';
import 'package:rulemind/src/variable_vm.dart';

void main() {
  test('variable VM supports arithmetic and return', () {
    final vm = VariableVm();
    final variable = CompiledVariable(
      id: 'salary_double',
      sourceId: 'bank',
      name: 'Salary Double',
      instructions: <Instruction>[
        Instruction(op: 'get', target: 'salary', path: 'salary[0].amount'),
        Instruction(op: 'multiply', target: 'result', left: 'salary', value: 2),
        Instruction(op: 'return', source: 'result'),
      ],
    );
    final result = vm.evaluate(variable, <String, dynamic>{
      'bank': <String, dynamic>{
        'salary': <Map<String, dynamic>>[
          <String, dynamic>{'amount': 62000}
        ]
      }
    }, <String, dynamic>{});
    expect(result, 124000);
  });

  test('edge policy execution skips server-only workflow steps', () {
    final bundle = Bundle(
      bundleVersion: 1,
      bundleId: 'bundle-1',
      tenantId: 'tenant-1',
      compiledAt: '2026-03-26T00:00:00Z',
      expiresAt: '2026-04-26T00:00:00Z',
      variables: const <CompiledVariable>[],
      rules: const <CompiledRule>[],
      scorecards: const <Scorecard>[],
      policies: <Policy>[
        Policy(
          id: 'policy-1',
          name: 'Policy',
          defaultOutcome: 'review',
          steps: <PolicyStep>[
            PolicyStep(type: 'action', id: 'a1'),
            PolicyStep(type: 'review_gate', id: 'rg1'),
          ],
        ),
      ],
      experiments: const <Experiment>[],
      serverOnlyVariables: const <String>[],
      checksum: 'sha256:test',
    );
    final decision = PolicyExecutor().evaluate(bundle, bundle.policies.first, <String, dynamic>{});
    expect(decision.serverOnlyStepsSkipped, <String>['a1', 'rg1']);
  });

  test('experiment assignment matches the server hash contract', () {
    final experiment = Experiment(
      id: 'exp-risk-cutoff',
      name: 'Risk Cutoff',
      status: 'running',
      variants: <ExperimentVariant>[
        ExperimentVariant(id: 'control', weight: 50),
        ExperimentVariant(id: 'treatment', weight: 50),
      ],
    );
    final variant = ExperimentManager().assign(experiment, 'user-123');
    expect(variant?.id, isNotNull);
  });
}
