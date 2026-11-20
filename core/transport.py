"""Acoustic-only modem transport over selected audio devices."""
from core.config import load
from core.modem import (modulate, demodulate, HEADER_V2_BITS, SYNC_V2, HOP_OFFSETS)


def packet_duration(packet_bytes, config=None, fec_mode="hamming", symbol_rate=None):
    """Estimate BFSK frame duration, including modem framing and selected FEC."""
    c = config or load()
    if not isinstance(packet_bytes, int) or packet_bytes < 0:
        raise ValueError("packet_bytes must be a non-negative integer")
    header_bits = len(SYNC_V2) + HEADER_V2_BITS
    decoded_body_bytes = packet_bytes + 4  # payload plus frame CRC32
    if fec_mode == "hamming":
        body_bits = 14 * decoded_body_bytes
    elif fec_mode == "repetition3":
        body_bits = 24 * decoded_body_bytes
    elif fec_mode == "raw":
        body_bits = 8 * decoded_body_bytes
    elif fec_mode == "conv":
        body_bits = 2 * (8 * decoded_body_bytes + 6)
    else:
        raise ValueError("Unknown FEC mode")
    encoded_bits = header_bits + body_bits
    rate = c.symbol_rate if symbol_rate is None else int(symbol_rate)
    if rate <= 0 or c.sample_rate % rate or c.sample_rate // rate < 32:
        raise ValueError("Invalid symbol rate for configured audio sample rate")
    guard_samples = min(96, max(8, (c.sample_rate // rate) // 2))
    return encoded_bits / rate + guard_samples / c.sample_rate


def transmit(packet, fec_mode="hamming", hop_key=None, symbol_rate=None,
             mark_hz=None, space_hz=None, gain=None):
    from core.audio import play

    c = load()
    rate = c.symbol_rate if symbol_rate is None else int(symbol_rate)
    mark = c.mark_hz if mark_hz is None else int(mark_hz)
    space = c.space_hz if space_hz is None else int(space_hz)
    amplitude = c.gain if gain is None else float(gain)
    wave = modulate(packet, c.sample_rate, rate, mark, space, amplitude,
                    c.max_payload, fec_mode=fec_mode, hop_key=hop_key)
    play(wave, c.sample_rate, c.output_device)
    return len(wave) / c.sample_rate


def receive(seconds=None):
    """Legacy fixed-window receive; protocol traffic should use receive_auto."""
    from core.audio import record

    c = load()
    duration = c.record_seconds if seconds is None else float(seconds)
    if duration <= 0:
        raise ValueError("Recording duration must be positive")
    samples = record(duration, c.sample_rate, c.input_device)
    return demodulate(samples, c.sample_rate, c.symbol_rate,
                      c.mark_hz, c.space_hz, c.max_payload)


def receive_auto(start_timeout=120.0, max_packet_bytes=None, max_record_seconds=None,
                 hop_key=None, symbol_rate=None, mark_hz=None, space_hz=None,
                 on_debug=None):
    """Detect one incoming acoustic frame, then decode its in-band FEC/hop header."""
    from core.audio import record_until_packet

    c = load()
    rate = c.symbol_rate if symbol_rate is None else int(symbol_rate)
    mark = c.mark_hz if mark_hz is None else int(mark_hz)
    space = c.space_hz if space_hz is None else int(space_hz)
    packet_limit = c.max_payload if max_packet_bytes is None else int(max_packet_bytes)
    if not 1 <= packet_limit <= c.max_payload:
        raise ValueError("Requested receive packet limit is outside modem configuration")
    if max_record_seconds is None:
        # Reserve the longest ECF2 header + repetition-3 body so retries are not cut off.
        maximum = (len(SYNC_V2) + HEADER_V2_BITS + 24 * (packet_limit + 4)) / rate + 1.5
    else:
        maximum = float(max_record_seconds)
    if on_debug:
        on_debug("RX", "Receiver pipeline started", max_payload=packet_limit,
                 timeout=f"{start_timeout:.1f}s", symbol_rate=rate,
                 tones=f"{mark}/{space} Hz", hopping="enabled" if hop_key else "disabled")
    # The body may move by any configured hopping offset. Listening only to
    # the two base tones can terminate a valid frame as soon as hopping starts.
    activity_tones = tuple(sorted({int(base + offset)
                                   for base in (mark, space)
                                   for offset in HOP_OFFSETS}))
    samples = record_until_packet(c.sample_rate, c.input_device,
                                  start_timeout=start_timeout,
                                  max_record_seconds=maximum,
                                  calibration_seconds=0.15,
                                  pre_roll_seconds=max(0.65, 64 / rate + 0.20),
                                  activity_frequencies=activity_tones,
                                  on_debug=on_debug)
    if on_debug:
        on_debug("RX", "Audio capture returned; passing samples to demodulator",
                 samples=len(samples), seconds=f"{len(samples) / c.sample_rate:.3f}")
    try:
        return demodulate(samples, c.sample_rate, rate, mark, space,
                          packet_limit, hop_key=hop_key, on_debug=on_debug)
    except Exception as exc:
        if on_debug:
            on_debug("RX", "Modem decoding failed", error=type(exc).__name__, detail=str(exc))
        raise


def transmit_hybrid(text_packet, media_packet, symbol_rate=None, fec_mode="hamming"):
    """Transmit two simultaneous FDM lanes in one acoustic waveform."""
    from core.audio import play
    from core.hybrid import modulate_hybrid

    c = load()
    rate = c.symbol_rate if symbol_rate is None else int(symbol_rate)
    wave = modulate_hybrid(text_packet, media_packet, c.sample_rate, rate,
                           max_payload=c.max_payload, fec_mode=fec_mode)
    play(wave, c.sample_rate, c.output_device)
    return len(wave) / c.sample_rate


def receive_auto_samples(start_timeout=120.0, max_packet_bytes=None,
                         symbol_rate=None, max_record_seconds=None, on_debug=None):
    """Capture a raw acoustic frame for multi-band FDM demultiplexing."""
    from core.audio import record_until_packet

    c = load()
    rate = c.symbol_rate if symbol_rate is None else int(symbol_rate)
    packet_limit = c.max_payload if max_packet_bytes is None else int(max_packet_bytes)
    if not 1 <= packet_limit <= c.max_payload:
        raise ValueError("Requested receive packet limit is outside modem configuration")
    if max_record_seconds is None:
        max_record_seconds = (len(SYNC_V2) + HEADER_V2_BITS + 24 * (packet_limit + 4)) / rate + 2.0
    activity_tones = tuple(sorted({int(base + offset)
                                   for base in (c.mark_hz, c.space_hz)
                                   for offset in HOP_OFFSETS}))
    return record_until_packet(
        c.sample_rate, c.input_device,
        start_timeout=start_timeout,
        max_record_seconds=float(max_record_seconds),
        calibration_seconds=0.15,
        pre_roll_seconds=max(0.65, 64 / rate + 0.20),
        activity_frequencies=activity_tones,
        on_debug=on_debug,
    )
