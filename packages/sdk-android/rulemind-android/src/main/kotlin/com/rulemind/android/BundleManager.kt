package com.rulemind.android

import android.content.Context
import com.rulemind.core.models.Bundle
import com.rulemind.core.models.RuleMindConfig
import java.util.concurrent.locks.ReentrantReadWriteLock
import kotlin.concurrent.read
import kotlin.concurrent.write

class BundleManager(
    context: Context,
    private val config: RuleMindConfig,
    private val networkClient: NetworkClient = NetworkClient(config),
    private val cryptoManager: CryptoManager = CryptoManager(context, config),
) {
    private val lock = ReentrantReadWriteLock()
    // See SecurePrefs: security-crypto 1.1.0 deprecates EncryptedSharedPreferences and the
    // Jetpack Security crypto library has no successor, so this talks to the Keystore directly.
    private val prefs = SecurePrefs(context, STORE_NAME).also {
        LegacySecureStoreMigration.migrateIfNeeded(context, STORE_NAME, it)
    }

    @Volatile private var bundle: Bundle? = loadPersisted()

    fun currentBundle(): Bundle? = lock.read { bundle ?: loadPersisted().also { bundle = it } }

    fun currentBundleVersion(): Int = currentBundle()?.bundleVersion ?: 0

    fun syncNow(): Bundle? = lock.write {
        val result = networkClient.fetchBundle(currentBundleVersion(), cryptoManager.publicKeyBase64())
        if (!result.changed) {
            return@write bundle
        }
        val envelope = requireNotNull(result.envelope) { "Bundle envelope missing for changed response." }
        val decrypted = cryptoManager.decryptBundle(envelope)
        require(cryptoManager.verifySignature(decrypted, envelope.signature)) { "Invalid bundle signature." }
        val parsed = BundleParser.parseBundle(decrypted)
        persist(parsed, decrypted)
        bundle = parsed
        parsed
    }

    fun clear() = lock.write {
        bundle = null
        prefs.edit { clear() }
    }

    private fun persist(bundle: Bundle, rawJson: String) {
        prefs.edit {
            putString(KEY_BUNDLE_JSON, rawJson)
            putInt(KEY_BUNDLE_VERSION, bundle.bundleVersion)
        }
    }

    private fun loadPersisted(): Bundle? {
        val rawJson = prefs.getString(KEY_BUNDLE_JSON, null) ?: return null
        return runCatching { BundleParser.parseBundle(rawJson) }.getOrNull()
    }

    companion object {
        private const val STORE_NAME = "rulemind.bundle.store"
        private const val KEY_BUNDLE_JSON = "bundle.json"
        private const val KEY_BUNDLE_VERSION = "bundle.version"
    }
}
