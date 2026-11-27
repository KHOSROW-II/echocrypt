"""Function-level and property-style regression tests for ECHOCRYPT core behavior.

These pytest tests complement the existing unittest integration suite. They avoid
physical audio devices and exercise deterministic codec, crypto, modem, recovery,
configuration, and transfer invariants.
"""
from __future__ import annotations

import wave

import numpy as np
import pytest
from cryptography.exceptions import InvalidTag

from core.binary_transfer import BinaryTransferError, _safe_filename
from core.codec import _resample_mono, decode_voice_message, encode_voice_message, write_voice_wav
from core.config import Config
from core.crypto import ReplayError, SecureChannel
from core.modem import demodulate, fec_decode, fec_encode, modulate
from core.recovery.frequency_recovery import estimate_frequency_offset
from core.recovery.interleaver import deinterleave, interleave
from core.transport import packet_duration
from core.worker_validation import compute_worker_hardware_limit


@pytest.mark.parametrize("mode", ["hamming", "repetition3", "raw", "conv"])
@pytest.mark.parametrize("payload", [b"\x00", b"\xff\x00", b"ECHOCRYPT", bytes(range(32))])
def test_fec_encoders_roundtrip_all_supported_modes(mode, payload):
    encoded = fec_encode(payload, mode=mode)
    assert fec_decode(encoded, mode=mode, expected_bytes=len(payload)) == payload


@pytest.mark.parametrize("fec_mode", ["hamming", "repetition3", "raw", "conv"])
def test_modem_roundtrip_with_each_fec_profile(fec_mode):
    payload = b"pytest modem loopback: \x00\x7f\xff"
    waveform = modulate(
        payload, sample_rate=48000, symbol_rate=200, mark=1800, space=2400,
        gain=0.3, max_payload=128, fec_mode=fec_mode,
    )
    assert waveform.dtype == np.float32
    assert waveform.ndim == 1 and waveform.size > 0
    assert np.isfinite(waveform).all()
    assert np.max(np.abs(waveform)) <= 0.30001
    assert demodulate(
        waveform, sample_rate=48000, symbol_rate=200, mark=1800, space=2400,
        max_payload=128,
    ) == payload


def test_hopping_modem_roundtrip_uses_the_authenticated_session_key():
    payload = b"authenticated frequency hopping"
    hopping_key = bytes(range(32))
    waveform = modulate(
        payload, sample_rate=48000, symbol_rate=200,
        mark=1800, space=2400, hop_key=hopping_key,
    )
    assert demodulate(
        waveform, sample_rate=48000, symbol_rate=200,
        mark=1800, space=2400, hop_key=hopping_key,
    ) == payload


@pytest.mark.parametrize("sample_rate,symbol_rate,mark,space", [
    (44100, 200, 1800, 2400), (48000, 0, 1800, 2400),
    (48000, 200, 24000, 1800), (48000, 200, 1800, 1800),
])
def test_modem_rejects_invalid_symbol_and_frequency_parameters(
    sample_rate, symbol_rate, mark, space
):
    with pytest.raises(ValueError):
        modulate(b"invalid", sample_rate=sample_rate, symbol_rate=symbol_rate,
                 mark=mark, space=space)


@pytest.mark.parametrize("padding_block", [0, 32, 64, 128, 256])
@pytest.mark.parametrize("payload", [b"", b"x", b"short text", bytes(range(64))])
def test_secure_channel_roundtrip_and_exact_wire_size(padding_block, payload):
    key = bytes(range(32))
    sender = SecureChannel(key, padding_block=padding_block)
    receiver = SecureChannel(key, padding_block=padding_block)
    aad = b"ECHOCRYPT/pytest/context"
    packet = sender.encrypt(payload, aad)
    assert len(packet) == sender.encrypted_size(len(payload))
    assert receiver.decrypt(packet, aad) == payload


def test_authentication_failure_does_not_consume_secure_channel_packet():
    key = b"k" * 32
    sender, receiver = SecureChannel(key), SecureChannel(key)
    packet = sender.encrypt(b"authenticated", b"correct context")
    with pytest.raises(InvalidTag):
        receiver.decrypt(packet, b"wrong context")
    assert receiver.decrypt(packet, b"correct context") == b"authenticated"


def test_secure_channel_accepts_out_of_order_packets_once_within_replay_window():
    key = b"r" * 32
    sender = SecureChannel(key, replay_window=4)
    receiver = SecureChannel(key, replay_window=4)
    packets = [sender.encrypt(bytes([index])) for index in range(4)]
    assert receiver.decrypt(packets[2]) == b"\x02"
    assert receiver.decrypt(packets[0]) == b"\x00"
    assert receiver.decrypt(packets[1]) == b"\x01"
    with pytest.raises(ReplayError):
        receiver.decrypt(packets[1])


def test_secure_channel_rejects_packets_outside_replay_window():
    key = b"w" * 32
    sender, receiver = SecureChannel(key, replay_window=2), SecureChannel(key, replay_window=2)
    packets = [sender.encrypt(bytes([index])) for index in range(4)]
    assert receiver.decrypt(packets[2]) == b"\x02"
    with pytest.raises(ReplayError, match="older than the replay window"):
        receiver.decrypt(packets[0])


@pytest.mark.parametrize("length", [0, 1, 2, 7, 8, 9, 15, 16, 17, 31, 32, 33, 63, 64, 65, 127])
@pytest.mark.parametrize("depth", [2, 3, 4, 8, 16])
def test_interleave_is_an_exact_inverse_for_partial_and_full_blocks(length, depth):
    original = list(range(length))
    assert deinterleave(interleave(original, depth), depth) == original


@pytest.mark.parametrize("function", [interleave, deinterleave])
@pytest.mark.parametrize("depth", [0, 1, -1, 1.5, True])
def test_interleaver_rejects_invalid_depth(function, depth):
    with pytest.raises(ValueError):
        function([1, 2, 3], depth)


def test_frequency_recovery_estimates_a_known_common_carrier_offset():
    sample_rate, samples_per_symbol = 48000, 240
    mark, space, offset = 1200, 2200, 30
    sync_bits = [1, 0] * 8
    waveform, phase = [], 0.0
    positions = np.arange(samples_per_symbol, dtype=np.float64)
    for bit in sync_bits:
        frequency = (mark if bit else space) + offset
        theta = phase + 2.0 * np.pi * frequency * positions / sample_rate
        waveform.extend(0.5 * np.sin(theta))
        phase = (theta[-1] + 2.0 * np.pi * frequency / sample_rate) % (2.0 * np.pi)
    estimated, score = estimate_frequency_offset(
        np.asarray(waveform), start=0, samples_per_symbol=samples_per_symbol,
        sync_bits=sync_bits, mark=mark, space=space, sample_rate=sample_rate,
        search_limit_hz=60, step_hz=10,
    )
    assert estimated == offset
    assert score > 0.8


def test_voice_codec_roundtrip_preserves_sample_count_and_writes_valid_wav(tmp_path):
    source_rate = 16000
    time_axis = np.arange(source_rate // 2, dtype=np.float64) / source_rate
    source = (0.4 * np.sin(2.0 * np.pi * 220.0 * time_axis)).astype(np.float32)
    encoded = encode_voice_message(source, source_rate, target_rate=4000)
    decoded, decoded_rate = decode_voice_message(encoded)
    reference = _resample_mono(source, source_rate, 4000).astype(np.float32) / 32768.0

    assert decoded_rate == 4000
    assert decoded.dtype == np.float32 and decoded.shape == reference.shape
    assert np.isfinite(decoded).all()
    assert np.mean(np.abs(decoded - reference)) < 0.025

    destination = write_voice_wav(decoded, decoded_rate, tmp_path / "nested" / "voice.wav")
    assert destination.is_file()
    with wave.open(str(destination), "rb") as wav:
        assert wav.getnchannels() == 1 and wav.getsampwidth() == 2
        assert wav.getframerate() == decoded_rate
        assert wav.getnframes() == len(decoded)


@pytest.mark.parametrize("logical_cpus,memory_bytes,expected", [
    (0, None, 1), (1, 16 * 1024**3, 1), (2, 512 * 1024**2, 1),
    (2, 1024 * 1024**2, 2), (16, 1024 * 1024**2, 8),
    (16, 2 * 1024**3, 8), (16, None, 8), (16, 0, 1),
])
def test_worker_hardware_limit_obeys_cpu_ram_and_global_caps(
    logical_cpus, memory_bytes, expected
):
    assert compute_worker_hardware_limit(logical_cpus, memory_bytes) == expected


@pytest.mark.parametrize("filename", [
    "../secret.txt", "..\\secret.txt", "/etc/passwd", "folder/file",
    ".", "..", "", "bad\x00name",
])
def test_binary_transfer_rejects_path_traversal_and_invalid_filenames(filename):
    with pytest.raises(BinaryTransferError):
        _safe_filename(filename)


def test_binary_transfer_sanitizes_unsafe_characters_without_allowing_paths():
    assert _safe_filename("voice?.wav") == "voice_.wav"
    assert _safe_filename("  report.txt  ") == "report.txt"


def test_packet_duration_scales_with_fec_overhead_and_rejects_invalid_inputs():
    config = Config()
    durations = {
        mode: packet_duration(128, config=config, fec_mode=mode)
        for mode in ("raw", "hamming", "conv", "repetition3")
    }
    assert 0 < durations["raw"] < durations["hamming"]
    assert durations["hamming"] < durations["conv"] < durations["repetition3"]
    with pytest.raises(ValueError):
        packet_duration(-1, config=config)
    with pytest.raises(ValueError, match="Unknown FEC"):
        packet_duration(1, config=config, fec_mode="unknown")
    with pytest.raises(ValueError, match="Invalid symbol rate"):
        packet_duration(1, config=Config(symbol_rate=201))
