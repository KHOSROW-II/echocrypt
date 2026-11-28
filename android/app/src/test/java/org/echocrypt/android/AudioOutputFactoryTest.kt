package org.echocrypt.android

import org.echocrypt.android.audio.AudioOutputFactory
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

class AudioOutputFactoryTest {
    @Test fun resampling44100PreservesFrameCount() {
        val source = ShortArray(48_000) { if (it % 2 == 0) 1000 else -1000 }
        assertEquals(44_100, AudioOutputFactory.convertPcm16(source, 44_100, 1).size)
    }
    @Test fun stereoFallbackDuplicatesMonoSamples() {
        val output = AudioOutputFactory.convertPcm16(shortArrayOf(100, -200, 300), 48_000, 2)
        assertEquals(6, output.size)
        assertEquals(output[0], output[1])
        assertEquals(output[2], output[3])
        assertEquals(output[4], output[5])
    }
    @Test fun nonEmptyInputNeverProducesEmptyOutput() {
        assertTrue(AudioOutputFactory.convertPcm16(shortArrayOf(123), 16_000, 1).isNotEmpty())
    }
}
