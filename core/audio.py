"""Audio device helpers and automatic one-frame acoustic capture."""

from collections import deque
import math
import time
import os
import threading

import numpy as np


def _sounddevice(allow_install=True):
    from core.dependencies import require_python_package, offer_portaudio_install
    try:
        return require_python_package("sounddevice", "sounddevice>=0.4.6",
                                      "audio input/output", allow_install=allow_install)
    except OSError as exc:
        if allow_install and offer_portaudio_install():
            import importlib
            import sys
            sys.modules.pop("sounddevice", None)
            importlib.invalidate_caches()
            return importlib.import_module("sounddevice")
        if os.name == "nt":
            detail = "Reinstall sounddevice inside .venv and verify its bundled PortAudio DLL can load."
        else:
            detail = "Install the PortAudio runtime (for Ubuntu/Debian: sudo apt install libportaudio2)."
        raise RuntimeError(f"sounddevice could not load its PortAudio runtime. {detail}") from exc


def _is_android_audio_runtime():
    """Return whether this process is running inside an Android userspace."""
    return bool(
        os.environ.get("ANDROID_ROOT")
        or os.environ.get("ANDROID_DATA")
        or os.path.exists("/system/bin/app_process")
        or os.environ.get("TERMUX_VERSION")
    )


def _is_termux_audio_runtime():
    """Avoid treating inherited Termux variables inside unrelated Linux as Termux."""
    prefix = os.environ.get("PREFIX", "")
    return bool(
        os.environ.get("TERMUX_VERSION")
        and prefix.startswith("/data/data/com.termux/")
    )


def _explain_android_input_error(exc):
    """Add Android-specific guidance to native microphone initialization failures."""
    message = str(exc)
    lowered = message.lower()
    opensles_failure = (
        "opensles" in lowered
        or "paerrorcode -9999" in lowered
        or "initializing inputstream failed" in lowered
    )
    if not _is_android_audio_runtime() or not opensles_failure:
        return exc

    guidance = [
        "EchoCrypt could not open the Android microphone through PortAudio.",
        "This happens before modem decoding or Broadcast protocol handling.",
        "Check Android's microphone privacy toggle and the Microphone permission "
        "for the Android app that is actually running this Python process.",
        "Close other apps that may be holding the microphone, then retry.",
    ]
    if _is_termux_audio_runtime():
        guidance.extend([
            "Termux note: the current upstream Termux app manifest does not "
            "itself declare RECORD_AUDIO, but the Termux:API manifest does. "
            "The upstream apps request the same shared UID, so Android may "
            "share the runtime permission grant across the matching packages. "
            "Install Termux:API from the same trusted distribution as Termux "
            "and grant microphone permission if it is requested or exposed in "
            "Android settings.",
            "The Termux:API recording command writes a file; that file-based "
            "capture is not a live PCM stream and cannot replace EchoCrypt's "
            "continuous modem input. The direct InputStream test below is the "
            "relevant check for PortAudio/OpenSL ES.",
        ])
    guidance.append(f"Underlying audio error: {message}")
    return RuntimeError("\n".join(guidance))


def default_device_names(allow_install=False):
    """Return OS-selected default input/output names, never the first enumerated device."""
    sd = _sounddevice(allow_install=allow_install)

    def resolve(kind, index_position):
        try:
            defaults = sd.default.device
            index = defaults[index_position] if defaults is not None else None
            if index is not None and int(index) >= 0:
                info = sd.query_devices(int(index))
            else:
                # PortAudio resolves kind-specific None to the operating-system default.
                info = sd.query_devices(kind=kind)
            if info:
                return str(info.get("name", "System default"))
        except Exception:
            pass
        return "No system default configured"

    return resolve("input", 0), resolve("output", 1)


def configured_device_names(input_device=None, output_device=None, allow_install=False):
    """Resolve configured devices; None follows the OS default for that direction."""
    sd = _sounddevice(allow_install=allow_install)

    def resolve(device, kind):
        try:
            if device is None:
                index = 0 if kind == "input" else 1
                defaults = sd.default.device
                selected = defaults[index] if defaults is not None else None
                info = sd.query_devices(int(selected)) if selected is not None and int(selected) >= 0 else sd.query_devices(kind=kind)
            elif isinstance(device, str):
                info = sd.query_devices(device, kind=kind)
            else:
                info = sd.query_devices(int(device))
            if info:
                return str(info.get("name", "Unknown device"))
        except Exception:
            pass
        return "No device available"

    return resolve(input_device, "input"), resolve(output_device, "output")


def list_devices():
    sd = _sounddevice()
    return [(i, d["name"], int(d["max_input_channels"]), int(d["max_output_channels"]))
            for i, d in enumerate(sd.query_devices())]


def play(data, rate, device=None):
    sd = _sounddevice()
    sd.play(np.asarray(data, dtype=np.float32), samplerate=rate, device=device, blocking=True)


def stop_playback():
    """Stop current PortAudio playback, used when an acoustic session closes."""
    sd = _sounddevice()
    sd.stop()


def record(seconds, rate, device=None):
    if not math.isfinite(float(seconds)) or seconds <= 0:
        raise ValueError("Duration must be positive and finite")
    sd = _sounddevice()
    audio = sd.rec(int(seconds * rate), samplerate=rate, channels=1,
                   dtype="float32", device=device)
    sd.wait()
    return audio[:, 0]


class MicrophoneRecorder:
    """Callback-driven mono recorder for press-and-hold voice notes."""

    def __init__(self, rate: int, device=None, *, blocksize=1024, max_seconds=60.0):
        if not isinstance(rate, int) or rate < 8000:
            raise ValueError("Recording sample rate must be at least 8000 Hz")
        if not isinstance(blocksize, int) or blocksize <= 0:
            raise ValueError("Recording block size must be positive")
        if not math.isfinite(float(max_seconds)) or not 0.25 <= max_seconds <= 300:
            raise ValueError("Maximum recording duration must be 0.25..300 seconds")
        self.rate, self.device, self.blocksize = rate, device, blocksize
        self.max_samples = int(rate * max_seconds)
        self._sd = _sounddevice()
        self._lock = threading.Lock()
        self._chunks = []
        self._samples = 0
        self._stream = None
        self._stopped = False

    def _callback(self, indata, frames, time_info, status):
        data = np.asarray(indata, dtype=np.float32).reshape(-1).copy()
        with self._lock:
            if self._stopped or self._samples >= self.max_samples:
                return
            data = data[:self.max_samples - self._samples]
            if data.size:
                self._chunks.append(data)
                self._samples += int(data.size)

    def start(self):
        if self._stream is not None:
            return self
        self._stream = self._sd.InputStream(
            samplerate=self.rate, channels=1, dtype="float32",
            blocksize=self.blocksize, device=self.device, callback=self._callback,
        )
        try:
            self._stream.start()
        except Exception:
            try:
                self._stream.close()
            finally:
                self._stream = None
            raise
        return self

    @property
    def duration(self):
        with self._lock:
            return self._samples / float(self.rate)

    @property
    def limit_reached(self):
        with self._lock:
            return self._samples >= self.max_samples

    def stop(self):
        stream, self._stream = self._stream, None
        if stream is not None:
            try:
                stream.stop()
            finally:
                stream.close()
        with self._lock:
            self._stopped = True
            chunks, self._chunks = self._chunks, []
        return np.concatenate(chunks).astype(np.float32, copy=False) if chunks else np.empty(0, dtype=np.float32)

    def __enter__(self):
        return self.start()

    def __exit__(self, exc_type, exc, tb):
        self.stop()
        return False



def test_speaker_channels(rate, device=None, duration=2.0, frequency=880.0, amplitude=0.18,
                         allow_install=True):
    """Play a two-second tone on LEFT, then RIGHT, to verify stereo output.

    Returns device metadata. A mono output is rejected rather than pretending
    that independent left/right testing took place.
    """
    if not isinstance(rate, int) or rate < 8000:
        raise ValueError("Sample rate must be an integer of at least 8000 Hz")
    if not math.isfinite(float(duration)) or not 0.25 <= duration <= 5.0:
        raise ValueError("Speaker test duration must be between 0.25 and 5 seconds")
    if not math.isfinite(float(frequency)) or not 100 <= frequency < rate / 2:
        raise ValueError("Speaker test frequency is outside the valid range")
    if not math.isfinite(float(amplitude)) or not 0.01 <= amplitude <= 0.5:
        raise ValueError("Speaker test amplitude must be between 0.01 and 0.5")

    sd = _sounddevice(allow_install=allow_install)
    try:
        info = sd.query_devices(device, kind="output")
    except TypeError:
        # Accommodate minimal compatible backends and simple test doubles.
        info = sd.query_devices(device)
    if not info or int(info.get("max_output_channels", 0)) < 2:
        raise RuntimeError(
            "The selected system output is mono or unavailable. "
            "Choose a stereo output device to test left and right independently."
        )

    count = int(round(duration * rate))
    t = np.arange(count, dtype=np.float64) / float(rate)
    tone = (amplitude * np.sin(2.0 * np.pi * frequency * t)).astype(np.float32)
    # Short fades avoid clicks without materially changing the two-second tone.
    fade_count = min(int(rate * 0.015), count // 4)
    if fade_count > 0:
        fade = np.linspace(0.0, 1.0, fade_count, dtype=np.float32)
        tone[:fade_count] *= fade
        tone[-fade_count:] *= fade[::-1]

    left = np.zeros((count, 2), dtype=np.float32)
    left[:, 0] = tone
    right = np.zeros((count, 2), dtype=np.float32)
    right[:, 1] = tone

    sd.play(left, samplerate=rate, device=device, blocking=True)
    time.sleep(0.20)
    sd.play(right, samplerate=rate, device=device, blocking=True)
    return {
        "device": str(info.get("name", "System default output")),
        "channels": 2,
        "duration_per_channel": float(duration),
        "frequency": float(frequency),
    }


def test_acoustic_path(rate, input_device=None, output_device=None,
                       frequencies=(1800.0, 2400.0), duration=0.22,
                       amplitude=0.10, allow_install=True):
    """Verify the selected output-to-input path across every supplied modem tone.

    This is a local loopback test. It does not certify remote conferencing-codec
    transparency or physical-device performance beyond the selected local path.
    """
    if isinstance(rate, bool) or not isinstance(rate, int) or rate < 8000:
        raise ValueError("Acoustic path testing requires a sample rate of at least 8000 Hz")
    tones = tuple(sorted({float(value) for value in frequencies}))
    if len(tones) < 2 or any(
        not math.isfinite(value) or value <= 100 or value >= rate / 2 - 500
        for value in tones
    ):
        raise ValueError("Test tones must be distinct and safely inside the audio passband")
    if not math.isfinite(float(duration)) or not 0.12 <= duration <= 1.0:
        raise ValueError("Each test tone must last between 0.12 and 1 second")
    if not math.isfinite(float(amplitude)) or not 0.02 <= amplitude <= 0.25:
        raise ValueError("Test amplitude must be between 0.02 and 0.25")

    sd = _sounddevice(allow_install=allow_install)
    samples_per_tone = int(round(duration * rate))
    t = np.arange(samples_per_tone, dtype=np.float64) / float(rate)
    fade_count = min(max(1, int(rate * 0.012)), samples_per_tone // 4)
    fade = np.ones(samples_per_tone, dtype=np.float64)
    fade[:fade_count] = np.linspace(0.0, 1.0, fade_count)
    fade[-fade_count:] = np.linspace(1.0, 0.0, fade_count)
    waveform = np.concatenate([
        (float(amplitude) * np.sin(2.0 * np.pi * frequency * t) * fade).astype(np.float32)
        for frequency in tones
    ])

    captured = []
    position = 0
    finished = threading.Event()
    callback_status = []

    def callback(indata, outdata, frames, time_info, status):
        nonlocal position
        if status:
            callback_status.append(str(status))
        outdata.fill(0)
        count = min(int(frames), max(0, len(waveform) - position))
        if count:
            outdata[:count, 0] = waveform[position:position + count]
            captured.append(np.asarray(indata[:count, 0], dtype=np.float32).copy())
            position += count
        if position >= len(waveform):
            finished.set()
            raise sd.CallbackStop

    stream = sd.Stream(
        samplerate=rate, channels=(1, 1), dtype="float32",
        device=(input_device, output_device), blocksize=1024, callback=callback,
    )
    try:
        stream.start()
        if not finished.wait(len(waveform) / float(rate) + 5.0):
            raise TimeoutError("Acoustic input/output loopback test timed out")
    finally:
        try:
            stream.stop()
        except Exception:
            pass
        finally:
            stream.close()

    if not captured:
        raise RuntimeError("The selected microphone returned no samples during the path test")
    recording = np.concatenate(captured)
    rms = float(np.sqrt(np.mean(np.square(recording, dtype=np.float64))))
    results = []
    for frequency in tones:
        ratio = _tone_band_ratio(recording, rate, (frequency,), bandwidth_hz=140.0)
        results.append({
            "frequency": frequency,
            "band_ratio": ratio,
            "detected": bool(rms >= 0.0002 and ratio >= 0.012),
        })
    severe_status = any(
        "overflow" in message.lower() or "underflow" in message.lower()
        for message in callback_status
    )
    return {
        "sample_rate": rate,
        "input_device": input_device,
        "output_device": output_device,
        "rms": rms,
        "tones": tuple(results),
        "callback_status": tuple(callback_status),
        "passed": all(item["detected"] for item in results) and not severe_status,
    }


def test_ultrasonic_path(rate, input_device=None, output_device=None,
                         frequencies=(20700.0, 22200.0), duration=0.45,
                         amplitude=0.20, allow_install=True):
    """Run a local full-duplex ultrasonic speaker-to-microphone loopback test.

    This is a practical path check, not a certification of the transducer's
    frequency response or a guarantee that a remote device supports the band.
    Every test frequency must be above 20 kHz and safely below Nyquist.
    """
    if isinstance(rate, bool) or not isinstance(rate, int) or rate < 44100:
        raise ValueError("Ultrasonic testing requires an integer sample rate of at least 44100 Hz")
    tones = tuple(float(value) for value in frequencies)
    if len(tones) < 2 or len(set(tones)) != len(tones):
        raise ValueError("At least two distinct ultrasonic test frequencies are required")
    if any(not math.isfinite(value) or value <= 20000
           or value >= rate / 2 - 500 for value in tones):
        raise ValueError("Ultrasonic tones must be distinct, above 20000 Hz and at least 500 Hz below Nyquist")
    if not math.isfinite(float(duration)) or not 0.20 <= duration <= 2.0:
        raise ValueError("Each ultrasonic test tone must last between 0.20 and 2 seconds")
    if not math.isfinite(float(amplitude)) or not 0.02 <= amplitude <= 0.5:
        raise ValueError("Ultrasonic test amplitude must be between 0.02 and 0.5")

    sd = _sounddevice(allow_install=allow_install)
    samples_per_tone = int(round(duration * rate))
    t = np.arange(samples_per_tone, dtype=np.float64) / float(rate)
    fade_count = min(max(1, int(rate * 0.015)), samples_per_tone // 4)
    fade = np.ones(samples_per_tone, dtype=np.float64)
    fade[:fade_count] = np.linspace(0.0, 1.0, fade_count)
    fade[-fade_count:] = np.linspace(1.0, 0.0, fade_count)
    blocks = [
        (float(amplitude) * np.sin(2.0 * np.pi * frequency * t) * fade).astype(np.float32)
        for frequency in tones
    ]
    waveform = np.concatenate(blocks)
    captured = []
    position = 0
    finished = threading.Event()
    callback_errors = []

    def callback(indata, outdata, frames, time_info, status):
        nonlocal position
        if status:
            callback_errors.append(str(status))
        outdata.fill(0)
        remaining = len(waveform) - position
        count = min(int(frames), max(0, remaining))
        if count:
            outdata[:count, 0] = waveform[position:position + count]
            captured.append(np.asarray(indata[:count, 0], dtype=np.float32).copy())
            position += count
        if position >= len(waveform):
            finished.set()
            raise sd.CallbackStop

    stream = sd.Stream(
        samplerate=rate,
        channels=(1, 1),
        dtype="float32",
        device=(input_device, output_device),
        blocksize=1024,
        callback=callback,
    )
    try:
        stream.start()
        if not finished.wait(len(waveform) / float(rate) + 4.0):
            raise TimeoutError("Ultrasonic full-duplex audio test timed out")
    finally:
        try:
            stream.stop()
        except Exception:
            # Preserve the original open/callback failure; close still runs.
            pass
        finally:
            stream.close()

    if not captured:
        raise RuntimeError("Ultrasonic test captured no microphone samples")
    recording = np.concatenate(captured)
    results = []
    for index, frequency in enumerate(tones):
        start = index * samples_per_tone
        end = min(start + samples_per_tone, len(recording))
        segment = recording[start:end]
        if segment.size < int(rate * 0.10):
            raise RuntimeError("Ultrasonic test capture ended before both tones were measured")
        rms = float(np.sqrt(np.mean(np.square(segment, dtype=np.float64))))
        ratio = _tone_band_ratio(segment, rate, (frequency,), bandwidth_hz=180.0)
        results.append({
            "frequency": frequency,
            "rms": rms,
            "band_ratio": ratio,
            "detected": bool(rms >= 0.0005 and ratio >= 0.08),
        })
    return {
        "sample_rate": rate,
        "input_device": input_device,
        "output_device": output_device,
        "tones": tuple(results),
        "passed": all(item["detected"] for item in results),
        "callback_status": tuple(callback_errors),
    }


class MicrophoneLevelMonitor:
    """Non-recording microphone level monitor backed by a live InputStream."""

    def __init__(self, rate, device=None, blocksize=1024, allow_install=True):
        if not isinstance(rate, int) or rate < 8000:
            raise ValueError("Sample rate must be an integer of at least 8000 Hz")
        if not isinstance(blocksize, int) or blocksize <= 0:
            raise ValueError("Block size must be a positive integer")
        self.rate = rate
        self.device = device
        self.blocksize = blocksize
        self._lock = threading.Lock()
        self._rms = 0.0
        self._peak = 0.0
        self._status = "Waiting for microphone input"
        self._stream = None
        self._sd = _sounddevice(allow_install=allow_install)

    def _callback(self, indata, frames, time_info, status):
        samples = np.asarray(indata, dtype=np.float32)
        if samples.size:
            rms = float(np.sqrt(np.mean(np.square(samples, dtype=np.float64))))
            peak = float(np.max(np.abs(samples)))
        else:
            rms = peak = 0.0
        with self._lock:
            self._rms = rms
            self._peak = peak
            self._status = str(status) if status else "Microphone stream active"

    def start(self):
        if self._stream is not None:
            return self
        self._stream = self._sd.InputStream(
            samplerate=self.rate, channels=1, dtype="float32",
            blocksize=self.blocksize, device=self.device, callback=self._callback,
        )
        try:
            self._stream.start()
        except Exception:
            try:
                self._stream.close()
            finally:
                self._stream = None
            raise
        return self

    def snapshot(self):
        with self._lock:
            rms, peak, status = self._rms, self._peak, self._status
        dbfs = 20.0 * math.log10(max(rms, 1e-7))
        # Map -60..-6 dBFS to a readable 0..100% meter.
        percent = max(0.0, min(100.0, (dbfs + 60.0) / 54.0 * 100.0))
        return {"rms": rms, "peak": peak, "dbfs": dbfs,
                "percent": percent, "clipping": peak >= 0.98, "status": status}

    def stop(self):
        stream, self._stream = self._stream, None
        if stream is not None:
            try:
                stream.stop()
            finally:
                stream.close()

    def __enter__(self):
        return self.start()

    def __exit__(self, exc_type, exc, tb):
        self.stop()
        return False

def _first_sustained_activity(levels, threshold, consecutive=3):
    """Return first index of a sustained above-threshold run, or None."""
    run = 0
    for index, level in enumerate(levels):
        if level >= threshold:
            run += 1
            if run >= consecutive:
                return index - consecutive + 1
        else:
            run = 0
    return None


def _tone_band_ratio(samples, sample_rate, frequencies, bandwidth_hz=85.0):
    """Estimate the fraction of block energy around known modem tone bands."""
    x = np.asarray(samples, dtype=np.float32).reshape(-1)
    if x.size < 64:
        return 0.0
    window = np.hanning(x.size)
    spectrum = np.fft.rfft(x * window)
    power = np.abs(spectrum) ** 2
    total = float(power.sum())
    if total <= 1e-18:
        return 0.0
    bins = np.fft.rfftfreq(x.size, d=1.0 / sample_rate)
    mask = np.zeros(bins.shape, dtype=bool)
    tolerance = max(float(bandwidth_hz), sample_rate / x.size * 1.5)
    for frequency in frequencies:
        mask |= np.abs(bins - float(frequency)) <= tolerance
    return float(power[mask].sum() / total)


def _emit_debug(callback, stage, message, **fields):
    if callback is None:
        return
    try:
        callback(stage, message, **fields)
    except Exception:
        # Diagnostics must never break an audio capture.
        pass


def record_until_packet(rate, device=None, start_timeout=120.0,
                        max_record_seconds=80.0, silence_seconds=0.45,
                        blocksize=1024, calibration_seconds=0.15,
                        pre_roll_seconds=0.65, activity_frequencies=(1800, 2400),
                        on_debug=None, cancel_event=None):
    """Arm input and capture a modem frame using tone-aware activity detection.

    Detection uses energy around the modem's MARK/SPACE frequencies rather than
    broadband RMS alone. This avoids triggering on fans, mains hum, and room noise.
    A rolling pre-roll retains the complete preamble. Debug callbacks receive
    level/ratio and capture-stage events when enabled by the application.
    """
    for name, value in (("start_timeout", start_timeout),
                        ("max_record_seconds", max_record_seconds),
                        ("silence_seconds", silence_seconds),
                        ("calibration_seconds", calibration_seconds),
                        ("pre_roll_seconds", pre_roll_seconds)):
        if not math.isfinite(float(value)) or value <= 0:
            raise ValueError(f"{name} must be positive and finite")
    if not isinstance(rate, int) or rate <= 0 or not isinstance(blocksize, int) or blocksize <= 0:
        raise ValueError("Invalid sample rate or block size")
    if not activity_frequencies or any(not 100 < float(f) < rate / 2 for f in activity_frequencies):
        raise ValueError("Invalid modem activity-detection frequencies")

    def check_cancelled():
        if cancel_event is not None and cancel_event.is_set():
            raise InterruptedError("Acoustic receive cancelled")

    check_cancelled()
    sd = _sounddevice()
    calibration_count = max(4, int(math.ceil(calibration_seconds * rate / blocksize)))
    pre_roll_count = max(4, int(math.ceil(pre_roll_seconds * rate / blocksize)))
    block_seconds = blocksize / rate
    quiet_blocks_required = max(1, int(math.ceil(silence_seconds / block_seconds)))

    _emit_debug(on_debug, "AUDIO", "Opening microphone input stream",
                sample_rate=rate, blocksize=blocksize,
                device="SYSTEM DEFAULT" if device is None else device,
                tones="/".join(str(int(f)) for f in activity_frequencies))
    with sd.InputStream(samplerate=rate, channels=1, dtype="float32",
                        blocksize=blocksize, device=device) as stream:
        calibration = []
        rms_levels = []
        tone_levels = []
        started_at = time.monotonic()
        for _ in range(calibration_count):
            check_cancelled()
            block, overflowed = stream.read(blocksize)
            if overflowed:
                raise RuntimeError("Audio input overflowed during microphone calibration")
            mono = np.asarray(block, dtype=np.float32).reshape(-1)
            rms = float(np.sqrt(np.mean(np.square(mono, dtype=np.float64))))
            ratio = _tone_band_ratio(mono, rate, activity_frequencies)
            calibration.append(mono.copy())
            rms_levels.append(rms)
            tone_levels.append(ratio)

        # Use a low percentile because a signal may begin immediately after the
        # user arms the microphone; the median could then be the modem itself.
        noise_floor = float(np.percentile(rms_levels, 20)) if rms_levels else 0.0
        # The spectral gate already rejects broadband noise. Keep this RMS
        # threshold low enough to accept an attenuated acoustic link even in a
        # noisy room; a high noise-relative threshold can hide a quiet modem.
        rms_floor = max(0.0005, min(noise_floor * 0.20, 0.003))
        # A tone-band ratio makes detection selective; the RMS gate only rejects
        # numerically silent input and is intentionally permissive.
        tone_threshold = 0.12
        active_flags = [rms >= rms_floor and ratio >= tone_threshold
                        for rms, ratio in zip(rms_levels, tone_levels)]
        first_activity = _first_sustained_activity(active_flags, True, consecutive=2)
        if first_activity is not None:
            # Keep only one block before the first modem-tone block. Including
            # the full calibration interval would let unrelated room noise
            # confuse the modem's preamble search.
            keep_from = max(0, first_activity - 1)
            chunks = calibration[keep_from:]
            active = True
            _emit_debug(on_debug, "DETECT", "Modem tone was already present during arming",
                        calibration_block=first_activity, noise_rms=f"{noise_floor:.6f}")
        else:
            chunks = []
            pre_roll = deque(((block.copy(), flag) for block, flag in
                              zip(calibration[-pre_roll_count:], active_flags[-pre_roll_count:])),
                             maxlen=pre_roll_count)
            active = False
            _emit_debug(on_debug, "CALIBRATION", "Microphone armed; waiting for modem tones",
                        noise_rms=f"{noise_floor:.6f}", rms_gate=f"{rms_floor:.6f}",
                        tone_ratio_gate=f"{tone_threshold:.2f}")

        quiet_blocks = 0
        signal_started = time.monotonic() if active else None
        high_run = 0
        last_report = 0.0
        best_rms = max(rms_levels) if rms_levels else 0.0
        best_ratio = max(tone_levels) if tone_levels else 0.0

        while True:
            check_cancelled()
            now = time.monotonic()
            elapsed = now - started_at
            if not active and elapsed >= start_timeout:
                _emit_debug(on_debug, "TIMEOUT", "No modem tone detected",
                            elapsed=f"{elapsed:.1f}s", max_rms=f"{best_rms:.6f}",
                            max_tone_ratio=f"{best_ratio:.3f}",
                            hint="Check the selected input device, volume, cabling, and that the peer is transmitting")
                raise TimeoutError(
                    "No modem signal detected. Check microphone input, system input volume, "
                    "and peer playback; enable Debug mode for the live signal meter."
                )
            if active and signal_started is not None and now - signal_started >= max_record_seconds:
                _emit_debug(on_debug, "CAPTURE", "Maximum capture duration reached",
                            seconds=f"{max_record_seconds:.1f}")
                break

            block, overflowed = stream.read(blocksize)
            if overflowed:
                _emit_debug(on_debug, "AUDIO", "Input overflow reported by PortAudio")
                raise RuntimeError("Audio input overflowed while capturing a modem frame; ask the peer to retry")
            mono = np.asarray(block, dtype=np.float32).reshape(-1)
            level = float(np.sqrt(np.mean(np.square(mono, dtype=np.float64))))
            tone_ratio = _tone_band_ratio(mono, rate, activity_frequencies)
            tone_active = level >= rms_floor and tone_ratio >= tone_threshold
            best_rms = max(best_rms, level)
            best_ratio = max(best_ratio, tone_ratio)

            if not active:
                pre_roll.append((mono.copy(), tone_active))
                if tone_active:
                    high_run += 1
                else:
                    high_run = 0
                if high_run >= 2:
                    buffered = list(pre_roll)
                    first_tone = next((i for i, (_samples, flag) in enumerate(buffered) if flag),
                                      max(0, len(buffered) - high_run))
                    keep_from = max(0, first_tone - 1)
                    chunks = [samples for samples, _flag in buffered[keep_from:]]
                    active = True
                    signal_started = time.monotonic()
                    quiet_blocks = 0
                    _emit_debug(on_debug, "DETECT", "Modem tone detected; preserving pre-roll and starting frame capture",
                                rms=f"{level:.6f}", tone_ratio=f"{tone_ratio:.3f}",
                                pre_roll_seconds=f"{len(chunks) * block_seconds:.2f}",
                                trimmed_noise_blocks=keep_from)
                elif now - last_report >= 0.75:
                    _emit_debug(on_debug, "LEVEL", "Microphone is listening",
                                rms=f"{level:.6f}", tone_ratio=f"{tone_ratio:.3f}",
                                peak=f"{float(np.max(np.abs(mono))) if mono.size else 0.0:.6f}")
                    last_report = now
                continue

            chunks.append(mono.copy())
            if not tone_active:
                quiet_blocks += 1
                if quiet_blocks >= quiet_blocks_required:
                    _emit_debug(on_debug, "CAPTURE", "End-of-frame silence detected",
                                trailing_silence=f"{quiet_blocks * block_seconds:.2f}s")
                    break
            else:
                quiet_blocks = 0
            if now - last_report >= 0.75:
                _emit_debug(on_debug, "LEVEL", "Receiving modem waveform",
                            rms=f"{level:.6f}", tone_ratio=f"{tone_ratio:.3f}",
                            elapsed=f"{now - signal_started:.1f}s")
                last_report = now

    if not chunks:
        raise TimeoutError("No acoustic frame detected")
    result = np.concatenate(chunks).astype(np.float32, copy=False)
    duration = result.size / rate
    _emit_debug(on_debug, "CAPTURE", "Audio frame captured",
                samples=result.size, duration=f"{duration:.3f}s",
                rms=f"{float(np.sqrt(np.mean(np.square(result, dtype=np.float64)))):.6f}",
                peak=f"{float(np.max(np.abs(result))):.6f}")
    if result.size < int(rate * 0.08):
        raise ValueError("Detected audio frame is too short")
    return result


class ContinuousAudioListener:
    """Continuously capture acoustic frames from one long-lived input stream.

    PortAudio's callback only copies blocks into a bounded queue. Signal
    analysis, burst segmentation and modem decoding run on worker threads, so
    the real-time callback never performs FFTs or protocol work.

    The listener detects the complete configured tone bank (including the
    ECHO-CRYPT hopping offsets), preserves pre-roll, and emits a frame only
    after a configurable quiet gap. It can be paused briefly while the local
    device transmits; this is necessary on half-duplex speaker/microphone rigs.
    """

    def __init__(self, rate, device=None, *, blocksize=1024,
                 activity_frequencies=(1800, 2400), bandwidth_hz=240.0,
                 tone_ratio_threshold=0.10, quiet_seconds=0.55,
                 pre_roll_seconds=0.65, max_frame_seconds=180.0,
                 queue_blocks=512, frame_queue_size=8, on_error=None,
                 on_debug=None):
        from queue import Queue

        if not isinstance(rate, int) or rate < 8000:
            raise ValueError("Sample rate must be an integer of at least 8000 Hz")
        if not isinstance(blocksize, int) or blocksize < 128:
            raise ValueError("Block size must be an integer of at least 128 samples")
        frequencies = tuple(sorted({float(x) for x in activity_frequencies}))
        if not frequencies or any(not 100 < f < rate / 2 for f in frequencies):
            raise ValueError("Activity frequencies must be inside the audio passband")
        if not 0.02 <= float(tone_ratio_threshold) <= 0.9:
            raise ValueError("Tone ratio threshold must be between 0.02 and 0.9")
        if not 0.1 <= float(quiet_seconds) <= 3.0:
            raise ValueError("Quiet gap must be between 0.1 and 3 seconds")
        if not 0.1 <= float(pre_roll_seconds) <= 3.0:
            raise ValueError("Pre-roll must be between 0.1 and 3 seconds")
        if not 1.0 <= float(max_frame_seconds) <= 600.0:
            raise ValueError("Maximum frame duration must be between 1 and 600 seconds")

        self.rate = rate
        self.device = device
        self.blocksize = blocksize
        self.activity_frequencies = frequencies
        self.bandwidth_hz = float(bandwidth_hz)
        self.tone_ratio_threshold = float(tone_ratio_threshold)
        self.quiet_seconds = float(quiet_seconds)
        self.pre_roll_seconds = float(pre_roll_seconds)
        self.max_frame_seconds = float(max_frame_seconds)
        self.on_error = on_error
        self.on_debug = on_debug
        self._blocks = Queue(maxsize=int(queue_blocks))
        self._frames = Queue(maxsize=int(frame_queue_size))
        self._errors = Queue(maxsize=32)
        self._stream = None
        self._stream_lock = threading.RLock()
        self._running = threading.Event()
        self._paused = threading.Event()
        self._busy = threading.Event()
        self._thread = None
        self._dropped_blocks = 0
        self._captured_frames = 0
        self._rejected_frames = 0
        self._metrics_lock = threading.Lock()
        self._last_level = {"rms": 0.0, "peak": 0.0, "tone_ratio": 0.0}
        self._sd = None

    @property
    def busy(self):
        """True while a modem-tone burst is currently being captured."""
        return self._busy.is_set()

    @property
    def paused(self):
        return self._paused.is_set()

    def snapshot(self):
        with self._metrics_lock:
            level = dict(self._last_level)
            dropped = self._dropped_blocks
            captured = self._captured_frames
            rejected = self._rejected_frames
        return {
            **level,
            "busy": self.busy,
            "paused": self.paused,
            "dropped_blocks": dropped,
            "captured_frames": captured,
            "rejected_frames": rejected,
        }

    def _notify_error(self, exc):
        _emit_debug(self.on_debug, "AUDIO", "Continuous listener reported an error",
                    error=type(exc).__name__, detail=str(exc))
        if self.on_error is not None:
            try:
                self.on_error(exc)
            except Exception:
                pass

    def _queue_error(self, message):
        try:
            self._errors.put_nowait(RuntimeError(str(message)))
        except Exception:
            pass

    def _audio_callback(self, indata, frames, time_info, status):
        if not self._running.is_set() or self._paused.is_set():
            return
        if status and getattr(status, "input_overflow", False):
            self._queue_error("PortAudio input overflow during continuous listening")
        try:
            block = np.asarray(indata[:, 0], dtype=np.float32).copy()
            self._blocks.put_nowait(block)
        except Exception:
            with self._metrics_lock:
                self._dropped_blocks += 1
            self._queue_error("Continuous audio block queue overflow; samples were dropped")

    def start(self):
        if self._running.is_set():
            return self
        # A timed-out stop must not allow a second collector to consume the
        # same queues. Join the previous worker before resetting shared state.
        previous_thread = self._thread
        if previous_thread is not None:
            if previous_thread is threading.current_thread():
                raise RuntimeError(
                    "Cannot restart the audio listener from its collector callback"
                )
            if previous_thread.is_alive():
                previous_thread.join(timeout=3.0)
            if previous_thread.is_alive():
                raise RuntimeError(
                    "Previous audio collector is still stopping; refusing to start a second collector"
                )
            self._thread = None
        for pending_queue in (self._blocks, self._frames, self._errors):
            while True:
                try:
                    pending_queue.get_nowait()
                except Exception:
                    break
        self._sd = _sounddevice()
        self._running.set()
        try:
            _emit_debug(self.on_debug, "AUDIO", "Starting continuous microphone listener",
                        sample_rate=self.rate, blocksize=self.blocksize,
                        device="SYSTEM DEFAULT" if self.device is None else self.device,
                        activity_frequencies=",".join(str(int(x)) for x in self.activity_frequencies),
                        tone_ratio_gate=f"{self.tone_ratio_threshold:.3f}",
                        quiet_seconds=f"{self.quiet_seconds:.3f}",
                        max_frame_seconds=f"{self.max_frame_seconds:.1f}")
            self._stream = self._sd.InputStream(
                samplerate=self.rate, channels=1, dtype="float32",
                blocksize=self.blocksize, device=self.device,
                callback=self._audio_callback,
            )
            self._stream.start()
            self._thread = threading.Thread(
                target=self._collector_loop, name="echocrypt-audio-listener", daemon=True
            )
            self._thread.start()
        except Exception as exc:
            self._running.clear()
            if self._stream is not None:
                try:
                    self._stream.close()
                except Exception:
                    pass
                self._stream = None
            explained = _explain_android_input_error(exc)
            if explained is exc:
                raise
            raise explained from exc
        return self

    def _collector_loop(self):
        from queue import Empty, Full

        block_seconds = self.blocksize / self.rate
        pre_roll_blocks = max(2, int(math.ceil(self.pre_roll_seconds / block_seconds)))
        quiet_blocks_required = max(1, int(math.ceil(self.quiet_seconds / block_seconds)))
        pre_roll = deque(maxlen=pre_roll_blocks)
        chunks = []
        active = False
        high_run = 0
        quiet_run = 0
        frame_started = 0.0
        noise_levels = deque(maxlen=24)
        rms_floor = 0.0005
        last_level_report = time.monotonic() - 1.0

        while self._running.is_set():
            while True:
                try:
                    self._notify_error(self._errors.get_nowait())
                except Empty:
                    break
            if self._paused.is_set():
                time.sleep(0.02)
                continue
            try:
                block = self._blocks.get(timeout=0.20)
            except Empty:
                continue
            if not self._running.is_set():
                break
            if block is None:
                # A local transmit interrupted the half-duplex capture window.
                # Never join samples from before and after that interruption.
                pre_roll.clear()
                chunks = []
                active = False
                high_run = quiet_run = 0
                self._busy.clear()
                _emit_debug(self.on_debug, "CAPTURE", "Discarded partial capture after local transmit pause")
                continue
            mono = np.asarray(block, dtype=np.float32).reshape(-1)
            if mono.size == 0:
                continue
            rms = float(np.sqrt(np.mean(np.square(mono, dtype=np.float64))))
            peak = float(np.max(np.abs(mono)))
            ratio = _tone_band_ratio(
                mono, self.rate, self.activity_frequencies,
                bandwidth_hz=self.bandwidth_hz,
            )
            with self._metrics_lock:
                self._last_level = {"rms": rms, "peak": peak, "tone_ratio": ratio}

            # Keep the always-on listener observable without flooding the log.
            # One level record per ~0.8 seconds is enough to diagnose wrong
            # devices, low input gain, and a modem signal the detector misses.
            now = time.monotonic()
            if now - last_level_report >= 0.80:
                _emit_debug(
                    self.on_debug, "LEVEL",
                    "Receiving modem waveform" if active else "Microphone is listening",
                    rms=f"{rms:.6f}", peak=f"{peak:.6f}",
                    tone_ratio=f"{ratio:.3f}",
                    noise_floor=f"{rms_floor:.6f}",
                    threshold=f"{self.tone_ratio_threshold:.3f}",
                    elapsed=f"{now - frame_started:.1f}s" if active else "idle",
                    dropped_blocks=self._dropped_blocks,
                )
                last_level_report = now

            if not active:
                # Estimate the room noise floor from non-modem blocks only.
                if ratio < self.tone_ratio_threshold:
                    noise_levels.append(rms)
                    if len(noise_levels) >= 6:
                        baseline = float(np.percentile(noise_levels, 35))
                        # Do not cap the measured room floor at -48 dBFS.
                        # On louder desktops this made ambient RMS look like
                        # modem energy whenever the spectral detector dipped.
                        rms_floor = max(0.0004, min(baseline * 1.35, 0.020))
                signal = rms >= rms_floor and ratio >= self.tone_ratio_threshold
                pre_roll.append(mono.copy())
                high_run = high_run + 1 if signal else 0
                if high_run >= 2:
                    prior = list(pre_roll)
                    # Retain enough signal before the trigger for the 64-symbol sync.
                    chunks = prior
                    active = True
                    self._busy.set()
                    frame_started = time.monotonic()
                    quiet_run = 0
                    _emit_debug(self.on_debug, "DETECT",
                                "Modem tone detected; preserving pre-roll and starting continuous capture",
                                rms=f"{rms:.6f}", peak=f"{peak:.6f}",
                                tone_ratio=f"{ratio:.3f}",
                                pre_roll_blocks=len(prior),
                                pre_roll_seconds=f"{len(prior) * block_seconds:.3f}",
                                activity_frequencies=",".join(str(int(x)) for x in self.activity_frequencies))
                continue

            chunks.append(mono.copy())
            # Once a modem burst has started, out-of-band room noise must
            # not keep it open just because its broadband RMS is high. Recent
            # real captures showed 500 Hz/ambient energy at RMS 0.02 while the
            # modem tone ratio was < 0.01; treating RMS alone as activity merged
            # adjacent Broadcast chunks into one undecodable capture.
            #
            # Preserve short spectral dropouts by allowing a bounded 450 ms
            # grace period only while broadband RMS remains elevated. Genuine
            # low-level silence still uses the configured quiet_seconds value.
            # Unlike the old RMS OR gate, this can never hold a burst open
            # indefinitely when the modem tones disappear.
            active_rms_floor = max(rms_floor * 1.25, 0.010)
            spectral_dropout_grace_blocks = max(
                quiet_blocks_required,
                int(math.ceil(0.45 / block_seconds)),
            )
            if ratio >= self.tone_ratio_threshold:
                quiet_run = 0
            else:
                quiet_run += 1

            required_quiet_blocks = (
                spectral_dropout_grace_blocks
                if rms >= active_rms_floor
                else quiet_blocks_required
            )
            elapsed = time.monotonic() - frame_started
            if quiet_run >= required_quiet_blocks or elapsed >= self.max_frame_seconds:
                end_reason = "trailing silence" if quiet_run >= quiet_blocks_required else "maximum frame duration"
                _emit_debug(self.on_debug, "CAPTURE", "Continuous audio burst ended",
                            reason=end_reason,
                            trailing_silence=f"{quiet_run * block_seconds:.3f}s",
                            elapsed=f"{elapsed:.3f}s",
                            chunks=len(chunks))
                try:
                    frame = np.concatenate(chunks).astype(np.float32, copy=False)
                    # A frame must contain enough samples for synchronization.
                    if frame.size >= int(self.rate * 0.08):
                        self._frames.put(frame, timeout=0.25)
                        with self._metrics_lock:
                            self._captured_frames += 1
                        frame_rms = float(np.sqrt(np.mean(np.square(frame, dtype=np.float64))))
                        frame_peak = float(np.max(np.abs(frame)))
                        clipping_fraction = float(np.mean(np.abs(frame) >= 0.999))
                        _emit_debug(self.on_debug, "CAPTURE", "Continuous audio frame queued for modem decoder",
                                    samples=frame.size, duration=f"{frame.size / self.rate:.3f}s",
                                    rms=f"{frame_rms:.6f}", peak=f"{frame_peak:.6f}",
                                    clipping_fraction=f"{clipping_fraction:.5f}",
                                    captured_frames=self._captured_frames,
                                    rejected_frames=self._rejected_frames)
                    else:
                        with self._metrics_lock:
                            self._rejected_frames += 1
                        _emit_debug(self.on_debug, "CAPTURE", "Rejected undersized continuous audio frame",
                                    samples=frame.size, minimum_samples=int(self.rate * 0.08))
                except Full:
                    self._notify_error(RuntimeError("Continuous listener frame queue is full; frame dropped"))
                    with self._metrics_lock:
                        self._rejected_frames += 1
                except Exception as exc:
                    self._notify_error(exc)
                chunks = []
                pre_roll.clear()
                high_run = quiet_run = 0
                active = False
                self._busy.clear()
                noise_levels.clear()

        self._busy.clear()

    def get_frame(self, timeout=None):
        return self._frames.get(timeout=timeout)

    def pause(self):
        """Stop input capture before local playback to prevent self-echo."""
        if not self._running.is_set() or self._paused.is_set():
            return
        self._paused.set()
        _emit_debug(self.on_debug, "AUDIO", "Microphone capture paused for local half-duplex playback")
        with self._stream_lock:
            if self._stream is not None:
                self._stream.stop()
        # Discard blocks captured just before stream shutdown; never combine
        # the tail of an old frame with samples captured after local playback.
        while True:
            try:
                self._blocks.get_nowait()
            except Exception:
                break
        try:
            self._blocks.put_nowait(None)
        except Exception:
            pass
        self._busy.clear()

    def resume(self):
        if not self._running.is_set() or not self._paused.is_set():
            return
        with self._stream_lock:
            if self._stream is not None:
                self._stream.start()
        self._paused.clear()
        _emit_debug(self.on_debug, "AUDIO", "Microphone capture resumed after local playback")

    def stop(self):
        if not self._running.is_set():
            return
        self._running.clear()
        _emit_debug(self.on_debug, "AUDIO", "Continuous microphone listener stopping")
        self._paused.clear()
        with self._stream_lock:
            stream, self._stream = self._stream, None
            if stream is not None:
                try:
                    stream.stop()
                finally:
                    stream.close()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            # Bound shutdown latency, but retain the reference if the worker
            # has not exited. start() will refuse a second collector until it
            # has joined, preventing concurrent consumers of the same queues.
            thread.join(timeout=3.0)
            if not thread.is_alive() and self._thread is thread:
                self._thread = None
        # If stop() runs inside a collector callback, keep the thread reference.
        # A later start() joins it before it can create a replacement worker.
        try:
            self._frames.put_nowait(None)
        except Exception:
            pass

    def __enter__(self):
        return self.start()

    def __exit__(self, exc_type, exc, tb):
        self.stop()
        return False
