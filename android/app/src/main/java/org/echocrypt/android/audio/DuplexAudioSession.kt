package org.echocrypt.android.audio

import android.media.AudioAttributes
import android.media.AudioFormat
import android.media.AudioRecord
import android.media.AudioTrack
import android.media.MediaRecorder
import org.echocrypt.android.EchocryptSettings
import android.media.audiofx.AcousticEchoCanceler
import android.media.audiofx.NoiseSuppressor
import java.util.concurrent.ArrayBlockingQueue
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicBoolean
import kotlin.math.max

/**
 * Best-effort simultaneous 48 kHz mono capture/playback.
 *
 * Capture and playback have independent worker threads. Playback submissions are
 * bounded; callers receive false instead of silently blocking the UI/capture path.
 * AEC/NS are optional platform effects and are enabled only when the device exposes them.
 */
class DuplexAudioSession(
    private val onPcm: (FloatArray) -> Unit,
    private val onError: (String) -> Unit,
    private val onInputLevel: (Float) -> Unit = {},
    private val settings: EchocryptSettings = EchocryptSettings()
) {
    companion object {
        private const val RATE = 48_000
        private const val QUEUE_CAPACITY = 32
        private const val CHUNK_SAMPLES = 960 // 20 ms at 48 kHz
    }

    private val running = AtomicBoolean(false)
    private data class PlaybackItem(val samples: ShortArray, val onPlayed: (() -> Unit)?)
    private val playbackQueue = ArrayBlockingQueue<PlaybackItem>(QUEUE_CAPACITY)
    @Volatile private var openedOutput: AudioOutputFactory.OpenedTrack? = null
    @Volatile private var recorder: AudioRecord? = null
    @Volatile private var track: AudioTrack? = null
    @Volatile private var captureThread: Thread? = null
    @Volatile private var playbackThread: Thread? = null
    @Volatile private var echoCanceler: AcousticEchoCanceler? = null
    @Volatile private var noiseSuppressor: NoiseSuppressor? = null

    val isRunning: Boolean get() = running.get()
    val supportsEchoCancellation: Boolean get() = echoCanceler != null
    val supportsNoiseSuppression: Boolean get() = noiseSuppressor != null

    @Synchronized
    fun start() {
        check(running.compareAndSet(false, true)) { "Duplex audio is already running" }
        try {
            val inputMin = AudioRecord.getMinBufferSize(
                RATE, AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT
            )
            check(inputMin > 0) { "48 kHz mono microphone capture is unsupported" }

            val inputBuffer = max(inputMin * 2, CHUNK_SAMPLES * 4)
            var input = if (settings.voiceCommunicationInput) runCatching {
                createRecorder(MediaRecorder.AudioSource.VOICE_COMMUNICATION, inputBuffer)
            }.getOrNull() else null
            if (input == null || input.state != AudioRecord.STATE_INITIALIZED) {
                input?.release()
                input = createRecorder(MediaRecorder.AudioSource.MIC, inputBuffer)
            }
            check(input.state == AudioRecord.STATE_INITIALIZED) {
                input.release()
                "Could not initialize microphone for duplex audio"
            }
            // Publish input immediately so any later setup failure is cleaned up by stop().
            recorder = input

            val opened = AudioOutputFactory.open(
                preferredSampleRate = settings.preferredOutputSampleRate,
                requestedBufferFrames = CHUNK_SAMPLES * 4,
                usage = AudioAttributes.USAGE_VOICE_COMMUNICATION,
                contentType = AudioAttributes.CONTENT_TYPE_SPEECH
            )
            openedOutput = opened
            val output = opened.track
            track = output
            if (settings.echoCancellationEnabled && AcousticEchoCanceler.isAvailable()) {
                echoCanceler = runCatching {
                    AcousticEchoCanceler.create(input.audioSessionId)?.apply { enabled = true }
                }.getOrNull()
            }
            if (settings.noiseSuppressionEnabled && NoiseSuppressor.isAvailable()) {
                noiseSuppressor = runCatching {
                    NoiseSuppressor.create(input.audioSessionId)?.apply { enabled = true }
                }.getOrNull()
            }

            output.play()
            input.startRecording()
            captureThread = Thread({ captureLoop(input) }, "echocrypt-duplex-capture").apply {
                isDaemon = true
                start()
            }
            playbackThread = Thread({ playbackLoop(output) }, "echocrypt-duplex-playback").apply {
                isDaemon = true
                start()
            }
        } catch (e: Exception) {
            stop()
            throw e
        }
    }

    private fun createRecorder(source: Int, bufferBytes: Int) = AudioRecord(
        source, RATE, AudioFormat.CHANNEL_IN_MONO,
        AudioFormat.ENCODING_PCM_16BIT, bufferBytes
    )

    /** Queue already-modulated PCM for playback. Returns false when the bounded queue is full. */
    fun enqueuePlayback(samples: FloatArray, onPlayed: (() -> Unit)? = null): Boolean {
        if (!running.get() || samples.isEmpty()) return false
        val pcm = ShortArray(samples.size) { i ->
            (samples[i].coerceIn(-1f, 1f) * Short.MAX_VALUE).toInt().toShort()
        }
        return playbackQueue.offer(PlaybackItem(pcm, onPlayed))
    }

    private fun captureLoop(input: AudioRecord) {
        val buffer = ShortArray(CHUNK_SAMPLES)
        try {
            while (running.get() && !Thread.currentThread().isInterrupted) {
                val count = input.read(buffer, 0, buffer.size, AudioRecord.READ_BLOCKING)
                if (count < 0) error("Duplex microphone read failed: $count")
                if (count > 0) {
                    var energy = 0.0
                    for (i in 0 until count) {
                        val value = buffer[i] / 32768.0
                        energy += value * value
                    }
                    val rms = kotlin.math.sqrt(energy / count)
                    val db = if (rms <= 0.0) -90f else (20.0 * kotlin.math.log10(rms)).toFloat()
                    onInputLevel(db.coerceIn(-90f, 0f))
                    onPcm(FloatArray(count) { buffer[it] / 32768f })
                }
            }
        } catch (e: Exception) {
            if (running.get()) onError(e.message ?: "Duplex capture failed")
        }
    }

    private fun playbackLoop(output: AudioTrack) {
        var submittedFrames = 0L
        try {
            while (running.get() && !Thread.currentThread().isInterrupted) {
                val item = playbackQueue.poll(100, TimeUnit.MILLISECONDS) ?: continue
                val opened = checkNotNull(openedOutput) { "Audio output format is unavailable" }
                val converted = AudioOutputFactory.convertPcm16(
                    item.samples, opened.sampleRate, opened.channelCount, RATE
                )
                var offset = 0
                while (offset < converted.size && running.get()) {
                    val written = output.write(converted, offset, converted.size - offset, AudioTrack.WRITE_BLOCKING)
                    check(written > 0) { "Duplex AudioTrack write failed: $written" }
                    offset += written
                }
                submittedFrames += (converted.size / opened.channelCount).toLong()
                if (running.get() && item.onPlayed != null) {
                    val deadline = System.currentTimeMillis() +
                        (converted.size.toLong() * 1000L / opened.channelCount / opened.sampleRate) + 2_000L
                    while (running.get() && output.playbackHeadPosition.toLong() < submittedFrames &&
                        System.currentTimeMillis() < deadline) Thread.sleep(10)
                    check(output.playbackHeadPosition.toLong() >= submittedFrames) {
                        "Audio playback completion timed out"
                    }
                    item.onPlayed.invoke()
                }
            }
        } catch (e: Exception) {
            if (running.get()) onError(e.message ?: "Duplex playback failed")
        }
    }

    @Synchronized
    fun stop() {
        if (!running.getAndSet(false) && recorder == null && track == null) return
        playbackQueue.clear()

        val capture = captureThread
        val playback = playbackThread
        capture?.interrupt()
        playback?.interrupt()

        // Stop the devices before joining so blocking read/write calls are released.
        recorder?.let { input ->
            try { input.stop() } catch (_: IllegalStateException) { }
        }
        track?.let { output ->
            if (output.state == AudioTrack.STATE_INITIALIZED) {
                try {
                    if (output.playState == AudioTrack.PLAYSTATE_PLAYING) output.pause()
                    output.flush()
                    if (output.playState != AudioTrack.PLAYSTATE_STOPPED) output.stop()
                } catch (_: IllegalStateException) {
                    // Device teardown may race with a platform audio-service reset.
                }
            }
        }

        if (capture !== Thread.currentThread()) runCatching { capture?.join(1_000) }
        if (playback !== Thread.currentThread()) runCatching { playback?.join(1_000) }

        recorder?.release()
        track?.release()
        echoCanceler?.release()
        noiseSuppressor?.release()
        recorder = null
        track = null
        openedOutput = null
        echoCanceler = null
        noiseSuppressor = null
        captureThread = null
        playbackThread = null
    }

    fun release() = stop()
}
