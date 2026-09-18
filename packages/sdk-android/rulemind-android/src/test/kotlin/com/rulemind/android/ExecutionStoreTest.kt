package com.rulemind.android

import android.content.Context
import androidx.test.core.app.ApplicationProvider
import com.rulemind.core.models.Decision
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import javax.crypto.KeyGenerator
import javax.crypto.SecretKey

/**
 * `ExecutionStore` is what lets a paused decision be resumed after the app is killed, and it had
 * no tests until 2026-09-18.
 *
 * The behaviour worth pinning is `persist`, which is NOT "save everything": it saves only
 * executions that are still RESUMABLE and prunes the rest, so the store cannot grow without
 * bound. Get that backwards and either the store leaks every decision the device ever made, or a
 * paused review is silently dropped and the user's journey dead-ends.
 */
@RunWith(RobolectricTestRunner::class)
class ExecutionStoreTest {

    // Robolectric has no AndroidKeyStore, so the key is injected. See SecurePrefsTest for
    // exactly what that does and does not prove.
    private val key: SecretKey =
        KeyGenerator.getInstance("AES").apply { init(256) }.generateKey()

    private fun store() =
        ExecutionStore(ApplicationProvider.getApplicationContext<Context>()) { key }

    private fun decision(
        id: String,
        status: String = "completed",
        pending: List<Map<String, Any?>> = emptyList(),
    ) = Decision(outcome = "approve", executionId = id, status = status, pendingOperations = pending)

    @Test
    fun `a saved decision comes back`() {
        val store = store()
        store.save(decision("exec-1"))
        val loaded = store.get("exec-1")
        assertNotNull("a saved execution must be retrievable", loaded)
        assertEquals("exec-1", loaded!!.executionId)
        assertEquals("approve", loaded.outcome)
    }

    @Test
    fun `an unknown id reads as null`() {
        assertNull(store().get("never-saved"))
    }

    @Test
    fun `persist KEEPS an execution that is still paused`() {
        val store = store()
        store.persist(decision("paused-1", status = "paused"))
        assertNotNull("a paused execution is resumable and must be kept", store.get("paused-1"))
    }

    @Test
    fun `persist KEEPS a completed execution with an undelivered operation`() {
        val store = store()
        store.persist(decision("pending-1", pending = listOf(mapOf("status" to "queued"))))
        assertNotNull(
            "completed but with an undelivered operation is still resumable",
            store.get("pending-1"))
    }

    @Test
    fun `persist PRUNES a completed execution whose operations were all delivered`() {
        val store = store()
        store.save(decision("done-1"))
        assertNotNull("precondition: it was saved", store.get("done-1"))
        store.persist(decision("done-1", pending = listOf(mapOf("status" to "delivered"))))
        assertNull(
            "a terminal decision must be pruned -- the durable outbox is the record of those, " +
                "and keeping them here is how this store grows without bound",
            store.get("done-1"))
    }

    @Test
    fun `list returns every stored execution and nothing else`() {
        val store = store()
        store.save(decision("a"))
        store.save(decision("b"))
        val ids = store.list().mapNotNull { it.executionId }.toSet()
        assertEquals(setOf("a", "b"), ids)
    }

    @Test
    fun `delete removes one execution and leaves the index consistent`() {
        val store = store()
        store.save(decision("keep"))
        store.save(decision("drop"))
        store.delete("drop")
        assertNull(store.get("drop"))
        assertNotNull(store.get("keep"))
        assertEquals(
            "the id index must be updated too, or list() resurrects a deleted execution",
            setOf("keep"), store.list().mapNotNull { it.executionId }.toSet())
    }

    @Test
    fun `a decision with no execution id is ignored rather than crashing`() {
        val store = store()
        store.save(Decision(outcome = "approve", executionId = null))
        assertTrue("nothing should have been stored", store.list().isEmpty())
    }
}
