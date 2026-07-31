package com.rulemind.core

import java.util.Random
import kotlin.math.min
import kotlin.math.pow

/** Result of uploading one batch. `ackedIds` are the ids the server confirmed (safe to clear). */
data class UploadResult(val ok: Boolean, val ackedIds: List<String> = emptyList()) {
    companion object {
        fun success(ackedIds: List<String>): UploadResult = UploadResult(true, ackedIds)
        fun failure(): UploadResult = UploadResult(false)
    }
}

data class SyncStats(val uploaded: Int, val failed: Int, val dropped: Int)

/** Tunable limits — config, not code, so they can move per deployment. */
data class DecisionSyncConfig(
    val batchSize: Int = 200,
    val baseBackoffMs: Long = 60_000L,            // 1 min
    val maxBackoffMs: Long = 6 * 60 * 60 * 1000L, // 6 h ceiling
    val capacity: Int = 50_000,                   // generous local cap (raise-the-cap policy)
    val maxRunsPerSync: Int = 100,                // safety bound on batches per pass
)

/**
 * Drains a [DecisionOutbox] to the backend. This is the whole sync contract, kept off the
 * platform layer so every outbox implementation (in-memory, Room, sqflite) behaves identically:
 *
 *  - trims the outbox to capacity first (bounded local space even during a long outage),
 *  - sends decisions in bounded batches to the `uploader`,
 *  - on success clears exactly the acked ids (idempotent server-side, so retries never double-count),
 *  - on failure defers the batch with EXPONENTIAL BACKOFF + jitter and stops the pass (retry later).
 *
 * A 1–2×/day background job (WorkManager on Android, workmanager on Flutter) calls [sync].
 */
class DecisionSyncer(
    private val outbox: DecisionOutbox,
    private val uploader: suspend (List<PendingDecision>) -> UploadResult,
    private val config: DecisionSyncConfig = DecisionSyncConfig(),
    private val clock: () -> Long = { System.currentTimeMillis() },
    private val random: Random = Random(),
) {
    suspend fun sync(): SyncStats {
        val dropped = outbox.trimToCapacity(config.capacity)
        var uploaded = 0
        var failed = 0
        var runs = 0
        while (runs++ < config.maxRunsPerSync) {
            val batch = outbox.pending(config.batchSize, clock())
            if (batch.isEmpty()) break
            val result = try {
                uploader(batch)
            } catch (_: Exception) {
                UploadResult.failure()
            }
            if (result.ok) {
                // Trust the server ack; fall back to the batch ids if the server didn't echo them.
                val acked = result.ackedIds.ifEmpty { batch.map { it.id } }
                outbox.markSynced(acked)
                uploaded += acked.size
            } else {
                val attempts = (batch.maxOfOrNull { it.attempts } ?: 0) + 1
                outbox.recordFailure(batch.map { it.id }, clock() + backoffMs(attempts))
                failed += batch.size
                break // network is down — try again on the next scheduled run
            }
        }
        return SyncStats(uploaded = uploaded, failed = failed, dropped = dropped)
    }

    /** Exponential backoff (base * 2^(attempt-1), capped) with ±15% jitter to avoid thundering herds. */
    fun backoffMs(attempt: Int): Long {
        val exp = min(config.maxBackoffMs.toDouble(), config.baseBackoffMs.toDouble() * 2.0.pow((attempt - 1).coerceAtLeast(0)))
        val jitter = 0.85 + random.nextDouble() * 0.30
        return (exp * jitter).toLong().coerceAtMost(config.maxBackoffMs)
    }
}
