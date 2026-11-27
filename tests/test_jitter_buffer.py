from __future__ import annotations

import pytest

from core import jitter


def test_jitter_buffer_fifo_and_empty_pop():
    buffer = jitter.JitterBuffer()
    assert len(buffer) == 0
    assert buffer.pop() is None

    buffer.push(10, b"first")
    buffer.push(11, b"second")
    assert len(buffer) == 2
    assert buffer.pop() == (10, b"first")
    assert buffer.pop() == (11, b"second")
    assert buffer.pop() is None
    assert len(buffer) == 0


@pytest.mark.parametrize("capacity", [0, 1, -1])
def test_jitter_buffer_rejects_capacity_below_two(capacity):
    with pytest.raises(ValueError, match="capacity must be >=2"):
        jitter.JitterBuffer(capacity)


def test_jitter_buffer_capacity_discards_oldest_packet():
    buffer = jitter.JitterBuffer(capacity=2)
    for seq in range(4):
        buffer.push(seq, bytes([seq]))
    assert len(buffer) == 2
    assert buffer.pop() == (2, b"\x02")
    assert buffer.pop() == (3, b"\x03")
