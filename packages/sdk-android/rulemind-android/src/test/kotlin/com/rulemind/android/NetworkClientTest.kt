package com.rulemind.android

import com.rulemind.core.models.RuleMindConfig
import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.MockWebServer
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import java.io.IOException

/**
 * `NetworkClient` is 268 lines and was untested. It carries the behaviour that decides what the
 * SDK does when the server is unhappy — retries, a circuit breaker, and the 304 that means "your
 * bundle is current" rather than "something went wrong".
 *
 * Against a real MockWebServer rather than a mocked OkHttp: the parts worth checking are the
 * headers actually put on the wire and the response codes actually reacted to, and a mock of the
 * client would let both drift while the test kept passing.
 */
@RunWith(RobolectricTestRunner::class)
class NetworkClientTest {

    private lateinit var server: MockWebServer

    @Before fun start() { server = MockWebServer(); server.start() }
    @After fun stop() { server.shutdown() }

    /** `retryCount = 0` unless a test is about retrying: the backoff sleeps 500ms per attempt. */
    private fun client(retryCount: Int = 0, threshold: Int = 5, trailingSlash: Boolean = false) =
        NetworkClient(
            RuleMindConfig(
                baseUrl = server.url("/").toString().let { if (trailingSlash) it else it.trimEnd('/') },
                apiKey = "test-key",
                retryCount = retryCount,
                circuitBreakerFailureThreshold = threshold,
            )
        )

    @Test
    fun `fetchBundle sends the headers the server needs to answer`() {
        server.enqueue(MockResponse().setResponseCode(304))
        client().fetchBundle(currentVersion = 7, publicKey = "PUBKEY")
        val recorded = server.takeRequest()

        assertEquals("/sdk/v1/bundle", recorded.path)
        assertEquals("test-key", recorded.getHeader("X-API-Key"))
        assertEquals("7", recorded.getHeader("X-Bundle-Version"))
        assertEquals(
            "without the client key the server cannot encrypt the bundle to this device",
            "PUBKEY", recorded.getHeader("X-Client-Public-Key"))
    }

    @Test
    fun `a 304 means the bundle is current, not a failure`() {
        server.enqueue(MockResponse().setResponseCode(304))
        val result = client().fetchBundle(3, "k")
        assertEquals("304 must read as 'unchanged'", false, result.changed)
        assertNull("nothing to apply on a 304", result.envelope)
    }

    @Test
    fun `a 200 parses the envelope`() {
        server.enqueue(MockResponse().setBody(
            """{"bundleVersion":9,"encryptedBundle":"AAA","encryptedKey":"BBB",
                "signature":"SIG","checksum":"CS"}"""))
        val result = client().fetchBundle(3, "k")
        assertTrue(result.changed)
        assertEquals(9, result.envelope!!.bundleVersion)
        assertEquals("AAA", result.envelope!!.encryptedBundle)
        assertEquals("SIG", result.envelope!!.signature)
    }

    @Test
    fun `a trailing slash on baseUrl does not produce a double slash`() {
        server.enqueue(MockResponse().setResponseCode(304))
        client(trailingSlash = true).fetchBundle(1, "k")
        assertEquals("/sdk/v1/bundle", server.takeRequest().path)
    }

    @Test
    fun `a server error is retried and the retry can succeed`() {
        server.enqueue(MockResponse().setResponseCode(500))
        server.enqueue(MockResponse().setBody("""{"bundleVersion":2}"""))
        val result = client(retryCount = 1).fetchBundle(1, "k")
        assertEquals("the second attempt should have been used", 2, result.envelope!!.bundleVersion)
        assertEquals("both attempts must reach the server", 2, server.requestCount)
    }

    @Test
    fun `a request that never succeeds throws rather than returning an empty result`() {
        repeat(3) { server.enqueue(MockResponse().setResponseCode(500)) }
        try {
            client(retryCount = 0).fetchBundle(1, "k")
            throw AssertionError("a failed fetch must throw, not return a silently empty bundle")
        } catch (expected: IOException) {
            assertTrue(expected.message!!.contains("500"))
        }
    }

    @Test
    fun `the circuit breaker opens after the threshold and stops calling the server`() {
        // The point of the breaker: once the server is clearly down, stop hammering it. Without
        // this the SDK keeps a failing backend under load from every device that has it.
        val c = client(retryCount = 0, threshold = 2)
        repeat(2) {
            server.enqueue(MockResponse().setResponseCode(500))
            runCatching { c.fetchBundle(1, "k") }
        }
        val before = server.requestCount
        try {
            c.fetchBundle(1, "k")
            throw AssertionError("the breaker should be open")
        } catch (expected: IOException) {
            assertTrue("the breaker must say so", expected.message!!.contains("Circuit breaker"))
        }
        assertEquals(
            "an open breaker must not reach the server at all",
            before, server.requestCount)
    }

    @Test
    fun `health reaches the health path`() {
        server.enqueue(MockResponse().setBody("""{"status":"ok"}"""))
        assertEquals("ok", client().health()["status"])
        assertEquals("/sdk/v1/health", server.takeRequest().path)
    }
}
