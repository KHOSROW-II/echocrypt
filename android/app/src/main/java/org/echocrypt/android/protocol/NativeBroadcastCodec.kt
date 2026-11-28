package org.echocrypt.android.protocol

import org.bouncycastle.crypto.digests.Blake2sDigest
import java.io.ByteArrayOutputStream
import java.nio.ByteBuffer
import java.nio.ByteOrder
import java.security.MessageDigest
import java.security.SecureRandom
import java.util.LinkedHashMap
import java.util.zip.CRC32

/**
 * Android port of core/broadcast.py ECBR v1 TEXT frames.
 * The compatibility key is public: Broadcast is open, not confidential, and not sender-authenticated.
 */
internal data class NativeBroadcastFrame(
    val senderId: ByteArray, val messageId: ByteArray,
    val index: Int, val count: Int, val payload: ByteArray
)

internal class NativeBroadcastCodec(senderId: ByteArray? = null) {
    companion object {
        const val MAX_PACKET_BYTES = 1024
        const val HEADER_BYTES = 32
        const val TAG_BYTES = 16
        const val CRC_BYTES = 4
        const val MAX_FRAGMENT_PAYLOAD = MAX_PACKET_BYTES - HEADER_BYTES - TAG_BYTES - CRC_BYTES
        const val MAX_FRAGMENTS = 128
        private val MAGIC = byteArrayOf(0x45, 0x43, 0x42, 0x52)
        private const val KEY_DOMAIN = "ECHO-CRYPT/BROADCAST/PUBLIC-COMPATIBILITY-TRANSFORM/v1"
        private const val NONCE_DOMAIN = "ECHO-CRYPT/BROADCAST/NONCE/v1"
        private val PUBLIC_KEY = MessageDigest.getInstance("SHA-256").digest(KEY_DOMAIN.toByteArray(Charsets.UTF_8))
        private val RANDOM = SecureRandom()
    }

    val senderId: ByteArray = (senderId ?: ByteArray(8).also(RANDOM::nextBytes)).copyOf()

    init { require(this.senderId.size == 8) { "Broadcast sender ID must be exactly 8 bytes" } }

    fun encodeText(text: String, messageId: ByteArray? = null): List<ByteArray> {
        require(text.isNotEmpty()) { "Broadcast text must not be empty" }
        val id = (messageId ?: ByteArray(8).also(RANDOM::nextBytes)).copyOf()
        require(id.size == 8) { "Broadcast message ID must be exactly 8 bytes" }
        val fragments = splitUtf8(text, MAX_FRAGMENT_PAYLOAD)
        require(fragments.size <= MAX_FRAGMENTS) { "Broadcast text exceeds the Android fragment limit" }
        return fragments.mapIndexed { index, fragment -> encodeFrame(id, index, fragments.size, fragment) }
    }

    fun decode(packet: ByteArray): NativeBroadcastFrame {
        require(packet.size in (HEADER_BYTES + TAG_BYTES + CRC_BYTES)..MAX_PACKET_BYTES) {
            "Broadcast packet length is invalid"
        }
        val header = packet.copyOfRange(0, HEADER_BYTES)
        val buffer = ByteBuffer.wrap(header).order(ByteOrder.BIG_ENDIAN)
        val magic = ByteArray(4).also { buffer.get(it) }
        val version = buffer.get().toInt() and 0xff
        val kind = buffer.get().toInt() and 0xff
        val flags = buffer.get().toInt() and 0xff
        val reserved = buffer.get().toInt() and 0xff
        val sender = ByteArray(8).also { buffer.get(it) }
        val messageId = ByteArray(8).also { buffer.get(it) }
        val index = buffer.short.toInt() and 0xffff
        val count = buffer.short.toInt() and 0xffff
        val encryptedLength = buffer.int
        require(magic.contentEquals(MAGIC) && version == 1 && kind == 1) {
            "Unsupported Broadcast packet type or version"
        }
        require(flags == 0 && reserved == 0) { "Unsupported Broadcast header flags" }
        require(count in 1..MAX_FRAGMENTS && index < count) { "Invalid Broadcast fragment coordinates" }
        require(encryptedLength == packet.size - HEADER_BYTES - CRC_BYTES && encryptedLength >= TAG_BYTES) {
            "Broadcast packet size does not match its header"
        }
        val crc = CRC32().apply { update(packet, 0, packet.size - CRC_BYTES) }.value
        val receivedCrc = ByteBuffer.wrap(packet, packet.size - CRC_BYTES, CRC_BYTES)
            .order(ByteOrder.BIG_ENDIAN).int.toLong() and 0xffffffffL
        require(crc == receivedCrc) { "Broadcast transport CRC mismatch" }
        val encrypted = packet.copyOfRange(HEADER_BYTES, packet.size - CRC_BYTES)
        val plaintext = NativeCrypto.chacha20Poly1305Decrypt(
            PUBLIC_KEY, nonce(sender, messageId, kind, index), encrypted, header
        )
        return NativeBroadcastFrame(sender, messageId, index, count, plaintext)
    }

    private fun encodeFrame(messageId: ByteArray, index: Int, count: Int, payload: ByteArray): ByteArray {
        val encryptedLength = payload.size + TAG_BYTES
        val header = ByteBuffer.allocate(HEADER_BYTES).order(ByteOrder.BIG_ENDIAN)
            .put(MAGIC).put(1.toByte()).put(1.toByte()).put(0.toByte()).put(0.toByte())
            .put(senderId).put(messageId).putShort(index.toShort()).putShort(count.toShort())
            .putInt(encryptedLength).array()
        val encrypted = NativeCrypto.chacha20Poly1305Encrypt(
            PUBLIC_KEY, nonce(senderId, messageId, 1, index), payload, header
        )
        val body = header + encrypted
        val checksum = CRC32().apply { update(body) }.value
        return body + ByteBuffer.allocate(CRC_BYTES).order(ByteOrder.BIG_ENDIAN).putInt(checksum.toInt()).array()
    }

    private fun nonce(sender: ByteArray, messageId: ByteArray, kind: Int, index: Int): ByteArray {
        val material = NONCE_DOMAIN.toByteArray(Charsets.UTF_8) + sender + messageId +
            byteArrayOf(kind.toByte()) +
            ByteBuffer.allocate(2).order(ByteOrder.BIG_ENDIAN).putShort(index.toShort()).array()
        val digest = Blake2sDigest(96)
        digest.update(material, 0, material.size)
        return ByteArray(12).also { digest.doFinal(it, 0) }
    }

    private fun splitUtf8(text: String, maximumBytes: Int): List<ByteArray> {
        val chunks = mutableListOf<ByteArray>()
        var current = ByteArrayOutputStream()
        var offset = 0
        while (offset < text.length) {
            val codePoint = text.codePointAt(offset)
            val encoded = String(Character.toChars(codePoint)).toByteArray(Charsets.UTF_8)
            if (current.size() + encoded.size > maximumBytes && current.size() > 0) {
                chunks += current.toByteArray()
                current = ByteArrayOutputStream()
            }
            require(encoded.size <= maximumBytes) { "UTF-8 code point exceeds Broadcast fragment capacity" }
            current.write(encoded)
            offset += Character.charCount(codePoint)
        }
        if (current.size() > 0 || chunks.isEmpty()) chunks += current.toByteArray()
        return chunks
    }
}

/** Bounded, duplicate-safe, out-of-order text reassembly. */
internal class NativeBroadcastAssembler {
    private data class Partial(val count: Int, val fragments: MutableMap<Int, ByteArray> = mutableMapOf(),
                               var updatedAt: Long)
    private val partials = LinkedHashMap<String, Partial>()
    private val completed = LinkedHashMap<String, Long>()
    private val maximumPendingMessages = 64
    private val maximumTotalBytes = 256 * 1024
    private val expiryMillis = 90_000L

    @Synchronized
    fun accept(frame: NativeBroadcastFrame, nowMillis: Long = System.currentTimeMillis()): String? {
        expire(nowMillis)
        require(frame.count in 1..NativeBroadcastCodec.MAX_FRAGMENTS && frame.index in 0 until frame.count)
        val key = hex(frame.senderId) + ":" + hex(frame.messageId)
        if (completed.containsKey(key)) return null
        var partial = partials[key]
        if (partial == null) {
            if (partials.size >= maximumPendingMessages) partials.remove(partials.keys.first())
            partial = Partial(frame.count, updatedAt = nowMillis)
            partials[key] = partial
        }
        require(partial.count == frame.count) { "Broadcast fragment count changed within a message" }
        val previous = partial.fragments[frame.index]
        require(previous == null || previous.contentEquals(frame.payload)) {
            "Broadcast fragment index was reused with different data"
        }
        if (previous == null) partial.fragments[frame.index] = frame.payload.copyOf()
        partial.updatedAt = nowMillis
        val used = partials.values.sumOf { item -> item.fragments.values.sumOf { it.size } }
        if (used > maximumTotalBytes) {
            partials.remove(key)
            throw IllegalArgumentException("Broadcast reassembly memory limit exceeded")
        }
        if (partial.fragments.size != partial.count) return null
        val output = ByteArrayOutputStream()
        for (index in 0 until partial.count) output.write(checkNotNull(partial.fragments[index]))
        val bytes = output.toByteArray()
        require(bytes.size <= 16 * 1024) { "Broadcast text exceeds the message size limit" }
        val text = Charsets.UTF_8.newDecoder()
            .onMalformedInput(java.nio.charset.CodingErrorAction.REPORT)
            .onUnmappableCharacter(java.nio.charset.CodingErrorAction.REPORT)
            .decode(java.nio.ByteBuffer.wrap(bytes)).toString()
        partials.remove(key)
        completed[key] = nowMillis
        while (completed.size > 256) completed.remove(completed.keys.first())
        return text
    }

    @Synchronized
    fun expire(nowMillis: Long = System.currentTimeMillis()) {
        partials.entries.removeAll { nowMillis - it.value.updatedAt > expiryMillis }
        completed.entries.removeAll { nowMillis - it.value > expiryMillis * 2 }
    }

    private fun hex(bytes: ByteArray): String = bytes.joinToString("") { "%02x".format(it.toInt() and 0xff) }
}
