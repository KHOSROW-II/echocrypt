package org.echocrypt.android.audio

import android.media.AudioAttributes
import android.media.AudioFormat
import android.media.AudioRecord
import android.media.AudioTrack
import android.media.MediaRecorder
import kotlin.math.log10
import kotlin.math.max
import kotlin.math.sin

/** Native PCM device access. Protocol/modem integration is a separate layer. */
class AndroidAudioEngine {
    companion object {
        const val SAMPLE_RATE = 48_000
        const val CHANNEL_IN = AudioFormat.CHANNEL_IN_MONO
        const val CHANNEL_OUT = AudioFormat.CHANNEL_OUT_MONO
        const val ENCODING = AudioFormat.ENCODING_PCM_16BIT
    }

    @Volatile private var recorder: AudioRecord? = null
    @Volatile private var captureThread: Thread? = null

    @Synchronized
    fun startCapture(
        onLevel: (Float) -> Unit,
        onError: (String) -> Unit,
        onPcm: (FloatArray) -> Unit = {}
    ) {
        check(recorder == null) { "Microphone capture is already running" }
        val minimum = AudioRecord.getMinBufferSize(SAMPLE_RATE, CHANNEL_IN, ENCODING)
        check(minimum > 0) { "48 kHz mono PCM capture is not supported on this device" }
        val input = AudioRecord(
            MediaRecorder.AudioSource.MIC, SAMPLE_RATE, CHANNEL_IN, ENCODING,
            max(minimum * 2, SAMPLE_RATE / 5)
        )
        check(input.state == AudioRecord.STATE_INITIALIZED) {
            input.release()
            "Android could not initialize the microphone"
        }
        recorder = input
        input.startRecording()
        captureThread = Thread({
            val buffer = ShortArray(1024)
            try {
                while (!Thread.currentThread().isInterrupted) {
                    val count = input.read(buffer, 0, buffer.size, AudioRecord.READ_BLOCKING)
                    if (count < 0) error("AudioRecord read failed: $count")
                    if (count > 0) {
                        var energy = 0.0
                        for (i in 0 until count) {
                            val sample = buffer[i] / 32768.0
                            energy += sample * sample
                        }
                        val rms = kotlin.math.sqrt(energy / count)
                        val db = if (rms <= 0.0) -90f else (20.0 * log10(rms)).toFloat()
                        onLevel(db.coerceIn(-90f, 0f))
                        // Deliver normalized PCM independently of the reusable ShortArray.
                        onPcm(FloatArray(count) { buffer[it] / 32768f })
                    }
                }
            } catch (e: Exception) {
                if (!Thread.currentThread().isInterrupted) onError(e.message ?: "Microphone capture failed")
            }
        }, "echocrypt-audio-capture").apply { isDaemon = true; start() }
    }

    @Synchronized
    fun stopCapture() {
        val input = recorder ?: return
        recorder = null
        captureThread?.interrupt()
        try { input.stop() } catch (_: IllegalStateException) { }
        input.release()
        captureThread = null
    }

    /** Stream an already-modulated 48 kHz mono waveform to Android output. */
    fun transmitPcm(samples: FloatArray) {
        require(samples.isNotEmpty()) { "PCM transmission cannot be empty" }
        val pcm = ShortArray(samples.size) { index ->
            (samples[index].coerceIn(-1f, 1f) * Short.MAX_VALUE).toInt().toShort()
        }
        val minimum = AudioTrack.getMinBufferSize(SAMPLE_RATE, CHANNEL_OUT, ENCODING)
        check(minimum > 0) { "PCM playback is not supported on this device" }
        val track = AudioTrack.Builder()
            .setAudioAttributes(AudioAttributes.Builder()
                .setUsage(AudioAttributes.USAGE_MEDIA)
                .setContentType(AudioAttributes.CONTENT_TYPE_MUSIC).build())
            .setAudioFormat(AudioFormat.Builder().setSampleRate(SAMPLE_RATE)
                .setEncoding(ENCODING).setChannelMask(CHANNEL_OUT).build())
            .setBufferSizeInBytes(max(minimum, 8_192))
            .setTransferMode(AudioTrack.MODE_STREAM).build()
        try {
            check(track.state == AudioTrack.STATE_INITIALIZED) { "Android could not initialize acoustic output" }
            track.play()
            var offset = 0
            while (offset < pcm.size) {
                val count = track.write(pcm, offset, minOf(4_096, pcm.size - offset), AudioTrack.WRITE_BLOCKING)
                check(count > 0) { "AudioTrack write failed: $count" }
                offset += count
            }
            val deadline = System.currentTimeMillis() + (samples.size * 1_000L / SAMPLE_RATE) + 2_000L
            while (track.playbackHeadPosition.toLong() < pcm.size && System.currentTimeMillis() < deadline) {
                Thread.sleep(10)
            }
            check(track.playbackHeadPosition.toLong() >= pcm.size) { "Audio transmission playback timed out" }
        } finally {
            try { track.stop() } catch (_: IllegalStateException) { }
            track.release()
        }
    }

    fun playTestTone(frequencyHz: Int = 1800, durationMs: Int = 700) {
        require(frequencyHz in 100..10_000)
        require(durationMs in 100..5_000)
        val frames = SAMPLE_RATE * durationMs / 1000
        val pcm = ShortArray(frames) { index ->
            val fade = minOf(1.0, index / 240.0, (frames - index) / 240.0).coerceAtLeast(0.0)
            (sin(2.0 * Math.PI * frequencyHz * index / SAMPLE_RATE) * 0.18 * fade * Short.MAX_VALUE)
                .toInt().toShort()
        }
        val minimum = AudioTrack.getMinBufferSize(SAMPLE_RATE, CHANNEL_OUT, ENCODING)
        check(minimum > 0) { "PCM playback is not supported on this device" }
        val output = AudioTrack.Builder()
            .setAudioAttributes(AudioAttributes.Builder()
                .setUsage(AudioAttributes.USAGE_MEDIA)
                .setContentType(AudioAttributes.CONTENT_TYPE_SONIFICATION).build())
            .setAudioFormat(AudioFormat.Builder().setSampleRate(SAMPLE_RATE)
                .setEncoding(ENCODING).setChannelMask(CHANNEL_OUT).build())
            .setBufferSizeInBytes(max(minimum, pcm.size * 2))
            .setTransferMode(AudioTrack.MODE_STATIC).build()
        try {
            check(output.state == AudioTrack.STATE_INITIALIZED) { "Android could not initialize audio output" }
            val written = output.write(pcm, 0, pcm.size)
            check(written == pcm.size) { "AudioTrack test-tone write was incomplete: $written/${pcm.size}" }
            output.play()
            val until = System.currentTimeMillis() + durationMs + 800
            while (output.playState == AudioTrack.PLAYSTATE_PLAYING && System.currentTimeMillis() < until) {
                Thread.sleep(20)
            }
        } finally {
            // AudioTrack.Builder can return an uninitialized track on some devices.
            // Calling stop() in that state throws and masks the original initialization error.
            if (output.state == AudioTrack.STATE_INITIALIZED) {
                try {
                    if (output.playState == AudioTrack.PLAYSTATE_PLAYING) output.stop()
                } catch (_: IllegalStateException) {
                    // The track may have been stopped by the platform during teardown.
                }
            }
            output.release()
        }
    }

    fun release() = stopCapture()
}
