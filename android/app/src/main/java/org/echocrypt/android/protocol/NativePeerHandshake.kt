package org.echocrypt.android.protocol

import java.nio.ByteBuffer
import java.nio.ByteOrder
import java.security.MessageDigest
import java.security.SecureRandom

/**
 * ECH1 v2 mutual-authentication handshake, wire-compatible with
 * security/peer_handshake.py. Caller transports each returned byte array as
 * one complete acoustic packet and feeds received packets to receive().
 */
class NativePeerHandshake(
    private val role: Role,
    private val identity: NativeIdentity.PrivateIdentity,
    peerIdentity: NativeIdentity.PublicIdentity,
    private val securityProfile: NativeSecurityProfile = NativeSecurityProfile.preset(),
    private val ultrasonicEnabled: Boolean = false,
    private val maxWorkers: Int = 1
) {
    enum class Role(val code: Int) { INITIATOR(0), RESPONDER(1) }
    enum class State { READY, WAIT_HELLO, WAIT_REPLY, WAIT_INIT_FINISH, WAIT_RESP_FINISH, ESTABLISHED, CLOSED }

    data class Session(
        val tx: NativeCrypto.SecureChannel,
        val rx: NativeCrypto.SecureChannel,
        val peerFingerprint: String,
        val hoppingKey: ByteArray?,
        val securityProfile: NativeSecurityProfile
    ) {
        fun encryptText(text: String): ByteArray =
            tx.encrypt(text.toByteArray(Charsets.UTF_8), TEXT_AAD)

        fun decryptText(packet: ByteArray): String =
            rx.decrypt(packet, TEXT_AAD).toString(Charsets.UTF_8)

        fun encryptPayload(payload: ByteArray): ByteArray = tx.encrypt(payload, DATA_AAD)
        fun decryptPayload(packet: ByteArray): ByteArray = rx.decrypt(packet, DATA_AAD)

        fun close() { tx.destroy(); rx.destroy(); hoppingKey?.fill(0) }
    }

    private val peerPublic = peerIdentity.publicKey.copyOf()
    private val peerFingerprint = peerIdentity.fingerprint
    private val localFingerprint = identity.publicIdentity.fingerprint
    private val random = SecureRandom()
    private val peerRole = if (role == Role.INITIATOR) Role.RESPONDER else Role.INITIATOR

    var state: State = if (role == Role.INITIATOR) State.READY else State.WAIT_HELLO
        private set
    var negotiatedProfile: NativeSecurityProfile = securityProfile
        private set
    var peerUltrasonicEnabled: Boolean = false
        private set
    var peerMaxWorkers: Int = 1
        private set
    var negotiatedUltrasonicEnabled: Boolean = false
        private set
    var negotiatedWorkerCount: Int = 1
        private set
    var session: Session? = null
        private set

    private var localEphemeral: NativeIdentity.X25519KeyPair? = null
    private var peerEphemeral: ByteArray? = null
    private var helloI: ByteArray? = null
    private var helloR: ByteArray? = null
    private var finishI: ByteArray? = null
    private var finishR: ByteArray? = null
    private var transcriptHash: ByteArray? = null

    init {
        require(peerPublic.size == 32)
        require(peerFingerprint == NativeIdentity.sha256(peerPublic)) { "Peer identity fingerprint mismatch" }
        require(localFingerprint != peerFingerprint) { "Local and peer identities must be different" }
        require(maxWorkers in 1..8) { "maxWorkers must be between 1 and 8" }
    }

    val established: Boolean get() = state == State.ESTABLISHED

    val retryPacket: ByteArray?
        get() = when {
            role == Role.INITIATOR && state == State.WAIT_REPLY -> helloI?.copyOf()
            role == Role.INITIATOR && state == State.WAIT_RESP_FINISH -> finishI?.copyOf()
            role == Role.RESPONDER && state == State.WAIT_INIT_FINISH -> helloR?.copyOf()
            else -> null
        }

    @Synchronized
    fun start(): ByteArray {
        check(role == Role.INITIATOR && state == State.READY && helloI == null) {
            "Only a new initiator can start the handshake"
        }
        localEphemeral = NativeIdentity.x25519KeyPair()
        helloI = createHello(Role.INITIATOR, negotiatedProfile)
        state = State.WAIT_REPLY
        return helloI!!.copyOf()
    }

    @Synchronized
    fun receive(input: ByteArray): ByteArray? {
        check(state != State.CLOSED) { "Handshake is closed" }

        if (role == Role.RESPONDER && state == State.ESTABLISHED &&
            finishI != null && input.contentEquals(finishI)) return finishR?.copyOf()
        if (role == Role.INITIATOR && state == State.ESTABLISHED &&
            finishR != null && input.contentEquals(finishR)) return null
        check(state != State.ESTABLISHED) { "Handshake is no longer accepting packets" }

        if (role == Role.RESPONDER && state == State.WAIT_INIT_FINISH &&
            helloI != null && input.contentEquals(helloI)) return helloR?.copyOf()
        if (role == Role.INITIATOR && state == State.WAIT_RESP_FINISH &&
            helloR != null && input.contentEquals(helloR)) return finishI?.copyOf()

        if (role == Role.RESPONDER && state == State.WAIT_HELLO) {
            val peer = verifyHello(input, Role.INITIATOR)
            peerEphemeral = peer.ephemeral
            negotiate(peer)
            helloI = input.copyOf()
            localEphemeral = NativeIdentity.x25519KeyPair()
            helloR = createHello(Role.RESPONDER, negotiatedProfile)
            deriveSession()
            state = State.WAIT_INIT_FINISH
            return helloR!!.copyOf()
        }

        if (role == Role.INITIATOR && state == State.WAIT_REPLY) {
            val peer = verifyHello(input, Role.RESPONDER)
            peerEphemeral = peer.ephemeral
            negotiate(peer)
            helloR = input.copyOf()
            deriveSession()
            state = State.WAIT_RESP_FINISH
            finishI = makeFinish(Role.INITIATOR)
            return finishI!!.copyOf()
        }

        if (role == Role.RESPONDER && state == State.WAIT_INIT_FINISH) {
            verifyFinish(input, Role.INITIATOR)
            finishI = input.copyOf()
            finishR = makeFinish(Role.RESPONDER)
            state = State.ESTABLISHED
            return finishR!!.copyOf()
        }

        if (role == Role.INITIATOR && state == State.WAIT_RESP_FINISH) {
            verifyFinish(input, Role.RESPONDER)
            finishR = input.copyOf()
            state = State.ESTABLISHED
            return null
        }
        throw IllegalArgumentException("Unexpected handshake packet in state $state")
    }

    @Synchronized
    fun sendText(text: String, maxPacketSize: Int = 1024): ByteArray {
        check(established) { "Peer session is not established" }
        require(maxPacketSize >= 32)
        val active = checkNotNull(session)
        val payload = text.toByteArray(Charsets.UTF_8)
        require(active.tx.encryptedSize(payload.size) <= maxPacketSize) {
            "Message exceeds configured modem packet limit"
        }
        return active.encryptText(text)
    }

    @Synchronized
    fun receiveText(packet: ByteArray): String {
        check(established) { "Peer session is not established" }
        return checkNotNull(session).decryptText(packet)
    }

    @Synchronized
    fun sendPayload(payload: ByteArray, maxPacketSize: Int = 1024): ByteArray {
        check(established) { "Peer session is not established" }
        require(checkNotNull(session).tx.encryptedSize(payload.size) <= maxPacketSize) {
            "Reliable packet exceeds configured modem packet limit"
        }
        return checkNotNull(session).encryptPayload(payload)
    }

    @Synchronized
    fun receivePayload(packet: ByteArray): ByteArray {
        check(established) { "Peer session is not established" }
        return checkNotNull(session).decryptPayload(packet)
    }

    @Synchronized
    fun close() {
        session?.close()
        session = null
        localEphemeral?.destroy()
        localEphemeral = null
        peerEphemeral?.fill(0)
        peerEphemeral = null
        helloI?.fill(0); helloR?.fill(0)
        finishI?.fill(0); finishR?.fill(0)
        transcriptHash?.fill(0)
        helloI = null; helloR = null; finishI = null; finishR = null
        transcriptHash = null
        state = State.CLOSED
    }

    private data class PeerHello(
        val ephemeral: ByteArray,
        val profile: NativeSecurityProfile,
        val ultrasonic: Boolean,
        val workers: Int
    )

    private fun createHello(senderRole: Role, profile: NativeSecurityProfile): ByteArray {
        val ephemeral = checkNotNull(localEphemeral).publicKey
        val nonce = ByteArray(16).also(random::nextBytes)
        val fields = profile.wireFields()
        val flags = fields[3] or (if (ultrasonicEnabled) 0x02 else 0)
        val retries = fields[4] or ((maxWorkers - 1) shl 4)
        val header = ByteBuffer.allocate(HELLO_HEADER_SIZE).order(ByteOrder.BIG_ENDIAN)
            .put(HELLO_MAGIC)
            .put(HELLO_VERSION.toByte())
            .put(senderRole.code.toByte())
            .put(ephemeral)
            .put(nonce)
            .put(fields[0].toByte())
            .putShort(fields[1].toShort())
            .putShort(fields[2].toShort())
            .put(flags.toByte())
            .put(retries.toByte())
            .array()
        return header + identity.sign(HELLO_DOMAIN + header)
    }

    private fun verifyHello(packet: ByteArray, expectedRole: Role): PeerHello {
        require(packet.size == HELLO_HEADER_SIZE + SIGNATURE_SIZE) { "Malformed HELLO packet" }
        val header = packet.copyOfRange(0, HELLO_HEADER_SIZE)
        val signature = packet.copyOfRange(HELLO_HEADER_SIZE, packet.size)
        require(NativeIdentity.verify(peerPublic, HELLO_DOMAIN + header, signature)) {
            "HELLO signature verification failed"
        }
        val buffer = ByteBuffer.wrap(header).order(ByteOrder.BIG_ENDIAN)
        val magic = ByteArray(4).also { buffer.get(it) }
        val version = buffer.get().toInt() and 0xff
        val roleCode = buffer.get().toInt() and 0xff
        val ephemeral = ByteArray(32).also { buffer.get(it) }
        buffer.position(buffer.position() + 16) // random nonce; transcript-bound by signature
        val profileId = buffer.get().toInt() and 0xff
        val symbolRate = buffer.short.toInt() and 0xffff
        val paddingBlock = buffer.short.toInt() and 0xffff
        val flags = buffer.get().toInt() and 0xff
        val retries = buffer.get().toInt() and 0xff
        require(magic.contentEquals(HELLO_MAGIC) && version == HELLO_VERSION) {
            "Unsupported HELLO format; update both ECHOCRYPT devices"
        }
        require(roleCode == expectedRole.code) { "Unexpected peer role" }
        require((flags and 0xfc) == 0) { "HELLO contains unsupported capability flags" }
        val workers = ((retries ushr 4) and 0x0f) + 1
        require(workers <= 8) { "HELLO advertises unsupported worker count" }
        val profile = NativeSecurityProfile.fromWire(
            profileId, symbolRate, paddingBlock, (flags and 1), (retries and 0x0f)
        )
        return PeerHello(ephemeral, profile, (flags and 2) != 0, workers)
    }

    private fun negotiate(peer: PeerHello) {
        negotiatedProfile = NativeSecurityProfile.negotiate(securityProfile, peer.profile)
        peerUltrasonicEnabled = peer.ultrasonic
        peerMaxWorkers = peer.workers
        negotiatedUltrasonicEnabled = ultrasonicEnabled && peer.ultrasonic
        negotiatedWorkerCount = minOf(maxWorkers, peer.workers)
    }

    private fun deriveSession() {
        val local = checkNotNull(localEphemeral)
        val remote = checkNotNull(peerEphemeral)
        val initiatorHello = checkNotNull(helloI)
        val responderHello = checkNotNull(helloR)
        val shared = local.agree(remote)
        val transcript = TRANSCRIPT_DOMAIN + initiatorHello + responderHello
        val hash = MessageDigest.getInstance("SHA-256").digest(transcript)
        transcriptHash = hash
        val material = NativeCrypto.hkdfSha256(
            shared, hash, HKDF_INFO, 96
        )
        shared.fill(0)
        val i2r = material.copyOfRange(0, 32)
        val r2i = material.copyOfRange(32, 64)
        val hopping = if (negotiatedProfile.frequencyHopping) material.copyOfRange(64, 96) else null
        material.fill(0)
        val tx = if (role == Role.INITIATOR) i2r else r2i
        val rx = if (role == Role.INITIATOR) r2i else i2r
        session = Session(
            NativeCrypto.SecureChannel(tx, paddingBlock = negotiatedProfile.paddingBlock),
            NativeCrypto.SecureChannel(rx, paddingBlock = negotiatedProfile.paddingBlock),
            peerFingerprint, hopping, negotiatedProfile
        )
        tx.fill(0); rx.fill(0)
    }

    private fun makeFinish(senderRole: Role): ByteArray {
        val payload = FINISH_MAGIC + byteArrayOf(senderRole.code.toByte()) + checkNotNull(transcriptHash)
        return checkNotNull(session).tx.encrypt(payload, CONTROL_AAD)
    }

    private fun verifyFinish(packet: ByteArray, expectedRole: Role) {
        val payload = try {
            checkNotNull(session).rx.decrypt(packet, CONTROL_AAD)
        } catch (error: Exception) {
            throw IllegalArgumentException("Key confirmation packet could not be authenticated", error)
        }
        val expected = FINISH_MAGIC + byteArrayOf(expectedRole.code.toByte()) + checkNotNull(transcriptHash)
        require(payload.contentEquals(expected)) { "Key confirmation transcript mismatch" }
    }

    companion object {
        private const val HELLO_HEADER_SIZE = 61
        private const val SIGNATURE_SIZE = 64
        private const val HELLO_VERSION = 2
        private val HELLO_MAGIC = byteArrayOf(0x45, 0x43, 0x48, 0x31) // ECH1
        private val FINISH_MAGIC = byteArrayOf(0x45, 0x43, 0x48, 0x46) // ECHF
        private val HELLO_DOMAIN = "ECHOCRYPT/HELLO/v2\u0000".toByteArray(Charsets.UTF_8)
        private val TRANSCRIPT_DOMAIN = "ECHOCRYPT/TRANSCRIPT/v2\u0000".toByteArray(Charsets.UTF_8)
        private val HKDF_INFO =
            "ECHOCRYPT/v2 directional traffic, padding and acoustic hopping keys".toByteArray(Charsets.UTF_8)
        private val CONTROL_AAD = "ECHOCRYPT/CONTROL/v2".toByteArray(Charsets.UTF_8)
        private val TEXT_AAD = "ECHOCRYPT/TEXT/v2".toByteArray(Charsets.UTF_8)
        private val DATA_AAD = "ECHOCRYPT/RELIABLE-DATA/v1".toByteArray(Charsets.UTF_8)
    }
}
