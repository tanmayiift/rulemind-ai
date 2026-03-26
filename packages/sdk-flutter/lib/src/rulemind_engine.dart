import "models.dart";
import "experiment_manager.dart";
import "policy_executor.dart";

class RuleMindEngine {
  RuleMindEngine({
    PolicyExecutor? policyExecutor,
    ExperimentManager? experimentManager,
  })  : _policyExecutor = policyExecutor ?? PolicyExecutor(),
        _experimentManager = experimentManager ?? ExperimentManager();

  final PolicyExecutor _policyExecutor;
  final ExperimentManager _experimentManager;

  Decision evaluate(Bundle bundle, String policyId, Map<String, dynamic> payload, {String? userId}) {
    final policy = bundle.policies.firstWhere((item) => item.id == policyId);
    final decision = _policyExecutor.evaluate(bundle, policy, payload);
    Experiment? experiment;
    for (final item in bundle.experiments) {
      if (item.status == "running" && item.targetPolicyId == policyId) {
        experiment = item;
        break;
      }
    }
    final variant = experiment == null ? null : _experimentManager.assign(experiment, userId);
    return Decision(
      outcome: decision.outcome,
      score: decision.score,
      variables: decision.variables,
      ruleResults: decision.ruleResults,
      experimentId: experiment?.id,
      experimentVariant: variant?.id,
      latencyMs: decision.latencyMs,
      requestId: decision.requestId,
      serverOnlyStepsSkipped: decision.serverOnlyStepsSkipped,
    );
  }

  ExperimentVariant? getExperiment(Bundle bundle, String experimentId, String? userId) {
    Experiment? experiment;
    for (final item in bundle.experiments) {
      if (item.id == experimentId) {
        experiment = item;
        break;
      }
    }
    if (experiment == null) {
      return null;
    }
    return _experimentManager.assign(experiment, userId);
  }
}
