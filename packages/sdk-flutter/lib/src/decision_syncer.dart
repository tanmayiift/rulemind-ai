import 'dart:math';

import 'decision_outbox.dart';

/// Result of uploading one batch. `ackedIds` are the ids the server confirmed (safe to clear).
class UploadResult {
  const UploadResult(this.ok, [this.ackedIds = const <String>[]]);
  factory UploadResult.success(List<String> ackedIds) => UploadResult(true, ackedIds);
  factory UploadResult.failure() => const UploadResult(false);
  final bool ok;
  final List<String> ackedIds;
}

class SyncStats {
  const SyncStats({required this.uploaded, required this.failed, required this.dropped});
  final int uploaded;
  final int failed;
  final int dropped;
}

/// Tunable limits — config, not code, so they can move per deployment.
class DecisionSyncConfig {
  const DecisionSyncConfig({
    this.batchSize = 200,
    this.baseBackoffMs = 60000, // 1 min
    this.maxBackoffMs = 6 * 60 * 60 * 1000, // 6 h ceiling
    this.capacity = 50000, // generous local cap (raise-the-cap policy)
    this.maxRunsPerSync = 100,
  });
  final int batchSize;
  final int baseBackoffMs;
  final int maxBackoffMs;
  final int capacity;
  final int maxRunsPerSync;
}

/// Drains a [DecisionOutbox] to the backend. The whole sync contract, kept off the platform
/// layer so every outbox implementation behaves identically: trim to capacity, send in bounded
/// batches, clear exactly the acked ids on success (idempotent server-side), and on failure
/// defer with EXPONENTIAL BACKOFF + jitter then stop the pass. A 1–2×/day background job calls [sync].
class DecisionSyncer {
  DecisionSyncer({
    required DecisionOutbox outbox,
    required Future<UploadResult> Function(List<PendingDecision>) uploader,
    DecisionSyncConfig config = const DecisionSyncConfig(),
    int Function()? clock,
    Random? random,
  })  : _outbox = outbox,
        _uploader = uploader,
        _config = config,
        _clock = clock ?? (() => DateTime.now().millisecondsSinceEpoch),
        _random = random ?? Random();

  final DecisionOutbox _outbox;
  final Future<UploadResult> Function(List<PendingDecision>) _uploader;
  final DecisionSyncConfig _config;
  final int Function() _clock;
  final Random _random;

  Future<SyncStats> sync() async {
    final dropped = await _outbox.trimToCapacity(_config.capacity);
    var uploaded = 0;
    var failed = 0;
    var runs = 0;
    while (runs++ < _config.maxRunsPerSync) {
      final batch = await _outbox.pending(_config.batchSize, _clock());
      if (batch.isEmpty) break;
      UploadResult result;
      try {
        result = await _uploader(batch);
      } catch (_) {
        result = UploadResult.failure();
      }
      if (result.ok) {
        final acked = result.ackedIds.isEmpty ? batch.map((d) => d.id).toList() : result.ackedIds;
        await _outbox.markSynced(acked);
        uploaded += acked.length;
      } else {
        final attempts = batch.map((d) => d.attempts).fold<int>(0, max) + 1;
        await _outbox.recordFailure(batch.map((d) => d.id).toList(), _clock() + backoffMs(attempts));
        failed += batch.length;
        break; // network is down — retry on the next scheduled run
      }
    }
    return SyncStats(uploaded: uploaded, failed: failed, dropped: dropped);
  }

  /// Exponential backoff (base * 2^(attempt-1), capped) with ±15% jitter.
  int backoffMs(int attempt) {
    final exp = min(_config.maxBackoffMs.toDouble(),
        _config.baseBackoffMs.toDouble() * pow(2.0, (attempt - 1).clamp(0, 62)));
    final jitter = 0.85 + _random.nextDouble() * 0.30;
    return min((exp * jitter).toInt(), _config.maxBackoffMs);
  }
}
