package com.rulemind.core

/**
 * A decision awaiting upload to the backend. The `id` is the client-stable key used for
 * idempotent, retry-safe ingestion (see POST /sdk/v1/decisions) — so a retry after a lost
 * ack never double-counts server-side. `payloadJson` is the fully-serialized decision
 * record the syncer sends verbatim.
 */
data class PendingDecision(
    val id: String,
    val policyId: String?,
    val outcome: String,
    val payloadJson: String,
    val createdAt: String,
    val attempts: Int = 0,
    val nextAttemptAtMs: Long = 0,
)

/**
 * Durable, bounded queue of on-device decisions awaiting sync. This is the persistence
 * seam: the platform layer provides a SQLite-backed implementation (Room on Android),
 * while [InMemoryDecisionOutbox] backs unit tests and non-persistent hosts. All the sync
 * behaviour (batching, exponential backoff, clear-on-ack, capacity) lives in
 * [DecisionSyncer], so every implementation gets it for free.
 */
// Every method is `suspend` — durable stores (SQLite) do I/O, which MUST run off the main
// thread (the Room/raw-SQLite adapter uses Dispatchers.IO) so a decision write on evaluate()
// never blocks the UI and never triggers an ANR. The in-memory impl completes immediately.
interface DecisionOutbox {
    /** Persist a new decision as pending (idempotent on id — enqueuing the same id twice is a no-op). */
    suspend fun enqueue(decision: PendingDecision)

    /** Up to [limit] decisions eligible to send now (nextAttemptAtMs <= nowMs), oldest first. */
    suspend fun pending(limit: Int, nowMs: Long): List<PendingDecision>

    /** Delete the given ids after the server acknowledged them — reclaims local space. */
    suspend fun markSynced(ids: List<String>)

    /** Record a failed attempt: bump attempts and defer each id until nextAttemptAtMs. */
    suspend fun recordFailure(ids: List<String>, nextAttemptAtMs: Long)

    /** Total pending rows. */
    suspend fun size(): Int

    /**
     * Enforce a hard cap so a long sync outage can't grow the local store without bound.
     * Drops the OLDEST rows beyond [maxRows]; returns how many were dropped.
     */
    suspend fun trimToCapacity(maxRows: Int): Int
}
