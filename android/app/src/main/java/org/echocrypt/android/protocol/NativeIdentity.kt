package org.echocrypt.android.protocol

import android.content.Context
import android.security.keystore.KeyGenParameterSpec
import android.security.keystore.KeyProperties
import org.bouncycastle.crypto.params.Ed25519PrivateKeyParameters
import org.bouncycastle.crypto.params.Ed25519PublicKeyParameters
import org.bouncycastle.crypto.params.X25519PrivateKeyParameters
import org.bouncycastle.crypto.signers.Ed25519Signer
import org.json.JSONObject
import java.io.File
import java.nio.ByteBuffer
import java.nio.ByteOrder
import java.security.KeyStore
import java.security.MessageDigest
import java.security.SecureRandom
import javax.crypto.Cipher
import javax.crypto.KeyGenerator
import javax.crypto.SecretKey
import javax.crypto.spec.GCMParameterSpec

/** Raw Ed25519 identity and ephemeral X25519 primitives matching security/identity.py. */
object NativeIdentity {
    private val random = SecureRandom()
    private const val IDENTITY_FORMAT = "ECHOCRYPT-IDENTITY-v1"

    data class PublicIdentity(val publicKey: ByteArray, val fingerprint: String) {
        init {
            require(publicKey.size == 32) { "Ed25519 public key must be 32 bytes" }
            require(fingerprint == sha256(publicKey)) { "Fingerprint mismatch" }
        }

        fun toJson(): String = JSONObject()
            .put("format", IDENTITY_FORMAT)
            .put("public_key", NativeIdentity.run { publicKey.toHex() })
            .put("fingerprint", fingerprint)
            .toString(2)

        companion object {
            fun fromJson(json: String): PublicIdentity {
                val value = JSONObject(json)
                require(value.optString("format") == IDENTITY_FORMAT) { "Unsupported identity format" }
                val key = value.getString("public_key").hexToBytes()
                require(key.size == 32) { "Ed25519 public key must be exactly 32 bytes" }
                val actual = sha256(key)
                require(value.getString("fingerprint").equals(actual, ignoreCase = true)) {
                    "Fingerprint mismatch"
                }
                return PublicIdentity(key, actual)
            }
        }
    }

    class PrivateIdentity internal constructor(seed: ByteArray) {
        private val seedBytes = seed.copyOf()
        val publicIdentity: PublicIdentity by lazy {
            check(seedBytes.any { it.toInt() != 0 }) { "Identity has been destroyed" }
            val public = Ed25519PrivateKeyParameters(seedBytes, 0).generatePublicKey().encoded
            PublicIdentity(public, sha256(public))
        }

        @Synchronized
        fun sign(message: ByteArray): ByteArray {
            check(seedBytes.any { it.toInt() != 0 }) { "Identity has been destroyed" }
            val signingKey = Ed25519PrivateKeyParameters(seedBytes, 0)
            return Ed25519Signer().run {
                init(true, signingKey)
                update(message, 0, message.size)
                generateSignature()
            }
        }

        @Synchronized
        fun destroy() {
            seedBytes.fill(0)
        }
    }

    fun generate(): PrivateIdentity {
        val seed = ByteArray(32).also(random::nextBytes)
        return PrivateIdentity(seed).also { seed.fill(0) }
    }

    fun verify(publicKey: ByteArray, message: ByteArray, signature: ByteArray): Boolean {
        require(publicKey.size == 32) { "Ed25519 public key must be 32 bytes" }
        require(signature.size == 64) { "Ed25519 signature must be 64 bytes" }
        return try {
            Ed25519Signer().run {
                init(false, Ed25519PublicKeyParameters(publicKey, 0))
                update(message, 0, message.size)
                verifySignature(signature)
            }
        } catch (_: IllegalArgumentException) {
            false
        }
    }

    fun x25519KeyPair(): X25519KeyPair {
        val privateKey = X25519PrivateKeyParameters(random)
        return X25519KeyPair(privateKey.encoded, privateKey.generatePublicKey().encoded)
    }

    data class X25519KeyPair internal constructor(
        private val privateBytes: ByteArray,
        val publicKey: ByteArray
    ) {
        fun agree(peerPublicKey: ByteArray): ByteArray {
            require(peerPublicKey.size == 32) { "X25519 public key must be 32 bytes" }
            val output = ByteArray(32)
            X25519PrivateKeyParameters(privateBytes, 0)
                .generateSecret(X25519PrivateKeyParameters(peerPublicKey, 0), output, 0)
            require(output.any { it.toInt() != 0 }) { "Invalid all-zero X25519 shared secret" }
            return output
        }

        fun destroy() = privateBytes.fill(0)
    }

    /** SHA-256 of the raw 32-byte Ed25519 public key, lowercase hex. */
    fun sha256(bytes: ByteArray): String =
        MessageDigest.getInstance("SHA-256").digest(bytes).toHex()

    private fun ByteArray.toHex(): String = joinToString("") { "%02x".format(it) }

    private fun String.hexToBytes(): ByteArray {
        require(length % 2 == 0 && all { it in "0123456789abcdefABCDEF" }) {
            "Malformed hexadecimal identity key"
        }
        return ByteArray(length / 2) { index ->
            substring(index * 2, index * 2 + 2).toInt(16).toByte()
        }
    }

    /**
     * Encrypted-at-rest identity vault. The Ed25519 seed is AES-GCM wrapped by
     * a non-exportable Android Keystore key; only public identity is exportable.
     */
    class Vault(context: Context) {
        private val appContext = context.applicationContext
        private val identityFile = File(appContext.filesDir, "identity.ed25519.enc")
        private val keyAlias = "org.echocrypt.identity.wrap.v1"
        private val lock = Any()

        fun exists(): Boolean = synchronized(lock) { identityFile.exists() }

        fun create(): PrivateIdentity = synchronized(lock) {
            check(!identityFile.exists()) { "An identity already exists; refusing to overwrite it" }
            val seed = ByteArray(32).also(random::nextBytes)
            try {
                persist(seed)
                PrivateIdentity(seed)
            } finally {
                seed.fill(0)
            }
        }

        fun load(): PrivateIdentity = synchronized(lock) {
            check(identityFile.exists()) { "No local identity exists" }
            val wrapped = identityFile.readBytes()
            require(wrapped.size >= 12 + 16 + 32) { "Encrypted identity file is truncated" }
            val iv = wrapped.copyOfRange(0, 12)
            val ciphertext = wrapped.copyOfRange(12, wrapped.size)
            val seed = cipher(Cipher.DECRYPT_MODE, getOrCreateWrappingKey(), iv)
                .doFinal(ciphertext)
            require(seed.size == 32) { "Invalid decrypted identity seed" }
            try {
                PrivateIdentity(seed)
            } finally {
                seed.fill(0)
                wrapped.fill(0)
            }
        }

        fun exportPublicIdentity(): String = load().useIdentity { it.publicIdentity.toJson() }

        private fun persist(seed: ByteArray) {
            val wrappingKey = getOrCreateWrappingKey()
            val encryptor = Cipher.getInstance("AES/GCM/NoPadding")
            encryptor.init(Cipher.ENCRYPT_MODE, wrappingKey)
            val ciphertext = encryptor.doFinal(seed)
            val encoded = encryptor.iv + ciphertext
            val temp = File(appContext.filesDir, "identity.ed25519.enc.tmp")
            temp.writeBytes(encoded)
            check(temp.renameTo(identityFile)) { "Could not atomically store identity" }
            encoded.fill(0)
            ciphertext.fill(0)
        }

        private fun getOrCreateWrappingKey(): SecretKey {
            val store = KeyStore.getInstance("AndroidKeyStore").apply { load(null) }
            (store.getKey(keyAlias, null) as? SecretKey)?.let { return it }
            val generator = KeyGenerator.getInstance(KeyProperties.KEY_ALGORITHM_AES, "AndroidKeyStore")
            generator.init(
                KeyGenParameterSpec.Builder(
                    keyAlias,
                    KeyProperties.PURPOSE_ENCRYPT or KeyProperties.PURPOSE_DECRYPT
                )
                    .setBlockModes(KeyProperties.BLOCK_MODE_GCM)
                    .setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE)
                    .setKeySize(256)
                    .setRandomizedEncryptionRequired(true)
                    .build()
            )
            return generator.generateKey()
        }

        private fun cipher(mode: Int, key: SecretKey, iv: ByteArray): Cipher =
            Cipher.getInstance("AES/GCM/NoPadding").apply {
                init(mode, key, GCMParameterSpec(128, iv))
            }
    }

    private inline fun <T> PrivateIdentity.useIdentity(block: (PrivateIdentity) -> T): T =
        try { block(this) } finally { destroy() }
}
