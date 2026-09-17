@file:Suppress("DEPRECATION")

package com.rulemind.android

import android.content.Context
import androidx.security.crypto.EncryptedSharedPreferences
import androidx.security.crypto.MasterKey

/**
 * One-time move of the two on-device stores off `EncryptedSharedPreferences`.
 *
 * **This file is the ONLY remaining use of androidx.security:security-crypto, and it exists to be
 * deleted.** Everything else now goes through [SecurePrefs], which talks to the Android Keystore
 * directly. Keeping the deprecated read path for one release is the difference between "upgraded"
 * and "upgraded, and threw away every existing install's data".
 *
 * WHY IT MATTERS MOST FOR THE KEYPAIR. `CryptoManager` stores the device's RSA private key here.
 * The SERVER holds the matching public key, so silently regenerating it does not fail loudly --
 * it produces a device that can no longer decrypt any bundle addressed to it, and the symptom
 * appears later and somewhere else. An execution record lost from `ExecutionStore` is a resumable
 * execution re-fetched; a lost keypair is a device that has to re-enrol.
 *
 * Safe to run on every start: it is a no-op once the marker is set, and it never deletes the
 * legacy store. Leaving the old data in place costs a few KB and means a user who downgrades the
 * app is not stranded.
 *
 * DELETE THIS FILE, and the `androidx.security:security-crypto` dependency with it, one release
 * after 4.1.0 -- by then every install that is going to migrate has.
 */
internal object LegacySecureStoreMigration {

    fun migrateIfNeeded(context: Context, storeName: String, target: SecurePrefs) {
        val marker = context.applicationContext
            .getSharedPreferences(MARKER_STORE, Context.MODE_PRIVATE)
        if (marker.getBoolean(markerKey(storeName), false)) return

        // Every failure mode here is non-fatal by design. A legacy store can be unreadable for
        // reasons that are not defects -- the master key is dropped when the user changes their
        // device lock, and some OEM restore paths leave the file without the key. In all of them
        // the right behaviour is "there was nothing to migrate", not a crash on SDK init.
        runCatching {
            val legacy = EncryptedSharedPreferences.create(
                context.applicationContext,
                storeName,
                MasterKey.Builder(context.applicationContext)
                    .setKeyScheme(MasterKey.KeyScheme.AES256_GCM)
                    .build(),
                EncryptedSharedPreferences.PrefKeyEncryptionScheme.AES256_SIV,
                EncryptedSharedPreferences.PrefValueEncryptionScheme.AES256_GCM,
            )
            // `.all` on an EncryptedSharedPreferences returns DECRYPTED entries, so this copies
            // both stores without either one enumerating its own keys. ExecutionStore needs that:
            // its keys are `execution.<id>`, so a caller-supplied list could only be built by
            // reading the index first and would miss anything the index had drifted from.
            target.edit {
                for ((key, value) in legacy.all) {
                    if (value is String) putString(key, value)
                }
            }
        }

        // Marked even when the read failed. A store that cannot be decrypted this time will not
        // decrypt next time either, and retrying on every start turns a one-off into a permanent
        // cost on the startup path.
        marker.edit().putBoolean(markerKey(storeName), true).apply()
    }

    private fun markerKey(storeName: String) = "migrated.$storeName"

    private const val MARKER_STORE = "rulemind.migration.markers"
}
