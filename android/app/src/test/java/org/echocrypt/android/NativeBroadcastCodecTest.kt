package org.echocrypt.android

import org.echocrypt.android.protocol.NativeBroadcastAssembler
import org.echocrypt.android.protocol.NativeBroadcastCodec
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertThrows
import org.junit.Test

class NativeBroadcastCodecTest {
    @Test fun desktopCompatibleEcbv1GoldenPacket() {
        val sender = NativeBroadcastCodec("ALICE001".toByteArray())
        val packet = sender.encodeText("hello echo", "MESSAGE1".toByteArray()).single()
        val expected = "4543425201010000414c4943453030314d45535341474531000000010000001ae00f0646de6e5a1dd5fe8f79f6580db4eeff9a018721eb0feb17d41994f5"
        assertEquals(expected, packet.joinToString("") { "%02x".format(it.toInt() and 0xff) })
    }

    @Test fun publicBroadcastTextRoundTripsWithoutPeerHandshake() {
        val sender = NativeBroadcastCodec("ALICE001".toByteArray())
        val receiver = NativeBroadcastCodec("BOB00001".toByteArray())
        val frame = receiver.decode(sender.encodeText("open room: درود به همه").single())
        assertEquals("ALICE001", String(frame.senderId))
        assertEquals("open room: درود به همه", String(frame.payload, Charsets.UTF_8))
    }

    @Test fun multiFragmentTextReassemblesOutOfOrderAndSuppressesDuplicates() {
        val sender = NativeBroadcastCodec("ALICE001".toByteArray())
        val receiver = NativeBroadcastCodec("BOB00001".toByteArray())
        val assembler = NativeBroadcastAssembler()
        val message = "ECHOCRYPT-".repeat(180)
        val packets = sender.encodeText(message)
        assertEquals(2, packets.size)
        assertNull(assembler.accept(receiver.decode(packets[1])))
        val first = receiver.decode(packets[0])
        assertEquals(message, assembler.accept(first))
        assertNull(assembler.accept(first))
    }

    @Test fun leavingAndRejoiningIgnoresCompletionFromThePreviousRoom() {
        val callbacks = mutableListOf<() -> Unit>()
        var queueCalls = 0
        val transport = org.echocrypt.android.protocol.NativeBroadcastTransport(
            enqueueAudio = { _, callback ->
                queueCalls++
                callbacks += callback
                true
            },
            onTextReceived = { _, _ -> },
            onError = { throw AssertionError(it) },
            onStateChanged = { }
        )
        transport.join()
        transport.sendText("old room")
        assertEquals(1, queueCalls)
        transport.leave()

        transport.join()
        transport.sendText("new room")
        assertEquals(2, queueCalls)

        callbacks[0].invoke() // stale callback must not consume or duplicate the new frame
        assertEquals(2, queueCalls)
        callbacks[1].invoke()
        assertEquals(2, queueCalls)
        transport.leave()
    }

    @Test fun rejectsCorruptedBroadcastPacket() {
        val sender = NativeBroadcastCodec("ALICE001".toByteArray())
        val receiver = NativeBroadcastCodec("BOB00001".toByteArray())
        val packet = sender.encodeText("test").single().copyOf()
        packet[packet.lastIndex - 5] = (packet[packet.lastIndex - 5].toInt() xor 0x40).toByte()
        assertThrows(IllegalArgumentException::class.java) { receiver.decode(packet) }
    }
}
