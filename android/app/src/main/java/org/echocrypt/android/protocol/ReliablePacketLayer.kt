package org.echocrypt.android.protocol

import java.nio.ByteBuffer
import java.nio.ByteOrder
import java.security.SecureRandom
import java.util.ArrayDeque
import java.util.LinkedHashMap

/**
 * Encrypted stop-and-wait ARQ. One fragment is in flight to bound audio queue use.
 * Complete application messages wait in a bounded FIFO so chat and file traffic
 * can share one reliable channel without rejecting each other.
 */
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
    private val reassemblyTimeoutMillis: Long = DEFAULT_REASSEMBLY_TIMEOUT_MS,
    private val maxInFlightFragments: Int = DEFAULT_WINDOW_SIZE
) {
    private data class Flight(var attempts: Int = 0, var sentAt: Long = 0)
    private data class Outgoing(
        val id: Int,
        val fragments: List<ByteArray>,
        val messageBytes: Int,
        val flights: MutableMap<Int, Flight> = LinkedHashMap(),
        var nextIndex: Int = 0,
        var acknowledged: Int = 0
    )
    private data class Incoming(
        val count: Int,
        val fragments: Array<ByteArray?>,
        val createdAt: Long,
        var received: Int = 0,
        var bytes: Int = 0
    )

    private val random = SecureRandom()
    private var outgoing: Outgoing? = null
    private val outgoingQueue = ArrayDeque<Outgoing>()
    private var pendingOutgoingBytes = 0
    private val incoming = LinkedHashMap<Int, Incoming>()
    private val completed = LinkedHashMap<Int, Long>()
    private var closed = false

    init {
        require(fragmentPayloadBytes in 16..900)
        require(retryTimeoutMillis >= 1_000 && reassemblyTimeoutMillis >= retryTimeoutMillis)
        require(maxAttempts in 1..10)
        require(maxInFlightFragments in 1..MAX_WINDOW_SIZE)
    }

    /**
     * Queues one complete application message and returns its stable message ID.
     * The active message and queue are bounded by both count and byte size.
     */
    @Synchronized
    fun send(message: ByteArray, now: Long): Int {
        check(!closed) { "Reliable transport is closed" }
        require(message.isNotEmpty() && message.size <= MAX_MESSAGE_BYTES) {
            "Message must be 1..$MAX_MESSAGE_BYTES bytes"
        }
        val pendingCount = outgoingQueue.size + if (outgoing != null) 1 else 0
        require(pendingCount < MAX_PENDING_MESSAGES) {
            "Reliable outgoing queue is full ($MAX_PENDING_MESSAGES messages)"
        }
        require(pendingOutgoingBytes + message.size <= MAX_PENDING_BYTES) {
            "Reliable outgoing queue byte limit exceeded"
        }

        val count = (message.size + fragmentPayloadBytes - 1) / fragmentPayloadBytes
        require(count <= MAX_FRAGMENTS)
        var id: Int
        do {
            id = random.nextInt() and 0x7fffffff
        } while (id == 0)

        val parts = (0 until count).map { index ->
            val start = index * fragmentPayloadBytes
            message.copyOfRange(start, minOf(start + fragmentPayloadBytes, message.size))
        }
        outgoingQueue.addLast(Outgoing(id, parts, message.size))
        pendingOutgoingBytes += message.size

        // startNext preserves FIFO when a previous queued item is still waiting.
        if (outgoing == null) startNext(now, propagateFailureForId = id)
        return id
    }

    @Synchronized
    fun receive(encrypted: ByteArray, now: Long) {
        check(!closed) { "Reliable transport is closed" }
        val plain = decrypt(encrypted)
        try {
            require(plain.size >= HEADER_SIZE) { "Truncated reliable packet" }
            val b = ByteBuffer.wrap(plain).order(ByteOrder.BIG_ENDIAN)
            val magic = ByteArray(4).also { b.get(it) }
            require(magic.contentEquals(MAGIC)) { "Invalid reliable packet magic" }
            val type = b.get().toInt() and 0xff
            val id = b.int
            val index = b.short.toInt() and 0xffff
            val count = b.short.toInt() and 0xffff
            require(id > 0 && count in 1..MAX_FRAGMENTS && index < count) {
                "Invalid reliable fragment header"
            }
            when (type) {
                TYPE_ACK -> {
                    require(plain.size == HEADER_SIZE) { "Malformed ACK" }
                    acceptAck(id, index, count, now)
                }
                TYPE_DATA -> {
                    val payload = plain.copyOfRange(HEADER_SIZE, plain.size)
                    try {
                        require(payload.isNotEmpty() && payload.size <= fragmentPayloadBytes) {
                            "Invalid fragment size"
                        }
                        acceptData(id, index, count, payload, now)
                    } finally {
                        payload.fill(0)
                    }
                }
                else -> throw IllegalArgumentException("Unknown reliable packet type")
            }
        } finally {
            plain.fill(0)
        }
    }

    @Synchronized
    fun tick(now: Long) {
        if (closed) return
        val expired = incoming.filterValues {
            now - it.createdAt > reassemblyTimeoutMillis
        }.keys.toList()
        expired.forEach { id ->
            incoming.remove(id)?.fragments?.forEach { it?.fill(0) }
        }
        pruneCompleted(now)

        val active = outgoing
        if (active == null) {
            startNext(now)
            return
        }
        val expiredFlights = active.flights.entries.filter { now - it.value.sentAt >= retryTimeoutMillis }
        for ((index, flight) in expiredFlights) {
            if (flight.attempts >= maxAttempts) {
                failActive(active, "Fragment $index received no ACK after $maxAttempts transmissions", now)
                return
            }
            try { transmitFragment(active, index, flight, now) }
            catch (_: Exception) { /* bounded retry on a later tick */ }
        }
        pumpWindow(active, now)
    }

    @Synchronized
    fun hasPendingMessage(): Boolean = outgoing != null || outgoingQueue.isNotEmpty()

    @Synchronized
    fun pendingMessageCount(): Int = outgoingQueue.size + if (outgoing != null) 1 else 0

    @Synchronized
    fun inFlightFragmentCount(): Int = outgoing?.flights?.size ?: 0

    @Synchronized
    fun close() {
        closed = true
        outgoing?.let(::release)
        outgoing = null
        while (outgoingQueue.isNotEmpty()) release(outgoingQueue.removeFirst())
        pendingOutgoingBytes = 0
        incoming.values.forEach { it.fragments.forEach { part -> part?.fill(0) } }
        incoming.clear()
        completed.clear()
    }

    private fun pumpWindow(active: Outgoing, now: Long) {
        while (outgoing === active && active.flights.size < maxInFlightFragments &&
            active.nextIndex < active.fragments.size) {
            val index = active.nextIndex++
            val flight = Flight()
            active.flights[index] = flight
            try { transmitFragment(active, index, flight, now) }
            catch (error: Exception) {
                if (flight.attempts == 0) {
                    flight.attempts = 1
                    flight.sentAt = now
                }
                if (index == 0 && active.acknowledged == 0) throw error
                return
            }
        }
    }

    private fun transmitFragment(active: Outgoing, index: Int, flight: Flight, now: Long) {
        val clearPacket = packet(TYPE_DATA, active.id, index, active.fragments.size, active.fragments[index])
        val encrypted = try { encrypt(clearPacket) } finally { clearPacket.fill(0) }
        flight.attempts++
        flight.sentAt = now
        transmit(encrypted)
    }

    private fun acceptAck(id: Int, index: Int, count: Int, now: Long) {
        val active = outgoing ?: return
        if (active.id != id || active.fragments.size != count) return
        if (active.flights.remove(index) == null) return
        active.fragments[index].fill(0)
        active.acknowledged++
        if (active.acknowledged == active.fragments.size) {
            outgoing = null
            release(active)
            try { onDelivered(id) } finally { startNext(now) }
        } else {
            pumpWindow(active, now)
        }
    }

    private fun failActive(active: Outgoing, reason: String, now: Long) {
        if (outgoing !== active) return
        outgoing = null
        release(active)
        try { onFailure(active.id, reason) } finally { startNext(now) }
    }

    private fun startNext(now: Long, propagateFailureForId: Int? = null) {
        if (closed || outgoing != null) return
        while (outgoingQueue.isNotEmpty()) {
            val next = outgoingQueue.removeFirst()
            outgoing = next
            try {
                pumpWindow(next, now)
                return
            } catch (error: Exception) {
                outgoing = null
                release(next)
                if (next.id == propagateFailureForId) throw error
                try {
                    onFailure(next.id, error.message ?: "Could not start queued reliable message")
                } catch (_: Exception) {
                    // A callback failure must not strand later queued messages.
                }
            }
        }
    }

    private fun release(message: Outgoing) {
        message.fragments.forEach { it.fill(0) }
        message.flights.clear()
        pendingOutgoingBytes = (pendingOutgoingBytes - message.messageBytes).coerceAtLeast(0)
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
        // The fragment is copied into the bounded reassembly buffer before ACK.
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
            try {
                onMessage(message)
            } finally {
                message.fill(0)
            }
        }
    }

    private fun sendAck(id: Int, index: Int, count: Int) {
        val clearPacket = packet(TYPE_ACK, id, index, count, ByteArray(0))
        val encrypted = try {
            encrypt(clearPacket)
        } finally {
            clearPacket.fill(0)
        }
        transmit(encrypted)
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
        const val MAX_PENDING_MESSAGES = 32
        const val MAX_PENDING_BYTES = 64 * 1024
        const val DEFAULT_FRAGMENT_BYTES = 96
        const val DEFAULT_RETRY_TIMEOUT_MS = 30_000L
        const val DEFAULT_MAX_ATTEMPTS = 5
        const val DEFAULT_REASSEMBLY_TIMEOUT_MS = 120_000L
        const val DEFAULT_WINDOW_SIZE = 2
        const val MAX_WINDOW_SIZE = 4
    }
}
