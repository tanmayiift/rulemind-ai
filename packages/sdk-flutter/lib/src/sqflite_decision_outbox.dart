import 'package:sqflite/sqflite.dart';

import 'decision_outbox.dart';

/// Durable, SQLite-backed [DecisionOutbox] (sqflite). Optimised for a high-volume queue:
///
///  - **WAL** journal so the sync job can read while `evaluate()` writes, without locking;
///  - an index on `(next_attempt_at)` so eligibility + oldest-first draining and capacity
///    trimming stay index-backed (no full scans) at 50k+ rows;
///  - batch INSERT/DELETE inside a single transaction.
///
/// The database factory + path are injectable so tests run headless via
/// `sqflite_common_ffi` (no device/emulator needed).
class SqfliteDecisionOutbox implements DecisionOutbox {
  SqfliteDecisionOutbox._(this._db);

  final Database _db;
  static const String _table = "pending_decisions";

  static Future<SqfliteDecisionOutbox> open({
    DatabaseFactory? factory,
    String path = "rulemind_decisions.db",
  }) async {
    final dbFactory = factory ?? databaseFactory;
    final db = await dbFactory.openDatabase(
      path,
      options: OpenDatabaseOptions(
        version: 1,
        onConfigure: (db) async {
          await db.execute("PRAGMA journal_mode=WAL");
        },
        onCreate: (db, _) async {
          await db.execute('''
            CREATE TABLE $_table (
              id TEXT PRIMARY KEY,
              policy_id TEXT,
              outcome TEXT NOT NULL,
              payload_json TEXT NOT NULL,
              created_at TEXT NOT NULL,
              attempts INTEGER NOT NULL DEFAULT 0,
              next_attempt_at INTEGER NOT NULL DEFAULT 0,
              seq INTEGER NOT NULL
            )
          ''');
          await db.execute("CREATE INDEX idx_${_table}_eligible ON $_table (next_attempt_at, seq)");
        },
      ),
    );
    return SqfliteDecisionOutbox._(db);
  }

  PendingDecision _fromRow(Map<String, Object?> r) => PendingDecision(
        id: r["id"] as String,
        policyId: r["policy_id"] as String?,
        outcome: r["outcome"] as String,
        payloadJson: r["payload_json"] as String,
        createdAt: r["created_at"] as String,
        attempts: (r["attempts"] as int?) ?? 0,
        nextAttemptAtMs: (r["next_attempt_at"] as int?) ?? 0,
      );

  @override
  Future<void> enqueue(PendingDecision d) async {
    // Monotonic insertion order for oldest-first draining / trimming.
    final seq = DateTime.now().microsecondsSinceEpoch;
    await _db.insert(
      _table,
      {
        "id": d.id,
        "policy_id": d.policyId,
        "outcome": d.outcome,
        "payload_json": d.payloadJson,
        "created_at": d.createdAt,
        "attempts": d.attempts,
        "next_attempt_at": d.nextAttemptAtMs,
        "seq": seq,
      },
      conflictAlgorithm: ConflictAlgorithm.ignore, // idempotent on id (PK)
    );
  }

  @override
  Future<List<PendingDecision>> pending(int limit, int nowMs) async {
    final rows = await _db.query(
      _table,
      where: "next_attempt_at <= ?",
      whereArgs: [nowMs],
      orderBy: "seq ASC",
      limit: limit,
    );
    return rows.map(_fromRow).toList();
  }

  @override
  Future<void> markSynced(List<String> ids) async {
    if (ids.isEmpty) return;
    final placeholders = List.filled(ids.length, "?").join(",");
    await _db.delete(_table, where: "id IN ($placeholders)", whereArgs: ids);
  }

  @override
  Future<void> recordFailure(List<String> ids, int nextAttemptAtMs) async {
    if (ids.isEmpty) return;
    final placeholders = List.filled(ids.length, "?").join(",");
    await _db.rawUpdate(
      "UPDATE $_table SET attempts = attempts + 1, next_attempt_at = ? WHERE id IN ($placeholders)",
      [nextAttemptAtMs, ...ids],
    );
  }

  @override
  Future<int> size() async =>
      Sqflite.firstIntValue(await _db.rawQuery("SELECT COUNT(*) FROM $_table")) ?? 0;

  @override
  Future<int> trimToCapacity(int maxRows) async {
    final total = await size();
    final overflow = total - maxRows;
    if (overflow <= 0) return 0;
    // Delete the oldest `overflow` rows (smallest seq) in one statement.
    final deleted = await _db.rawDelete(
      "DELETE FROM $_table WHERE id IN (SELECT id FROM $_table ORDER BY seq ASC LIMIT ?)",
      [overflow],
    );
    return deleted;
  }

  Future<void> close() => _db.close();
}
