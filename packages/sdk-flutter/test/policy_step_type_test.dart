import "package:flutter_test/flutter_test.dart";
import "package:rulemind/src/models.dart";
import "package:rulemind/src/policy_executor.dart";

/// The five step types the Python executor dispatches that the offline engine did not.
///
/// `loop`, `branch` and `workflow` are pure control flow and now run on-device. `model` and
/// `monitor` need the server (a model blob + an ML runtime; audit events, alert webhooks and
/// scheduled re-evaluation) and now FAIL LOUDLY. And an unrecognised step type is an ERROR, as in
/// `executor.py`, not a silent `skipped` — a policy authored with a step this SDK version does not
/// understand must not quietly return a decision that step never contributed to.
void main() {
  final executor = PolicyExecutor();

  Bundle bundleOf(List<Policy> policies) => Bundle(
        bundleVersion: 1,
        bundleId: "bundle-steps",
        tenantId: "tenant-1",
        compiledAt: "2026-09-03T00:00:00Z",
        expiresAt: "2026-10-03T00:00:00Z",
        variables: const <CompiledVariable>[],
        rules: const <CompiledRule>[],
        scorecards: const <Scorecard>[],
        policies: policies,
        experiments: const <Experiment>[],
        serverOnlyVariables: const <String>[],
        checksum: "checksum",
      );

  Decision run(
    List<PolicyStep> steps, {
    Map<String, dynamic> payload = const <String, dynamic>{},
    List<Policy> extra = const <Policy>[],
  }) {
    final policy = Policy(id: "policy-root", name: "Root", steps: steps, defaultOutcome: "review");
    return executor.evaluate(bundleOf(<Policy>[policy, ...extra]), policy, payload);
  }

  Map<String, dynamic> traceFor(Decision decision, String type) =>
      decision.trace.firstWhere((entry) => (entry["step"] as Map)["type"] == type);

  Map<String, dynamic>? resultFor(Decision decision, String type) =>
      traceFor(decision, type)["result"] as Map<String, dynamic>?;

  group("loop", () {
    test("iterates a literal collection and binds item and index", () {
      final decision = run(<PolicyStep>[
        PolicyStep(
          id: "loop-1",
          type: "loop",
          config: <String, dynamic>{
            "over": <dynamic>[10, 20, 30],
            "steps": <dynamic>[
              <String, dynamic>{
                "id": "approve-second",
                "type": "outcome",
                "refId": "approve",
                "config": <String, dynamic>{"condition": "index == 1"},
              },
            ],
          },
        ),
      ]);
      final result = resultFor(decision, "loop")!;
      expect(result["count"], 3);
      expect(result["iterations_run"], 3);
      expect(result["truncated"], isFalse);
      expect(result["mode"], "sequential");
      // Only the second iteration's condition matched, so the outcome moved exactly once.
      expect(decision.outcome, "approve");
      final iterations = (result["iterations"] as List).cast<Map<String, dynamic>>();
      expect(iterations.map((item) => item["item"]).toList(), <dynamic>[10, 20, 30]);
      expect(iterations[0]["outcome_after"], "pending");
      expect(iterations[1]["outcome_after"], "approve");
    });

    test("resolves a dotted path and a map collection and honours maxIterations", () {
      final fromPayload = run(
        <PolicyStep>[
          PolicyStep(id: "loop-1", type: "loop", config: <String, dynamic>{"over": "payload.items", "steps": <dynamic>[]}),
        ],
        payload: <String, dynamic>{
          "items": <dynamic>[1, 2, 3, 4, 5],
        },
      );
      expect(resultFor(fromPayload, "loop")!["count"], 5);

      final capped = run(<PolicyStep>[
        PolicyStep(
          id: "loop-1",
          type: "loop",
          config: <String, dynamic>{"over": <dynamic>[1, 2, 3, 4, 5], "maxIterations": 2, "steps": <dynamic>[]},
        ),
      ]);
      final cappedResult = resultFor(capped, "loop")!;
      expect(cappedResult["count"], 5);
      expect(cappedResult["iterations_run"], 2);
      expect(cappedResult["truncated"], isTrue);

      // A map iterates as {key, value} pairs; a non-iterable is a no-op, not an error.
      final overMap = run(
        <PolicyStep>[
          PolicyStep(id: "loop-1", type: "loop", config: <String, dynamic>{"over": "payload.lookup", "steps": <dynamic>[]}),
        ],
        payload: <String, dynamic>{
          "lookup": <String, dynamic>{"a": 1, "b": 2},
        },
      );
      final pairs = (resultFor(overMap, "loop")!["iterations"] as List).cast<Map<String, dynamic>>();
      expect(pairs[0]["item"], <String, dynamic>{"key": "a", "value": 1});

      final overNothing = run(<PolicyStep>[
        PolicyStep(id: "loop-1", type: "loop", config: <String, dynamic>{"over": "payload.missing", "steps": <dynamic>[]}),
      ]);
      expect(resultFor(overNothing, "loop")!["count"], 0);
    });

    test("nesting beyond the server's depth limit is refused", () {
      // Ten nested loops is the Python executor's `_MAX_WORKFLOW_DEPTH`; the eleventh must not run.
      Map<String, dynamic> innermost = <String, dynamic>{
        "id": "loop-11",
        "type": "loop",
        "config": <String, dynamic>{"over": <dynamic>[1], "steps": <dynamic>[]},
      };
      for (var level = 0; level < 10; level += 1) {
        innermost = <String, dynamic>{
          "id": "loop-${10 - level}",
          "type": "loop",
          "config": <String, dynamic>{"over": <dynamic>[1], "steps": <dynamic>[innermost]},
        };
      }
      final decision = run(<PolicyStep>[
        PolicyStep(
          id: "loop-0",
          type: "loop",
          config: <String, dynamic>{"over": <dynamic>[1], "steps": <dynamic>[innermost]},
        ),
      ]);
      final errors = decision.trace.map((entry) => entry["error"]).whereType<String>().toList();
      expect(errors.any((message) => message.contains("nesting depth")), isTrue, reason: "got $errors");
    });
  });

  group("branch", () {
    PolicyStep branchStep() => PolicyStep(
          id: "branch-1",
          type: "branch",
          config: <String, dynamic>{
            "branches": <dynamic>[
              <String, dynamic>{
                "condition": "payload.tier == 'gold'",
                "label": "gold",
                "steps": <dynamic>[
                  <String, dynamic>{"id": "s", "type": "outcome", "refId": "approve"},
                ],
              },
              <String, dynamic>{
                "condition": "payload.tier == 'silver'",
                "label": "silver",
                "steps": <dynamic>[
                  <String, dynamic>{"id": "s", "type": "outcome", "refId": "review"},
                ],
              },
            ],
            "default": <dynamic>[
              <String, dynamic>{"id": "s", "type": "outcome", "refId": "reject"},
            ],
          },
        );

    test("takes the first match, then the default", () {
      final gold = run(<PolicyStep>[branchStep()], payload: <String, dynamic>{"tier": "gold"});
      expect(resultFor(gold, "branch")!["branch"], 0);
      expect(gold.outcome, "approve");

      final silver = run(<PolicyStep>[branchStep()], payload: <String, dynamic>{"tier": "silver"});
      expect(resultFor(silver, "branch")!["branch"], 1);
      expect(silver.outcome, "review");

      final other = run(<PolicyStep>[branchStep()], payload: <String, dynamic>{"tier": "bronze"});
      expect(resultFor(other, "branch")!["branch"], "default");
      expect(other.outcome, "reject");
    });

    test("no match and no default is an unmatched no-op", () {
      final decision = run(
        <PolicyStep>[
          PolicyStep(
            id: "branch-1",
            type: "branch",
            config: <String, dynamic>{
              "branches": <dynamic>[
                <String, dynamic>{
                  "condition": "payload.tier == 'gold'",
                  "steps": <dynamic>[
                    <String, dynamic>{"id": "s", "type": "outcome", "refId": "approve"},
                  ],
                },
              ],
            },
          ),
        ],
        payload: <String, dynamic>{"tier": "bronze"},
      );
      final result = resultFor(decision, "branch")!;
      expect(result["matched"], isFalse);
      expect(result["branch"], isNull);
      expect(decision.outcome, "review");
    });
  });

  group("workflow", () {
    test("runs the sub-policy steps against the live context", () {
      final sub = Policy(
        id: "policy-sub",
        name: "Sub",
        steps: <PolicyStep>[PolicyStep(id: "sub-outcome", type: "outcome", refId: "approve")],
      );
      final decision = run(
        <PolicyStep>[PolicyStep(id: "wf", type: "workflow", refId: "policy-sub")],
        extra: <Policy>[sub],
      );
      expect(decision.outcome, "approve");
      expect(resultFor(decision, "workflow"), <String, dynamic>{"workflow": "policy-sub", "outcome": "approve"});
    });

    test("refuses cycles and unknown references", () {
      // The sub-policy calls the ROOT policy back; the root id seeds the visited set, so this is
      // caught on the first re-entry rather than recursing until the stack dies.
      final sub = Policy(
        id: "policy-sub",
        name: "Sub",
        steps: <PolicyStep>[PolicyStep(id: "back", type: "workflow", refId: "policy-root")],
      );
      final cycle = run(
        <PolicyStep>[PolicyStep(id: "wf", type: "workflow", refId: "policy-sub")],
        extra: <Policy>[sub],
      );
      expect(
        cycle.trace.any((entry) => (entry["error"] as String?)?.contains("cycle detected") ?? false),
        isTrue,
        reason: "got ${cycle.trace.map((entry) => entry["error"]).toList()}",
      );

      final unknown = run(<PolicyStep>[PolicyStep(id: "wf", type: "workflow", refId: "policy-nope")]);
      expect(traceFor(unknown, "workflow")["error"], "Unknown sub-workflow: policy-nope");

      final missingRef = run(<PolicyStep>[PolicyStep(id: "wf", type: "workflow")]);
      expect(traceFor(missingRef, "workflow")["error"], "Sub-workflow step missing ref_id");
    });
  });

  group("model / monitor / unknown", () {
    test("model and monitor fail explicitly instead of being skipped", () {
      final model = run(<PolicyStep>[PolicyStep(id: "m", type: "model", refId: "model_pd")]);
      final modelError = traceFor(model, "model")["error"] as String?;
      expect(modelError, isNotNull);
      expect(modelError, contains("server runtime"));
      expect(resultFor(model, "model"), isNull, reason: "a model step must not produce a result offline");

      final monitor = run(<PolicyStep>[PolicyStep(id: "mon", type: "monitor")]);
      final monitorError = traceFor(monitor, "monitor")["error"] as String?;
      expect(monitorError, isNotNull);
      expect(monitorError, contains("server-side telemetry"));
    });

    test("an unknown step type is an error, not a silent skip", () {
      final decision = run(<PolicyStep>[PolicyStep(id: "x", type: "teleport")]);
      final entry = traceFor(decision, "teleport");
      // Word-for-word with `executor.py`'s `"Unknown step type: {0}".format(step_type)`.
      expect(entry["error"], "Unknown step type: teleport");
      expect(entry["result"], isNull);
      // ...and it is NOT filed under serverOnlyStepsSkipped, which would read as "handled".
      expect(decision.serverOnlyStepsSkipped, isEmpty);
    });

    test("a step that aborts stops the run and a throwing gate escalates to review", () {
      final aborted = run(<PolicyStep>[
        PolicyStep(
          id: "m",
          type: "model",
          refId: "model_pd",
          config: <String, dynamic>{"onFailure": "abort"},
        ),
        PolicyStep(id: "after", type: "outcome", refId: "approve"),
      ]);
      expect(aborted.status, "failed");
      expect(aborted.trace.length, 1);

      // A scorecard step whose reference is missing is a decision GATE that did not run; the
      // Python core escalates such a failure to human review rather than passing it over.
      final gate = run(<PolicyStep>[PolicyStep(id: "sc", type: "scorecard", refId: "nope")]);
      expect(traceFor(gate, "scorecard")["error"], "Unknown scorecard: nope");
      expect(gate.outcome, "review");
    });
  });
}
