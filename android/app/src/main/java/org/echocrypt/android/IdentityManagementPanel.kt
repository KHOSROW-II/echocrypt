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
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateListOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalClipboardManager
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.AnnotatedString
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import org.echocrypt.android.protocol.NativeIdentity
import org.echocrypt.android.protocol.TrustedPeersStore

private val IdentityGold = Color(0xFFE5B85C)
private val IdentityMuted = Color(0xFF8995A7)
private val IdentityPanel = Color(0xFF151D29)

/** Local identity and explicit out-of-band peer trust management. */
@Composable
fun IdentityManagementPanel(onPeersChanged: () -> Unit = {}) {
    val context = LocalContext.current.applicationContext
    val vault = remember(context) { NativeIdentity.Vault(context) }
    val peersStore = remember(context) { TrustedPeersStore(context) }
    val clipboard = LocalClipboardManager.current
    var identityJson by remember { mutableStateOf("") }
    var fingerprint by remember { mutableStateOf("") }
    var peerName by remember { mutableStateOf("") }
    var peerJson by remember { mutableStateOf("") }
    var confirmedFingerprint by remember { mutableStateOf("") }
    var message by remember { mutableStateOf<String?>(null) }
    var localError by remember { mutableStateOf<String?>(null) }
    val peers = remember { mutableStateListOf<TrustedPeersStore.TrustedPeer>() }

    fun refresh() {
        try {
            identityJson = if (vault.exists()) vault.exportPublicIdentity() else ""
            fingerprint = if (identityJson.isNotEmpty()) {
                NativeIdentity.PublicIdentity.fromJson(identityJson).fingerprint
            } else ""
            peers.clear()
            peers.addAll(peersStore.list())
            localError = null
        } catch (error: Exception) {
            localError = error.message ?: "Could not load local identity data"
        }
    }

    LaunchedEffect(vault, peersStore) { refresh() }

    Card(
        colors = CardDefaults.cardColors(containerColor = IdentityPanel),
        modifier = Modifier.fillMaxWidth()
    ) {
        Column(
            Modifier.padding(18.dp),
            verticalArrangement = Arrangement.spacedBy(10.dp)
        ) {
            Text("IDENTITY & TRUST", color = IdentityGold, fontWeight = FontWeight.Bold, letterSpacing = 1.4.sp)
            Text("Local Ed25519 identity", fontWeight = FontWeight.SemiBold)
            if (identityJson.isEmpty()) {
                Text(
                    "No local identity exists. Create one before establishing authenticated Direct sessions.",
                    color = IdentityMuted,
                    fontSize = 12.sp
                )
                Button(onClick = {
                    try {
                        val privateIdentity = vault.create()
                        try {
                            identityJson = privateIdentity.publicIdentity.toJson()
                            fingerprint = privateIdentity.publicIdentity.fingerprint
                        } finally {
                            privateIdentity.destroy()
                        }
                        message = "Identity created and encrypted with Android Keystore."
                        localError = null
                    } catch (error: Exception) {
                        localError = error.message ?: "Identity creation failed"
                    }
                }) { Text("Create device identity") }
            } else {
                Text("Fingerprint (SHA-256)", color = IdentityMuted, fontSize = 11.sp)
                Text(fingerprint, color = IdentityGold, fontSize = 12.sp)
                Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    TextButton(onClick = {
                        clipboard.setText(AnnotatedString(fingerprint))
                        message = "Fingerprint copied"
                    }) { Text("Copy fingerprint") }
                    TextButton(onClick = {
                        clipboard.setText(AnnotatedString(identityJson))
                        message = "Public identity JSON copied"
                    }) { Text("Copy public identity") }
                }
                Text(
                    "Only the public key is shown/exported. The private signing seed remains in the encrypted local vault.",
                    color = IdentityMuted,
                    fontSize = 11.sp
                )
            }

            Spacer(Modifier.height(4.dp))
            Text("Add trusted peer", color = IdentityGold, fontWeight = FontWeight.SemiBold)
            OutlinedTextField(
                value = peerName,
                onValueChange = { peerName = it },
                label = { Text("Peer name") },
                singleLine = true,
                modifier = Modifier.fillMaxWidth()
            )
            OutlinedTextField(
                value = peerJson,
                onValueChange = { peerJson = it },
                label = { Text("Peer public identity JSON") },
                minLines = 3,
                modifier = Modifier.fillMaxWidth()
            )
            OutlinedTextField(
                value = confirmedFingerprint,
                onValueChange = { confirmedFingerprint = it },
                label = { Text("Verified SHA-256 fingerprint (64 hex chars)") },
                minLines = 2,
                modifier = Modifier.fillMaxWidth()
            )
            Text(
                "Trust is granted only when the fingerprint entered here matches the imported key. Verify it with the peer over a separate trusted channel; importing JSON alone is not authentication.",
                color = IdentityMuted,
                fontSize = 11.sp
            )
            Button(
                onClick = {
                    try {
                        peersStore.import(peerJson, peerName, confirmedFingerprint)
                        peerName = ""
                        peerJson = ""
                        confirmedFingerprint = ""
                        refresh()
                        onPeersChanged()
                        message = "Peer added to trusted contacts."
                        localError = null
                    } catch (error: Exception) {
                        localError = error.message ?: "Peer import failed"
                    }
                },
                enabled = peerName.isNotBlank() && peerJson.isNotBlank() && confirmedFingerprint.isNotBlank()
            ) { Text("Verify fingerprint & trust peer") }

            Spacer(Modifier.height(4.dp))
            Text("Trusted contacts · ${peers.size}", color = IdentityGold, fontWeight = FontWeight.SemiBold)
            if (peers.isEmpty()) {
                Text("No trusted peers yet.", color = IdentityMuted, fontSize = 12.sp)
            } else {
                peers.forEach { peer ->
                    Column(verticalArrangement = Arrangement.spacedBy(4.dp)) {
                        Text(peer.name, fontWeight = FontWeight.SemiBold)
                        Text(peer.identity.fingerprint, color = IdentityMuted, fontSize = 10.sp)
                        TextButton(onClick = {
                            try {
                                peersStore.remove(peer.identity.fingerprint)
                                refresh()
                                onPeersChanged()
                                message = "Trusted peer removed."
                                localError = null
                            } catch (error: Exception) {
                                localError = error.message ?: "Could not remove peer"
                            }
                        }) { Text("Remove trust", color = Color(0xFFFF7777)) }
                    }
                }
            }
            message?.let { Text(it, color = IdentityGold, fontSize = 11.sp) }
            localError?.let { Text(it, color = Color(0xFFFF7777), fontSize = 12.sp) }
            TextButton(onClick = { refresh() }) { Text("Refresh identity & contacts") }
        }
    }
}
