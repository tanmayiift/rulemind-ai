// A polling action queued on device must hand the host everything it needs to poll.
//
// This engine does no outbound I/O, so an action step becomes a pending operation that the host
// app performs. `mode: "poll"` is not a semantics this engine implements -- it is a semantics it
// must not LOSE. The server executor calls the endpoint until the author's condition holds and
// fails CLOSED (review) if it never does; a host handed only url/method/body fires once and
// reports whatever came back, which for a job-status endpoint is {"status": "pending"} treated as
// a final answer. Same policy, same input, opposite decision.
import 'package:flutter_test/flutter_test.dart';
import 'package:rulemind/src/models.dart';
import 'package:rulemind/src/policy_executor.dart';

void main() {
  Bundle bundleWith(Map<String, dynamic> config) => Bundle(
        bundleVersion: 1,
        bundleId: 'bundle-poll',
        tenantId: 'tenant-1',
        compiledAt: '2026-03-26T00:00:00Z',
        expiresAt: '2026-04-26T00:00:00Z',
        variables: const [],
        rules: const [],
        scorecards: const [],
        policies: [
          Policy(
            id: 'policy-poll',
            name: 'Polling Policy',
            steps: [
              PolicyStep(type: 'action', id: 'kyc-status', label: 'KYC status', config: config),
              PolicyStep(type: 'outcome', id: 'decision-step', config: const {'outcome': 'approve'}),
            ],
            defaultOutcome: 'review',
          ),
        ],
        experiments: const [],
        serverOnlyVariables: const [],
        checksum: 'sha256:test',
      );

  const pollConfig = <String, dynamic>{
    'url': 'https://api.example.test/jobs/1',
    'method': 'GET',
    'mode': 'poll',
    'pollUntil': {'path': 'body.status', 'operator': '==', 'value': 'complete'},
    'maxPolls': 5,
    'pollIntervalMs': 1000,
  };

  Map<String, dynamic> queuedOp(Map<String, dynamic> config) {
    final bundle = bundleWith(config);
    final result = PolicyExecutor().evaluate(bundle, bundle.policies.first, const {});
    expect(result.pendingOperations, hasLength(1));
    return result.pendingOperations.first;
  }

  test('a queued poll carries its condition and bounds', () {
    final op = queuedOp(pollConfig);
    expect(op['mode'], 'poll', reason: 'the host cannot tell this from a fire-and-forget action');
    expect(op['pollUntil'], isNotNull,
        reason: 'the exit condition was dropped; the host has nothing to poll until');
    expect(op['maxPolls'], 5);
    expect(op['pollIntervalMs'], 1000);
    // Fail closed, and it must survive the handoff.
    expect(op['onPollTimeout'], 'review_gate');
    // Poll config is additive, not a replacement.
    expect(op['url'], 'https://api.example.test/jobs/1');
    expect(op['method'], 'GET');
  });

  test('an ordinary action is unchanged', () {
    final op = queuedOp(const {'url': 'https://api.example.test/hooks', 'method': 'POST'});
    expect(op.containsKey('mode'), isFalse, reason: 'a non-poll action was tagged as a poll');
    expect(op.containsKey('pollUntil'), isFalse);
    expect(op.containsKey('onPollTimeout'), isFalse);
    expect(op['url'], 'https://api.example.test/hooks');
  });

  test('an explicit timeout choice is preserved rather than forced to the default', () {
    final op = queuedOp({...pollConfig, 'onPollTimeout': 'abort'});
    expect(op['onPollTimeout'], 'abort');
  });

  test('polling does not change whether the step blocks', () {
    // `blocking` is derived from onFailure == "abort" and nothing else.
    expect(queuedOp(pollConfig)['blocking'], isFalse);
  });
}
