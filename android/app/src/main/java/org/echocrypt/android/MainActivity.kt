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
import java.util.concurrent.Executors

private val Background = Color(0xFF090C12)
private val Panel = Color(0xFF151D29)
private val Gold = Color(0xFFE5B85C)
private val Muted = Color(0xFF8995A7)

class MainActivity : ComponentActivity() {
    private val engine = AndroidAudioEngine()
    private val executor = Executors.newSingleThreadExecutor()
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

            val hasPermission = {
                ContextCompat.checkSelfPermission(this, Manifest.permission.RECORD_AUDIO) ==
                    PackageManager.PERMISSION_GRANTED
            }
            val start = {
                error = null
                executor.execute {
                    try {
                        engine.startCapture({ value -> runOnUiThread { level = value } }, { message ->
                            runOnUiThread { listening = false; status = "AUDIO ERROR"; error = message }
                        })
                    } catch (e: Exception) {
                        runOnUiThread { listening = false; status = "AUDIO ERROR"; error = e.message }
                    }
                }
            }

            MaterialTheme(colorScheme = darkColorScheme(primary = Gold, background = Background,
                surface = Panel, onPrimary = Background, onBackground = Color.White, onSurface = Color.White)) {
                Surface(Modifier.fillMaxSize(), color = Background) {
                    Column(Modifier.fillMaxSize().padding(22.dp), verticalArrangement = Arrangement.spacedBy(18.dp)) {
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
                                    Spacer(Modifier.weight(1f))
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
                            }
                        }

                        Card(colors = CardDefaults.cardColors(containerColor = Color(0xFF101722)),
                            shape = RoundedCornerShape(16.dp), modifier = Modifier.fillMaxWidth()) {
                            Column(Modifier.padding(16.dp)) {
                                Text("PROTOCOL INTEGRATION", color = Gold, fontWeight = FontWeight.Bold, letterSpacing = 1.3.sp)
                                Spacer(Modifier.height(8.dp))
                                Text("Not connected yet", fontWeight = FontWeight.SemiBold)
                                Text("This milestone establishes native Android audio and the application shell. Modem, Direct/Broadcast sessions, cryptographic handshake and file transfer are not implemented in this APK foundation.",
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
        super.onDestroy()
    }
}
