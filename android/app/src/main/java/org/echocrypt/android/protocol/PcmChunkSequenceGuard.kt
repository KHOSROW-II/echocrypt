package org.echocrypt.android.protocol

/**
 * Detects missing PCM chunks at the receiver boundary.
 *
 * A dropped chunk invalidates modem timing and buffered frame continuity;
 * callers must reset the stream decoder before feeding the next chunk.
 */
internal class PcmChunkSequenceGuard {
    private var lastSequence: Long? = null

    @Synchronized
    fun requiresReset(sequence: Long): Boolean {
        val previous = lastSequence
        lastSequence = sequence
        return previous != null && sequence != previous + 1L
    }

    @Synchronized
    fun reset() {
        lastSequence = null
    }
}
