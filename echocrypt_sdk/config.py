"""Validated public configuration for the acoustic SDK."""
from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Optional

from core.config import Config as _CoreConfig
from core.config import _validate as _validate_core_config


_SUPPORTED_FEC = frozenset({"conv", "hamming", "repetition3", "raw"})


@dataclass(frozen=True)
class AcousticConfig:
    """Immutable settings for one acoustic modem endpoint.

    symbol_rates controls the receive decoder's supported rates. The transmit
    rate can be selected per call, provided it is valid for the configured
    sample rate. channel_access="broadcast" selects the shorter randomized
    listen-before-talk profile; it does not enable the public Broadcast
    messaging protocol.
    """

    sample_rate: int = 48000
    symbol_rate: int = 200
    mark_hz: int = 1800
    space_hz: int = 2400
    input_device: Optional[int] = None
    output_device: Optional[int] = None
    gain: float = 0.35
    max_packet_bytes: int = 1024
    symbol_rates: Optional[tuple[int, ...]] = None
    default_fec_mode: str = "conv"
    channel_access: object = True
    inter_frame_gap: float = 0.20

    def __post_init__(self) -> None:
        integer_fields = (
            "sample_rate", "symbol_rate", "mark_hz", "space_hz",
            "max_packet_bytes",
        )
        for name in integer_fields:
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError(f"{name} must be an integer")

        for name in ("input_device", "output_device"):
            value = getattr(self, name)
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, int) or value < 0
            ):
                raise ValueError(f"{name} must be a non-negative device index or None")

        if self.symbol_rates is None:
            rates = (self.symbol_rate,)
        else:
            try:
                rates = tuple(self.symbol_rates)
            except TypeError as exc:
                raise TypeError("symbol_rates must be an iterable of positive integers") from exc
            if not rates:
                raise ValueError("symbol_rates cannot be empty")
            if any(isinstance(rate, bool) or not isinstance(rate, int) for rate in rates):
                raise TypeError("Every symbol rate must be an integer")
            rates = tuple(dict.fromkeys(rates))
        if any(rate <= 0 for rate in rates):
            raise ValueError("Every symbol rate must be positive")
        object.__setattr__(self, "symbol_rates", rates)

        if not isinstance(self.default_fec_mode, str) or self.default_fec_mode not in _SUPPORTED_FEC:
            raise ValueError(
                "default_fec_mode must be one of: " + ", ".join(sorted(_SUPPORTED_FEC))
            )
        if self.channel_access is not True and self.channel_access is not False \
                and self.channel_access != "broadcast":
            raise ValueError("channel_access must be True, False, or 'broadcast'")
        if isinstance(self.inter_frame_gap, bool) or not isinstance(
            self.inter_frame_gap, (int, float)
        ):
            raise TypeError("inter_frame_gap must be a number")
        gap = float(self.inter_frame_gap)
        if not math.isfinite(gap) or not 0 <= gap <= 10:
            raise ValueError("inter_frame_gap must be finite and between 0 and 10 seconds")
        object.__setattr__(self, "inter_frame_gap", gap)

        core = self.to_core_config()
        _validate_core_config(core)
        for rate in rates:
            if self.sample_rate % rate or self.sample_rate // rate < 32:
                raise ValueError(
                    f"symbol rate {rate} must divide sample_rate and leave at least "
                    "32 samples per symbol"
                )

    def to_core_config(self) -> _CoreConfig:
        """Build the validated internal configuration consumed by the modem."""
        return _CoreConfig(
            sample_rate=self.sample_rate,
            symbol_rate=self.symbol_rate,
            mark_hz=self.mark_hz,
            space_hz=self.space_hz,
            input_device=self.input_device,
            output_device=self.output_device,
            gain=float(self.gain),
            max_payload=self.max_packet_bytes,
            # These legacy slots are unused by this public client. Keep them
            # valid for the internal Config validator for custom sample rates.
            fast_symbol_rate=self.symbol_rate,
            voice_symbol_rate=self.symbol_rate,
            emergency_symbol_rate=self.symbol_rate,
        )
