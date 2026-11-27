"""Interactive hardware checks for the currently configured audio devices."""
from __future__ import annotations

import shutil

from prompt_toolkit import Application
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import Layout
from prompt_toolkit.layout.containers import Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.styles import Style

from core.audio import MicrophoneLevelMonitor, test_speaker_channels, test_ultrasonic_path
from core.config import load
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
    print("\n  This plays two low-level near-ultrasonic tones and measures them through")
    print("  the selected microphone. You normally should not hear these tones.")
    print("  Keep the speaker and microphone in the same room and avoid changing volume.")
    print("  The result checks only this local speaker-to-microphone path.\n")
    try:
        result = test_ultrasonic_path(
            cfg.sample_rate,
            input_device=cfg.input_device,
            output_device=cfg.output_device,
            frequencies=(cfg.ultrasonic_mark_hz, cfg.ultrasonic_space_hz),
            duration=0.45,
            amplitude=0.16,
        )
        for tone in result["tones"]:
            status = "DETECTED" if tone["detected"] else "NOT DETECTED"
            print(
                f"  {tone['frequency']:.0f} Hz: {status} · "
                f"RMS {tone['rms']:.5f} · band ratio {tone['band_ratio']:.3f}"
            )
        if result["passed"]:
            print("\n  ✓ Both ultrasonic tones were detected on the local audio path.")
            print("  This does not guarantee that the remote peer supports them.")
            return True
        print("\n  ✗ The local speaker/microphone path did not reliably carry both tones.")
        print("  Keep ultrasonic mode disabled or use dedicated ultrasonic-capable audio hardware.")
        return False
    except Exception as exc:
        print(f"\n  ✗ Ultrasonic hardware test failed: {exc}")
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
