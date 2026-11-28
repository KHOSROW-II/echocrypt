package org.echocrypt.android.protocol

import java.io.File
import java.io.RandomAccessFile
import java.nio.ByteBuffer
import java.nio.ByteOrder
import java.security.MessageDigest
import java.security.SecureRandom

/**
 * Streaming file-transfer protocol carried inside the encrypted reliable packet layer.
 * A single application packet is outstanding at a time; transport ACKs drive the sender.
 */
class NativeFileTransfer(
    private val storageDirectory: File,
    private val sendPacket: (ByteArray) -> Unit,
    private val onProgress: (String) -> Unit = {},
    private val onReceived: (File) -> Unit = {},
    private val onError: (String) -> Unit = {}
) {
    private data class Outgoing(
        val id: ByteArray,
        val source: File,
        val name: String,
        val digest: ByteArray,
        val chunkCount: Int,
        var nextChunk: Int = 0
    )
    private data class Incoming(
        val id: ByteArray,
        val name: String,
        val expectedSize: Long,
        val digest: ByteArray,
        val chunkCount: Int,
        val temp: File,
        val destination: File,
        val file: RandomAccessFile,
        val received: BooleanArray,
        var receivedCount: Int = 0
    )

    private val random = SecureRandom()
    private var outgoing: Outgoing? = null
    private val incoming = LinkedHashMap<String, Incoming>()
    private var awaitingTransportAck = false

    init {
        require(storageDirectory.exists() || storageDirectory.mkdirs()) { "Cannot create file-transfer storage" }
    }

    @Synchronized
    fun start(source: File, requestedName: String = source.name, deleteSourceAfterSend: Boolean = false) {
        check(outgoing == null) { "A file transfer is already in progress" }
        require(source.isFile && source.canRead()) { "Selected file is not readable" }
        require(source.length() in 1..MAX_FILE_BYTES) { "File must be between 1 byte and $MAX_FILE_BYTES bytes" }
        val name = sanitizeName(requestedName)
        val digest = sha256(source)
        val id = ByteArray(TRANSFER_ID_BYTES).also(random::nextBytes)
        val count = ((source.length() + CHUNK_BYTES - 1) / CHUNK_BYTES).toInt()
        outgoing = Outgoing(id, source, name, digest, count, deleteSourceAfterSend)
        onProgress("Preparing $name · ${source.length()} bytes")
        sendManifest(checkNotNull(outgoing))
    }

    /** Called only when the reliable transport ACKs the current file application packet. */
    @Synchronized
    fun onReliablePacketDelivered() {
        if (!awaitingTransportAck) return
        awaitingTransportAck = false
        val active = outgoing ?: return
        if (active.nextChunk >= active.chunkCount) {
            outgoing = null
            if (active.deleteSourceAfterSend) active.source.delete()
            onProgress("File sent · ${active.name}")
            return
        }
        sendNextChunk(active)
    }

    @Synchronized
    fun receive(packet: ByteArray) {
        require(packet.size >= 1) { "Empty file-transfer packet" }
        when (packet[0].toInt() and 0xff) {
            TYPE_MANIFEST -> receiveManifest(packet)
            TYPE_CHUNK -> receiveChunk(packet)
            else -> throw IllegalArgumentException("Unknown file-transfer packet type")
        }
    }

    @Synchronized
    fun close() {
        outgoing?.let { if (it.deleteSourceAfterSend) it.source.delete() }
        outgoing = null
        awaitingTransportAck = false
        incoming.values.forEach { runCatching { it.file.close() }; runCatching { it.temp.delete() } }
        incoming.clear()
    }

    private fun sendManifest(active: Outgoing) {
        val nameBytes = active.name.toByteArray(Charsets.UTF_8)
        val packet = ByteBuffer.allocate(1 + TRANSFER_ID_BYTES + 8 + 4 + 4 + 32 + 2 + nameBytes.size)
            .order(ByteOrder.BIG_ENDIAN)
            .put(TYPE_MANIFEST.toByte()).put(active.id).putLong(active.source.length())
            .putInt(CHUNK_BYTES).putInt(active.chunkCount).put(active.digest)
            .putShort(nameBytes.size.toShort()).put(nameBytes).array()
        transmit(packet)
    }

    private fun sendNextChunk(active: Outgoing) {
        if (outgoing !== active) return
        try {
            val index = active.nextChunk
            val offset = index.toLong() * CHUNK_BYTES
            val length = minOf(CHUNK_BYTES.toLong(), active.source.length() - offset).toInt()
            val bytes = ByteArray(length)
            RandomAccessFile(active.source, "r").use { file ->
                file.seek(offset)
                file.readFully(bytes)
            }
            val packet = ByteBuffer.allocate(1 + TRANSFER_ID_BYTES + 4 + 2 + bytes.size)
                .order(ByteOrder.BIG_ENDIAN)
                .put(TYPE_CHUNK.toByte()).put(active.id).putInt(index).putShort(bytes.size.toShort()).put(bytes).array()
            active.nextChunk++
            onProgress("Sending ${active.name} · ${active.nextChunk}/${active.chunkCount} chunks")
            transmit(packet)
            bytes.fill(0)
        } catch (error: Exception) {
            outgoing = null
            awaitingTransportAck = false
            if (active.deleteSourceAfterSend) active.source.delete()
            onError(error.message ?: "Could not read outgoing file")
        }
    }

    private fun transmit(packet: ByteArray) {
        check(!awaitingTransportAck) { "File transfer is waiting for the previous packet ACK" }
        awaitingTransportAck = true
        try { sendPacket(packet) } catch (error: Exception) {
            awaitingTransportAck = false
            outgoing = null
            throw error
        }
    }

    private fun receiveManifest(packet: ByteArray) {
        require(packet.size >= MANIFEST_FIXED_BYTES + 1) { "Truncated file manifest" }
        val b = ByteBuffer.wrap(packet).order(ByteOrder.BIG_ENDIAN)
        require((b.get().toInt() and 0xff) == TYPE_MANIFEST)
        val id = ByteArray(TRANSFER_ID_BYTES).also(b::get)
        val size = b.long
        val chunkSize = b.int
        val count = b.int
        val digest = ByteArray(32).also(b::get)
        val nameLength = b.short.toInt() and 0xffff
        require(size in 1..MAX_FILE_BYTES && chunkSize == CHUNK_BYTES) { "Unsupported file size or chunk size" }
        require(count == ((size + CHUNK_BYTES - 1) / CHUNK_BYTES).toInt()) { "Invalid file chunk count" }
        require(nameLength in 1..MAX_NAME_BYTES && b.remaining() == nameLength) { "Invalid file name length" }
        val rawName = ByteArray(nameLength).also(b::get).toString(Charsets.UTF_8)
        val name = sanitizeName(rawName)
        val key = id.toHex()
        if (incoming.containsKey(key)) return
        require(incoming.size < MAX_INCOMING_TRANSFERS) { "Too many simultaneous incoming files" }
        val temp = File(storageDirectory, ".$key.part")
        val destination = uniqueDestination(name)
        val randomFile = RandomAccessFile(temp, "rw")
        try {
            randomFile.setLength(size)
            incoming[key] = Incoming(id, name, size, digest, count, temp, destination,
                randomFile, BooleanArray(count))
            onProgress("Receiving $name · $size bytes")
        } catch (error: Exception) {
            randomFile.close()
            temp.delete()
            throw error
        }
    }

    private fun receiveChunk(packet: ByteArray) {
        require(packet.size >= CHUNK_FIXED_BYTES + 1) { "Truncated file chunk" }
        val b = ByteBuffer.wrap(packet).order(ByteOrder.BIG_ENDIAN)
        require((b.get().toInt() and 0xff) == TYPE_CHUNK)
        val id = ByteArray(TRANSFER_ID_BYTES).also(b::get)
        val index = b.int
        val length = b.short.toInt() and 0xffff
        require(length in 1..CHUNK_BYTES && b.remaining() == length) { "Invalid file chunk length" }
        val state = incoming[id.toHex()] ?: throw IllegalArgumentException("File manifest not received")
        require(index in 0 until state.chunkCount) { "File chunk index out of range" }
        val expected = minOf(CHUNK_BYTES.toLong(), state.expectedSize - index.toLong() * CHUNK_BYTES).toInt()
        require(length == expected) { "Unexpected file chunk size" }
        if (state.received[index]) return
        val bytes = ByteArray(length).also(b::get)
        state.file.seek(index.toLong() * CHUNK_BYTES)
        state.file.write(bytes)
        bytes.fill(0)
        state.received[index] = true
        state.receivedCount++
        onProgress("Receiving ${state.name} · ${state.receivedCount}/${state.chunkCount} chunks")
        if (state.receivedCount == state.chunkCount) finalizeIncoming(state)
    }

    private fun finalizeIncoming(state: Incoming) {
        state.file.fd.sync()
        state.file.close()
        val actual = sha256(state.temp)
        incoming.remove(state.id.toHex())
        if (!MessageDigest.isEqual(actual, state.digest)) {
            state.temp.delete()
            onError("SHA-256 mismatch; rejected ${state.name}")
            return
        }
        val finalDestination = uniqueDestination(state.name)
        if (!state.temp.renameTo(finalDestination)) {
            state.temp.delete()
            onError("Could not finalize received file ${state.name}")
            return
        }
        onProgress("Received and verified · ${finalDestination.name}")
        onReceived(finalDestination)
    }

    private fun uniqueDestination(name: String): File {
        var candidate = File(storageDirectory, name)
        if (!candidate.exists()) return candidate
        val dot = name.lastIndexOf('.')
        val stem = if (dot > 0) name.substring(0, dot) else name
        val ext = if (dot > 0) name.substring(dot) else ""
        var suffix = 1
        while (candidate.exists()) {
            candidate = File(storageDirectory, "$stem ($suffix)$ext")
            suffix++
        }
        return candidate
    }

    companion object {
        const val MAX_FILE_BYTES = 10L * 1024L * 1024L
        const val CHUNK_BYTES = 1_400
        const val MAX_NAME_BYTES = 180
        const val MAX_INCOMING_TRANSFERS = 2
        private const val TRANSFER_ID_BYTES = 16
        private const val TYPE_MANIFEST = 1
        private const val TYPE_CHUNK = 2
        private const val MANIFEST_FIXED_BYTES = 1 + TRANSFER_ID_BYTES + 8 + 4 + 4 + 32 + 2
        private const val CHUNK_FIXED_BYTES = 1 + TRANSFER_ID_BYTES + 4 + 2

        fun isTransferPacket(packet: ByteArray): Boolean =
            packet.isNotEmpty() && (packet[0].toInt() and 0xff) in TYPE_MANIFEST..TYPE_CHUNK

        fun sanitizeName(input: String): String {
            require('/' !in input && '\\' !in input) { "Path separators are not allowed in file names" }
            val cleaned = input.trim().filter { it >= ' ' && it != '\u007f' }.trim('.', ' ')
            require(cleaned.isNotBlank() && cleaned != "." && cleaned != "..") { "Invalid file name" }
            require(cleaned.toByteArray(Charsets.UTF_8).size <= MAX_NAME_BYTES) { "File name is too long" }
            return cleaned
        }

        private fun sha256(file: File): ByteArray {
            val digest = MessageDigest.getInstance("SHA-256")
            file.inputStream().buffered().use { input ->
                val buffer = ByteArray(32 * 1024)
                while (true) {
                    val read = input.read(buffer)
                    if (read < 0) break
                    digest.update(buffer, 0, read)
                }
                buffer.fill(0)
            }
            return digest.digest()
        }

        private fun ByteArray.toHex(): String = joinToString("") { "%02x".format(it) }
    }
}
