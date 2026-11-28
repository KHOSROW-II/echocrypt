package org.echocrypt.android

import org.echocrypt.android.protocol.NativeModem
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import java.security.MessageDigest
import org.junit.Test

class NativeModemTest {
    @Test
    fun encodesRepeatableEcf3Waveform() {
        val payload = "ECHOCRYPT ANDROID LINK TEST".toByteArray(Charsets.UTF_8)
        val first = NativeModem.encode(payload)
        val second = NativeModem.encode(payload)

        assertEquals(payload.size, first.payloadBytes)
        assertEquals(first.symbolCount, second.symbolCount)
        assertEquals(first.samples.size, second.samples.size)
        assertTrue(first.samples.indices.all { first.samples[it] == second.samples[it] })
        assertTrue(first.samples.all { it in -0.351f..0.351f })
    }

    @Test
    fun frameLengthMatchesEcf3HeaderAndConvolutionalFecLayout() {
        val payload = byteArrayOf(0x45, 0x43, 0x48, 0x4f)
        val frame = NativeModem.encode(payload)
        val protectedPayloadBytes = 11 + payload.size + 4
        val convolutionalBits = 2 * (protectedPayloadBytes * 8 + 6)
        val expectedSymbols = 64 + (11 * 15 * 2) + convolutionalBits
        assertEquals(expectedSymbols, frame.symbolCount)
        assertEquals(expectedSymbols * 240 + 96, frame.samples.size)
    }


    @Test
    fun matchesPythonEcf3GoldenVectors() {
        // SHA-256 digests generated independently from core.modem.modulate()
        // for the same ECF3 parameters and payloads. Each symbol is serialized
        // as one byte (0x00 or 0x01) before hashing.
        val vectors = listOf(
            byteArrayOf(0x00) to Pair(662, "20d24256ca49026eee840ba11f46c45e63cfac297d595b11732aeaeed97bc5ba"),
            "ECHO".toByteArray(Charsets.UTF_8) to Pair(710, "5895a75caf4bf0a0788dcabae54b97d18b656f97853618f00076578f9678b821"),
            "ECHOCRYPT ANDROID LINK TEST".toByteArray(Charsets.UTF_8) to Pair(1078, "8a0e0b536c478be167b9fe2eccbf42c9f28d153a87f2379cc64f9a7d08eb1d30"),
            ByteArray(32) { it.toByte() } to Pair(1158, "dd3fd3bcaba166234c43dcbb404645b60657526aa451ec8ae6966dafe76a76e1")
        )

        vectors.forEach { (payload, expected) ->
            val frame = NativeModem.encode(payload)
            val digest = MessageDigest.getInstance("SHA-256")
                .digest(frame.symbols.map { it.toByte() }.toByteArray())
                .joinToString("") { "%02x".format(it.toInt() and 0xff) }
            assertEquals(expected.first, frame.symbolCount)
            assertEquals(expected.second, digest)
        }
    }

    @Test(expected = IllegalArgumentException::class)
    fun rejectsEmptyPayload() {
        NativeModem.encode(byteArrayOf())
    }

    @Test(expected = IllegalArgumentException::class)
    fun rejectsPayloadAboveProtocolLimit() {
        NativeModem.encode(ByteArray(1_025))
    }
}
