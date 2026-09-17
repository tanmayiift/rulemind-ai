package com.rulemind.android

import android.content.Context
import android.content.SharedPreferences
import android.security.keystore.KeyGenParameterSpec
import android.security.keystore.KeyProperties
import android.util.Base64
import java.security.KeyStore
import java.security.MessageDigest
import javax.crypto.Cipher
import javax.crypto.KeyGenerator
import javax.crypto.SecretKey
import javax.crypto.spec.GCMParameterSpec

/**
 * Encrypted key/value storage backed directly by the Android Keystore.
 *
 * WHY THIS EXISTS. Both stores in this module used `EncryptedSharedPreferences`, which
 * **androidx.security:security-crypto 1.1.0 deprecates** -- Google has deprecated the Jetpack
 * Security crypto library as a whole, so there is no newer version to move to. Upgrading off the
 * 1.1.0-alpha06 this SDK shipped on (an alpha, in a released SDK) surfaced 5 deprecation warnings
 * where there had been 0, which is the library telling you it is going away.
 *
 * This does what EncryptedSharedPreferences did, without the wrapper: one AES-256-GCM key held in
 * the AndroidKeyStore, used to encrypt each value. The key is generated inside the keystore and is
 * **not exportable** -- the app can use it but cannot read it, which is the property that made the
 * Jetpack wrapper worth using in the first place.
 *
 * KEY NAMES ARE HASHED, not stored in the clear. EncryptedSharedPreferences encrypted them
 * deterministically (AES-SIV) so that lookup still worked; SHA-256 gives the same lookup property
 * without another cipher. It matters here: `ExecutionStore` keys on execution ids.
 *
 * NOT `user-authentication-required`: this store must be readable while the device is locked,
 * because `SyncWorker` drains the decision outbox in the background. Requiring auth would make a
 * background sync fail on a locked phone, which is the normal case rather than the exception.
 */
internal class SecurePrefs(context: Context, name: String) {

    private val prefs: SharedPreferences =
        context.applicationContext.getSharedPreferences(name, Context.MODE_PRIVATE)

    fun getString(key: String, defaultValue: String?): String? {
        val stored = prefs.getString(hash(key), null) ?: return defaultValue
        return decrypt(stored) ?: defaultValue
    }

    fun putString(key: String, value: String) {
        prefs.edit().putString(hash(key), encrypt(value)).apply()
    }

    fun getInt(key: String, defaultValue: Int): Int =
        getString(key, null)?.toIntOrNull() ?: defaultValue

    /** Batched, so a multi-key update is one commit rather than several. */
    fun edit(block: Editor.() -> Unit) {
        val editor = Editor(prefs.edit())
        editor.block()
        editor.apply()
    }

    inner class Editor(private val delegate: SharedPreferences.Editor) {
        fun putString(key: String, value: String): Editor {
            delegate.putString(hash(key), encrypt(value)); return this
        }

        /** Ints go through the same encryption as strings -- a stored bundle VERSION is as much
         *  a fact about the device as the bundle itself, and leaving it in the clear would be an
         *  odd exception. */
        fun putInt(key: String, value: Int): Editor {
            delegate.putString(hash(key), encrypt(value.toString())); return this
        }

        fun remove(key: String): Editor {
            delegate.remove(hash(key)); return this
        }

        fun clear(): Editor {
            delegate.clear(); return this
        }

        internal fun apply() = delegate.apply()
    }

    private fun encrypt(plaintext: String): String {
        val cipher = Cipher.getInstance(TRANSFORMATION)
        cipher.init(Cipher.ENCRYPT_MODE, secretKey())
        val ciphertext = cipher.doFinal(plaintext.toByteArray(Charsets.UTF_8))
        // IV first, then ciphertext. GCM generates the IV, and it must never be reused with the
        // same key -- letting the cipher pick it and storing it alongside is the safe form.
        return Base64.encodeToString(cipher.iv + ciphertext, Base64.NO_WRAP)
    }

    /**
     * Returns null rather than throwing on anything unreadable.
     *
     * A value can legitimately become undecryptable: the keystore key is dropped when the user
     * adds or removes a device lock, and on some OEM images after a restore-from-backup. The
     * caller's contract is "absent", not "crash" -- an execution that cannot be read is an
     * execution that gets re-fetched, whereas an exception here would take down the SDK's
     * initialisation on a phone whose owner merely changed their PIN.
     */
    private fun decrypt(stored: String): String? = runCatching {
        val raw = Base64.decode(stored, Base64.NO_WRAP)
        if (raw.size <= IV_BYTES) return null
        val cipher = Cipher.getInstance(TRANSFORMATION)
        cipher.init(
            Cipher.DECRYPT_MODE,
            secretKey(),
            GCMParameterSpec(TAG_BITS, raw.copyOfRange(0, IV_BYTES)),
        )
        cipher.doFinal(raw.copyOfRange(IV_BYTES, raw.size)).toString(Charsets.UTF_8)
    }.getOrNull()

    private fun secretKey(): SecretKey {
        val keyStore = KeyStore.getInstance(ANDROID_KEYSTORE).apply { load(null) }
        (keyStore.getEntry(KEY_ALIAS, null) as? KeyStore.SecretKeyEntry)?.let { return it.secretKey }
        synchronized(SecurePrefs::class.java) {
            // Re-check inside the lock: two threads reaching this together would otherwise
            // generate two keys, and the second would silently orphan everything the first wrote.
            val reloaded = KeyStore.getInstance(ANDROID_KEYSTORE).apply { load(null) }
            (reloaded.getEntry(KEY_ALIAS, null) as? KeyStore.SecretKeyEntry)
                ?.let { return it.secretKey }
            val generator = KeyGenerator.getInstance(KeyProperties.KEY_ALGORITHM_AES, ANDROID_KEYSTORE)
            generator.init(
                KeyGenParameterSpec.Builder(
                    KEY_ALIAS,
                    KeyProperties.PURPOSE_ENCRYPT or KeyProperties.PURPOSE_DECRYPT,
                )
                    .setBlockModes(KeyProperties.BLOCK_MODE_GCM)
                    .setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE)
                    .setKeySize(256)
                    .build()
            )
            return generator.generateKey()
        }
    }

    private fun hash(key: String): String =
        Base64.encodeToString(
            MessageDigest.getInstance("SHA-256").digest(key.toByteArray(Charsets.UTF_8)),
            Base64.NO_WRAP or Base64.URL_SAFE,
        )

    companion object {
        private const val ANDROID_KEYSTORE = "AndroidKeyStore"
        private const val KEY_ALIAS = "rulemind.secureprefs.v1"
        private const val TRANSFORMATION = "AES/GCM/NoPadding"
        private const val IV_BYTES = 12
        private const val TAG_BITS = 128
    }
}
