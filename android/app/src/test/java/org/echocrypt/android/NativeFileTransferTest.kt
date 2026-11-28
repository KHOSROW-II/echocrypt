package org.echocrypt.android

import org.echocrypt.android.protocol.NativeFileTransfer
import org.junit.Assert.*
import org.junit.Test
import java.io.File
import java.nio.file.Files

class NativeFileTransferTest {
    @Test fun sendsManifestThenChunksOnlyAfterReliableAck() {
        val root = Files.createTempDirectory("ec-send").toFile()
        val source = File(root, "source.bin").apply { writeBytes(ByteArray(3_000) { (it % 251).toByte() }) }
        val packets = mutableListOf<ByteArray>()
        val sender = NativeFileTransfer(root, { packets.add(it) })
        sender.start(source, "payload.bin")
        assertEquals(1, packets.size)
        assertEquals(1, packets[0][0].toInt())
        sender.onReliablePacketDelivered()
        assertEquals(2, packets.size)
        assertEquals(2, packets[1][0].toInt())
        sender.onReliablePacketDelivered()
        assertEquals(3, packets.size)
        sender.onReliablePacketDelivered()
        assertEquals(4, packets.size)
        sender.onReliablePacketDelivered()
        assertTrue(packets.size == 4)
        root.deleteRecursively()
    }

    @Test fun receivesOutOfOrderChunksAndVerifiesHash() {
        val root = Files.createTempDirectory("ec-recv").toFile()
        val data = ByteArray(2_500) { (it % 239).toByte() }
        val source = File(root, "input").apply { writeBytes(data) }
        val packets = mutableListOf<ByteArray>()
        val sender = NativeFileTransfer(root, { packets.add(it) })
        sender.start(source, "received.dat")
        val receiverRoot = File(root, "receiver").apply { mkdirs() }
        var received: File? = null
        val receiver = NativeFileTransfer(receiverRoot, {}, onReceived = { received = it })
        receiver.receive(packets[0])
        sender.onReliablePacketDelivered()
        sender.onReliablePacketDelivered()
        sender.onReliablePacketDelivered()
        assertEquals(3, packets.size)
        receiver.receive(packets[2])
        receiver.receive(packets[1])
        assertNull(received)
        receiver.receive(packets[2])
        assertNotNull(received)
        assertArrayEquals(data, received!!.readBytes())
        receiver.close()
        sender.close()
        root.deleteRecursively()
    }

    @Test fun rejectsTraversalAndOversizedFiles() {
        try { NativeFileTransfer.sanitizeName("../../secret.txt"); fail("traversal") }
        catch (_: IllegalArgumentException) {}
        val root = Files.createTempDirectory("ec-limit").toFile()
        val huge = File(root, "huge.bin").apply { RandomAccessFileCompat.create(this, NativeFileTransfer.MAX_FILE_BYTES + 1) }
        val sender = NativeFileTransfer(root, {})
        try { sender.start(huge); fail("size limit") }
        catch (_: IllegalArgumentException) {}
        root.deleteRecursively()
    }

    private object RandomAccessFileCompat {
        fun create(file: File, size: Long) { java.io.RandomAccessFile(file, "rw").use { it.setLength(size) } }
    }
}
