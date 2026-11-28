package org.echocrypt.android.protocol

data class NativeSecurityProfile(
    val name: String,
    val profileId: Int,
    val symbolRate: Int,
    val paddingBlock: Int,
    val frequencyHopping: Boolean,
    val retries: Int
) {
    init {
        require(name in PROFILE_IDS && PROFILE_IDS[name] == profileId) { "Unknown security profile" }
        require(symbolRate in 50..3000) { "Symbol rate must be between 50 and 3000 baud" }
        require(paddingBlock in ALLOWED_PADDING) { "Unsupported padding block" }
        require(retries in 0..10) { "Retry count must be between 0 and 10" }
    }

    fun wireFields() = listOf(
        profileId, symbolRate, paddingBlock,
        if (frequencyHopping) 1 else 0, retries
    )

    companion object {
        private val PROFILE_IDS = mapOf("low" to 1, "medium" to 2, "secure" to 3, "custom" to 4)
        private val ALLOWED_PADDING = setOf(0, 32, 64, 128, 256)

        fun preset(name: String = "medium"): NativeSecurityProfile = when (name.lowercase()) {
            "low" -> NativeSecurityProfile("low", 1, 800, 0, false, 1)
            "medium" -> NativeSecurityProfile("medium", 2, 400, 64, true, 3)
            "secure" -> NativeSecurityProfile("secure", 3, 200, 256, true, 5)
            else -> throw IllegalArgumentException("Profile must be low, medium or secure")
        }

        fun custom(symbolRate: Int = 400, paddingBlock: Int = 128,
                   frequencyHopping: Boolean = true, retries: Int = 3) =
            NativeSecurityProfile("custom", 4, symbolRate, paddingBlock, frequencyHopping, retries)

        fun fromWire(id: Int, symbolRate: Int, paddingBlock: Int, flags: Int, retries: Int): NativeSecurityProfile {
            require((flags and 0xfe) == 0) { "Unsupported profile flags" }
            val hopping = flags and 1 != 0
            if (id == 4) return custom(symbolRate, paddingBlock, hopping, retries)
            val expected = when (id) {
                1 -> preset("low")
                2 -> preset("medium")
                3 -> preset("secure")
                else -> throw IllegalArgumentException("Unsupported security profile ID")
            }
            require(
                symbolRate == expected.symbolRate &&
                    paddingBlock == expected.paddingBlock &&
                    hopping == expected.frequencyHopping &&
                    retries == expected.retries
            ) { "Preset security-profile parameters were modified" }
            return expected
        }

        fun negotiate(local: NativeSecurityProfile, remote: NativeSecurityProfile): NativeSecurityProfile {
            if (local.name == "custom" || remote.name == "custom") {
                require(local == remote) { "Custom security settings must match exactly on both devices" }
                return local
            }
            val rank = mapOf("low" to 1, "medium" to 2, "secure" to 3)
            return if (rank.getValue(local.name) >= rank.getValue(remote.name)) local else remote
        }
    }
}
