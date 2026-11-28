package org.echocrypt.android

import android.Manifest
import android.content.pm.PackageManager
import android.net.Uri
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
import org.echocrypt.android.protocol.NativeBroadcastTransport
import org.echocrypt.android.protocol.NativeModem
import org.echocrypt.android.protocol.NativeStreamReceiver
import org.echocrypt.android.protocol.PcmChunkSequenceGuard
import org.echocrypt.android.protocol.ReliablePacketLayer
import org.echocrypt.android.protocol.TrustedPeersStore
import java.io.File
import java.io.FileOutputStream
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
    private val receiveExecutor = ThreadPoolExecutor(1, 1, 0L, TimeUnit.MILLISECONDS,
        ArrayBlockingQueue(64), ThreadPoolExecutor.AbortPolicy())
    private val streamReceiver = NativeStreamReceiver()
    private val chunkSequence = AtomicLong(0L)
    private val sequenceGuard = PcmChunkSequenceGuard()
    private lateinit var directTransport: NativeDirectTransport
    private lateinit var broadcastTransport: NativeBroadcastTransport
    private lateinit var settingsStore: EchocryptSettingsStore
    @Volatile private var lastLoggedDirectStatus = ""
    private var broadcastJoined by mutableStateOf(false)
    private var broadcastStatus by mutableStateOf("NOT JOINED")
    private var broadcastError by mutableStateOf<String?>(null)
    private val broadcastMessages = mutableStateListOf<String>()
    private val reliabilityHandler = Handler(Looper.getMainLooper())
    private val reliabilityTicker = object : Runnable {
        override fun run() {
            if (::directTransport.isInitialized || ::broadcastTransport.isInitialized) {
                executor.execute {
                    if (::directTransport.isInitialized && directTransport.isActive) runCatching { directTransport.tick() }
                    if (::broadcastTransport.isInitialized) runCatching { broadcastTransport.tick() }
                }
            }
            reliabilityHandler.postDelayed(this, 1_000L)
        }
    }

    private var lastReceivedFile by mutableStateOf<File?>(null)
    private var directStatus by mutableStateOf("IDLE")
    private var directError by mutableStateOf<String?>(null)
    private val directMessages = mutableStateListOf<String>()
    private var trustedPeers by mutableStateOf<List<TrustedPeersStore.TrustedPeer>>(emptyList())
    private var permissionResult: ((Boolean) -> Unit)? = null
    private val permissionRequest = registerForActivityResult(ActivityResultContracts.RequestPermission()) {
        permissionResult?.invoke(it); permissionResult = null
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        AppEventLog.initialize(applicationContext)
        settingsStore = EchocryptSettingsStore(applicationContext)
        val initialSettings = settingsStore.load()
        directTransport = NativeDirectTransport(
            context = applicationContext,
            settingsProvider = { settingsStore.load() },
            enqueueAudio = { samples -> duplexSession?.enqueuePlayback(samples) == true },
            onStateChanged = { value ->
                if (value != lastLoggedDirectStatus) {
                    AppEventLog.record("DIRECT_STATE", value)
                    lastLoggedDirectStatus = value
                }
                runOnUiThread { directStatus = value }
            },
            onTextReceived = { value ->
                AppEventLog.record("DIRECT_RX_TEXT", "utf8Bytes=" + value.toByteArray(Charsets.UTF_8).size)
                runOnUiThread { addDirectMessage("Peer: $value") }
            },
            onError = { value ->
                AppEventLog.record("DIRECT_ERROR", value)
                runOnUiThread { directError = value }
            },
            onFileReceived = { file -> runOnUiThread {
                lastReceivedFile = file
                addDirectMessage("File received: ${file.name} · ${file.length()} bytes · app storage")
            } }
        )
        broadcastTransport = NativeBroadcastTransport(
            enqueueAudio = { samples, onPlayed -> duplexSession?.enqueuePlayback(samples, onPlayed) == true },
            onTextReceived = { sender, text -> runOnUiThread { addBroadcastMessage(sender + ": " + text) } },
            onError = { value ->
                AppEventLog.record("BROADCAST_ERROR", value)
                runOnUiThread { broadcastError = value }
            },
            onStateChanged = { value -> runOnUiThread {
                broadcastStatus = value
                broadcastJoined = value.startsWith("JOINED")
            } }
        )
        lastReceivedFile = directTransport.latestReceivedFile()
        refreshTrustedPeers()
        reliabilityHandler.postDelayed(reliabilityTicker, 1_000L)

        setContent {
            var appSettings by remember { mutableStateOf(initialSettings) }
            val logDirectoryPath = remember { AppEventLog.directoryPath(applicationContext) }
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

            val saveReceivedFile = rememberLauncherForActivityResult(ActivityResultContracts.CreateDocument("*/*")) { uri: Uri? ->
                val source = lastReceivedFile
                if (uri != null && source != null) {
                    executor.execute {
                        try {
                            val output = contentResolver.openOutputStream(uri)
                                ?: throw IllegalStateException("Cannot open destination")
                            output.use { stream -> source.inputStream().use { input -> input.copyTo(stream) } }
                            runOnUiThread { directError = null }
                        } catch (e: Exception) {
                            runOnUiThread { directError = e.message ?: "Could not export received file" }
                        }
                    }
                }
            }

            val filePicker = rememberLauncherForActivityResult(ActivityResultContracts.OpenDocument()) { uri: Uri? ->
                if (uri != null) {
                    directError = null
                    executor.execute {
                        var staged: File? = null
                        try {
                            val displayName = contentResolver.query(uri, arrayOf(android.provider.OpenableColumns.DISPLAY_NAME), null, null, null)
                                ?.use { cursor -> if (cursor.moveToFirst()) cursor.getString(0) else null }
                                ?: "received-file"
                            staged = File(cacheDir, "outgoing-${System.nanoTime()}.bin")
                            contentResolver.openInputStream(uri).use { input ->
                                requireNotNull(input) { "Cannot open selected file" }
                                FileOutputStream(staged).use { output ->
                                    val buffer = ByteArray(32 * 1024)
                                    var total = 0L
                                    while (true) {
                                        val read = input.read(buffer)
                                        if (read < 0) break
                                        total += read
                                        require(total <= org.echocrypt.android.protocol.NativeFileTransfer.MAX_FILE_BYTES) {
                                            "File exceeds the 10 MiB Android transfer limit"
                                        }
                                        output.write(buffer, 0, read)
                                    }
                                }
                            }
                            directTransport.startFileTransfer(staged, displayName)
                            staged = null
                        } catch (e: Exception) {
                            AppEventLog.record("FILE_PICKER_OR_TX_ERROR", e.message ?: e.javaClass.simpleName)
                            runOnUiThread { directError = e.message ?: "Could not start file transfer" }
                        } finally {
                            staged?.delete()
                        }
                    }
                }
            }

            val hasPermission = {
                ContextCompat.checkSelfPermission(this, Manifest.permission.RECORD_AUDIO) == PackageManager.PERMISSION_GRANTED
            }
            val onCapturedPcm: (FloatArray) -> Unit = { pcm ->
                val sequence = chunkSequence.incrementAndGet()
                try {
                    receiveExecutor.execute {
                        if (sequenceGuard.requiresReset(sequence)) {
                            streamReceiver.reset()
                            AppEventLog.record("AUDIO_RX_OVERRUN", "pcmSequence=" + sequence)
                        runOnUiThread { receiveError = "Audio processing fell behind; decoder reacquired after dropped PCM." }
                        }
                        val frames = streamReceiver.push(pcm)
                        if (frames.isNotEmpty()) {
                            frames.forEach { frame ->
                                directTransport.onIncomingPayload(frame.payload)
                                broadcastTransport.onIncomingPayload(frame.payload)
                            }
                            runOnUiThread {
                                receivedCount += frames.size
                                lastReceived = frames.joinToString(" · ") { frame -> "ECF3 payload · ${frame.payload.size} bytes" }
                            }
                        }
                    }
                } catch (_: RejectedExecutionException) {
                    AppEventLog.record("AUDIO_RX_QUEUE_OVERFLOW")
                    AppEventLog.record("AUDIO_RX_QUEUE_OVERFLOW")
                    runOnUiThread { receiveError = "Receive processing overloaded; audio chunks were dropped. Waiting for resynchronization." }
                }
            }
            val start = {
                error = null
                receiveError = null
                chunkSequence.set(0L)
                receiveExecutor.execute { streamReceiver.reset(); sequenceGuard.reset() }
                executor.execute {
                    try {
                        AppEventLog.record("AUDIO_DUPLEX_START_REQUESTED", "preferredOutputRate=" + appSettings.preferredOutputSampleRate)
                        val session = DuplexAudioSession(
                            onPcm = onCapturedPcm,
                            onError = { message ->
                                AppEventLog.record("AUDIO_DUPLEX_ERROR", message)
                                runOnUiThread {
                                    listening = false; status = "AUDIO ERROR"; error = message
                                    directTransport.disconnect(); broadcastTransport.leave()
                                }
                                executor.execute { duplexSession?.stop(); duplexSession = null }
                            },
                            onInputLevel = { value -> runOnUiThread { level = value } },
                            settings = appSettings
                        )
                        duplexSession = session
                        session.start()
                        runOnUiThread {
                            status = "FULL DUPLEX"
                            effectsStatus = "AEC: " + (if (session.supportsEchoCancellation) "ON" else "unavailable") +
                                " · Noise suppression: " + (if (session.supportsNoiseSuppression) "ON" else "unavailable") +
                                " · Output: " + session.outputDescription
                            AppEventLog.record("AUDIO_DUPLEX_STARTED", effectsStatus)
                        }
                    } catch (e: Exception) {
                        AppEventLog.record("AUDIO_DUPLEX_START_ERROR", e.message ?: e.javaClass.simpleName)
                        duplexSession?.stop(); duplexSession = null
                        runOnUiThread { listening = false; status = "AUDIO ERROR"; error = e.message ?: "Could not start full-duplex audio" }
                    }
                }
            }

            MaterialTheme(
                colorScheme = darkColorScheme(
                    primary = Gold,
                    background = Background,
                    surface = Panel,
                    onPrimary = Background,
                    onBackground = Color.White,
                    onSurface = Color.White
                )
            ) {
                EchocryptAppShell(
                    duplexRunning = listening,
                    audioStatus = status,
                    audioLevel = level,
                    audioEffects = effectsStatus,
                    audioError = error,
                    directStatus = directStatus,
                    directMessages = directMessages.toList(),
                    directError = directError,
                    peers = trustedPeers,
                    latestReceivedFile = lastReceivedFile,
                    pendingOutgoingFileName = directTransport.pendingOutgoingFileName(),
                    directEstablished = directTransport.isEstablished,
                    receivedFrameCount = receivedCount,
                    lastReceivedFrame = lastReceived,
                    receiveError = receiveError,
                    modemStatus = modemStatus,
                    onStartDuplex = {
                        if (hasPermission()) {
                            listening = true
                            status = "STARTING"
                            start()
                        } else {
                            permissionResult = { granted ->
                                if (granted) {
                                    listening = true
                                    status = "STARTING"
                                    start()
                                } else {
                                    AppEventLog.record("MICROPHONE_PERMISSION_DENIED")
                                    error = "Microphone permission denied"
                                }
                            }
                            permissionRequest.launch(Manifest.permission.RECORD_AUDIO)
                        }
                    },
                    onStopDuplex = {
                        directTransport.disconnect()
                        broadcastTransport.leave()
                        AppEventLog.record("AUDIO_DUPLEX_STOPPED")
                        executor.execute { duplexSession?.stop(); duplexSession = null }
                        listening = false
                        level = -90f
                        status = "READY"
                    },
                    onPlayTestTone = {
                        outputError = null
                        executor.execute {
                            try {
                                AppEventLog.record("AUDIO_TEST_TONE", "frequencyHz=" + appSettings.speakerTestFrequencyHz + " durationMs=" + appSettings.speakerTestDurationMs)
                                engine.playTestTone(appSettings.speakerTestFrequencyHz, appSettings.speakerTestDurationMs,
                                    appSettings.preferredOutputSampleRate)
                            } catch (e: Exception) {
                                AppEventLog.record("AUDIO_TEST_TONE_ERROR", e.message ?: e.javaClass.simpleName)
                                runOnUiThread { outputError = e.message ?: "Output test failed" }
                            }
                        }
                    },
                    onSendDiagnosticFrame = {
                        outputError = null
                        modemStatus = "Encoding ECF3 frame…"
                        executor.execute {
                            try {
                                val frame = NativeModem.encode(
                                    "ECHOCRYPT ANDROID LINK TEST".toByteArray(Charsets.UTF_8)
                                )
                                val session = duplexSession
                                    ?: throw IllegalStateException("Start Full Duplex before queuing a frame")
                                check(session.isRunning) { "Full Duplex audio is not running" }
                                check(!directTransport.isActive) {
                                    "Disconnect the Direct session before running diagnostics"
                                }
                                check(session.enqueuePlayback(frame.samples)) {
                                    "Playback queue is full; wait for the current transmission"
                                }
                                AppEventLog.record("DIAGNOSTIC_FRAME_TX", "bytes=" + frame.payloadBytes)
                                runOnUiThread {
                                    modemStatus = "Queued ${frame.payloadBytes} bytes · ${"%.2f".format(frame.durationSeconds)} s"
                                }
                            } catch (e: Exception) {
                                AppEventLog.record("DIAGNOSTIC_FRAME_ERROR", e.message ?: e.javaClass.simpleName)
                                runOnUiThread {
                                    outputError = e.message ?: "Modem transmission failed"
                                    modemStatus = "Transmission failed"
                                }
                            }
                        }
                    },
                    onChooseFile = {
                        AppEventLog.record("FILE_PICKER_OPENED")
                        filePicker.launch(arrayOf("*/*"))
                    },
                    onExportReceivedFile = {
                        lastReceivedFile?.let {
                            AppEventLog.record("FILE_EXPORT_REQUESTED", "name=" + it.name + " bytes=" + it.length())
                            saveReceivedFile.launch(it.name)
                        }
                    },
                    onRefreshPeers = {
                        AppEventLog.record("PEER_LIST_REFRESHED")
                        refreshTrustedPeers()
                    },
                    onStartInitiator = { fingerprint ->
                        directError = null
                        executor.execute {
                            try {
                                check(!broadcastTransport.isJoined) { "Leave Broadcast before connecting Direct" }
                                directTransport.startInitiator(fingerprint)
                            }
                            catch (e: Exception) {
                                AppEventLog.record("DIRECT_CONNECT_ERROR", e.message ?: e.javaClass.simpleName)
                                runOnUiThread { directError = e.message ?: "Could not connect" }
                            }
                        }
                    },
                    onStartResponder = { fingerprint ->
                        directError = null
                        executor.execute {
                            try {
                                check(!broadcastTransport.isJoined) { "Leave Broadcast before waiting for Direct" }
                                directTransport.startResponder(fingerprint)
                            }
                            catch (e: Exception) {
                                AppEventLog.record("DIRECT_LISTEN_ERROR", e.message ?: e.javaClass.simpleName)
                                runOnUiThread { directError = e.message ?: "Could not wait for peer" }
                            }
                        }
                    },
                    onRetryHandshake = {
                        executor.execute {
                            try {
                                AppEventLog.record("DIRECT_HANDSHAKE_RETRY")
                                directTransport.retryHandshake()
                            } catch (e: Exception) {
                                AppEventLog.record("DIRECT_HANDSHAKE_RETRY_ERROR", e.message ?: e.javaClass.simpleName)
                                runOnUiThread { directError = e.message ?: "Retry failed" }
                            }
                        }
                    },
                    onDisconnect = {
                        AppEventLog.record("DIRECT_DISCONNECT_REQUESTED")
                        executor.execute {
                            directTransport.disconnect()
                            runOnUiThread { directMessages.clear(); directError = null }
                        }
                    },
                    onSendText = { message ->
                        executor.execute {
                            try {
                                directTransport.sendText(message)
                                AppEventLog.record("DIRECT_TX_TEXT", "utf8Bytes=" + message.toByteArray(Charsets.UTF_8).size)
                                runOnUiThread { addDirectMessage("Me: $message") }
                            } catch (e: Exception) {
                                runOnUiThread {
                                    directError = e.message ?: "Message transmission failed"
                                }
                            }
                        }
                    },
                    onPeersChanged = { refreshTrustedPeers() },
                    outputError = outputError,
                    settings = appSettings,
                    logDirectoryPath = logDirectoryPath,
                    onSettingsChanged = { updated ->
                        try {
                            settingsStore.save(updated)
                            appSettings = updated
                            AppEventLog.record("SETTINGS_UPDATED", updated.toString())
                        } catch (failure: Exception) {
                            AppEventLog.record("SETTINGS_SAVE_ERROR", failure.message ?: failure.javaClass.simpleName)
                            directError = failure.message ?: "Could not save settings"
                        }
                    },
                    broadcastJoined = broadcastJoined,
                    broadcastAudioReady = listening,
                    broadcastMessages = broadcastMessages.toList(),
                    broadcastStatus = broadcastStatus,
                    broadcastError = broadcastError,
                    onJoinBroadcast = {
                        executor.execute {
                            try {
                                check(duplexSession?.isRunning == true) { "Start Full Duplex before joining Broadcast" }
                                check(!directTransport.isActive) { "Disconnect Direct before joining Broadcast" }
                                broadcastError = null
                                broadcastTransport.join()
                            } catch (failure: Exception) {
                                AppEventLog.record("BROADCAST_JOIN_ERROR", failure.message ?: failure.javaClass.simpleName)
                                runOnUiThread { broadcastError = failure.message ?: "Could not join Broadcast" }
                            }
                        }
                    },
                    onLeaveBroadcast = { executor.execute { broadcastTransport.leave() } },
                    onSendBroadcast = { message ->
                        executor.execute {
                            try {
                                broadcastTransport.sendText(message)
                                runOnUiThread { addBroadcastMessage("Me: " + message) }
                            } catch (failure: Exception) {
                                AppEventLog.record("BROADCAST_TX_ERROR", failure.message ?: failure.javaClass.simpleName)
                                runOnUiThread { broadcastError = failure.message ?: "Broadcast send failed" }
                            }
                        }
                    },
                )
            }
        }
    }

    private fun addBroadcastMessage(value: String) {
        broadcastMessages.add(value)
        while (broadcastMessages.size > 200) broadcastMessages.removeAt(0)
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
        AppEventLog.record("APP_STOP")
        reliabilityHandler.removeCallbacks(reliabilityTicker)
        if (::broadcastTransport.isInitialized) broadcastTransport.close()
        if (::directTransport.isInitialized) directTransport.disconnect()
        duplexSession?.release(); duplexSession = null
        engine.release()
        executor.shutdownNow(); receiveExecutor.shutdownNow()
        super.onDestroy()
    }
}
