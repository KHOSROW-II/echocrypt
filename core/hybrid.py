"""Experimental simultaneous two-band FDM waveform for text + voice/data.

The two independent BFSK streams occupy separate audio frequency bands and are
summed into one waveform. This is a lab feature: test it on the actual audio
path before relying on it; physical band limiting and AGC may cause crosstalk.
"""
from __future__ import annotations

import numpy as np

from core.modem import modulate, demodulate

# Separated bands; the voice/data lane is intentionally low-bandwidth.
DEFAULT_TEXT_TONES = (1800, 2400)
DEFAULT_MEDIA_TONES = (600, 1000)


def modulate_hybrid(text_payload: bytes | None, media_payload: bytes | None,
                    sample_rate: int = 48000, symbol_rate: int = 200,
                    text_tones=DEFAULT_TEXT_TONES,
                    media_tones=DEFAULT_MEDIA_TONES,
                    gain: float = 0.24, max_payload: int = 1024,
                    fec_mode: str = "hamming") -> np.ndarray:
    """Create one FDM waveform carrying both independent payloads at once."""
    if text_payload is None and media_payload is None:
        raise ValueError("At least one FDM lane must contain a payload")
    waves = []
    if text_payload is not None:
        waves.append(modulate(text_payload, sample_rate, symbol_rate,
                              text_tones[0], text_tones[1], gain,
                              max_payload, fec_mode=fec_mode, hop_key=None))
    if media_payload is not None:
        waves.append(modulate(media_payload, sample_rate, symbol_rate,
                              media_tones[0], media_tones[1], gain,
                              max_payload, fec_mode=fec_mode, hop_key=None))
    length = max(map(len, waves))
    composite = np.zeros(length, dtype=np.float32)
    for wave in waves:
        composite[:len(wave)] += wave
    peak = float(np.max(np.abs(composite))) if composite.size else 0.0
    if peak > 0.95:
        composite *= 0.95 / peak
    return composite


def demodulate_hybrid(samples, sample_rate: int = 48000, symbol_rate: int = 200,
                      text_tones=DEFAULT_TEXT_TONES,
                      media_tones=DEFAULT_MEDIA_TONES,
                      max_payload: int = 1024,
                      expect_text: bool = True,
                      expect_media: bool = True) -> tuple[bytes | None, bytes | None]:
    """Decode one or both FDM lanes from a composite recording."""
    if not expect_text and not expect_media:
        raise ValueError("At least one FDM lane must be selected")
    text = demodulate(samples, sample_rate, symbol_rate, *text_tones, max_payload) if expect_text else None
    media = demodulate(samples, sample_rate, symbol_rate, *media_tones, max_payload) if expect_media else None
    return text, media
