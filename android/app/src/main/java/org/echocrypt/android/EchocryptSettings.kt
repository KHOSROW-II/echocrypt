package org.echocrypt.android

import android.content.Context

data class EchocryptSettings(
    val echoCancellationEnabled: Boolean = true,
    val noiseSuppressionEnabled: Boolean = true,
    val preferredOutputSampleRate: Int = 0,
    val voiceCommunicationInput: Boolean = true,
    val speakerTestFrequencyHz: Int = 1800,
    val speakerTestDurationMs: Int = 700,
    /** Must match on both peers. ECF3 waveform rate remains fixed at 200 baud. */
    val directPaddingBlock: Int = 0
) {
    fun validate(): EchocryptSettings {
        require(preferredOutputSampleRate in SUPPORTED_OUTPUT_RATES) { "Unsupported output sample rate" }
        require(speakerTestFrequencyHz in 100..10_000) { "Test tone frequency must be 100–10,000 Hz" }
        require(speakerTestDurationMs in 100..5_000) { "Test tone duration must be 100–5,000 ms" }
        require(directPaddingBlock in ALLOWED_PADDING) { "Unsupported Direct padding block" }
        return this
    }

    companion object {
        val SUPPORTED_OUTPUT_RATES = setOf(0, 48_000, 44_100, 32_000, 24_000, 22_050, 16_000)
        val ALLOWED_PADDING = setOf(0, 32, 64, 128, 256)
    }
}

class EchocryptSettingsStore(context: Context) {
    private val preferences = context.applicationContext.getSharedPreferences(
        "echocrypt_native_settings", Context.MODE_PRIVATE
    )

    fun load(): EchocryptSettings {
        val defaults = EchocryptSettings()
        return EchocryptSettings(
            echoCancellationEnabled = preferences.getBoolean("aec", defaults.echoCancellationEnabled),
            noiseSuppressionEnabled = preferences.getBoolean("noise_suppression", defaults.noiseSuppressionEnabled),
            preferredOutputSampleRate = preferences.getInt("output_rate", defaults.preferredOutputSampleRate),
            voiceCommunicationInput = preferences.getBoolean("voice_input", defaults.voiceCommunicationInput),
            speakerTestFrequencyHz = preferences.getInt("test_frequency", defaults.speakerTestFrequencyHz),
            speakerTestDurationMs = preferences.getInt("test_duration", defaults.speakerTestDurationMs),
            directPaddingBlock = preferences.getInt("direct_padding", defaults.directPaddingBlock)
        ).validate()
    }

    fun save(settings: EchocryptSettings) {
        val value = settings.validate()
        preferences.edit()
            .putBoolean("aec", value.echoCancellationEnabled)
            .putBoolean("noise_suppression", value.noiseSuppressionEnabled)
            .putInt("output_rate", value.preferredOutputSampleRate)
            .putBoolean("voice_input", value.voiceCommunicationInput)
            .putInt("test_frequency", value.speakerTestFrequencyHz)
            .putInt("test_duration", value.speakerTestDurationMs)
            .putInt("direct_padding", value.directPaddingBlock)
            .apply()
    }
}
