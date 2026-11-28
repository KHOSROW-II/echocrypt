package org.echocrypt.android.audio

import android.media.AudioAttributes
import android.media.AudioFormat
import android.media.AudioRecord
import android.media.AudioTrack
import android.media.MediaRecorder
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
    private val onInputLevel: (Float) -> Unit = {}
) {
    companion object {
        private const val RATE = 48_000
        private const val QUEUE_CAPACITY = 32
        private const val CHUNK_SAMPLES = 960 // 20 ms at 48 kHz
    }

    private val running = AtomicBoolean(false)
    private val playbackQueue = ArrayBlockingQueue<ShortArray>(QUEUE_CAPACITY)
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
            val outputMin = AudioTrack.getMinBufferSize(
                RATE, AudioFormat.CHANNEL_OUT_MONO, AudioFormat.ENCODING_PCM_16BIT
            )
            check(inputMin > 0 && outputMin > 0) { "48 kHz mono full-duplex audio is unsupported" }

            val inputBuffer = max(inputMin * 2, CHUNK_SAMPLES * 4)
            var input = createRecorder(MediaRecorder.AudioSource.VOICE_COMMUNICATION, inputBuffer)
            if (input.state != AudioRecord.STATE_INITIALIZED) {
                input.release()
                input = createRecorder(MediaRecorder.AudioSource.MIC, inputBuffer)
            }
            check(input.state == AudioRecord.STATE_INITIALIZED) {
                input.release()
                "Could not initialize microphone for duplex audio"
            }

            val output = AudioTrack.Builder()
                .setAudioAttributes(AudioAttributes.Builder()
                    .setUsage(AudioAttributes.USAGE_VOICE_COMMUNICATION)
                    .setContentType(AudioAttributes.CONTENT_TYPE_SPEECH).build())
                .setAudioFormat(AudioFormat.Builder().setSampleRate(RATE)
                    .setEncoding(AudioFormat.ENCODING_PCM_16BIT)
                    .setChannelMask(AudioFormat.CHANNEL_OUT_MONO).build())
                .setBufferSizeInBytes(max(outputMin * 2, CHUNK_SAMPLES * 4))
                .setTransferMode(AudioTrack.MODE_STREAM).build()
            check(output.state == AudioTrack.STATE_INITIALIZED) {
                input.release()
                output.release()
                "Could not initialize speaker for duplex audio"
            }

            recorder = input
            track = output
            if (AcousticEchoCanceler.isAvailable()) {
                echoCanceler = runCatching {
                    AcousticEchoCanceler.create(input.audioSessionId)?.apply { enabled = true }
                }.getOrNull()
            }
            if (NoiseSuppressor.isAvailable()) {
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
    fun enqueuePlayback(samples: FloatArray): Boolean {
        if (!running.get() || samples.isEmpty()) return false
        val pcm = ShortArray(samples.size) { i ->
            (samples[i].coerceIn(-1f, 1f) * Short.MAX_VALUE).toInt().toShort()
        }
        return playbackQueue.offer(pcm)
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
        try {
            while (running.get() && !Thread.currentThread().isInterrupted) {
                val chunk = playbackQueue.poll(100, TimeUnit.MILLISECONDS) ?: continue
                var offset = 0
                while (offset < chunk.size && running.get()) {
                    val written = output.write(
                        chunk, offset, chunk.size - offset, AudioTrack.WRITE_BLOCKING
                    )
                    check(written > 0) { "Duplex AudioTrack write failed: $written" }
                    offset += written
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
        captureThread?.interrupt()
        playbackThread?.interrupt()
        recorder?.let { input ->
            try { input.stop() } catch (_: IllegalStateException) { }
            input.release()
        }
        track?.let { output ->
            if (output.state == AudioTrack.STATE_INITIALIZED &&
                output.playState == AudioTrack.PLAYSTATE_PLAYING) {
                try { output.stop() } catch (_: IllegalStateException) { }
            }
            output.release()
        }
        echoCanceler?.release()
        noiseSuppressor?.release()
        recorder = null
        track = null
        echoCanceler = null
        noiseSuppressor = null
        captureThread = null
        playbackThread = null
    }

    fun release() = stop()
}
