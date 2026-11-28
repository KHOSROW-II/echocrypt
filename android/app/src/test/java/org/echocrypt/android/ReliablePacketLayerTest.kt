package org.echocrypt.android

import org.echocrypt.android.protocol.ReliablePacketLayer
import org.junit.Assert.*
import org.junit.Test

class ReliablePacketLayerTest {
    private data class Link(
        val ab: ArrayDeque<ByteArray> = ArrayDeque(),
        val ba: ArrayDeque<ByteArray> = ArrayDeque(),
        val received: MutableList<ByteArray> = mutableListOf(),
        val delivered: MutableList<Int> = mutableListOf()
    )
    private fun pair(): Triple<ReliablePacketLayer, ReliablePacketLayer, Link> {
        val l = Link()
        val a = ReliablePacketLayer({ it.copyOf() }, { it.copyOf() }, { l.ab.addLast(it) },
            { l.received.add(it.copyOf()) }, { l.delivered.add(it) },
            fragmentPayloadBytes = 16, retryTimeoutMillis = 1_000, maxAttempts = 3, reassemblyTimeoutMillis = 5_000)
        val b = ReliablePacketLayer({ it.copyOf() }, { it.copyOf() }, { l.ba.addLast(it) },
            { l.received.add(it.copyOf()) },
            fragmentPayloadBytes = 16, retryTimeoutMillis = 1_000, maxAttempts = 3, reassemblyTimeoutMillis = 5_000)
        return Triple(a, b, l)
    }
    @Test fun fragmentsAndWaitsForAckBeforeNextFragment() {
        val (a,b,l)=pair()
        val msg="Reliable ECHOCRYPT — سلام".toByteArray()
        val id=a.send(msg,0)
        assertEquals(1,l.ab.size)
        var now=10L
        while(a.hasPendingMessage()) {
            b.receive(l.ab.removeFirst(),now++)
            a.receive(l.ba.removeFirst(),now++)
        }
        assertArrayEquals(msg,l.received.single())
        assertEquals(listOf(id),l.delivered)
    }
    @Test fun timeoutRetransmitsAndDuplicateDoesNotRedeliver() {
        val (a,b,l)=pair()
        val msg="hello".toByteArray()
        a.send(msg,0)
        val first=l.ab.removeFirst()
        b.receive(first,10)
        val droppedAck=l.ba.removeFirst()
        a.tick(1_001)
        b.receive(l.ab.removeFirst(),1_010)
        assertEquals(1,l.received.size)
        assertEquals(1,l.ba.size)
        a.receive(droppedAck,1_020)
        assertFalse(a.hasPendingMessage())
        b.receive(first,1_030)
        assertEquals(1,l.received.size)
        assertEquals(1,l.ba.size)
    }
    @Test fun retryLimitFails() {
        val failed=mutableListOf<Int>()
        val a=ReliablePacketLayer({it},{it},{},{},{}, { id, _ -> failed.add(id) },
            fragmentPayloadBytes=16,retryTimeoutMillis=1_000,maxAttempts=2,reassemblyTimeoutMillis=5_000)
        val id=a.send(byteArrayOf(1),0)
        a.tick(1_001)
        a.tick(2_002)
        assertFalse(a.hasPendingMessage())
        assertEquals(listOf(id),failed)
    }
    @Test fun rejectsOversizedAndMalformedPackets() {
        val (a,b,_)=pair()
        try { a.send(ByteArray(ReliablePacketLayer.MAX_MESSAGE_BYTES+1),0); fail("oversized") }
        catch (_: IllegalArgumentException) {}
        try { b.receive(byteArrayOf(1,2,3),0); fail("malformed") }
        catch (_: IllegalArgumentException) {}
    }
}
