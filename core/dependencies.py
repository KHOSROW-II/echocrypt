"""Platform-aware, opt-in dependency installation for optional features."""
from __future__ import annotations

from pathlib import Path
import importlib
import subprocess
import sys
import os

ROOT = Path(__file__).resolve().parents[1]
MODE_FILE = ROOT / ".echocrypt-mode"


def install_mode() -> str:
    try:
        mode = MODE_FILE.read_text(encoding="utf-8").strip().lower()
    except OSError:
        mode = "manual"
    return mode if mode in {"full", "standby", "manual", "rootless"} else "manual"


def require_python_package(import_name: str, package_name: str, feature_name: str,
                           allow_install: bool = True):
    try:
        return importlib.import_module(import_name)
    except ImportError as first_error:
        mode = install_mode()
        if mode not in {"standby", "rootless"} or not allow_install:
            raise RuntimeError(
                f"The '{feature_name}' feature needs Python package '{package_name}'. "
                f"Install it in the active ECHO-CRYPT Python environment with:\n"
                f"  {sys.executable} -m pip install {package_name}\n"
                "On Termux, install native dependencies with pkg rather than pip."
            ) from first_error

        if not sys.stdin.isatty():
            raise RuntimeError(
                f"'{feature_name}' needs '{package_name}', but this session is non-interactive. "
                f"Install it with: {sys.executable} -m pip install {package_name}"
            ) from first_error

        is_termux = bool(os.environ.get("TERMUX_VERSION") or "com.termux" in os.environ.get("PREFIX", ""))
        target = "the Termux Python environment" if is_termux else "the active ECHO-CRYPT virtual environment"
        answer = input(
            f"Feature '{feature_name}' requires '{package_name}'. "
            f"Install it into {target} now? [Y/n]: "
        ).strip().lower()
        if answer not in {"", "y", "yes"}:
            raise RuntimeError(f"'{feature_name}' was not started because its dependency was declined.")
        subprocess.check_call([sys.executable, "-m", "pip", "install", package_name])
        importlib.invalidate_caches()
        return importlib.import_module(import_name)


def offer_codec2_install() -> bool:
    """In Standby mode, offer the optional Linux Codec2 runtime."""
    import ctypes.util
    import platform
    import re

    if ctypes.util.find_library("codec2"):
        return True
    if install_mode() != "standby":
        return False
    if platform.system() != "Linux" or not sys.stdin.isatty():
        return False

    answer = input(
        "Emergency speech can use Codec2 700C for much smaller audio bursts. "
        "Install the optional system libcodec2 runtime now? [Y/n]: "
    ).strip().lower()
    if answer not in {"", "y", "yes"}:
        return False
    if not shutil_which("apt-cache"):
        print("APT is unavailable; install the Codec2 runtime with your distribution's package manager.")
        return False

    try:
        result = subprocess.run(["apt-cache", "pkgnames"], check=True, capture_output=True, text=True)
        candidates = sorted({name for name in result.stdout.splitlines()
                             if re.fullmatch(r"libcodec2-[0-9][A-Za-z0-9.+~-]*", name)})
        if not candidates:
            print("No libcodec2 package is present in the current APT indexes. Refresh package indexes or install it manually.")
            return False
        package = candidates[-1]
        print(f"Installing optional system package: {package}")
        command = ["apt-get", "install", "-y", package]
        if hasattr(os, "geteuid") and os.geteuid() != 0:
            command.insert(0, "sudo")
        subprocess.check_call(command)
        return bool(ctypes.util.find_library("codec2"))
    except (OSError, subprocess.CalledProcessError) as exc:
        print(f"Codec2 installation failed: {exc}")
        return False


def shutil_which(command: str):
    from shutil import which
    return which(command)


def offer_portaudio_install() -> bool:
    """Offer the native PortAudio runtime using the active platform's package manager."""
    import ctypes.util
    import platform

    if ctypes.util.find_library("portaudio"):
        return True
    mode = install_mode()
    if mode not in {"full", "standby", "rootless"} or not sys.stdin.isatty():
        return False

    is_termux = bool(os.environ.get("TERMUX_VERSION") or "com.termux" in os.environ.get("PREFIX", ""))
    if is_termux:
        if not shutil_which("pkg"):
            return False
        answer = input("Termux audio needs PortAudio and PulseAudio. Install them now with pkg? [Y/n]: ").strip().lower()
        if answer not in {"", "y", "yes"}:
            return False
        try:
            subprocess.check_call([shutil_which("pkg"), "install", "-y", "portaudio", "pulseaudio", "libffi"])
            return bool(ctypes.util.find_library("portaudio"))
        except (OSError, subprocess.CalledProcessError) as exc:
            print(f"Termux audio runtime installation failed: {exc}")
            return False

    if mode == "rootless":
        print("Rootless mode will not invoke a host package manager. Install a user-accessible PortAudio runtime yourself if the environment does not provide one.")
        return False
    if platform.system() != "Linux":
        return False
    answer = input(
        "The audio binding is present but the PortAudio runtime is missing. "
        "Install libportaudio2 now? [Y/n]: "
    ).strip().lower()
    if answer not in {"", "y", "yes"}:
        return False
    if not shutil_which("apt-get"):
        print("APT is unavailable; install PortAudio using your distribution's package manager.")
        return False
    command = ["apt-get", "install", "-y", "libportaudio2"]
    if hasattr(os, "geteuid") and os.geteuid() != 0:
        if not shutil_which("sudo"):
            print("sudo is unavailable; install libportaudio2 with your administrator account.")
            return False
        command.insert(0, "sudo")
    try:
        subprocess.check_call(command)
        return bool(ctypes.util.find_library("portaudio"))
    except (OSError, subprocess.CalledProcessError) as exc:
        print(f"PortAudio installation failed: {exc}")
        return False

