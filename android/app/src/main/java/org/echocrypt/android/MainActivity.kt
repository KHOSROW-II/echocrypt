package org.echocrypt.android

import android.Manifest
import android.content.pm.PackageManager
import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.compose.setContent
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Lock
import androidx.compose.material.icons.filled.Mic
import androidx.compose.material.icons.filled.Stop
import androidx.compose.material.icons.filled.VolumeUp
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.core.content.ContextCompat
import org.echocrypt.android.audio.AndroidAudioEngine
import org.echocrypt.android.protocol.NativeModem
import org.echocrypt.android.protocol.NativeStreamReceiver
import java.util.concurrent.ArrayBlockingQueue
import java.util.concurrent.Executors
import java.util.concurrent.RejectedExecutionException
import java.util.concurrent.ThreadPoolExecutor
import java.util.concurrent.TimeUnit

private val Background = Color(0xFF090C12)
private val Panel = Color(0xFF151D29)
private val Gold = Color(0xFFE5B85C)
private val Muted = Color(0xFF8995A7)

class MainActivity : ComponentActivity() {
    private val engine = AndroidAudioEngine()
    private val executor = Executors.newSingleThreadExecutor()
    private val receiveExecutor = ThreadPoolExecutor(
        1, 1, 0L, TimeUnit.MILLISECONDS,
        ArrayBlockingQueue(64),
        ThreadPoolExecutor.AbortPolicy()
    )
    private val streamReceiver = NativeStreamReceiver()
    private var permissionResult: ((Boolean) -> Unit)? = null
    private val permissionRequest = registerForActivityResult(ActivityResultContracts.RequestPermission()) {
        permissionResult?.invoke(it)
        permissionResult = null
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContent {
            var listening by remember { mutableStateOf(false) }
            var level by remember { mutableFloatStateOf(-90f) }
            var status by remember { mutableStateOf("READY") }
            var error by remember { mutableStateOf<String?>(null) }
            var outputError by remember { mutableStateOf<String?>(null) }
            var modemStatus by remember { mutableStateOf("No frame transmitted") }
            var receivedCount by remember { mutableIntStateOf(0) }
            var lastReceived by remember { mutableStateOf("No ECF3 frame received") }
            var receiveError by remember { mutableStateOf<String?>(null) }

            val hasPermission = {
                ContextCompat.checkSelfPermission(this, Manifest.permission.RECORD_AUDIO) ==
                    PackageManager.PERMISSION_GRANTED
            }
            val start = {
                error = null
                receiveError = null
                streamReceiver.reset()
                executor.execute {
                    try {
                        engine.startCapture(
                            onLevel = { value -> runOnUiThread { level = value } },
                            onError = { message ->
                                runOnUiThread { listening = false; status = "AUDIO ERROR"; error = message }
                            },
                            onPcm = { pcm ->
                                try {
                                    receiveExecutor.execute {
                                        val frames = streamReceiver.push(pcm)
                                        if (frames.isNotEmpty()) {
                                            runOnUiThread {
                                                receivedCount += frames.size
                                                lastReceived = frames.joinToString(" · ") { frame ->
                                                    frame.payload.toString(Charsets.UTF_8)
                                                        .replace(Regex("[^\\x20-\\x7E]"), "�")
                                                        .take(96)
                                                }
                                            }
                                        }
                                    }
                                } catch (_: RejectedExecutionException) {
                                    runOnUiThread {
                                        receiveError = "Receive processing overloaded; audio chunks were dropped."
                                    }
                                }
                            }
                        )
                    } catch (e: Exception) {
                        runOnUiThread { listening = false; status = "AUDIO ERROR"; error = e.message }
                    }
                }
            }

            MaterialTheme(colorScheme = darkColorScheme(primary = Gold, background = Background,
                surface = Panel, onPrimary = Background, onBackground = Color.White, onSurface = Color.White)) {
                Surface(Modifier.fillMaxSize(), color = Background) {
                    Column(Modifier.fillMaxSize().verticalScroll(rememberScrollState()).padding(22.dp), verticalArrangement = Arrangement.spacedBy(18.dp)) {
                        Row(verticalAlignment = Alignment.CenterVertically) {
                            Icon(Icons.Default.Lock, null, tint = Gold, modifier = Modifier.size(34.dp))
                            Spacer(Modifier.width(12.dp))
                            Column {
                                Text("ECHOCRYPT", fontSize = 25.sp, fontWeight = FontWeight.Black, letterSpacing = 2.sp)
                                Text("NATIVE ACOUSTIC CLIENT", color = Muted, fontSize = 10.sp, letterSpacing = 2.sp)
                            }
                        }

                        Card(colors = CardDefaults.cardColors(containerColor = Panel),
                            shape = RoundedCornerShape(20.dp), modifier = Modifier.fillMaxWidth()) {
                            Column(Modifier.padding(18.dp), verticalArrangement = Arrangement.spacedBy(14.dp)) {
                                Row(verticalAlignment = Alignment.CenterVertically) {
                                    Text("AUDIO INPUT", color = Gold, fontWeight = FontWeight.Bold, letterSpacing = 1.5.sp)
                                                Text(status, color = Muted, fontSize = 11.sp)
                                }
                                Row(verticalAlignment = Alignment.CenterVertically) {
                                    Icon(Icons.Default.Mic, null, tint = Gold)
                                    Spacer(Modifier.width(10.dp))
                                    Column(Modifier.weight(1f)) {
                                        Text("Microphone")
                                        Text(if (listening) "48 kHz · Mono PCM · Active" else "Stopped",
                                            color = Muted, fontSize = 12.sp)
                                    }
                                    Text("${level.toInt()} dB", color = Gold, fontSize = 12.sp)
                                }
                                Box(Modifier.fillMaxWidth().height(6.dp)
                                    .background(Color(0xFF293547), RoundedCornerShape(8.dp))) {
                                    Box(Modifier.fillMaxWidth(((level + 90f) / 90f).coerceIn(0f, 1f))
                                        .height(6.dp).background(Gold, RoundedCornerShape(8.dp)))
                                }
                                Row(horizontalArrangement = Arrangement.spacedBy(10.dp)) {
                                    Button(onClick = {
                                        if (hasPermission()) {
                                            listening = true; status = "LISTENING"; start()
                                        } else {
                                            permissionResult = { granted ->
                                                if (granted) { listening = true; status = "LISTENING"; start() }
                                                else error = "Microphone permission denied"
                                            }
                                            permissionRequest.launch(Manifest.permission.RECORD_AUDIO)
                                        }
                                    }, enabled = !listening, modifier = Modifier.weight(1f),
                                        colors = ButtonDefaults.buttonColors(containerColor = Gold, contentColor = Background)) {
                                        Icon(Icons.Default.Mic, null); Spacer(Modifier.width(6.dp)); Text("Start")
                                    }
                                    Button(onClick = {
                                        executor.execute { engine.stopCapture() }
                                        listening = false; level = -90f; status = "READY"
                                    }, enabled = listening, modifier = Modifier.weight(1f)) {
                                        Icon(Icons.Default.Stop, null); Spacer(Modifier.width(6.dp)); Text("Stop")
                                    }
                                }
                            }
                        }

                        Card(colors = CardDefaults.cardColors(containerColor = Panel),
                            shape = RoundedCornerShape(20.dp), modifier = Modifier.fillMaxWidth()) {
                            Column(Modifier.padding(18.dp), verticalArrangement = Arrangement.spacedBy(10.dp)) {
                                Row(verticalAlignment = Alignment.CenterVertically) {
                                    Icon(Icons.Default.VolumeUp, null, tint = Gold)
                                    Spacer(Modifier.width(8.dp))
                                    Text("OUTPUT TEST", color = Gold, fontWeight = FontWeight.Bold, letterSpacing = 1.4.sp)
                                }
                                Text("Verify Android speaker or connected audio output.", color = Muted, fontSize = 12.sp)
                                Button(onClick = {
                                    outputError = null
                                    executor.execute {
                                        try { engine.playTestTone() }
                                        catch (e: Exception) { runOnUiThread { outputError = e.message } }
                                    }
                                }, modifier = Modifier.fillMaxWidth()) { Text("Play 1,800 Hz test tone") }
                                outputError?.let { Text(it, color = Color(0xFFFF7777), fontSize = 12.sp) }
                                Button(onClick = {
                                    outputError = null
                                    modemStatus = "Encoding ECF3 frame…"
                                    executor.execute {
                                        try {
                                            val frame = NativeModem.encode("ECHOCRYPT ANDROID LINK TEST".toByteArray(Charsets.UTF_8))
                                            engine.transmitPcm(frame.samples)
                                            runOnUiThread {
                                                modemStatus = "Sent ${frame.payloadBytes} bytes · ${"%.2f".format(frame.durationSeconds)} s"
                                            }
                                        } catch (e: Exception) {
                                            runOnUiThread {
                                                outputError = e.message ?: "Modem transmission failed"
                                                modemStatus = "Transmission failed"
                                            }
                                        }
                                    }
                                }, modifier = Modifier.fillMaxWidth(),
                                    colors = ButtonDefaults.buttonColors(containerColor = Gold, contentColor = Background)) {
                                    Text("Transmit ECF3 protocol test")
                                }
                                Text(modemStatus, color = Muted, fontSize = 11.sp)
                                Text("Diagnostic payload is sent without encryption or handshake.",
                                    color = Color(0xFFFFC56A), fontSize = 10.sp)
                            }
                        }

                        
                        Card(colors = CardDefaults.cardColors(containerColor = Panel),
                            shape = RoundedCornerShape(20.dp), modifier = Modifier.fillMaxWidth()) {
                            Column(Modifier.padding(18.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
                                Text("ECF3 RECEIVE", color = Gold, fontWeight = FontWeight.Bold, letterSpacing = 1.4.sp)
                                Text("$receivedCount frame(s) decoded", fontWeight = FontWeight.SemiBold)
                                Text(lastReceived, color = Muted, fontSize = 12.sp, lineHeight = 18.sp)
                                receiveError?.let { Text(it, color = Color(0xFFFF7777), fontSize = 11.sp) }
                                Text("Incoming microphone PCM is scanned for ECF3 frames. Payload display is diagnostic plaintext only.",
                                    color = Color(0xFFFFC56A), fontSize = 10.sp, lineHeight = 15.sp)
                            }
                        }
                        Card(colors = CardDefaults.cardColors(containerColor = Color(0xFF101722)),
                            shape = RoundedCornerShape(16.dp), modifier = Modifier.fillMaxWidth()) {
                            Column(Modifier.padding(16.dp)) {
                                Text("PROTOCOL INTEGRATION", color = Gold, fontWeight = FontWeight.Bold, letterSpacing = 1.3.sp)
                                Spacer(Modifier.height(8.dp))
                                Text("ECF3 transmit encoder connected", fontWeight = FontWeight.SemiBold)
                                Text("Native microphone capture, live ECF3 stream scanning, speaker output and diagnostic transmission are connected. ASRE recovery, secure handshake/session management, Direct/Broadcast orchestration, encrypted messaging and file transfer are not implemented yet.",
                                    color = Muted, fontSize = 12.sp, lineHeight = 18.sp)
                            }
                        }
                        Spacer(Modifier.weight(1f))
                        Text("ANDROID NATIVE · 0.1.0 DEVELOPMENT", color = Muted, fontSize = 10.sp,
                            modifier = Modifier.align(Alignment.CenterHorizontally))
                        error?.let { Text(it, color = Color(0xFFFF7777), fontSize = 12.sp) }
                    }
                }
            }
        }
    }

    override fun onDestroy() {
        engine.release()
        executor.shutdownNow()
        receiveExecutor.shutdownNow()
        super.onDestroy()
    }
}
