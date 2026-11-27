from __future__ import annotations

import numpy as np
import pytest

from core import hybrid


def test_hybrid_requires_at_least_one_payload():
    with pytest.raises(ValueError, match="At least one FDM lane"):
        hybrid.modulate_hybrid(None, None)


def test_hybrid_single_lane_preserves_waveform_and_parameters(monkeypatch):
    captured = []
    expected = np.array([0.1, -0.2, 0.3], dtype=np.float32)

    def fake_modulate(payload, sample_rate, symbol_rate, mark, space, gain, max_payload, **kwargs):
        captured.append((payload, sample_rate, symbol_rate, mark, space, gain, max_payload, kwargs))
        return expected.copy()

    monkeypatch.setattr(hybrid, "modulate", fake_modulate)
    actual = hybrid.modulate_hybrid(b"text", None, sample_rate=44100, symbol_rate=300)
    np.testing.assert_array_equal(actual, expected)
    assert captured == [
        (b"text", 44100, 300, 1800, 2400, 0.24, 1024,
         {"fec_mode": "hamming", "hop_key": None})
    ]


def test_hybrid_sums_unequal_lanes_zero_pads_and_normalizes_peak(monkeypatch):
    calls = []
    waves = {
        b"text": np.array([0.8, 0.8], dtype=np.float32),
        b"media": np.array([0.8], dtype=np.float32),
    }

    def fake_modulate(payload, sample_rate, symbol_rate, mark, space, gain, max_payload, **kwargs):
        calls.append((payload, mark, space))
        return waves[payload]

    monkeypatch.setattr(hybrid, "modulate", fake_modulate)
    actual = hybrid.modulate_hybrid(b"text", b"media")

    assert calls == [(b"text", 1800, 2400), (b"media", 600, 1000)]
    assert actual.dtype == np.float32
    assert actual.shape == (2,)
    np.testing.assert_allclose(actual, np.array([0.95, 0.475], dtype=np.float32), rtol=1e-6)


def test_hybrid_does_not_scale_when_peak_is_at_or_below_limit(monkeypatch):
    monkeypatch.setattr(
        hybrid, "modulate",
        lambda payload, *args, **kwargs: np.array([0.2, -0.3], dtype=np.float32),
    )
    actual = hybrid.modulate_hybrid(b"x", None)
    np.testing.assert_array_equal(actual, np.array([0.2, -0.3], dtype=np.float32))


def test_hybrid_demodulator_only_decodes_selected_lanes(monkeypatch):
    calls = []

    def fake_demodulate(samples, sample_rate, symbol_rate, mark, space, max_payload):
        calls.append((sample_rate, symbol_rate, mark, space, max_payload))
        return f"{mark}:{space}".encode()

    monkeypatch.setattr(hybrid, "demodulate", fake_demodulate)
    assert hybrid.demodulate_hybrid(b"wave", expect_text=True, expect_media=False) == (b"1800:2400", None)
    assert calls == [(48000, 200, 1800, 2400, 1024)]

    calls.clear()
    assert hybrid.demodulate_hybrid(b"wave", expect_text=False, expect_media=True) == (None, b"600:1000")
    assert calls == [(48000, 200, 600, 1000, 1024)]


def test_hybrid_demodulator_rejects_no_selected_lanes():
    with pytest.raises(ValueError, match="At least one FDM lane"):
        hybrid.demodulate_hybrid(b"wave", expect_text=False, expect_media=False)


def test_hybrid_media_only_lane_uses_media_band(monkeypatch):
    captured = []
    expected = np.array([0.05, 0.1], dtype=np.float32)

    def fake_modulate(payload, sample_rate, symbol_rate, mark, space, gain, max_payload, **kwargs):
        captured.append((payload, mark, space))
        return expected.copy()

    monkeypatch.setattr(hybrid, "modulate", fake_modulate)
    actual = hybrid.modulate_hybrid(None, b"media")
    np.testing.assert_array_equal(actual, expected)
    assert captured == [(b"media", 600, 1000)]
