package com.rulemind.android

import org.junit.Assert.assertNotNull
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner

/**
 * The "you have not called initialize" contract, IN ITS OWN CLASS ON PURPOSE.
 *
 * `RuleMind` is a mutable singleton (`object`), so once any test initialises it the object stays
 * initialised for every later test sharing that classloader. These assertions were originally in
 * `RuleMindTest` and passed or failed depending on JUnit's method order — which is exactly the
 * kind of test that is green by luck. Robolectric gives each test CLASS its own classloader, so
 * splitting them makes the precondition real rather than hoped for.
 *
 * Splitting was not enough on its own: Robolectric reuses a classloader across test CLASSES too,
 * so the object stayed initialised anyway. `RuleMind.resetForTesting()` (internal, not published)
 * is what makes the precondition real.
 */
@RunWith(RobolectricTestRunner::class)
class RuleMindUninitializedTest {

    @Before
    fun uninitialize() = RuleMind.resetForTesting()


    @Test
    fun `health before initialize refuses by name`() {
        val error = runCatching { RuleMind.health() }.exceptionOrNull()
        assertNotNull("calling before initialize must fail", error)
        assertTrue(
            "the message must name the SDK and the cause, not surface a bare NPE: ${error!!.message}",
            error.message!!.contains("not initialized"))
    }

    @Test
    fun `syncNow before initialize refuses by name`() {
        val error = runCatching { RuleMind.syncNow() }.exceptionOrNull()
        assertNotNull(error)
        assertTrue(error!!.message!!.contains("not initialized"))
    }

    @Test
    fun `evaluate before initialize refuses by name`() {
        val error = runCatching { RuleMind.evaluate("pol", emptyMap()) }.exceptionOrNull()
        assertNotNull("evaluate is the main entry point; it must refuse clearly", error)
        assertTrue(error!!.message!!.contains("not initialized"))
    }
}
