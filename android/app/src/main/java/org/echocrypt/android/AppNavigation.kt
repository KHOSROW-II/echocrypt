package org.echocrypt.android

import java.io.File
import org.echocrypt.android.protocol.TrustedPeersStore
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.ChatBubbleOutline
import androidx.compose.material.icons.filled.Fingerprint
import androidx.compose.material.icons.filled.FolderOpen
import androidx.compose.material.icons.filled.GraphicEq
import androidx.compose.material.icons.filled.Home
import androidx.compose.material.icons.filled.Public
import androidx.compose.material.icons.filled.Settings
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp

private val ShellBackground = Color(0xFF090C12)
private val ShellPanel = Color(0xFF151D29)
private val ShellGold = Color(0xFFE5B85C)
private val ShellMuted = Color(0xFF8995A7)
private val ShellLine = Color(0xFF293547)
private val ShellRed = Color(0xFFFF7777)
private val ShellGreen = Color(0xFF85D6A0)

private enum class AppSection(val title: String, val caption: String) {
    OVERVIEW("Overview", "Device and session summary"),
    DIRECT("Direct", "Authenticated encrypted communication"),
    FILES("Files", "Encrypted file transfer and delivery"),
    BROADCAST("Broadcast", "Open acoustic broadcast room"),
    IDENTITY("Identity", "Device keys and trusted contacts"),
    AUDIO("Audio", "Microphone, speaker and full-duplex"),
    DIAGNOSTICS("Diagnostics", "ECF3 receive and output tests"),
    SETTINGS("Settings", "Current protocol configuration")
}

@Composable
fun EchocryptAppShell(
    duplexRunning: Boolean,
    audioStatus: String,
    audioLevel: Float,
    audioEffects: String,
    audioError: String?,
    directStatus: String,
    directMessages: List<String>,
    directError: String?,
    peers: List<TrustedPeersStore.TrustedPeer>,
    latestReceivedFile: File?,
    pendingOutgoingFileName: String?,
    directEstablished: Boolean,
    receivedFrameCount: Int,
    lastReceivedFrame: String,
    receiveError: String?,
    modemStatus: String,
    onStartDuplex: () -> Unit,
    onStopDuplex: () -> Unit,
    onPlayTestTone: () -> Unit,
    onSendDiagnosticFrame: () -> Unit,
    onChooseFile: () -> Unit,
    onExportReceivedFile: () -> Unit,
    onRefreshPeers: () -> Unit,
    onStartInitiator: (String) -> Unit,
    onStartResponder: (String) -> Unit,
    onRetryHandshake: () -> Unit,
    onDisconnect: () -> Unit,
    onSendText: (String) -> Unit,
    onPeersChanged: () -> Unit,
    outputError: String?,
    settings: EchocryptSettings,
    logDirectoryPath: String,
    onSettingsChanged: (EchocryptSettings) -> Unit,
    broadcastJoined: Boolean,
    broadcastAudioReady: Boolean,
    broadcastMessages: List<String>,
    broadcastStatus: String,
    broadcastError: String?,
    onJoinBroadcast: () -> Unit,
    onLeaveBroadcast: () -> Unit,
    onSendBroadcast: (String) -> Unit
) {
    var selected by remember { mutableStateOf(AppSection.OVERVIEW) }
    LaunchedEffect(directEstablished) { if (directEstablished) selected = AppSection.DIRECT }
    LaunchedEffect(broadcastJoined) { if (broadcastJoined) selected = AppSection.BROADCAST }

    Scaffold(
        containerColor = ShellBackground,
        bottomBar = {
            NavigationBar(
                containerColor = Color(0xFF101722),
                contentColor = Color.White,
                tonalElevation = 0.dp
            ) {
                NavItem(AppSection.OVERVIEW, selected, { selected = it }, Icons.Default.Home)
                NavItem(AppSection.DIRECT, selected, { selected = it }, Icons.Default.ChatBubbleOutline)
                NavItem(AppSection.FILES, selected, { selected = it }, Icons.Default.FolderOpen)
                NavItem(AppSection.BROADCAST, selected, { selected = it }, Icons.Default.Public)
                NavItem(AppSection.IDENTITY, selected, { selected = it }, Icons.Default.Fingerprint)
                NavItem(AppSection.AUDIO, selected, { selected = it }, Icons.Default.GraphicEq)
                NavItem(AppSection.SETTINGS, selected, { selected = it }, Icons.Default.Settings)
            }
        }
    ) { insets ->
        Column(
            Modifier.fillMaxSize()
                .background(ShellBackground)
                .padding(insets)
        ) {
            AppHeader(
                section = selected,
                duplexRunning = duplexRunning,
                directEstablished = directEstablished
            )
            Box(Modifier.weight(1f).fillMaxWidth()) {
                when (selected) {
                    AppSection.OVERVIEW -> OverviewScreen(
                        duplexRunning = duplexRunning,
                        audioStatus = audioStatus,
                        directStatus = directStatus,
                        directEstablished = directEstablished,
                        peersCount = peers.size,
                        pendingFile = pendingOutgoingFileName,
                        latestReceivedFile = latestReceivedFile,
                        receivedFrameCount = receivedFrameCount,
                        onOpenDirect = { selected = AppSection.DIRECT },
                        onOpenFiles = { selected = AppSection.FILES },
                        onOpenIdentity = { selected = AppSection.IDENTITY },
                        onOpenAudio = { selected = AppSection.AUDIO },
                        onOpenDiagnostics = { selected = AppSection.DIAGNOSTICS }
                    )
                    AppSection.DIRECT -> DirectScreen(
                        peers = peers,
                        duplexRunning = duplexRunning,
                        directEstablished = directEstablished,
                        status = directStatus,
                        messages = directMessages,
                        error = directError,
                        onRefreshPeers = onRefreshPeers,
                        onStartInitiator = onStartInitiator,
                        onStartResponder = onStartResponder,
                        onRetry = onRetryHandshake,
                        onDisconnect = onDisconnect,
                        onSend = onSendText
                    )
                    AppSection.BROADCAST -> BroadcastRoomScreen(
                        joined = broadcastJoined,
                        audioReady = broadcastAudioReady,
                        messages = broadcastMessages,
                        status = broadcastStatus,
                        error = broadcastError,
                        onJoin = onJoinBroadcast,
                        onLeave = onLeaveBroadcast,
                        onSend = onSendBroadcast,
                        onOpenAudio = { selected = AppSection.AUDIO }
                    )
                    AppSection.FILES -> FilesScreen(
                        duplexRunning = duplexRunning,
                        directEstablished = directEstablished,
                        status = directStatus,
                        pendingFile = pendingOutgoingFileName,
                        latestReceivedFile = latestReceivedFile,
                        error = directError,
                        onChooseFile = onChooseFile,
                        onExportFile = onExportReceivedFile,
                        onOpenDirect = { selected = AppSection.DIRECT }
                    )
                    AppSection.IDENTITY -> IdentityScreen(
                        peersCount = peers.size,
                        onPeersChanged = onPeersChanged
                    )
                    AppSection.AUDIO -> AudioScreen(
                        duplexRunning = duplexRunning,
                        status = audioStatus,
                        level = audioLevel,
                        effects = audioEffects,
                        error = audioError,
                        outputError = outputError,
                        onStart = onStartDuplex,
                        onStop = onStopDuplex,
                        onPlayTestTone = onPlayTestTone,
                        speakerTestFrequencyHz = settings.speakerTestFrequencyHz
                    )
                    AppSection.DIAGNOSTICS -> DiagnosticsScreen(
                        duplexRunning = duplexRunning,
                        directEstablished = directEstablished,
                        receivedFrameCount = receivedFrameCount,
                        lastReceivedFrame = lastReceivedFrame,
                        receiveError = receiveError,
                        modemStatus = modemStatus,
                        outputError = outputError,
                        onSendDiagnosticFrame = onSendDiagnosticFrame,
                        onOpenAudio = { selected = AppSection.AUDIO }
                    )
                    AppSection.SETTINGS -> EchocryptSettingsScreen(
                        settings = settings,
                        logDirectoryPath = logDirectoryPath,
                        onChange = onSettingsChanged
                    )
                }
            }
        }
    }
}

@Composable
private fun RowScope.NavItem(
    section: AppSection,
    selected: AppSection,
    onSelect: (AppSection) -> Unit,
    icon: androidx.compose.ui.graphics.vector.ImageVector
) {
    NavigationBarItem(
        selected = selected == section,
        onClick = {
            AppEventLog.record("UI_NAVIGATION", section.title)
            onSelect(section)
        },
        icon = { Icon(icon, contentDescription = section.title) },
        label = { Text(section.title, maxLines = 1, fontSize = 9.sp) },
        alwaysShowLabel = false,
        colors = NavigationBarItemDefaults.colors(
            selectedIconColor = ShellGold,
            selectedTextColor = ShellGold,
            indicatorColor = Color(0xFF30303A),
            unselectedIconColor = ShellMuted,
            unselectedTextColor = ShellMuted
        )
    )
}

@Composable
private fun AppHeader(
    section: AppSection,
    duplexRunning: Boolean,
    directEstablished: Boolean
) {
    Surface(color = Color(0xFF0E141E), shadowElevation = 2.dp) {
        Row(
            Modifier.fillMaxWidth().padding(horizontal = 18.dp, vertical = 13.dp),
            verticalAlignment = Alignment.CenterVertically
        ) {
            Column(Modifier.weight(1f)) {
                Text(
                    "ECHOCRYPT",
                    color = Color.White,
                    fontWeight = FontWeight.Black,
                    fontSize = 21.sp,
                    letterSpacing = 2.sp
                )
                Text(
                    section.caption.uppercase(),
                    color = ShellMuted,
                    fontSize = 9.sp,
                    letterSpacing = 1.15.sp
                )
            }
            Column(horizontalAlignment = Alignment.End, verticalArrangement = Arrangement.spacedBy(4.dp)) {
                StatusPill(if (directEstablished) "DIRECT SECURE" else "DIRECT OFFLINE",
                    if (directEstablished) ShellGreen else ShellMuted)
                StatusPill(if (duplexRunning) "AUDIO READY" else "AUDIO STOPPED",
                    if (duplexRunning) ShellGold else ShellMuted)
            }
        }
    }
}

@Composable
private fun StatusPill(label: String, color: Color) {
    Surface(color = color.copy(alpha = 0.12f), shape = RoundedCornerShape(50)) {
        Text(
            label,
            color = color,
            fontSize = 8.sp,
            fontWeight = FontWeight.Bold,
            letterSpacing = 0.6.sp,
            modifier = Modifier.padding(horizontal = 9.dp, vertical = 5.dp)
        )
    }
}

@Composable
private fun PageColumn(content: @Composable ColumnScope.() -> Unit) {
    Column(
        Modifier.fillMaxSize().verticalScroll(rememberScrollState()).padding(18.dp),
        verticalArrangement = Arrangement.spacedBy(14.dp),
        content = content
    )
}

@Composable
private fun PageTitle(title: String, subtitle: String) {
    Column(verticalArrangement = Arrangement.spacedBy(4.dp)) {
        Text(title, color = Color.White, fontWeight = FontWeight.Bold, fontSize = 23.sp)
        Text(subtitle, color = ShellMuted, fontSize = 12.sp, lineHeight = 17.sp)
    }
}

@Composable
private fun SectionCard(
    title: String,
    subtitle: String? = null,
    content: @Composable ColumnScope.() -> Unit
) {
    Card(
        colors = CardDefaults.cardColors(containerColor = ShellPanel),
        shape = RoundedCornerShape(18.dp),
        modifier = Modifier.fillMaxWidth()
    ) {
        Column(
            Modifier.fillMaxWidth().padding(16.dp),
            verticalArrangement = Arrangement.spacedBy(10.dp)
        ) {
            Text(title.uppercase(), color = ShellGold, fontWeight = FontWeight.Bold, fontSize = 12.sp, letterSpacing = 1.1.sp)
            if (subtitle != null) Text(subtitle, color = ShellMuted, fontSize = 12.sp, lineHeight = 17.sp)
            content()
        }
    }
}

@Composable
private fun OverviewScreen(
    duplexRunning: Boolean,
    audioStatus: String,
    directStatus: String,
    directEstablished: Boolean,
    peersCount: Int,
    pendingFile: String?,
    latestReceivedFile: File?,
    receivedFrameCount: Int,
    onOpenDirect: () -> Unit,
    onOpenFiles: () -> Unit,
    onOpenIdentity: () -> Unit,
    onOpenAudio: () -> Unit,
    onOpenDiagnostics: () -> Unit
) {
    PageColumn {
        PageTitle("Overview", "Your acoustic link at a glance.")
        SectionCard("Connection status", "Direct messages and file transfer require an authenticated peer session.") {
            Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween) {
                Metric("DIRECT", if (directEstablished) "ESTABLISHED" else "NOT CONNECTED",
                    if (directEstablished) ShellGreen else ShellMuted)
                Metric("AUDIO", if (duplexRunning) "RUNNING" else "STOPPED",
                    if (duplexRunning) ShellGold else ShellMuted)
            }
            Text(directStatus, color = ShellMuted, fontSize = 11.sp, lineHeight = 16.sp)
            Button(
                onClick = onOpenDirect,
                modifier = Modifier.fillMaxWidth(),
                colors = ButtonDefaults.buttonColors(containerColor = ShellGold, contentColor = ShellBackground)
            ) { Text(if (directEstablished) "Open secure session" else "Open Direct communication") }
        }
        Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(10.dp)) {
            SummaryTile("TRUSTED PEERS", peersCount.toString(), Modifier.weight(1f), onOpenIdentity)
            SummaryTile("ECF3 FRAMES", receivedFrameCount.toString(), Modifier.weight(1f), onOpenDiagnostics)
        }
        SectionCard("File transfer", "Encrypted transfer with resume state and final SHA-256 verification.") {
            Text(
                pendingFile?.let { "Outgoing: $it" } ?: "No outgoing transfer currently pending.",
                color = if (pendingFile != null) ShellGold else ShellMuted,
                fontSize = 12.sp
            )
            Text(
                latestReceivedFile?.let { "Latest received: ${it.name} · ${formatBytes(it.length())}" }
                    ?: "No received files saved yet.",
                color = ShellMuted,
                fontSize = 11.sp
            )
            OutlinedButton(onClick = onOpenFiles, modifier = Modifier.fillMaxWidth()) {
                Text("Open file transfer")
            }
        }
        SectionCard("Quick access") {
            QuickAction("Audio controls", "Start or stop microphone and speaker capture.", onOpenAudio)
            HorizontalDivider(color = ShellLine)
            QuickAction("Identity & trust", "Create device identity and verify peer fingerprints.", onOpenIdentity)
            HorizontalDivider(color = ShellLine)
            QuickAction("Diagnostics", "Inspect received ECF3 frames and send a test frame.", onOpenDiagnostics)
        }
        Text(
            "Native Android client · ECF3 acoustic transport · ECH1 authenticated handshake · ECP1 encrypted channel",
            color = ShellMuted,
            fontSize = 9.sp,
            lineHeight = 14.sp
        )
    }
}

@Composable
private fun Metric(label: String, value: String, valueColor: Color) {
    Column(verticalArrangement = Arrangement.spacedBy(3.dp)) {
        Text(label, color = ShellMuted, fontSize = 9.sp, letterSpacing = 1.sp)
        Text(value, color = valueColor, fontSize = 11.sp, fontWeight = FontWeight.Bold)
    }
}

@Composable
private fun SummaryTile(title: String, value: String, modifier: Modifier, onClick: () -> Unit) {
    Surface(
        modifier = modifier,
        color = ShellPanel,
        shape = RoundedCornerShape(16.dp),
        onClick = onClick
    ) {
        Column(Modifier.padding(15.dp), verticalArrangement = Arrangement.spacedBy(5.dp)) {
            Text(title, color = ShellMuted, fontSize = 9.sp, letterSpacing = 0.8.sp)
            Text(value, color = ShellGold, fontSize = 25.sp, fontWeight = FontWeight.Bold)
        }
    }
}

@Composable
private fun QuickAction(title: String, description: String, onClick: () -> Unit) {
    TextButton(onClick = onClick, modifier = Modifier.fillMaxWidth()) {
        Column(Modifier.fillMaxWidth()) {
            Text(title, color = Color.White, fontWeight = FontWeight.SemiBold)
            Text(description, color = ShellMuted, fontSize = 11.sp)
        }
    }
}

@Composable
private fun DirectScreen(
    peers: List<TrustedPeersStore.TrustedPeer>,
    duplexRunning: Boolean,
    directEstablished: Boolean,
    status: String,
    messages: List<String>,
    error: String?,
    onRefreshPeers: () -> Unit,
    onStartInitiator: (String) -> Unit,
    onStartResponder: (String) -> Unit,
    onRetry: () -> Unit,
    onDisconnect: () -> Unit,
    onSend: (String) -> Unit
) {
    if (directEstablished) {
        MessengerPanel(
            title = "Secure Direct messenger",
            status = status,
            messages = messages,
            error = error,
            publicRoom = false,
            inputEnabled = duplexRunning,
            exitLabel = "Disconnect",
            onExit = onDisconnect,
            onSend = onSend
        )
    } else {
        PageColumn {
            PageTitle("Direct communication", "Connect to a trusted device. After authentication this page becomes the encrypted messenger.")
            if (!duplexRunning) {
                NoticeCard("Audio is stopped", "Open Audio and start Full Duplex before connecting or receiving acoustic packets.")
            }
            DirectCommunicationPanel(
                peers = peers,
                duplexRunning = duplexRunning,
                status = status,
                messages = messages,
                error = error,
                onRefreshPeers = onRefreshPeers,
                onStartInitiator = onStartInitiator,
                onStartResponder = onStartResponder,
                onRetry = onRetry,
                onDisconnect = onDisconnect,
                onSend = onSend
            )
        }
    }
}

@Composable
private fun FilesScreen(
    duplexRunning: Boolean,
    directEstablished: Boolean,
    status: String,
    pendingFile: String?,
    latestReceivedFile: File?,
    error: String?,
    onChooseFile: () -> Unit,
    onExportFile: () -> Unit,
    onOpenDirect: () -> Unit
) {
    PageColumn {
        PageTitle("File transfer", "Send and receive files through the authenticated encrypted Direct session.")
        SectionCard("Transfer requirements") {
            Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween) {
                Metric("AUDIO", if (duplexRunning) "READY" else "STOPPED", if (duplexRunning) ShellGold else ShellMuted)
                Metric("DIRECT", if (directEstablished) "SECURE" else "OFFLINE", if (directEstablished) ShellGreen else ShellMuted)
            }
            Text("Maximum file size: 10 MiB · Chunk size: 1,400 bytes", color = ShellMuted, fontSize = 11.sp)
            Text("Resume metadata is stored privately and negotiated again after reconnecting to the same trusted peer.", color = ShellMuted, fontSize = 11.sp, lineHeight = 16.sp)
        }
        SectionCard("Send file") {
            Text(
                pendingFile?.let { "Pending outgoing file: $it" } ?: "No pending outgoing file.",
                color = if (pendingFile != null) ShellGold else ShellMuted,
                fontSize = 12.sp
            )
            Button(
                onClick = onChooseFile,
                enabled = duplexRunning && directEstablished && pendingFile == null,
                modifier = Modifier.fillMaxWidth(),
                colors = ButtonDefaults.buttonColors(containerColor = ShellGold, contentColor = ShellBackground)
            ) { Text("Choose file") }
            if (!directEstablished) {
                TextButton(onClick = onOpenDirect, modifier = Modifier.fillMaxWidth()) {
                    Text("Connect to a trusted peer")
                }
            }
        }
        SectionCard("Received files") {
            if (latestReceivedFile?.exists() == true) {
                Text(latestReceivedFile.name, color = Color.White, fontWeight = FontWeight.SemiBold)
                Text(
                    "${formatBytes(latestReceivedFile.length())} · stored in app-private storage",
                    color = ShellMuted,
                    fontSize = 11.sp
                )
                Button(onClick = onExportFile, modifier = Modifier.fillMaxWidth()) {
                    Text("Export to device")
                }
            } else {
                Text("No completed file is currently available for export.", color = ShellMuted, fontSize = 12.sp)
            }
            Text("A file is marked delivered only after the receiver verifies SHA-256, commits it to storage and sends COMPLETE.", color = ShellMuted, fontSize = 10.sp, lineHeight = 15.sp)
        }
        error?.let { NoticeCard("Transfer / session error", it, isError = true) }
        Text(status, color = ShellMuted, fontSize = 10.sp, lineHeight = 15.sp)
    }
}

@Composable
private fun IdentityScreen(peersCount: Int, onPeersChanged: () -> Unit) {
    PageColumn {
        PageTitle("Identity & trust", "Manage this device identity and the peers allowed to authenticate with it.")
        SectionCard("Trust model", "Verify the peer fingerprint out-of-band before granting trust. Public-key import by itself is not authentication.") {
            Text("$peersCount trusted peer(s) on this device.", color = ShellGold, fontWeight = FontWeight.SemiBold)
        }
        IdentityManagementPanel(onPeersChanged = onPeersChanged)
    }
}

@Composable
private fun AudioScreen(
    duplexRunning: Boolean,
    status: String,
    level: Float,
    effects: String,
    error: String?,
    outputError: String?,
    onStart: () -> Unit,
    onStop: () -> Unit,
    onPlayTestTone: () -> Unit,
    speakerTestFrequencyHz: Int
) {
    PageColumn {
        PageTitle("Audio controls", "Full-duplex acoustic input and output are managed separately from the Direct session.")
        SectionCard("Full-duplex engine") {
            Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween) {
                Metric("ENGINE", if (duplexRunning) "RUNNING" else "STOPPED", if (duplexRunning) ShellGreen else ShellMuted)
                Metric("FORMAT", "48 kHz · MONO PCM16", ShellGold)
            }
            Text(status, color = ShellMuted, fontSize = 11.sp)
            Text("Input level · ${level.toInt()} dB", color = ShellGold, fontWeight = FontWeight.SemiBold, fontSize = 13.sp)
            Box(
                Modifier.fillMaxWidth().height(8.dp)
                    .background(ShellLine, RoundedCornerShape(50))
            ) {
                Box(
                    Modifier.fillMaxWidth(((level + 90f) / 90f).coerceIn(0f, 1f))
                        .height(8.dp)
                        .background(ShellGold, RoundedCornerShape(50))
                )
            }
            Text(effects, color = ShellMuted, fontSize = 10.sp)
            Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                Button(
                    onClick = onStart,
                    enabled = !duplexRunning,
                    modifier = Modifier.weight(1f),
                    colors = ButtonDefaults.buttonColors(containerColor = ShellGold, contentColor = ShellBackground)
                ) { Text("Start Duplex") }
                OutlinedButton(onClick = onStop, enabled = duplexRunning, modifier = Modifier.weight(1f)) {
                    Text("Stop")
                }
            }
        }
        SectionCard("Speaker test", "Play a tone to check the selected Android output device.") {
            Button(onClick = onPlayTestTone, enabled = !duplexRunning, modifier = Modifier.fillMaxWidth()) {
                Text("Play " + speakerTestFrequencyHz + " Hz test tone")
            }
            Text("The standalone output test is disabled while Full Duplex is active.", color = ShellMuted, fontSize = 10.sp)
            outputError?.let { Text(it, color = ShellRed, fontSize = 11.sp) }
        }
        if (error != null) NoticeCard("Audio error", error, isError = true)
        SectionCard("Audio processing") {
            Text("Echo cancellation and noise suppression are enabled only when the Android device exposes these platform effects.", color = ShellMuted, fontSize = 11.sp, lineHeight = 16.sp)
            Text(effects, color = ShellGold, fontSize = 11.sp)
        }
    }
}

@Composable
private fun DiagnosticsScreen(
    duplexRunning: Boolean,
    directEstablished: Boolean,
    receivedFrameCount: Int,
    lastReceivedFrame: String,
    receiveError: String?,
    modemStatus: String,
    outputError: String?,
    onSendDiagnosticFrame: () -> Unit,
    onOpenAudio: () -> Unit
) {
    PageColumn {
        PageTitle("Diagnostics", "Inspect acoustic framing and run a controlled ECF3 transmit test.")
        SectionCard("ECF3 receive") {
            Text("$receivedFrameCount frame(s) decoded", color = ShellGold, fontSize = 24.sp, fontWeight = FontWeight.Bold)
            Text(lastReceivedFrame, color = ShellMuted, fontSize = 11.sp, lineHeight = 16.sp)
            receiveError?.let { NoticeCard("Receive warning", it, isError = true) }
        }
        SectionCard("ECF3 transmit test") {
            Text("Sends a fixed diagnostic payload. It is not encrypted and must not run during a Direct session.", color = Color(0xFFFFC56A), fontSize = 11.sp, lineHeight = 16.sp)
            Button(
                onClick = onSendDiagnosticFrame,
                enabled = duplexRunning && !directEstablished,
                modifier = Modifier.fillMaxWidth(),
                colors = ButtonDefaults.buttonColors(containerColor = ShellGold, contentColor = ShellBackground)
            ) { Text("Queue ECF3 test frame") }
            Text(modemStatus, color = ShellMuted, fontSize = 11.sp)
            outputError?.let { Text(it, color = ShellRed, fontSize = 11.sp) }
            if (!duplexRunning) TextButton(onClick = onOpenAudio) { Text("Open Audio controls") }
        }
        SectionCard("Safe diagnostic use") {
            Text("Disconnect Direct before sending the unencrypted diagnostic frame. Use a second recording device or an acoustic loopback setup to inspect reception.", color = ShellMuted, fontSize = 11.sp, lineHeight = 16.sp)
        }
    }
}

@Composable
private fun SettingsScreen() {
    PageColumn {
        PageTitle("Settings", "Implementation-backed configuration and feature availability.")
        SectionCard("Current Direct transport") {
            SettingRow("Modem", "ECF3 · BFSK")
            SettingRow("Audio format", "48,000 Hz · mono PCM")
            SettingRow("Symbol rate", "200 baud")
            SettingRow("Padding", "Disabled")
            SettingRow("Frequency hopping", "Disabled")
            SettingRow("Handshake workers", "1")
            SettingRow("Retry setting", "1")
            Text("These values are currently fixed in the Android Direct transport code; this screen does not pretend they are editable.", color = ShellMuted, fontSize = 10.sp, lineHeight = 15.sp)
        }
        SectionCard("Cryptographic session") {
            SettingRow("Peer authentication", "ECH1 v2 · Ed25519 / X25519")
            SettingRow("Encrypted channel", "ECP1 · ChaCha20-Poly1305")
            SettingRow("Local private identity", "Android Keystore-backed vault")
        }
        SectionCard("File transfer") {
            SettingRow("Maximum file size", "10 MiB")
            SettingRow("Chunk size", "1,400 bytes")
            SettingRow("Resume state", "Persisted in app-private storage")
            SettingRow("Final integrity", "SHA-256 + COMPLETE")
        }
        SectionCard("Not exposed in this Android build") {
            AvailabilityRow("Selectable Low / Medium / Secure profile", "Not wired to the active Direct transport")
            AvailabilityRow("Ultrasonic mode", "Disabled in the active handshake")
            AvailabilityRow("Multiple acoustic workers", "Active worker count is fixed at one")
            AvailabilityRow("Background auto-reconnect", "User must re-establish authenticated Direct")
        }
        Text("ECHOCRYPT Android · Native development client · Debug build", color = ShellMuted, fontSize = 9.sp)
    }
}

@Composable
private fun SettingRow(label: String, value: String) {
    Row(
        Modifier.fillMaxWidth(),
        horizontalArrangement = Arrangement.SpaceBetween,
        verticalAlignment = Alignment.CenterVertically
    ) {
        Text(label, color = ShellMuted, fontSize = 11.sp)
        Spacer(Modifier.width(10.dp))
        Text(value, color = Color.White, fontSize = 11.sp, fontWeight = FontWeight.SemiBold)
    }
}

@Composable
private fun AvailabilityRow(title: String, status: String) {
    Column(verticalArrangement = Arrangement.spacedBy(3.dp)) {
        Text(title, color = Color.White, fontSize = 12.sp, fontWeight = FontWeight.SemiBold)
        Text(status, color = Color(0xFFFFC56A), fontSize = 10.sp)
    }
}

@Composable
private fun NoticeCard(title: String, message: String, isError: Boolean = false) {
    Surface(
        color = (if (isError) ShellRed else ShellGold).copy(alpha = 0.10f),
        shape = RoundedCornerShape(14.dp),
        modifier = Modifier.fillMaxWidth()
    ) {
        Column(Modifier.padding(13.dp), verticalArrangement = Arrangement.spacedBy(4.dp)) {
            Text(title, color = if (isError) ShellRed else ShellGold, fontWeight = FontWeight.Bold, fontSize = 12.sp)
            Text(message, color = Color.White, fontSize = 11.sp, lineHeight = 16.sp)
        }
    }
}

private fun formatBytes(bytes: Long): String = when {
    bytes >= 1024L * 1024L -> String.format("%.2f MiB", bytes / (1024.0 * 1024.0))
    bytes >= 1024L -> String.format("%.1f KiB", bytes / 1024.0)
    else -> "$bytes bytes"
}
