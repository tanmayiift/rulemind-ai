package com.rulemind.core

import java.util.Random
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertTrue

class DecisionSyncerTest {
    private fun decision(id: String) = PendingDecision(
        id = id, policyId = "p", outcome = "approve", payloadJson = "{}", createdAt = "2026-07-31T10:00:00Z",
    )

    @Test
    fun uploadsAllPendingThenEmptiesTheOutbox() {
        val outbox = InMemoryDecisionOutbox()
        (0 until 5).forEach { outbox.enqueue(decision("d$it")) }
        val syncer = DecisionSyncer(outbox, uploader = { batch -> UploadResult.success(batch.map { it.id }) })
        val stats = syncer.sync()
        assertEquals(5, stats.uploaded)
        assertEquals(0, outbox.size())
    }

    @Test
    fun enqueueIsIdempotentOnId() {
        val outbox = InMemoryDecisionOutbox()
        outbox.enqueue(decision("dup"))
        outbox.enqueue(decision("dup"))
        assertEquals(1, outbox.size())
    }

    @Test
    fun failureKeepsRowsAndDefersWithBackoff() {
        val outbox = InMemoryDecisionOutbox()
        (0 until 3).forEach { outbox.enqueue(decision("d$it")) }
        var now = 1_000_000L
        val syncer = DecisionSyncer(
            outbox,
            uploader = { UploadResult.failure() },
            config = DecisionSyncConfig(baseBackoffMs = 60_000L),
            clock = { now },
            random = Random(1),
        )
        val stats = syncer.sync()
        assertEquals(0, stats.uploaded)
        assertEquals(3, outbox.size()) // nothing lost
        // Not eligible yet (deferred into the future by backoff).
        assertTrue(outbox.pending(10, now).isEmpty())
        // Eligible again once enough time passes.
        assertTrue(outbox.pending(10, now + 60L * 60 * 1000).isNotEmpty())
    }

    @Test
    fun onlyAckedIdsAreCleared() {
        val outbox = InMemoryDecisionOutbox()
        (0 until 4).forEach { outbox.enqueue(decision("d$it")) }
        // Server acks a subset (e.g. partial write); the rest must remain for retry.
        val syncer = DecisionSyncer(outbox, uploader = { UploadResult.success(listOf("d0", "d1")) },
            config = DecisionSyncConfig(maxRunsPerSync = 1))
        syncer.sync()
        val remaining = outbox.pending(10, Long.MAX_VALUE).map { it.id }.toSet()
        assertEquals(setOf("d2", "d3"), remaining)
    }

    @Test
    fun backoffGrowsExponentiallyAndIsCapped() {
        val outbox = InMemoryDecisionOutbox()
        val syncer = DecisionSyncer(outbox, uploader = { UploadResult.failure() },
            config = DecisionSyncConfig(baseBackoffMs = 1000L, maxBackoffMs = 100_000L), random = Random(1))
        val b1 = syncer.backoffMs(1)
        val b3 = syncer.backoffMs(3)
        assertTrue(b3 > b1, "attempt 3 backoff ($b3) should exceed attempt 1 ($b1)")
        assertTrue(syncer.backoffMs(50) <= 100_000L, "backoff must be capped")
    }

    @Test
    fun capacityTrimDropsOldestFirst() {
        val outbox = InMemoryDecisionOutbox()
        (0 until 10).forEach { outbox.enqueue(decision("d$it")) }
        val dropped = outbox.trimToCapacity(6)
        assertEquals(4, dropped)
        val remaining = outbox.pending(100, Long.MAX_VALUE).map { it.id }
        assertEquals(listOf("d4", "d5", "d6", "d7", "d8", "d9"), remaining) // oldest four gone
    }

    @Test
    fun largeBacklogDrainsInBatches() {
        val outbox = InMemoryDecisionOutbox()
        (0 until 1000).forEach { outbox.enqueue(decision("d$it")) }
        var batches = 0
        val syncer = DecisionSyncer(outbox,
            uploader = { batch -> batches++; UploadResult.success(batch.map { it.id }) },
            config = DecisionSyncConfig(batchSize = 200))
        val stats = syncer.sync()
        assertEquals(1000, stats.uploaded)
        assertEquals(5, batches) // 1000 / 200
        assertEquals(0, outbox.size())
    }
}
