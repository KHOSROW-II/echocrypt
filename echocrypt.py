"""ECHO-CRYPT self-bootstrapping launcher.

Run ``python echocrypt.py``. On first launch it selects and installs a bootstrap
profile; no separate shell/PowerShell bootstrap script is required.
"""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys
import venv

ROOT = Path(__file__).resolve().parent
VENV_DIR = ROOT / ".venv"
MODE_FILE = ROOT / ".echocrypt-mode"
VALID_MODES = ("full", "standby", "manual", "rootless")


def _venv_python() -> Path:
    if os.name == "nt":
        return VENV_DIR / "Scripts" / "python.exe"
    return VENV_DIR / "bin" / "python"


def _is_termux() -> bool:
    """Identify the native Termux Python, not a PRoot child inheriting env vars."""
    termux_root = "/data/data/com.termux/"
    runtime_paths = (str(Path(sys.executable).resolve()), str(Path(sys.prefix).resolve()))
    python_is_termux = any(path.startswith(termux_root) for path in runtime_paths)
    environment_hints_termux = bool(
        os.environ.get("TERMUX_VERSION")
        or os.environ.get("PREFIX", "").startswith(termux_root)
    )
    return python_is_termux and environment_hints_termux


def _is_android_runtime() -> bool:
    return bool(
        _is_termux()
        or os.environ.get("ANDROID_ROOT")
        or os.environ.get("ANDROID_DATA")
        or Path("/system/bin/app_process").exists()
    )


def _inside_project_venv() -> bool:
    try:
        return Path(sys.prefix).resolve() == VENV_DIR.resolve() and sys.prefix != sys.base_prefix
    except OSError:
        return False


def _saved_mode() -> str | None:
    try:
        value = MODE_FILE.read_text(encoding="utf-8").strip().lower()
    except OSError:
        return None
    return value if value in VALID_MODES else None


def _save_mode(mode: str) -> None:
    MODE_FILE.write_text(mode + "\n", encoding="utf-8")


def _choose_mode(explicit: str | None = None, *, ignore_saved: bool = False) -> str:
    requested = explicit.strip().lower() if explicit else None
    if requested and requested not in VALID_MODES:
        raise ValueError(f"Unknown bootstrap mode: {explicit}")

    # Android runtime detection must outrank a saved desktop profile. A project
    # copied from Windows/Linux into Termux must never inherit Full/Standby.
    if _is_termux():
        if requested and requested != "rootless":
            raise RuntimeError("Termux supports only '--bootstrap rootless'.")
        return "rootless"
    if _is_android_runtime() and requested is None:
        return "rootless"
    if requested:
        return requested

    saved = None if ignore_saved else _saved_mode()
    if saved:
        return saved
    if not sys.stdin.isatty():
        raise RuntimeError(
            "ECHO-CRYPT has not been bootstrapped. Run interactively with "
            "'python echocrypt.py', or select a profile with "
            "'python echocrypt.py --bootstrap full|standby|manual|rootless'."
        )
    print("\nECHO-CRYPT bootstrap profiles")
    print("  1) Full              Core + audio Python dependencies")
    print("  2) Standby           Core now; optional audio setup on demand")
    print("  3) Manual            Create .venv only; install packages yourself")
    print("  4) Rootless (mobile) No sudo/system installs; Termux-aware setup")
    choice = input("Select profile [1-4]: ").strip()
    modes = {"1": "full", "2": "standby", "3": "manual", "4": "rootless"}
    if choice not in modes:
        raise ValueError("Invalid bootstrap selection.")
    return modes[choice]


def _run(command: list[str], *, check: bool = True) -> subprocess.CompletedProcess:
    print("\n> " + " ".join(command), flush=True)
    return subprocess.run(command, cwd=ROOT, check=check)


def _ensure_project_venv(system_site_packages: bool = False) -> Path:
    """Create or repair the project venv and verify its interpreter and pip."""
    python_path = _venv_python()

    def interpreter_works() -> bool:
        if not python_path.is_file():
            return False
        try:
            probe = subprocess.run(
                [str(python_path), "-c", "import sys, pip; print(sys.executable)"],
                cwd=ROOT, capture_output=True, text=True, check=False,
            )
        except OSError:
            return False
        return probe.returncode == 0

    if interpreter_works():
        return python_path

    if VENV_DIR.exists() or VENV_DIR.is_symlink():
        print("Existing ECHO-CRYPT .venv is incomplete; rebuilding it...")
        if VENV_DIR.is_symlink():
            VENV_DIR.unlink()
        elif VENV_DIR.is_dir():
            shutil.rmtree(VENV_DIR)
        else:
            VENV_DIR.unlink()
    print("Creating ECHO-CRYPT .venv...")
    venv.EnvBuilder(with_pip=True, clear=True,
                    system_site_packages=system_site_packages).create(VENV_DIR)
    if not interpreter_works():
        raise RuntimeError("The project virtual environment was created, but its Python/pip self-test failed.")
    return python_path


def _install_requirements(python_path: Path, *, audio: bool) -> None:
    _run([str(python_path), "-m", "pip", "install", "-r", str(ROOT / "requirements.txt")])
    if audio:
        _run([str(python_path), "-m", "pip", "install", "-r", str(ROOT / "requirements-audio.txt")])


def _probe_termux_live_input_stream() -> tuple[bool, str]:
    """Open and close a real PortAudio microphone stream, not just enumerate devices."""
    script = """import sounddevice as sd
stream = None
try:
    stream = sd.InputStream(
        samplerate=48000,
        channels=1,
        dtype="float32",
        blocksize=1024,
    )
    stream.start()
    print("ECHOCRYPT_INPUT_STREAM=OK")
except Exception as exc:
    print("ECHOCRYPT_INPUT_STREAM_ERROR=" + str(exc))
    raise SystemExit(4)
finally:
    if stream is not None:
        try:
            stream.stop()
        finally:
            stream.close()
"""
    try:
        probe = subprocess.run(
            [sys.executable, "-c", script],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)
    if probe.returncode == 0 and "ECHOCRYPT_INPUT_STREAM=OK" in probe.stdout:
        return True, ""
    details = (probe.stderr or probe.stdout).strip()[-1200:]
    return False, details or "PortAudio did not confirm that the input stream opened"


def _verify_termux_audio_runtime() -> tuple[int, int]:
    """Check device enumeration and verify that PortAudio can open live input."""
    input_devices, output_devices = _probe_termux_python_audio()
    print(f"PortAudio loaded successfully: {input_devices} input device(s), {output_devices} output device(s).")
    if input_devices:
        live_ok, detail = _probe_termux_live_input_stream()
        if live_ok:
            print("Live microphone InputStream opened successfully.")
        else:
            print("PortAudio lists an input device, but could not open the live microphone stream:")
            print(detail)
            print("Open Configuration > Audio settings > Request Android microphone permission.")
    if input_devices < 1 or output_devices < 1:
        print("WARNING: ECHO-CRYPT cannot yet confirm both microphone and speaker devices on this device.")
        print("Check the Termux:API companion app, Android microphone permission, and audio hardware.")
    return input_devices, output_devices


def _bootstrap_termux_rootless() -> None:
    """Install Termux-native wheels and Python tooling without venv or root."""
    pkg = shutil.which("pkg")
    if not pkg:
        raise RuntimeError("Termux was detected but the 'pkg' package manager is unavailable.")
    native_packages = [
        # Termux separates pip from the Python package on current releases.
        "python-pip", "python-numpy", "python-cryptography",
        "portaudio", "termux-api", "libffi", "clang", "make", "pkg-config",
    ]
    _run([pkg, "install", "-y", *native_packages])
    # Install the Termux-side Termux:API CLI. Its Android companion app is
    # installed separately and requests RECORD_AUDIO. Termux PortAudio uses
    # its native Android host API; a PulseAudio daemon is not required here.
    # Never upgrade pip.
    _run([sys.executable, "-m", "pip", "install", "--disable-pip-version-check",
          "prompt_toolkit>=3.0.43", "sounddevice>=0.4.6"])
    _verify_termux_audio_runtime()


CORE_IMPORTS = ("numpy", "cryptography", "prompt_toolkit")
TERMUX_IMPORTS = (*CORE_IMPORTS, "sounddevice")


def _missing_imports(python_executable: Path, names=CORE_IMPORTS) -> list[str]:
    """Import-probe dependencies in the target interpreter, not just metadata."""
    script = (
        "import importlib; names=" + repr(list(names)) + "; missing=[]; "
        "\nfor n in names:\n try: importlib.import_module(n)\n except Exception: missing.append(n)"
        "; [print('ECHOCRYPT_MISSING=' + name) for name in missing]"
    )
    try:
        probe = subprocess.run([str(python_executable), "-c", script],
                               cwd=ROOT, capture_output=True, text=True, check=False)
    except OSError:
        return list(names)
    if probe.returncode != 0:
        return list(names)
    return [line.split("=", 1)[1].strip() for line in probe.stdout.splitlines()
            if line.startswith("ECHOCRYPT_MISSING=")]


def _probe_sounddevice(python_executable: Path) -> tuple[bool, int, int, str]:
    """Probe the Python binding, PortAudio shared library and visible devices."""
    script = """import sounddevice as sd
print("PORTAUDIO=" + str(sd.get_portaudio_version()))
try:
    devices = sd.query_devices()
except Exception as exc:
    print("DEVICE_QUERY_ERROR=" + str(exc))
    devices = []
print("INPUTS=" + str(sum(int(d.get("max_input_channels", 0)) > 0 for d in devices)))
print("OUTPUTS=" + str(sum(int(d.get("max_output_channels", 0)) > 0 for d in devices)))
"""
    try:
        probe = subprocess.run([str(python_executable), "-c", script], cwd=ROOT,
                               capture_output=True, text=True, check=False)
    except OSError as exc:
        return False, 0, 0, str(exc)
    if probe.returncode != 0:
        details = (probe.stderr or probe.stdout).strip()[-1000:]
        return False, 0, 0, details or "sounddevice import or PortAudio loading failed"
    values = {}
    for line in probe.stdout.splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            values[key] = value
    if "PORTAUDIO" not in values:
        return False, 0, 0, "PortAudio returned no version information"
    device_error = values.get("DEVICE_QUERY_ERROR", "")
    return True, int(values.get("INPUTS", "0")), int(values.get("OUTPUTS", "0")), device_error


def _probe_termux_python_audio() -> tuple[int, int]:
    """Verify Termux core imports and PortAudio binding; report hardware separately."""
    missing = _missing_imports(Path(sys.executable), TERMUX_IMPORTS)
    if missing:
        raise RuntimeError("Termux dependencies failed import self-test: " + ", ".join(missing))
    ok, inputs, outputs, detail = _probe_sounddevice(Path(sys.executable))
    if not ok:
        raise RuntimeError("Termux PortAudio self-test failed: " + detail)
    if detail:
        print("PortAudio loaded, but device enumeration failed:", detail)
    return inputs, outputs


def _manual_setup_instructions(python_executable: Path) -> None:
    print("\nManual profile created the virtual environment but installed no packages.")
    print(f"Install core dependencies with:\n  {python_executable} -m pip install -r requirements.txt")
    print("Then run 'python echocrypt.py' again.")

def _bootstrap(mode: str) -> None:
    if _is_termux() and mode != "rootless":
        raise RuntimeError("Termux must use Rootless (mobile) mode; conventional venv/desktop bootstrap profiles are not supported there.")
    if mode == "rootless" and _is_termux():
        _bootstrap_termux_rootless()
        _save_mode(mode)
        print("\nRootless (mobile) bootstrap completed in the Termux Python environment.")
        return

    # Generic rootless/PRoot/mobile Linux uses only an unprivileged project venv.
    python_path = _ensure_project_venv()
    if mode == "manual":
        _save_mode(mode)
        _manual_setup_instructions(python_path)
        return
    if mode == "standby":
        _install_requirements(python_path, audio=False)
        _save_mode(mode)
        print("Standby profile ready. Audio dependencies are installed only when requested.")
    elif mode in {"full", "rootless"}:
        _install_requirements(python_path, audio=True)
        missing_core = _missing_imports(python_path, CORE_IMPORTS)
        if missing_core:
            raise RuntimeError("Core dependency self-test failed: " + ", ".join(missing_core))
        audio_ok, inputs, outputs, audio_detail = _probe_sounddevice(python_path)
        _save_mode(mode)
        if audio_ok and inputs and outputs:
            print(f"Audio self-test passed: {inputs} input device(s), {outputs} output device(s).")
        else:
            reason = audio_detail or "no usable input/output devices were enumerated"
            print("WARNING: Python dependencies installed, but audio I/O is not ready:", reason)
            print("Install a PortAudio runtime for this environment and check device permissions.")
        if mode == "rootless":
            print("Rootless (mobile) profile ready. No privileged/system package commands were run.")
            print("Audio hardware still depends on an audio backend exposed by the host/PRoot environment.")
        else:
            print("Full profile ready. A native PortAudio runtime may still be required on Linux.")
    else:
        raise ValueError(f"Unsupported bootstrap mode: {mode}")


def _usage() -> str:
    return (
        "Usage: python echocrypt.py [--bootstrap [full|standby|manual|rootless]]\n"
        "       python echocrypt.py --help\n"
        "The first normal launch also opens the bootstrap selector."
    )


def _parse_bootstrap_args(argv: list[str]) -> tuple[str | None, list[str], bool]:
    args = list(argv)
    if "--help" in args or "-h" in args:
        print(_usage())
        raise SystemExit(0)
    if "--bootstrap" in args:
        index = args.index("--bootstrap")
        selected = None
        if index + 1 < len(args) and args[index + 1] in VALID_MODES:
            selected = args.pop(index + 1)
        args.pop(index)
        return selected, args, True
    # Backward-compatible alias for prior bootstrap documentation.
    if "--mode" in args:
        index = args.index("--mode")
        if index + 1 >= len(args):
            raise ValueError("--mode requires a profile name")
        selected = args[index + 1]
        del args[index:index + 2]
        return selected, args, True
    return None, args, False


def main():
    try:
        explicit_mode, app_args, bootstrap_requested = _parse_bootstrap_args(sys.argv[1:])
        sys.argv = [sys.argv[0], *app_args]

        mode = _choose_mode(
            explicit_mode,
            ignore_saved=bootstrap_requested and explicit_mode is None,
        )
        if mode == "rootless" and _is_termux():
            # Re-run package setup only when dependencies/profile are missing.
            # The audio service is refreshed on every launch because Android may
            # stop PulseAudio while ECHO-CRYPT is not running.
            missing = _missing_imports(Path(sys.executable), TERMUX_IMPORTS)
            termux_api_cli_missing = shutil.which("termux-microphone-record") is None
            needs_bootstrap = (
                bootstrap_requested
                or bool(missing)
                or termux_api_cli_missing
                or _saved_mode() != "rootless"
            )
            if not needs_bootstrap:
                try:
                    _verify_termux_audio_runtime()
                except RuntimeError as exc:
                    print("Termux audio/dependency self-test failed; repairing Rootless setup:", exc)
                    needs_bootstrap = True
            if needs_bootstrap:
                _bootstrap(mode)
            try:
                from interface.menu import main as app_main
            except ModuleNotFoundError as exc:
                print(f"ECHO-CRYPT dependency/module error: {exc}", file=sys.stderr)
                raise SystemExit(2)
            app_main()
            return

        if bootstrap_requested or _saved_mode() is None:
            _bootstrap(mode)
            if mode == "manual":
                raise SystemExit(0)

        if not _inside_project_venv():
            python_path = _venv_python()
            if not python_path.exists():
                _bootstrap(mode)
                if mode == "manual":
                    raise SystemExit(0)
            missing = _missing_imports(python_path)
            if missing:
                if mode == "manual":
                    _manual_setup_instructions(python_path)
                    raise SystemExit(2)
                print("The selected ECHO-CRYPT environment is incomplete; re-running bootstrap.")
                _bootstrap(mode)
                missing = _missing_imports(python_path)
                if missing:
                    raise RuntimeError("Bootstrap finished but core packages are still missing: " + ", ".join(missing))
            os.execv(str(python_path), [str(python_path), str(Path(__file__).resolve()), *sys.argv[1:]])
            raise SystemExit("Could not re-execute ECHO-CRYPT inside .venv.")

        missing = _missing_imports(_venv_python(), CORE_IMPORTS)
        if missing:
            if mode == "manual":
                _manual_setup_instructions(_venv_python())
                raise SystemExit(2)
            raise RuntimeError("Core packages are missing from the active venv: " + ", ".join(missing))

        try:
            from interface.menu import main as app_main
        except ModuleNotFoundError as exc:
            print(f"ECHO-CRYPT dependency/module error: {exc}", file=sys.stderr)
            print("Re-run the built-in setup with: python echocrypt.py --bootstrap", file=sys.stderr)
            raise SystemExit(2)
        app_main()
    except (OSError, subprocess.CalledProcessError, RuntimeError, ValueError) as exc:
        print(f"ECHO-CRYPT bootstrap failed: {exc}", file=sys.stderr)
        print(_usage(), file=sys.stderr)
        raise SystemExit(2)


if __name__ == "__main__":
    main()
