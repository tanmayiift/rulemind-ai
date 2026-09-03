import 'dart:io';

import 'package:flutter_test/flutter_test.dart';
import 'package:hive/hive.dart';
import 'package:rulemind/rulemind.dart';
import 'package:http/http.dart' as http;
import 'package:rulemind/src/decision_outbox.dart';

/// resetForTest must actually reset (#138).
///
/// `RuleMind` is a static namespace class, so every field on it is process-global. That is a
/// reasonable design for a device SDK with one backend, but it makes `resetForTest` the ONLY
/// isolation boundary tests have — and it was leaking: `_decisionOutbox`, `_decisionSyncer`
/// and `_hiveReady` were never cleared, and the old `http.Client` was replaced without being
/// closed.
///
/// The existing `pending_count_test.dart` passed only because it is the sole unit test that
/// touches `RuleMind` and nothing had initialized before it — an assertion that was vacuous
/// rather than correct. This file makes the leak observable: it enqueues real state, resets,
/// and asserts the state is gone.
void main() {
  RuleMindConfig config() => RuleMindConfig(
        baseUrl: 'http://127.0.0.1:1',
        apiKey: 'test-key',
        // Nothing here reaches the network: initialize() only constructs collaborators, and
        // the injected outbox means no SQLite binding is needed either.
        enableServerFallback: false,
      );

  setUp(() async {
    await RuleMind.resetForTest();
    // Same setup the SDK's own integration test uses: without a platform path provider
    // Hive has nowhere to open its boxes, and DecisionCache/EventLogger both open one.
    Hive.init(Directory.systemTemp.createTempSync('rulemind_reset_test_').path);
  });

  test('outbox state does not survive resetForTest', () async {
    final outbox = InMemoryDecisionOutbox();
    await RuleMind.initialize(config(), outbox: outbox);
    await outbox.enqueue(const PendingDecision(
      id: 'd1',
      policyId: 'p1',
      outcome: 'approve',
      payloadJson: '{}',
      createdAt: '2026-09-03T00:00:00Z',
    ));
    expect(await RuleMind.pendingDecisionCount(), 1,
        reason: 'precondition: the injected outbox is the one the SDK reads');

    await RuleMind.resetForTest();

    // Fails before the fix: pendingDecisionCount() reads _decisionOutbox, which
    // resetForTest never cleared, so the previous test's queue was still visible.
    expect(await RuleMind.pendingDecisionCount(), 0,
        reason: 'a reset must not leave the previous outbox attached');
  });

  test('an injected http client is used instead of the default', () async {
    // The seam itself. _httpClient was private with no way in, which is why the
    // offline->online round trip had to be proven from Python rather than Dart.
    final client = _RecordingClient();
    await RuleMind.initialize(config(), httpClient: client, outbox: InMemoryDecisionOutbox());
    await RuleMind.syncPendingDecisions();
    expect(client.closed, isFalse);
    await RuleMind.resetForTest();
    expect(client.closed, isTrue,
        reason: 'resetForTest must close the client it was given, not leak the socket');
  });
}

class _RecordingClient extends http.BaseClient {
  bool closed = false;
  @override
  Future<http.StreamedResponse> send(http.BaseRequest request) async =>
      http.StreamedResponse(const Stream.empty(), 200);
  @override
  void close() {
    closed = true;
    super.close();
  }
}
