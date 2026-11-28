package org.echocrypt.android.audio

import android.media.AudioAttributes
import android.media.AudioFormat
import android.media.AudioTrack
import kotlin.math.floor
import kotlin.math.max
import kotlin.math.roundToInt

/** Adaptive Android AudioTrack creation and mono 48 kHz PCM conversion. */
object AudioOutputFactory {
    data class OpenedTrack(
        val track: AudioTrack,
        val sampleRate: Int,
        val channelCount: Int,
        val minimumBufferBytes: Int,
        val diagnostics: List<String>
    ) {
        val description: String
            get() = sampleRate.toString() + " Hz · " + if (channelCount == 1) "mono PCM16" else "stereo PCM16"
    }

    fun open(
        preferredSampleRate: Int = 0,
        requestedBufferFrames: Int = 4096,
        usage: Int = AudioAttributes.USAGE_MEDIA,
        contentType: Int = AudioAttributes.CONTENT_TYPE_MUSIC
    ): OpenedTrack {
        require(preferredSampleRate in setOf(0, 48_000, 44_100, 32_000, 24_000, 22_050, 16_000))
        require(requestedBufferFrames > 0)
        val rates = (listOf(if (preferredSampleRate == 0) 48_000 else preferredSampleRate) +
            listOf(48_000, 44_100, 32_000, 24_000, 22_050, 16_000)).distinct()
        val channels = listOf(AudioFormat.CHANNEL_OUT_MONO to 1, AudioFormat.CHANNEL_OUT_STEREO to 2)
        val attributes = listOf(
            usage to contentType,
            AudioAttributes.USAGE_MEDIA to AudioAttributes.CONTENT_TYPE_MUSIC,
            AudioAttributes.USAGE_VOICE_COMMUNICATION to AudioAttributes.CONTENT_TYPE_SPEECH
        ).distinct()
        val attempts = mutableListOf<String>()

        for (rate in rates) for ((mask, channelCount) in channels) {
            val minimum = try {
                AudioTrack.getMinBufferSize(rate, mask, AudioFormat.ENCODING_PCM_16BIT)
            } catch (failure: Exception) {
                attempts += rate.toString() + "Hz/" + channelCount + "ch min-buffer failed: " +
                    failure.javaClass.simpleName + ": " + failure.message
                continue
            }
            if (minimum <= 0) {
                attempts += rate.toString() + "Hz/" + channelCount + "ch min-buffer=" + minimum
                continue
            }
            for ((candidateUsage, candidateContent) in attributes) {
                var candidate: AudioTrack? = null
                try {
                    val bytes = max(minimum * 2, requestedBufferFrames * channelCount * 2)
                    candidate = AudioTrack.Builder()
                        .setAudioAttributes(
                            AudioAttributes.Builder().setUsage(candidateUsage)
                                .setContentType(candidateContent).build()
                        )
                        .setAudioFormat(
                            AudioFormat.Builder().setSampleRate(rate)
                                .setEncoding(AudioFormat.ENCODING_PCM_16BIT)
                                .setChannelMask(mask).build()
                        )
                        .setBufferSizeInBytes(bytes)
                        .setTransferMode(AudioTrack.MODE_STREAM)
                        .build()
                    if (candidate.state == AudioTrack.STATE_INITIALIZED) {
                        return OpenedTrack(candidate, rate, channelCount, minimum, attempts.toList())
                    }
                    attempts += rate.toString() + "Hz/" + channelCount + "ch state=" +
                        candidate.state + " usage=" + candidateUsage
                } catch (failure: Exception) {
                    attempts += rate.toString() + "Hz/" + channelCount + "ch " +
                        failure.javaClass.simpleName + ": " + failure.message
                }
                runCatching { candidate?.release() }
            }
        }
        throw IllegalStateException(
            "Android could not initialize audio output. Tried mono/stereo PCM16 at " +
                rates.joinToString() + " Hz. " + attempts.takeLast(18).joinToString("; ")
        )
    }

    /** Resample while preserving acoustic tones and duration. */
    fun convertPcm16(input: ShortArray, targetSampleRate: Int, targetChannels: Int,
                     sourceSampleRate: Int = 48_000): ShortArray {
        require(targetSampleRate > 0 && sourceSampleRate > 0)
        require(targetChannels == 1 || targetChannels == 2)
        if (input.isEmpty()) return ShortArray(0)
        val targetFrames = max(
            1, ((input.size.toLong() * targetSampleRate + sourceSampleRate / 2) / sourceSampleRate).toInt()
        )
        val output = ShortArray(targetFrames * targetChannels)
        for (frame in 0 until targetFrames) {
            val position = frame.toDouble() * sourceSampleRate / targetSampleRate
            val left = floor(position).toInt().coerceIn(0, input.lastIndex)
            val right = (left + 1).coerceAtMost(input.lastIndex)
            val fraction = (position - left).coerceIn(0.0, 1.0)
            val sample = (input[left].toDouble() +
                (input[right].toDouble() - input[left].toDouble()) * fraction)
                .roundToInt().coerceIn(Short.MIN_VALUE.toInt(), Short.MAX_VALUE.toInt()).toShort()
            val out = frame * targetChannels
            output[out] = sample
            if (targetChannels == 2) output[out + 1] = sample
        }
        return output
    }
}
