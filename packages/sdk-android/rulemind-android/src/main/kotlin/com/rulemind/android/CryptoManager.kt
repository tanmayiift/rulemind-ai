package com.rulemind.android

import android.content.Context
import android.util.Base64
import androidx.security.crypto.EncryptedSharedPreferences
import androidx.security.crypto.MasterKey
import com.rulemind.core.models.BundleEnvelope
import com.rulemind.core.models.RuleMindConfig
import java.security.KeyFactory
import java.security.KeyPair
import java.security.KeyPairGenerator
import java.security.Signature
import java.security.spec.PKCS8EncodedKeySpec
import java.security.spec.X509EncodedKeySpec
import javax.crypto.Cipher
import javax.crypto.SecretKey
import javax.crypto.spec.GCMParameterSpec
import javax.crypto.spec.SecretKeySpec

class CryptoManager(
    context: Context,
    private val config: RuleMindConfig,
) {
    private val applicationContext = context.applicationContext
    private val prefs = EncryptedSharedPreferences.create(
        applicationContext,
        "rulemind.crypto.store",
        MasterKey.Builder(applicationContext).setKeyScheme(MasterKey.KeyScheme.AES256_GCM).build(),
        EncryptedSharedPreferences.PrefKeyEncryptionScheme.AES256_SIV,
        EncryptedSharedPreferences.PrefValueEncryptionScheme.AES256_GCM,
    )
    @Volatile private var cachedKeyPair: KeyPair? = null

    fun publicKeyBase64(): String {
        return Base64.encodeToString(getOrCreateKeyPair().public.encoded, Base64.NO_WRAP)
    }

    fun decryptBundle(envelope: BundleEnvelope): String {
        val aesKey = decryptAesKey(envelope.encryptedKey)
        val encrypted = Base64.decode(envelope.encryptedBundle, Base64.DEFAULT)
        require(encrypted.size > 12) { "Invalid encrypted bundle payload." }
        val iv = encrypted.copyOfRange(0, 12)
        val ciphertext = encrypted.copyOfRange(12, encrypted.size)
        val cipher = Cipher.getInstance("AES/GCM/NoPadding")
        cipher.init(Cipher.DECRYPT_MODE, aesKey, GCMParameterSpec(128, iv))
        return cipher.doFinal(ciphertext).toString(Charsets.UTF_8)
    }

    fun verifySignature(payload: String, signatureBase64: String): Boolean {
        val publicKeyPem = config.serverPublicKeyPem ?: return true
        val publicKeyBytes = Base64.decode(
            publicKeyPem
                .replace("-----BEGIN PUBLIC KEY-----", "")
                .replace("-----END PUBLIC KEY-----", "")
                .replace("\\s".toRegex(), ""),
            Base64.DEFAULT,
        )
        val publicKey = KeyFactory.getInstance("RSA").generatePublic(X509EncodedKeySpec(publicKeyBytes))
        val signature = Signature.getInstance("SHA256withRSA")
        signature.initVerify(publicKey)
        signature.update(payload.toByteArray(Charsets.UTF_8))
        return signature.verify(Base64.decode(signatureBase64, Base64.DEFAULT))
    }

    private fun decryptAesKey(encryptedKeyBase64: String): SecretKey {
        val encryptedKey = Base64.decode(encryptedKeyBase64, Base64.DEFAULT)
        val cipher = Cipher.getInstance("RSA/ECB/OAEPWithSHA-256AndMGF1Padding")
        cipher.init(Cipher.DECRYPT_MODE, getOrCreateKeyPair().private)
        val rawKey = cipher.doFinal(encryptedKey)
        return SecretKeySpec(rawKey, "AES")
    }

    private fun getOrCreateKeyPair(): KeyPair {
        cachedKeyPair?.let { return it }
        synchronized(this) {
            cachedKeyPair?.let { return it }
            val storedPublic = prefs.getString(KEY_PUBLIC, null)
            val storedPrivate = prefs.getString(KEY_PRIVATE, null)
            if (!storedPublic.isNullOrBlank() && !storedPrivate.isNullOrBlank()) {
                val keyFactory = KeyFactory.getInstance("RSA")
                val publicKey = keyFactory.generatePublic(X509EncodedKeySpec(Base64.decode(storedPublic, Base64.DEFAULT)))
                val privateKey = keyFactory.generatePrivate(PKCS8EncodedKeySpec(Base64.decode(storedPrivate, Base64.DEFAULT)))
                return KeyPair(publicKey, privateKey).also { cachedKeyPair = it }
            }
            val generator = KeyPairGenerator.getInstance("RSA")
            generator.initialize(2048)
            val keyPair = generator.generateKeyPair()
            prefs.edit()
                .putString(KEY_PUBLIC, Base64.encodeToString(keyPair.public.encoded, Base64.NO_WRAP))
                .putString(KEY_PRIVATE, Base64.encodeToString(keyPair.private.encoded, Base64.NO_WRAP))
                .apply()
            cachedKeyPair = keyPair
            return keyPair
        }
    }

    companion object {
        private const val KEY_PUBLIC = "client_public"
        private const val KEY_PRIVATE = "client_private"
    }
}
