package org.echocrypt.android.protocol

import org.echocrypt.android.AppEventLog
import java.util.ArrayDeque

/** Open no-handshake acoustic room; packets use desktop ECBR v1 text framing. */
class NativeBroadcastTransport(
    private val enqueueAudio: (FloatArray, () -> Unit) -> Boolean,
    private val onTextReceived: (sender: String, text: String) -> Unit,
    private val onError: (String) -> Unit,
    private val onStateChanged: (String) -> Unit
) {
    private val codec = NativeBroadcastCodec()
    private val assembler = NativeBroadcastAssembler()
    private val pendingPackets = ArrayDeque<ByteArray>()
    private var pendingBytes = 0
    private var playbackInFlight = false
    private var generation = 0L

    @Volatile var isJoined: Boolean = false
        private set

    @Synchronized
    fun join() {
        if (isJoined) return
        generation++
        playbackInFlight = false
        isJoined = true
        AppEventLog.record("BROADCAST_JOIN", "mode=open-public sender=" + senderLabel())
        onStateChanged("JOINED · OPEN PUBLIC BROADCAST")
        pump()
    }

    @Synchronized
    fun leave() {
        if (!isJoined) return
        generation++
        isJoined = false
        pendingPackets.clear()
        pendingBytes = 0
        playbackInFlight = false
        AppEventLog.record("BROADCAST_LEAVE")
        onStateChanged("LEFT · OPEN PUBLIC BROADCAST")
    }

    @Synchronized
    fun sendText(text: String): Int {
        check(isJoined) { "Join the open Broadcast room before sending" }
        val frames = codec.encodeText(text)
        val bytes = frames.sumOf { it.size }
        require(pendingPackets.size + frames.size <= 128 && pendingBytes + bytes <= 128 * 1024) {
            "Broadcast transmit queue is full"
        }
        frames.forEach { pendingPackets.addLast(it); pendingBytes += it.size }
        AppEventLog.record("BROADCAST_TX_TEXT",
            "utf8Bytes=" + text.toByteArray(Charsets.UTF_8).size + " frames=" + frames.size)
        onStateChanged("JOINED · queued " + pendingPackets.size + " frame(s)")
        pump()
        return frames.size
    }

    fun onIncomingPayload(payload: ByteArray) {
        if (!isJoined || payload.size < 4 ||
            payload[0] != 0x45.toByte() || payload[1] != 0x43.toByte() ||
            payload[2] != 0x42.toByte() || payload[3] != 0x52.toByte()) return
        try {
            val frame = codec.decode(payload)
            if (frame.senderId.contentEquals(codec.senderId)) return
            val message = assembler.accept(frame) ?: return
            val sender = frame.senderId.take(4).joinToString("") { "%02x".format(it.toInt() and 0xff) }
            AppEventLog.record("BROADCAST_RX_TEXT",
                "sender=" + sender + " utf8Bytes=" + message.toByteArray(Charsets.UTF_8).size)
            onTextReceived(sender, message)
        } catch (failure: Exception) {
            AppEventLog.record("BROADCAST_PACKET_REJECTED", failure.message ?: failure.javaClass.simpleName)
            onError(failure.message ?: "Broadcast packet was rejected")
        }
    }

    @Synchronized fun tick() { assembler.expire(); pump() }
    @Synchronized fun close() = leave()

    private fun senderLabel(): String =
        codec.senderId.take(4).joinToString("") { "%02x".format(it.toInt() and 0xff) }

    @Synchronized
    private fun pump() {
        if (!isJoined || playbackInFlight || pendingPackets.isEmpty()) return
        val packet = pendingPackets.peekFirst()
        val frame = try { NativeModem.encode(packet) } catch (failure: Exception) {
            pendingPackets.removeFirst()
            pendingBytes -= packet.size
            AppEventLog.record("BROADCAST_ENCODE_FAILURE", failure.message ?: failure.javaClass.simpleName)
            onError(failure.message ?: "Could not encode Broadcast frame")
            pump()
            return
        }
        val queuedGeneration = generation
        playbackInFlight = true
        val accepted = try {
            enqueueAudio(frame.samples) {
                synchronized(this) {
                    // Ignore a completion callback belonging to a room that has
                    // already been left. It must not consume a new room's packet.
                    if (queuedGeneration != generation) return@synchronized
                    if (pendingPackets.isNotEmpty() && pendingPackets.peekFirst().contentEquals(packet)) {
                        pendingPackets.removeFirst()
                        pendingBytes -= packet.size
                    }
                    playbackInFlight = false
                    if (isJoined) onStateChanged("JOINED · queued " + pendingPackets.size + " frame(s)")
                    pump()
                }
            }
        } catch (failure: Exception) {
            AppEventLog.record("BROADCAST_AUDIO_QUEUE_ERROR", failure.message ?: failure.javaClass.simpleName)
            onError(failure.message ?: "Could not enqueue Broadcast audio")
            false
        }
        if (!accepted) {
            if (queuedGeneration == generation) playbackInFlight = false
            AppEventLog.record("BROADCAST_AUDIO_QUEUE_FULL", "pendingFrames=" + pendingPackets.size)
        }
    }
}
