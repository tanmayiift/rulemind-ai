package com.rulemind.core

import java.util.concurrent.ConcurrentHashMap

/**
 * In-memory [DecisionOutbox] — backs unit tests and hosts without a durable store. The
 * platform SDKs supply a SQLite-backed implementation (Room / sqflite) with identical
 * semantics; the behaviour that matters (batching, backoff, clear-on-ack, capacity) is in
 * [DecisionSyncer] and shared by all implementations.
 */
class InMemoryDecisionOutbox : DecisionOutbox {
    private val rows = ConcurrentHashMap<String, PendingDecision>()
    private var seq = 0L
    private val order = ConcurrentHashMap<String, Long>() // insertion order for oldest-first

    override suspend fun enqueue(decision: PendingDecision) {
        if (rows.containsKey(decision.id)) return // idempotent on id
        rows[decision.id] = decision
        order[decision.id] = seq++
    }

    override suspend fun pending(limit: Int, nowMs: Long): List<PendingDecision> =
        rows.values
            .filter { it.nextAttemptAtMs <= nowMs }
            .sortedBy { order[it.id] ?: 0 }
            .take(limit)

    override suspend fun markSynced(ids: List<String>) {
        ids.forEach { rows.remove(it); order.remove(it) }
    }

    override suspend fun recordFailure(ids: List<String>, nextAttemptAtMs: Long) {
        ids.forEach { id ->
            rows[id]?.let { rows[id] = it.copy(attempts = it.attempts + 1, nextAttemptAtMs = nextAttemptAtMs) }
        }
    }

    override suspend fun size(): Int = rows.size

    override suspend fun trimToCapacity(maxRows: Int): Int {
        val overflow = rows.size - maxRows
        if (overflow <= 0) return 0
        val oldest = rows.values.sortedBy { order[it.id] ?: 0 }.take(overflow)
        oldest.forEach { rows.remove(it.id); order.remove(it.id) }
        return oldest.size
    }
}
