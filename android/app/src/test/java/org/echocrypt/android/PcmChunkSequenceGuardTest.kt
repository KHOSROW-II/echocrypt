package org.echocrypt.android

import org.echocrypt.android.protocol.PcmChunkSequenceGuard
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class PcmChunkSequenceGuardTest {
    @Test
    fun contiguousChunksDoNotResetReceiver() {
        val guard = PcmChunkSequenceGuard()
        assertFalse(guard.requiresReset(10))
        assertFalse(guard.requiresReset(11))
        assertFalse(guard.requiresReset(12))
    }

    @Test
    fun droppedChunkResetsOnNextAcceptedChunk() {
        val guard = PcmChunkSequenceGuard()
        assertFalse(guard.requiresReset(100))
        assertTrue(guard.requiresReset(102))
        assertFalse(guard.requiresReset(103))
    }

    @Test
    fun explicitResetStartsNewSequence() {
        val guard = PcmChunkSequenceGuard()
        assertFalse(guard.requiresReset(5))
        guard.reset()
        assertFalse(guard.requiresReset(900))
    }
}
