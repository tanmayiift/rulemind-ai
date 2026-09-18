package com.rulemind.android

import androidx.test.core.app.ApplicationProvider
import android.content.Context
import org.junit.Assert.assertNotNull
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner

/** Proves the harness itself works before anything depends on it. */
@RunWith(RobolectricTestRunner::class)
class SmokeTest {
    @Test
    fun `robolectric provides an application context`() {
        assertNotNull(ApplicationProvider.getApplicationContext<Context>())
    }
}
