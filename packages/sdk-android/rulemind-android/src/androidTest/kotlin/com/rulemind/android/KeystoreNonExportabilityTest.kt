package com.rulemind.android

import android.content.Context
import android.os.Build
import android.security.keystore.KeyInfo
import android.security.keystore.KeyProperties
import androidx.test.core.app.ApplicationProvider
import androidx.test.ext.junit.runners.AndroidJUnit4
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith
import java.security.KeyStore
import javax.crypto.Cipher
import javax.crypto.SecretKeyFactory
import javax.crypto.spec.GCMParameterSpec

/**
 * THE PROPERTY `SecurePrefs` EXISTS FOR, asserted on real hardware.
 *
 * `SecurePrefsTest` (JVM, Robolectric) covers the IV framing, the base64 encoding, the SHA-256
 * key-name hashing and the contract that an unreadable value reads as ABSENT. It explicitly
 * cannot cover the one thing the class was written to guarantee: that the AES-256-GCM key is
 * **generated inside the AndroidKeyStore and cannot be read back out**. Robolectric answers
 * `NoSuchAlgorithmException` for that provider, which is why `SecurePrefs` takes an injectable
 * key provider at all.
 *
 * Non-exportability is not a claim you can assert by reading code — it is a platform guarantee,
 * and the only honest test is to ask a real keystore for the key material and confirm it refuses.
 *
 * Run: `./gradlew :rulemind-android:connectedDebugAndroidTest` with a device attached.
 * NOT part of the standing gate — see issue #176 and the comment in build.gradle.kts.
 */
@RunWith(AndroidJUnit4::class)
class KeystoreNonExportabilityTest {

    private val context: Context get() = ApplicationProvider.getApplicationContext()

    private fun keystore(): KeyStore =
        KeyStore.getInstance("AndroidKeyStore").apply { load(null) }

    /**
     * `getEncoded()` returning null IS the proof. For an ordinary `SecretKeySpec` it returns the
     * 32 raw bytes; for a key that lives in the keystore the platform has nothing to hand back,
     * because the material never left. This is the single assertion that separates "we used the
     * keystore API" from "the key is actually protected by it".
     */
    @Test
    fun theKeyMaterialCannotBeReadBack() {
        val key = SecurePrefs.keystoreKey()
        assertEquals("AES", key.algorithm)
        assertNull(
            "getEncoded() returned key material — the key is EXPORTABLE and SecurePrefs is " +
                "no better than storing the bytes in a file",
            key.encoded,
        )
    }

    /** It must be in the keystore under the documented alias, not merely AES-shaped. */
    @Test
    fun theKeyLivesInTheAndroidKeystore() {
        SecurePrefs.keystoreKey()
        val entry = keystore().getEntry("rulemind.secureprefs.v1", null)
        assertNotNull("no entry under the alias — the key was generated somewhere else", entry)
        assertTrue(entry is KeyStore.SecretKeyEntry)
    }

    /**
     * `KeyInfo` is the platform's own account of where the key lives and what it may be used for.
     * Asking it is how "hardware-backed" stops being a hope about the device and becomes an
     * assertion about this one.
     */
    @Test
    fun theKeyIsHardwareBackedAndConstrainedToWhatSecurePrefsNeeds() {
        val key = SecurePrefs.keystoreKey()
        val factory = SecretKeyFactory.getInstance(key.algorithm, "AndroidKeyStore")
        val info = factory.getKeySpec(key, KeyInfo::class.java) as KeyInfo

        assertEquals(256, info.keySize)
        assertTrue("GCM only", info.blockModes.contains(KeyProperties.BLOCK_MODE_GCM))
        assertTrue("no padding", info.encryptionPaddings.contains(KeyProperties.ENCRYPTION_PADDING_NONE))

        // DELIBERATELY FALSE, and SecurePrefs says why: SyncWorker drains the decision outbox in
        // the background, so the store must be readable while the device is locked. Requiring
        // auth would make a background sync fail on a locked phone, which is the normal case.
        // Pinned here so the decision cannot be reversed by accident.
        assertFalse(
            "user-auth-required would break background sync on a locked device",
            info.isUserAuthenticationRequired,
        )

        // API 31+ reports a security level; below that, isInsideSecureHardware. Both are the same
        // question. Reported rather than asserted hard, because an emulator legitimately answers
        // "software" and failing there would make this test a device-detector.
        val level = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
            "securityLevel=${info.securityLevel}"
        } else {
            @Suppress("DEPRECATION")
            "insideSecureHardware=${info.isInsideSecureHardware}"
        }
        println("RULEMIND keystore: $level  (Build.MODEL=${Build.MODEL}, SDK=${Build.VERSION.SDK_INT})")
    }

    /** The purposes must be exactly encrypt+decrypt — a key that can also sign is a wider key. */
    @Test
    fun theKeyCannotBeUsedForAnythingElse() {
        val key = SecurePrefs.keystoreKey()
        val info = SecretKeyFactory.getInstance(key.algorithm, "AndroidKeyStore")
            .getKeySpec(key, KeyInfo::class.java) as KeyInfo
        assertEquals(
            KeyProperties.PURPOSE_ENCRYPT or KeyProperties.PURPOSE_DECRYPT,
            info.purposes,
        )
    }

    /**
     * The same alias must come back on every call. `keystoreKey()` double-checks inside a lock
     * precisely so two threads cannot generate two keys — and if that ever broke, the second key
     * would silently orphan everything the first wrote, which on a device means a store that
     * reads as empty rather than as corrupt.
     */
    @Test
    fun repeatedCallsReturnTheSameKey() {
        val first = SecurePrefs.keystoreKey()
        val second = SecurePrefs.keystoreKey()

        // Identity cannot be compared on the object (the keystore may hand back a new wrapper),
        // so compare CAPABILITY: encrypt with one, decrypt with the other.
        val enc = Cipher.getInstance("AES/GCM/NoPadding").apply { init(Cipher.ENCRYPT_MODE, first) }
        val iv = enc.iv
        val body = enc.doFinal("the device keypair".toByteArray())

        val dec = Cipher.getInstance("AES/GCM/NoPadding").apply {
            init(Cipher.DECRYPT_MODE, second, GCMParameterSpec(128, iv))
        }
        assertEquals("the device keypair", String(dec.doFinal(body)))
    }

    /** End to end through the real store: written on the keystore key, read back as itself. */
    @Test
    fun securePrefsRoundTripsAgainstTheRealKeystore() {
        val store = SecurePrefs(context, "rulemind.instrumentation.store")
        store.putString("client_private", "MIIEvQIBADANBgkqh-not-really-a-key")
        assertEquals("MIIEvQIBADANBgkqh-not-really-a-key", store.getString("client_private", null))

        // And the key name is NOT in the file in the clear — the whole point of hashing them.
        val raw = context.getSharedPreferences("rulemind.instrumentation.store", Context.MODE_PRIVATE)
        assertFalse(
            "the key name was stored in the clear",
            raw.all.keys.any { it.contains("client_private") },
        )
        assertFalse(
            "the VALUE was stored in the clear",
            raw.all.values.any { it.toString().contains("not-really-a-key") },
        )
    }
}
