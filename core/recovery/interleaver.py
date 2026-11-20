"""Deterministic block interleaving for spreading short burst errors."""
from __future__ import annotations

import math


def interleave(values, depth: int = 8):
    """Write row-wise and transmit column-wise; no padding is transmitted."""
    values = list(values)
    if not isinstance(depth, int) or depth < 2:
        raise ValueError("Interleaver depth must be an integer >= 2")
    if len(values) <= depth:
        return values[:]
    rows = math.ceil(len(values) / depth)
    return [values[row * depth + col]
            for col in range(depth)
            for row in range(rows)
            if row * depth + col < len(values)]


def deinterleave(values, depth: int = 8):
    """Invert :func:`interleave` for the exact encoded stream length."""
    values = list(values)
    if not isinstance(depth, int) or depth < 2:
        raise ValueError("Interleaver depth must be an integer >= 2")
    if len(values) <= depth:
        return values[:]
    rows = math.ceil(len(values) / depth)
    matrix = [[None] * depth for _ in range(rows)]
    cursor = 0
    for col in range(depth):
        for row in range(rows):
            index = row * depth + col
            if index < len(values):
                matrix[row][col] = values[cursor]
                cursor += 1
    return [matrix[row][col]
            for row in range(rows)
            for col in range(depth)
            if row * depth + col < len(values)]
