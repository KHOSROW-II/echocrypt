package org.echocrypt.android.protocol

/**
 * Incremental ECF3 receiver for chunked PCM input.
 *
 * It tolerates arbitrary AudioRecord chunk boundaries and concatenated frames.
 * Detection uses a coarse candidate scan, followed by local sample-level
 * refinement against the known sync word. Symbol-clock drift / resampling
 * recovery is not implemented here; callers should feed native 48 kHz PCM.
 */
class NativeStreamReceiver(
    private val candidateStride: Int = 8,
    private val maxBufferedSamples: Int = 4_500_000
) {
    data class ReceivedFrame(
        val payload: ByteArray,
        val startOffsetSamples: Long,
        val frameSamples: Int
    )

    private var buffer = FloatArray(0)
    private var absoluteBase = 0L
    private var nextCandidate = 0

    init {
        require(candidateStride in 1..240)
        require(maxBufferedSamples >= 100_000)
    }

    @Synchronized
    fun reset() {
        buffer = FloatArray(0)
        absoluteBase = 0L
        nextCandidate = 0
    }

    /**
     * Appends one PCM chunk and returns every complete frame found in it.
     * The input is copied; the caller may safely reuse its capture buffer.
     */
    @Synchronized
    fun push(chunk: FloatArray): List<ReceivedFrame> {
        if (chunk.isEmpty()) return emptyList()
        append(chunk)
        val output = ArrayList<ReceivedFrame>()
        val minimumFrame = (64 + 330) * 240
        val maximumFrame = (64 + 330 + (1_028 * 8 + 6) * 2) * 240 + 96

        var waitingForPayload = false
        while (buffer.size - nextCandidate >= minimumFrame) {
            var decoded: ByteArray? = null
            try {
                decoded = NativeDemodulator.decodeAligned(buffer, nextCandidate)
            } catch (error: IllegalArgumentException) {
                // A valid sync/header with an incomplete payload must remain
                // buffered until the next audio chunk arrives.
                if (error.message?.startsWith("Truncated ECF3 payload") == true) {
                    waitingForPayload = true
                    break
                }
            }

            if (decoded != null) {
                val frameSamples = (64 + 330 + ((decoded.size + 4) * 8 + 6) * 2) * 240 + 96
                val refinedStart = refineStart(nextCandidate)
                output += ReceivedFrame(
                    payload = decoded,
                    startOffsetSamples = absoluteBase + refinedStart,
                    frameSamples = frameSamples
                )
                val consumed = refinedStart + frameSamples
                buffer = buffer.copyOfRange(minOf(consumed, buffer.size), buffer.size)
                absoluteBase += consumed
                nextCandidate = 0
                continue
            }

            nextCandidate += candidateStride
        }

        // Retain enough trailing PCM to catch a sync marker split across chunks.
        if (nextCandidate > 0 && !waitingForPayload) {
            val discard = minOf(nextCandidate, buffer.size)
            buffer = buffer.copyOfRange(discard, buffer.size)
            absoluteBase += discard
            nextCandidate = 0
        }
        if (buffer.size > maxBufferedSamples) {
            val discard = buffer.size - maxBufferedSamples
            buffer = buffer.copyOfRange(discard, buffer.size)
            absoluteBase += discard
        }
        // Avoid holding an oversized, non-frame stream indefinitely.
        if (buffer.size > maximumFrame && nextCandidate == 0) {
            val discard = buffer.size - maximumFrame
            buffer = buffer.copyOfRange(discard, buffer.size)
            absoluteBase += discard
        }
        return output
    }

    /**
     * The decoder may tolerate a few samples of phase error. Once CRC has
     * authenticated the frame, use the known sync word to recover its precise
     * start within the nearby coarse-search window.
     */
    private fun refineStart(candidate: Int): Int {
        val syncSamples = 64 * 240
        val first = maxOf(0, candidate - candidateStride)
        val last = minOf(
            buffer.size - syncSamples,
            candidate + candidateStride * 2
        )
        if (last < first) return candidate

        var bestOffset = candidate
        var bestScore = Double.NEGATIVE_INFINITY
        for (offset in first..last) {
            val score = NativeDemodulator.syncConfidence(buffer, offset)
            if (score > bestScore) {
                bestScore = score
                bestOffset = offset
            }
        }
        return bestOffset
    }

    private fun append(chunk: FloatArray) {
        val combined = FloatArray(buffer.size + chunk.size)
        buffer.copyInto(combined)
        chunk.copyInto(combined, buffer.size)
        buffer = combined
    }

    @Synchronized
    fun bufferedSamples(): Int = buffer.size
}
