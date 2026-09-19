package com.rulemind.android

import android.content.Context
import androidx.test.core.app.ApplicationProvider
import androidx.work.ListenableWorker
import androidx.work.NetworkType
import androidx.work.WorkInfo
import androidx.work.WorkManager
import androidx.work.testing.TestListenableWorkerBuilder
import androidx.work.testing.WorkManagerTestInitHelper
import com.rulemind.core.models.RuleMindConfig
import kotlinx.coroutines.runBlocking
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import java.util.concurrent.TimeUnit

/**
 * `SyncWorker` is how a device ever learns about a policy change. It was the last source file in
 * this module with no test of its own.
 *
 * The contract that matters is one line of it — `.getOrElse { Result.retry() }`. WorkManager
 * treats `Result.failure()` as terminal: it drops the work and never runs it again. So if that
 * `retry` ever became `failure`, a single transient sync error would permanently stop a device
 * receiving bundles, and NOTHING would report it. The device keeps deciding, confidently, on
 * whatever it last had. That is the same silent-staleness failure [RuleMindTest] guards the
 * enqueue against, one layer down.
 *
 * `doWork` also calls three things in sequence, and the `runCatching` wraps all three: bundle
 * sync, event flush, and the durable decision outbox drain. A throw from any of them must still
 * produce a retry rather than escaping the worker.
 */
@RunWith(RobolectricTestRunner::class)
class SyncWorkerTest {

    private val context: Context get() = ApplicationProvider.getApplicationContext()

    @Before
    fun setUp() {
        WorkManagerTestInitHelper.initializeTestWorkManager(context)
        RuleMind.resetForTesting()
    }

    @After
    fun tearDown() = RuleMind.resetForTesting()

    private fun worker() =
        TestListenableWorkerBuilder<SyncWorker>(context).build()

    // ---- the retry contract -------------------------------------------------------------------

    /**
     * UNINITIALISED is the harshest case: `RuleMind.syncNow()` throws immediately. The worker
     * must still come back with retry, not failure, and must not let the exception escape.
     */
    @Test
    fun `a throwing sync retries rather than failing permanently`() = runBlocking {
        val result = worker().doWork()
        assertEquals(
            "Result.failure() would make WorkManager drop this work forever, and the device " +
                "would silently stop receiving policy updates",
            ListenableWorker.Result.retry(),
            result,
        )
    }

    /**
     * Initialised but unreachable — port 1, nothing listening. This is the ordinary transient
     * case (offline, server down, DNS), and it is the one that must not be terminal.
     */
    @Test
    fun `an unreachable server retries`() = runBlocking {
        RuleMind.initialize(
            context,
            // Port 1 on purpose: nothing listens, so this fails fast instead of reaching anything.
            RuleMindConfig(baseUrl = "http://127.0.0.1:1", apiKey = "test-key"),
        )
        assertEquals(ListenableWorker.Result.retry(), worker().doWork())
    }

    /** Whatever happens, the worker must not throw out of `doWork`. */
    @Test
    fun `doWork never throws`() = runBlocking<Unit> {
        // Both states, because they fail at different points in the three-call sequence.
        worker().doWork()
        RuleMind.initialize(context, RuleMindConfig(baseUrl = "http://127.0.0.1:1", apiKey = "k"))
        worker().doWork()
    }

    // ---- scheduling ---------------------------------------------------------------------------

    @Test
    fun `schedule enqueues one periodic sync that requires a network`() {
        SyncWorker.schedule(context)

        val infos = WorkManager.getInstance(context)
            .getWorkInfosForUniqueWork("rulemind-bundle-sync").get()

        assertEquals(1, infos.size)
        assertTrue(
            infos.first().state == WorkInfo.State.ENQUEUED ||
                infos.first().state == WorkInfo.State.RUNNING,
        )
        assertEquals(
            "without CONNECTED the worker wakes with no network, fails, and burns a retry",
            NetworkType.CONNECTED,
            infos.first().constraints.requiredNetworkType,
        )
    }

    /**
     * `ExistingPeriodicWorkPolicy.UPDATE`, asserted rather than assumed — and asserted on the
     * INTERVAL, not just the count.
     *
     * The count alone does not distinguish UPDATE from KEEP: the unique-work name means both
     * leave exactly one request enqueued. Under KEEP the second call is silently discarded, so
     * a config change to `bundleSyncIntervalMinutes` would never reach the device and every
     * outward sign — one enqueued sync, correct name, right constraints — would look right.
     *
     * A first version of this test checked only `infos.size == 1`, and the KEEP mutation
     * survived it. That is the whole reason the interval assertion is here.
     */
    @Test
    fun `scheduling twice replaces rather than stacks`() {
        SyncWorker.schedule(context, intervalMinutes = 15)
        SyncWorker.schedule(context, intervalMinutes = 30)

        val infos = WorkManager.getInstance(context)
            .getWorkInfosForUniqueWork("rulemind-bundle-sync").get()
            .filter { it.state != WorkInfo.State.CANCELLED }

        assertEquals("a second schedule must not add a second periodic sync", 1, infos.size)
        assertEquals(
            "the second schedule's interval must take effect; under KEEP it is discarded and " +
                "the device keeps syncing on the old cadence with nothing to show for it",
            TimeUnit.MINUTES.toMillis(30),
            infos.first().periodicityInfo?.repeatIntervalMillis,
        )
    }
}
