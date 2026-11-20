"""Coarse carrier-frequency offset estimation from the known BFSK preamble."""
from __future__ import annotations

import numpy as np


def _tone_energy(segment, frequency, sample_rate):
    n = np.arange(len(segment), dtype=np.float64)
    window = np.hanning(len(segment))
    reference = np.exp(-2j * np.pi * frequency * n / sample_rate)
    return abs(np.sum(segment * window * reference)) ** 2


def estimate_frequency_offset(samples, start, samples_per_symbol, sync_bits,
                              mark, space, sample_rate, search_limit_hz=100,
                              step_hz=10):
    x = np.asarray(samples, dtype=np.float64).reshape(-1)
    candidates = range(-int(search_limit_hz), int(search_limit_hz) + 1, int(step_hz))
    best_offset, best_score = 0, -float("inf")
    for delta in candidates:
        score = 0.0
        used = 0
        for index, expected in enumerate(sync_bits):
            begin = start + index * samples_per_symbol
            segment = x[begin:begin + samples_per_symbol]
            if len(segment) != samples_per_symbol:
                break
            e_mark = _tone_energy(segment, mark + delta, sample_rate)
            e_space = _tone_energy(segment, space + delta, sample_rate)
            denominator = e_mark + e_space + 1e-18
            score += ((e_mark - e_space) if expected else (e_space - e_mark)) / denominator
            used += 1
        if used:
            score /= used
            if score > best_score:
                best_offset, best_score = delta, score
    return int(best_offset), float(best_score)
