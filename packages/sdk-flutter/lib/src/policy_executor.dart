import "models.dart";
import "rule_evaluator.dart";
import "scorecard_evaluator.dart";
import "variable_vm.dart";

class PolicyExecutor {
  PolicyExecutor({
    VariableVm? variableVm,
    RuleEvaluator? ruleEvaluator,
    ScorecardEvaluator? scorecardEvaluator,
  })  : _variableVm = variableVm ?? VariableVm(),
        _ruleEvaluator = ruleEvaluator ?? RuleEvaluator(),
        _scorecardEvaluator = scorecardEvaluator ?? ScorecardEvaluator();

  final VariableVm _variableVm;
  final RuleEvaluator _ruleEvaluator;
  final ScorecardEvaluator _scorecardEvaluator;

  Decision evaluate(Bundle bundle, Policy policy, Map<String, dynamic> payload) {
    final variables = <String, dynamic>{};
    for (final variable in bundle.variables.where((item) => item.compilable && !bundle.serverOnlyVariables.contains(item.id))) {
      variables[variable.id] = _variableVm.evaluate(variable, payload, variables);
    }

    final ruleResults = <Map<String, dynamic>>[];
    final skipped = <String>[];
    double? score;
    var outcome = policy.defaultOutcome ?? "review";

    for (final step in policy.steps) {
      final refId = step.refId ?? step.ref;
      switch (step.type) {
        case "rule":
          final rule = bundle.rules.firstWhere((item) => item.id == refId);
          final result = _ruleEvaluator.evaluate(rule, variables);
          ruleResults.add(result);
          if (result["passed"] == true) {
            outcome = result["outcome"]?.toString() ?? outcome;
          }
          break;
        case "scorecard":
          final scorecard = bundle.scorecards.firstWhere((item) => item.id == refId);
          score = (_scorecardEvaluator.evaluate(scorecard, variables)["score"] as num?)?.toDouble();
          break;
        case "outcome":
          outcome = refId ?? step.label ?? outcome;
          break;
        case "action":
        case "transform":
        case "review_gate":
          skipped.add(step.id ?? refId ?? step.type);
          break;
        default:
          skipped.add(step.id ?? refId ?? step.type);
      }
    }

    return Decision(
      outcome: outcome,
      score: score,
      variables: variables,
      ruleResults: ruleResults,
      serverOnlyStepsSkipped: skipped.toSet().toList(),
    );
  }
}
