package com.rulemind.android

import android.content.Context
import androidx.security.crypto.EncryptedSharedPreferences
import androidx.security.crypto.MasterKey
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
    private val prefs = EncryptedSharedPreferences.create(
        context,
        "rulemind.bundle.store",
        MasterKey.Builder(context).setKeyScheme(MasterKey.KeyScheme.AES256_GCM).build(),
        EncryptedSharedPreferences.PrefKeyEncryptionScheme.AES256_SIV,
        EncryptedSharedPreferences.PrefValueEncryptionScheme.AES256_GCM,
    )

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
        prefs.edit().clear().apply()
    }

    private fun persist(bundle: Bundle, rawJson: String) {
        prefs.edit()
            .putString(KEY_BUNDLE_JSON, rawJson)
            .putInt(KEY_BUNDLE_VERSION, bundle.bundleVersion)
            .apply()
    }

    private fun loadPersisted(): Bundle? {
        val rawJson = prefs.getString(KEY_BUNDLE_JSON, null) ?: return null
        return runCatching { BundleParser.parseBundle(rawJson) }.getOrNull()
    }

    companion object {
        private const val KEY_BUNDLE_JSON = "bundle.json"
        private const val KEY_BUNDLE_VERSION = "bundle.version"
    }
}
