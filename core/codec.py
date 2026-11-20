"""Low-complexity voice-note codec for very slow acoustic transport.

The codec is IMA ADPCM at a narrowband rate (default 3 kHz, mono). It is not
intended for real-time conversation quality; it trades bandwidth for latency.
"""
from __future__ import annotations

import struct
import wave
from pathlib import Path
import numpy as np

VOICE_MAGIC = b"ECV1"
VOICE2_MAGIC = b"ECV2"
VOICE3_MAGIC = b"ECV3"
VOICE_HEADER = struct.Struct("!4sIIhB")  # magic, sample rate, sample count, predictor, index
VOICE2_HEADER = struct.Struct("!4sBII")  # magic, Codec2 mode, sample rate, sample count
VOICE3_HEADER = struct.Struct("!4sBII")  # magic, Codec2 mode, sample rate, sample count
VOICE2_RATE = 8000
VOICE2_MODE = 8
VOICE2_FRAME_SAMPLES = 320
VOICE2_FRAME_BYTES = 4
VOICE3_RATE = 8000
VOICE3_MODE = 4
VOICE3_FRAME_SAMPLES = 320
VOICE3_FRAME_BYTES = 7
STEP_TABLE = [
    7,8,9,10,11,12,13,14,16,17,19,21,23,25,28,31,
    34,37,41,45,50,55,60,66,73,80,88,97,107,118,130,143,
    157,173,190,209,230,253,279,307,337,371,408,449,494,544,598,658,
    724,796,876,963,1060,1166,1282,1411,1552,1707,1878,2066,2272,2499,
    2749,3024,3327,3660,4026,4428,4871,5358,5894,6484,7132,7845,8630,
    9493,10442,11487,12635,13899,15289,16818,18500,20350,22385,24623,
    27086,29794,32767,
]
INDEX_TABLE = [-1,-1,-1,-1,2,4,6,8,-1,-1,-1,-1,2,4,6,8]


class CodecUnavailable(RuntimeError):
    """Legacy compatibility exception; the built-in ADPCM codec is available."""


def _ima_encode(samples: np.ndarray) -> tuple[int, int, bytes]:
    if samples.size == 0:
        return 0, 0, b""
    predictor = int(samples[0])
    index = 0
    packed = bytearray()
    pending = None
    for sample in samples[1:]:
        target = int(sample)
        step = STEP_TABLE[index]
        diff = target - predictor
        code = 0
        if diff < 0:
            code = 8
            diff = -diff
        delta = step >> 3
        if diff >= step:
            code |= 4
            diff -= step
            delta += step
        if diff >= (step >> 1):
            code |= 2
            diff -= step >> 1
            delta += step >> 1
        if diff >= (step >> 2):
            code |= 1
            delta += step >> 2
        predictor += -delta if code & 8 else delta
        predictor = max(-32768, min(32767, predictor))
        index = max(0, min(88, index + INDEX_TABLE[code]))
        if pending is None:
            pending = code
        else:
            packed.append(pending | (code << 4))
            pending = None
    if pending is not None:
        packed.append(pending)
    return int(samples[0]), 0, bytes(packed)


def _ima_decode(predictor: int, index: int, encoded: bytes, sample_count: int) -> np.ndarray:
    if not -32768 <= predictor <= 32767 or not 0 <= index <= 88:
        raise ValueError("Invalid ADPCM predictor state")
    if sample_count < 0 or sample_count > 100_000_000:
        raise ValueError("Invalid voice sample count")
    out = np.empty(sample_count, dtype=np.int16)
    if sample_count == 0:
        return out
    out[0] = predictor
    pos = 1
    for byte in encoded:
        for code in (byte & 0x0F, (byte >> 4) & 0x0F):
            if pos >= sample_count:
                break
            step = STEP_TABLE[index]
            delta = step >> 3
            if code & 4:
                delta += step
            if code & 2:
                delta += step >> 1
            if code & 1:
                delta += step >> 2
            predictor += -delta if code & 8 else delta
            predictor = max(-32768, min(32767, predictor))
            index = max(0, min(88, index + INDEX_TABLE[code]))
            out[pos] = predictor
            pos += 1
    if pos != sample_count:
        raise ValueError("Truncated ADPCM voice payload")
    return out


def _resample_mono(samples, source_rate: int, target_rate: int) -> np.ndarray:
    arr = np.asarray(samples, dtype=np.float32)
    if arr.ndim == 2:
        arr = arr.mean(axis=1)
    arr = arr.reshape(-1)
    if not np.all(np.isfinite(arr)):
        raise ValueError("Voice recording contains non-finite samples")
    if source_rate <= 0 or target_rate <= 0:
        raise ValueError("Sample rates must be positive")
    if arr.size == 0:
        return np.empty(0, dtype=np.int16)
    # Anti-alias before downsampling. A short windowed-sinc low-pass is enough
    # for the narrowband 4/8 kHz voice profiles and avoids high-frequency foldback.
    work = arr.astype(np.float64)
    if target_rate < source_rate and work.size > 80:
        cutoff = 0.45 * target_rate / source_rate
        taps = 63
        positions = np.arange(taps, dtype=np.float64) - (taps - 1) / 2
        kernel = 2 * cutoff * np.sinc(2 * cutoff * positions) * np.hamming(taps)
        kernel /= np.sum(kernel)
        work = np.convolve(work, kernel, mode="same")
    output_count = max(1, int(round(work.size * target_rate / source_rate)))
    src_positions = np.arange(work.size, dtype=np.float64)
    dst_positions = np.linspace(0, work.size - 1, output_count, dtype=np.float64)
    resampled = np.interp(dst_positions, src_positions, work)
    return np.clip(np.rint(resampled * 32767.0), -32768, 32767).astype(np.int16)


def encode_voice_message(samples, source_rate: int, target_rate: int = 4000) -> bytes:
    """Encode mono voice samples into a compact ECV1 narrowband ADPCM file."""
    if target_rate not in (3000, 4000, 8000):
        raise ValueError("Voice target rate must be 3000, 4000 or 8000 Hz")
    pcm = _resample_mono(samples, int(source_rate), target_rate)
    if not 1 <= pcm.size <= target_rate * 300:
        raise ValueError("Voice message must contain between a sample and 5 minutes")
    predictor, index, body = _ima_encode(pcm)
    return VOICE_HEADER.pack(VOICE_MAGIC, target_rate, int(pcm.size), predictor, index) + body


def decode_voice_message(blob: bytes) -> tuple[np.ndarray, int]:
    blob = bytes(blob)
    if blob.startswith(VOICE3_MAGIC):
        return decode_codec2_1300_message(blob)
    if blob.startswith(VOICE2_MAGIC):
        return decode_emergency_voice_message(blob)
    if len(blob) < VOICE_HEADER.size:
        raise ValueError("Truncated ECV voice file")
    magic, sample_rate, sample_count, predictor, index = VOICE_HEADER.unpack(blob[:VOICE_HEADER.size])
    if magic != VOICE_MAGIC or sample_rate not in (3000, 4000, 8000):
        raise ValueError("Unsupported ECV voice format")
    expected_body = (max(0, sample_count - 1) + 1) // 2
    body = blob[VOICE_HEADER.size:]
    if len(body) != expected_body:
        raise ValueError("Voice payload length does not match sample count")
    pcm = _ima_decode(predictor, index, body, sample_count)
    return pcm.astype(np.float32) / 32768.0, sample_rate


def encode_codec2_1300_message(samples, source_rate: int) -> bytes:
    """Encode voice as ECV3 using Codec2 1300 bit/s, mono 8 kHz frames."""
    from core.codec2_backend import Codec2_1300

    pcm = _resample_mono(samples, int(source_rate), VOICE3_RATE)
    if not 1 <= pcm.size <= VOICE3_RATE * 300:
        raise ValueError("Voice message must contain between a sample and 5 minutes")
    frame_count = (int(pcm.size) + VOICE3_FRAME_SAMPLES - 1) // VOICE3_FRAME_SAMPLES
    padded = np.zeros(frame_count * VOICE3_FRAME_SAMPLES, dtype=np.int16)
    padded[:pcm.size] = pcm
    encoded = bytearray()
    with Codec2_1300() as codec:
        for start in range(0, padded.size, VOICE3_FRAME_SAMPLES):
            encoded.extend(codec.encode_frame(padded[start:start + VOICE3_FRAME_SAMPLES]))
    header = VOICE3_HEADER.pack(
        VOICE3_MAGIC, VOICE3_MODE, VOICE3_RATE, int(pcm.size)
    )
    return header + bytes(encoded)


def decode_codec2_1300_message(blob: bytes) -> tuple[np.ndarray, int]:
    from core.codec2_backend import Codec2_1300

    raw = bytes(blob)
    if len(raw) < VOICE3_HEADER.size:
        raise ValueError("Truncated Codec2 1300 voice file")
    magic, mode, sample_rate, sample_count = VOICE3_HEADER.unpack(raw[:VOICE3_HEADER.size])
    if magic != VOICE3_MAGIC or mode != VOICE3_MODE or sample_rate != VOICE3_RATE:
        raise ValueError("Unsupported Codec2 1300 voice format")
    if not 1 <= sample_count <= VOICE3_RATE * 300:
        raise ValueError("Invalid Codec2 1300 sample count")
    frames = (sample_count + VOICE3_FRAME_SAMPLES - 1) // VOICE3_FRAME_SAMPLES
    body = raw[VOICE3_HEADER.size:]
    if len(body) != frames * VOICE3_FRAME_BYTES:
        raise ValueError("Codec2 1300 payload length does not match sample count")
    out = np.empty(frames * VOICE3_FRAME_SAMPLES, dtype=np.int16)
    with Codec2_1300() as codec:
        for frame in range(frames):
            start = frame * VOICE3_FRAME_BYTES
            decoded = codec.decode_frame(body[start:start + VOICE3_FRAME_BYTES])
            dst = frame * VOICE3_FRAME_SAMPLES
            out[dst:dst + VOICE3_FRAME_SAMPLES] = decoded
    return out[:sample_count].astype(np.float32) / 32768.0, sample_rate




def encode_emergency_voice_message(samples, source_rate: int) -> bytes:
    """Encode a low-bitrate 700C emergency voice clip (requires libcodec2)."""
    from core.codec2_backend import Codec2_700C, Codec2Unavailable

    pcm = _resample_mono(samples, int(source_rate), VOICE2_RATE)
    if not 1 <= pcm.size <= VOICE2_RATE * 300:
        raise ValueError("Emergency voice message must contain between a sample and 5 minutes")
    frame_count = (int(pcm.size) + VOICE2_FRAME_SAMPLES - 1) // VOICE2_FRAME_SAMPLES
    padded = np.zeros(frame_count * VOICE2_FRAME_SAMPLES, dtype=np.int16)
    padded[:pcm.size] = pcm
    encoded = bytearray()
    try:
        with Codec2_700C() as codec:
            for start in range(0, padded.size, VOICE2_FRAME_SAMPLES):
                encoded.extend(codec.encode_frame(padded[start:start + VOICE2_FRAME_SAMPLES]))
    except Codec2Unavailable:
        raise
    header = VOICE2_HEADER.pack(VOICE2_MAGIC, VOICE2_MODE, VOICE2_RATE, int(pcm.size))
    return header + bytes(encoded)


def decode_emergency_voice_message(blob: bytes) -> tuple[np.ndarray, int]:
    from core.codec2_backend import Codec2_700C, Codec2Unavailable

    blob = bytes(blob)
    if len(blob) < VOICE2_HEADER.size:
        raise ValueError("Truncated Codec2 voice file")
    magic, mode, sample_rate, sample_count = VOICE2_HEADER.unpack(blob[:VOICE2_HEADER.size])
    if magic != VOICE2_MAGIC or mode != VOICE2_MODE or sample_rate != VOICE2_RATE:
        raise ValueError("Unsupported Codec2 voice format")
    if not 1 <= sample_count <= VOICE2_RATE * 300:
        raise ValueError("Invalid Codec2 sample count")
    frames = (sample_count + VOICE2_FRAME_SAMPLES - 1) // VOICE2_FRAME_SAMPLES
    body = blob[VOICE2_HEADER.size:]
    if len(body) != frames * VOICE2_FRAME_BYTES:
        raise ValueError("Codec2 payload length does not match sample count")
    out = np.empty(frames * VOICE2_FRAME_SAMPLES, dtype=np.int16)
    try:
        with Codec2_700C() as codec:
            for frame in range(frames):
                start = frame * VOICE2_FRAME_BYTES
                decoded = codec.decode_frame(body[start:start + VOICE2_FRAME_BYTES])
                dst = frame * VOICE2_FRAME_SAMPLES
                out[dst:dst + VOICE2_FRAME_SAMPLES] = decoded
    except Codec2Unavailable:
        raise
    return out[:sample_count].astype(np.float32) / 32768.0, sample_rate


def encode_voice(pcm, sample_rate: int = 48000):
    """Compatibility API: encode mono float/PCM samples as an ECV1 voice blob."""
    return encode_voice_message(pcm, sample_rate)


def decode_voice(blob):
    """Compatibility API: decode an ECV1 voice blob to float32 mono samples."""
    samples, _sample_rate = decode_voice_message(blob)
    return samples


def write_voice_wav(samples, sample_rate: int, destination):
    """Write decoded float32 mono samples as a standard PCM16 WAV file."""
    data = np.asarray(samples, dtype=np.float32).reshape(-1)
    if sample_rate not in (3000, 4000, 8000) or not np.all(np.isfinite(data)):
        raise ValueError("Invalid decoded voice samples")
    pcm = np.clip(np.rint(data * 32767.0), -32768, 32767).astype("<i2")
    path = Path(destination)
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(sample_rate)
        stream.writeframes(pcm.tobytes())
    return path
