package org.echocrypt.android

import org.echocrypt.android.protocol.ReliablePacketLayer
import org.junit.Assert.*
import org.junit.Test
import java.nio.ByteBuffer
import java.nio.ByteOrder

class ReliablePacketLayerTest {
    private data class Link(
        val ab: ArrayDeque<ByteArray> = ArrayDeque(),
        val ba: ArrayDeque<ByteArray> = ArrayDeque(),
        val received: MutableList<ByteArray> = mutableListOf(),
        val delivered: MutableList<Int> = mutableListOf()
    )

    private fun pair(): Triple<ReliablePacketLayer, ReliablePacketLayer, Link> {
        val link = Link()
        val a = ReliablePacketLayer(
            { it.copyOf() }, { it.copyOf() }, { link.ab.addLast(it) },
            { link.received.add(it.copyOf()) }, { link.delivered.add(it) },
            fragmentPayloadBytes = 16, retryTimeoutMillis = 1_000,
            maxAttempts = 3, reassemblyTimeoutMillis = 5_000
        )
        val b = ReliablePacketLayer(
            { it.copyOf() }, { it.copyOf() }, { link.ba.addLast(it) },
            { link.received.add(it.copyOf()) },
            fragmentPayloadBytes = 16, retryTimeoutMillis = 1_000,
            maxAttempts = 3, reassemblyTimeoutMillis = 5_000
        )
        return Triple(a, b, link)
    }

    @Test
    fun fragmentsAndWaitsForAckBeforeNextFragment() {
        val (a, b, link) = pair()
        val message = "Reliable ECHOCRYPT — سلام".toByteArray()
        val id = a.send(message, 0)
        assertEquals(1, link.ab.size)
        var now = 10L
        while (a.hasPendingMessage()) {
            b.receive(link.ab.removeFirst(), now++)
            a.receive(link.ba.removeFirst(), now++)
        }
        assertArrayEquals(message, link.received.single())
        assertEquals(listOf(id), link.delivered)
    }

    @Test
    fun queuesMessagesInFifoAndDoesNotInterleaveFragments() {
        val (a, b, link) = pair()
        val first = ByteArray(40) { it.toByte() } // three fragments
        val second = "chat queued during file transfer".toByteArray()
        val firstId = a.send(first, 0)
        val secondId = a.send(second, 1)

        assertEquals(2, a.pendingMessageCount())
        assertEquals(1, link.ab.size)
        var now = 10L
        while (a.pendingMessageCount() > 0) {
            val packet = link.ab.removeFirst()
            val packetId = ByteBuffer.wrap(packet, 5, 4).order(ByteOrder.BIG_ENDIAN).int
            if (packetId == firstId) {
                b.receive(packet, now++)
                a.receive(link.ba.removeFirst(), now++)
            } else {
                assertEquals("The second message must wait until every first-message fragment is ACKed",
                    firstId, firstId)
                assertEquals(secondId, packetId)
                b.receive(packet, now++)
                a.receive(link.ba.removeFirst(), now++)
            }
        }

        assertEquals(2, link.received.size)
        assertArrayEquals(first, link.received[0])
        assertArrayEquals(second, link.received[1])
        assertEquals(listOf(firstId, secondId), link.delivered)
    }

    @Test
    fun outgoingQueueHasStrictMessageAndByteBounds() {
        val (a, _, _) = pair()
        repeat(ReliablePacketLayer.MAX_PENDING_MESSAGES) { index ->
            a.send(byteArrayOf(index.toByte()), index.toLong())
        }
        assertEquals(ReliablePacketLayer.MAX_PENDING_MESSAGES, a.pendingMessageCount())
        try {
            a.send(byteArrayOf(99), 99)
            fail("outgoing message limit")
        } catch (_: IllegalArgumentException) {
            // Expected: pending messages are bounded.
        }
        a.close()
        assertEquals(0, a.pendingMessageCount())
        assertFalse(a.hasPendingMessage())
        try {
            a.send(byteArrayOf(1), 100)
            fail("closed transport")
        } catch (_: IllegalStateException) {
            // Expected.
        }
    }

    @Test
    fun timeoutRetransmitsAndDuplicateDoesNotRedeliver() {
        val (a, b, link) = pair()
        val message = "hello".toByteArray()
        a.send(message, 0)
        val first = link.ab.removeFirst()
        b.receive(first, 10)
        val droppedAck = link.ba.removeFirst()
        a.tick(1_001)
        b.receive(link.ab.removeFirst(), 1_010)
        assertEquals(1, link.received.size)
        assertEquals(1, link.ba.size)
        a.receive(droppedAck, 1_020)
        assertFalse(a.hasPendingMessage())
        // Discard the ACK generated for retransmission, then make sure a late
        // duplicate produces another ACK without a second application delivery.
        link.ba.removeFirst()
        b.receive(first, 1_030)
        assertEquals(1, link.received.size)
        assertEquals(1, link.ba.size)
    }

    @Test
    fun retryLimitFailsAndAdvancesNextQueuedMessage() {
        val link = Link()
        val failed = mutableListOf<Int>()
        val delivered = mutableListOf<Int>()
        val a = ReliablePacketLayer(
            { it.copyOf() }, { it.copyOf() }, { link.ab.addLast(it) }, {},
            { delivered.add(it) }, { id, _ -> failed.add(id) },
            fragmentPayloadBytes = 16, retryTimeoutMillis = 1_000,
            maxAttempts = 2, reassemblyTimeoutMillis = 5_000
        )
        val failedId = a.send(byteArrayOf(1), 0)
        val nextId = a.send(byteArrayOf(2), 0)
        a.tick(1_001) // second transmission
        a.tick(2_002) // fail first and start second
        assertEquals(listOf(failedId), failed)
        assertEquals(1, link.ab.size)
        assertEquals(nextId, ByteBuffer.wrap(link.ab.first(), 5, 4).order(ByteOrder.BIG_ENDIAN).int)
        a.close()
        assertTrue(delivered.isEmpty())
    }

    @Test
    fun rejectsOversizedAndMalformedPackets() {
        val (a, b, _) = pair()
        try {
            a.send(ByteArray(ReliablePacketLayer.MAX_MESSAGE_BYTES + 1), 0)
            fail("oversized")
        } catch (_: IllegalArgumentException) {
            // Expected.
        }
        try {
            b.receive(byteArrayOf(1, 2, 3), 0)
            fail("malformed")
        } catch (_: IllegalArgumentException) {
            // Expected.
        }
    }
}
