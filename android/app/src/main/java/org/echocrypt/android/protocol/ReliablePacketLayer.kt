package org.echocrypt.android.protocol

import java.nio.ByteBuffer
import java.nio.ByteOrder
import java.security.SecureRandom
import java.util.LinkedHashMap

/** Encrypted stop-and-wait ARQ. One fragment is in flight to bound audio queue use. */
class ReliablePacketLayer(
    private val encrypt: (ByteArray) -> ByteArray,
    private val decrypt: (ByteArray) -> ByteArray,
    private val transmit: (ByteArray) -> Unit,
    private val onMessage: (ByteArray) -> Unit,
    private val onDelivered: (Int) -> Unit = {},
    private val onFailure: (Int, String) -> Unit = { _, _ -> },
    private val fragmentPayloadBytes: Int = DEFAULT_FRAGMENT_BYTES,
    private val retryTimeoutMillis: Long = DEFAULT_RETRY_TIMEOUT_MS,
    private val maxAttempts: Int = DEFAULT_MAX_ATTEMPTS,
    private val reassemblyTimeoutMillis: Long = DEFAULT_REASSEMBLY_TIMEOUT_MS
) {
    private data class Outgoing(val id: Int, val fragments: List<ByteArray>, var index: Int = 0, var attempts: Int = 0, var sentAt: Long = 0)
    private data class Incoming(val count: Int, val fragments: Array<ByteArray?>, val createdAt: Long, var received: Int = 0, var bytes: Int = 0)

    private val random = SecureRandom()
    private var outgoing: Outgoing? = null
    private val incoming = LinkedHashMap<Int, Incoming>()
    private val completed = LinkedHashMap<Int, Long>()
    private var closed = false

    init {
        require(fragmentPayloadBytes in 16..900)
        require(retryTimeoutMillis >= 1_000 && reassemblyTimeoutMillis >= retryTimeoutMillis)
        require(maxAttempts in 1..10)
    }

    @Synchronized
    fun send(message: ByteArray, now: Long): Int {
        check(!closed) { "Reliable transport is closed" }
        require(message.isNotEmpty() && message.size <= MAX_MESSAGE_BYTES) { "Message must be 1..$MAX_MESSAGE_BYTES bytes" }
        check(outgoing == null) { "Another message is awaiting acknowledgement" }
        val count = (message.size + fragmentPayloadBytes - 1) / fragmentPayloadBytes
        require(count <= MAX_FRAGMENTS)
        var id: Int
        do { id = random.nextInt() and 0x7fffffff } while (id == 0)
        val parts = (0 until count).map { i ->
            val start = i * fragmentPayloadBytes
            message.copyOfRange(start, minOf(start + fragmentPayloadBytes, message.size))
        }
        outgoing = Outgoing(id, parts)
        try { sendCurrent(now) } catch (e: Exception) { outgoing = null; throw e }
        return id
    }

    @Synchronized
    fun receive(encrypted: ByteArray, now: Long) {
        check(!closed) { "Reliable transport is closed" }
        val plain = decrypt(encrypted)
        require(plain.size >= HEADER_SIZE) { "Truncated reliable packet" }
        val b = ByteBuffer.wrap(plain).order(ByteOrder.BIG_ENDIAN)
        val magic = ByteArray(4).also { b.get(it) }
        require(magic.contentEquals(MAGIC)) { "Invalid reliable packet magic" }
        val type = b.get().toInt() and 0xff
        val id = b.int
        val index = b.short.toInt() and 0xffff
        val count = b.short.toInt() and 0xffff
        require(id > 0 && count in 1..MAX_FRAGMENTS && index < count) { "Invalid reliable fragment header" }
        when (type) {
            TYPE_ACK -> {
                require(plain.size == HEADER_SIZE) { "Malformed ACK" }
                acceptAck(id, index, count, now)
            }
            TYPE_DATA -> {
                val payload = plain.copyOfRange(HEADER_SIZE, plain.size)
                require(payload.isNotEmpty() && payload.size <= fragmentPayloadBytes) { "Invalid fragment size" }
                acceptData(id, index, count, payload, now)
            }
            else -> throw IllegalArgumentException("Unknown reliable packet type")
        }
    }

    @Synchronized
    fun tick(now: Long) {
        if (closed) return
        incoming.entries.removeAll { now - it.value.createdAt > reassemblyTimeoutMillis }
        pruneCompleted(now)
        val active = outgoing ?: return
        if (active.attempts == 0 || now - active.sentAt >= retryTimeoutMillis) {
            if (active.attempts >= maxAttempts) {
                outgoing = null
                active.fragments.forEach { it.fill(0) }
                onFailure(active.id, "No ACK received after $maxAttempts transmissions")
            } else {
                try { sendCurrent(now) } catch (e: Exception) {
                    onFailure(active.id, e.message ?: "Retransmission failed")
                }
            }
        }
    }

    @Synchronized fun hasPendingMessage(): Boolean = outgoing != null

    @Synchronized
    fun close() {
        closed = true
        outgoing?.fragments?.forEach { it.fill(0) }
        outgoing = null
        incoming.values.forEach { it.fragments.forEach { part -> part?.fill(0) } }
        incoming.clear()
        completed.clear()
    }

    private fun sendCurrent(now: Long) {
        val active = checkNotNull(outgoing)
        val payload = active.fragments[active.index]
        transmit(encrypt(packet(TYPE_DATA, active.id, active.index, active.fragments.size, payload)))
        active.attempts++
        active.sentAt = now
    }

    private fun acceptAck(id: Int, index: Int, count: Int, now: Long) {
        val active = outgoing ?: return
        if (active.id != id || active.fragments.size != count || active.index != index) return
        active.fragments[index].fill(0)
        active.index++
        active.attempts = 0
        active.sentAt = 0
        if (active.index == active.fragments.size) {
            outgoing = null
            onDelivered(id)
        } else sendCurrent(now)
    }

    private fun acceptData(id: Int, index: Int, count: Int, payload: ByteArray, now: Long) {
        // A completed duplicate is still ACKed because the original ACK may be lost.
        if (completed.containsKey(id)) {
            sendAck(id, index, count)
            return
        }
        var state = incoming[id]
        if (state == null) {
            require(incoming.size < MAX_INCOMING) { "Too many incomplete messages" }
            state = Incoming(count, arrayOfNulls(count), now)
            incoming[id] = state
        }
        require(state.count == count) { "Conflicting fragment count" }
        if (state.fragments[index] != null) {
            sendAck(id, index, count)
            return
        }
        require(state.bytes + payload.size <= MAX_MESSAGE_BYTES) { "Incoming message exceeds limit" }
        state.fragments[index] = payload.copyOf()
        state.bytes += payload.size
        state.received++
        // ACK only after the fragment has been accepted into the reassembly buffer.
        sendAck(id, index, count)
        if (state.received == state.count) {
            val message = ByteArray(state.bytes)
            var offset = 0
            state.fragments.forEach { part ->
                val bytes = checkNotNull(part)
                bytes.copyInto(message, offset)
                offset += bytes.size
                bytes.fill(0)
            }
            incoming.remove(id)
            completed[id] = now
            pruneCompleted(now)
            try { onMessage(message) } finally { message.fill(0) }
        }
    }

    private fun packet(type: Int, id: Int, index: Int, count: Int, payload: ByteArray): ByteArray =
        ByteBuffer.allocate(HEADER_SIZE + payload.size).order(ByteOrder.BIG_ENDIAN)
            .put(MAGIC).put(type.toByte()).putInt(id).putShort(index.toShort())
            .putShort(count.toShort()).put(payload).array()

    private fun pruneCompleted(now: Long) {
        completed.entries.removeAll { now - it.value > COMPLETED_TTL }
        while (completed.size > MAX_COMPLETED) completed.remove(completed.keys.first())
    }

    companion object {
        private const val HEADER_SIZE = 13
        private const val TYPE_DATA = 1
        private const val TYPE_ACK = 2
        private val MAGIC = byteArrayOf(0x45, 0x43, 0x52, 0x31)
        const val MAX_MESSAGE_BYTES = 2_048
        private const val MAX_FRAGMENTS = 64
        private const val MAX_INCOMING = 4
        private const val MAX_COMPLETED = 256
        private const val COMPLETED_TTL = 120_000L
        const val DEFAULT_FRAGMENT_BYTES = 96
        const val DEFAULT_RETRY_TIMEOUT_MS = 30_000L
        const val DEFAULT_MAX_ATTEMPTS = 5
        const val DEFAULT_REASSEMBLY_TIMEOUT_MS = 120_000L
    }
}
