package org.echocrypt.android

import org.echocrypt.android.protocol.NativeCrypto
import org.echocrypt.android.protocol.ReplayWindow
import org.junit.Assert.*
import org.junit.Test
import java.util.HexFormat

class NativeCryptoTest {
    private fun hex(value: String) = HexFormat.of().parseHex(value)

    @Test
    fun hkdfMatchesRfc5869Sha256CaseOne() {
        val output = NativeCrypto.hkdfSha256(
            ikm = ByteArray(22) { 0x0b },
            salt = hex("000102030405060708090a0b0c"),
            info = hex("f0f1f2f3f4f5f6f7f8f9"),
            length = 42
        )
        assertArrayEquals(
            hex("3cb25f25faacd57a90434f64d0362f2a" +
                "2d2d0a90cf1a5a4c5db02d56ecc4c5bf" +
                "34007208d5b887185865"),
            output
        )
    }

    @Test
    fun directionalSecureChannelsRoundTripAndRejectReplay() {
        val key = ByteArray(32) { it.toByte() }
        val sender = NativeCrypto.SecureChannel(key)
        val receiver = NativeCrypto.SecureChannel(key)
        val packet = sender.encrypt("ECHOCRYPT".toByteArray(), "ECHOCRYPT/TEXT/v2".toByteArray())
        assertEquals("ECHOCRYPT", String(receiver.decrypt(packet, "ECHOCRYPT/TEXT/v2".toByteArray())))
        assertFalse(receiver.run { ReplayWindow().wouldAccept(-1L) })
        try {
            receiver.decrypt(packet, "ECHOCRYPT/TEXT/v2".toByteArray())
            fail("Duplicate packet must be rejected")
        } catch (_: IllegalArgumentException) {
            // Expected.
        }
    }

    @Test
    fun paddingAndAuthenticationFailureDoNotConsumeSequence() {
        val key = ByteArray(32) { (it * 3).toByte() }
        val sender = NativeCrypto.SecureChannel(key, paddingBlock = 64)
        val receiver = NativeCrypto.SecureChannel(key, paddingBlock = 64)
        val packet = sender.encrypt("payload".toByteArray())
        val damaged = packet.copyOf().also { it[it.lastIndex] = (it.last().toInt() xor 1).toByte() }
        try {
            receiver.decrypt(damaged)
            fail("Tampered packet must fail authentication")
        } catch (_: Exception) {
            // Expected.
        }
        assertEquals("payload", String(receiver.decrypt(packet)))
        assertEquals(16 + 64 + 16, packet.size)
    }

    @Test
    fun aadIsAuthenticated() {
        val key = ByteArray(32) { 7 }
        val sender = NativeCrypto.SecureChannel(key)
        val receiver = NativeCrypto.SecureChannel(key)
        val packet = sender.encrypt(byteArrayOf(1, 2, 3), byteArrayOf(9))
        try {
            receiver.decrypt(packet, byteArrayOf(8))
            fail("Wrong AAD must fail authentication")
        } catch (_: Exception) {
            // Expected.
        }
        assertArrayEquals(byteArrayOf(1, 2, 3), receiver.decrypt(packet, byteArrayOf(9)))
    }
}
