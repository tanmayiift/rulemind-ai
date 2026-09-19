package com.rulemind.android

import android.content.Context
import androidx.test.core.app.ApplicationProvider
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import javax.crypto.KeyGenerator
import javax.crypto.SecretKey

/**
 * BE PRECISE ABOUT WHAT THIS FILE CAN AND CANNOT PROVE.
 *
 * `LegacySecureStoreMigration` reads the deprecated `EncryptedSharedPreferences`, which requires
 * a real AndroidKeyStore. Robolectric answers `NoSuchAlgorithmException` for that provider, so
 * `EncryptedSharedPreferences.create` throws here — which means:
 *
 *   NOT covered   the happy path. That a populated legacy store is actually COPIED into
 *                 [SecurePrefs] needs a device or an emulator (issue #133), and no assertion in
 *                 this file should be read as covering it.
 *
 *   covered here  the FAILURE path, in full — and that is not a consolation prize. The class's
 *                 own comment says an unreadable legacy store is the common production case, not
 *                 the exotic one: the master key is dropped when a user changes their device
 *                 lock, and some OEM restore paths leave the file without its key. Robolectric
 *                 reproduces exactly that state for free.
 *
 * The contracts below are all about what happens when the read fails, and every one of them is
 * load-bearing on the SDK's startup path: this runs inside `CryptoManager`'s constructor, so a
 * throw here is a crash on `RuleMind.initialize`.
 */
@RunWith(RobolectricTestRunner::class)
class LegacySecureStoreMigrationTest {

    private val context: Context get() = ApplicationProvider.getApplicationContext()

    private val storeKey: SecretKey =
        KeyGenerator.getInstance("AES").apply { init(256) }.generateKey()

    private fun target(name: String) = SecurePrefs(context, name) { storeKey }

    private fun markerFor(storeName: String): Boolean =
        context.getSharedPreferences("rulemind.migration.markers", Context.MODE_PRIVATE)
            .getBoolean("migrated.$storeName", false)

    /**
     * THE contract. This runs from `CryptoManager`'s constructor, which runs from
     * `RuleMind.initialize`. A propagated exception is an SDK that cannot start on any device
     * whose user changed their screen lock.
     */
    @Test
    fun `an unreadable legacy store does not throw`() {
        LegacySecureStoreMigration.migrateIfNeeded(context, "rulemind.crypto.store", target("t1"))
    }

    /**
     * Marked even though the read failed — deliberately. A store that will not decrypt now will
     * not decrypt next time either, and retrying on every start turns a one-off into a permanent
     * cost on the startup path. This is the assertion that pins that decision.
     */
    @Test
    fun `a failed read still sets the marker`() {
        assertFalse(markerFor("store.a"))
        LegacySecureStoreMigration.migrateIfNeeded(context, "store.a", target("t2"))
        assertTrue(
            "without this the migration re-attempts on every SDK init, forever",
            markerFor("store.a"),
        )
    }

    /**
     * Once marked it must not touch the target again. A second pass over a legacy store would
     * overwrite values [SecurePrefs] has since accepted — for `CryptoManager` that is the device
     * keypair, so "run the migration twice" and "re-enrol the device" would be the same thing.
     */
    @Test
    fun `a marked store is a no-op and leaves the target alone`() {
        val store = target("t3")
        LegacySecureStoreMigration.migrateIfNeeded(context, "store.b", store)

        store.putString("client_private", "the key this device already has")
        LegacySecureStoreMigration.migrateIfNeeded(context, "store.b", store)

        assertEquals("the key this device already has", store.getString("client_private", null))
    }

    /** Markers are per store name. Migrating one must not silently skip the other. */
    @Test
    fun `markers are independent per store`() {
        LegacySecureStoreMigration.migrateIfNeeded(context, "rulemind.crypto.store", target("t4"))
        assertTrue(markerFor("rulemind.crypto.store"))
        assertFalse(
            "ExecutionStore's migration must not be skipped because CryptoManager's ran",
            markerFor("rulemind.execution.store"),
        )
    }

    /** A failed read must not fabricate entries in the target. */
    @Test
    fun `a failed read writes nothing to the target`() {
        val store = target("t5")
        LegacySecureStoreMigration.migrateIfNeeded(context, "store.c", store)
        assertNull(store.getString("client_private", null))
        assertNull(store.getString("client_public", null))
    }
}
