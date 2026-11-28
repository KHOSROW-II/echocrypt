package org.echocrypt.android.protocol

import android.content.Context
import android.util.AtomicFile
import org.json.JSONArray
import org.json.JSONObject
import java.io.File

/**
 * App-private persistence for public peer identities.
 *
 * Import is deliberately gated by a separately supplied fingerprint. This is
 * an out-of-band trust decision, not a claim that a JSON file is authentic.
 */
class TrustedPeersStore(context: Context) {
    data class TrustedPeer(val name: String, val identity: NativeIdentity.PublicIdentity)

    private val file = AtomicFile(File(context.applicationContext.filesDir, "trusted-peers.json"))
    private val lock = Any()

    fun list(): List<TrustedPeer> = synchronized(lock) { readUnlocked() }

    fun import(json: String, displayName: String, confirmedFingerprint: String): TrustedPeer =
        synchronized(lock) {
            val name = displayName.trim()
            require(name.length in 1..32 && name.none { it.isISOControl() }) {
                "Peer name must be 1–32 printable characters"
            }
            val identity = NativeIdentity.PublicIdentity.fromJson(json)
            val confirmation = confirmedFingerprint
                .filterNot { it == ':' || it == ' ' }
                .lowercase()
            require(confirmation.matches(Regex("[0-9a-f]{64}")) &&
                confirmation == identity.fingerprint) {
                "Fingerprint does not match. Verify the fingerprint with the peer through a separate channel."
            }
            val peers = readUnlocked().toMutableList()
            require(peers.none { it.identity.fingerprint == identity.fingerprint }) {
                "This peer is already trusted"
            }
            val peer = TrustedPeer(name, identity)
            peers += peer
            writeUnlocked(peers)
            peer
        }

    fun remove(fingerprint: String) = synchronized(lock) {
        val peers = readUnlocked()
        val filtered = peers.filterNot { it.identity.fingerprint == fingerprint }
        require(filtered.size != peers.size) { "Trusted peer was not found" }
        writeUnlocked(filtered)
    }

    private fun readUnlocked(): List<TrustedPeer> {
        if (!file.baseFile.exists()) return emptyList()
        val raw = file.openRead().bufferedReader(Charsets.UTF_8).use { it.readText() }
        val root = JSONObject(raw)
        require(root.getString("format") == FORMAT) { "Unsupported trusted peers file" }
        val array = root.getJSONArray("peers")
        val result = ArrayList<TrustedPeer>(array.length())
        val seen = HashSet<String>()
        for (index in 0 until array.length()) {
            val item = array.getJSONObject(index)
            val name = item.getString("name")
            val identity = NativeIdentity.PublicIdentity.fromJson(item.getString("identity"))
            require(name.length in 1..32 && name.none { it.isISOControl() }) { "Invalid stored peer name" }
            require(seen.add(identity.fingerprint)) { "Duplicate trusted peer fingerprint" }
            result += TrustedPeer(name, identity)
        }
        return result
    }

    private fun writeUnlocked(peers: List<TrustedPeer>) {
        val array = JSONArray()
        peers.forEach { peer ->
            array.put(JSONObject()
                .put("name", peer.name)
                .put("identity", peer.identity.toJson()))
        }
        val bytes = JSONObject()
            .put("format", FORMAT)
            .put("peers", array)
            .toString(2)
            .toByteArray(Charsets.UTF_8)
        var stream: java.io.FileOutputStream? = null
        try {
            stream = file.startWrite()
            stream.write(bytes)
            file.finishWrite(stream)
        } catch (error: Exception) {
            if (stream != null) file.failWrite(stream)
            throw error
        } finally {
            bytes.fill(0)
        }
    }

    companion object {
        private const val FORMAT = "ECHOCRYPT-TRUSTED-PEERS-v1"
    }
}
