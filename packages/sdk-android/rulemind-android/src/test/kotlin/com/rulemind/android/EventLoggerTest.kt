package com.rulemind.android

import android.content.Context
import androidx.test.core.app.ApplicationProvider
import androidx.work.testing.WorkManagerTestInitHelper
import com.rulemind.core.models.RuleMindConfig
import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.MockWebServer
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import javax.crypto.KeyGenerator
import javax.crypto.SecretKey

/**
 * The SDK's analytics queue. Its one dangerous behaviour is in `flush`.
 */
@RunWith(RobolectricTestRunner::class)
class EventLoggerTest {

    private lateinit var server: MockWebServer
    private val key: SecretKey = KeyGenerator.getInstance("AES").apply { init(256) }.generateKey()
    private val context: Context get() = ApplicationProvider.getApplicationContext()

    @Before fun setUp() {
        WorkManagerTestInitHelper.initializeTestWorkManager(context)
        server = MockWebServer(); server.start()
    }
    @After fun tearDown() = server.shutdown()

    private fun logger(batchSize: Int = 50): EventLogger {
        val config = RuleMindConfig(
            baseUrl = server.url("/").toString().trimEnd('/'),
            apiKey = "k",
            eventFlushBatchSize = batchSize,
            retryCount = 0,
        )
        return EventLogger(context, config, NetworkClient(config)) { key }
    }

    @Test
    fun `a failed upload KEEPS the events instead of dropping them`() {
        // The whole point. `flush` clears `pending` only when the upload returns true; clearing
        // unconditionally would mean every network blip silently destroys the events it was
        // trying to send, and the loss is invisible -- there is nothing left to notice.
        server.enqueue(MockResponse().setResponseCode(500))
        val log = logger()
        log.queue("decision.made")
        log.flush()

        // A second flush must still have something to send.
        server.enqueue(MockResponse().setBody("""{"ok":true}"""))
        log.flush()
        assertTrue(
            "the event was dropped on the failed upload -- it should have been retained",
            server.requestCount >= 2)
    }

    @Test
    fun `a successful upload clears the queue so events are not sent twice`() {
        server.enqueue(MockResponse().setBody("""{"ok":true}"""))
        val log = logger()
        log.queue("decision.made")
        log.flush()
        val afterFirst = server.requestCount

        log.flush() // nothing left; must not re-send
        assertEquals("a cleared queue must not re-upload", afterFirst, server.requestCount)
    }

    @Test
    fun `flushing an empty queue makes no request at all`() {
        logger().flush()
        assertEquals(0, server.requestCount)
    }

    @Test
    fun `reaching the batch size flushes immediately rather than waiting`() {
        server.enqueue(MockResponse().setBody("""{"ok":true}"""))
        val log = logger(batchSize = 2)
        log.queue("a")
        assertEquals("below the batch size it should defer to the worker", 0, server.requestCount)
        log.queue("b")
        assertEquals("hitting the batch size must flush now", 1, server.requestCount)
    }

    @Test
    fun `queued events survive a new logger over the same store`() {
        // Persistence is what makes the queue durable across a process death; without it a
        // crash between queue() and flush() loses everything buffered.
        val first = logger()
        first.queue("persisted.event")

        server.enqueue(MockResponse().setBody("""{"ok":true}"""))
        logger().flush()
        assertEquals(
            "a fresh logger must see events the previous one persisted",
            1, server.requestCount)
    }
}
