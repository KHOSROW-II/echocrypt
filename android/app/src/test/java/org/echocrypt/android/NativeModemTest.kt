package org.echocrypt.android

import org.echocrypt.android.protocol.NativeModem
import org.echocrypt.android.protocol.NativeDemodulator
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
        val protectedPayloadBytes = payload.size + 4
        val convolutionalBits = 2 * (protectedPayloadBytes * 8 + 6)
        val expectedSymbols = 64 + (11 * 15 * 2) + convolutionalBits
        assertEquals(expectedSymbols, frame.symbolCount)
        assertEquals(expectedSymbols * 240 + 96, frame.samples.size)
    }


    @Test
    fun matchesPythonEcf3GoldenVectors() {
        // SHA-256 digests generated from the Python core.modem.modulate() ECF3 wire format
        // for the same ECF3 parameters and payloads. Each symbol is serialized
        // as one byte (0x00 or 0x01) before hashing.
        val vectors = listOf(
            byteArrayOf(0x00) to Pair(486, "c616f25ad3a6b8ec7e414742f2908a634f0a1d17b2a1fc68101e0cb4f09de7a9"),
            "ECHO".toByteArray(Charsets.UTF_8) to Pair(534, "4687bab88b82247f2435465e513c37ab21a238ddf9b20f69f6a0aafab6b24c7a"),
            "ECHOCRYPT ANDROID LINK TEST".toByteArray(Charsets.UTF_8) to Pair(902, "10d1e11acb932c8dcfa9431c1eadb14eb66f81c33af1b4e7e2844b5e89afb866"),
            ByteArray(32) { it.toByte() } to Pair(982, "17acbcc1d5fb4bcf4641b10118ed010754969a6ca146cc6d2631d55be50b51a3")
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


    @Test
    fun fastSyncPrefilterSeparatesEcf3FromSilence() {
        val frame = NativeModem.encode("FAST SYNC FILTER".toByteArray(Charsets.UTF_8))
        val confidence = NativeDemodulator.fastSyncConfidence(frame.samples, 0)
        assertTrue("valid ECF3 sync should pass prefilter: $confidence", confidence >= 0.08)
        assertTrue(NativeDemodulator.fastSyncConfidence(FloatArray(64 * 240), 0) < 0.08)
    }

    @Test
    fun nativeDemodulatorDecodesAlignedEcf3Waveform() {
        val payloads = listOf(
            "E".toByteArray(Charsets.UTF_8),
            "ECHO".toByteArray(Charsets.UTF_8),
            "ECHOCRYPT ANDROID LINK TEST".toByteArray(Charsets.UTF_8),
            ByteArray(32) { it.toByte() }
        )
        payloads.forEach { payload ->
            val frame = NativeModem.encode(payload)
            assertTrue(NativeDemodulator.decodeAligned(frame.samples).contentEquals(payload))
        }
    }

    @Test
    fun nativeDemodulatorAcceptsExplicitAlignedStartOffset() {
        val payload = "OFFSET TEST".toByteArray(Charsets.UTF_8)
        val frame = NativeModem.encode(payload)
        val prefix = FloatArray(37) { 0f }
        val combined = prefix + frame.samples
        assertTrue(NativeDemodulator.decodeAligned(combined, prefix.size).contentEquals(payload))
    }

    @Test(expected = IllegalArgumentException::class)
    fun nativeDemodulatorRejectsNonEcf3Audio() {
        NativeDemodulator.decodeAligned(FloatArray(20_000))
    }

    @Test(expected = IllegalArgumentException::class)
    fun rejectsEmptyPayload() {
        NativeModem.encode(byteArrayOf())
    }

    @Test(expected = IllegalArgumentException::class)
    fun rejectsPayloadAboveProtocolLimit() {
        NativeModem.encode(ByteArray(1_025))
    }

    @Test
    fun streamReceiverHandlesArbitraryChunkBoundariesAndConsecutiveFrames() {
        val firstPayload = "STREAM ONE".toByteArray(Charsets.UTF_8)
        val secondPayload = "STREAM TWO WITH MORE DATA".toByteArray(Charsets.UTF_8)
        val first = NativeModem.encode(firstPayload)
        val second = NativeModem.encode(secondPayload)
        val stream = FloatArray(16) + first.samples + second.samples
        val receiver = org.echocrypt.android.protocol.NativeStreamReceiver()
        val received = ArrayList<org.echocrypt.android.protocol.NativeStreamReceiver.ReceivedFrame>()

        var cursor = 0
        val chunks = intArrayOf(127, 4093, 8191, 2048, 7777, 16001)
        var index = 0
        while (cursor < stream.size) {
            val end = minOf(stream.size, cursor + chunks[index++ % chunks.size])
            received += receiver.push(stream.copyOfRange(cursor, end))
            cursor = end
        }

        assertEquals(2, received.size)
        assertTrue(received[0].payload.contentEquals(firstPayload))
        assertTrue(received[1].payload.contentEquals(secondPayload))
        assertEquals(16L, received[0].startOffsetSamples)
        assertEquals(16L + first.samples.size, received[1].startOffsetSamples)
    }


    @Test
    fun streamReceiverRefinesStartAtNonStrideAlignedSampleOffset() {
        val payload = "SAMPLE ACCURATE SYNC".toByteArray(Charsets.UTF_8)
        val frame = NativeModem.encode(payload)
        val prefix = FloatArray(19) { 0f }
        val stream = prefix + frame.samples
        val receiver = org.echocrypt.android.protocol.NativeStreamReceiver(candidateStride = 8)
        val received = receiver.push(stream)

        assertEquals(1, received.size)
        assertTrue(received.single().payload.contentEquals(payload))
        assertEquals(19L, received.single().startOffsetSamples)
    }


    @Test
    fun streamReceiverRecoversPositiveAndNegativeSampleClockDrift() {
        val payload = "CLOCK DRIFT RECOVERY".toByteArray(Charsets.UTF_8)
        val frame = NativeModem.encode(payload)
        for (period in listOf(239.0, 241.0)) {
            val driftedSize = (frame.samples.size * period / 240.0).toInt()
            val drifted = FloatArray(driftedSize) { index ->
                val source = index * 240.0 / period
                val left = source.toInt()
                val fraction = source - left
                if (left + 1 >= frame.samples.size) frame.samples.last()
                else (frame.samples[left] * (1.0 - fraction) + frame.samples[left + 1] * fraction).toFloat()
            }
            val receiver = org.echocrypt.android.protocol.NativeStreamReceiver(candidateStride = 1)
            val received = receiver.push(drifted)
            assertEquals("period=$period", 1, received.size)
            assertTrue("period=$period", received.single().payload.contentEquals(payload))
        }
    }

    @Test
    fun streamReceiverWaitsForIncompleteFrameInsteadOfDiscardingItsSync() {
        val payload = "INCREMENTAL RECEIVE".toByteArray(Charsets.UTF_8)
        val frame = NativeModem.encode(payload)
        val receiver = org.echocrypt.android.protocol.NativeStreamReceiver(candidateStride = 1)
        val split = frame.samples.size / 2

        assertTrue(receiver.push(frame.samples.copyOfRange(0, split)).isEmpty())
        val completed = receiver.push(frame.samples.copyOfRange(split, frame.samples.size))
        assertEquals(1, completed.size)
        assertTrue(completed.single().payload.contentEquals(payload))
    }
}
