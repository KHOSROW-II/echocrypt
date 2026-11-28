package org.echocrypt.android.protocol

import java.io.ByteArrayInputStream
import java.io.ByteArrayOutputStream
import java.io.DataInputStream
import java.io.DataOutputStream
import java.io.File
import java.io.FileInputStream
import java.io.FileOutputStream
import java.io.RandomAccessFile
import java.nio.ByteBuffer
import java.nio.ByteOrder
import java.nio.charset.CodingErrorAction
import java.nio.file.Files
import java.nio.file.StandardCopyOption
import java.security.MessageDigest
import java.security.SecureRandom
import java.util.ArrayDeque
import java.util.BitSet

/**
 * Resumable file transfer carried inside the encrypted reliable-packet channel.
 *
 * Transfer metadata and staged outgoing data are kept in app-private storage.
 * A transfer is considered complete only after the receiver has persisted and
 * hashed the assembled file, and the sender has received the application-level
 * COMPLETE response. Reliable-packet ACKs alone never mean "file delivered".
 */
class NativeFileTransfer(
    private val storageDirectory: File,
    private val sendPacket: (ByteArray) -> Int,
    private val onProgress: (String) -> Unit = {},
    private val onReceived: (File) -> Unit = {},
    private val onError: (String) -> Unit = {},
    private val onSent: (String) -> Unit = {},
    private val receivedDirectory: File = File(storageDirectory, "received")
) {
    private data class Record(
        val id: ByteArray,
        val name: String,
        val size: Long,
        val chunkCount: Int,
        val digest: ByteArray,
        val bitmap: BitSet
    )

    private data class Outgoing(
        val record: Record,
        val source: File,
        val deleteOriginalAfterSend: Boolean,
        val originalSource: File?,
        var waitingMessageId: Int? = null,
        var manifestNegotiated: Boolean = false,
        var blockedByReject: Boolean = false,
        var awaitingResume: Boolean = false,
        var resumeRequestedAt: Long = 0L,
        var awaitingComplete: Boolean = false,
        var finishSentAt: Long = 0L,
        var retryAt: Long = 0L
    )

    private data class Incoming(
        val record: Record,
        val temp: File,
        val file: RandomAccessFile
    )

    private data class Completed(
        val digest: ByteArray,
        val fileName: String,
        val size: Long
    )

    private enum class ActionKind { MANIFEST, CHUNK, FINISH, CONTROL }

    private data class PendingAction(
        val kind: ActionKind,
        val transferId: String,
        val chunkIndex: Int = -1,
        val packet: ByteArray? = null
    )

    private val random = SecureRandom()
    private val incomingDirectory = File(storageDirectory, "incoming")
    private val outgoingDirectory = File(storageDirectory, "outgoing")
    private val completedDirectory = File(storageDirectory, "completed")
    private var outgoing: Outgoing? = null
    private val incoming = LinkedHashMap<String, Incoming>()
    private val pendingActions = HashMap<Int, PendingAction>()
    private val controlQueue = ArrayDeque<ByteArray>()
    private var suspended = false
    private var linkPaused = false

    init {
        listOf(storageDirectory, incomingDirectory, outgoingDirectory, completedDirectory, receivedDirectory)
            .forEach { require(it.exists() || it.mkdirs()) { "Cannot create file-transfer storage" } }
        listOf(incomingDirectory, outgoingDirectory, completedDirectory).forEach { directory ->
            directory.listFiles { file -> file.isFile && file.name.endsWith(".tmp") }?.forEach { it.delete() }
        }
        cleanupCommittedIncomingRecords()
        recoverIncoming()
        incomingDirectory.listFiles { file -> file.isFile && file.name.endsWith(".part") }
            ?.forEach { part ->
                val meta = File(incomingDirectory, part.name.removeSuffix(".part") + ".meta")
                if (!meta.exists()) part.delete()
            }
        recoverOutgoing()
        outgoingDirectory.listFiles { file -> file.isFile && file.name.endsWith(".src") }
            ?.forEach { source ->
                val meta = File(outgoingDirectory, source.name.removeSuffix(".src") + ".meta")
                if (!meta.exists()) source.delete()
            }
        pruneCompletedRecords()
        if (incoming.isNotEmpty()) onProgress("Restored ${incoming.size} partial incoming transfer(s)")
    }

    @Synchronized
    fun start(source: File, requestedName: String = source.name, deleteSourceAfterSend: Boolean = false) {
        check(outgoing == null) { "A pending outgoing file transfer already exists; reconnect to resume it first" }
        require(source.isFile && source.canRead()) { "Selected file is not readable" }
        val name = sanitizeName(requestedName)
        val id = ByteArray(TRANSFER_ID_BYTES).also(random::nextBytes)
        val key = id.toHex()
        val staged = File(outgoingDirectory, "$key.src")
        try {
            copyBounded(source, staged)
            val size = staged.length()
            require(size in 1..MAX_FILE_BYTES) {
                "File must be between 1 byte and $MAX_FILE_BYTES bytes"
            }
            val digest = sha256(staged)
            val count = ((size + CHUNK_BYTES - 1) / CHUNK_BYTES).toInt()
            val record = Record(id, name, size, count, digest, BitSet(count))
            val active = Outgoing(record, staged, deleteSourceAfterSend, source)
            persistRecord(outgoingDirectory, record, META_OUTGOING)
            outgoing = active
            suspended = false
            onProgress("Prepared $name · $size bytes · persistent resume enabled")
            pump(System.currentTimeMillis())
        } catch (error: Exception) {
            staged.delete()
            File(outgoingDirectory, "$key.meta").delete()
            throw error
        }
    }

    /** Re-sends the manifest on a fresh authenticated session and negotiates receiver state. */
    @Synchronized
    fun resumePending() {
        suspended = false
        linkPaused = false
        val active = outgoing
        if (active == null) {
            onProgress("File-transfer channel resumed; waiting for peer manifest or control retry")
            pump(System.currentTimeMillis())
            return
        }
        active.waitingMessageId = null
        active.manifestNegotiated = false
        active.blockedByReject = false
        active.awaitingResume = false
        active.awaitingComplete = false
        active.resumeRequestedAt = 0L
        active.finishSentAt = 0L
        active.retryAt = 0L
        pendingActions.values.forEach { it.packet?.fill(0) }
        pendingActions.clear()
        controlQueue.forEach { it.fill(0) }
        controlQueue.clear()
        onProgress("Reconnecting transfer · ${active.record.name}")
        pump(System.currentTimeMillis(), forceManifest = true)
    }

    /** Called only after ReliablePacketLayer reports delivery of the matching message ID. */
    @Synchronized
    fun onReliablePacketDelivered(messageId: Int) {
        val action = pendingActions.remove(messageId)
        if (action == null) {
            pump(System.currentTimeMillis())
            return
        }
        if (action.kind == ActionKind.CONTROL) {
            action.packet?.fill(0)
            pump(System.currentTimeMillis())
            return
        }
        val active = outgoing ?: return
        if (action.transferId != active.record.id.toHex() || active.waitingMessageId != messageId) return
        active.waitingMessageId = null
        val now = System.currentTimeMillis()
        when (action.kind) {
            ActionKind.MANIFEST -> {
                active.awaitingResume = true
                active.resumeRequestedAt = now
                onProgress("Manifest delivered · waiting for receiver resume map")
            }
            ActionKind.CHUNK -> {
                active.record.bitmap.set(action.chunkIndex)
                persistRecord(outgoingDirectory, active.record, META_OUTGOING)
                active.retryAt = 0L
                onProgress("Sending ${active.record.name} · ${active.record.bitmap.cardinality()}/${active.record.chunkCount} chunks acknowledged")
                pump(now)
            }
            ActionKind.FINISH -> {
                active.awaitingComplete = true
                active.finishSentAt = now
                onProgress("All chunks acknowledged · waiting for final file verification")
            }
            ActionKind.CONTROL -> Unit
        }
        pump(now)
    }

    @Synchronized
    fun onReliablePacketFailed(messageId: Int, reason: String) {
        val action = pendingActions.remove(messageId) ?: return
        linkPaused = true
        if (action.kind == ActionKind.CONTROL) {
            action.packet?.let {
                if (controlQueue.none { queued -> queued.contentEquals(it) } &&
                    controlQueue.size < MAX_CONTROL_QUEUE) controlQueue.addFirst(it)
            }
            onError("File-transfer control packet was not acknowledged: $reason. Transfer state is retained; reconnect or resume.")
            return
        }
        val active = outgoing
        if (active != null && action.transferId == active.record.id.toHex() &&
            active.waitingMessageId == messageId) {
            active.waitingMessageId = null
            when (action.kind) {
                ActionKind.MANIFEST -> { active.awaitingResume = false; active.manifestNegotiated = false }
                ActionKind.FINISH -> active.awaitingComplete = false
                ActionKind.CHUNK -> Unit // receiver bitmap decides whether this chunk needs retransmission.
                ActionKind.CONTROL -> Unit
            }
            active.retryAt = 0L
            onError("Transfer packet was not acknowledged: $reason. Transmission paused; reconnect or resume to continue.")
        }
    }

    @Synchronized
    fun receive(packet: ByteArray) {
        check(!suspended) { "File-transfer session is suspended" }
        require(isTransferPacket(packet)) { "Invalid file-transfer packet magic" }
        val buffer = ByteBuffer.wrap(packet).order(ByteOrder.BIG_ENDIAN)
        buffer.position(MAGIC.size)
        val type = buffer.get().toInt() and 0xff
        when (type) {
            TYPE_MANIFEST -> receiveManifest(buffer)
            TYPE_CHUNK -> receiveChunk(buffer)
            TYPE_RESUME -> receiveResume(buffer)
            TYPE_FINISH -> receiveFinish(buffer)
            TYPE_COMPLETE -> receiveComplete(buffer)
            TYPE_REJECT -> receiveReject(buffer)
            else -> throw IllegalArgumentException("Unknown file-transfer packet type")
        }
    }

    @Synchronized
    fun tick(nowMillis: Long = System.currentTimeMillis()) {
        if (suspended || linkPaused) return
        pump(nowMillis)
        val active = outgoing ?: return
        if (active.blockedByReject) return
        if (active.waitingMessageId != null) return
        if (active.awaitingResume && nowMillis - active.resumeRequestedAt >= RESUME_RETRY_MS) {
            active.awaitingResume = false
            active.manifestNegotiated = false
            onProgress("Receiver did not answer resume request · retrying manifest")
            pump(nowMillis, forceManifest = true)
            return
        }
        if (active.awaitingComplete && nowMillis - active.finishSentAt >= FINISH_RETRY_MS) {
            active.awaitingComplete = false
            pump(nowMillis)
        }
    }

    /** Closes file descriptors but deliberately retains every durable transfer record. */
    @Synchronized
    fun close() {
        suspended = true
        pendingActions.values.forEach { it.packet?.fill(0) }
        pendingActions.clear()
        controlQueue.clear()
        incoming.values.forEach { runCatching { it.file.fd.sync() }; runCatching { it.file.close() } }
        incoming.clear()
        outgoing?.let {
            it.waitingMessageId = null
            it.awaitingResume = false
            it.awaitingComplete = false
        }
    }


    @Synchronized
    fun pendingOutgoingName(): String? = outgoing?.record?.name

    @Synchronized
    fun pendingIncomingCount(): Int = incoming.size

    private fun pump(now: Long, forceManifest: Boolean = false) {
        if (suspended || linkPaused) return
        while (controlQueue.isNotEmpty()) {
            val packet = controlQueue.peekFirst()
            val id = try {
                sendPacket(packet)
            } catch (_: IllegalStateException) {
                return // ReliablePacketLayer is stop-and-wait; retry after its next ACK.
            } catch (error: Exception) {
                onError(error.message ?: "Could not queue file-transfer control packet")
                return
            }
            controlQueue.removeFirst()
            pendingActions[id] = PendingAction(ActionKind.CONTROL, "", packet = packet)
            return
        }

        val active = outgoing ?: return
        if (active.blockedByReject) return
        if (active.waitingMessageId != null) return
        if (active.retryAt > now) return
        if (forceManifest) {
            active.awaitingResume = false
            active.manifestNegotiated = false
        }

        if (active.awaitingResume) {
            if (now - active.resumeRequestedAt < RESUME_RETRY_MS) return
            active.awaitingResume = false
            active.manifestNegotiated = false
        }
        if (active.awaitingComplete) return

        if (!active.manifestNegotiated) {
            val packet = manifestPacket(active.record)
            sendTracked(packet, PendingAction(ActionKind.MANIFEST, active.record.id.toHex()))
            return
        }

        if (active.record.bitmap.cardinality() == active.record.chunkCount) {
            val packet = finishPacket(active.record)
            sendTracked(packet, PendingAction(ActionKind.FINISH, active.record.id.toHex()))
            return
        }

        val index = active.record.bitmap.nextClearBit(0)
        if (index >= active.record.chunkCount) {
            val packet = finishPacket(active.record)
            sendTracked(packet, PendingAction(ActionKind.FINISH, active.record.id.toHex()))
        } else {
            val packet = chunkPacket(active, index)
            sendTracked(packet, PendingAction(ActionKind.CHUNK, active.record.id.toHex(), index))
        }
    }

    private fun sendTracked(packet: ByteArray, action: PendingAction) {
        val id = try {
            sendPacket(packet)
        } catch (_: IllegalStateException) {
            return // Another reliable message (for example chat) is currently in flight.
        } catch (error: Exception) {
            onError(error.message ?: "Could not queue file-transfer packet; transfer retained")
            outgoing?.retryAt = System.currentTimeMillis() + SEND_RETRY_DELAY_MS
            return
        }
        pendingActions[id] = action
        if (action.kind != ActionKind.CONTROL) {
            outgoing?.takeIf { it.record.id.toHex() == action.transferId }?.waitingMessageId = id
        }
    }

    private fun queueControl(packet: ByteArray) {
        if (controlQueue.none { it.contentEquals(packet) }) {
            if (controlQueue.size >= MAX_CONTROL_QUEUE) {
                onError("File-transfer control queue is full; peer retry will recover the operation.")
                return
            }
            controlQueue.addLast(packet)
        }
        pump(System.currentTimeMillis())
    }

    private fun receiveManifest(buffer: ByteBuffer) {
        require(buffer.remaining() >= MANIFEST_FIXED_BYTES - MAGIC.size - 1 + 1) { "Truncated file manifest" }
        val id = ByteArray(TRANSFER_ID_BYTES).also(buffer::get)
        val size = buffer.long
        val chunkSize = buffer.int
        val count = buffer.int
        val digest = ByteArray(32).also(buffer::get)
        val nameLength = buffer.short.toInt() and 0xffff
        require(size in 1..MAX_FILE_BYTES && chunkSize == CHUNK_BYTES) { "Unsupported file size or chunk size" }
        require(count == ((size + CHUNK_BYTES - 1) / CHUNK_BYTES).toInt()) { "Invalid file chunk count" }
        require(nameLength in 1..MAX_NAME_BYTES && buffer.remaining() == nameLength) { "Invalid file name length" }
        val nameBytes = ByteArray(nameLength).also(buffer::get)
        val rawName = decodeUtf8(nameBytes)
        val name = sanitizeName(rawName)
        val record = Record(id, name, size, count, digest, BitSet(count))
        val key = id.toHex()

        val completed = readCompleted(id)
        if (completed != null && isCompletedFileValid(completed, digest, size)) {
            incoming.remove(key)?.let { runCatching { it.file.close() } }
            File(incomingDirectory, "$key.meta").delete()
            File(incomingDirectory, "$key.part").delete()
            queueControl(completePacket(id, digest))
            return
        }
        if (completed != null) File(completedDirectory, "$key.done").delete()

        val existing = incoming[key]
        if (existing != null) {
            require(sameManifest(existing.record, record)) { "Conflicting manifest for existing transfer ID" }
            queueControl(resumePacket(existing.record))
            return
        }

        val metaFile = File(incomingDirectory, "$key.meta")
        if (metaFile.exists()) {
            val restored = readRecord(metaFile, META_INCOMING)
            require(sameManifest(restored, record)) { "Conflicting persisted manifest for transfer ID" }
            openIncoming(restored)
            queueControl(resumePacket(checkNotNull(incoming[key]).record))
            return
        }

        require(incoming.size < MAX_INCOMING_TRANSFERS) { "Too many simultaneous incoming files" }
        val temp = File(incomingDirectory, "$key.part")
        val randomFile = RandomAccessFile(temp, "rw")
        try {
            randomFile.setLength(size)
            val state = Incoming(record, temp, randomFile)
            persistRecord(incomingDirectory, record, META_INCOMING)
            incoming[key] = state
            onProgress("Receiving $name · $size bytes · resumable")
            queueControl(resumePacket(record))
        } catch (error: Exception) {
            runCatching { randomFile.close() }
            temp.delete()
            metaFile.delete()
            throw error
        }
    }

    private fun receiveChunk(buffer: ByteBuffer) {
        require(buffer.remaining() >= CHUNK_FIXED_BYTES - MAGIC.size - 1 + 1) { "Truncated file chunk" }
        val id = ByteArray(TRANSFER_ID_BYTES).also(buffer::get)
        val index = buffer.int
        val length = buffer.short.toInt() and 0xffff
        require(length in 1..CHUNK_BYTES && buffer.remaining() == length) { "Invalid file chunk length" }
        val key = id.toHex()
        val state = incoming[key] ?: throw IllegalArgumentException("File manifest not received")
        require(index in 0 until state.record.chunkCount) { "File chunk index out of range" }
        val expected = minOf(CHUNK_BYTES.toLong(), state.record.size - index.toLong() * CHUNK_BYTES).toInt()
        require(length == expected) { "Unexpected file chunk size" }
        if (state.record.bitmap[index]) return

        val bytes = ByteArray(length).also(buffer::get)
        try {
            state.file.seek(index.toLong() * CHUNK_BYTES)
            state.file.write(bytes)
            state.file.fd.sync()
            state.record.bitmap.set(index)
            persistRecord(incomingDirectory, state.record, META_INCOMING)
        } finally {
            bytes.fill(0)
        }
        onProgress("Receiving ${state.record.name} · ${state.record.bitmap.cardinality()}/${state.record.chunkCount} chunks")
    }

    private fun receiveResume(buffer: ByteBuffer) {
        require(buffer.remaining() >= TRANSFER_ID_BYTES + 32 + 4 + 2) { "Truncated resume state" }
        val id = ByteArray(TRANSFER_ID_BYTES).also(buffer::get)
        val digest = ByteArray(32).also(buffer::get)
        val count = buffer.int
        val bitmapLength = buffer.short.toInt() and 0xffff
        require(count in 1..MAX_CHUNKS && bitmapLength <= (count + 7) / 8 && buffer.remaining() == bitmapLength) {
            "Invalid resume bitmap"
        }
        val bitmapBytes = ByteArray(bitmapLength).also(buffer::get)
        val active = outgoing ?: return
        require(active.record.id.contentEquals(id) && active.record.chunkCount == count &&
            MessageDigest.isEqual(active.record.digest, digest)) { "Resume state does not match pending transfer" }
        val remote = BitSet.valueOf(bitmapBytes)
        require(remote.length() <= count) { "Resume bitmap has bits outside the transfer" }
        active.record.bitmap.clear()
        active.record.bitmap.or(remote)
        active.manifestNegotiated = true
        active.awaitingResume = false
        active.awaitingComplete = false
        active.retryAt = 0L
        persistRecord(outgoingDirectory, active.record, META_OUTGOING)
        onProgress("Resume state received · ${remote.cardinality()}/$count chunks already stored by receiver")
        pump(System.currentTimeMillis())
    }

    private fun receiveFinish(buffer: ByteBuffer) {
        require(buffer.remaining() == TRANSFER_ID_BYTES + 32) { "Malformed file-transfer FINISH" }
        val id = ByteArray(TRANSFER_ID_BYTES).also(buffer::get)
        val digest = ByteArray(32).also(buffer::get)
        val completed = readCompleted(id)
        if (completed != null) {
            if (isCompletedFileValid(completed, digest)) {
                incoming.remove(id.toHex())?.let { runCatching { it.file.close() } }
                File(incomingDirectory, "${id.toHex()}.meta").delete()
                File(incomingDirectory, "${id.toHex()}.part").delete()
                queueControl(completePacket(id, digest))
            } else {
                File(completedDirectory, "${id.toHex()}.done").delete()
                queueControl(rejectPacket(id, REJECT_CONFLICT, "Completed transfer metadata mismatch"))
            }
            return
        }
        val state = incoming[id.toHex()]
        if (state == null) {
            queueControl(rejectPacket(id, REJECT_UNKNOWN, "Unknown transfer; send manifest again"))
            return
        }
        if (!MessageDigest.isEqual(state.record.digest, digest) ||
            state.record.bitmap.cardinality() != state.record.chunkCount) {
            queueControl(resumePacket(state.record))
            return
        }
        finalizeIncoming(state)
    }

    private fun finalizeIncoming(state: Incoming) {
        val key = state.record.id.toHex()
        try {
            state.file.fd.sync()
            state.file.close()
            val actual = sha256(state.temp)
            if (!MessageDigest.isEqual(actual, state.record.digest)) {
                state.temp.delete()
                File(incomingDirectory, "$key.meta").delete()
                incoming.remove(key)
                queueControl(rejectPacket(state.record.id, REJECT_HASH, "SHA-256 verification failed"))
                onError("SHA-256 mismatch; rejected ${state.record.name}")
                return
            }

            val destination = uniqueDestination(state.record.name)
            // Persist the intended commit first. A crash before rename leaves the .part + .meta
            // resumable; a crash after rename lets the next manifest recover COMPLETE from .done.
            persistCompleted(state.record, destination)
            if (!state.temp.renameTo(destination)) {
                File(completedDirectory, "$key.done").delete()
                queueControl(rejectPacket(state.record.id, REJECT_STORAGE, "Could not finalize received file"))
                throw IllegalStateException("Could not finalize received file ${state.record.name}")
            }
            File(incomingDirectory, "$key.meta").delete()
            incoming.remove(key)
            onProgress("File verified and committed · ${destination.name}")
            onReceived(destination)
            queueControl(completePacket(state.record.id, state.record.digest))
        } catch (error: Exception) {
            incoming.remove(key)
            runCatching { state.file.close() }
            onError(error.message ?: "Could not finalize received file")
            throw error
        }
    }

    private fun receiveComplete(buffer: ByteBuffer) {
        require(buffer.remaining() == TRANSFER_ID_BYTES + 32) { "Malformed file-transfer COMPLETE" }
        val id = ByteArray(TRANSFER_ID_BYTES).also(buffer::get)
        val digest = ByteArray(32).also(buffer::get)
        val active = outgoing ?: return
        if (!active.record.id.contentEquals(id) || !MessageDigest.isEqual(active.record.digest, digest)) return

        outgoing = null
        pendingActions.entries.removeAll { it.value.transferId == id.toHex() }
        // Remove the durable pointer first. A crash after this point leaves only an orphan .src,
        // which startup cleanup safely removes; it cannot leave metadata pointing to no source.
        File(outgoingDirectory, "${id.toHex()}.meta").delete()
        active.source.delete()
        if (active.deleteOriginalAfterSend) active.originalSource?.delete()
        onProgress("Peer verified and committed file · ${active.record.name}")
        onSent(active.record.name)
    }

    private fun receiveReject(buffer: ByteBuffer) {
        require(buffer.remaining() >= TRANSFER_ID_BYTES + 1 + 2) { "Malformed file-transfer REJECT" }
        val id = ByteArray(TRANSFER_ID_BYTES).also(buffer::get)
        val code = buffer.get().toInt() and 0xff
        require(code in REJECT_UNKNOWN..REJECT_CONFLICT) { "Unknown file-transfer rejection code" }
        val length = buffer.short.toInt() and 0xffff
        require(length <= MAX_REJECT_TEXT_BYTES && buffer.remaining() == length) { "Invalid REJECT reason" }
        val reason = decodeUtf8(ByteArray(length).also(buffer::get))
        val active = outgoing ?: return
        if (!active.record.id.contentEquals(id)) return
        active.awaitingComplete = false
        active.awaitingResume = false
        active.waitingMessageId = null
        active.blockedByReject = true
        onError("Peer rejected ${active.record.name} (code $code): $reason. The source remains saved for retry.")
    }

    private fun chunkPacket(active: Outgoing, index: Int): ByteArray {
        val offset = index.toLong() * CHUNK_BYTES
        val length = minOf(CHUNK_BYTES.toLong(), active.record.size - offset).toInt()
        val bytes = ByteArray(length)
        RandomAccessFile(active.source, "r").use { file ->
            file.seek(offset)
            file.readFully(bytes)
        }
        return try {
            ByteBuffer.allocate(PACKET_HEADER_BYTES + TRANSFER_ID_BYTES + 4 + 2 + length)
                .order(ByteOrder.BIG_ENDIAN)
                .put(MAGIC).put(TYPE_CHUNK.toByte()).put(active.record.id).putInt(index)
                .putShort(length.toShort()).put(bytes).array()
        } finally {
            bytes.fill(0)
        }
    }

    private fun manifestPacket(record: Record): ByteArray {
        val name = record.name.toByteArray(Charsets.UTF_8)
        return ByteBuffer.allocate(PACKET_HEADER_BYTES + TRANSFER_ID_BYTES + 8 + 4 + 4 + 32 + 2 + name.size)
            .order(ByteOrder.BIG_ENDIAN)
            .put(MAGIC).put(TYPE_MANIFEST.toByte()).put(record.id).putLong(record.size)
            .putInt(CHUNK_BYTES).putInt(record.chunkCount).put(record.digest)
            .putShort(name.size.toShort()).put(name).array()
    }

    private fun resumePacket(record: Record): ByteArray {
        val bitmap = record.bitmap.toByteArray()
        return ByteBuffer.allocate(PACKET_HEADER_BYTES + TRANSFER_ID_BYTES + 32 + 4 + 2 + bitmap.size)
            .order(ByteOrder.BIG_ENDIAN)
            .put(MAGIC).put(TYPE_RESUME.toByte()).put(record.id).put(record.digest).putInt(record.chunkCount)
            .putShort(bitmap.size.toShort()).put(bitmap).array()
    }

    private fun finishPacket(record: Record): ByteArray =
        ByteBuffer.allocate(PACKET_HEADER_BYTES + TRANSFER_ID_BYTES + 32)
            .order(ByteOrder.BIG_ENDIAN).put(MAGIC).put(TYPE_FINISH.toByte())
            .put(record.id).put(record.digest).array()

    private fun completePacket(id: ByteArray, digest: ByteArray): ByteArray =
        ByteBuffer.allocate(PACKET_HEADER_BYTES + TRANSFER_ID_BYTES + 32)
            .order(ByteOrder.BIG_ENDIAN).put(MAGIC).put(TYPE_COMPLETE.toByte())
            .put(id).put(digest).array()

    private fun rejectPacket(id: ByteArray, code: Int, reason: String): ByteArray {
        val text = reason.take(MAX_REJECT_TEXT_BYTES / 4).toByteArray(Charsets.UTF_8)
        return ByteBuffer.allocate(PACKET_HEADER_BYTES + TRANSFER_ID_BYTES + 1 + 2 + text.size)
            .order(ByteOrder.BIG_ENDIAN).put(MAGIC).put(TYPE_REJECT.toByte())
            .put(id).put(code.toByte()).putShort(text.size.toShort()).put(text).array()
    }

    private fun cleanupCommittedIncomingRecords() {
        incomingDirectory.listFiles { file -> file.isFile && file.name.endsWith(".meta") }
            ?.forEach { meta ->
                runCatching {
                    val record = readRecord(meta, META_INCOMING)
                    val completed = readCompleted(record.id)
                    if (completed != null && isCompletedFileValid(completed, record.digest, record.size)) {
                        incoming.remove(record.id.toHex())?.let { runCatching { it.file.close() } }
                        meta.delete()
                        File(incomingDirectory, "${record.id.toHex()}.part").delete()
                    }
                }
            }
    }

    private fun recoverIncoming() {
        incomingDirectory.listFiles { file -> file.isFile && file.name.endsWith(".meta") }
            ?.sortedBy { it.name }
            ?.forEach { meta ->
                if (incoming.size >= MAX_INCOMING_TRANSFERS) {
                    onError("More persisted incoming transfers exist than the active limit; remaining records were retained.")
                    return@forEach
                }
                runCatching {
                    val record = readRecord(meta, META_INCOMING)
                    openIncoming(record)
                }.onFailure { onError("Could not restore incoming transfer ${meta.name}: ${it.message}") }
            }
    }

    private fun openIncoming(record: Record) {
        val key = record.id.toHex()
        val temp = File(incomingDirectory, "$key.part")
        require(temp.isFile && temp.length() == record.size) { "Partial file is missing or has an invalid size" }
        require(incoming.size < MAX_INCOMING_TRANSFERS || incoming.containsKey(key)) { "Incoming transfer limit reached" }
        if (!incoming.containsKey(key)) {
            incoming[key] = Incoming(record, temp, RandomAccessFile(temp, "rw"))
        }
    }

    private fun recoverOutgoing() {
        val metaFiles = outgoingDirectory.listFiles { file -> file.isFile && file.name.endsWith(".meta") }
            ?.sortedBy { it.lastModified() } ?: return
        for (meta in metaFiles) {
            runCatching {
                val record = readRecord(meta, META_OUTGOING)
                val source = File(outgoingDirectory, "${record.id.toHex()}.src")
                require(source.isFile && source.length() == record.size) { "Staged source file is missing or truncated" }
                require(MessageDigest.isEqual(sha256(source), record.digest)) { "Staged source SHA-256 mismatch" }
                if (outgoing == null) {
                    outgoing = Outgoing(record, source, false, null)
                    onProgress("Restored pending outgoing transfer · ${record.name}")
                } else {
                    onError("Additional persisted outgoing transfer ${record.name} is retained but not active.")
                }
            }.onFailure { onError("Could not restore outgoing transfer ${meta.name}: ${it.message}") }
        }
    }

    private fun persistRecord(directory: File, record: Record, kind: Int) {
        val bitmap = record.bitmap.toByteArray()
        val name = record.name.toByteArray(Charsets.UTF_8)
        val bytes = ByteArrayOutputStream().use { raw ->
            DataOutputStream(raw).use { out ->
                out.writeInt(META_MAGIC)
                out.writeByte(META_VERSION)
                out.writeByte(kind)
                out.write(record.id)
                out.writeLong(record.size)
                out.writeInt(CHUNK_BYTES)
                out.writeInt(record.chunkCount)
                out.write(record.digest)
                out.writeShort(name.size)
                out.write(name)
                out.writeInt(bitmap.size)
                out.write(bitmap)
            }
            raw.toByteArray()
        }
        atomicWrite(File(directory, "${record.id.toHex()}.meta"), bytes)
        bytes.fill(0)
    }

    private fun readRecord(file: File, expectedKind: Int): Record {
        val raw = file.readBytes()
        require(raw.size <= MAX_META_BYTES) { "Transfer metadata is too large" }
        return DataInputStream(ByteArrayInputStream(raw)).use { input ->
            require(input.readInt() == META_MAGIC) { "Invalid transfer metadata magic" }
            require(input.readUnsignedByte() == META_VERSION) { "Unsupported transfer metadata version" }
            require(input.readUnsignedByte() == expectedKind) { "Unexpected transfer metadata type" }
            val id = ByteArray(TRANSFER_ID_BYTES).also(input::readFully)
            val size = input.readLong()
            val chunkSize = input.readInt()
            val count = input.readInt()
            val digest = ByteArray(32).also(input::readFully)
            val nameLength = input.readUnsignedShort()
            require(nameLength in 1..MAX_NAME_BYTES) { "Invalid persisted file name length" }
            val name = sanitizeName(decodeUtf8(ByteArray(nameLength).also(input::readFully)))
            val bitmapLength = input.readInt()
            require(size in 1..MAX_FILE_BYTES && chunkSize == CHUNK_BYTES) { "Invalid persisted transfer dimensions" }
            require(count == ((size + CHUNK_BYTES - 1) / CHUNK_BYTES).toInt() && count <= MAX_CHUNKS) {
                "Invalid persisted chunk count"
            }
            require(bitmapLength in 0..((count + 7) / 8)) { "Invalid persisted bitmap size" }
            val bitmapBytes = ByteArray(bitmapLength).also(input::readFully)
            require(input.available() == 0) { "Trailing transfer metadata bytes" }
            val bitmap = BitSet.valueOf(bitmapBytes)
            require(bitmap.length() <= count) { "Persisted bitmap exceeds chunk count" }
            Record(id, name, size, count, digest, bitmap)
        }
    }

    private fun persistCompleted(record: Record, destination: File) {
        val name = destination.name.toByteArray(Charsets.UTF_8)
        val bytes = ByteArrayOutputStream().use { raw ->
            DataOutputStream(raw).use { out ->
                out.writeInt(DONE_MAGIC)
                out.writeByte(META_VERSION)
                out.write(record.digest)
                out.writeLong(record.size)
                out.writeLong(System.currentTimeMillis())
                out.writeShort(name.size)
                out.write(name)
            }
            raw.toByteArray()
        }
        atomicWrite(File(completedDirectory, "${record.id.toHex()}.done"), bytes)
        bytes.fill(0)
    }

    private fun readCompleted(id: ByteArray): Completed? {
        val file = File(completedDirectory, "${id.toHex()}.done")
        if (!file.isFile) return null
        return runCatching {
            DataInputStream(ByteArrayInputStream(file.readBytes())).use { input ->
                require(input.readInt() == DONE_MAGIC && input.readUnsignedByte() == META_VERSION)
                val digest = ByteArray(32).also(input::readFully)
                val size = input.readLong()
                val timestamp = input.readLong()
                val nameLength = input.readUnsignedShort()
                require(nameLength in 1..255 && input.available() == nameLength)
                val name = sanitizeStoredName(decodeUtf8(ByteArray(nameLength).also(input::readFully)))
                require(size in 1..MAX_FILE_BYTES && timestamp > 0)
                Completed(digest, name, size)
            }
        }.getOrElse {
            file.delete()
            null
        }
    }

    private fun pruneCompletedRecords() {
        val files = completedDirectory.listFiles { file -> file.isFile && file.name.endsWith(".done") } ?: return
        val cutoff = System.currentTimeMillis() - COMPLETED_RETENTION_MS
        files.forEach { file ->
            val timestamp = runCatching {
                DataInputStream(FileInputStream(file)).use { input ->
                    require(input.readInt() == DONE_MAGIC)
                    input.readUnsignedByte()
                    input.skipBytes(32 + 8)
                    input.readLong()
                }
            }.getOrDefault(0L)
            if (timestamp < cutoff) file.delete()
        }
        completedDirectory.listFiles()?.filter { it.isFile && it.name.endsWith(".done") }
            ?.sortedBy { it.lastModified() }
            ?.let { remaining ->
                remaining.take((remaining.size - MAX_COMPLETED_RECORDS).coerceAtLeast(0)).forEach { it.delete() }
            }
    }

    private fun sameManifest(left: Record, right: Record): Boolean =
        left.id.contentEquals(right.id) && left.name == right.name && left.size == right.size &&
            left.chunkCount == right.chunkCount && MessageDigest.isEqual(left.digest, right.digest)

    private fun isCompletedFileValid(
        completed: Completed,
        expectedDigest: ByteArray,
        expectedSize: Long? = null
    ): Boolean {
        if (!MessageDigest.isEqual(completed.digest, expectedDigest)) return false
        if (expectedSize != null && completed.size != expectedSize) return false
        val file = File(receivedDirectory, completed.fileName)
        if (!file.isFile || file.length() != completed.size) return false
        return MessageDigest.isEqual(sha256(file), completed.digest)
    }

    private fun uniqueDestination(name: String): File {
        var candidate = File(receivedDirectory, name)
        if (!candidate.exists()) return candidate
        val dot = name.lastIndexOf('.')
        val stem = if (dot > 0) name.substring(0, dot) else name
        val ext = if (dot > 0) name.substring(dot) else ""
        var suffix = 1
        while (candidate.exists()) {
            candidate = File(receivedDirectory, "$stem ($suffix)$ext")
            suffix++
        }
        return candidate
    }

    private fun atomicWrite(destination: File, bytes: ByteArray) {
        val temporary = File(destination.parentFile, destination.name + ".tmp")
        FileOutputStream(temporary).use { output ->
            output.write(bytes)
            output.fd.sync()
        }
        try {
            Files.move(temporary.toPath(), destination.toPath(),
                StandardCopyOption.ATOMIC_MOVE, StandardCopyOption.REPLACE_EXISTING)
        } catch (_: Exception) {
            Files.move(temporary.toPath(), destination.toPath(), StandardCopyOption.REPLACE_EXISTING)
        }
    }

    private fun copyBounded(source: File, destination: File) {
        var total = 0L
        FileInputStream(source).use { input ->
            FileOutputStream(destination).use { output ->
                val buffer = ByteArray(32 * 1024)
                try {
                    while (true) {
                        val read = input.read(buffer)
                        if (read < 0) break
                        total += read
                        require(total <= MAX_FILE_BYTES) { "File exceeds the 10 MiB Android transfer limit" }
                        output.write(buffer, 0, read)
                    }
                    output.fd.sync()
                } finally {
                    buffer.fill(0)
                }
            }
        }
    }

    private fun sha256(file: File): ByteArray {
        val digest = MessageDigest.getInstance("SHA-256")
        file.inputStream().buffered().use { input ->
            val buffer = ByteArray(32 * 1024)
            try {
                while (true) {
                    val read = input.read(buffer)
                    if (read < 0) break
                    digest.update(buffer, 0, read)
                }
            } finally {
                buffer.fill(0)
            }
        }
        return digest.digest()
    }

    private fun decodeUtf8(bytes: ByteArray): String =
        Charsets.UTF_8.newDecoder()
            .onMalformedInput(CodingErrorAction.REPORT)
            .onUnmappableCharacter(CodingErrorAction.REPORT)
            .decode(ByteBuffer.wrap(bytes)).toString()

    private fun sanitizeStoredName(input: String): String {
        require('/' !in input && '\\' !in input) { "Invalid stored destination name" }
        require(input.isNotBlank() && input != "." && input != "..") { "Invalid stored destination name" }
        require(input.toByteArray(Charsets.UTF_8).size <= 255 && input.none { it < ' ' || it == '\u007f' }) {
            "Invalid stored destination name"
        }
        return input
    }

    companion object {
        const val MAX_FILE_BYTES = 10L * 1024L * 1024L
        const val CHUNK_BYTES = 1_400
        const val MAX_NAME_BYTES = 180
        const val MAX_INCOMING_TRANSFERS = 2

        private const val TRANSFER_ID_BYTES = 16
        private const val MAGIC_SIZE = 4
        private const val PACKET_HEADER_BYTES = MAGIC_SIZE + 1
        private const val MANIFEST_FIXED_BYTES = PACKET_HEADER_BYTES + TRANSFER_ID_BYTES + 8 + 4 + 4 + 32 + 2
        private const val CHUNK_FIXED_BYTES = PACKET_HEADER_BYTES + TRANSFER_ID_BYTES + 4 + 2

        private const val TYPE_MANIFEST = 1
        private const val TYPE_CHUNK = 2
        private const val TYPE_RESUME = 3
        private const val TYPE_FINISH = 4
        private const val TYPE_COMPLETE = 5
        private const val TYPE_REJECT = 6

        private const val REJECT_UNKNOWN = 1
        private const val REJECT_HASH = 2
        private const val REJECT_STORAGE = 3
        private const val REJECT_CONFLICT = 4
        private const val MAX_REJECT_TEXT_BYTES = 160

        private const val META_MAGIC = 0x45434F46 // ECOF
        private const val DONE_MAGIC = 0x45434F44 // ECOD
        private const val META_VERSION = 1
        private const val META_INCOMING = 1
        private const val META_OUTGOING = 2
        private const val MAX_META_BYTES = 2_048
        private const val MAX_CHUNKS = 7_490
        private const val MAX_COMPLETED_RECORDS = 512
        private const val MAX_CONTROL_QUEUE = 8
        private const val COMPLETED_RETENTION_MS = 30L * 24L * 60L * 60L * 1_000L
        private const val RESUME_RETRY_MS = 45_000L
        private const val FINISH_RETRY_MS = 30_000L
        private const val SEND_RETRY_DELAY_MS = 5_000L
        private val MAGIC = byteArrayOf(0x45, 0x43, 0x54, 0x31) // ECT1

        fun isTransferPacket(packet: ByteArray): Boolean =
            packet.size >= PACKET_HEADER_BYTES &&
                packet[0] == MAGIC[0] && packet[1] == MAGIC[1] &&
                packet[2] == MAGIC[2] && packet[3] == MAGIC[3]

        fun sanitizeName(input: String): String {
            require('/' !in input && '\\' !in input) { "Path separators are not allowed in file names" }
            val cleaned = input.trim().filter { it >= ' ' && it != '\u007f' }.trim('.', ' ')
            require(cleaned.isNotBlank() && cleaned != "." && cleaned != "..") { "Invalid file name" }
            require(cleaned.toByteArray(Charsets.UTF_8).size <= MAX_NAME_BYTES) { "File name is too long" }
            return cleaned
        }

        private fun ByteArray.toHex(): String = joinToString("") { "%02x".format(it) }
    }
}