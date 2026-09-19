package com.rulemind.android

import android.content.Context
import androidx.test.core.app.ApplicationProvider
import com.rulemind.core.PendingDecision
import kotlinx.coroutines.runBlocking
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner

/**
 * The durable queue of on-device decisions awaiting sync — 137 lines, untested until now.
 *
 * This is the thing standing between "the device decided" and "the server knows", so its failure
 * modes are data loss and double-delivery rather than a wrong answer on screen. The behaviours
 * below are the ones a reader would otherwise have to infer from SQL:
 *
 *   enqueue           idempotent on id, so a retried write cannot double-deliver
 *   pending           only what is ELIGIBLE (backoff respected), oldest-first, honouring limit
 *   trimToCapacity    drops the OLDEST — the opposite would discard the freshest decisions,
 *                     which is the ones you most want
 */
@RunWith(RobolectricTestRunner::class)
class SqliteDecisionOutboxTest {

    private fun outbox() =
        SqliteDecisionOutbox(ApplicationProvider.getApplicationContext<Context>())

    private fun decision(id: String, nextAttemptAtMs: Long = 0, attempts: Int = 0) =
        PendingDecision(
            id = id,
            policyId = "pol_loan",
            outcome = "approve",
            payloadJson = """{"amount":100}""",
            createdAt = "2026-09-19T00:00:00Z",
            attempts = attempts,
            nextAttemptAtMs = nextAttemptAtMs,
        )

    @Test
    fun `an enqueued decision is pending and round-trips its fields`() = runBlocking {
        val box = outbox()
        box.enqueue(decision("d1"))
        val pending = box.pending(limit = 10, nowMs = 1_000)
        assertEquals(1, pending.size)
        val d = pending.first()
        assertEquals("d1", d.id)
        assertEquals("pol_loan", d.policyId)
        assertEquals("approve", d.outcome)
        assertEquals("""{"amount":100}""", d.payloadJson)
        assertEquals("2026-09-19T00:00:00Z", d.createdAt)
    }

    @Test
    fun `enqueuing the same id twice is a no-op, not a duplicate`() = runBlocking {
        // The sync path can retry a write it is not sure landed. Without CONFLICT_IGNORE that
        // retry becomes a second delivery of the same decision to the server.
        val box = outbox()
        box.enqueue(decision("same"))
        box.enqueue(decision("same"))
        assertEquals("a repeated enqueue must not duplicate", 1, box.size())
    }

    @Test
    fun `a repeated enqueue must not RESET the retry state`() = runBlocking {
        // The row count alone does not distinguish CONFLICT_IGNORE from CONFLICT_REPLACE -- both
        // leave exactly one row, which is why the test above passed against either. This is the
        // assertion that separates them, and it is the one that matters:
        //
        // REPLACE deletes and re-inserts, so `attempts` and `next_attempt_at` revert to the
        // INCOMING values. A decision that had failed three times and was backed off would come
        // back as attempts=0 and immediately eligible -- the SDK would re-hammer a server it had
        // already learned was failing, and the give-up logic would never fire.
        //
        // Found by mutation checking: swapping IGNORE for REPLACE broke nothing until this existed.
        val box = outbox()
        box.enqueue(decision("retried"))
        box.recordFailure(listOf("retried"), nextAttemptAtMs = 50_000)

        // The same decision arrives again, as a fresh one would: attempts 0, no backoff.
        box.enqueue(decision("retried", nextAttemptAtMs = 0, attempts = 0))

        assertEquals(
            "the backoff was cleared -- the retry state was overwritten",
            0, box.pending(limit = 10, nowMs = 49_999).size)
        val row = box.pending(limit = 10, nowMs = 50_000).single()
        assertEquals("the attempt count was reset", 1, row.attempts)
        assertEquals(50_000, row.nextAttemptAtMs)
    }

    @Test
    fun `a decision whose backoff has not elapsed is NOT pending`() = runBlocking {
        val box = outbox()
        box.enqueue(decision("later", nextAttemptAtMs = 10_000))
        assertEquals(
            "returning it early defeats the backoff and hammers a failing server",
            0, box.pending(limit = 10, nowMs = 5_000).size)
        assertEquals(1, box.pending(limit = 10, nowMs = 10_000).size)
    }

    @Test
    fun `pending drains oldest first`() = runBlocking {
        val box = outbox()
        box.enqueue(decision("first"))
        box.enqueue(decision("second"))
        box.enqueue(decision("third"))
        assertEquals(
            listOf("first", "second", "third"),
            box.pending(limit = 10, nowMs = 1_000).map { it.id })
    }

    @Test
    fun `pending honours the limit`() = runBlocking {
        val box = outbox()
        repeat(5) { box.enqueue(decision("d$it")) }
        assertEquals(2, box.pending(limit = 2, nowMs = 1_000).size)
    }

    @Test
    fun `markSynced removes exactly those ids`() = runBlocking {
        val box = outbox()
        box.enqueue(decision("a")); box.enqueue(decision("b")); box.enqueue(decision("c"))
        box.markSynced(listOf("a", "c"))
        assertEquals(listOf("b"), box.pending(limit = 10, nowMs = 1_000).map { it.id })
    }

    @Test
    fun `markSynced with an empty list changes nothing`() = runBlocking {
        // Guarding this matters: an unguarded `id IN ()` is a SQL error, and a delete with no
        // WHERE would empty the queue.
        val box = outbox()
        box.enqueue(decision("a"))
        box.markSynced(emptyList())
        assertEquals(1, box.size())
    }

    @Test
    fun `recordFailure increments attempts and pushes the next attempt out`() = runBlocking {
        val box = outbox()
        box.enqueue(decision("a"))
        box.recordFailure(listOf("a"), nextAttemptAtMs = 9_000)
        assertEquals("not eligible until the new time", 0, box.pending(limit = 10, nowMs = 8_999).size)
        val after = box.pending(limit = 10, nowMs = 9_000).single()
        assertEquals("the attempt count is what drives give-up logic", 1, after.attempts)
        assertEquals(9_000, after.nextAttemptAtMs)
    }

    @Test
    fun `trimToCapacity drops the OLDEST and reports how many went`() = runBlocking {
        val box = outbox()
        listOf("old1", "old2", "new1", "new2").forEach { box.enqueue(decision(it)) }
        val dropped = box.trimToCapacity(maxRows = 2)
        assertEquals(2, dropped)
        assertEquals(
            "capacity trimming must discard the oldest -- dropping the newest would throw away " +
                "the decisions most likely to still matter",
            listOf("new1", "new2"),
            box.pending(limit = 10, nowMs = 1_000).map { it.id })
    }

    @Test
    fun `trimToCapacity under the limit is a no-op`() = runBlocking {
        val box = outbox()
        box.enqueue(decision("a"))
        assertEquals(0, box.trimToCapacity(maxRows = 10))
        assertEquals(1, box.size())
    }

    @Test
    fun `size counts everything, eligible or not`() = runBlocking {
        val box = outbox()
        box.enqueue(decision("now"))
        box.enqueue(decision("later", nextAttemptAtMs = 99_999))
        assertEquals("size is the QUEUE, not the eligible subset", 2, box.size())
        assertTrue(box.pending(limit = 10, nowMs = 1_000).size < box.size())
    }
}
