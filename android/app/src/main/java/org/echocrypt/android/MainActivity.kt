package org.echocrypt.android

import android.Manifest
import android.content.pm.PackageManager
import android.os.Bundle
import android.os.Handler
import android.os.Looper
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
import org.echocrypt.android.audio.DuplexAudioSession
import org.echocrypt.android.protocol.NativeDirectTransport
import org.echocrypt.android.protocol.NativeModem
import org.echocrypt.android.protocol.NativeStreamReceiver
import org.echocrypt.android.protocol.PcmChunkSequenceGuard
import org.echocrypt.android.protocol.TrustedPeersStore
import java.util.concurrent.ArrayBlockingQueue
import java.util.concurrent.Executors
import java.util.concurrent.RejectedExecutionException
import java.util.concurrent.ThreadPoolExecutor
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicLong

private val Background = Color(0xFF090C12)
private val Panel = Color(0xFF151D29)
private val Gold = Color(0xFFE5B85C)
private val Muted = Color(0xFF8995A7)

class MainActivity : ComponentActivity() {
    private val engine = AndroidAudioEngine()
    @Volatile private var duplexSession: DuplexAudioSession? = null
    private val executor = Executors.newSingleThreadExecutor()
    private val receiveExecutor = ThreadPoolExecutor(
        1, 1, 0L, TimeUnit.MILLISECONDS,
        ArrayBlockingQueue(64),
        ThreadPoolExecutor.AbortPolicy()
    )
    private val streamReceiver = NativeStreamReceiver()
    private val chunkSequence = AtomicLong(0L)
    private val sequenceGuard = PcmChunkSequenceGuard()
    private lateinit var directTransport: NativeDirectTransport
    private val reliabilityHandler = Handler(Looper.getMainLooper())
    private val reliabilityTicker = object : Runnable {
        override fun run() {
            if (::directTransport.isInitialized && directTransport.isActive) {
                executor.execute { runCatching { directTransport.tick() } }
            }
            reliabilityHandler.postDelayed(this, 1_000L)
        }
    }

    private var directStatus by mutableStateOf("IDLE")
    private var directError by mutableStateOf<String?>(null)
    private val directMessages = mutableStateListOf<String>()
    private var trustedPeers by mutableStateOf<List<TrustedPeersStore.TrustedPeer>>(emptyList())

    private var permissionResult: ((Boolean) -> Unit)? = null
    private val permissionRequest = registerForActivityResult(ActivityResultContracts.RequestPermission()) {
        permissionResult?.invoke(it)
        permissionResult = null
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        directTransport = NativeDirectTransport(
            context = applicationContext,
            enqueueAudio = { samples -> duplexSession?.enqueuePlayback(samples) == true },
            onStateChanged = { value -> runOnUiThread { directStatus = value } },
            onTextReceived = { value ->
                runOnUiThread { addDirectMessage("Peer: $value") }
            },
            onError = { value -> runOnUiThread { directError = value } }
        )
        refreshTrustedPeers()
        reliabilityHandler.postDelayed(reliabilityTicker, 1_000L)

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
            var effectsStatus by remember { mutableStateOf("AEC: unknown · Noise suppression: unknown") }

            val hasPermission = {
                ContextCompat.checkSelfPermission(this, Manifest.permission.RECORD_AUDIO) ==
                    PackageManager.PERMISSION_GRANTED
            }
            val onCapturedPcm: (FloatArray) -> Unit = { pcm ->
                val sequence = chunkSequence.incrementAndGet()
                try {
                    receiveExecutor.execute {
                        if (sequenceGuard.requiresReset(sequence)) {
                            streamReceiver.reset()
                            runOnUiThread {
                                receiveError = "Audio processing fell behind; decoder reacquired after dropped PCM."
                            }
                        }
                        val frames = streamReceiver.push(pcm)
                        if (frames.isNotEmpty()) {
                            frames.forEach { frame -> directTransport.onIncomingPayload(frame.payload) }
                            runOnUiThread {
                                receivedCount += frames.size
                                lastReceived = frames.joinToString(" · ") { frame ->
                                    "ECF3 payload · ${frame.payload.size} bytes"
                                }
                            }
                        }
                    }
                } catch (_: RejectedExecutionException) {
                    runOnUiThread {
                        receiveError = "Receive processing overloaded; audio chunks were dropped. Waiting for resynchronization."
                    }
                }
            }
            val start = {
                error = null
                receiveError = null
                chunkSequence.set(0L)
                receiveExecutor.execute {
                    streamReceiver.reset()
                    sequenceGuard.reset()
                }
                executor.execute {
                    try {
                        val session = DuplexAudioSession(
                            onPcm = onCapturedPcm,
                            onError = { message ->
                                runOnUiThread {
                                    listening = false
                                    status = "AUDIO ERROR"
                                    error = message
                                    directTransport.disconnect()
                                }
                                executor.execute {
                                    duplexSession?.stop()
                                    duplexSession = null
                                }
                            },
                            onInputLevel = { value -> runOnUiThread { level = value } }
                        )
                        duplexSession = session
                        session.start()
                        runOnUiThread {
                            status = "FULL DUPLEX"
                            effectsStatus = "AEC: " + (if (session.supportsEchoCancellation) "ON" else "unavailable") +
                                " · Noise suppression: " + (if (session.supportsNoiseSuppression) "ON" else "unavailable")
                        }
                    } catch (e: Exception) {
                        duplexSession?.stop()
                        duplexSession = null
                        runOnUiThread {
                            listening = false
                            status = "AUDIO ERROR"
                            error = e.message ?: "Could not start full-duplex audio"
                        }
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
                                    Text("FULL DUPLEX AUDIO", color = Gold, fontWeight = FontWeight.Bold, letterSpacing = 1.5.sp)
                                    Spacer(Modifier.weight(1f))
                                    Text(status, color = Muted, fontSize = 11.sp)
                                }
                                Row(verticalAlignment = Alignment.CenterVertically) {
                                    Icon(Icons.Default.Mic, null, tint = Gold)
                                    Spacer(Modifier.width(10.dp))
                                    Column(Modifier.weight(1f)) {
                                        Text("Microphone")
                                        Text(if (listening) "48 kHz · Mono PCM · Capture + playback ready" else "Stopped",
                                            color = Muted, fontSize = 12.sp)
                                    }
                                    Text("${level.toInt()} dB", color = Gold, fontSize = 12.sp)
                                }
                                Box(Modifier.fillMaxWidth().height(6.dp)
                                    .background(Color(0xFF293547), RoundedCornerShape(8.dp))) {
                                    Box(Modifier.fillMaxWidth(((level + 90f) / 90f).coerceIn(0f, 1f))
                                        .height(6.dp).background(Gold, RoundedCornerShape(8.dp)))
                                }
                                Text(effectsStatus, color = Muted, fontSize = 11.sp)
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
                                        Icon(Icons.Default.Mic, null); Spacer(Modifier.width(6.dp)); Text("Start Duplex")
                                    }
                                    Button(onClick = {
                                        directTransport.disconnect()
                                        executor.execute { duplexSession?.stop(); duplexSession = null }
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
                                }, enabled = !listening, modifier = Modifier.fillMaxWidth()) { Text("Play 1,800 Hz test tone") }
                                outputError?.let { Text(it, color = Color(0xFFFF7777), fontSize = 12.sp) }
                                Button(onClick = {
                                    outputError = null
                                    modemStatus = "Encoding ECF3 frame…"
                                    executor.execute {
                                        try {
                                            val frame = NativeModem.encode("ECHOCRYPT ANDROID LINK TEST".toByteArray(Charsets.UTF_8))
                                            val session = duplexSession
                                                ?: throw IllegalStateException("Start Full Duplex before queuing a frame")
                                            check(session.isRunning) { "Full Duplex audio is not running" }
                                            check(!directTransport.isActive) { "Disconnect the Direct session before running diagnostics" }
                                            check(session.enqueuePlayback(frame.samples)) { "Playback queue is full; wait for the current transmission" }
                                            runOnUiThread {
                                                modemStatus = "Queued ${frame.payloadBytes} bytes · ${"%.2f".format(frame.durationSeconds)} s"
                                            }
                                        } catch (e: Exception) {
                                            runOnUiThread {
                                                outputError = e.message ?: "Modem transmission failed"
                                                modemStatus = "Transmission failed"
                                            }
                                        }
                                    }
                                }, enabled = listening && !directTransport.isActive, modifier = Modifier.fillMaxWidth(),
                                    colors = ButtonDefaults.buttonColors(containerColor = Gold, contentColor = Background)) {
                                    Text("Queue ECF3 test frame")
                                }
                                Text(modemStatus, color = Muted, fontSize = 11.sp)
                                Text("Diagnostic payload is queued as unencrypted test data. Do not use during a Direct session.",
                                    color = Color(0xFFFFC56A), fontSize = 10.sp)
                            }
                        }

                        IdentityManagementPanel(onPeersChanged = { refreshTrustedPeers() })

                        DirectCommunicationPanel(
                            peers = trustedPeers,
                            duplexRunning = duplexSession?.isRunning == true,
                            status = directStatus,
                            messages = directMessages,
                            error = directError,
                            onRefreshPeers = { refreshTrustedPeers() },
                            onStartInitiator = { fingerprint ->
                                directError = null
                                executor.execute {
                                    try { directTransport.startInitiator(fingerprint) }
                                    catch (e: Exception) { runOnUiThread { directError = e.message ?: "Could not connect" } }
                                }
                            },
                            onStartResponder = { fingerprint ->
                                directError = null
                                executor.execute {
                                    try { directTransport.startResponder(fingerprint) }
                                    catch (e: Exception) { runOnUiThread { directError = e.message ?: "Could not wait for peer" } }
                                }
                            },
                            onRetry = {
                                executor.execute {
                                    try { directTransport.retryHandshake() }
                                    catch (e: Exception) { runOnUiThread { directError = e.message ?: "Retry failed" } }
                                }
                            },
                            onDisconnect = {
                                executor.execute {
                                    directTransport.disconnect()
                                    runOnUiThread { directMessages.clear(); directError = null }
                                }
                            },
                            onSend = { text ->
                                executor.execute {
                                    try {
                                        directTransport.sendText(text)
                                        runOnUiThread { addDirectMessage("Me: $text") }
                                    } catch (e: Exception) {
                                        runOnUiThread { directError = e.message ?: "Message transmission failed" }
                                    }
                                }
                            }
                        )

                        Card(colors = CardDefaults.cardColors(containerColor = Panel),
                            shape = RoundedCornerShape(20.dp), modifier = Modifier.fillMaxWidth()) {
                            Column(Modifier.padding(18.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
                                Text("ECF3 RECEIVE", color = Gold, fontWeight = FontWeight.Bold, letterSpacing = 1.4.sp)
                                Text("$receivedCount frame(s) decoded", fontWeight = FontWeight.SemiBold)
                                Text(lastReceived, color = Muted, fontSize = 12.sp, lineHeight = 18.sp)
                                receiveError?.let { Text(it, color = Color(0xFFFF7777), fontSize = 11.sp) }
                                Text("Decoded packet bytes are routed to the active Direct session and are not rendered as plaintext diagnostics.",
                                    color = Muted, fontSize = 10.sp, lineHeight = 15.sp)
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

    private fun addDirectMessage(value: String) {
        directMessages.add(value)
        while (directMessages.size > 100) directMessages.removeAt(0)
    }

    private fun refreshTrustedPeers() {
        runCatching { directTransport.trustedPeers() }
            .onSuccess { trustedPeers = it }
            .onFailure { directError = it.message ?: "Could not load trusted peers" }
    }

    override fun onDestroy() {
        reliabilityHandler.removeCallbacks(reliabilityTicker)
        if (::directTransport.isInitialized) directTransport.disconnect()
        duplexSession?.release()
        duplexSession = null
        engine.release()
        executor.shutdownNow()
        receiveExecutor.shutdownNow()
        super.onDestroy()
    }
}
