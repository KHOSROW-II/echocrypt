package org.echocrypt.android

import org.echocrypt.android.protocol.NativeFileTransfer
import org.junit.Assert.*
import org.junit.Test
import java.io.File
import java.nio.ByteBuffer
import java.nio.ByteOrder
import java.nio.file.Files
import java.util.ArrayDeque

class NativeFileTransferTest {
    private data class Sent(val id: Int, val packet: ByteArray)

    private class Endpoint(
        val root: File,
        received: File = File(root, "received")
    ) {
        val sent = ArrayDeque<Sent>()
        val errors = mutableListOf<String>()
        val receivedFiles = mutableListOf<File>()
        val sentNames = mutableListOf<String>()
        private var nextId = 1
        val transfer = NativeFileTransfer(
            storageDirectory = File(root, "state"),
            sendPacket = { bytes ->
                val id = nextId++
                sent.addLast(Sent(id, bytes.copyOf()))
                id
            },
            onReceived = { receivedFiles.add(it) },
            onError = { errors.add(it) },
            onSent = { sentNames.add(it) },
            receivedDirectory = received
        )
    }

    @Test
    fun transferUsesManifestResumeChunksAndApplicationComplete() {
        val root = Files.createTempDirectory("ec-e2e").toFile()
        val source = File(root, "source.bin").apply {
            writeBytes(ByteArray(3_200) { (it * 17 % 251).toByte() })
        }
        val sender = Endpoint(File(root, "sender"))
        val receiver = Endpoint(File(root, "receiver"))

        sender.transfer.start(source, "payload.bin")
        val manifest = sender.sent.removeFirst()
        assertTrue(NativeFileTransfer.isTransferPacket(manifest.packet))
        assertEquals(1, type(manifest.packet))

        receiver.transfer.receive(manifest.packet)
        sender.transfer.onReliablePacketDelivered(manifest.id)
        val resume = receiver.sent.removeFirst()
        assertEquals(3, type(resume.packet))
        sender.transfer.receive(resume.packet)
        receiver.transfer.onReliablePacketDelivered(resume.id)

        var guard = 0
        while (guard++ < 100) {
            if (sender.sent.isNotEmpty()) {
                val sent = sender.sent.removeFirst()
                receiver.transfer.receive(sent.packet)
                sender.transfer.onReliablePacketDelivered(sent.id)
            } else if (receiver.sent.isNotEmpty()) {
                val sent = receiver.sent.removeFirst()
                sender.transfer.receive(sent.packet)
                receiver.transfer.onReliablePacketDelivered(sent.id)
            } else if (sender.transfer.pendingOutgoingName() == null) {
                break
            } else {
                fail("Transfer stalled with pending file ${sender.transfer.pendingOutgoingName()}")
            }
        }

        assertTrue("sender must receive final app-level completion", sender.sentNames.contains("payload.bin"))
        assertEquals(1, receiver.receivedFiles.size)
        assertArrayEquals(source.readBytes(), receiver.receivedFiles.single().readBytes())
        assertNull(sender.transfer.pendingOutgoingName())
        sender.transfer.close()
        receiver.transfer.close()
        root.deleteRecursively()
    }

    @Test
    fun incomingAndOutgoingStateSurviveRecreationAndResumeFromFirstMissingChunk() {
        val root = Files.createTempDirectory("ec-resume").toFile()
        val sourceBytes = ByteArray(4_600) { (it * 31 % 253).toByte() }
        val source = File(root, "source.bin").apply { writeBytes(sourceBytes) }

        val senderRoot = File(root, "sender")
        val receiverRoot = File(root, "receiver")
        var sender = Endpoint(senderRoot)
        var receiver = Endpoint(receiverRoot)

        sender.transfer.start(source, "resume.bin")
        val manifest = sender.sent.removeFirst()
        receiver.transfer.receive(manifest.packet)
        sender.transfer.onReliablePacketDelivered(manifest.id)
        val initialResume = receiver.sent.removeFirst()
        sender.transfer.receive(initialResume.packet)
        receiver.transfer.onReliablePacketDelivered(initialResume.id)

        val firstChunk = sender.sent.removeFirst()
        assertEquals(2, type(firstChunk.packet))
        assertEquals(0, chunkIndex(firstChunk.packet))
        receiver.transfer.receive(firstChunk.packet)
        sender.transfer.onReliablePacketDelivered(firstChunk.id)

        // Chunk 1 is queued but intentionally lost with the connection.
        assertTrue(sender.sent.isNotEmpty())
        assertEquals(1, chunkIndex(sender.sent.first().packet))
        sender.transfer.close()
        receiver.transfer.close()
        sender.sent.clear()

        sender = Endpoint(senderRoot)
        receiver = Endpoint(receiverRoot)
        sender.transfer.resumePending()
        val reconnectManifest = sender.sent.removeFirst()
        assertEquals(1, type(reconnectManifest.packet))
        receiver.transfer.receive(reconnectManifest.packet)
        sender.transfer.onReliablePacketDelivered(reconnectManifest.id)
        val persistedResume = receiver.sent.removeFirst()
        sender.transfer.receive(persistedResume.packet)
        receiver.transfer.onReliablePacketDelivered(persistedResume.id)

        val resumedChunk = sender.sent.removeFirst()
        assertEquals(2, type(resumedChunk.packet))
        assertEquals("receiver's durable bitmap must skip chunk 0", 1, chunkIndex(resumedChunk.packet))
        receiver.transfer.receive(resumedChunk.packet)
        sender.transfer.onReliablePacketDelivered(resumedChunk.id)

        var guard = 0
        while (guard++ < 100) {
            if (sender.sent.isNotEmpty()) {
                val sent = sender.sent.removeFirst()
                receiver.transfer.receive(sent.packet)
                sender.transfer.onReliablePacketDelivered(sent.id)
            } else if (receiver.sent.isNotEmpty()) {
                val sent = receiver.sent.removeFirst()
                sender.transfer.receive(sent.packet)
                receiver.transfer.onReliablePacketDelivered(sent.id)
            } else if (sender.transfer.pendingOutgoingName() == null) {
                break
            } else {
                fail("Resumed transfer stalled")
            }
        }

        assertEquals(1, sender.sentNames.size)
        assertArrayEquals(sourceBytes, receiver.receivedFiles.single().readBytes())
        sender.transfer.close()
        receiver.transfer.close()
        root.deleteRecursively()
    }

    @Test
    fun senderDoesNotReportSuccessUntilReceiverSendsFinalComplete() {
        val root = Files.createTempDirectory("ec-complete").toFile()
        val source = File(root, "small.bin").apply { writeBytes(byteArrayOf(1, 2, 3, 4, 5)) }
        val sender = Endpoint(File(root, "sender"))
        val receiver = Endpoint(File(root, "receiver"))

        sender.transfer.start(source, "verified.bin")
        val manifest = sender.sent.removeFirst()
        receiver.transfer.receive(manifest.packet)
        sender.transfer.onReliablePacketDelivered(manifest.id)
        val resume = receiver.sent.removeFirst()
        sender.transfer.receive(resume.packet)
        receiver.transfer.onReliablePacketDelivered(resume.id)

        val chunk = sender.sent.removeFirst()
        receiver.transfer.receive(chunk.packet)
        sender.transfer.onReliablePacketDelivered(chunk.id)
        val finish = sender.sent.removeFirst()
        assertEquals(4, type(finish.packet))
        receiver.transfer.receive(finish.packet)
        sender.transfer.onReliablePacketDelivered(finish.id)

        assertTrue(receiver.receivedFiles.single().isFile)
        assertTrue("transport ACK is not application-level file completion", sender.sentNames.isEmpty())
        val complete = receiver.sent.removeFirst()
        assertEquals(5, type(complete.packet))
        sender.transfer.receive(complete.packet)
        receiver.transfer.onReliablePacketDelivered(complete.id)
        assertEquals(listOf("verified.bin"), sender.sentNames)
        assertNull(sender.transfer.pendingOutgoingName())

        // A repeated FINISH/manifest is answered from the durable completed marker.
        receiver.transfer.receive(manifest.packet)
        assertEquals(5, type(receiver.sent.last().packet))
        sender.transfer.close()
        receiver.transfer.close()
        root.deleteRecursively()
    }

    @Test
    fun rejectsHashMismatchAndRetainsOutgoingSource() {
        val root = Files.createTempDirectory("ec-hash").toFile()
        val source = File(root, "source.bin").apply { writeBytes(ByteArray(128) { it.toByte() }) }
        val sender = Endpoint(File(root, "sender"))
        val receiver = Endpoint(File(root, "receiver"))

        sender.transfer.start(source, "bad.bin")
        val manifest = sender.sent.removeFirst()
        receiver.transfer.receive(manifest.packet)
        sender.transfer.onReliablePacketDelivered(manifest.id)
        val resume = receiver.sent.removeFirst()
        sender.transfer.receive(resume.packet)
        receiver.transfer.onReliablePacketDelivered(resume.id)

        val chunk = sender.sent.removeFirst()
        val damaged = chunk.packet.copyOf().also { it[it.lastIndex] = (it.last().toInt() xor 0x44).toByte() }
        receiver.transfer.receive(damaged)
        sender.transfer.onReliablePacketDelivered(chunk.id)
        val finish = sender.sent.removeFirst()
        receiver.transfer.receive(finish.packet)
        sender.transfer.onReliablePacketDelivered(finish.id)
        val reject = receiver.sent.removeFirst()
        assertEquals(6, type(reject.packet))
        sender.transfer.receive(reject.packet)

        assertTrue(sender.errors.any { it.contains("rejected") })
        assertEquals("bad.bin", sender.transfer.pendingOutgoingName())
        assertTrue(receiver.receivedFiles.isEmpty())
        sender.transfer.close()
        receiver.transfer.close()
        root.deleteRecursively()
    }

    @Test
    fun receivesChunksOutOfOrderAndIgnoresDuplicates() {
        val root = Files.createTempDirectory("ec-order").toFile()
        val data = ByteArray(2_900) { (it * 7 % 239).toByte() }
        val source = File(root, "source.bin").apply { writeBytes(data) }
        val sender = Endpoint(File(root, "sender"))
        val receiver = Endpoint(File(root, "receiver"))

        sender.transfer.start(source, "ordered.dat")
        val manifest = sender.sent.removeFirst()
        receiver.transfer.receive(manifest.packet)
        sender.transfer.onReliablePacketDelivered(manifest.id)
        val resume = receiver.sent.removeFirst()
        sender.transfer.receive(resume.packet)
        receiver.transfer.onReliablePacketDelivered(resume.id)

        val chunks = mutableListOf<ByteArray>()
        var guard = 0
        while (chunks.size < 3 && guard++ < 10) {
            val sent = sender.sent.removeFirst()
            assertEquals(2, type(sent.packet))
            chunks.add(sent.packet)
            sender.transfer.onReliablePacketDelivered(sent.id)
        }
        assertEquals(3, chunks.size)
        receiver.transfer.receive(chunks[2])
        receiver.transfer.receive(chunks[2]) // duplicate index is harmless
        receiver.transfer.receive(chunks[0])
        receiver.transfer.receive(chunks[1])

        // Complete the application-level commit after all data chunks were stored.
        val finish = sender.sent.removeFirst()
        receiver.transfer.receive(finish.packet)
        sender.transfer.onReliablePacketDelivered(finish.id)
        val complete = receiver.sent.removeFirst()
        sender.transfer.receive(complete.packet)
        receiver.transfer.onReliablePacketDelivered(complete.id)

        assertArrayEquals(data, receiver.receivedFiles.single().readBytes())
        sender.transfer.close()
        receiver.transfer.close()
        root.deleteRecursively()
    }

    @Test
    fun rejectsTraversalOversizedAndLegacyPackets() {
        try {
            NativeFileTransfer.sanitizeName("../../secret.txt")
            fail("path traversal must be rejected")
        } catch (_: IllegalArgumentException) {
        }
        val root = Files.createTempDirectory("ec-limit").toFile()
        val huge = File(root, "huge.bin").apply {
            java.io.RandomAccessFile(this, "rw").use { it.setLength(NativeFileTransfer.MAX_FILE_BYTES + 1) }
        }
        val sender = Endpoint(File(root, "sender"))
        try {
            sender.transfer.start(huge)
            fail("size limit")
        } catch (_: IllegalArgumentException) {
        }
        assertFalse(NativeFileTransfer.isTransferPacket(byteArrayOf(1, 0, 0, 0, 0)))
        sender.transfer.close()
        root.deleteRecursively()
    }



    @Test
    fun transportFailurePausesUntilExplicitResumeAndRetainsStagedSource() {
        val root = Files.createTempDirectory("ec-pause").toFile()
        val source = File(root, "source.bin").apply { writeBytes(ByteArray(256) { it.toByte() }) }
        val senderRoot = File(root, "sender")
        var sender = Endpoint(senderRoot)
        sender.transfer.start(source, "paused.bin")
        val manifest = sender.sent.removeFirst()
        sender.transfer.onReliablePacketFailed(manifest.id, "no acoustic ACK")
        val countAfterFailure = sender.sent.size
        sender.transfer.tick(System.currentTimeMillis() + 600_000L)
        assertEquals("no repeated acoustic traffic while link is unavailable", countAfterFailure, sender.sent.size)
        assertEquals("paused.bin", sender.transfer.pendingOutgoingName())
        sender.transfer.close()

        sender = Endpoint(senderRoot)
        sender.transfer.resumePending()
        assertTrue("fresh session starts with a new manifest", sender.sent.isNotEmpty())
        assertEquals(1, type(sender.sent.first().packet))
        sender.transfer.close()
        root.deleteRecursively()
    }

    @Test
    fun receiverReplaysCompleteAfterAppRestartWhenFinalResponseWasLost() {
        val root = Files.createTempDirectory("ec-final-retry").toFile()
        val source = File(root, "source.bin").apply { writeBytes(ByteArray(90) { (it * 13).toByte() }) }
        val senderRoot = File(root, "sender")
        val receiverRoot = File(root, "receiver")
        var sender = Endpoint(senderRoot)
        var receiver = Endpoint(receiverRoot)

        sender.transfer.start(source, "committed.bin")
        val manifest = sender.sent.removeFirst()
        receiver.transfer.receive(manifest.packet)
        sender.transfer.onReliablePacketDelivered(manifest.id)
        val resume = receiver.sent.removeFirst()
        sender.transfer.receive(resume.packet)
        receiver.transfer.onReliablePacketDelivered(resume.id)
        val chunk = sender.sent.removeFirst()
        receiver.transfer.receive(chunk.packet)
        sender.transfer.onReliablePacketDelivered(chunk.id)
        val finish = sender.sent.removeFirst()
        receiver.transfer.receive(finish.packet)
        sender.transfer.onReliablePacketDelivered(finish.id)
        assertEquals(1, receiver.receivedFiles.size)
        assertEquals("sender has not seen COMPLETE yet", emptyList<String>(), sender.sentNames)
        // Drop COMPLETE and simulate both processes being destroyed.
        receiver.transfer.close()
        sender.transfer.close()

        sender = Endpoint(senderRoot)
        receiver = Endpoint(receiverRoot)
        sender.transfer.resumePending()
        val repeatedManifest = sender.sent.removeFirst()
        receiver.transfer.receive(repeatedManifest.packet)
        val repeatedComplete = receiver.sent.removeFirst()
        assertEquals(5, type(repeatedComplete.packet))
        sender.transfer.receive(repeatedComplete.packet)
        assertEquals(listOf("committed.bin"), sender.sentNames)
        assertNull(sender.transfer.pendingOutgoingName())
        sender.transfer.close()
        receiver.transfer.close()
        root.deleteRecursively()
    }

    @Test
    fun completedFileIsRehashedBeforeDuplicateManifestCanBeConfirmed() {
        val root = Files.createTempDirectory("ec-corrupt-done").toFile()
        val source = File(root, "source.bin").apply { writeBytes(ByteArray(48) { it.toByte() }) }
        val sender = Endpoint(File(root, "sender"))
        val receiverRoot = File(root, "receiver")
        val receiver = Endpoint(receiverRoot)

        sender.transfer.start(source, "committed.bin")
        val manifest = sender.sent.removeFirst()
        receiver.transfer.receive(manifest.packet)
        sender.transfer.onReliablePacketDelivered(manifest.id)
        val resume = receiver.sent.removeFirst()
        sender.transfer.receive(resume.packet)
        receiver.transfer.onReliablePacketDelivered(resume.id)
        val chunk = sender.sent.removeFirst()
        receiver.transfer.receive(chunk.packet)
        sender.transfer.onReliablePacketDelivered(chunk.id)
        val finish = sender.sent.removeFirst()
        receiver.transfer.receive(finish.packet)
        sender.transfer.onReliablePacketDelivered(finish.id)
        receiver.sent.removeFirst() // Drop initial COMPLETE.
        val completed = receiver.receivedFiles.single()
        completed.writeBytes(ByteArray(completed.length().toInt()) { 0x55 })

        receiver.transfer.receive(manifest.packet)
        val resumed = receiver.sent.removeFirst()
        assertEquals("corrupt committed data is not acknowledged as complete", 3, type(resumed.packet))
        sender.transfer.close()
        receiver.transfer.close()
        root.deleteRecursively()
    }

    private fun type(packet: ByteArray): Int = packet[4].toInt() and 0xff

    private fun chunkIndex(packet: ByteArray): Int =
        ByteBuffer.wrap(packet).order(ByteOrder.BIG_ENDIAN).getInt(21)
}