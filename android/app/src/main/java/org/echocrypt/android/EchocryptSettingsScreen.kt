package org.echocrypt.android

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.ColumnScope
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.DropdownMenu
import androidx.compose.material3.DropdownMenuItem
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Switch
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalClipboardManager
import androidx.compose.ui.text.AnnotatedString
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp

@Composable
fun EchocryptSettingsScreen(settings: EchocryptSettings, logDirectoryPath: String,
                            onChange: (EchocryptSettings) -> Unit) {
    val clipboard = LocalClipboardManager.current
    Column(Modifier.fillMaxWidth().verticalScroll(rememberScrollState()).padding(18.dp),
        verticalArrangement = Arrangement.spacedBy(14.dp)) {
        Text("Settings", color = Color.White, fontSize = 23.sp, fontWeight = FontWeight.Bold)
        Text("Runtime settings persist on this device. Audio settings apply on the next Full Duplex start; Direct padding applies to the next handshake.",
            color = Color(0xFF8995A7), fontSize = 12.sp, lineHeight = 17.sp)
        SettingsCard("Audio processing") {
            SettingsSwitch("Acoustic echo cancellation (AEC)", settings.echoCancellationEnabled) {
                onChange(settings.copy(echoCancellationEnabled = it))
            }
            SettingsSwitch("Noise suppression", settings.noiseSuppressionEnabled) {
                onChange(settings.copy(noiseSuppressionEnabled = it))
            }
            SettingsChoice("Microphone source",
                if (settings.voiceCommunicationInput) "Voice communication (AEC-friendly)" else "Microphone (raw)",
                listOf("Voice communication (AEC-friendly)" to true, "Microphone (raw)" to false)) {
                onChange(settings.copy(voiceCommunicationInput = it))
            }
            SettingsChoice("Preferred speaker rate", outputRateLabel(settings.preferredOutputSampleRate),
                listOf("Automatic · 48 kHz first" to 0, "48,000 Hz" to 48_000,
                    "44,100 Hz" to 44_100, "32,000 Hz" to 32_000, "24,000 Hz" to 24_000,
                    "22,050 Hz" to 22_050, "16,000 Hz" to 16_000)) {
                onChange(settings.copy(preferredOutputSampleRate = it))
            }
            Text("When a device rejects 48 kHz mono output, the engine retries common rates and stereo, then resamples the 48 kHz modem waveform without changing its tones.",
                color = Color(0xFF8995A7), fontSize = 10.sp, lineHeight = 15.sp)
        }
        SettingsCard("Speaker test") {
            NumericSetting("Test frequency (Hz)", settings.speakerTestFrequencyHz, 100..10_000) {
                onChange(settings.copy(speakerTestFrequencyHz = it))
            }
            NumericSetting("Test duration (ms)", settings.speakerTestDurationMs, 100..5_000) {
                onChange(settings.copy(speakerTestDurationMs = it))
            }
        }
        SettingsCard("Direct encrypted transport") {
            SettingsChoice("ECP1 padding block",
                if (settings.directPaddingBlock == 0) "Disabled" else settings.directPaddingBlock.toString() + " bytes",
                listOf("Disabled" to 0, "32 bytes" to 32, "64 bytes" to 64,
                    "128 bytes" to 128, "256 bytes" to 256)) {
                onChange(settings.copy(directPaddingBlock = it))
            }
            Text("Both peers must use the same padding setting. Changing it only affects a new handshake.",
                color = Color(0xFFFFC56A), fontSize = 10.sp, lineHeight = 15.sp)
            SettingsValue("ECF3 modem", "200 baud · fixed in this Android modem")
            SettingsValue("MARK / SPACE tones", "1,800 / 2,400 Hz")
            SettingsValue("Frequency hopping", "Unavailable in this native waveform path")
            SettingsValue("Ultrasonic mode", "Unavailable in this native waveform path")
            SettingsValue("Acoustic workers", "1 · concurrent playback is not enabled")
        }
        SettingsCard("File transfer") {
            SettingsValue("Maximum file size", "10 MiB")
            SettingsValue("Chunk size", "1,400 bytes")
            SettingsValue("Resume", "Persistent state + reconnect negotiation")
            SettingsValue("Integrity", "SHA-256 + COMPLETE")
        }
        SettingsCard("Event logs") {
            Text("App, audio, Direct, Broadcast, file-transfer, settings and error events are appended automatically. Chat bodies, private keys and file contents are excluded.",
                color = Color(0xFF8995A7), fontSize = 11.sp, lineHeight = 16.sp)
            Text(logDirectoryPath, color = Color.White, fontSize = 10.sp, lineHeight = 15.sp)
            OutlinedButton(onClick = { clipboard.setText(AnnotatedString(logDirectoryPath)) },
                modifier = Modifier.fillMaxWidth()) { Text("Copy log folder path") }
        }
        Text("ECHOCRYPT Native Android · Debug configuration", color = Color(0xFF8995A7), fontSize = 9.sp)
    }
}

@Composable
private fun SettingsCard(title: String, content: @Composable ColumnScope.() -> Unit) {
    Card(colors = CardDefaults.cardColors(containerColor = Color(0xFF151D29)),
        shape = RoundedCornerShape(16.dp), modifier = Modifier.fillMaxWidth()) {
        Column(Modifier.fillMaxWidth().padding(15.dp), verticalArrangement = Arrangement.spacedBy(10.dp)) {
            Text(title.uppercase(), color = Color(0xFFE5B85C), fontWeight = FontWeight.Bold, fontSize = 12.sp)
            content()
        }
    }
}

@Composable
private fun SettingsSwitch(label: String, checked: Boolean, onChange: (Boolean) -> Unit) {
    Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
        Text(label, color = Color.White, fontSize = 12.sp, modifier = Modifier.weight(1f))
        Switch(checked = checked, onCheckedChange = onChange)
    }
}

@Composable
private fun <T> SettingsChoice(label: String, current: String, options: List<Pair<String, T>>,
                               onSelected: (T) -> Unit) {
    var expanded by remember { mutableStateOf(false) }
    Column(verticalArrangement = Arrangement.spacedBy(4.dp)) {
        Text(label, color = Color(0xFF8995A7), fontSize = 11.sp)
        OutlinedButton(onClick = { expanded = true }, modifier = Modifier.fillMaxWidth()) {
            Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween) {
                Text(current, color = Color.White, fontSize = 11.sp)
                Text("▾", color = Color(0xFFE5B85C))
            }
        }
        DropdownMenu(expanded = expanded, onDismissRequest = { expanded = false }) {
            options.forEach { (labelText, value) ->
                DropdownMenuItem(text = { Text(labelText) }, onClick = {
                    expanded = false
                    onSelected(value)
                })
            }
        }
    }
}

@Composable
private fun NumericSetting(label: String, value: Int, range: IntRange, onValidValue: (Int) -> Unit) {
    var text by remember(value) { mutableStateOf(value.toString()) }
    OutlinedTextField(value = text, onValueChange = { input ->
        val filtered = input.filter(Char::isDigit).take(5)
        text = filtered
        filtered.toIntOrNull()?.takeIf { it in range }?.let(onValidValue)
    }, modifier = Modifier.fillMaxWidth(), label = { Text(label) }, singleLine = true)
    Text("Allowed range: " + range.first + "–" + range.last, color = Color(0xFF8995A7), fontSize = 9.sp)
}

@Composable
private fun SettingsValue(label: String, value: String) {
    Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween,
        verticalAlignment = Alignment.CenterVertically) {
        Text(label, color = Color(0xFF8995A7), fontSize = 10.sp, modifier = Modifier.weight(1f))
        Spacer(Modifier.padding(horizontal = 5.dp))
        Text(value, color = Color.White, fontSize = 10.sp, fontWeight = FontWeight.SemiBold)
    }
}

private fun outputRateLabel(rate: Int): String =
    if (rate == 0) "Automatic · 48 kHz first" else "%,d Hz".format(rate)
