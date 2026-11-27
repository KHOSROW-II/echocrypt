"""Interactive hardware checks for the currently configured audio devices."""
from __future__ import annotations

import shutil

from prompt_toolkit import Application
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import Layout
from prompt_toolkit.layout.containers import Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.styles import Style

from core.audio import (
    MicrophoneLevelMonitor, test_speaker_channels, test_ultrasonic_path,
    test_acoustic_path,
)
from core.config import load
from core.virtual_audio import temporary_worker_test_route
from core.worker_validation import approve_worker_hardware
from core.modem import HOP_OFFSETS
from interface.dashboard import render as render_app_header

TEST_STYLE = Style.from_dict({
    "title": "bold #c0a16d",
    "muted": "#85898e",
    "bar": "#a58a60",
    "empty": "#3b3e42",
    "warning": "bold #b58b62",
    "good": "#9b9f91",
})


def run_speaker_test():
    cfg = load()
    render_app_header(session_status="SPEAKER TEST")
    print("\n  🔊 LEFT channel: a 2-second tone will play first.")
    print("  🔊 RIGHT channel: a 2-second tone will play second.\n")
    print("  Listen for the first tone only on your LEFT speaker/headphone,")
    print("  then the second tone only on your RIGHT speaker/headphone.\n")
    try:
        result = test_speaker_channels(cfg.sample_rate, cfg.output_device,
                                       duration=2.0, allow_install=True)
        print(f"  ✓ Playback test completed: {result['device']}")
        print("  Did you hear LEFT then RIGHT? If not, check OS channel routing / balance.")
        return True
    except Exception as exc:
        print(f"\n  ✗ Speaker test failed: {exc}")
        return False


def run_ultrasonic_hardware_test():
    cfg = load()
    render_app_header(session_status="ULTRASONIC LOOPBACK TEST")
    print("\n  This plays low-level near-ultrasonic tones across the full hopping range and measures them through")
    print("  the selected microphone. You normally should not hear these tones.")
    print("  Keep the speaker and microphone in the same room and avoid changing volume.")
    print("  The result checks only this local speaker-to-microphone path.\n")
    try:
        result = test_ultrasonic_path(
            cfg.sample_rate,
            input_device=cfg.input_device,
            output_device=cfg.output_device,
            frequencies=tuple(sorted({
                float(base + offset)
                for base in (cfg.ultrasonic_mark_hz, cfg.ultrasonic_space_hz)
                for offset in HOP_OFFSETS
            })),
            duration=0.30,
            amplitude=0.16,
        )
        for tone in result["tones"]:
            status = "DETECTED" if tone["detected"] else "NOT DETECTED"
            print(
                f"  {tone['frequency']:.0f} Hz: {status} · "
                f"RMS {tone['rms']:.5f} · band ratio {tone['band_ratio']:.3f}"
            )
        if result["passed"]:
            print("\n  ✓ All test frequencies were detected on the local audio path.")
            print("  This does not guarantee that the remote peer supports them.")
            return True
        print("\n  ✗ The local speaker/microphone path did not reliably carry the full hopping range.")
        print("  Keep ultrasonic mode disabled or use dedicated ultrasonic-capable audio hardware.")
        return False
    except Exception as exc:
        print(f"\n  ✗ Ultrasonic hardware test failed: {exc}")
        return False


def run_worker_hardware_test():
    """Run an independent modem-band loopback check before enabling workers."""
    cfg = load()
    render_app_header(session_status="WORKER HARDWARE VALIDATION")
    bases = (
        (cfg.ultrasonic_mark_hz, cfg.ultrasonic_space_hz)
        if cfg.ultrasonic_enabled else (cfg.mark_hz, cfg.space_hz)
    )
    frequencies = tuple(sorted({
        float(base + offset) for base in bases for offset in HOP_OFFSETS
    }))
    print("\\nDedicated test for the currently selected microphone and speaker.")
    print("Tests every active modem tone, including the configured frequency-hopping offsets.")
    print("Do not run during a live call. Keep a physical speaker audible to its microphone.")
    try:
        virtual_pair = (
            isinstance(cfg.input_device, str)
            and "ECHOCRYPT Virtual Speaker Monitor" in cfg.input_device
            and isinstance(cfg.output_device, str)
            and "ECHOCRYPT Virtual Microphone TX" in cfg.output_device
        )
        if virtual_pair:
            with temporary_worker_test_route():
                result = test_acoustic_path(
                    cfg.sample_rate, cfg.input_device, cfg.output_device,
                    frequencies=frequencies, duration=0.22, amplitude=0.08,
                )
        else:
            result = test_acoustic_path(
                cfg.sample_rate, cfg.input_device, cfg.output_device,
                frequencies=frequencies, duration=0.22, amplitude=0.10,
            )
        for tone in result["tones"]:
            state = "DETECTED" if tone["detected"] else "NOT DETECTED"
            print(f"  {tone['frequency']:.0f} Hz: {state} · band ratio {tone['band_ratio']:.3f}")
        if result["callback_status"]:
            print("  Audio callback status:", "; ".join(result["callback_status"]))
        if not result["passed"]:
            print("\\n✗ Hardware validation failed. Parallel workers remain disabled.")
            return False
        approval = approve_worker_hardware(cfg, result)
        print(
            f"\\n✓ Hardware path approved for the current device pair at {cfg.sample_rate} Hz "
            f"({approval['tone_count']} tones)."
        )
        print("Changing the microphone, speaker, sample rate or modem frequencies invalidates this approval.")
        print("This is a local audio-path check, not a test of the remote conferencing codec.")
        return True
    except Exception as exc:
        print("\\n✗ Worker hardware test failed:", exc)
        print("Parallel workers remain disabled until this dedicated test passes.")
        return False


def run_microphone_level_test():
    cfg = load()
    columns = max(48, shutil.get_terminal_size((80, 24)).columns)
    bar_width = max(16, min(42, columns - 24))
    monitor = None
    final_state = None

    def draw():
        if monitor is None:
            return [("class:muted", "Preparing microphone stream...\n")]
        state = monitor.snapshot()
        filled = int(round(bar_width * state["percent"] / 100.0))
        bar = "█" * filled + "░" * (bar_width - filled)
        if state["clipping"]:
            status_style, status = "class:warning", "CLIPPING — lower microphone gain"
        elif state["peak"] < 0.002:
            status_style, status = "class:warning", "NO SIGNAL — check mute / input device"
        else:
            status_style, status = "class:good", "SIGNAL DETECTED"
        return [
            ("class:title", "\n  ◉ MICROPHONE INPUT TEST\n\n"),
            ("class:muted", "  Speak normally, tap near the microphone, or make a short sound.\n"),
            ("class:muted", "  Audio is measured live and is NOT recorded or saved.\n\n"),
            ("", "  LEVEL  ["), ("class:bar", bar[:filled]),
            ("class:empty", bar[filled:]), ("", "] "),
            ("class:title", f"{state['percent']:5.1f}%\n\n"),
            ("", f"  RMS: {state['rms']:.5f}    Peak: {state['peak']:.5f}    "),
            ("", f"{state['dbfs']:.1f} dBFS\n"),
            (status_style, f"\n  {status}\n"),
            ("class:muted", f"  Stream: {state['status']}\n\n"),
            ("class:muted", "  [ESC / Q / ENTER] Stop microphone test\n"),
        ]

    kb = KeyBindings()

    @kb.add("escape")
    @kb.add("q")
    @kb.add("enter")
    @kb.add("c-c")
    def _quit(event):
        event.app.exit(result=True)

    app = Application(
        layout=Layout(Window(FormattedTextControl(draw), wrap_lines=True)),
        key_bindings=kb,
        full_screen=True,
        erase_when_done=True,
        style=TEST_STYLE,
        refresh_interval=0.10,
    )

    try:
        monitor = MicrophoneLevelMonitor(cfg.sample_rate, cfg.input_device,
                                         blocksize=1024, allow_install=True)
        with monitor:
            app.run()
            final_state = monitor.snapshot()
        render_app_header(session_status="MICROPHONE TEST COMPLETE")
        if final_state is not None:
            if final_state["peak"] < 0.002:
                print("\n  No meaningful microphone signal was detected.")
            elif final_state["clipping"]:
                print("\n  Input is active but clipping was detected; lower microphone gain.")
            else:
                print(f"\n  Microphone input detected: peak {final_state['peak']:.4f}, "
                      f"{final_state['dbfs']:.1f} dBFS.")
        return True
    except Exception as exc:
        render_app_header(session_status="MICROPHONE TEST ERROR")
        print(f"\n  ✗ Microphone test failed: {exc}")
        return False
