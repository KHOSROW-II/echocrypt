"""Soft-decision FEC primitives used by ASRE.

LLR convention: positive values favor acoustic MARK / bit 1; negative values
favor SPACE / bit 0. Magnitude represents confidence, not calibrated probability.
"""
from __future__ import annotations

from functools import lru_cache
import math

import numpy as np

from .interleaver import interleave, deinterleave


INTERLEAVER_DEPTH = 8
CONV_K = 7
CONV_POLYS = (0o171, 0o133)


def _parity(value: int) -> int:
    return value.bit_count() & 1


def _bits_from_bytes(data: bytes):
    return [(byte >> shift) & 1 for byte in data for shift in range(7, -1, -1)]


def _bytes_from_bits(bits):
    if len(bits) % 8:
        raise ValueError("Decoded bit count is not byte aligned")
    result = bytearray()
    for offset in range(0, len(bits), 8):
        value = 0
        for bit in bits[offset:offset + 8]:
            value = (value << 1) | int(bit)
        result.append(value)
    return bytes(result)


def hamming74_encode_bits(bits):
    if len(bits) % 4:
        raise ValueError("Hamming(7,4) input must be a multiple of four bits")
    encoded = []
    for offset in range(0, len(bits), 4):
        a, b, c, d = map(int, bits[offset:offset + 4])
        encoded.extend((a ^ b ^ d, a ^ c ^ d, a, b ^ c ^ d, b, c, d))
    return encoded


def hamming74_encode(data: bytes):
    bits = _bits_from_bytes(data)
    return hamming74_encode_bits(bits)


@lru_cache(maxsize=1)
def _hamming74_codebook():
    words = []
    nibble_values = []
    for value in range(16):
        bits = [(value >> shift) & 1 for shift in (3, 2, 1, 0)]
        words.append(hamming74_encode_bits(bits))
        nibble_values.append(value)
    return np.asarray(words, dtype=np.int8), np.asarray(nibble_values, dtype=np.uint8)


def hamming74_decode_soft(llrs):
    if len(llrs) % 7:
        raise ValueError("Bad Hamming(7,4) soft-decoder length")
    codebook, values = _hamming74_codebook()
    signs = (codebook.astype(np.float64) * 2.0) - 1.0
    result_bits = []
    for offset in range(0, len(llrs), 7):
        chunk = np.asarray(llrs[offset:offset + 7], dtype=np.float64)
        best = int(np.argmax(np.sum(signs * chunk[None, :], axis=1)))
        value = int(values[best])
        result_bits.extend((value >> shift) & 1 for shift in (3, 2, 1, 0))
    return result_bits


def hamming1511_encode_bits(bits):
    """Encode data bits using the standard binary Hamming(15,11) layout."""
    if len(bits) % 11:
        raise ValueError("Hamming(15,11) input must be a multiple of eleven bits")
    parity_positions = {1, 2, 4, 8}
    encoded = []
    for offset in range(0, len(bits), 11):
        word = [0] * 16  # one-indexed code positions
        data_cursor = 0
        for position in range(1, 16):
            if position not in parity_positions:
                word[position] = int(bits[offset + data_cursor])
                data_cursor += 1
        for parity_position in (1, 2, 4, 8):
            parity = 0
            for position in range(1, 16):
                if position & parity_position and position != parity_position:
                    parity ^= word[position]
            word[parity_position] = parity
        encoded.extend(word[1:])
    return encoded


@lru_cache(maxsize=1)
def _hamming1511_codebook():
    rows = []
    data_values = []
    for value in range(1 << 11):
        bits = [(value >> shift) & 1 for shift in range(10, -1, -1)]
        rows.append(hamming1511_encode_bits(bits))
        data_values.append(value)
    codebook = np.asarray(rows, dtype=np.int8)
    signs = codebook.astype(np.float32) * 2.0 - 1.0
    return signs, np.asarray(data_values, dtype=np.uint16)


def hamming1511_decode_soft(llrs, output_bytes: int):
    if not isinstance(output_bytes, int) or output_bytes <= 0:
        raise ValueError("output_bytes must be a positive integer")
    data_bit_count = output_bytes * 8
    blocks = math.ceil(data_bit_count / 11)
    if len(llrs) != blocks * 15:
        raise ValueError("Hamming(15,11) header length mismatch")
    signs, _values = _hamming1511_codebook()
    decoded = []
    for block_index in range(blocks):
        chunk = np.asarray(llrs[block_index * 15:(block_index + 1) * 15], dtype=np.float32)
        value = int(np.argmax(np.sum(signs * chunk[None, :], axis=1)))
        decoded.extend((value >> shift) & 1 for shift in range(10, -1, -1))
    return _bytes_from_bits(decoded[:data_bit_count])


def convolutional_encode(data: bytes):
    """Rate-1/2, constraint-length-7 convolutional code (171,133 octal)."""
    bits = _bits_from_bytes(data) + [0] * (CONV_K - 1)
    state = 0
    encoded = []
    for bit in bits:
        register = (state << 1) | bit
        encoded.append(_parity(register & CONV_POLYS[0]))
        encoded.append(_parity(register & CONV_POLYS[1]))
        state = register & ((1 << (CONV_K - 1)) - 1)
    return interleave(encoded, INTERLEAVER_DEPTH)


def _viterbi_decode(llrs, expected_bytes: int):
    expected_steps = expected_bytes * 8 + CONV_K - 1
    if len(llrs) != expected_steps * 2:
        raise ValueError("Convolutional FEC length mismatch")
    soft = np.asarray(deinterleave(llrs, INTERLEAVER_DEPTH), dtype=np.float32).reshape(-1, 2)
    states = 1 << (CONV_K - 1)
    neg_inf = -1e30
    metrics = np.full(states, neg_inf, dtype=np.float64)
    metrics[0] = 0.0
    predecessors = np.zeros((expected_steps, states), dtype=np.uint8)
    decisions = np.zeros((expected_steps, states), dtype=np.uint8)
    state_mask = states - 1

    transitions = []
    for state in range(states):
        row = []
        for bit in (0, 1):
            register = (state << 1) | bit
            next_state = register & state_mask
            out0 = _parity(register & CONV_POLYS[0])
            out1 = _parity(register & CONV_POLYS[1])
            row.append((next_state, out0, out1, bit))
        transitions.append(row)

    for step, pair in enumerate(soft):
        next_metrics = np.full(states, neg_inf, dtype=np.float64)
        for state in range(states):
            base_metric = metrics[state]
            if base_metric <= neg_inf / 2:
                continue
            for next_state, out0, out1, bit in transitions[state]:
                score = base_metric + (1.0 if out0 else -1.0) * float(pair[0])
                score += (1.0 if out1 else -1.0) * float(pair[1])
                if score > next_metrics[next_state]:
                    next_metrics[next_state] = score
                    predecessors[step, next_state] = state
                    decisions[step, next_state] = bit
        metrics = next_metrics

    if metrics[0] <= neg_inf / 2:
        raise ValueError("Convolutional decoder could not reach the terminated state")
    decoded = [0] * expected_steps
    state = 0  # encoder appends six zero bits, so the final state is known
    for step in range(expected_steps - 1, -1, -1):
        decoded[step] = int(decisions[step, state])
        state = int(predecessors[step, state])
    if state != 0:
        raise ValueError("Convolutional decoder traceback did not return to the initial state")
    return _bytes_from_bits(decoded[:expected_bytes * 8])


def fec_encode(data: bytes, mode: str = "hamming"):
    if mode == "hamming":
        return hamming74_encode(data)
    if mode == "repetition3":
        return [bit for byte in data for shift in range(7, -1, -1)
                for bit in ([(byte >> shift) & 1] * 3)]
    if mode == "raw":
        return _bits_from_bytes(data)
    if mode == "conv":
        return convolutional_encode(data)
    raise ValueError(f"Unknown FEC mode: {mode}")


def fec_decode_soft(llrs, mode: str = "hamming", expected_bytes: int | None = None):
    if mode == "hamming":
        bits = hamming74_decode_soft(llrs)
        decoded = _bytes_from_bits(bits)
    elif mode == "repetition3":
        if len(llrs) % 24:
            raise ValueError("Bad repetition FEC length")
        count = len(llrs) // 24
        if expected_bytes is not None and count != expected_bytes:
            raise ValueError("Repetition FEC length mismatch")
        bits = []
        for offset in range(0, len(llrs), 3):
            bits.append(1 if sum(float(x) for x in llrs[offset:offset + 3]) >= 0 else 0)
        decoded = _bytes_from_bits(bits)
    elif mode == "raw":
        if len(llrs) % 8:
            raise ValueError("Bad raw FEC length")
        count = len(llrs) // 8
        if expected_bytes is not None and count != expected_bytes:
            raise ValueError("Raw FEC length mismatch")
        decoded = _bytes_from_bits([1 if float(x) >= 0 else 0 for x in llrs])
    elif mode == "conv":
        if expected_bytes is None:
            raise ValueError("Convolutional decoding requires expected_bytes")
        decoded = _viterbi_decode(llrs, expected_bytes)
    else:
        raise ValueError(f"Unknown FEC mode: {mode}")
    if expected_bytes is not None and len(decoded) != expected_bytes:
        raise ValueError("Decoded FEC payload length mismatch")
    return decoded


def fec_decode(bits, mode: str = "hamming", expected_bytes: int | None = None):
    if any(bit not in (0, 1, False, True) for bit in bits):
        raise ValueError("FEC input must contain only binary values")
    return fec_decode_soft([1.0 if int(bit) else -1.0 for bit in bits],
                           mode=mode, expected_bytes=expected_bytes)
