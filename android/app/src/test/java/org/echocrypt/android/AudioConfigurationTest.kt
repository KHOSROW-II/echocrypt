package org.echocrypt.android

import org.echocrypt.android.audio.AndroidAudioEngine
import org.junit.Assert.assertEquals
import org.junit.Test

class AudioConfigurationTest {
    @Test fun baselineSampleRateIs48Khz() {
        assertEquals(48_000, AndroidAudioEngine.SAMPLE_RATE)
    }

    @Test fun captureUsesMonoPcm16() {
        assertEquals(android.media.AudioFormat.CHANNEL_IN_MONO, AndroidAudioEngine.CHANNEL_IN)
        assertEquals(android.media.AudioFormat.ENCODING_PCM_16BIT, AndroidAudioEngine.ENCODING)
    }
}
