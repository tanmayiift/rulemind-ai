/// A decision awaiting upload to the backend. `id` is the client-stable key used for
/// idempotent, retry-safe ingestion (POST /sdk/v1/decisions) — a retry after a lost ack
/// never double-counts server-side. `payloadJson` is the serialized record sent verbatim.
class PendingDecision {
  const PendingDecision({
    required this.id,
    required this.policyId,
    required this.outcome,
    required this.payloadJson,
    required this.createdAt,
    this.attempts = 0,
    this.nextAttemptAtMs = 0,
  });

  final String id;
  final String? policyId;
  final String outcome;
  final String payloadJson;
  final String createdAt;
  final int attempts;
  final int nextAttemptAtMs;

  PendingDecision copyWith({int? attempts, int? nextAttemptAtMs}) => PendingDecision(
        id: id,
        policyId: policyId,
        outcome: outcome,
        payloadJson: payloadJson,
        createdAt: createdAt,
        attempts: attempts ?? this.attempts,
        nextAttemptAtMs: nextAttemptAtMs ?? this.nextAttemptAtMs,
      );
}

/// Durable, bounded queue of on-device decisions awaiting sync. The persistence seam:
/// the platform layer provides a SQLite-backed implementation (sqflite), while
/// [InMemoryDecisionOutbox] backs unit tests. All sync behaviour lives in DecisionSyncer,
/// so every implementation gets it for free.
abstract class DecisionOutbox {
  /// Persist a new decision as pending (idempotent on id).
  void enqueue(PendingDecision decision);

  /// Up to [limit] decisions eligible to send now (nextAttemptAtMs <= nowMs), oldest first.
  List<PendingDecision> pending(int limit, int nowMs);

  /// Delete the given ids after the server acknowledged them — reclaims local space.
  void markSynced(List<String> ids);

  /// Record a failed attempt: bump attempts and defer each id until nextAttemptAtMs.
  void recordFailure(List<String> ids, int nextAttemptAtMs);

  /// Total pending rows.
  int size();

  /// Drop the OLDEST rows beyond [maxRows]; returns how many were dropped.
  int trimToCapacity(int maxRows);
}

class InMemoryDecisionOutbox implements DecisionOutbox {
  final Map<String, PendingDecision> _rows = <String, PendingDecision>{};
  final Map<String, int> _order = <String, int>{};
  int _seq = 0;

  @override
  void enqueue(PendingDecision decision) {
    if (_rows.containsKey(decision.id)) return; // idempotent on id
    _rows[decision.id] = decision;
    _order[decision.id] = _seq++;
  }

  @override
  List<PendingDecision> pending(int limit, int nowMs) {
    final eligible = _rows.values.where((d) => d.nextAttemptAtMs <= nowMs).toList()
      ..sort((a, b) => (_order[a.id] ?? 0).compareTo(_order[b.id] ?? 0));
    return eligible.take(limit).toList();
  }

  @override
  void markSynced(List<String> ids) {
    for (final id in ids) {
      _rows.remove(id);
      _order.remove(id);
    }
  }

  @override
  void recordFailure(List<String> ids, int nextAttemptAtMs) {
    for (final id in ids) {
      final row = _rows[id];
      if (row != null) {
        _rows[id] = row.copyWith(attempts: row.attempts + 1, nextAttemptAtMs: nextAttemptAtMs);
      }
    }
  }

  @override
  int size() => _rows.length;

  @override
  int trimToCapacity(int maxRows) {
    final overflow = _rows.length - maxRows;
    if (overflow <= 0) return 0;
    final oldest = _rows.values.toList()
      ..sort((a, b) => (_order[a.id] ?? 0).compareTo(_order[b.id] ?? 0));
    for (final row in oldest.take(overflow)) {
      _rows.remove(row.id);
      _order.remove(row.id);
    }
    return overflow;
  }
}
