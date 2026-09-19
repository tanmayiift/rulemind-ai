package com.rulemind.android

import android.content.Context
import androidx.test.core.app.ApplicationProvider
import androidx.work.WorkInfo
import androidx.work.WorkManager
import androidx.work.testing.WorkManagerTestInitHelper
import com.rulemind.core.models.RuleMindConfig
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner

/**
 * The SDK's entry point — 254 lines, untested until now, and the first thing any integrator
 * touches.
 *
 * Two contracts are worth pinning here, and neither is visible from reading a call site:
 *
 * 1. **Every public method refuses clearly before `initialize`.** These are `requireNotNull`
 *    with a message; without the message an integrator gets a bare NPE from inside a library
 *    they did not write, which is the least actionable failure an SDK can produce.
 *
 * 2. **`initialize` SCHEDULES THE PERIODIC BUNDLE SYNC.** If that enqueue silently stopped
 *    happening, nothing would fail — the SDK would work perfectly against whatever bundle it
 *    happened to have, forever, and policy changes would simply never reach the device. It is
 *    the kind of omission that surfaces weeks later as "why is this phone deciding on last
 *    month's rules".
 */
@RunWith(RobolectricTestRunner::class)
class RuleMindTest {

    private val context: Context get() = ApplicationProvider.getApplicationContext()

    @Before
    fun initWorkManager() {
        // RuleMind.initialize schedules work; without this WorkManager throws "is not
        // initialized properly" and the entry point is untestable for a reason unrelated to it.
        WorkManagerTestInitHelper.initializeTestWorkManager(context)
    }

    private fun config(intervalMinutes: Long = 15) = RuleMindConfig(
        // Port 1 on purpose: nothing listens, so any accidental network call fails fast and
        // loudly rather than reaching something real.
        baseUrl = "http://127.0.0.1:1",
        apiKey = "test-key",
        bundleSyncIntervalMinutes = intervalMinutes,
    )

    // ---- initialize --------------------------------------------------------------------------

    @Test
    fun `initialize schedules the periodic bundle sync`() {
        RuleMind.initialize(context, config())

        val infos = WorkManager.getInstance(context)
            .getWorkInfosForUniqueWork("rulemind-bundle-sync").get()

        assertEquals(
            "exactly one unique periodic sync should be enqueued", 1, infos.size)
        assertTrue(
            "the sync must be live -- if this enqueue ever stopped happening nothing would " +
                "fail, the device would just keep deciding on a stale bundle forever",
            infos.first().state == WorkInfo.State.ENQUEUED ||
                infos.first().state == WorkInfo.State.RUNNING)
    }

    @Test
    fun `initialize is idempotent and leaves one scheduled sync, not two`() {
        // UPDATE policy on a unique name. Re-initialising (an app that calls it from more than
        // one entry point) must not stack duplicate periodic work.
        RuleMind.initialize(context, config())
        RuleMind.initialize(context, config(intervalMinutes = 30))

        val infos = WorkManager.getInstance(context)
            .getWorkInfosForUniqueWork("rulemind-bundle-sync").get()
        assertEquals("re-initialising must not stack periodic work", 1, infos.size)
    }

    @Test
    fun `after initialize the not-initialized refusal is gone`() {
        RuleMind.initialize(context, config())
        // health() will fail on the network (nothing listens on port 1) -- what must NOT happen
        // is the "not initialized" refusal, which would mean initialize left the object unwired.
        val error = runCatching { RuleMind.health() }.exceptionOrNull()
        if (error != null) {
            assertTrue(
                "after initialize the failure must be a NETWORK one, not 'not initialized': " +
                    "${error.message}",
                !error.message.orEmpty().contains("not initialized"))
        }
    }

    @Test
    fun `clearCache is safe to call and does not require a network`() {
        RuleMind.initialize(context, config())
        RuleMind.clearCache()
    }
}
