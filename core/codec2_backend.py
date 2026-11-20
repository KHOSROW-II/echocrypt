"""Optional native Codec2 700C backend loaded dynamically through ctypes.

Codec2 is an optional system library (LGPL). This module does not bundle it.
"""
from __future__ import annotations

import ctypes
import ctypes.util

import numpy as np

MODE_700C = 8
EXPECTED_SAMPLES = 320
EXPECTED_BITS = 28
EXPECTED_BYTES = 4

MODE_1300 = 4
MODE_1300_SAMPLES = 320
MODE_1300_BITS = 52
MODE_1300_BYTES = 7


class Codec2Unavailable(RuntimeError):
    """Raised when a compatible libcodec2 is not installed on this system."""


class Codec2_700C:
    def __init__(self):
        library_name = ctypes.util.find_library("codec2")
        if not library_name:
            raise Codec2Unavailable(
                "libcodec2 was not found; install the system Codec2 runtime or use ADPCM voice mode"
            )
        try:
            lib = ctypes.CDLL(library_name)
            lib.codec2_create.argtypes = [ctypes.c_int]
            lib.codec2_create.restype = ctypes.c_void_p
            lib.codec2_destroy.argtypes = [ctypes.c_void_p]
            for name in ("codec2_samples_per_frame", "codec2_bits_per_frame", "codec2_bytes_per_frame"):
                fn = getattr(lib, name)
                fn.argtypes = [ctypes.c_void_p]
                fn.restype = ctypes.c_int
            lib.codec2_encode.argtypes = [
                ctypes.c_void_p,
                ctypes.POINTER(ctypes.c_ubyte),
                ctypes.POINTER(ctypes.c_short),
            ]
            lib.codec2_decode.argtypes = [
                ctypes.c_void_p,
                ctypes.POINTER(ctypes.c_short),
                ctypes.POINTER(ctypes.c_ubyte),
            ]
            lib.codec2_set_natural_or_gray.argtypes = [ctypes.c_void_p, ctypes.c_int]
            state = lib.codec2_create(MODE_700C)
        except (OSError, AttributeError) as exc:
            raise Codec2Unavailable(f"Could not load a compatible libcodec2: {exc}") from exc
        if not state:
            raise Codec2Unavailable("libcodec2 does not support Codec2 mode 700C")
        samples = lib.codec2_samples_per_frame(state)
        bits = lib.codec2_bits_per_frame(state)
        size = lib.codec2_bytes_per_frame(state)
        if (samples, bits, size) != (EXPECTED_SAMPLES, EXPECTED_BITS, EXPECTED_BYTES):
            lib.codec2_destroy(state)
            raise Codec2Unavailable(
                "This libcodec2 build has an incompatible 700C frame layout "
                f"(samples={samples}, bits={bits}, bytes={size})"
            )
        lib.codec2_set_natural_or_gray(state, 1)
        self.lib = lib
        self.state = state
        self.closed = False

    def encode_frame(self, samples) -> bytes:
        arr = np.asarray(samples, dtype=np.int16).reshape(-1)
        if arr.size != EXPECTED_SAMPLES:
            raise ValueError(f"Codec2 700C frame must contain {EXPECTED_SAMPLES} samples")
        arr = np.ascontiguousarray(arr)
        pcm = (ctypes.c_short * EXPECTED_SAMPLES).from_buffer_copy(arr.tobytes())
        output = (ctypes.c_ubyte * EXPECTED_BYTES)()
        self.lib.codec2_encode(self.state, output, pcm)
        return bytes(output)

    def decode_frame(self, encoded: bytes) -> np.ndarray:
        if len(encoded) != EXPECTED_BYTES:
            raise ValueError(f"Codec2 700C frame must contain {EXPECTED_BYTES} bytes")
        packed = (ctypes.c_ubyte * EXPECTED_BYTES).from_buffer_copy(encoded)
        output = (ctypes.c_short * EXPECTED_SAMPLES)()
        self.lib.codec2_decode(self.state, output, packed)
        return np.ctypeslib.as_array(output).copy()

    def close(self):
        if not self.closed:
            self.lib.codec2_destroy(self.state)
            self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()


class Codec2_1300:
    """Codec2 1300 bit/s mono speech codec (40 ms frames at 8 kHz)."""

    def __init__(self):
        library_name = ctypes.util.find_library("codec2")
        if not library_name:
            raise Codec2Unavailable("libcodec2 was not found")
        try:
            lib = ctypes.CDLL(library_name)
            lib.codec2_create.argtypes = [ctypes.c_int]
            lib.codec2_create.restype = ctypes.c_void_p
            lib.codec2_destroy.argtypes = [ctypes.c_void_p]
            for name in ("codec2_samples_per_frame", "codec2_bits_per_frame", "codec2_bytes_per_frame"):
                fn = getattr(lib, name)
                fn.argtypes = [ctypes.c_void_p]
                fn.restype = ctypes.c_int
            lib.codec2_encode.argtypes = [
                ctypes.c_void_p,
                ctypes.POINTER(ctypes.c_ubyte),
                ctypes.POINTER(ctypes.c_short),
            ]
            lib.codec2_decode.argtypes = [
                ctypes.c_void_p,
                ctypes.POINTER(ctypes.c_short),
                ctypes.POINTER(ctypes.c_ubyte),
            ]
            lib.codec2_set_natural_or_gray.argtypes = [ctypes.c_void_p, ctypes.c_int]
            state = lib.codec2_create(MODE_1300)
        except (OSError, AttributeError) as exc:
            raise Codec2Unavailable(f"Could not load a compatible libcodec2: {exc}") from exc
        if not state:
            raise Codec2Unavailable("This libcodec2 build does not support Codec2 mode 1300")
        actual = (
            lib.codec2_samples_per_frame(state),
            lib.codec2_bits_per_frame(state),
            lib.codec2_bytes_per_frame(state),
        )
        expected = (MODE_1300_SAMPLES, MODE_1300_BITS, MODE_1300_BYTES)
        if actual != expected:
            lib.codec2_destroy(state)
            raise Codec2Unavailable(
                "This libcodec2 build has an incompatible 1300 layout "
                f"(samples={actual[0]}, bits={actual[1]}, bytes={actual[2]})"
            )
        lib.codec2_set_natural_or_gray(state, 1)
        self.lib = lib
        self.state = state
        self.closed = False

    def encode_frame(self, samples) -> bytes:
        arr = np.asarray(samples, dtype=np.int16).reshape(-1)
        if arr.size != MODE_1300_SAMPLES:
            raise ValueError(f"Codec2 1300 frame must contain {MODE_1300_SAMPLES} samples")
        arr = np.ascontiguousarray(arr)
        pcm = (ctypes.c_short * MODE_1300_SAMPLES).from_buffer_copy(arr.tobytes())
        output = (ctypes.c_ubyte * MODE_1300_BYTES)()
        self.lib.codec2_encode(self.state, output, pcm)
        return bytes(output)

    def decode_frame(self, encoded: bytes) -> np.ndarray:
        if len(encoded) != MODE_1300_BYTES:
            raise ValueError(f"Codec2 1300 frame must contain {MODE_1300_BYTES} bytes")
        packed = (ctypes.c_ubyte * MODE_1300_BYTES).from_buffer_copy(encoded)
        output = (ctypes.c_short * MODE_1300_SAMPLES)()
        self.lib.codec2_decode(self.state, output, packed)
        return np.ctypeslib.as_array(output).copy()

    def close(self):
        if not self.closed:
            self.lib.codec2_destroy(self.state)
            self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()


def codec2_available() -> bool:
    try:
        codec = Codec2_700C()
    except Codec2Unavailable:
        return False
    codec.close()
    return True


def codec2_1300_available() -> bool:
    try:
        codec = Codec2_1300()
    except Codec2Unavailable:
        return False
    codec.close()
    return True
