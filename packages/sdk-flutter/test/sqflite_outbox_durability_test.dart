import 'dart:io';

import 'package:flutter_test/flutter_test.dart';
import 'package:rulemind/src/decision_outbox.dart';
import 'package:rulemind/src/decision_syncer.dart';
import 'package:rulemind/src/sqflite_decision_outbox.dart';
import 'package:sqflite_common_ffi/sqflite_ffi.dart';

/// The half of the offline->online round trip that the Python-side test
/// (apps/python-executor/tests/test_sdk_offline_round_trip.py) structurally cannot see.
///
/// That test drives the SERVER contract: batch upload, dedup on (tenant_id, client_id),
/// and the audit row. It talks to a TestClient, so the device's queue is a Python list --
/// it proves nothing about whether a decision survives the app being killed.
///
/// This one drives the DEVICE contract against real SQLite via sqflite_common_ffi
/// (headless -- no emulator), on a real FILE rather than inMemoryDatabasePath, because
/// an in-memory database is destroyed by close() and therefore cannot express "restart".
/// The properties under test:
///
///   1. backoff is enforced by the SQL WHERE clause, not just by the syncer's bookkeeping;
///   2. queued decisions, their attempt counts and their deferral deadlines all survive a
///      process restart -- a restart must not reset backoff into a hot retry loop;
///   3. acked decisions stay gone after a restart (no resurrection, no double-count);
///   4. FIFO drain order survives a restart.
///
/// (4) is the one that would silently regress: SqfliteDecisionOutbox.enqueue stamps `seq`
/// from DateTime.now().microsecondsSinceEpoch, which is monotonic ACROSS processes, whereas
/// InMemoryDecisionOutbox uses a `_seq++` field that resets to 0 on every launch. Swapping
/// the durable impl for the in-memory one would pass every other test in this package and
/// fail only this assertion -- confirmed by mutating the source, not assumed.
void main() {
  setUpAll(sqfliteFfiInit);

  late Directory tmp;
  setUp(() async {
    tmp = await Directory.systemTemp.createTemp('rm_outbox_');
  });
  tearDown(() async {
    if (tmp.existsSync()) await tmp.delete(recursive: true);
  });

  /// Reopening THIS path is what stands in for an app relaunch: same file, new Database
  /// handle, no shared in-process state.
  Future<SqfliteDecisionOutbox> openAt(String name) => SqfliteDecisionOutbox.open(
        factory: databaseFactoryFfi,
        path: '${tmp.path}/$name',
      );

  PendingDecision decision(String id) => PendingDecision(
        id: id,
        policyId: 'policy-1',
        outcome: 'approve',
        payloadJson: '{"score":742}',
        createdAt: '2026-09-02T10:00:00Z',
      );

  /// Fails its first [failures] calls, then succeeds -- the network coming back.
  ({Future<UploadResult> Function(List<PendingDecision>) upload, List<List<String>> calls})
      stubUploader({required int failures}) {
    final calls = <List<String>>[];
    var seen = 0;
    return (
      upload: (batch) async {
        calls.add(batch.map((d) => d.id).toList());
        if (seen++ < failures) return UploadResult.failure();
        return UploadResult.success(batch.map((d) => d.id).toList());
      },
      calls: calls,
    );
  }

  test('a failed upload defers past backoff, then succeeds on a later pass', () async {
    final outbox = await openAt('backoff.db');
    addTearDown(outbox.close);
    for (var i = 0; i < 3; i++) {
      await outbox.enqueue(decision('d$i'));
    }

    var now = 1000000;
    final stub = stubUploader(failures: 1);
    final syncer = DecisionSyncer(
      outbox: outbox,
      uploader: stub.upload,
      // Fixed, small backoff so the test asserts on an exact deadline rather than a range.
      config: const DecisionSyncConfig(baseBackoffMs: 60000, maxBackoffMs: 60000),
      clock: () => now,
    );

    final first = await syncer.sync();
    expect(first.uploaded, 0);
    expect(first.failed, 3);
    expect(await outbox.size(), 3, reason: 'a failed upload must not drop decisions');

    // The retry is gated by SQL, not by the syncer: pending() filters on next_attempt_at.
    // maxBackoffMs caps the value, so the deadline is exactly now + 60000 with no jitter.
    expect(await outbox.pending(10, now), isEmpty, reason: 'still inside the backoff window');
    final second = await syncer.sync();
    expect(second.uploaded, 0);
    expect(stub.calls.length, 1, reason: 'a deferred batch must not reach the uploader again');

    now += 60000; // backoff elapsed
    final third = await syncer.sync();
    expect(third.uploaded, 3);
    expect(await outbox.size(), 0);
    expect(stub.calls.length, 2);
    expect(stub.calls.last, ['d0', 'd1', 'd2']);
  });

  test('queued decisions survive a restart with their payload intact', () async {
    var outbox = await openAt('restart.db');
    await outbox.enqueue(decision('keep-me'));
    await outbox.close();

    outbox = await openAt('restart.db'); // relaunch
    addTearDown(outbox.close);
    expect(await outbox.size(), 1);
    final row = (await outbox.pending(10, 1 << 62)).single;
    expect(row.id, 'keep-me');
    expect(row.policyId, 'policy-1');
    expect(row.outcome, 'approve');
    expect(row.payloadJson, '{"score":742}',
        reason: 'the body sent to /sdk/v1/decisions must survive verbatim');
    expect(row.createdAt, '2026-09-02T10:00:00Z');
  });

  test('backoff state survives a restart -- a relaunch is not a free retry', () async {
    const deadline = 5000000;
    var outbox = await openAt('backoff-restart.db');
    await outbox.enqueue(decision('deferred'));
    await outbox.recordFailure(['deferred'], deadline);
    await outbox.close();

    outbox = await openAt('backoff-restart.db'); // relaunch
    addTearDown(outbox.close);

    // If attempts reset to 0, the next failure would restart the exponential curve at its
    // base -- an app that is relaunched often would hammer a downed backend forever.
    final row = (await outbox.pending(10, 1 << 62)).single;
    expect(row.attempts, 1);
    expect(row.nextAttemptAtMs, deadline);
    expect(await outbox.pending(10, deadline - 1), isEmpty,
        reason: 'the deferral deadline must still gate the row after a restart');
    expect((await outbox.pending(10, deadline)).single.id, 'deferred');
  });

  test('acked decisions stay gone after a restart', () async {
    var outbox = await openAt('acked.db');
    await outbox.enqueue(decision('a'));
    await outbox.enqueue(decision('b'));
    final syncer = DecisionSyncer(
      outbox: outbox,
      uploader: stubUploader(failures: 0).upload,
      clock: () => 1000000,
    );
    expect((await syncer.sync()).uploaded, 2);
    await outbox.close();

    outbox = await openAt('acked.db'); // relaunch
    addTearDown(outbox.close);
    expect(await outbox.size(), 0,
        reason: 'a resurrected row would be re-uploaded and double-counted');
  });

  test('FIFO drain order survives a restart', () async {
    var outbox = await openAt('order.db');
    await outbox.enqueue(decision('first'));
    await outbox.enqueue(decision('second'));
    await outbox.close();

    outbox = await openAt('order.db'); // relaunch
    addTearDown(outbox.close);
    await outbox.enqueue(decision('third-after-restart'));

    // The durable impl stamps seq from the wall clock, so ordering holds across processes.
    // A PER-INSTANCE counter -- the shape InMemoryDecisionOutbox uses, and what you get by
    // reaching for a plain field -- restarts at 0 on each open() and yields
    // ['first', 'third-after-restart', 'second']. Verified by mutation, not assumed.
    // (A *static* counter would survive this test, because a reopen is not a real process
    // restart; that gap is inherent to testing relaunch inside one Dart process.)
    expect(
      (await outbox.pending(10, 1 << 62)).map((d) => d.id).toList(),
      ['first', 'second', 'third-after-restart'],
    );
  });
}
