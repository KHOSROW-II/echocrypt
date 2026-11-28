package org.echocrypt.android.protocol

/**
 * Incremental ECF3 receiver for chunked PCM input.
 *
 * It tolerates arbitrary AudioRecord chunk boundaries and concatenated frames.
 * Detection currently performs a coarse 8-sample start search and then delegates
 * to the fixed-clock aligned decoder. Clock drift / resampling recovery is not
 * implemented here; callers should feed native 48 kHz PCM.
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

        var waitingForPayload = false\n        while (buffer.size - nextCandidate >= minimumFrame) {
            var decoded: ByteArray? = null
            try {
                decoded = NativeDemodulator.decodeAligned(buffer, nextCandidate)
            } catch (error: IllegalArgumentException) {
                // A valid sync/header with an incomplete payload must remain
                // buffered until the next audio chunk arrives.
                if (error.message?.startsWith("Truncated ECF3 payload") == true) {\n                    waitingForPayload = true\n                    break\n                }
            }

            if (decoded != null) {
                val frameSamples = (64 + 330 + ((decoded.size + 4) * 8 + 6) * 2) * 240 + 96
                output += ReceivedFrame(
                    payload = decoded,
                    startOffsetSamples = absoluteBase + nextCandidate,
                    frameSamples = frameSamples
                )
                val consumed = nextCandidate + frameSamples
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

    private fun append(chunk: FloatArray) {
        val combined = FloatArray(buffer.size + chunk.size)
        buffer.copyInto(combined)
        chunk.copyInto(combined, buffer.size)
        buffer = combined
    }

    @Synchronized
    fun bufferedSamples(): Int = buffer.size
}
