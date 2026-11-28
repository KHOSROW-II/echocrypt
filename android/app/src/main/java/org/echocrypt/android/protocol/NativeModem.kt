package org.echocrypt.android.protocol

import java.nio.ByteBuffer
import java.nio.ByteOrder
import java.security.MessageDigest
import java.util.zip.CRC32
import kotlin.math.PI
import kotlin.math.max
import kotlin.math.sin

/**
 * ECF3 BFSK frame encoder ported from core/modem.py.
 *
 * Wire compatibility constants are deliberately kept in sync with the Python
 * implementation. Hopping is rejected until an authenticated hop-key provider
 * is connected; silently emitting a non-hopped frame when hopping was requested
 * would be a protocol/security downgrade.
 */
object NativeModem {
    private val MAGIC = byteArrayOf(0x45, 0x43, 0x46, 0x33) // ECF3
    private const val SAMPLE_RATE = 48_000
    private const val SYMBOL_RATE = 200
    private const val MARK_HZ = 1_800
    private const val SPACE_HZ = 2_400
    private const val GAIN = 0.35
    private const val MAX_PAYLOAD = 1_024
    private const val PAYLOAD_INTERLEAVER_DEPTH = 16
    private const val CONV_INTERLEAVER_DEPTH = 8
    private const val CONV_K = 7
    private val CONV_POLYS = intArrayOf(0x79, 0x5B) // octal 171, 133

    data class EncodedFrame(
        val samples: FloatArray,
        val payloadBytes: Int,
        val symbolCount: Int,
        val symbols: IntArray,
        val durationSeconds: Double
    )

    fun encode(payload: ByteArray): EncodedFrame {
        require(payload.isNotEmpty()) { "Payload must not be empty" }
        require(payload.size <= MAX_PAYLOAD) { "Payload exceeds $MAX_PAYLOAD bytes" }

        val header = ByteBuffer.allocate(11).order(ByteOrder.BIG_ENDIAN)
            .put(MAGIC)
            .putShort(payload.size.toShort())
            .put(3.toByte()) // FEC_CONV
            .putInt(0) // no frequency hopping until authenticated key integration
            .array()

        val protectedHeader = header + crc32(header)
        val headerBits = bytesToBits(protectedHeader)
        while (headerBits.size % 11 != 0) headerBits.add(0)
        val encodedHeaderCopy = hamming1511(headerBits)
        val encodedHeader = encodedHeaderCopy + encodedHeaderCopy

        val protectedPayload = header + payload + crc32(header + payload)
        val tailBits = interleave(convolutionalEncode(protectedPayload), PAYLOAD_INTERLEAVER_DEPTH)

        val sync = MessageDigest.getInstance("SHA-256")
            .digest("ECHOCRYPT/ECF3/SYNC/v1".toByteArray(Charsets.UTF_8))
            .copyOfRange(0, 8)
        val bits = bytesToBits(sync).toIntArray() + encodedHeader.toIntArray() + tailBits.toIntArray()
        val samplesPerSymbol = SAMPLE_RATE / SYMBOL_RATE
        val output = FloatArray(bits.size * samplesPerSymbol + 96)
        var phase = 0.0
        var lastFrequency = MARK_HZ
        for (index in bits.indices) {
            val frequency = if (bits[index] == 1) MARK_HZ else SPACE_HZ
            lastFrequency = frequency
            for (n in 0 until samplesPerSymbol) {
                val theta = phase + 2.0 * PI * frequency * n / SAMPLE_RATE
                output[index * samplesPerSymbol + n] = (GAIN * sin(theta)).toFloat()
            }
            val lastTheta = phase + 2.0 * PI * frequency * (samplesPerSymbol - 1) / SAMPLE_RATE
            phase = (lastTheta + 2.0 * PI * frequency / SAMPLE_RATE) % (2.0 * PI)
        }
        val guardSize = minOf(96, max(8, samplesPerSymbol / 2))
        for (n in 0 until guardSize) {
            val fade = if (guardSize == 1) 0.0 else 1.0 - n.toDouble() / (guardSize - 1)
            val theta = phase + 2.0 * PI * lastFrequency * n / SAMPLE_RATE
            output[bits.size * samplesPerSymbol + n] = (GAIN * sin(theta) * fade).toFloat()
        }
        return EncodedFrame(
            samples = output,
            payloadBytes = payload.size,
            symbolCount = bits.size,
            symbols = bits,
            durationSeconds = output.size.toDouble() / SAMPLE_RATE
        )
    }

    private fun crc32(bytes: ByteArray): ByteArray {
        val crc = CRC32().apply { update(bytes) }.value
        return ByteBuffer.allocate(4).order(ByteOrder.BIG_ENDIAN).putInt(crc.toInt()).array()
    }

    private fun bytesToBits(bytes: ByteArray): MutableList<Int> {
        val result = ArrayList<Int>(bytes.size * 8)
        for (byte in bytes) for (shift in 7 downTo 0) {
            result.add((byte.toInt() ushr shift) and 1)
        }
        return result
    }

    private fun hamming1511(bits: List<Int>): List<Int> {
        require(bits.size % 11 == 0)
        val parityPositions = setOf(1, 2, 4, 8)
        val result = ArrayList<Int>(bits.size / 11 * 15)
        for (offset in bits.indices step 11) {
            val word = IntArray(16)
            var cursor = 0
            for (position in 1..15) {
                if (position !in parityPositions) word[position] = bits[offset + cursor++]
            }
            for (parityPosition in intArrayOf(1, 2, 4, 8)) {
                var parity = 0
                for (position in 1..15) {
                    if ((position and parityPosition) != 0 && position != parityPosition) {
                        parity = parity xor word[position]
                    }
                }
                word[parityPosition] = parity
            }
            for (position in 1..15) result.add(word[position])
        }
        return result
    }

    private fun convolutionalEncode(bytes: ByteArray): List<Int> {
        val bits = bytesToBits(bytes)
        repeat(CONV_K - 1) { bits.add(0) }
        var state = 0
        val encoded = ArrayList<Int>(bits.size * 2)
        for (bit in bits) {
            val register = (state shl 1) or bit
            encoded.add(Integer.bitCount(register and CONV_POLYS[0]) and 1)
            encoded.add(Integer.bitCount(register and CONV_POLYS[1]) and 1)
            state = register and ((1 shl (CONV_K - 1)) - 1)
        }
        return interleave(encoded, CONV_INTERLEAVER_DEPTH)
    }

    /** Row-wise write, column-wise read; final incomplete row is not padded. */
    private fun interleave(values: List<Int>, depth: Int): List<Int> {
        if (values.size <= depth) return values.toList()
        val rows = (values.size + depth - 1) / depth
        val result = ArrayList<Int>(values.size)
        for (column in 0 until depth) {
            for (row in 0 until rows) {
                val index = row * depth + column
                if (index < values.size) result.add(values[index])
            }
        }
        return result
    }
}
