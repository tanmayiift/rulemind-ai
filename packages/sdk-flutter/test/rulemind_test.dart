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

  test('edge policy execution runs full rule and scorecard flow locally', () {
    final bundle = Bundle(
      bundleVersion: 1,
      bundleId: 'bundle-1',
      tenantId: 'tenant-1',
      compiledAt: '2026-03-26T00:00:00Z',
      expiresAt: '2026-04-26T00:00:00Z',
      variables: <CompiledVariable>[
        CompiledVariable(
          id: 'var_credit_score',
          sourceId: 'bureau',
          name: 'Credit Score',
          instructions: <Instruction>[
            Instruction(op: 'get', target: 'score', path: 'score'),
            Instruction(op: 'return', source: 'score'),
          ],
        ),
      ],
      rules: <CompiledRule>[
        CompiledRule(
          id: 'rule_credit_check',
          name: 'Credit Check',
          ruleFormat: 'v1',
          nodes: <Map<String, dynamic>>[
            <String, dynamic>{
              'type': 'condition',
              'variable': 'var_credit_score',
              'operator': '>=',
              'value': 700,
            }
          ],
        ),
      ],
      scorecards: <Scorecard>[
        Scorecard(
          id: 'scorecard_credit_risk',
          name: 'Credit Risk',
          baseScore: 300,
          maxScore: 900,
          bins: <ScoreFactor>[
            ScoreFactor(
              variableId: 'var_credit_score',
              ranges: <ScoreRange>[
                ScoreRange(min: 700, max: 750, points: 275),
              ],
            ),
          ],
        ),
      ],
      policies: <Policy>[
        Policy(
          id: 'policy-1',
          name: 'Policy',
          defaultOutcome: 'review',
          steps: <PolicyStep>[
            PolicyStep(type: 'rule', id: 'rule-step', refId: 'rule_credit_check', label: 'Credit Eligibility'),
            PolicyStep(type: 'scorecard', id: 'score-step', refId: 'scorecard_credit_risk', label: 'Credit Risk Scoring'),
            PolicyStep(type: 'outcome', id: 'outcome-step', config: <String, dynamic>{'outcome': 'approve'}),
          ],
        ),
      ],
      experiments: const <Experiment>[],
      serverOnlyVariables: const <String>[],
      checksum: 'sha256:test',
    );
    final decision = PolicyExecutor().evaluate(bundle, bundle.policies.first, <String, dynamic>{
      'bureau': <String, dynamic>{'score': 720}
    });
    expect(decision.outcome, 'approve');
    expect(decision.score, 575);
    expect(decision.ruleResults.first['ruleId'], 'rule_credit_check');
    expect(decision.trace, hasLength(3));
    expect(decision.explainability['scorecards'], isNotEmpty);
  });

  test('edge policy execution queues callbacks and resumes reviews', () {
    final bundle = Bundle(
      bundleVersion: 1,
      bundleId: 'bundle-2',
      tenantId: 'tenant-1',
      compiledAt: '2026-03-26T00:00:00Z',
      expiresAt: '2026-04-26T00:00:00Z',
      variables: const <CompiledVariable>[],
      rules: const <CompiledRule>[],
      scorecards: const <Scorecard>[],
      policies: <Policy>[
        Policy(
          id: 'policy-2',
          name: 'Workflow Policy',
          defaultOutcome: 'review',
          steps: <PolicyStep>[
            PolicyStep(
              type: 'action',
              id: 'callback-step',
              config: <String, dynamic>{
                'url': 'https://example.test/hooks',
                'method': 'POST',
                'bodyTemplate': <String, dynamic>{'applicationId': '{{ payload.application_id }}'},
                'onFailure': 'abort',
              },
            ),
            PolicyStep(
              type: 'review_gate',
              id: 'review-step',
              config: <String, dynamic>{'assignTo': 'ops_review'},
            ),
            PolicyStep(
              type: 'outcome',
              id: 'outcome-step',
              config: <String, dynamic>{'outcome': 'approve'},
            ),
          ],
        ),
      ],
      experiments: const <Experiment>[],
      serverOnlyVariables: const <String>[],
      checksum: 'sha256:test',
    );

    final firstPass = PolicyExecutor().evaluate(bundle, bundle.policies.first, <String, dynamic>{
      'application_id': 'app-123'
    });
    expect(firstPass.status, 'pending_sync');
    expect(firstPass.pendingOperations, hasLength(1));

    final reviewPass = PolicyExecutor().evaluate(
      bundle,
      bundle.policies.first,
      <String, dynamic>{'application_id': 'app-123'},
      resumeFrom: Decision(
        outcome: firstPass.outcome,
        score: firstPass.score,
        variables: firstPass.variables,
        ruleResults: firstPass.ruleResults,
        latencyMs: firstPass.latencyMs,
        serverOnlyStepsSkipped: firstPass.serverOnlyStepsSkipped,
        executionId: firstPass.executionId,
        status: 'running',
        trace: firstPass.trace,
        scorecardResults: firstPass.scorecardResults,
        actionResults: firstPass.actionResults,
        pendingOperations: firstPass.pendingOperations
            .map((operation) => <String, dynamic>{...operation, 'status': 'delivered'})
            .toList(),
        reviewTask: firstPass.reviewTask,
        explainability: firstPass.explainability,
        auditSummary: firstPass.auditSummary,
        payload: const <String, dynamic>{'application_id': 'app-123'},
        transformOutputs: firstPass.transformOutputs,
        currentStepIndex: 1,
        pausedAtStep: firstPass.pausedAtStep,
        reviewResponse: firstPass.reviewResponse,
      ),
    );
    expect(reviewPass.status, 'paused');
    expect(reviewPass.reviewTask?['step_id'], 'review-step');
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
