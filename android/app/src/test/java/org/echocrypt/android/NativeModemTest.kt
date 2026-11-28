package org.echocrypt.android

import org.echocrypt.android.protocol.NativeModem
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
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

    @Test(expected = IllegalArgumentException::class)
    fun rejectsEmptyPayload() {
        NativeModem.encode(byteArrayOf())
    }

    @Test(expected = IllegalArgumentException::class)
    fun rejectsPayloadAboveProtocolLimit() {
        NativeModem.encode(ByteArray(1_025))
    }
}
