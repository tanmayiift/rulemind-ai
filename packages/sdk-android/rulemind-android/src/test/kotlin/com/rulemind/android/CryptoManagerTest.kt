package com.rulemind.android

import android.content.Context
import android.util.Base64
import androidx.test.core.app.ApplicationProvider
import com.rulemind.core.models.BundleEnvelope
import com.rulemind.core.models.RuleMindConfig
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import java.security.KeyFactory
import java.security.KeyPairGenerator
import java.security.Signature
import java.security.spec.X509EncodedKeySpec
import javax.crypto.Cipher
import javax.crypto.KeyGenerator
import javax.crypto.SecretKey
import javax.crypto.spec.GCMParameterSpec

/**
 * `CryptoManager` holds the DEVICE'S RSA PRIVATE KEY. The server already has its public half, so
 * this is the one store in the SDK where losing the contents is not a cache miss: a regenerated
 * key pair produces a device that can no longer decrypt any bundle addressed to it, and the
 * symptom surfaces later, somewhere else, as a bundle that will not load.
 *
 * Until this file the class had no test at all. Its header comment explains why the migration
 * matters; nothing checked that the key actually survives.
 *
 * As with [SecurePrefsTest], Robolectric cannot back the AndroidKeyStore, so these pass an
 * ordinary AES key through the `keyProvider` seam. What that covers and what it does not:
 *
 *   covered here  key-pair persistence across instances, the bundle decryption format
 *                 (12-byte IV prefix + AES-GCM), the RSA-OAEP key unwrap, signature
 *                 verification in both directions, and the fail-OPEN default below
 *   NOT covered   that the wrapping key lives in the keystore and is non-exportable (#133)
 */
@RunWith(RobolectricTestRunner::class)
class CryptoManagerTest {

    private val storeKey: SecretKey =
        KeyGenerator.getInstance("AES").apply { init(256) }.generateKey()

    private fun context(): Context = ApplicationProvider.getApplicationContext()

    private fun manager(config: RuleMindConfig = config()) =
        CryptoManager(context(), config) { storeKey }

    private fun config(serverPublicKeyPem: String? = null) =
        RuleMindConfig(baseUrl = "https://example.invalid", apiKey = "k", serverPublicKeyPem = serverPublicKeyPem)

    // ── the property the class exists for ────────────────────────────────────────────────────

    /**
     * THE regression to guard. A second CryptoManager over the same store must present the same
     * public key -- if it generates a new pair, every bundle the server already encrypted for
     * this device becomes undecryptable, silently.
     */
    @Test
    fun `the device key pair survives a new CryptoManager over the same store`() {
        val first = manager().publicKeyBase64()
        val second = manager().publicKeyBase64()
        assertEquals(first, second)
    }

    @Test
    fun `the public key is a decodable X509 RSA key`() {
        val encoded = Base64.decode(manager().publicKeyBase64(), Base64.DEFAULT)
        val key = KeyFactory.getInstance("RSA").generatePublic(X509EncodedKeySpec(encoded))
        assertEquals("RSA", key.algorithm)
    }

    /**
     * A DIFFERENT store key means the stored pair cannot be read back. SecurePrefs answers null
     * for a value it cannot decrypt rather than throwing, so CryptoManager falls through to
     * generating a fresh pair -- which is the correct behaviour (a device that cannot read its
     * own keystore has effectively been reset) but is worth pinning, because the alternative
     * shapes are a crash or, far worse, a silently EMPTY key.
     */
    @Test
    fun `an unreadable store yields a fresh pair rather than a crash`() {
        val original = manager().publicKeyBase64()
        val otherKey = KeyGenerator.getInstance("AES").apply { init(256) }.generateKey()
        val regenerated = CryptoManager(context(), config()) { otherKey }.publicKeyBase64()
        assertNotEquals(original, regenerated)
    }

    // ── bundle decryption ────────────────────────────────────────────────────────────────────

    /** Encrypt exactly the way the server does, then assert the device gets the plaintext back. */
    private fun envelopeFor(manager: CryptoManager, plaintext: String): BundleEnvelope {
        val devicePublic = KeyFactory.getInstance("RSA")
            .generatePublic(X509EncodedKeySpec(Base64.decode(manager.publicKeyBase64(), Base64.DEFAULT)))

        val aesKey = KeyGenerator.getInstance("AES").apply { init(256) }.generateKey()
        val wrap = Cipher.getInstance("RSA/ECB/OAEPWithSHA-256AndMGF1Padding")
        wrap.init(Cipher.ENCRYPT_MODE, devicePublic)
        val encryptedKey = Base64.encodeToString(wrap.doFinal(aesKey.encoded), Base64.NO_WRAP)

        val cipher = Cipher.getInstance("AES/GCM/NoPadding")
        cipher.init(Cipher.ENCRYPT_MODE, aesKey)
        val iv = cipher.iv
        // The wire format: a 12-byte IV prefix, then the GCM ciphertext. CryptoManager splits on
        // that offset, so the framing is part of the contract and not an implementation detail.
        assertEquals(12, iv.size)
        val body = cipher.doFinal(plaintext.toByteArray(Charsets.UTF_8))

        return BundleEnvelope(
            bundleVersion = 1,
            encryptedBundle = Base64.encodeToString(iv + body, Base64.NO_WRAP),
            encryptedKey = encryptedKey,
            signature = "",
            checksum = "",
            compiledAt = "2026-09-19T00:00:00Z",
            expiresAt = "2027-09-19T00:00:00Z",
        )
    }

    @Test
    fun `a bundle encrypted for this device round-trips`() {
        val crypto = manager()
        val plaintext = """{"policies":[],"variables":[]}"""
        assertEquals(plaintext, crypto.decryptBundle(envelopeFor(crypto, plaintext)))
    }

    @Test(expected = IllegalArgumentException::class)
    fun `a payload too short to hold an IV is rejected`() {
        val crypto = manager()
        val good = envelopeFor(crypto, "x")
        crypto.decryptBundle(good.copy(encryptedBundle = Base64.encodeToString(ByteArray(8), Base64.NO_WRAP)))
    }

    /** A tampered ciphertext must not decrypt to anything. GCM's tag is what makes this true. */
    @Test
    fun `a tampered bundle fails the GCM tag`() {
        val crypto = manager()
        val envelope = envelopeFor(crypto, "the original bundle")
        val bytes = Base64.decode(envelope.encryptedBundle, Base64.DEFAULT)
        bytes[bytes.size - 1] = (bytes[bytes.size - 1].toInt() xor 0x01).toByte()
        val tampered = envelope.copy(encryptedBundle = Base64.encodeToString(bytes, Base64.NO_WRAP))
        var threw = false
        try {
            crypto.decryptBundle(tampered)
        } catch (e: Exception) {
            threw = true
        }
        assertTrue("a flipped ciphertext bit must not decrypt", threw)
    }

    /** An envelope wrapped for a DIFFERENT device must not open here. */
    @Test
    fun `a bundle addressed to another device does not open`() {
        val crypto = manager()
        val otherKey = KeyGenerator.getInstance("AES").apply { init(256) }.generateKey()
        val otherDevice = CryptoManager(context(), config()) { otherKey }
        val envelope = envelopeFor(otherDevice, "not for you")
        var threw = false
        try {
            crypto.decryptBundle(envelope)
        } catch (e: Exception) {
            threw = true
        }
        assertTrue("an envelope wrapped for another key must not decrypt", threw)
    }

    // ── signature verification ───────────────────────────────────────────────────────────────

    /**
     * FAIL-OPEN, DELIBERATELY, AND WORTH SAYING OUT LOUD. With no `serverPublicKeyPem` configured
     * `verifySignature` returns TRUE for everything -- an unconfigured SDK accepts any bundle
     * signature. That is a real deployment consideration, not a bug to fix here, and this test
     * exists so the behaviour cannot change silently in either direction.
     */
    @Test
    fun `with no server public key configured every signature verifies`() {
        assertTrue(manager().verifySignature("anything at all", "bm90LWEtc2lnbmF0dXJl"))
    }

    @Test
    fun `a real signature verifies and a tampered payload does not`() {
        val serverPair = KeyPairGenerator.getInstance("RSA").apply { initialize(2048) }.generateKeyPair()
        val pem = "-----BEGIN PUBLIC KEY-----\n" +
            Base64.encodeToString(serverPair.public.encoded, Base64.NO_WRAP) +
            "\n-----END PUBLIC KEY-----"

        val payload = """{"bundleVersion":7}"""
        val signer = Signature.getInstance("SHA256withRSA")
        signer.initSign(serverPair.private)
        signer.update(payload.toByteArray(Charsets.UTF_8))
        val signature = Base64.encodeToString(signer.sign(), Base64.NO_WRAP)

        val crypto = manager(config(serverPublicKeyPem = pem))
        assertTrue(crypto.verifySignature(payload, signature))
        assertFalse(crypto.verifySignature("""{"bundleVersion":8}""", signature))
    }

    @Test
    fun `a signature from the wrong key is rejected`() {
        val serverPair = KeyPairGenerator.getInstance("RSA").apply { initialize(2048) }.generateKeyPair()
        val impostor = KeyPairGenerator.getInstance("RSA").apply { initialize(2048) }.generateKeyPair()
        val pem = "-----BEGIN PUBLIC KEY-----\n" +
            Base64.encodeToString(serverPair.public.encoded, Base64.NO_WRAP) +
            "\n-----END PUBLIC KEY-----"

        val payload = "signed by someone else"
        val signer = Signature.getInstance("SHA256withRSA")
        signer.initSign(impostor.private)
        signer.update(payload.toByteArray(Charsets.UTF_8))

        assertFalse(
            manager(config(serverPublicKeyPem = pem))
                .verifySignature(payload, Base64.encodeToString(signer.sign(), Base64.NO_WRAP)),
        )
    }
}
