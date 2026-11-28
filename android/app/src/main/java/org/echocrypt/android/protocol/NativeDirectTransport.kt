package org.echocrypt.android.protocol

import android.content.Context
import java.io.File

/**
 * Connects the authenticated ECH1 handshake and ECP1 secure channel to the
 * native ECF3 acoustic encoder. Incoming ECF3 payloads must be passed here in
 * order; outgoing payloads are modulated and queued on the DuplexAudioSession.
 */
class NativeDirectTransport(
    context: Context,
    private val enqueueAudio: (FloatArray) -> Boolean,
    private val onStateChanged: (String) -> Unit,
    private val onTextReceived: (String) -> Unit,
    private val onError: (String) -> Unit,
    private val onFileReceived: (File) -> Unit = {}
) {
    private val vault = NativeIdentity.Vault(context.applicationContext)
    private val peers = TrustedPeersStore(context.applicationContext)
    private val receivedFilesDirectory = File(context.applicationContext.filesDir, "received")
    private var localIdentity: NativeIdentity.PrivateIdentity? = null
    private var handshake: NativePeerHandshake? = null
    private var reliable: ReliablePacketLayer? = null
    private var fileTransfer: NativeFileTransfer? = null
    private var peerName: String = ""

    @Synchronized fun trustedPeers(): List<TrustedPeersStore.TrustedPeer> = peers.list()

    val isActive: Boolean
        @Synchronized get() = handshake?.state?.let { it != NativePeerHandshake.State.CLOSED } == true
    val isEstablished: Boolean
        @Synchronized get() = handshake?.established == true

    @Synchronized fun startInitiator(peerFingerprint: String) {
        start(peerFingerprint, NativePeerHandshake.Role.INITIATOR)
        try {
            transmit(checkNotNull(handshake).start())
            publishState()
        } catch (error: Exception) {
            onError(error.message ?: "Could not start Direct handshake")
            closeUnlocked()
            onStateChanged("IDLE")
            throw error
        }
    }

    @Synchronized fun startResponder(peerFingerprint: String) {
        start(peerFingerprint, NativePeerHandshake.Role.RESPONDER)
        publishState()
    }

    private fun start(peerFingerprint: String, role: NativePeerHandshake.Role) {
        closeUnlocked()
        val peer = peers.list().firstOrNull { it.identity.fingerprint == peerFingerprint }
            ?: throw IllegalArgumentException("Select a trusted peer before starting Direct")
        check(vault.exists()) { "Create the local device identity first" }
        val local = vault.load()
        try {
            val profile = NativeSecurityProfile.custom(
                symbolRate = 200, paddingBlock = 0, frequencyHopping = false, retries = 1
            )
            val direct = NativePeerHandshake(
                role = role, identity = local, peerIdentity = peer.identity,
                securityProfile = profile, ultrasonicEnabled = false, maxWorkers = 1
            )
            localIdentity = local
            handshake = direct
            peerName = peer.name
            onStateChanged(if (role == NativePeerHandshake.Role.INITIATOR)
                "WAIT_REPLY · authenticating $peerName" else "WAIT_HELLO · listening for $peerName")
        } catch (error: Exception) {
            local.destroy()
            throw error
        }
    }

    @Synchronized fun onIncomingPayload(payload: ByteArray) {
        val active = handshake ?: return
        try {
            if (active.established) {
                ensureReliableLayer(active)
                checkNotNull(reliable).receive(payload, android.os.SystemClock.elapsedRealtime())
                return
            }
            val response = active.receive(payload)
            if (response != null) transmit(response)
            publishState()
            if (active.established) {
                ensureReliableLayer(active)
                localIdentity?.destroy()
                localIdentity = null
                onStateChanged("ESTABLISHED · $peerName · ECP1 encrypted")
            }
        } catch (error: Exception) {
            onError(error.message ?: "Direct packet rejected")
        }
    }

    @Synchronized fun sendText(text: String) {
        require(text.isNotBlank()) { "Message cannot be empty" }
        val bytes = text.toByteArray(Charsets.UTF_8)
        require(bytes.size <= ReliablePacketLayer.MAX_MESSAGE_BYTES) { "Message exceeds reliable transport limit" }
        val active = checkNotNull(handshake) { "No Direct session is active" }
        check(active.established) { "Direct handshake has not completed" }
        ensureReliableLayer(active)
        checkNotNull(reliable).send(bytes, android.os.SystemClock.elapsedRealtime())
    }

    @Synchronized fun startFileTransfer(file: File, displayName: String = file.name) {
        val active = checkNotNull(handshake) { "No Direct session is active" }
        check(active.established) { "Direct handshake has not completed" }
        ensureReliableLayer(active)
        checkNotNull(fileTransfer).start(file, displayName)
    }

    @Synchronized fun tick() { reliable?.tick(android.os.SystemClock.elapsedRealtime()) }

    @Synchronized fun retryHandshake() {
        val retry = checkNotNull(handshake) { "No Direct handshake is active" }.retryPacket
            ?: throw IllegalStateException("There is no handshake flight waiting for retry")
        transmit(retry)
        publishState()
    }

    @Synchronized fun disconnect() {
        closeUnlocked()
        onStateChanged("IDLE")
    }

    private fun ensureReliableLayer(active: NativePeerHandshake) {
        if (reliable != null) return
        fileTransfer = NativeFileTransfer(
            storageDirectory = receivedFilesDirectory,
            sendPacket = { packet -> checkNotNull(reliable).send(packet, android.os.SystemClock.elapsedRealtime()) },
            onProgress = { value -> onStateChanged("FILE · $value") },
            onReceived = onFileReceived,
            onError = onError
        )
        reliable = ReliablePacketLayer(
            encrypt = { plain -> active.sendPayload(plain, maxPacketSize = 1024) },
            decrypt = { encrypted -> active.receivePayload(encrypted) },
            transmit = { encrypted -> transmit(encrypted) },
            onMessage = { bytes ->
                try {
                    if (NativeFileTransfer.isTransferPacket(bytes)) {
                        checkNotNull(fileTransfer).receive(bytes)
                    } else {
                        val decoded = Charsets.UTF_8.newDecoder()
                            .onMalformedInput(java.nio.charset.CodingErrorAction.REPORT)
                            .onUnmappableCharacter(java.nio.charset.CodingErrorAction.REPORT)
                            .decode(java.nio.ByteBuffer.wrap(bytes)).toString()
                        onTextReceived(decoded)
                    }
                } catch (error: Exception) {
                    onError(error.message ?: "Received application packet is invalid")
                }
            },
            onDelivered = { _ -> fileTransfer?.onReliablePacketDelivered() },
            onFailure = { id, reason -> onError("Message #$id failed: $reason") }
        )
    }

    private fun transmit(packet: ByteArray) {
        require(packet.isNotEmpty() && packet.size <= 1024) { "Direct protocol packet exceeds the current ECF3 payload limit" }
        val frame = NativeModem.encode(packet)
        check(enqueueAudio(frame.samples)) { "Audio playback queue is full or duplex audio is not running" }
    }

    private fun publishState() {
        val state = handshake?.state ?: NativePeerHandshake.State.CLOSED
        onStateChanged("$state · $peerName · ECF3 200 baud")
    }

    private fun closeUnlocked() {
        fileTransfer?.close()
        fileTransfer = null
        reliable?.close()
        reliable = null
        handshake?.close()
        handshake = null
        localIdentity?.destroy()
        localIdentity = null
        peerName = ""
    }
}
