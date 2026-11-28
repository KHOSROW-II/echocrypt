package org.echocrypt.android

import org.echocrypt.android.protocol.NativeIdentity
import org.junit.Assert.*
import org.junit.Test

class NativeIdentityTest {
    @Test
    fun ed25519SignaturesVerifyAndRejectTampering() {
        val identity = NativeIdentity.generate()
        val message = "ECHOCRYPT/HELLO/v2".toByteArray()
        val signature = identity.sign(message)
        assertEquals(64, signature.size)
        assertTrue(NativeIdentity.verify(identity.publicIdentity.publicKey, message, signature))
        assertFalse(NativeIdentity.verify(identity.publicIdentity.publicKey, message + 1, signature))
        assertFalse(NativeIdentity.verify(ByteArray(32), message, signature))
    }

    @Test
    fun publicIdentityJsonRoundTripsAndPinsFingerprint() {
        val identity = NativeIdentity.generate()
        val exported = identity.publicIdentity.toJson()
        val imported = NativeIdentity.PublicIdentity.fromJson(exported)
        assertArrayEquals(identity.publicIdentity.publicKey, imported.publicKey)
        assertEquals(identity.publicIdentity.fingerprint, imported.fingerprint)
        val altered = exported.replace(imported.fingerprint, "00".repeat(32))
        try {
            NativeIdentity.PublicIdentity.fromJson(altered)
            fail("Fingerprint mismatch must be rejected")
        } catch (_: IllegalArgumentException) {
            // Expected.
        }
    }

    @Test
    fun x25519DerivesSameSharedSecretOnBothSides() {
        val alice = NativeIdentity.x25519KeyPair()
        val bob = NativeIdentity.x25519KeyPair()
        assertArrayEquals(alice.agree(bob.publicKey), bob.agree(alice.publicKey))
        alice.destroy()
        bob.destroy()
    }

    @Test
    fun fingerprintsAreSha256OfRawPublicKey() {
        val identity = NativeIdentity.generate()
        assertEquals(64, identity.publicIdentity.fingerprint.length)
        assertEquals(
            identity.publicIdentity.fingerprint,
            NativeIdentity.sha256(identity.publicIdentity.publicKey)
        )
    }
}
