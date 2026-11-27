"""Arrow-key configuration screens for ECHO-CRYPT."""
from prompt_toolkit import prompt

from core.audio import list_devices
from core.config import load, save_setting
from core.profile import get_username, set_username
from core.virtual_audio import (
    setup_virtual_audio, remove_virtual_audio,
    is_virtual_rx_device, is_virtual_tx_device,
)
from core.worker_validation import worker_hardware_approval_status
from interface.dashboard import render as render_app_header
from interface.terminal import Menu
from interface.audio_tests import (
    run_microphone_level_test, run_speaker_test, run_ultrasonic_hardware_test,
    run_worker_hardware_test,
)


def _pause():
    prompt("\nPress Enter to return...")


def _edit_numeric(section, key, attr, label, cast):
    cfg = load()
    render_app_header(session_status="CONFIGURATION")
    current = getattr(cfg, attr)
    raw = prompt(f"{label} [{current}] (Enter keeps current): ").strip()
    if not raw:
        return
    try:
        value = cast(raw)
        save_setting(section, key, value)
        print(f"Saved {label}: {value}")
    except (ValueError, TypeError, OverflowError) as exc:
        print(f"Invalid value: {exc}")
    _pause()


def _select_device(direction: str):
    section_key = "input_device" if direction == "input" else "output_device"
    channel_key = 2 if direction == "input" else 3
    title = "INPUT DEVICE" if direction == "input" else "OUTPUT DEVICE"
    render_app_header(session_status="AUDIO CONFIGURATION")
    try:
        devices = list_devices()
        filtered = [item for item in devices if item[channel_key] > 0]
    except Exception as exc:
        print(f"Could not enumerate audio devices: {exc}")
        _pause()
        return

    items = [("default", "Use system default (recommended)")]
    items.extend((str(index), f"Device {index}: {name} | IN {ins} | OUT {outs}")
                 for index, name, ins, outs in filtered)
    items.append(("back", "Back"))
    selected = Menu(title, items).run()
    if selected in (None, "back"):
        return
    render_app_header(session_status="SAVE AUDIO DEVICE")
    value = "default" if selected == "default" else int(selected)
    try:
        save_setting("audio", "full_duplex", False)
        save_setting("audio", section_key, value)
        print(f"Saved {direction} device: {'System default' if value == 'default' else value}")
    except Exception as exc:
        print(f"Could not save audio device: {exc}")
    _pause()


def _request_termux_android_microphone():
    from core.termux_api import request_microphone_permission

    render_app_header(session_status="ANDROID MICROPHONE PERMISSION")
    print("\nThis uses Termux:API to request Android's RECORD_AUDIO permission.")
    print("A one-second test recording is created temporarily and deleted automatically.")
    try:
        status, detail = request_microphone_permission()
        if detail:
            print("\n" + detail)
        if status == "permission_required":
            prompt("\nAllow microphone access in Android. If no dialog appears, enable Microphone for Termux:API in Android Settings, return here, then press Enter to retry...")
            status, detail = request_microphone_permission()
            if detail:
                print("\n" + detail)
        if status == "granted":
            print("\nPermission check succeeded. Testing the live PortAudio microphone stream...")
            run_microphone_level_test()
        else:
            print("\nAndroid microphone permission is not yet confirmed.")
            _pause()
    except Exception as exc:
        print("\nCould not request microphone permission through Termux:API:", exc)
        print("Install both the 'termux-api' package and the matching Termux:API Android app.")
        _pause()


def _setup_virtual_audio():
    render_app_header(session_status="VIRTUAL AUDIO SETUP")
    print("\nCreating isolated ECHOCRYPT virtual endpoints through PipeWire-Pulse / PulseAudio.")
    print("The system default microphone and speaker are not changed.")
    try:
        endpoints = setup_virtual_audio(
            sample_rate=load().sample_rate,
            monitor_to_physical_output=True,
        )
        save_setting("audio", "input_device", endpoints["input_device"])
        save_setting("audio", "output_device", endpoints["output_device"])
        save_setting("audio", "full_duplex", True)
        print("\nECHOCRYPT endpoints configured:")
        print("  RX input  :", endpoints["input_device"])
        print("  TX output :", endpoints["output_device"])
        print("\nIn your call/browser application choose:")
        print("  Microphone:", endpoints["application_microphone"])
        print("  Speaker   :", endpoints["application_speaker"])
        print("\nFull-duplex mode is enabled only for these isolated endpoints.")
        if endpoints.get("loopback_warning"):
            print("\nAudio-monitoring warning:", endpoints["loopback_warning"])
    except Exception as exc:
        print("\nVirtual audio setup failed:", exc)
    _pause()


def _setup_browser_tab_audio():
    render_app_header(session_status="BROWSER TAB AUDIO SETUP")
    print("\nPreparing the isolated Chromium tab-audio input.")
    print("Only the browser tab captured by the ECHOCRYPT extension will enter RX.")
    print("The extension forwards captured PCM locally; other applications are not routed.")
    try:
        cfg = load()
        if cfg.ultrasonic_enabled:
            raise RuntimeError(
                "Disable Ultrasonic acoustic mode before using browser-tab capture. "
                "Internet call audio is not a transparent ultrasonic channel."
            )
        endpoints = setup_virtual_audio(
            sample_rate=cfg.sample_rate,
            monitor_to_physical_output=False,
        )
        save_setting("audio", "input_device", endpoints["browser_tab_input_device"])
        save_setting("audio", "output_device", endpoints["output_device"])
        save_setting("audio", "full_duplex", True)
        print("\nECHOCRYPT routing configured:")
        print("  RX input  :", endpoints["browser_tab_input_device"])
        print("  TX output :", endpoints["output_device"])
        print("\nIn the meeting website, select ECHOCRYPT Virtual Microphone as its microphone.")
        print("Leave the website's speaker output on its ordinary system/default output.")
        print("Then load browser_extension/ as an unpacked Chromium extension and register its native host.")
        print("See docs/BROWSER_TAB_CAPTURE.md for installation.")
    except Exception as exc:
        print("\nBrowser-tab audio setup failed:", exc)
    _pause()


def _remove_virtual_audio():
    render_app_header(session_status="REMOVE VIRTUAL AUDIO")
    try:
        cfg = load()
        result = remove_virtual_audio()
        using_virtual = (
            is_virtual_rx_device(cfg.input_device)
            or is_virtual_tx_device(cfg.output_device)
        )
        if using_virtual:
            save_setting("audio", "full_duplex", False)
            save_setting("audio", "input_device", "default")
            save_setting("audio", "output_device", "default")
        print("Unloaded ECHOCRYPT-owned virtual audio modules:", result["removed_module_ids"])
        for error in result["errors"]:
            print("Cleanup warning:", error)
    except Exception as exc:
        print("Could not remove virtual audio endpoints:", exc)
    _pause()


def _audio_settings():
    while True:
        cfg = load()
        c = Menu("AUDIO SETTINGS", [
            ("input", f"Input device           : {'SYSTEM DEFAULT' if cfg.input_device is None else cfg.input_device}"),
            ("output", f"Output device          : {'SYSTEM DEFAULT' if cfg.output_device is None else cfg.output_device}"),
            ("speaker-test", "Test speakers (LEFT then RIGHT, 2 seconds each)"),
            ("mic-test", "Test microphone (live level meter)"),
            ("ultrasonic-test", "Test ultrasonic speaker → microphone path"),
            ("virtual-setup", "Create / configure generic ECHOCRYPT virtual microphone + speaker (Linux)"),
            ("browser-tab-setup", "Use Chromium selected-tab capture as ECHOCRYPT RX"),
            ("virtual-remove", "Remove ECHOCRYPT virtual audio endpoints"),
            ("android-mic", "Request Android microphone permission (Termux:API)"),
            ("sample", f"Sample rate            : {cfg.sample_rate} Hz"),
            ("record", f"Maximum record window  : {cfg.record_seconds:g} sec"),
            ("gain", f"Playback gain           : {cfg.gain:.2f}"),
            ("back", "Back"),
        ]).run()
        if c in (None, "back"):
            return
        if c == "input":
            _select_device("input")
        elif c == "output":
            _select_device("output")
        elif c == "speaker-test":
            run_speaker_test()
            _pause()
        elif c == "mic-test":
            run_microphone_level_test()
        elif c == "ultrasonic-test":
            run_ultrasonic_hardware_test()
            _pause()
        elif c == "virtual-setup":
            _setup_virtual_audio()
        elif c == "browser-tab-setup":
            _setup_browser_tab_audio()
        elif c == "virtual-remove":
            _remove_virtual_audio()
        elif c == "android-mic":
            _request_termux_android_microphone()
        elif c == "sample":
            _edit_numeric("audio", "sample_rate", "sample_rate", "Sample rate (48000 or 96000 recommended)", int)
        elif c == "record":
            _edit_numeric("audio", "record_seconds", "record_seconds", "Record window in seconds", float)
        elif c == "gain":
            _edit_numeric("audio", "gain", "gain", "Audio gain (0.05 to 0.95)", float)


def _modem_settings():
    fields = [
        ("symbol_rate", "symbol_rate", "Robust symbol rate (baud)"),
        ("fast_symbol_rate", "fast_symbol_rate", "Fast symbol rate (baud)"),
        ("voice_symbol_rate", "voice_symbol_rate", "Voice-note symbol rate (baud)"),
        ("emergency_symbol_rate", "emergency_symbol_rate", "Emergency PTT symbol rate (baud)"),
        ("mark_hz", "mark_hz", "BFSK MARK frequency (Hz)"),
        ("space_hz", "space_hz", "BFSK SPACE frequency (Hz)"),
        ("ultrasonic_mark_hz", "ultrasonic_mark_hz", "Ultrasonic MARK frequency (>20 kHz)"),
        ("ultrasonic_space_hz", "ultrasonic_space_hz", "Ultrasonic SPACE frequency (>20 kHz)"),
    ]
    while True:
        cfg = load()
        menu_items = [(key, f"{label}: {getattr(cfg, attr)}") for key, attr, label in fields]
        menu_items.insert(
            0,
            ("ultrasonic_enabled",
             f"Ultrasonic acoustic mode: {'ON' if cfg.ultrasonic_enabled else 'OFF'} (toggle)"),
        )
        menu_items.append(("back", "Back"))
        choice = Menu("MODEM SETTINGS", menu_items).run()
        if choice in (None, "back"):
            return
        if choice == "ultrasonic_enabled":
            enabled = not load().ultrasonic_enabled
            if enabled:
                print("\nRun the local ultrasonic speaker-to-microphone check before enabling this mode.")
                if not run_ultrasonic_hardware_test():
                    print("Ultrasonic mode was NOT enabled. The local audio path did not pass its test.")
                    _pause()
                    continue
            try:
                save_setting("modem", "ultrasonic_enabled", enabled)
                print(
                    "Ultrasonic acoustic mode is now "
                    f"{'ENABLED' if enabled else 'DISABLED'}. "
                    "Both peers must enable it and use suitable audio hardware."
                )
            except Exception as exc:
                print("Could not update ultrasonic setting:", exc)
            _pause()
            continue
        key, attr, label = next(row for row in fields if row[0] == choice)
        _edit_numeric("modem", key, attr, label, int)


def _transfer_settings():
    while True:
        cfg = load()
        approved, approval_reason = worker_hardware_approval_status(cfg)
        approval_label = "APPROVED" if approved else "NOT APPROVED"
        choice = Menu("DIRECT TRANSFER SETTINGS", [
            ("worker-hardware-test", "Run dedicated microphone/speaker modem-path test"),
            ("worker-hardware-status", f"Hardware gate: {approval_label} — {approval_reason}"),
            ("multi_worker_enabled",
             f"Parallel file/audio preparation: {'ON' if cfg.multi_worker_enabled else 'OFF'} (toggle)"),
            ("multi_worker_count", f"Maximum preparation workers: {cfg.multi_worker_count} (2-8)"),
            ("back", "Back"),
        ]).run()
        if choice in (None, "back"):
            return
        if choice == "worker-hardware-test":
            run_worker_hardware_test()
            _pause()
        elif choice == "worker-hardware-status":
            print("\nWorker hardware approval:", approval_label)
            print(approval_reason)
            _pause()
        elif choice == "multi_worker_enabled":
            enabled = not load().multi_worker_enabled
            if enabled:
                approved, reason = worker_hardware_approval_status(load())
                if not approved:
                    print("\nParallel workers were NOT enabled.")
                    print("Run the dedicated hardware test first:", reason)
                    _pause()
                    continue
            try:
                save_setting("transfer", "multi_worker_enabled", enabled)
                print(
                    "Parallel Direct preparation is now "
                    f"{'ENABLED' if enabled else 'DISABLED'}. "
                    "It is available only when both peers negotiate an authenticated Direct session."
                )
            except Exception as exc:
                print("Could not update transfer setting:", exc)
            _pause()
        elif choice == "multi_worker_count":
            _edit_numeric(
                "transfer", "multi_worker_count", "multi_worker_count",
                "Parallel preparation workers (2 to 8)", int,
            )


def _protocol_settings():
    fields = [
        ("max_payload", "max_payload", "Maximum acoustic packet payload (bytes)", int),
        ("ack_timeout_seconds", "ack_timeout_seconds", "ACK timeout (seconds)", float),
        ("max_retries", "max_retries", "Maximum retries (0 to 10)", int),
        ("listen_timeout_seconds", "listen_timeout_seconds", "Initial receive timeout (seconds)", float),
        ("retry_listen_seconds", "retry_listen_seconds", "Retry receive window (seconds)", float),
    ]
    while True:
        cfg = load()
        menu_items = [(key, f"{label}: {getattr(cfg, attr)}") for key, attr, label, _cast in fields]
        menu_items.append(("back", "Back"))
        choice = Menu("PROTOCOL SETTINGS", menu_items).run()
        if choice in (None, "back"):
            return
        key, attr, label, cast = next(row for row in fields if row[0] == choice)
        _edit_numeric("protocol", key, attr, label, cast)


def _profile_settings():
    while True:
        username = get_username()
        choice = Menu("PROFILE SETTINGS", [
            ("username", f"Username: {username}  [edit]"),
            ("back", "Back"),
        ]).run()
        if choice in (None, "back"):
            return
        if choice == "username":
            render_app_header(session_status="PROFILE SETTINGS")
            raw = prompt("New username (2-24 letters/numbers/_/-; blank cancels): ").strip()
            if not raw:
                continue
            try:
                print("Saved username:", set_username(raw))
            except ValueError as exc:
                print("Invalid username:", exc)
            _pause()


def settings_menu():
    while True:
        cfg = load()
        choice = Menu("CONFIGURATION", [
            ("profile", "Profile / username"),
            ("audio", "Audio settings"),
            ("debug", f"Acoustic debug logging: {'ON' if cfg.debug_enabled else 'OFF'} (toggle)"),
            ("debug-log", "View latest acoustic debug log"),
            ("modem", "Modem and frequency settings"),
            ("transfer", "Direct transfer and worker settings"),
            ("protocol", "Protocol, ACK and retry settings"),
            ("defaults", "Reset input/output to system defaults"),
            ("back", "Back"),
        ]).run()
        if choice in (None, "back"):
            return
        if choice == "profile":
            _profile_settings()
        elif choice == "audio":
            _audio_settings()
        elif choice == "modem":
            _modem_settings()
        elif choice == "transfer":
            _transfer_settings()
        elif choice == "protocol":
            _protocol_settings()
        elif choice == "debug":
            render_app_header(session_status="ACOUSTIC DEBUG SETTINGS")
            enabled = not load().debug_enabled
            try:
                save_setting("debug", "enabled", enabled)
                print(f"Acoustic debug logging is now {'ENABLED' if enabled else 'DISABLED'}.")
                if enabled:
                    print("Diagnostic events are saved in the background only; no live trace is printed in chat or over the interface.")
                    print("Open 'View latest acoustic debug log' to inspect logs/acoustic-debug.log. Payload contents and keys are never logged.")
            except Exception as exc:
                print("Could not update debug setting:", exc)
            _pause()
        elif choice == "debug-log":
            from pathlib import Path
            render_app_header(session_status="ACOUSTIC DEBUG LOG")
            log_path = Path(__file__).resolve().parents[1] / "logs" / "acoustic-debug.log"
            if log_path.exists():
                lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
                print("\n".join(lines[-80:]) if lines else "Debug log is empty.")
            else:
                print("No acoustic debug log exists yet. Enable debug and perform a receive attempt first.")
            _pause()
        elif choice == "defaults":
            render_app_header(session_status="RESET AUDIO DEFAULTS")
            try:
                save_setting("audio", "input_device", "default")
                save_setting("audio", "output_device", "default")
                print("Input and output now follow the operating system's default audio devices.")
            except Exception as exc:
                print("Could not reset audio devices:", exc)
            _pause()
