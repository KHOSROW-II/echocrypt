package org.echocrypt.android.protocol

import java.nio.ByteBuffer
import java.nio.ByteOrder
import java.security.MessageDigest
import java.util.zip.CRC32
import kotlin.math.PI
import kotlin.math.cos
import kotlin.math.sin

/**
 * First native ECF3 receive path. Requires an already aligned frame start and
 * symbol clock; ASRE, timing/frequency recovery, hopping and stream sync remain
 * deliberately out of scope for this initial decoder.
 */
object NativeDemodulator {
    private const val SAMPLE_RATE = 48_000
    private const val SAMPLES_PER_SYMBOL = 240
    private const val MARK_HZ = 1_800
    private const val SPACE_HZ = 2_400
    private const val MAX_PAYLOAD = 1_024
    private const val HEADER_BYTES = 11
    private const val PROTECTED_HEADER_BYTES = 15
    private const val HEADER_CODE_BITS = 165
    private const val HEADER_SYMBOLS = 330
    private const val SYNC_SYMBOLS = 64
    private const val CONV_DEPTH = 8
    private const val PAYLOAD_DEPTH = 16
    private const val CONV_K = 7
    private val POLYS = intArrayOf(0x79, 0x5B)
    private val MAGIC = byteArrayOf(0x45, 0x43, 0x46, 0x33)
    private val SYNC_BITS: List<Int> by lazy {
        val sync = MessageDigest.getInstance("SHA-256")
            .digest("ECHOCRYPT/ECF3/SYNC/v1".toByteArray(Charsets.UTF_8))
            .copyOfRange(0, 8)
        bytesToBits(sync)
    }
    private val MARK_COS = DoubleArray(SAMPLES_PER_SYMBOL) { n ->
        cos(2.0 * PI * MARK_HZ * n / SAMPLE_RATE)
    }
    private val MARK_SIN = DoubleArray(SAMPLES_PER_SYMBOL) { n ->
        sin(2.0 * PI * MARK_HZ * n / SAMPLE_RATE)
    }
    private val SPACE_COS = DoubleArray(SAMPLES_PER_SYMBOL) { n ->
        cos(2.0 * PI * SPACE_HZ * n / SAMPLE_RATE)
    }
    private val SPACE_SIN = DoubleArray(SAMPLES_PER_SYMBOL) { n ->
        sin(2.0 * PI * SPACE_HZ * n / SAMPLE_RATE)
    }

    /**
     * Scores the known 64-symbol ECF3 sync word at an exact sample offset.
     * Positive scores favor the expected BFSK sequence; higher is better.
     */
    fun syncConfidence(samples: FloatArray, startSample: Int): Double {
        require(startSample >= 0)
        require(samples.size - startSample >= SYNC_SYMBOLS * SAMPLES_PER_SYMBOL)
        var score = 0.0
        for (index in 0 until SYNC_SYMBOLS) {
            val metric = symbolMetric(samples, startSample + index * SAMPLES_PER_SYMBOL)
            score += if (SYNC_BITS[index] == 1) metric else -metric
        }
        return score / SYNC_SYMBOLS
    }

    /**
     * Low-cost sync prefilter for stream scanning. It samples 48 evenly spaced
     * points per symbol (every fifth PCM sample), reducing correlation work by
     * 5x. This is only a candidate gate; decodeAligned performs the full check.
     */
    fun fastSyncConfidence(samples: FloatArray, startSample: Int): Double {
        require(startSample >= 0)
        require(samples.size - startSample >= SYNC_SYMBOLS * SAMPLES_PER_SYMBOL)
        var score = 0.0
        for (index in 0 until SYNC_SYMBOLS) {
            val offset = startSample + index * SAMPLES_PER_SYMBOL
            var markI = 0.0; var markQ = 0.0
            var spaceI = 0.0; var spaceQ = 0.0
            for (sample in 0 until SAMPLES_PER_SYMBOL step 5) {
                val value = samples[offset + sample].toDouble()
                markI += value * MARK_COS[sample]; markQ += value * MARK_SIN[sample]
                spaceI += value * SPACE_COS[sample]; spaceQ += value * SPACE_SIN[sample]
            }
            val markEnergy = markI * markI + markQ * markQ
            val spaceEnergy = spaceI * spaceI + spaceQ * spaceQ
            val metric = (markEnergy - spaceEnergy) / (markEnergy + spaceEnergy + 1e-18)
            score += if (SYNC_BITS[index] == 1) metric else -metric
        }
        return score / SYNC_SYMBOLS
    }

    data class TimedDecode(val payload: ByteArray, val symbolPeriod: Double, val startSample: Int)

    /**
     * Recovers small sample-clock errors by fitting the known sync word, then
     * linearly resampling the candidate into the protocol's nominal 240-sample
     * symbol clock. The ECF3 encoder and on-wire format remain unchanged.
     *
     * Search range is +/- 5,000 ppm around 48 kHz. This is a sync-acquisition
     * estimator, not a continuous PLL; large clock errors and time-varying drift
     * still require a streaming timing loop.
     */
    fun decodeWithTimingRecovery(
        samples: FloatArray,
        candidateStart: Int,
        searchRadius: Int = 8
    ): TimedDecode {
        require(candidateStart >= 0 && candidateStart < samples.size)
        val firstStart = maxOf(0, candidateStart - searchRadius)
        val lastStart = minOf(candidateStart + searchRadius, samples.size - SYNC_SYMBOLS * SAMPLES_PER_SYMBOL)
        require(lastStart >= firstStart) { "Not enough samples for timing acquisition" }

        var bestStart = candidateStart
        var bestPeriod = SAMPLES_PER_SYMBOL.toDouble()
        var bestScore = Double.NEGATIVE_INFINITY
        var period = SAMPLES_PER_SYMBOL - 1.2
        while (period <= SAMPLES_PER_SYMBOL + 1.2001) {
            for (start in firstStart..lastStart) {
                val score = timedSyncConfidence(samples, start, period)
                if (score > bestScore) {
                    bestScore = score
                    bestStart = start
                    bestPeriod = period
                }
            }
            period += 0.1
        }
        require(bestScore > 0.12) { "ECF3 sync marker not detected during timing recovery (confidence=$bestScore)" }

        val available = samples.size - bestStart
        val outputSize = ((available / bestPeriod) * SAMPLES_PER_SYMBOL).toInt()
        require(outputSize >= (SYNC_SYMBOLS + HEADER_SYMBOLS) * SAMPLES_PER_SYMBOL)
        val normalized = FloatArray(outputSize) { index ->
            val sourcePosition = bestStart + index * bestPeriod / SAMPLES_PER_SYMBOL
            val left = sourcePosition.toInt()
            val fraction = sourcePosition - left
            if (left + 1 >= samples.size) samples[samples.lastIndex]
            else (samples[left] * (1.0 - fraction) + samples[left + 1] * fraction).toFloat()
        }
        return TimedDecode(decodeAligned(normalized), bestPeriod, bestStart)
    }

    private fun timedSyncConfidence(samples: FloatArray, start: Int, period: Double): Double {
        var score = 0.0
        for (symbol in 0 until SYNC_SYMBOLS) {
            val base = start + symbol * period
            var markI = 0.0; var markQ = 0.0
            var spaceI = 0.0; var spaceQ = 0.0
            for (n in 0 until SAMPLES_PER_SYMBOL) {
                val position = base + n * period / SAMPLES_PER_SYMBOL
                val left = position.toInt()
                if (left + 1 >= samples.size) return Double.NEGATIVE_INFINITY
                val fraction = position - left
                val value = (samples[left] * (1.0 - fraction) + samples[left + 1] * fraction).toDouble()
                markI += value * MARK_COS[n]; markQ += value * MARK_SIN[n]
                spaceI += value * SPACE_COS[n]; spaceQ += value * SPACE_SIN[n]
            }
            val markEnergy = markI * markI + markQ * markQ
            val spaceEnergy = spaceI * spaceI + spaceQ * spaceQ
            val metric = (markEnergy - spaceEnergy) / (markEnergy + spaceEnergy + 1e-18)
            score += if (SYNC_BITS[symbol] == 1) metric else -metric
        }
        return score / SYNC_SYMBOLS
    }

    fun decodeAligned(samples: FloatArray, startSample: Int = 0): ByteArray {
        require(startSample >= 0) { "Frame start must not be negative" }
        require(samples.size - startSample >= (SYNC_SYMBOLS + HEADER_SYMBOLS) * SAMPLES_PER_SYMBOL) {
            "Not enough samples for an ECF3 preamble and protected header"
        }
        val syncScore = syncConfidence(samples, startSample)
        require(syncScore > 0.12) { "ECF3 sync marker not detected (confidence=$syncScore)" }

        val headerStart = startSample + SYNC_SYMBOLS * SAMPLES_PER_SYMBOL
        val headerLlrs = FloatArray(HEADER_SYMBOLS) { symbolMetric(samples, headerStart + it * SAMPLES_PER_SYMBOL) }
        val header = decodeProtectedHeader(headerLlrs)
        val buffer = ByteBuffer.wrap(header).order(ByteOrder.BIG_ENDIAN)
        val magic = ByteArray(4).also { buffer.get(it) }
        require(magic.contentEquals(MAGIC)) { "Unsupported modem frame version" }
        val payloadLength = buffer.short.toInt() and 0xffff
        val fecId = buffer.get().toInt() and 0xff
        val hopSeed = buffer.int
        require(payloadLength in 1..MAX_PAYLOAD) { "Invalid ECF3 payload length: $payloadLength" }
        require(fecId == 3) { "Native receiver currently supports convolutional FEC only" }
        require(hopSeed == 0) { "Authenticated frequency hopping is not supported by this decoder yet" }

        // ECF3 convolutional FEC covers payload + CRC32(header + payload).
        // The fixed header is independently protected by duplicated Hamming
        // coding and its own CRC32.
        val decodedBytes = payloadLength + 4
        val steps = decodedBytes * 8 + CONV_K - 1
        val payloadCodeBits = steps * 2
        val payloadStart = headerStart + HEADER_SYMBOLS * SAMPLES_PER_SYMBOL
        val requiredEnd = payloadStart + payloadCodeBits * SAMPLES_PER_SYMBOL
        require(requiredEnd <= samples.size) {
            "Truncated ECF3 payload: need $requiredEnd samples, received ${samples.size}"
        }
        val payloadLlrs = FloatArray(payloadCodeBits) {
            symbolMetric(samples, payloadStart + it * SAMPLES_PER_SYMBOL)
        }
        val convolutionalLlrs = deinterleave(deinterleave(payloadLlrs, PAYLOAD_DEPTH), CONV_DEPTH)
        val decoded = viterbiDecode(convolutionalLlrs, decodedBytes)
        val payload = decoded.copyOfRange(0, payloadLength)
        val receivedCrc = ByteBuffer.wrap(decoded, payloadLength, 4).order(ByteOrder.BIG_ENDIAN).int.toLong() and 0xffffffffL
        val expectedCrc = crc32(header + payload)
        require(receivedCrc == expectedCrc) {
            "ECF3 payload CRC mismatch (expected=${expectedCrc.toString(16)} received=${receivedCrc.toString(16)})"
        }
        return payload
    }

    /** Positive values favor mark/bit 1; negative values favor space/bit 0. */
    private fun symbolMetric(samples: FloatArray, offset: Int): Float {
        var markI = 0.0; var markQ = 0.0
        var spaceI = 0.0; var spaceQ = 0.0
        for (n in 0 until SAMPLES_PER_SYMBOL) {
            val value = samples[offset + n].toDouble()
            markI += value * MARK_COS[n]; markQ += value * MARK_SIN[n]
            spaceI += value * SPACE_COS[n]; spaceQ += value * SPACE_SIN[n]
        }
        val markEnergy = markI * markI + markQ * markQ
        val spaceEnergy = spaceI * spaceI + spaceQ * spaceQ
        return ((markEnergy - spaceEnergy) / (markEnergy + spaceEnergy + 1e-18)).toFloat()
    }

    private fun decodeProtectedHeader(llrs: FloatArray): ByteArray {
        require(llrs.size == HEADER_SYMBOLS)
        val first = llrs.copyOfRange(0, HEADER_CODE_BITS)
        val second = llrs.copyOfRange(HEADER_CODE_BITS, llrs.size)
        val combined = FloatArray(HEADER_CODE_BITS) { (first[it] + second[it]) / 2f }
        for (candidate in listOf(combined, first, second)) {
            val decoded = decodeHamming1511(candidate, PROTECTED_HEADER_BYTES)
            val header = decoded.copyOfRange(0, HEADER_BYTES)
            val received = ByteBuffer.wrap(decoded, HEADER_BYTES, 4).order(ByteOrder.BIG_ENDIAN).int.toLong() and 0xffffffffL
            if (received == crc32(header)) return header
        }
        throw IllegalArgumentException("ECF3 protected header CRC mismatch")
    }

    private fun decodeHamming1511(llrs: FloatArray, outputBytes: Int): ByteArray {
        val blocks = (outputBytes * 8 + 10) / 11
        require(llrs.size == blocks * 15)
        val bits = ArrayList<Int>(blocks * 11)
        for (block in 0 until blocks) {
            val chunk = llrs.copyOfRange(block * 15, (block + 1) * 15)
            var bestScore = Double.NEGATIVE_INFINITY
            var bestValue = 0
            for (value in 0 until (1 shl 11)) {
                val codeword = hammingCodeword(value)
                var score = 0.0
                for (i in 0 until 15) score += (if (codeword[i] == 1) 1.0 else -1.0) * chunk[i]
                if (score > bestScore) { bestScore = score; bestValue = value }
            }
            for (shift in 10 downTo 0) bits.add((bestValue ushr shift) and 1)
        }
        return bitsToBytes(bits.take(outputBytes * 8))
    }

    private fun hammingCodeword(value: Int): IntArray {
        val parityPositions = setOf(1, 2, 4, 8)
        val word = IntArray(16)
        var cursor = 0
        for (position in 1..15) if (position !in parityPositions) {
            word[position] = (value ushr (10 - cursor)) and 1
            cursor++
        }
        for (parityPosition in intArrayOf(1, 2, 4, 8)) {
            var parity = 0
            for (position in 1..15) if ((position and parityPosition) != 0 && position != parityPosition) {
                parity = parity xor word[position]
            }
            word[parityPosition] = parity
        }
        return IntArray(15) { word[it + 1] }
    }

    private fun viterbiDecode(llrs: FloatArray, outputBytes: Int): ByteArray {
        val steps = outputBytes * 8 + CONV_K - 1
        require(llrs.size == steps * 2) { "Convolutional FEC length mismatch" }
        val states = 1 shl (CONV_K - 1)
        val negInf = -1e30
        var metrics = DoubleArray(states) { negInf }.also { it[0] = 0.0 }
        val predecessors = Array(steps) { IntArray(states) }
        val decisions = Array(steps) { ByteArray(states) }
        for (step in 0 until steps) {
            val nextMetrics = DoubleArray(states) { negInf }
            val a = llrs[step * 2].toDouble()
            val b = llrs[step * 2 + 1].toDouble()
            for (state in 0 until states) {
                val base = metrics[state]
                if (base <= negInf / 2) continue
                for (bit in 0..1) {
                    val register = (state shl 1) or bit
                    val next = register and (states - 1)
                    val out0 = Integer.bitCount(register and POLYS[0]) and 1
                    val out1 = Integer.bitCount(register and POLYS[1]) and 1
                    val score = base + (if (out0 == 1) a else -a) + (if (out1 == 1) b else -b)
                    if (score > nextMetrics[next]) {
                        nextMetrics[next] = score
                        predecessors[step][next] = state
                        decisions[step][next] = bit.toByte()
                    }
                }
            }
            metrics = nextMetrics
        }
        require(metrics[0] > negInf / 2) { "Convolutional decoder could not reach terminated state" }
        val decodedBits = IntArray(steps)
        var state = 0
        for (step in steps - 1 downTo 0) {
            decodedBits[step] = decisions[step][state].toInt()
            state = predecessors[step][state]
        }
        require(state == 0) { "Viterbi traceback did not reach initial state" }
        return bitsToBytes(decodedBits.take(outputBytes * 8))
    }

    private fun deinterleave(values: FloatArray, depth: Int): FloatArray {
        if (values.size <= depth) return values.copyOf()
        val rows = (values.size + depth - 1) / depth
        val matrix = Array(rows) { FloatArray(depth) }
        var cursor = 0
        for (column in 0 until depth) for (row in 0 until rows) {
            val index = row * depth + column
            if (index < values.size) matrix[row][column] = values[cursor++]
        }
        val result = FloatArray(values.size)
        cursor = 0
        for (row in 0 until rows) for (column in 0 until depth) {
            if (row * depth + column < values.size) result[cursor++] = matrix[row][column]
        }
        return result
    }

    private fun bytesToBits(bytes: ByteArray): List<Int> =
        bytes.flatMap { byte -> (7 downTo 0).map { (byte.toInt() ushr it) and 1 } }

    private fun bitsToBytes(bits: List<Int>): ByteArray {
        require(bits.size % 8 == 0)
        return ByteArray(bits.size / 8) { index ->
            var value = 0
            for (bit in 0 until 8) value = (value shl 1) or bits[index * 8 + bit]
            value.toByte()
        }
    }

    private fun crc32(bytes: ByteArray): Long = CRC32().apply { update(bytes) }.value
}
