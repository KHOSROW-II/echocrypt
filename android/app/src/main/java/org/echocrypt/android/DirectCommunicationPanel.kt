package org.echocrypt.android

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.DropdownMenu
import androidx.compose.material3.DropdownMenuItem
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import org.echocrypt.android.protocol.TrustedPeersStore

private val DirectGold = Color(0xFFE5B85C)
private val DirectMuted = Color(0xFF8995A7)
private val DirectPanel = Color(0xFF151D29)

@Composable
fun DirectCommunicationPanel(
    peers: List<TrustedPeersStore.TrustedPeer>,
    duplexRunning: Boolean,
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
    var selectedFingerprint by remember { mutableStateOf("") }
    var menuExpanded by remember { mutableStateOf(false) }
    var draft by remember { mutableStateOf("") }
    LaunchedEffect(peers) {
        if (peers.none { it.identity.fingerprint == selectedFingerprint }) {
            selectedFingerprint = peers.firstOrNull()?.identity?.fingerprint.orEmpty()
        }
    }
    val selectedPeer = peers.firstOrNull { it.identity.fingerprint == selectedFingerprint }
    val established = status.startsWith("ESTABLISHED")
    val active = status != "IDLE" && status != "CLOSED" && status != "READY"

    Card(
        colors = CardDefaults.cardColors(containerColor = DirectPanel),
        modifier = Modifier.fillMaxWidth()
    ) {
        Column(
            Modifier.padding(18.dp),
            verticalArrangement = Arrangement.spacedBy(10.dp)
        ) {
            Text("DIRECT · AUTHENTICATED SESSION", color = DirectGold, fontWeight = FontWeight.Bold, letterSpacing = 1.2.sp)
            Text(status, color = if (established) DirectGold else DirectMuted, fontSize = 12.sp)
            Text(
                "Transport: ECF3 · 48 kHz PCM · 200 baud · no frequency hopping · 1 worker",
                color = DirectMuted,
                fontSize = 10.sp
            )
            Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                Button(onClick = { menuExpanded = true }, enabled = !active) {
                    Text(selectedPeer?.name ?: "Select trusted peer")
                }
                DropdownMenu(expanded = menuExpanded, onDismissRequest = { menuExpanded = false }) {
                    peers.forEach { peer ->
                        DropdownMenuItem(
                            text = { Text(peer.name) },
                            onClick = {
                                selectedFingerprint = peer.identity.fingerprint
                                menuExpanded = false
                            }
                        )
                    }
                }
                TextButton(onClick = onRefreshPeers) { Text("Refresh") }
            }
            if (peers.isEmpty()) {
                Text(
                    "No trusted peer is available. Add and fingerprint-verify a public identity in the Identity & Trust panel.",
                    color = DirectMuted,
                    fontSize = 12.sp
                )
            }
            Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                Button(
                    onClick = { onStartInitiator(selectedFingerprint) },
                    enabled = duplexRunning && selectedFingerprint.isNotBlank() && !active,
                    modifier = Modifier.weight(1f)
                ) { Text("Connect") }
                Button(
                    onClick = { onStartResponder(selectedFingerprint) },
                    enabled = duplexRunning && selectedFingerprint.isNotBlank() && !active,
                    modifier = Modifier.weight(1f)
                ) { Text("Wait for peer") }
            }
            Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                TextButton(
                    onClick = onRetry,
                    enabled = duplexRunning && (status.startsWith("WAIT_REPLY") ||
                        status.startsWith("WAIT_RESP_FINISH") || status.startsWith("WAIT_INIT_FINISH"))
                ) { Text("Retry handshake") }
                TextButton(onClick = onDisconnect, enabled = active) { Text("Disconnect") }
            }
            OutlinedTextField(
                value = draft,
                onValueChange = { draft = it },
                label = { Text("Encrypted message") },
                enabled = established && duplexRunning,
                modifier = Modifier.fillMaxWidth(),
                minLines = 2
            )
            Button(
                onClick = {
                    val outgoing = draft.trim()
                    if (outgoing.isNotEmpty()) {
                        onSend(outgoing)
                        draft = ""
                    }
                },
                enabled = established && duplexRunning && draft.isNotBlank(),
                modifier = Modifier.fillMaxWidth()
            ) { Text("Encrypt & transmit") }
            if (messages.isEmpty()) {
                Text("No messages in this session.", color = DirectMuted, fontSize = 11.sp)
            } else {
                Column(
                    Modifier.fillMaxWidth().height(180.dp).verticalScroll(rememberScrollState()),
                    verticalArrangement = Arrangement.spacedBy(6.dp)
                ) {
                    messages.takeLast(30).forEach { message ->
                        Text(message, color = Color.White, fontSize = 12.sp)
                    }
                }
            }
            error?.let { Text(it, color = Color(0xFFFF7777), fontSize = 11.sp) }
        }
    }
}
