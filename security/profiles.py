"""Authenticated Direct-session security/transport profiles.

All profiles retain the same Ed25519 identity signatures, ephemeral X25519
key exchange, HKDF-SHA256 and ChaCha20-Poly1305 encryption. Profiles tune
traffic-shape padding, keyed acoustic hopping, PHY speed and retry policy.
"""
from dataclasses import dataclass


_ALLOWED_PADDING = (0, 32, 64, 128, 256)
_PROFILE_IDS = {"low": 1, "medium": 2, "secure": 3, "custom": 4}


class SecurityProfileError(ValueError):
    """Invalid or incompatible Direct security profile."""


@dataclass(frozen=True)
class SecurityProfile:
    name: str
    profile_id: int
    symbol_rate: int
    padding_block: int
    frequency_hopping: bool
    retries: int

    def __post_init__(self):
        if self.name not in _PROFILE_IDS or _PROFILE_IDS[self.name] != self.profile_id:
            raise SecurityProfileError("Unknown Direct security profile")
        if isinstance(self.symbol_rate, bool) or not isinstance(self.symbol_rate, int) or not 50 <= self.symbol_rate <= 3000:
            raise SecurityProfileError("Symbol rate must be an integer between 50 and 3000 baud")
        if self.padding_block not in _ALLOWED_PADDING:
            raise SecurityProfileError(f"Padding block must be one of {_ALLOWED_PADDING}")
        if not isinstance(self.frequency_hopping, bool):
            raise SecurityProfileError("frequency_hopping must be boolean")
        if isinstance(self.retries, bool) or not isinstance(self.retries, int) or not 0 <= self.retries <= 10:
            raise SecurityProfileError("Retry count must be between 0 and 10")

    @classmethod
    def preset(cls, name="medium"):
        key = str(name).strip().casefold()
        presets = {
            "low": cls("low", 1, 800, 0, False, 1),
            "medium": cls("medium", 2, 400, 64, True, 3),
            "secure": cls("secure", 3, 200, 256, True, 5),
        }
        try:
            return presets[key]
        except KeyError as exc:
            raise SecurityProfileError("Profile must be low, medium or secure") from exc

    @classmethod
    def custom(cls, symbol_rate=400, padding_block=128,
               frequency_hopping=True, retries=3):
        return cls("custom", 4, int(symbol_rate), int(padding_block),
                   bool(frequency_hopping), int(retries))

    @classmethod
    def from_value(cls, value):
        if value is None:
            return cls.preset("medium")
        if isinstance(value, cls):
            return value
        if isinstance(value, str):
            if value.casefold() == "custom":
                return cls.custom()
            return cls.preset(value)
        raise TypeError("security_profile must be a profile name or SecurityProfile")

    def validate_for_sample_rate(self, sample_rate):
        if (isinstance(sample_rate, bool) or not isinstance(sample_rate, int)
                or sample_rate <= 0 or sample_rate % self.symbol_rate
                or sample_rate // self.symbol_rate < 32):
            raise SecurityProfileError(
                f"{self.symbol_rate} baud is invalid for a {sample_rate} Hz sample rate"
            )
        return self

    def wire_fields(self):
        return (
            self.profile_id, self.symbol_rate, self.padding_block,
            1 if self.frequency_hopping else 0, self.retries,
        )

    @classmethod
    def from_wire(cls, profile_id, symbol_rate, padding_block, flags, retries):
        if flags & ~1:
            raise SecurityProfileError("Unsupported security-profile flags")
        hopping = bool(flags & 1)
        try:
            profile_id = int(profile_id)
            values = (int(symbol_rate), int(padding_block), hopping, int(retries))
        except (TypeError, ValueError) as exc:
            raise SecurityProfileError("Malformed security-profile descriptor") from exc
        if profile_id == 4:
            return cls.custom(*values)
        names = {1: "low", 2: "medium", 3: "secure"}
        try:
            preset = cls.preset(names[profile_id])
        except KeyError as exc:
            raise SecurityProfileError("Unsupported security-profile ID") from exc
        if values != (preset.symbol_rate, preset.padding_block,
                      preset.frequency_hopping, preset.retries):
            raise SecurityProfileError("Preset security-profile parameters were modified")
        return preset

    @staticmethod
    def negotiate(local, remote):
        local = SecurityProfile.from_value(local)
        remote = SecurityProfile.from_value(remote)
        if local.name == "custom" or remote.name == "custom":
            if local != remote:
                raise SecurityProfileError(
                    "Custom security settings must match exactly on both devices"
                )
            return local
        rank = {"low": 1, "medium": 2, "secure": 3}
        return local if rank[local.name] >= rank[remote.name] else remote
