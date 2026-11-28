package org.echocrypt.android.protocol

import org.bouncycastle.jce.provider.BouncyCastleProvider
import java.nio.ByteBuffer
import java.nio.ByteOrder
import java.security.MessageDigest
import java.security.Provider
import java.security.SecureRandom
import javax.crypto.Cipher
import javax.crypto.Mac
import javax.crypto.spec.IvParameterSpec
import javax.crypto.spec.SecretKeySpec

/**
 * Cryptographic primitives and ECP1 secure-channel framing compatible with
 * core/crypto.py. Keys are directional: never reuse one key for both directions.
 */
object NativeCrypto {
    private val provider: Provider by lazy { BouncyCastleProvider() }
    private val random = SecureRandom()

    const val HEADER_SIZE = 16
    const val TAG_SIZE = 16
    const val REPLAY_WINDOW_SIZE = 1024
    private val MAGIC = byteArrayOf(0x45, 0x43, 0x50, 0x31) // ECP1
    private val PADDING_MAGIC = byteArrayOf(0x45, 0x43, 0x50, 0x44) // ECPD
    private val ALLOWED_PADDING = setOf(0, 32, 64, 128, 256)

    /** RFC 5869 HKDF-SHA256. */
    fun hkdfSha256(ikm: ByteArray, salt: ByteArray, info: ByteArray, length: Int): ByteArray {
        require(length in 1..(255 * 32)) { "HKDF output length is invalid" }
        val actualSalt = if (salt.isEmpty()) ByteArray(32) else salt
        val extract = Mac.getInstance("HmacSHA256").run {
            init(SecretKeySpec(actualSalt, "HmacSHA256"))
            doFinal(ikm)
        }
        val output = ByteArray(length)
        var previous = ByteArray(0)
        var offset = 0
        var counter = 1
        while (offset < length) {
            val mac = Mac.getInstance("HmacSHA256")
            mac.init(SecretKeySpec(extract, "HmacSHA256"))
            mac.update(previous)
            mac.update(info)
            mac.update(counter.toByte())
            previous = mac.doFinal()
            val count = minOf(previous.size, length - offset)
            previous.copyInto(output, offset, 0, count)
            offset += count
            counter++
        }
        extract.fill(0)
        previous.fill(0)
        return output
    }

    /** Stateless ChaCha20-Poly1305 operation, useful for handshake key confirmation. */
    fun chacha20Poly1305Encrypt(key: ByteArray, nonce: ByteArray, plaintext: ByteArray, aad: ByteArray): ByteArray {
        require(key.size == 32) { "ChaCha20-Poly1305 key must be 32 bytes" }
        require(nonce.size == 12) { "ChaCha20-Poly1305 nonce must be 12 bytes" }
        val cipher = Cipher.getInstance("ChaCha20-Poly1305", provider)
        cipher.init(Cipher.ENCRYPT_MODE, SecretKeySpec(key, "ChaCha20"), IvParameterSpec(nonce))
        cipher.updateAAD(aad)
        return cipher.doFinal(plaintext)
    }

    fun chacha20Poly1305Decrypt(key: ByteArray, nonce: ByteArray, ciphertext: ByteArray, aad: ByteArray): ByteArray {
        require(key.size == 32) { "ChaCha20-Poly1305 key must be 32 bytes" }
        require(nonce.size == 12) { "ChaCha20-Poly1305 nonce must be 12 bytes" }
        val cipher = Cipher.getInstance("ChaCha20-Poly1305", provider)
        cipher.init(Cipher.DECRYPT_MODE, SecretKeySpec(key, "ChaCha20"), IvParameterSpec(nonce))
        cipher.updateAAD(aad)
        return cipher.doFinal(ciphertext)
    }

    class SecureChannel(
        key: ByteArray,
        private val replayWindow: ReplayWindow = ReplayWindow(REPLAY_WINDOW_SIZE),
        private val paddingBlock: Int = 0
    ) {
        private val keyBytes = key.copyOf()
        private val txPrefix = ByteArray(4).also(random::nextBytes)
        private var txCounter = 0L
        private var rxPrefix: ByteArray? = null

        init {
            require(keyBytes.size == 32) { "Key must be exactly 32 bytes" }
            require(paddingBlock in ALLOWED_PADDING) { "Unsupported padding block" }
        }

        @Synchronized
        fun encryptedSize(plaintextSize: Int): Int {
            require(plaintextSize >= 0)
            var protectedSize = plaintextSize
            if (paddingBlock != 0) {
                protectedSize += 8 // ECPD + uint32 original length
                protectedSize = ((protectedSize + paddingBlock - 1) / paddingBlock) * paddingBlock
            }
            return HEADER_SIZE + protectedSize + TAG_SIZE
        }

        @Synchronized
        fun encrypt(plaintext: ByteArray, aad: ByteArray = ByteArray(0)): ByteArray {
            check(txCounter >= 0L && txCounter < Long.MAX_VALUE) { "Rekey required before sequence overflow" }
            val sequence = txCounter++
            val header = ByteBuffer.allocate(HEADER_SIZE).order(ByteOrder.BIG_ENDIAN)
                .put(MAGIC).put(txPrefix).putLong(sequence).array()
            val protected = pad(plaintext)
            val nonce = txPrefix + ByteBuffer.allocate(8).order(ByteOrder.BIG_ENDIAN).putLong(sequence).array()
            val ciphertext = chacha20Poly1305Encrypt(keyBytes, nonce, protected, header + aad)
            return header + ciphertext
        }

        @Synchronized
        fun decrypt(packet: ByteArray, aad: ByteArray = ByteArray(0)): ByteArray {
            require(packet.size >= HEADER_SIZE + TAG_SIZE) { "Truncated secure packet" }
            val header = packet.copyOfRange(0, HEADER_SIZE)
            require(header.copyOfRange(0, 4).contentEquals(MAGIC)) { "Bad secure packet magic" }
            val prefix = header.copyOfRange(4, 8)
            val pinned = rxPrefix
            require(pinned == null || MessageDigest.isEqual(pinned, prefix)) {
                "Unexpected nonce prefix for this receive channel"
            }
            val sequence = ByteBuffer.wrap(header, 8, 8).order(ByteOrder.BIG_ENDIAN).long
            require(replayWindow.wouldAccept(sequence)) { "Replay or stale secure packet" }
            val nonce = prefix + ByteBuffer.allocate(8).order(ByteOrder.BIG_ENDIAN).putLong(sequence).array()
            // Authentication must succeed before prefix/replay state is committed.
            val plaintext = chacha20Poly1305Decrypt(
                keyBytes, nonce, packet.copyOfRange(HEADER_SIZE, packet.size), header + aad
            )
            val unpadded = unpad(plaintext)
            check(replayWindow.acceptAuthenticated(sequence)) { "Replay state changed during packet authentication" }
            if (pinned == null) rxPrefix = prefix
            return unpadded
        }

        private fun pad(input: ByteArray): ByteArray {
            if (paddingBlock == 0) return input
            val protectedSize = encryptedSize(input.size) - HEADER_SIZE - TAG_SIZE
            val header = ByteBuffer.allocate(8).order(ByteOrder.BIG_ENDIAN)
                .put(PADDING_MAGIC).putInt(input.size).array()
            val result = ByteArray(protectedSize)
            header.copyInto(result)
            input.copyInto(result, 8)
            val padding = ByteArray(protectedSize - 8 - input.size)
            if (padding.isNotEmpty()) random.nextBytes(padding)
            padding.copyInto(result, 8 + input.size)
            return result
        }

        private fun unpad(input: ByteArray): ByteArray {
            if (paddingBlock == 0) return input
            require(input.size >= 8 && input.size % paddingBlock == 0) { "Malformed padding envelope" }
            require(input.copyOfRange(0, 4).contentEquals(PADDING_MAGIC)) { "Invalid padding envelope" }
            val length = ByteBuffer.wrap(input, 4, 4).order(ByteOrder.BIG_ENDIAN).int
            require(length >= 0 && length <= input.size - 8) { "Invalid padded payload length" }
            return input.copyOfRange(8, 8 + length)
        }

        @Synchronized
        fun destroy() {
            keyBytes.fill(0)
            txPrefix.fill(0)
            rxPrefix?.fill(0)
            rxPrefix = null
            replayWindow.reset()
            txCounter = Long.MAX_VALUE
        }
    }
}
