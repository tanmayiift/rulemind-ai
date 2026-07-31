import 'package:flutter_test/flutter_test.dart';
import 'package:rulemind/src/decision_outbox.dart';
import 'package:rulemind/src/decision_syncer.dart';
import 'package:rulemind/src/sqflite_decision_outbox.dart';
import 'package:sqflite_common_ffi/sqflite_ffi.dart';

/// Exercises the DURABLE sqflite-backed outbox against real SQLite, headless via
/// sqflite_common_ffi (no device/emulator). Proves the SQL persistence layer honours
/// the same contract the DecisionSyncer relies on.
void main() {
  setUpAll(() {
    sqfliteFfiInit();
  });

  PendingDecision decision(String id) => PendingDecision(
        id: id, policyId: 'p', outcome: 'approve', payloadJson: '{"k":1}', createdAt: '2026-07-31T10:00:00Z',
      );

  Future<SqfliteDecisionOutbox> openOutbox() =>
      SqfliteDecisionOutbox.open(factory: databaseFactoryFfi, path: inMemoryDatabasePath);

  test('persists, drains in order, and clears acked rows', () async {
    final outbox = await openOutbox();
    for (var i = 0; i < 5; i++) {
      await outbox.enqueue(decision('d$i'));
    }
    expect(await outbox.size(), 5);
    final batch = await outbox.pending(10, 1 << 62);
    expect(batch.map((d) => d.id).toList(), ['d0', 'd1', 'd2', 'd3', 'd4']); // oldest-first
    await outbox.markSynced(['d0', 'd1']);
    expect(await outbox.size(), 3);
    await outbox.close();
  });

  test('enqueue is idempotent on id (PK conflict ignored)', () async {
    final outbox = await openOutbox();
    await outbox.enqueue(decision('same'));
    await outbox.enqueue(decision('same'));
    expect(await outbox.size(), 1);
    await outbox.close();
  });

  test('recordFailure bumps attempts and defers eligibility', () async {
    final outbox = await openOutbox();
    await outbox.enqueue(decision('x'));
    await outbox.recordFailure(['x'], 5000);
    expect(await outbox.pending(10, 4000), isEmpty); // not eligible before 5000
    final eligible = await outbox.pending(10, 6000);
    expect(eligible.single.attempts, 1);
    await outbox.close();
  });

  test('trimToCapacity drops the oldest rows', () async {
    final outbox = await openOutbox();
    for (var i = 0; i < 10; i++) {
      await outbox.enqueue(decision('d$i'));
    }
    expect(await outbox.trimToCapacity(6), 4);
    final remaining = (await outbox.pending(100, 1 << 62)).map((d) => d.id).toList();
    expect(remaining, ['d4', 'd5', 'd6', 'd7', 'd8', 'd9']);
    await outbox.close();
  });

  test('drives a full DecisionSyncer run end-to-end on SQLite', () async {
    final outbox = await openOutbox();
    for (var i = 0; i < 450; i++) {
      await outbox.enqueue(decision('d$i'));
    }
    final syncer = DecisionSyncer(
      outbox: outbox,
      uploader: (batch) async => UploadResult.success(batch.map((d) => d.id).toList()),
      config: const DecisionSyncConfig(batchSize: 200),
    );
    final stats = await syncer.sync();
    expect(stats.uploaded, 450);
    expect(await outbox.size(), 0); // fully drained + cleared from disk
    await outbox.close();
  });
}
