package org.echocrypt.android

import org.echocrypt.android.protocol.NativeIdentity
import org.echocrypt.android.protocol.NativePeerHandshake
import org.echocrypt.android.protocol.NativeSecurityProfile
import org.junit.Assert.*
import org.junit.Test

class NativePeerHandshakeTest {
    private fun peers(
        aliceProfile: NativeSecurityProfile = NativeSecurityProfile.preset("medium"),
        bobProfile: NativeSecurityProfile = NativeSecurityProfile.preset("medium"),
        aliceUltrasonic: Boolean = false,
        bobUltrasonic: Boolean = false,
        aliceWorkers: Int = 1,
        bobWorkers: Int = 1
    ): Pair<NativePeerHandshake, NativePeerHandshake> {
        val aliceIdentity = NativeIdentity.generate()
        val bobIdentity = NativeIdentity.generate()
        val alice = NativePeerHandshake(
            NativePeerHandshake.Role.INITIATOR, aliceIdentity, bobIdentity.publicIdentity,
            aliceProfile, aliceUltrasonic, aliceWorkers
        )
        val bob = NativePeerHandshake(
            NativePeerHandshake.Role.RESPONDER, bobIdentity, aliceIdentity.publicIdentity,
            bobProfile, bobUltrasonic, bobWorkers
        )
        return alice to bob
    }

    private fun establish(alice: NativePeerHandshake, bob: NativePeerHandshake) {
        val helloI = alice.start()
        assertEquals(125, helloI.size)
        val helloR = bob.receive(helloI)!!
        assertEquals(125, helloR.size)
        val finishI = alice.receive(helloR)!!
        val finishR = bob.receive(finishI)!!
        assertNull(alice.receive(finishR))
        assertTrue(alice.established)
        assertTrue(bob.established)
    }

    @Test
    fun fullHandshakeAndBidirectionalEncryptedText() {
        val (alice, bob) = peers()
        establish(alice, bob)
        val message = "ECHOCRYPT secure text — سلام"
        val encrypted = alice.sendText(message)
        assertFalse(encrypted.toString(Charsets.ISO_8859_1).contains(message))
        assertEquals(message, bob.receiveText(encrypted))
        val reply = bob.sendText("reply")
        assertEquals("reply", alice.receiveText(reply))
    }

    @Test
    fun handshakeRetriesReturnExactPreviousFlight() {
        val (alice, bob) = peers()
        val helloI = alice.start()
        val helloR = bob.receive(helloI)!!
        assertArrayEquals(helloR, bob.receive(helloI))
        val finishI = alice.receive(helloR)!!
        assertArrayEquals(finishI, alice.receive(helloR))
        val finishR = bob.receive(finishI)!!
        assertNull(alice.receive(finishR))
        assertArrayEquals(finishR, bob.receive(finishI))
        assertNull(alice.receive(finishR))
    }

    @Test
    fun strongestPresetIsNegotiatedAndPaddingApplied() {
        val (alice, bob) = peers(
            aliceProfile = NativeSecurityProfile.preset("low"),
            bobProfile = NativeSecurityProfile.preset("secure")
        )
        establish(alice, bob)
        assertEquals(NativeSecurityProfile.preset("secure"), alice.negotiatedProfile)
        assertEquals(NativeSecurityProfile.preset("secure"), bob.negotiatedProfile)
        assertEquals(256, alice.session!!.securityProfile.paddingBlock)
        assertNotNull(alice.session!!.hoppingKey)
    }

    @Test
    fun ultrasonicAndWorkerCapabilitiesAreIntersected() {
        val (alice, bob) = peers(
            aliceUltrasonic = true, bobUltrasonic = true,
            aliceWorkers = 6, bobWorkers = 3
        )
        establish(alice, bob)
        assertTrue(alice.negotiatedUltrasonicEnabled)
        assertTrue(bob.negotiatedUltrasonicEnabled)
        assertEquals(3, alice.negotiatedWorkerCount)
        assertEquals(3, bob.negotiatedWorkerCount)
    }

    @Test
    fun capabilityIsDisabledUnlessBothPeersAdvertiseIt() {
        val (alice, bob) = peers(aliceUltrasonic = true, bobUltrasonic = false, aliceWorkers = 5)
        establish(alice, bob)
        assertFalse(alice.negotiatedUltrasonicEnabled)
        assertFalse(bob.negotiatedUltrasonicEnabled)
        assertEquals(1, alice.negotiatedWorkerCount)
    }

    @Test
    fun modifiedHelloSignatureIsRejected() {
        val (alice, bob) = peers()
        val damaged = alice.start().also { it[it.lastIndex] = (it.last().toInt() xor 1).toByte() }
        try {
            bob.receive(damaged)
            fail("Modified signature must be rejected")
        } catch (_: IllegalArgumentException) {
            // Expected.
        }
    }

    @Test
    fun wrongPeerIdentityCannotCompleteHandshake() {
        val aliceIdentity = NativeIdentity.generate()
        val bobIdentity = NativeIdentity.generate()
        val stranger = NativeIdentity.generate()
        val alice = NativePeerHandshake(
            NativePeerHandshake.Role.INITIATOR, aliceIdentity, bobIdentity.publicIdentity
        )
        val wrongBob = NativePeerHandshake(
            NativePeerHandshake.Role.RESPONDER, bobIdentity, stranger.publicIdentity
        )
        try {
            wrongBob.receive(alice.start())
            fail("Pinned identity mismatch must reject the handshake")
        } catch (_: IllegalArgumentException) {
            // Expected.
        }
    }

    @Test
    fun customProfilesMustMatchExactly() {
        val (alice, bob) = peers(
            aliceProfile = NativeSecurityProfile.custom(400, 64, true, 3),
            bobProfile = NativeSecurityProfile.custom(400, 128, true, 3)
        )
        try {
            bob.receive(alice.start())
            fail("Mismatched custom profiles must be rejected")
        } catch (_: IllegalArgumentException) {
            // Expected.
        }
    }

    @Test
    fun oversizedTextDoesNotConsumeTrafficSequence() {
        val (alice, bob) = peers()
        establish(alice, bob)
        
        try {
            alice.sendText("A".repeat(300), maxPacketSize = 64)
            fail("Oversized text must be rejected")
        } catch (_: IllegalArgumentException) {
            // Expected.
        }
        assertEquals("ok", bob.receiveText(alice.sendText("ok")))
    }
}
