package com.rulemind.android

import android.content.Context
import android.util.Base64
import androidx.test.core.app.ApplicationProvider
import com.rulemind.core.models.RuleMindConfig
import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.MockWebServer
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import java.security.KeyFactory
import java.security.KeyPair
import java.security.KeyPairGenerator
import java.security.SecureRandom
import java.security.Signature
import java.security.spec.X509EncodedKeySpec
import javax.crypto.Cipher
import javax.crypto.KeyGenerator
import javax.crypto.SecretKey
import javax.crypto.spec.GCMParameterSpec
import javax.crypto.spec.SecretKeySpec

/**
 * `BundleManager` applies what the server sends to the on-device engine, so **the signature check
 * in `syncNow` is a security boundary**: a bundle is compiled policy — rules, scorecards,
 * thresholds — and accepting a tampered one means someone else decides who gets credit.
 *
 * Most of this file exists to exercise that one `require(...)`. It builds a genuinely encrypted,
 * genuinely signed envelope against the device's real public key rather than stubbing the crypto,
 * because a stub would prove the test's own arithmetic and not the SDK's.
 *
 * Note `verifySignature` returns true when `serverPublicKeyPem` is unset — the documented default
 * for a deployment that has not pinned a key. That is why these tests configure one: without it
 * there is no check to test.
 */
@RunWith(RobolectricTestRunner::class)
class BundleManagerTest {

    private lateinit var server: MockWebServer
    private val storeKey: SecretKey = KeyGenerator.getInstance("AES").apply { init(256) }.generateKey()
    private val context: Context get() = ApplicationProvider.getApplicationContext()

    /** Stands in for the RuleMind server that signs bundles. */
    private val serverKeys: KeyPair =
        KeyPairGenerator.getInstance("RSA").apply { initialize(2048) }.generateKeyPair()
    private val impostorKeys: KeyPair =
        KeyPairGenerator.getInstance("RSA").apply { initialize(2048) }.generateKeyPair()

    @Before fun setUp() { server = MockWebServer(); server.start() }
    @After fun tearDown() = server.shutdown()

    private fun pem(pair: KeyPair) =
        "-----BEGIN PUBLIC KEY-----\n" +
            Base64.encodeToString(pair.public.encoded, Base64.NO_WRAP) +
            "\n-----END PUBLIC KEY-----"

    private fun config(serverPem: String?) = RuleMindConfig(
        baseUrl = server.url("/").toString().trimEnd('/'),
        apiKey = "k",
        retryCount = 0,
        serverPublicKeyPem = serverPem,
    )

    private fun managers(serverPem: String?): Pair<BundleManager, CryptoManager> {
        val cfg = config(serverPem)
        val crypto = CryptoManager(context, cfg) { storeKey }
        val manager = BundleManager(context, cfg, NetworkClient(cfg), crypto) { storeKey }
        return manager to crypto
    }

    /** A real envelope: AES-GCM bundle, AES key RSA-encrypted to the DEVICE's public key. */
    private fun envelope(bundleJson: String, crypto: CryptoManager, signWith: KeyPair): String {
        val aes = KeyGenerator.getInstance("AES").apply { init(256) }.generateKey()
        val iv = ByteArray(12).also { SecureRandom().nextBytes(it) }
        val gcm = Cipher.getInstance("AES/GCM/NoPadding")
        gcm.init(Cipher.ENCRYPT_MODE, SecretKeySpec(aes.encoded, "AES"), GCMParameterSpec(128, iv))
        val encryptedBundle = Base64.encodeToString(iv + gcm.doFinal(bundleJson.toByteArray()), Base64.NO_WRAP)

        val devicePublic = KeyFactory.getInstance("RSA").generatePublic(
            X509EncodedKeySpec(Base64.decode(crypto.publicKeyBase64(), Base64.NO_WRAP)))
        val rsa = Cipher.getInstance("RSA/ECB/OAEPWithSHA-256AndMGF1Padding")
        rsa.init(Cipher.ENCRYPT_MODE, devicePublic)
        val encryptedKey = Base64.encodeToString(rsa.doFinal(aes.encoded), Base64.NO_WRAP)

        val signer = Signature.getInstance("SHA256withRSA").apply {
            initSign(signWith.private); update(bundleJson.toByteArray())
        }
        val signature = Base64.encodeToString(signer.sign(), Base64.NO_WRAP)

        return """{"bundleVersion":5,"encryptedBundle":"$encryptedBundle",
                   "encryptedKey":"$encryptedKey","signature":"$signature","checksum":"c"}"""
    }

    @Test
    fun `a correctly signed bundle is accepted and applied`() {
        // The positive control. Without it, the rejection test below could pass because NOTHING
        // is ever accepted -- a signature check that refuses everything is not a working check.
        val (manager, crypto) = managers(pem(serverKeys))
        server.enqueue(MockResponse().setBody(
            envelope("""{"bundleId":"b1","bundleVersion":5}""", crypto, signWith = serverKeys)))

        val applied = manager.syncNow()
        assertNotNull("a correctly signed bundle must be applied", applied)
        assertEquals(5, applied!!.bundleVersion)
        assertEquals("b1", applied.bundleId)
    }

    @Test
    fun `a bundle signed by the WRONG key is REJECTED`() {
        // A bundle is compiled policy. Accepting one this device cannot attribute to the server
        // hands the decisioning rules to whoever produced it.
        val (manager, crypto) = managers(pem(serverKeys))
        server.enqueue(MockResponse().setBody(
            envelope("""{"bundleId":"evil","bundleVersion":9}""", crypto, signWith = impostorKeys)))

        val error = runCatching { manager.syncNow() }.exceptionOrNull()
        assertNotNull("a wrongly signed bundle must be refused, not applied", error)
        assertTrue(
            "the refusal should name the signature: ${error!!.message}",
            error.message.orEmpty().contains("signature", ignoreCase = true))
        assertNull("nothing from the rejected bundle may be retained", manager.currentBundle())
    }

    @Test
    fun `an unchanged response leaves the current bundle alone`() {
        val (manager, _) = managers(pem(serverKeys))
        server.enqueue(MockResponse().setResponseCode(304))
        assertNull(manager.syncNow())
        assertEquals(0, manager.currentBundleVersion())
    }

    @Test
    fun `clear removes the stored bundle`() {
        val (manager, crypto) = managers(pem(serverKeys))
        server.enqueue(MockResponse().setBody(
            envelope("""{"bundleId":"b1","bundleVersion":5}""", crypto, signWith = serverKeys)))
        manager.syncNow()
        assertNotNull(manager.currentBundle())

        manager.clear()
        assertNull("clear must empty the store, not just the in-memory copy", manager.currentBundle())
        assertEquals(0, manager.currentBundleVersion())
    }

    @Test
    fun `an applied bundle survives a new manager over the same store`() {
        val (manager, crypto) = managers(pem(serverKeys))
        server.enqueue(MockResponse().setBody(
            envelope("""{"bundleId":"b1","bundleVersion":5}""", crypto, signWith = serverKeys)))
        manager.syncNow()

        val cfg = config(pem(serverKeys))
        val reopened = BundleManager(context, cfg, NetworkClient(cfg), CryptoManager(context, cfg) { storeKey }) { storeKey }
        assertEquals(
            "the bundle must be readable after a restart, or the device re-downloads every launch",
            5, reopened.currentBundleVersion())
    }
}
