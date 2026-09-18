package com.rulemind.android

import android.content.Context
import androidx.test.core.app.ApplicationProvider
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import javax.crypto.KeyGenerator
import javax.crypto.SecretKey

/**
 * `SecurePrefs` replaced the deprecated `EncryptedSharedPreferences` in September 2026, and until
 * this file it was verified BY COMPILATION ONLY -- this module had no test source set at all.
 *
 * ROBOLECTRIC CANNOT BACK THE ANDROIDKEYSTORE -- it answers `NoSuchAlgorithmException` for that
 * provider, which is how these tests first failed. So `SecurePrefs` takes its key from an
 * injectable provider that DEFAULTS to the keystore, and this file passes an ordinary AES key.
 *
 * Be exact about what that covers and what it does not:
 *
 *   covered here  the IV framing, the base64 encoding, the SHA-256 key-name hashing, the
 *                 batched editor, store isolation, and the contract that an unreadable value
 *                 reads as ABSENT rather than throwing
 *   NOT covered   that the key is generated inside the keystore and is non-exportable. That is
 *                 the property the class exists for, it is a platform guarantee, and no JVM
 *                 test can demonstrate it -- it needs a device (issue #133).
 */
@RunWith(RobolectricTestRunner::class)
class SecurePrefsTest {

    /** One key for the whole test, mirroring the single keystore key in production. */
    private val key: SecretKey =
        KeyGenerator.getInstance("AES").apply { init(256) }.generateKey()

    private fun prefs(name: String = "test.store") =
        SecurePrefs(ApplicationProvider.getApplicationContext<Context>(), name) { key }

    @Test
    fun `a value round-trips`() {
        val store = prefs()
        store.putString("k", "hello")
        assertEquals("hello", store.getString("k", null))
    }

    @Test
    fun `an absent key returns the default`() {
        assertEquals("fallback", prefs().getString("never-written", "fallback"))
    }

    @Test
    fun `the stored form is not the plaintext`() {
        val store = prefs("ciphertext.store")
        store.putString("secret", "SENSITIVE-VALUE")
        val raw = ApplicationProvider.getApplicationContext<Context>()
            .getSharedPreferences("ciphertext.store", Context.MODE_PRIVATE)
            .all
        assertTrue("something should be stored", raw.isNotEmpty())
        for ((key, value) in raw) {
            assertNotEquals("the KEY NAME was stored in the clear", "secret", key)
            assertTrue("the VALUE was stored in the clear",
                value !is String || !value.contains("SENSITIVE-VALUE"))
        }
    }

    @Test
    fun `two writes of the same value produce different ciphertext`() {
        // GCM must never reuse an IV with one key. The cipher picks it and it is stored
        // alongside, so identical plaintext must not encrypt identically -- if it does, the IV
        // is fixed and the mode's guarantee is gone.
        val store = prefs("iv.store")
        store.putString("a", "same")
        val first = ApplicationProvider.getApplicationContext<Context>()
            .getSharedPreferences("iv.store", Context.MODE_PRIVATE).all.values.first()
        store.putString("a", "same")
        val second = ApplicationProvider.getApplicationContext<Context>()
            .getSharedPreferences("iv.store", Context.MODE_PRIVATE).all.values.first()
        assertNotEquals("identical plaintext encrypted identically -- the IV is not random",
                        first, second)
        assertEquals("same", store.getString("a", null))
    }

    @Test
    fun `an unreadable value reads as absent rather than throwing`() {
        // The contract that keeps a changed device PIN from taking down SDK init: the keystore
        // key is dropped when the user adds or removes a lock, and every value becomes garbage.
        val context = ApplicationProvider.getApplicationContext<Context>()
        val store = prefs("corrupt.store")
        store.putString("k", "value")
        val storedKey = context.getSharedPreferences("corrupt.store", Context.MODE_PRIVATE)
            .all.keys.first()
        context.getSharedPreferences("corrupt.store", Context.MODE_PRIVATE)
            .edit().putString(storedKey, "not-base64-or-ciphertext").apply()
        assertNull("a corrupt value must read as absent", store.getString("k", null))
        assertEquals("default", store.getString("k", "default"))
    }

    @Test
    fun `edit writes several keys and remove deletes one`() {
        val store = prefs("batch.store")
        store.edit {
            putString("a", "1")
            putString("b", "2")
            putInt("n", 7)
        }
        assertEquals("1", store.getString("a", null))
        assertEquals("2", store.getString("b", null))
        assertEquals(7, store.getInt("n", 0))
        store.edit { remove("a") }
        assertNull(store.getString("a", null))
        assertEquals("2", store.getString("b", null))
    }

    @Test
    fun `clear empties the store`() {
        val store = prefs("clear.store")
        store.edit { putString("a", "1") }
        store.edit { clear() }
        assertNull(store.getString("a", null))
    }

    @Test
    fun `an int that was never written returns the default`() {
        assertEquals(42, prefs("int.store").getInt("absent", 42))
    }

    @Test
    fun `two stores with different names do not share values`() {
        val one = prefs("store.one")
        val two = prefs("store.two")
        one.putString("k", "from-one")
        assertNull("stores must be isolated by name", two.getString("k", null))
    }
}
