@file:Suppress("DEPRECATION")

package com.rulemind.android

import android.content.Context
import androidx.security.crypto.EncryptedSharedPreferences
import androidx.security.crypto.MasterKey
import androidx.test.core.app.ApplicationProvider
import androidx.test.ext.junit.runners.AndroidJUnit4
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith

/**
 * THE MIGRATION'S HAPPY PATH, which only exists on a device.
 *
 * `LegacySecureStoreMigrationTest` (JVM) covers the FAILURE path in full — and that is the common
 * production case, not the exotic one. What it cannot cover is the case the class was written
 * for: a **populated** `EncryptedSharedPreferences` being copied into [SecurePrefs].
 * `EncryptedSharedPreferences.create` needs a real AndroidKeyStore, so on the JVM it throws
 * before the copy is ever attempted.
 *
 * WHY THIS IS THE TEST WORTH HAVING. `CryptoManager` keeps the device's RSA private key in this
 * store and the server already holds the matching public half. A migration that silently copies
 * nothing does not fail loudly — it produces a device that can no longer decrypt any bundle
 * addressed to it, and the symptom appears later and somewhere else. "It compiled and the
 * failure path is covered" is not evidence that the copy works.
 *
 * Run: `./gradlew :rulemind-android:connectedDebugAndroidTest` with a device attached.
 */
@RunWith(AndroidJUnit4::class)
class LegacyMigrationInstrumentationTest {

    private val context: Context get() = ApplicationProvider.getApplicationContext()

    /** Each test gets its own store names, so a rerun on the same device starts clean. */
    private val suffix = System.nanoTime().toString()

    @Before
    fun clearMarkers() {
        // The migration is marker-gated and markers persist on the device across runs, so without
        // this the second execution of this file would be a no-op that "passed".
        context.getSharedPreferences("rulemind.migration.markers", Context.MODE_PRIVATE)
            .edit().clear().commit()
    }

    private fun legacyStore(name: String) = EncryptedSharedPreferences.create(
        context,
        name,
        MasterKey.Builder(context).setKeyScheme(MasterKey.KeyScheme.AES256_GCM).build(),
        EncryptedSharedPreferences.PrefKeyEncryptionScheme.AES256_SIV,
        EncryptedSharedPreferences.PrefValueEncryptionScheme.AES256_GCM,
    )

    @Test
    fun aPopulatedLegacyStoreIsActuallyCopied() {
        val name = "legacy.copy.$suffix"
        legacyStore(name).edit()
            .putString("client_public", "PUBLIC-KEY-MATERIAL")
            .putString("client_private", "PRIVATE-KEY-MATERIAL")
            .commit()

        val target = SecurePrefs(context, "target.copy.$suffix")
        assertNull("precondition: the target starts empty", target.getString("client_private", null))

        LegacySecureStoreMigration.migrateIfNeeded(context, name, target)

        assertEquals("PRIVATE-KEY-MATERIAL", target.getString("client_private", null))
        assertEquals("PUBLIC-KEY-MATERIAL", target.getString("client_public", null))
    }

    /**
     * `ExecutionStore` keys on execution ids (`execution.<id>`), so the copy has to enumerate the
     * legacy store itself rather than take a caller-supplied list — a list could only be built
     * from an index, and would miss anything the index had drifted from. `.all` on an
     * EncryptedSharedPreferences returns DECRYPTED entries, which is what makes that possible.
     */
    @Test
    fun everyKeyIsCopiedWithoutEnumeratingThemAtTheCallSite() {
        val name = "legacy.many.$suffix"
        val editor = legacyStore(name).edit()
        repeat(25) { editor.putString("execution.$it", "payload-$it") }
        editor.commit()

        val target = SecurePrefs(context, "target.many.$suffix")
        LegacySecureStoreMigration.migrateIfNeeded(context, name, target)

        repeat(25) { assertEquals("payload-$it", target.getString("execution.$it", null)) }
    }

    /**
     * IT MUST NOT DELETE THE LEGACY STORE. Leaving the old data costs a few KB and means a user
     * who downgrades the app is not stranded — the class says so and nothing checked it.
     */
    @Test
    fun theLegacyStoreSurvivesTheMigration() {
        val name = "legacy.kept.$suffix"
        legacyStore(name).edit().putString("client_private", "KEEP-ME").commit()

        LegacySecureStoreMigration.migrateIfNeeded(context, name, SecurePrefs(context, "target.kept.$suffix"))

        assertEquals(
            "the legacy store was cleared — a downgrade would strand this device",
            "KEEP-ME",
            legacyStore(name).getString("client_private", null),
        )
    }

    /**
     * Once migrated, a SECOND pass must not overwrite what SecurePrefs has since accepted. For
     * `CryptoManager` that value is the device keypair, so "run the migration twice" and
     * "re-enrol the device" would otherwise be the same thing.
     */
    @Test
    fun aSecondPassDoesNotOverwriteNewerData() {
        val name = "legacy.once.$suffix"
        legacyStore(name).edit().putString("client_private", "OLD").commit()

        val target = SecurePrefs(context, "target.once.$suffix")
        LegacySecureStoreMigration.migrateIfNeeded(context, name, target)
        assertEquals("OLD", target.getString("client_private", null))

        target.putString("client_private", "ROTATED")
        LegacySecureStoreMigration.migrateIfNeeded(context, name, target)

        assertEquals(
            "the marker did not hold — a re-migration overwrote the live key",
            "ROTATED",
            target.getString("client_private", null),
        )
    }

    /** The marker is per store name; migrating one must not silently skip the other. */
    @Test
    fun markersAreIndependentPerStore() {
        val a = "legacy.a.$suffix"
        val b = "legacy.b.$suffix"
        legacyStore(a).edit().putString("k", "A").commit()
        legacyStore(b).edit().putString("k", "B").commit()

        val ta = SecurePrefs(context, "target.a.$suffix")
        val tb = SecurePrefs(context, "target.b.$suffix")
        LegacySecureStoreMigration.migrateIfNeeded(context, a, ta)
        LegacySecureStoreMigration.migrateIfNeeded(context, b, tb)

        assertEquals("A", ta.getString("k", null))
        assertEquals("B", tb.getString("k", null))
    }

    /**
     * END TO END, the way production actually runs it, and the assertion that stands between an
     * upgrade and a fleet of devices that quietly cannot decrypt their bundles.
     *
     * NOTE THE STORE NAME. `CryptoManager` uses ONE name — `rulemind.crypto.store` — for both the
     * legacy `EncryptedSharedPreferences` and the new [SecurePrefs]. They are the same underlying
     * SharedPreferences file; the migration reads it through the old API and writes it back
     * through the new one. That is deliberate (an upgraded install keeps its file) but it has a
     * consequence the next test pins.
     */
    @Test
    fun cryptoManagerKeepsItsIdentityAcrossTheMigration() {
        val storeName = "rulemind.crypto.store"

        // A PRE-UPGRADE INSTALL: the file holds ONLY legacy entries. Clear the raw file first —
        // not just the legacy view — because a previous run of this suite, or the app itself, may
        // have left SecurePrefs entries in it, and those are what the next test is about.
        context.getSharedPreferences(storeName, Context.MODE_PRIVATE).edit().clear().commit()

        val planted = java.security.KeyPairGenerator.getInstance("RSA")
            .apply { initialize(2048) }.generateKeyPair()
        val publicB64 = android.util.Base64.encodeToString(planted.public.encoded, android.util.Base64.NO_WRAP)
        legacyStore(storeName).edit()
            .putString("client_public", publicB64)
            .putString("client_private", android.util.Base64.encodeToString(
                planted.private.encoded, android.util.Base64.NO_WRAP))
            .commit()

        // THE UPGRADE. CryptoManager's constructor runs the migration; nothing else is called.
        val crypto = CryptoManager(context, com.rulemind.core.models.RuleMindConfig(
            baseUrl = "https://example.invalid", apiKey = "k"))

        assertEquals(
            "the device's public key changed across the migration — every bundle the server " +
                "already encrypted for it is now undecryptable, and nothing would report it",
            publicB64,
            crypto.publicKeyBase64(),
        )
    }

    /**
     * A FINDING, not a contract I invented for the test. The migration writes into the SAME file
     * it reads from, so a migrated store ends up holding BOTH encodings: AES-SIV key names from
     * the legacy library and SHA-256 hashed ones from [SecurePrefs].
     * `EncryptedSharedPreferences.getAll()` decrypts every key it finds and throws
     * `SecurityException: Could not decrypt key` on the ones it did not write — so a SECOND
     * migration pass over a migrated store copies **nothing**, silently.
     *
     * In practice the marker prevents that second pass, which is exactly why the marker is
     * load-bearing rather than an optimisation. This test exists so that reasoning is written
     * down and checked: if anyone ever "simplifies" the marker away, the migration does not start
     * being slow, it starts being a no-op that reports success.
     *
     * Found by this suite on a real device — the JVM tests cannot reach it, because they never
     * get a legacy store to migrate in the first place.
     */
    @Test
    fun aMigratedStoreCanNoLongerBeReadThroughTheLegacyApi() {
        val storeName = "legacy.mixed.$suffix"
        context.getSharedPreferences(storeName, Context.MODE_PRIVATE).edit().clear().commit()
        legacyStore(storeName).edit().putString("client_private", "ORIGINAL").commit()

        // First pass: into the SAME file, the way CryptoManager does it.
        val target = SecurePrefs(context, storeName)
        LegacySecureStoreMigration.migrateIfNeeded(context, storeName, target)
        assertEquals("ORIGINAL", target.getString("client_private", null))

        // The file now holds both encodings.
        val raw = context.getSharedPreferences(storeName, Context.MODE_PRIVATE)
        assertTrue("expected legacy AND migrated entries", raw.all.size >= 2)

        // Second pass, with the marker cleared: the legacy read now fails on the hashed keys and
        // the copy is abandoned. Non-fatal by design — but a no-op, not a migration.
        context.getSharedPreferences("rulemind.migration.markers", Context.MODE_PRIVATE)
            .edit().clear().commit()
        val fresh = SecurePrefs(context, "target.mixed.$suffix")
        LegacySecureStoreMigration.migrateIfNeeded(context, storeName, fresh)
        assertNull(
            "a second pass over a migrated store copied something — if that ever starts working, " +
                "re-read this test, because the marker is what makes it safe either way",
            fresh.getString("client_private", null),
        )

        // And it must still not throw. This runs inside RuleMind.initialize.
        LegacySecureStoreMigration.migrateIfNeeded(context, storeName, fresh)
    }

    /** Values must not be readable in the raw SharedPreferences file after migration. */
    @Test
    fun migratedValuesAreStoredEncrypted() {
        val name = "legacy.enc.$suffix"
        legacyStore(name).edit().putString("client_private", "SENSITIVE-MATERIAL").commit()

        val targetName = "target.enc.$suffix"
        LegacySecureStoreMigration.migrateIfNeeded(context, name, SecurePrefs(context, targetName))

        val raw = context.getSharedPreferences(targetName, Context.MODE_PRIVATE)
        assertTrue("nothing was migrated at all", raw.all.isNotEmpty())
        assertFalse(
            "the migrated value is in the clear",
            raw.all.values.any { it.toString().contains("SENSITIVE-MATERIAL") },
        )
        assertFalse(
            "the migrated key name is in the clear",
            raw.all.keys.any { it.contains("client_private") },
        )
    }
}
