"""BFSK acoustic modem with ASRE soft decisions and versioned robust framing."""
from __future__ import annotations

import hashlib
import hmac
import math
import struct
import zlib
from functools import lru_cache

import numpy as np
from numpy.lib.stride_tricks import sliding_window_view

from core.recovery.channel_analyzer import analyze_channel
from core.recovery.engine import AdaptiveSignalRecoveryEngine
from core.recovery.errors import (
    FrameCRCError, HeaderCRCError, HoppingKeyRequiredError, PreambleNotFoundError,
)
from core.recovery.frequency_recovery import estimate_frequency_offset
from core.recovery.profile_generator import default_profile
from core.recovery.interleaver import interleave, deinterleave
from core.recovery.soft_decoder import (
    fec_encode as _fec_encode,
    fec_decode as _fec_decode,
    fec_decode_soft,
    hamming1511_encode_bits,
    hamming1511_decode_soft,
)

FMAG_V1 = b"ECF1"
FMAG_V2 = b"ECF2"
FMAG_V3 = b"ECF3"
FMAG = FMAG_V3
HEAD = struct.Struct("!4sHBI")
SYNC_V1 = tuple([0, 1] * 32)
SYNC_V2 = tuple(int(bit) for byte in bytes.fromhex("6f0cb24f4bc67221")
                for bit in f"{byte:08b}")
# ECF3 uses a distinct 64-bit sync marker so ECF2 receivers and ECF3 receivers
# can discriminate formats before interpreting the protected header.
SYNC_V3 = tuple(int(bit) for byte in hashlib.sha256(b"ECHOCRYPT/ECF3/SYNC/v1").digest()[:8]
                for bit in f"{byte:08b}")
PAYLOAD_INTERLEAVER_DEPTH = 16
# Compatibility name for code that imported the legacy sync constant.
SYNC = SYNC_V2
FEC_HAMMING = 0
FEC_REPETITION3 = 1
FEC_RAW = 2
FEC_CONV = 3
FEC_NAMES = {"hamming": FEC_HAMMING, "repetition3": FEC_REPETITION3,
             "raw": FEC_RAW, "conv": FEC_CONV}
FEC_BY_ID = {value: key for key, value in FEC_NAMES.items()}
HOP_OFFSETS = (-600, -300, 0, 300, 600)
HEADER_V2_BYTES = HEAD.size + 4  # fixed modem header plus independent CRC32
HEADER_V2_BLOCKS = math.ceil(HEADER_V2_BYTES * 8 / 11)
HEADER_V2_COPY_BITS = HEADER_V2_BLOCKS * 15
HEADER_V2_REPETITIONS = 2
HEADER_V2_BITS = HEADER_V2_COPY_BITS * HEADER_V2_REPETITIONS


def fec_encode(data, mode="hamming"):
    if not isinstance(data, (bytes, bytearray, memoryview)):
        raise TypeError("FEC input must be bytes-like")
    return _fec_encode(bytes(data), mode)


def fec_decode(bits, mode="hamming", expected_bytes=None):
    return _fec_decode(bits, mode, expected_bytes)


def _validate_rates(sample_rate, symbol_rate, mark, space):
    if not isinstance(sample_rate, int) or not isinstance(symbol_rate, int):
        raise ValueError("Sample rate and symbol rate must be integers")
    if sample_rate <= 0 or symbol_rate <= 0 or sample_rate % symbol_rate:
        raise ValueError("Invalid sample/symbol rate")
    if sample_rate // symbol_rate < 32:
        raise ValueError("At least 32 samples per symbol are required")
    if mark <= 0 or space <= 0 or mark == space or min(mark, space) <= 100 or max(mark, space) >= sample_rate / 2:
        raise ValueError("Invalid modem frequencies")


def _fec_id(mode):
    try:
        return FEC_NAMES[mode]
    except KeyError as exc:
        raise ValueError(f"Unsupported FEC mode: {mode}") from exc


def _hop_seed(packet: bytes, hop_key: bytes | None) -> int:
    if hop_key is None:
        return 0
    if not isinstance(hop_key, (bytes, bytearray)) or len(hop_key) < 16:
        raise ValueError("Hopping key must contain at least 16 bytes")
    return int.from_bytes(hmac.new(bytes(hop_key), b"ECHOCRYPT/HOP/v1" + packet[:24], hashlib.sha256).digest()[:4], "big") or 1


def _hop_pair(seed: int, symbol_index: int, hop_key: bytes | None, mark: int, space: int):
    if seed == 0:
        return mark, space
    if hop_key is None:
        raise ValueError("Frame requires the authenticated hopping key")
    material = seed.to_bytes(4, "big") + struct.pack("!I", symbol_index)
    digest = hashlib.blake2s(material, key=bytes(hop_key[:32]), digest_size=1).digest()[0]
    offset = HOP_OFFSETS[digest % len(HOP_OFFSETS)]
    return mark + offset, space + offset


def _header_v2_encode(header: bytes):
    protected = header + struct.pack("!I", zlib.crc32(header) & 0xffffffff)
    bits = [(byte >> shift) & 1 for byte in protected for shift in range(7, -1, -1)]
    pad = (-len(bits)) % 11
    if pad:
        bits.extend([0] * pad)
    coded = hamming1511_encode_bits(bits)
    return coded * HEADER_V2_REPETITIONS


def _header_v2_decode(llrs):
    """Decode the duplicated, CRC-protected ECF2/ECF3 header.

    The combined soft decisions remain the primary path. When one copy is
    badly faded, independently validating each copy can still recover the
    header without weakening its CRC requirement.
    """
    if len(llrs) != HEADER_V2_BITS:
        raise ValueError("Protected ECF2/ECF3 header bit count mismatch")
    copy_length = HEADER_V2_COPY_BITS
    first = [float(x) for x in llrs[:copy_length]]
    second = [float(x) for x in llrs[copy_length:]]
    combined = [(first[i] + second[i]) / 2.0 for i in range(copy_length)]
    for candidate in (combined, first, second):
        protected = hamming1511_decode_soft(candidate, HEADER_V2_BYTES)
        header, received_crc = protected[:-4], struct.unpack("!I", protected[-4:])[0]
        expected_crc = zlib.crc32(header) & 0xffffffff
        if received_crc == expected_crc:
            return header
    raise HeaderCRCError("ECF2/ECF3 protected header CRC mismatch")


def modulate(payload, sample_rate=48000, symbol_rate=200, mark=1800, space=2400,
             gain=.35, max_payload=1024, fec_mode="conv", hop_key=None,
             wire_version=3):
    """Create a framed BFSK waveform. New transmissions use ECF3 burst interleaving."""
    _validate_rates(sample_rate, symbol_rate, mark, space)
    if not math.isfinite(gain) or not 0.0 < gain <= 1.0:
        raise ValueError("Gain must be finite and within (0, 1]")
    if not 1 <= max_payload <= 65535:
        raise ValueError("max_payload must be between 1 and 65535 bytes")
    if not isinstance(payload, (bytes, bytearray, memoryview)):
        raise TypeError("Payload must be bytes-like")
    payload = bytes(payload)
    if len(payload) > max_payload:
        raise ValueError("Payload too large")
    if wire_version not in (1, 2, 3):
        raise ValueError("Unsupported modem wire version")
    if wire_version == 1 and fec_mode == "conv":
        raise ValueError("Convolutional FEC requires ECF2 framing")

    fec_id = _fec_id(fec_mode)
    seed = _hop_seed(payload, hop_key)
    if seed:
        for offset in HOP_OFFSETS:
            if min(mark + offset, space + offset) <= 100 or max(mark + offset, space + offset) >= sample_rate / 2:
                raise ValueError("Configured base tones do not support the hopping plan")

    magic = {1: FMAG_V1, 2: FMAG_V2, 3: FMAG_V3}[wire_version]
    header = HEAD.pack(magic, len(payload), fec_id, seed)
    crc = zlib.crc32(header + payload) & 0xffffffff
    if wire_version == 1:
        header_bits = _fec_encode(header, "hamming")
        sync = SYNC_V1
    else:
        header_bits = _header_v2_encode(header)
        sync = SYNC_V3 if wire_version == 3 else SYNC_V2
    tail_bits = _fec_encode(payload + struct.pack("!I", crc), fec_mode)
    if wire_version == 3:
        # Spread short bursts across the FEC codeword before BFSK modulation.
        tail_bits = interleave(tail_bits, PAYLOAD_INTERLEAVER_DEPTH)
    bits = list(sync) + header_bits + tail_bits

    samples_per_symbol = sample_rate // symbol_rate
    out = np.empty(len(bits) * samples_per_symbol, dtype=np.float32)
    phase = 0.0
    n = np.arange(samples_per_symbol)
    fixed_symbols = len(sync) + len(header_bits)
    for i, bit in enumerate(bits):
        if i < fixed_symbols:
            frequency = mark if bit else space
        else:
            hop_mark, hop_space = _hop_pair(seed, i, hop_key, mark, space)
            frequency = hop_mark if bit else hop_space
        theta = phase + 2 * np.pi * frequency * n / sample_rate
        out[i * samples_per_symbol:(i + 1) * samples_per_symbol] = gain * np.sin(theta)
        phase = (theta[-1] + 2 * np.pi * frequency / sample_rate) % (2 * np.pi)
    # Do not fade encoded symbols: in particular, fading the last symbol
    # weakens the final FEC/CRC bit group. Fade a non-data continuation instead.
    guard_samples = min(96, max(8, samples_per_symbol // 2))
    guard_n = np.arange(guard_samples, dtype=np.float64)
    guard_phase = phase + (2.0 * np.pi * frequency * guard_n / sample_rate)
    guard = (gain * np.sin(guard_phase) *
             np.linspace(1.0, 0.0, guard_samples, dtype=np.float64)).astype(np.float32)
    return np.concatenate((out, guard))


@lru_cache(maxsize=512)
def _reference(length, frequency, rate):
    n = np.arange(length, dtype=float)
    return np.hanning(length) * np.exp(-2j * np.pi * frequency * n / rate)


def _energy(seg, f, rate):
    return abs(np.sum(seg * _reference(len(seg), f, rate))) ** 2


def _soft_symbol_metric(seg, mark_frequency, space_frequency, sample_rate,
                        tone_balance=None):
    e_mark = _energy(seg, mark_frequency, sample_rate)
    e_space = _energy(seg, space_frequency, sample_rate)
    if tone_balance is not None:
        # Normalize fixed-frequency channel gain estimated from the known sync
        # symbols. This is used for the fixed header only; hopping payload tones
        # have different frequencies and must not inherit this calibration.
        mark_reference, space_reference = tone_balance
        e_mark /= max(float(mark_reference), 1e-18)
        e_space /= max(float(space_reference), 1e-18)
    return float((e_mark - e_space) / (e_mark + e_space + 1e-18))


def _estimate_preamble_tone_balance(samples, start, samples_per_symbol,
                                    sync_bits, mark, space, sample_rate,
                                    frequency_offset=0):
    """Estimate relative mark/space channel gain from known preamble bits."""
    x = np.asarray(samples, dtype=np.float64).reshape(-1)
    mark_levels = []
    space_levels = []
    mark_frequency = mark + frequency_offset
    space_frequency = space + frequency_offset
    for index, expected in enumerate(sync_bits):
        begin = start + index * samples_per_symbol
        segment = x[begin:begin + samples_per_symbol]
        if len(segment) != samples_per_symbol:
            continue
        if expected:
            mark_levels.append(_energy(segment, mark_frequency, sample_rate))
        else:
            space_levels.append(_energy(segment, space_frequency, sample_rate))
    if len(mark_levels) < 8 or len(space_levels) < 8:
        return None
    mark_reference = float(np.median(mark_levels))
    space_reference = float(np.median(space_levels))
    if (not math.isfinite(mark_reference) or not math.isfinite(space_reference)
            or min(mark_reference, space_reference) <= 1e-18):
        return None
    ratio = mark_reference / space_reference
    if not math.isfinite(ratio) or not 0.05 <= ratio <= 20.0:
        return None
    return mark_reference, space_reference


def _find_preamble_candidates(x, starts, windows, samples_per_symbol, sample_rate,
                              mark, space, on_debug=None, search_bounds=None,
                              coarse_step=1, *, max_per_format=8,
                              minimum_score=0.24):
    """Return several independently refined sync candidates, not just one.

    A room-noise peak or a cross-correlation with another wire-version marker
    can score higher than the real preamble. The old single-winner strategy
    then failed the protected-header CRC and abandoned the entire capture even
    when a weaker, correctly aligned preamble was present later in the search
    window. We now retain a small bounded set of separated peaks per format;
    the protected header CRC and payload CRC decide which one is genuine.
    """
    formats = (("ECF3", SYNC_V3), ("ECF2", SYNC_V2), ("ECF1", SYNC_V1))
    mark_ref = _reference(samples_per_symbol, mark, sample_rate)
    space_ref = _reference(samples_per_symbol, space, sample_rate)

    def score_starts(candidate_starts, sync_bits):
        scores = np.zeros(candidate_starts.size, dtype=np.float64)
        for symbol_index, expected in enumerate(sync_bits):
            segments = windows[candidate_starts + symbol_index * samples_per_symbol]
            mark_corr = np.sum(segments * mark_ref[None, :], axis=1)
            space_corr = np.sum(segments * space_ref[None, :], axis=1)
            mark_energy = np.abs(mark_corr) ** 2
            space_energy = np.abs(space_corr) ** 2
            llrs = (mark_energy - space_energy) / (mark_energy + space_energy + 1e-18)
            scores += llrs if expected else -llrs
        return scores / len(sync_bits)

    lower, upper = search_bounds if search_bounds is not None else (
        int(starts[0]), int(starts[-1]))
    radius = max(1, int(coarse_step))
    # Prevent adjacent points on the same correlation plateau from consuming
    # the bounded candidate budget. A preamble shifted by more than 8 symbols
    # remains eligible as a distinct candidate.
    separation = max(samples_per_symbol * 8, radius * 2)
    found = []
    coarse_best = []

    for label, sync_bits in formats:
        coarse_scores = score_starts(starts, sync_bits)
        if coarse_scores.size == 0:
            continue
        best = float(np.max(coarse_scores))
        coarse_best.append((label, best))
        order = np.argsort(coarse_scores)[::-1]
        selected_starts = []
        per_format = []
        for index in order:
            coarse_score = float(coarse_scores[int(index)])
            if coarse_score < minimum_score:
                break
            coarse_start = int(starts[int(index)])
            if any(abs(coarse_start - prior) < separation for prior in selected_starts):
                continue
            fine_starts = np.arange(max(lower, coarse_start - radius),
                                    min(upper, coarse_start + radius) + 1,
                                    dtype=np.int64)
            fine_scores = score_starts(fine_starts, sync_bits)
            fine_index = int(np.argmax(fine_scores))
            score = float(fine_scores[fine_index])
            start = int(fine_starts[fine_index])
            selected_starts.append(coarse_start)
            per_format.append((score, start, label, sync_bits))
            if len(per_format) >= max_per_format:
                break
        found.extend(per_format)

    found.sort(key=lambda item: item[0], reverse=True)
    # Remove near-identical timing candidates for the same sync format while
    # retaining same-position candidates from different formats.
    deduped = []
    duplicate_radius = max(samples_per_symbol * 3, radius * 2)
    for item in found:
        if any(item[2] == prior[2] and abs(item[1] - prior[1]) < duplicate_radius
               for prior in deduped):
            continue
        deduped.append(item)
    # Three formats x eight peaks is a hard upper bound on decoder work.
    deduped = deduped[:24]

    if on_debug is not None:
        top = deduped[0] if deduped else (0.0, 0, "NONE", ())
        score_by_format = {f"{label.lower()}_coarse": f"{score:.4f}"
                           for label, score in coarse_best}
        on_debug("SYNC", "Preamble candidate scan completed",
                 format=top[2], normalized_score=f"{top[0]:.6f}",
                 best_start_sample=top[1], candidate_count=len(deduped),
                 candidates_scanned=int(starts.size), coarse_step=int(coarse_step),
                 **score_by_format)
    return deduped


def _find_preamble(x, starts, windows, samples_per_symbol, sample_rate, mark, space,
                   on_debug=None, search_bounds=None, coarse_step=1):
    """Compatibility helper returning only a strong top preamble candidate."""
    candidates = _find_preamble_candidates(
        x, starts, windows, samples_per_symbol, sample_rate, mark, space,
        on_debug=on_debug, search_bounds=search_bounds, coarse_step=coarse_step,
        max_per_format=1, minimum_score=0.0,
    )
    if not candidates:
        raise PreambleNotFoundError("Preamble confidence too low (0.000 < 0.30)")
    best_score, best_start, best_label, best_sync = candidates[0]
    minimum_confidence = 0.30
    if best_score < minimum_confidence:
        raise PreambleNotFoundError(
            f"Preamble confidence too low ({best_score:.3f} < {minimum_confidence:.2f})"
        )
    return best_start, best_label, best_sync, best_score

def _demodulate_once(samples, sample_rate=48000, symbol_rate=200, mark=1800,
                     space=2400, max_payload=1024, hop_key=None, on_debug=None,
                     return_metrics=False, sample_clock_ratio=1.0, decode_attempts=1,
                     recovery_profile=None):
    _validate_rates(sample_rate, symbol_rate, mark, space)
    if not 1 <= max_payload <= 65535:
        raise ValueError("max_payload must be between 1 and 65535 bytes")
    x = np.asarray(samples, dtype=np.float64).reshape(-1)

    def report(stage, message, **fields):
        if on_debug is not None:
            try:
                on_debug(stage, message, **fields)
            except Exception:
                pass

    if not np.all(np.isfinite(x)):
        raise ValueError("Recording contains non-finite samples")
    sps = sample_rate // symbol_rate
    if len(x) < sps * (len(SYNC_V1) + 1):
        raise ValueError("Recording too short")
    rms = float(np.sqrt(np.mean(x * x)))
    peak = float(np.max(np.abs(x))) if x.size else 0.0
    input_clipping = float(np.mean(np.abs(x) >= 0.995)) if x.size else 0.0
    report("DEMOD", "Audio samples handed to ASRE BFSK demodulator",
           samples=len(x), duration=f"{len(x) / sample_rate:.3f}s",
           rms=f"{rms:.6f}", peak=f"{peak:.6f}",
           sample_rate=sample_rate, symbol_rate=symbol_rate)
    report("CHANNEL", "Input level and clipping preflight",
           signal_rms=f"{rms:.6f}", peak=f"{peak:.6f}",
           clipping_fraction=f"{input_clipping:.5f}",
           status=("SEVERE_CLIPPING" if input_clipping >= 0.01 else
                   "CLIPPING_WARNING" if input_clipping >= 0.002 else "OK"))
    if input_clipping >= 0.01:
        report("CHANNEL", "Severe input clipping detected; reduce microphone gain or speaker volume",
               clipping_fraction=f"{input_clipping:.5f}", threshold="0.01000")

    energy_window = max(64, sps // 2)
    power = x * x
    cumulative = np.concatenate(([0.0], np.cumsum(power, dtype=np.float64)))
    half = energy_window // 2
    left = np.maximum(0, np.arange(len(x)) - half)
    right = np.minimum(len(x), left + energy_window)
    energy = (cumulative[right] - cumulative[left]) / np.maximum(1, right - left)
    if energy.max() < 1e-10:
        report("DEMOD", "No usable signal energy in captured audio")
        raise ValueError("No signal detected")
    active = np.flatnonzero(energy > max(energy.max() * .04, 1e-8))
    if active.size == 0:
        raise ValueError("No active acoustic signal detected")
    # The continuous listener retains up to 650 ms of pre-roll. Search that
    # complete region; broadband room noise can precede the modem tone.
    guess = max(0, int(active[0]) - sps)
    preamble_search_samples = max(sps * 12, int(sample_rate * 0.85))
    last_start = min(guess + preamble_search_samples,
                     len(x) - sps * max(len(SYNC_V1), len(SYNC_V2), len(SYNC_V3)))
    coarse_step = max(1, sps // 3)
    starts = np.arange(guess, last_start + 1, coarse_step, dtype=np.int64)
    report("SYNC", "Searching ECF3, ECF2 and legacy ECF1 preambles",
           first_active_sample=int(active[0]), search_from=guess,
           search_to=last_start, candidate_offsets=len(starts),
           coarse_step=coarse_step, symbol_samples=sps)
    if starts.size == 0:
        raise ValueError("Recording too short for synchronization")

    windows = sliding_window_view(x, sps)
    # The logs show real ECF3 captures whose sync score lands just below 0.30,
    # while a false ECF2 cross-correlation scores higher and fails header CRC.
    # CRC-validated fallback candidates allow a slightly weaker preamble to be
    # considered without treating arbitrary room noise as a valid packet.
    candidates = _find_preamble_candidates(
        x, starts, windows, sps, sample_rate, mark, space,
        on_debug=on_debug, search_bounds=(guess, last_start),
        coarse_step=coarse_step, max_per_format=8, minimum_score=0.24,
    )
    if not candidates:
        best_possible = 0.0
        raise PreambleNotFoundError(
            f"Preamble confidence too low ({best_possible:.3f} < 0.24)"
        )

    profile = recovery_profile or default_profile()
    failures = []
    header_crc_failure_scores = []
    strong_header_crc_failure_scores = []
    strong_frame_crc_failure_scores = []
    protected_header_valid = False
    # A false preamble candidate must not hide the real one. We validate the
    # duplicated protected header and final frame CRC for each bounded
    # candidate; only a CRC-valid packet escapes this loop.
    for candidate_number, (sync_score, preamble_start, wire_label, sync_bits) in enumerate(candidates, 1):
        try:
            frequency_limit = int(profile.frequency_search_limit_hz)
            frequency_step = int(profile.frequency_search_step_hz)
            frequency_offset, frequency_score = estimate_frequency_offset(
                x, preamble_start, sps, sync_bits, mark, space, sample_rate,
                search_limit_hz=frequency_limit,
                step_hz=frequency_step)
            report("CHANNEL", "Carrier-frequency offset estimated from the preamble",
                   offset_hz=frequency_offset, normalized_score=f"{frequency_score:.4f}")
            report("BITS", "Preamble synchronized; decoding protected frame header",
                   preamble_start_sample=preamble_start,
                   preamble_time=f"{preamble_start / sample_rate:.4f}s",
                   format=wire_label, frequency_offset_hz=frequency_offset,
                   candidate=f"{candidate_number}/{len(candidates)}",
                   preamble_score=f"{sync_score:.4f}")

            header_timing_offset = 0

            def get_llr(symbol_index: int, hopping_seed: int = 0,
                        *, timing_offset=None, tone_balance=None):
                phase_offset = header_timing_offset if timing_offset is None else int(timing_offset)
                begin = preamble_start + symbol_index * sps + phase_offset
                segment = x[begin:begin + sps]
                if len(segment) < sps:
                    if len(segment) < int(sps * .90):
                        raise ValueError("Incomplete frame: recording ended early")
                    segment = np.pad(segment, (0, sps - len(segment)))
                if hopping_seed:
                    bit_mark, bit_space = _hop_pair(
                        hopping_seed, symbol_index, hop_key,
                        mark + frequency_offset, space + frequency_offset)
                else:
                    bit_mark = mark + frequency_offset
                    bit_space = space + frequency_offset
                return _soft_symbol_metric(
                    segment, bit_mark, bit_space, sample_rate,
                    tone_balance=tone_balance)

            header_symbol_start = len(sync_bits)
            if wire_label == "ECF1":
                header_symbol_count = HEAD.size * 14
                header_llrs = [get_llr(header_symbol_start + i)
                               for i in range(header_symbol_count)]
                header = fec_decode_soft(header_llrs, "hamming", expected_bytes=HEAD.size)
                if len(header) != HEAD.size:
                    raise ValueError("Legacy frame header size mismatch")
                expected_magic = FMAG_V1
            else:
                header_symbol_count = HEADER_V2_BITS
                try:
                    repeated_header_llrs = [get_llr(header_symbol_start + i)
                                             for i in range(header_symbol_count)]
                    header = _header_v2_decode(repeated_header_llrs)
                except HeaderCRCError as first_error:
                    # Weak preamble correlations are usually false syncs. One
                    # important exception is a candidate whose carrier estimate
                    # is pinned to the configured search boundary: real acoustic
                    # captures can have a weak sync score and still be valid.
                    # Give only those boundary hits a small, CRC-gated header
                    # timing/balance recovery budget; do not widen carrier search.
                    frequency_limit = int(profile.frequency_search_limit_hz)
                    frequency_step = int(profile.frequency_search_step_hz)
                    boundary_hit = (
                        frequency_limit > 0
                        and abs(frequency_offset) >= max(0, frequency_limit - frequency_step)
                    )
                    weak_boundary_recovery = sync_score < 0.50 and boundary_hit
                    if sync_score < 0.50 and not weak_boundary_recovery:
                        raise
                    if weak_boundary_recovery:
                        report(
                            "HEADER_RECOVERY",
                            "Trying bounded header recovery for a weak sync at the carrier-search boundary",
                            sync_score=f"{sync_score:.4f}",
                            frequency_offset_hz=frequency_offset,
                            frequency_limit_hz=frequency_limit,
                        )
                    # Do not replace an estimate just because a broader search
                    # scores marginally better: ordinary frames can have noisy
                    # edge scores. Only after the protected header rejects the
                    # first estimate do we test one expanded candidate. The
                    # candidate is adopted solely if the header CRC validates.
                    recovered = None
                    recovered_offset = 0
                    recovered_method = ""
                    trials = 0
                    if boundary_hit:
                        expanded_limit = min(max(frequency_limit * 2, frequency_limit + 240), 900)
                        expanded_offset, expanded_score = estimate_frequency_offset(
                            x, preamble_start, sps, sync_bits, mark, space, sample_rate,
                            search_limit_hz=expanded_limit,
                            step_hz=frequency_step,
                        )
                        expanded_is_interior = (
                            abs(expanded_offset) < max(0, expanded_limit - frequency_step)
                        )
                        report(
                            "FREQUENCY_RECOVERY",
                            "Testing expanded carrier estimate after protected-header CRC failure",
                            initial_offset_hz=frequency_offset,
                            initial_score=f"{frequency_score:.4f}",
                            expanded_offset_hz=expanded_offset,
                            expanded_score=f"{expanded_score:.4f}",
                            initial_limit_hz=frequency_limit,
                            expanded_limit_hz=expanded_limit,
                        )
                        if expanded_is_interior and expanded_score > frequency_score + 1e-6:
                            original_frequency_offset = frequency_offset
                            frequency_offset = expanded_offset
                            expanded_header_llrs = [
                                get_llr(header_symbol_start + i)
                                for i in range(header_symbol_count)
                            ]
                            trials += 1
                            try:
                                recovered = _header_v2_decode(expanded_header_llrs)
                                recovered_method = "expanded-carrier-search"
                                report(
                                    "FREQUENCY_RECOVERY",
                                    "Expanded carrier estimate recovered a CRC-valid protected header",
                                    original_offset_hz=original_frequency_offset,
                                    accepted_offset_hz=frequency_offset,
                                    trials=trials,
                                )
                            except HeaderCRCError:
                                frequency_offset = original_frequency_offset
                                report(
                                    "FREQUENCY_RECOVERY",
                                    "Expanded carrier estimate also failed protected-header CRC",
                                    attempted_offset_hz=expanded_offset,
                                    trials=trials,
                                )
                    # The sync is known data, so estimate the relative gain of
                    # both BFSK tones from it. Some speaker/microphone paths
                    # attenuate one tone enough to bias every subsequent bit.
                    balance = _estimate_preamble_tone_balance(
                        x, preamble_start, sps, sync_bits, mark, space,
                        sample_rate, frequency_offset,
                    )
                    # Expand fixed symbol-phase recovery to ±1/6 symbol.
                    # The old ±10-sample window was too narrow at 400 baud.
                    phase_step = max(1, sps // 12)
                    offsets = (
                        (0, -phase_step, phase_step) if weak_boundary_recovery
                        else (0, -phase_step, phase_step, -2 * phase_step, 2 * phase_step)
                    )
                    balance_modes = []
                    if balance is not None:
                        balance_modes.append(("preamble-equalized", balance))
                    balance_modes.append(("raw-phase", None))
                    if recovered is None:
                        for method, tone_balance in balance_modes:
                            for timing_offset in offsets:
                                if method == "raw-phase" and timing_offset == 0:
                                    continue  # already tried the baseline path
                                trial_llrs = [
                                    get_llr(header_symbol_start + i,
                                            timing_offset=timing_offset,
                                            tone_balance=tone_balance)
                                    for i in range(header_symbol_count)
                                ]
                                trials += 1
                                try:
                                    recovered = _header_v2_decode(trial_llrs)
                                    recovered_offset = timing_offset
                                    recovered_method = method
                                    break
                                except HeaderCRCError:
                                    pass
                            if recovered is not None:
                                break
                    if recovered is None:
                        raise HeaderCRCError(
                            "ECF2/ECF3 protected header CRC mismatch after "
                            f"{trials} channel-balance/timing recovery trials",
                            sync_score=sync_score,
                        ) from first_error
                    header = recovered
                    header_timing_offset = recovered_offset
                    report("HEADER_RECOVERY",
                           "Protected header recovered with preamble channel calibration",
                           method=recovered_method,
                           symbol_offset_samples=header_timing_offset,
                           mark_space_energy_ratio=(
                               f"{balance[0] / balance[1]:.4f}" if balance else "unavailable"),
                           trials=trials + 1)
                expected_magic = FMAG_V3 if wire_label == "ECF3" else FMAG_V2
            magic, length, fec_id, seed = HEAD.unpack(header)
            report("FRAME", "Protected frame header parsed and independently validated",
                   magic=magic.decode("ascii", errors="replace"), payload_length=length,
                   fec_id=fec_id, hopping="required" if seed else "off", format=wire_label)
            if magic != expected_magic:
                raise ValueError("Frame header magic/version mismatch")
            if wire_label != "ECF1":
                # A CRC-valid header parsed under the wrong preamble format is
                # still a false synchronization candidate. Only a version-matched
                # protected header justifies spending time on clock recovery.
                protected_header_valid = True
            if length > max_payload:
                raise ValueError("Frame length too large")
            fec_mode = FEC_BY_ID.get(fec_id)
            if fec_mode is None:
                raise ValueError("Unsupported in-band FEC mode")
            if wire_label == "ECF1" and fec_mode == "conv":
                raise ValueError("Convolutional FEC is not valid in legacy ECF1 frames")
            if seed and hop_key is None:
                raise HoppingKeyRequiredError(
                    "Frame requires the authenticated hopping key before payload decoding"
                )
            if seed:
                for offset in HOP_OFFSETS:
                    if (min(mark + frequency_offset + offset,
                            space + frequency_offset + offset) <= 100 or
                            max(mark + frequency_offset + offset,
                                space + frequency_offset + offset) >= sample_rate / 2):
                        raise ValueError("Configured base tones do not support the hopping plan")

            expected_decoded_bytes = length + 4
            if fec_mode == "hamming":
                tail_bit_count = expected_decoded_bytes * 14
            elif fec_mode == "repetition3":
                tail_bit_count = expected_decoded_bytes * 24
            elif fec_mode == "raw":
                tail_bit_count = expected_decoded_bytes * 8
            elif fec_mode == "conv":
                tail_bit_count = (expected_decoded_bytes * 8 + 6) * 2
            else:
                raise ValueError("Unsupported FEC mode")
            fixed_end_symbol = len(sync_bits) + header_symbol_count
            report("BITS", "Extracting soft payload and CRC decisions", fec=fec_mode,
                   expected_bits=tail_bit_count)
            tail_llrs = [get_llr(fixed_end_symbol + i, seed)
                         for i in range(tail_bit_count)]
            if wire_label == "ECF3":
                tail_llrs = deinterleave(tail_llrs, PAYLOAD_INTERLEAVER_DEPTH)
                report("FEC", "Payload soft decisions deinterleaved before FEC decoding",
                       depth=PAYLOAD_INTERLEAVER_DEPTH, bits=len(tail_llrs))
            report("FEC", "Soft FEC decoder started", bits=len(tail_llrs), mode=fec_mode)
            decoded = fec_decode_soft(tail_llrs, fec_mode,
                                      expected_bytes=expected_decoded_bytes)
            payload = decoded[:length]
            received_crc = struct.unpack("!I", decoded[length:])[0]
            body = HEAD.pack(magic, length, fec_id, seed) + payload
            expected_crc = zlib.crc32(body) & 0xffffffff
            if expected_crc != received_crc:
                report("CRC", "Frame integrity check failed",
                       expected=f"{expected_crc:08x}", received=f"{received_crc:08x}")
                raise FrameCRCError(
                    "CRC mismatch: soft FEC decode did not recover a valid frame",
                    sync_score=sync_score,
                )
            report("CRC", "Frame integrity verified", crc=f"{received_crc:08x}")
            report("PACKET", "Modem frame delivered to protocol layer", payload_bytes=len(payload),
                   payload_type=(payload[:4].decode("ascii", errors="replace") if payload else "empty"))
            metrics = analyze_channel(
                x, preamble_start, sps, sync_bits, mark, space, sample_rate,
                frequency_offset, sync_score, preamble_start - guess,
                sample_clock_ratio=sample_clock_ratio, decode_attempts=decode_attempts,
            )
            report("SYNC", "CRC validation selected acoustic preamble candidate",
                   candidate=f"{candidate_number}/{len(candidates)}",
                   format=wire_label, preamble_score=f"{sync_score:.4f}",
                   preamble_start_sample=preamble_start)
            report("CHANNEL", "ASRE channel analysis complete",
                   grade=metrics.channel_grade,
                   tone_contrast_db=f"{metrics.tone_contrast_db:.2f}",
                   bit_confidence=f"{metrics.mean_bit_confidence:.3f}",
                   clipping_fraction=f"{metrics.clipping_fraction:.5f}",
                   frequency_offset_hz=metrics.frequency_offset_hz,
                   timing_offset_samples=metrics.timing_offset_samples)
            return (payload, metrics) if return_metrics else payload
        except (ValueError, struct.error) as exc:
            if isinstance(exc, HoppingKeyRequiredError) and sync_score >= 0.50:
                # A strong, CRC-validated header proves this is a hopping frame.
                # Do not downgrade the profile mismatch to a generic ValueError
                # after trying unrelated false preamble candidates.
                raise
            if isinstance(exc, HeaderCRCError):
                header_crc_failure_scores.append(float(sync_score))
                if sync_score >= 0.50:
                    strong_header_crc_failure_scores.append(float(sync_score))
            if isinstance(exc, FrameCRCError) and sync_score >= 0.50:
                strong_frame_crc_failure_scores.append(float(sync_score))
            failures.append(f"{candidate_number}:{wire_label}@{preamble_start} score={sync_score:.3f} {type(exc).__name__}: {exc}")
            report("SYNC_CANDIDATE", "Preamble candidate failed validation; trying another candidate",
                   candidate=f"{candidate_number}/{len(candidates)}", format=wire_label,
                   preamble_start_sample=preamble_start, preamble_score=f"{sync_score:.4f}",
                   error=type(exc).__name__, detail=str(exc))

    detail = " | ".join(failures[:8])
    # A strong preamble followed by a protected-header CRC error is evidence
    # of a real signal. Lower-ranked false candidates may fail as incomplete
    # or wrong-version frames; they must not erase the stronger diagnosis.
    strongest_header_score = max(strong_header_crc_failure_scores, default=0.0)
    strongest_frame_score = max(strong_frame_crc_failure_scores, default=0.0)
    if strongest_frame_score > strongest_header_score:
        raise FrameCRCError(
            "Strong preamble candidate(s) decoded a protected header but failed frame CRC; "
            f"best_sync_score={strongest_frame_score:.3f}: {detail}",
            sync_score=strongest_frame_score,
        )
    if strongest_header_score:
        raise HeaderCRCError(
            "Strong preamble candidate(s) failed the protected header CRC; "
            f"best_sync_score={strongest_header_score:.3f}: {detail}",
            sync_score=strongest_header_score,
        )
    if candidates and header_crc_failure_scores and len(header_crc_failure_scores) == len(failures):
        raise HeaderCRCError(
            "No preamble candidate passed the protected header CRC; "
            f"tried={len(failures)} best_score={max(header_crc_failure_scores):.3f}: {detail}",
            sync_score=max(header_crc_failure_scores),
        )
    if (not candidates or candidates[0][0] < 0.30) and not protected_header_valid:
        # Weak random/noise correlations are not worth multiplying into a
        # sample-clock retry storm. A weak preamble is retained only when its
        # protected header CRC proves that it is a plausible modem frame.
        score = candidates[0][0] if candidates else 0.0
        raise PreambleNotFoundError(
            f"Preamble confidence too low ({score:.3f} < 0.30)"
        )
    raise ValueError(
        f"No preamble candidate passed protected-header and payload CRC validation; "
        f"tried={len(failures)} best_score={candidates[0][0]:.3f}: {detail}"
    )

_DEFAULT_ASRE_ENGINE = AdaptiveSignalRecoveryEngine(default_profile())


def demodulate(samples, sample_rate=48000, symbol_rate=200, mark=1800,
               space=2400, max_payload=1024, hop_key=None, on_debug=None,
               timing_recovery=True, return_metrics=False, recovery_engine=None):
    """Decode ECF3/ECF2/ECF1 with ASRE soft decisions and bounded recovery."""
    engine = recovery_engine or _DEFAULT_ASRE_ENGINE

    def decoder(adjusted, sample_clock_ratio=1.0, decode_attempts=1,
                recovery_profile=None):
        return _demodulate_once(
            adjusted, sample_rate, symbol_rate, mark, space, max_payload,
            hop_key, on_debug, return_metrics=True,
            sample_clock_ratio=sample_clock_ratio, decode_attempts=decode_attempts,
            recovery_profile=recovery_profile,
        )

    payload, metrics = engine.recover(samples, decoder, on_debug, timing_recovery)
    if return_metrics:
        return payload, metrics
    return payload
