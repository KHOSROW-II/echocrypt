package org.echocrypt.android

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.imePadding
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp

@Composable
fun MessengerPanel(
    title: String, status: String, messages: List<String>, error: String?,
    publicRoom: Boolean, inputEnabled: Boolean, exitLabel: String,
    onExit: () -> Unit, onSend: (String) -> Unit
) {
    var draft by remember { mutableStateOf("") }
    val scroll = rememberScrollState()
    LaunchedEffect(messages.size) { scroll.animateScrollTo(scroll.maxValue) }

    Column(Modifier.fillMaxSize().imePadding().padding(horizontal = 12.dp, vertical = 10.dp),
        verticalArrangement = Arrangement.spacedBy(9.dp)) {
        Card(colors = CardDefaults.cardColors(containerColor = Color(0xFF151D29)),
            shape = RoundedCornerShape(16.dp), modifier = Modifier.fillMaxWidth()) {
            androidx.compose.foundation.layout.Row(
                Modifier.fillMaxWidth().padding(13.dp), verticalAlignment = Alignment.CenterVertically
            ) {
                Column(Modifier.weight(1f), verticalArrangement = Arrangement.spacedBy(3.dp)) {
                    Text(title, color = Color.White, fontWeight = FontWeight.Bold, fontSize = 16.sp)
                    Text(status, color = Color(0xFF85D6A0), fontSize = 10.sp)
                }
                OutlinedButton(onClick = onExit) { Text(exitLabel, fontSize = 10.sp) }
            }
        }
        if (publicRoom) {
            Surface(color = Color(0xFFE5B85C).copy(alpha = 0.12f),
                shape = RoundedCornerShape(12.dp), modifier = Modifier.fillMaxWidth()) {
                Text(
                    "OPEN BROADCAST · NOT CONFIDENTIAL. The compatibility transform uses a public key; anyone who knows the protocol can read and forge messages.",
                    color = Color(0xFFFFD47D), fontSize = 10.sp, lineHeight = 15.sp,
                    modifier = Modifier.padding(11.dp)
                )
            }
        }
        Card(colors = CardDefaults.cardColors(containerColor = Color(0xFF111925)),
            modifier = Modifier.weight(1f).fillMaxWidth()) {
            if (messages.isEmpty()) {
                Box(Modifier.fillMaxSize(), contentAlignment = Alignment.Center) {
                    Text("No messages yet. Start the conversation.", color = Color(0xFF8995A7), fontSize = 12.sp)
                }
            } else {
                Column(Modifier.fillMaxSize().verticalScroll(scroll).padding(11.dp),
                    verticalArrangement = Arrangement.spacedBy(8.dp)) {
                    messages.forEach { message ->
                        val local = message.startsWith("Me:")
                        val label = message.substringBefore(":").ifBlank { "Peer" }
                        val body = message.substringAfter(":", message).trim()
                        androidx.compose.foundation.layout.Row(
                            Modifier.fillMaxWidth(),
                            horizontalArrangement = if (local) Arrangement.End else Arrangement.Start
                        ) {
                            Column(Modifier.fillMaxWidth(0.88f),
                                horizontalAlignment = if (local) Alignment.End else Alignment.Start) {
                                Text(if (local) "YOU" else label.uppercase(),
                                    color = if (local) Color(0xFFE5B85C) else Color(0xFF8995A7),
                                    fontSize = 8.sp, fontWeight = FontWeight.Bold)
                                Surface(color = if (local) Color(0xFF5D4A2C) else Color(0xFF202B3A),
                                    shape = RoundedCornerShape(14.dp)) {
                                    Text(body, color = Color.White, fontSize = 13.sp, lineHeight = 19.sp,
                                        modifier = Modifier.padding(horizontal = 12.dp, vertical = 9.dp))
                                }
                            }
                        }
                    }
                }
            }
        }
        error?.let { Text(it, color = Color(0xFFFF7777), fontSize = 10.sp) }
        androidx.compose.foundation.layout.Row(
            Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(8.dp),
            verticalAlignment = Alignment.Bottom
        ) {
            OutlinedTextField(value = draft, onValueChange = { draft = it }, enabled = inputEnabled,
                modifier = Modifier.weight(1f), label = { Text("Message") }, maxLines = 4)
            Button(onClick = {
                val value = draft.trim()
                if (value.isNotEmpty()) { onSend(value); draft = "" }
            }, enabled = inputEnabled && draft.isNotBlank(), modifier = Modifier.height(54.dp)) {
                Text("Send")
            }
        }
    }
}

@Composable
fun BroadcastRoomScreen(
    joined: Boolean, audioReady: Boolean, messages: List<String>,
    status: String, error: String?, onJoin: () -> Unit, onLeave: () -> Unit,
    onSend: (String) -> Unit, onOpenAudio: () -> Unit
) {
    if (joined) {
        MessengerPanel("Open Broadcast room", status, messages, error, true, audioReady,
            "Leave", onLeave, onSend)
    } else {
        Column(Modifier.fillMaxSize().verticalScroll(rememberScrollState()).padding(18.dp),
            verticalArrangement = Arrangement.spacedBy(14.dp)) {
            Text("Broadcast", color = Color.White, fontWeight = FontWeight.Bold, fontSize = 23.sp)
            Text("Open acoustic room · no identity or peer handshake required.",
                color = Color(0xFF8995A7), fontSize = 12.sp)
            Card(colors = CardDefaults.cardColors(containerColor = Color(0xFF151D29)),
                modifier = Modifier.fillMaxWidth()) {
                Column(Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(10.dp)) {
                    Text("JOIN OPEN BROADCAST ROOM", color = Color(0xFFE5B85C), fontWeight = FontWeight.Bold)
                    Text("This mode uses the same ECBR v1 public Broadcast framing as desktop. It is an open channel, not end-to-end confidential communication.",
                        color = Color.White, fontSize = 12.sp, lineHeight = 18.sp)
                    Text("Anyone transmitting compatible Broadcast packets on the same acoustic channel can be received. No room password, peer key, or member list exists in this protocol.",
                        color = Color(0xFF8995A7), fontSize = 11.sp, lineHeight = 16.sp)
                    Button(onClick = onJoin, enabled = audioReady, modifier = Modifier.fillMaxWidth()) {
                        Text("Join open room")
                    }
                    if (!audioReady) {
                        Text("Start Full Duplex before joining so this device can receive and transmit.",
                            color = Color(0xFFFFC56A), fontSize = 11.sp)
                        OutlinedButton(onClick = onOpenAudio, modifier = Modifier.fillMaxWidth()) {
                            Text("Open audio controls")
                        }
                    }
                }
            }
        }
    }
}
