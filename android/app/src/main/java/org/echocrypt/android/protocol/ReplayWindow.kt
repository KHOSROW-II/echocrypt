package org.echocrypt.android.protocol

/**
 * Bounded sliding replay window matching the desktop ECHOCRYPT default (1024).
 *
 * Call acceptAuthenticated() only after the packet's AEAD tag has been verified.
 * Failed authentication must never consume a sequence number.
 */
class ReplayWindow(windowSize: Int = DEFAULT_WINDOW_SIZE) {
    companion object {
        const val DEFAULT_WINDOW_SIZE = 1024
    }

    private val seen: BooleanArray
    private var highest = -1L

    init {
        require(windowSize in 1..65_536) { "Replay window must be between 1 and 65536" }
        seen = BooleanArray(windowSize)
    }

    val size: Int get() = seen.size

    @Synchronized
    fun wouldAccept(sequence: Long): Boolean {
        if (sequence < 0L) return false
        if (highest < 0L || sequence > highest) return true
        val distance = highest - sequence
        return distance < seen.size && !seen[distance.toInt()]
    }

    /**
     * Commits a sequence after successful authentication. Returns false for
     * duplicates, stale packets, and invalid negative values.
     */
    @Synchronized
    fun acceptAuthenticated(sequence: Long): Boolean {
        if (!wouldAccept(sequence)) return false
        if (highest < 0L) {
            highest = sequence
            seen.fill(false)
            seen[0] = true
            return true
        }
        if (sequence > highest) {
            val shift = sequence - highest
            if (shift >= seen.size) {
                seen.fill(false)
            } else {
                for (index in seen.lastIndex downTo 0) {
                    val source = index - shift.toInt()
                    seen[index] = source >= 0 && seen[source]
                }
            }
            highest = sequence
            seen[0] = true
            return true
        }
        seen[(highest - sequence).toInt()] = true
        return true
    }

    @Synchronized
    fun reset() {
        highest = -1L
        seen.fill(false)
    }
}
