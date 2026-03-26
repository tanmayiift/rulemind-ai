package com.rulemind.android

import android.content.Context
import android.security.keystore.KeyGenParameterSpec
import android.security.keystore.KeyProperties
import android.util.Base64
import com.rulemind.core.models.BundleEnvelope
import com.rulemind.core.models.RuleMindConfig
import java.security.KeyFactory
import java.security.KeyPairGenerator
import java.security.KeyStore
import java.security.PublicKey
import java.security.Signature
import java.security.spec.X509EncodedKeySpec
import javax.crypto.Cipher
import javax.crypto.KeyGenerator
import javax.crypto.SecretKey
import javax.crypto.spec.GCMParameterSpec
import javax.crypto.spec.SecretKeySpec

class CryptoManager(
    context: Context,
    private val config: RuleMindConfig,
) {
    private val applicationContext = context.applicationContext
    private val alias = "rulemind.sdk.keypair"

    init {
        ensureKeyPair()
    }

    fun publicKeyBase64(): String {
        val keyStore = KeyStore.getInstance("AndroidKeyStore").apply { load(null) }
        val certificate = keyStore.getCertificate(alias)
        return Base64.encodeToString(certificate.publicKey.encoded, Base64.NO_WRAP)
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
        val keyStore = KeyStore.getInstance("AndroidKeyStore").apply { load(null) }
        val privateKey = keyStore.getKey(alias, null)
        val cipher = Cipher.getInstance("RSA/ECB/OAEPWithSHA-256AndMGF1Padding")
        cipher.init(Cipher.DECRYPT_MODE, privateKey)
        val rawKey = cipher.doFinal(encryptedKey)
        return SecretKeySpec(rawKey, "AES")
    }

    private fun ensureKeyPair() {
        val keyStore = KeyStore.getInstance("AndroidKeyStore").apply { load(null) }
        if (keyStore.containsAlias(alias)) {
            return
        }
        val generator = KeyPairGenerator.getInstance(KeyProperties.KEY_ALGORITHM_RSA, "AndroidKeyStore")
        generator.initialize(
            KeyGenParameterSpec.Builder(alias, KeyProperties.PURPOSE_DECRYPT or KeyProperties.PURPOSE_ENCRYPT)
                .setDigests(KeyProperties.DIGEST_SHA256, KeyProperties.DIGEST_SHA512)
                .setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_RSA_OAEP)
                .build(),
        )
        generator.generateKeyPair()
    }
}
