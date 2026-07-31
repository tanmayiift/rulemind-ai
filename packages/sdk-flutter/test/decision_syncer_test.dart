import 'dart:math';

import 'package:flutter_test/flutter_test.dart';
import 'package:rulemind/src/decision_outbox.dart';
import 'package:rulemind/src/decision_syncer.dart';

PendingDecision _decision(String id) => PendingDecision(
      id: id, policyId: 'p', outcome: 'approve', payloadJson: '{}', createdAt: '2026-07-31T10:00:00Z',
    );

void main() {
  test('uploads all pending then empties the outbox', () async {
    final outbox = InMemoryDecisionOutbox();
    for (var i = 0; i < 5; i++) {
      await outbox.enqueue(_decision('d$i'));
    }
    final syncer = DecisionSyncer(
        outbox: outbox, uploader: (batch) async => UploadResult.success(batch.map((d) => d.id).toList()));
    final stats = await syncer.sync();
    expect(stats.uploaded, 5);
    expect(await outbox.size(), 0);
  });

  test('enqueue is idempotent on id', () async {
    final outbox = InMemoryDecisionOutbox();
    await outbox.enqueue(_decision('dup'));
    await outbox.enqueue(_decision('dup'));
    expect(await outbox.size(), 1);
  });

  test('failure keeps rows and defers with backoff', () async {
    final outbox = InMemoryDecisionOutbox();
    for (var i = 0; i < 3; i++) {
      await outbox.enqueue(_decision('d$i'));
    }
    var now = 1000000;
    final syncer = DecisionSyncer(
      outbox: outbox,
      uploader: (_) async => UploadResult.failure(),
      config: const DecisionSyncConfig(baseBackoffMs: 60000),
      clock: () => now,
      random: Random(1),
    );
    final stats = await syncer.sync();
    expect(stats.uploaded, 0);
    expect(await outbox.size(), 3); // nothing lost
    expect(await outbox.pending(10, now), isEmpty); // deferred
    expect(await outbox.pending(10, now + 60 * 60 * 1000), isNotEmpty); // eligible later
  });

  test('only acked ids are cleared', () async {
    final outbox = InMemoryDecisionOutbox();
    for (var i = 0; i < 4; i++) {
      await outbox.enqueue(_decision('d$i'));
    }
    final syncer = DecisionSyncer(
        outbox: outbox,
        uploader: (_) async => UploadResult.success(['d0', 'd1']),
        config: const DecisionSyncConfig(maxRunsPerSync: 1));
    await syncer.sync();
    final remaining = (await outbox.pending(10, 1 << 62)).map((d) => d.id).toSet();
    expect(remaining, {'d2', 'd3'});
  });

  test('backoff grows exponentially and is capped', () {
    final outbox = InMemoryDecisionOutbox();
    final syncer = DecisionSyncer(
        outbox: outbox,
        uploader: (_) async => UploadResult.failure(),
        config: const DecisionSyncConfig(baseBackoffMs: 1000, maxBackoffMs: 100000),
        random: Random(1));
    expect(syncer.backoffMs(3) > syncer.backoffMs(1), isTrue);
    expect(syncer.backoffMs(50) <= 100000, isTrue);
  });

  test('capacity trim drops oldest first', () async {
    final outbox = InMemoryDecisionOutbox();
    for (var i = 0; i < 10; i++) {
      await outbox.enqueue(_decision('d$i'));
    }
    expect(await outbox.trimToCapacity(6), 4);
    final remaining = (await outbox.pending(100, 1 << 62)).map((d) => d.id).toList();
    expect(remaining, ['d4', 'd5', 'd6', 'd7', 'd8', 'd9']);
  });

  test('large backlog drains in batches', () async {
    final outbox = InMemoryDecisionOutbox();
    for (var i = 0; i < 1000; i++) {
      await outbox.enqueue(_decision('d$i'));
    }
    var batches = 0;
    final syncer = DecisionSyncer(
        outbox: outbox,
        uploader: (batch) async {
          batches++;
          return UploadResult.success(batch.map((d) => d.id).toList());
        },
        config: const DecisionSyncConfig(batchSize: 200));
    final stats = await syncer.sync();
    expect(stats.uploaded, 1000);
    expect(batches, 5);
    expect(await outbox.size(), 0);
  });
}
