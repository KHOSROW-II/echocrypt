"""Persistent application header: brand banner plus a responsive status dashboard."""
from __future__ import annotations

import shutil

from interface.theme import BANNER, dashboard as render_dashboard
from core.config import Config, load
from core.profile import get_username
from security.keystore import get_fingerprint
from core.audio import configured_device_names


def _audio_names(cfg):
    try:
        inp, out = configured_device_names(cfg.input_device, cfg.output_device, allow_install=False)
        if cfg.input_device is None:
            inp = f"System default: {inp}"
        if cfg.output_device is None:
            out = f"System default: {out}"
        return inp, out
    except Exception:
        return (
            "System default" if cfg.input_device is None else f"Device {cfg.input_device}",
            "System default" if cfg.output_device is None else f"Device {cfg.output_device}",
        )


def _compact_dashboard(identity, username, inp, out, cfg, columns, session_status):
    width = max(24, min(100, columns - 2))
    inner = width - 4

    def fit(text):
        text = str(text)
        if len(text) > inner:
            text = text[:max(1, inner - 1)] + "…"
        return text.ljust(inner)

    def row(text):
        return "│ " + fit(text) + " │"

    top = "┌" + "─" * (width - 2) + "┐"
    mid = "├" + "─" * (width - 2) + "┤"
    bottom = "└" + "─" * (width - 2) + "┘"
    key_state = "KEY PRESENT" if identity == "KEY PRESENT" else "NO KEY"
    lines = [
        top,
        row(f"IDENTITY  {username}  •  {key_state}  •  SESSION: {session_status}"),
        row(f"AUDIO  IN: {inp}  →  OUT: {out}"),
        row(f"TRANSPORT  BFSK {cfg.symbol_rate} baud  •  {cfg.mark_hz}/{cfg.space_hz} Hz"),
        bottom,
    ]
    return "\n".join(lines) + "\n"


def header_fragments(session_status="NOT CONNECTED", compact=None):
    try:
        cfg = load()
    except Exception:
        # Keep the application shell visible even when a user has damaged TOML.
        cfg = Config()
    try:
        identity = "KEY PRESENT" if get_fingerprint() else "NOT INITIALIZED"
    except Exception:
        identity = "IDENTITY ERROR"
    try:
        username = get_username()
    except Exception:
        username = "anon-000"
    inp, out = _audio_names(cfg)
    columns, rows = shutil.get_terminal_size((100, 30))
    if compact is None:
        compact = rows < 31
    if compact:
        # Menus and chat run in a full-screen terminal. On a common 24-row
        # terminal, a separate tagline would push the banner above the viewport.
        # Keep the session state inside the compact dashboard instead.
        board = _compact_dashboard(identity, username, inp, out, cfg, columns, session_status)
        return [
            ("class:brand", BANNER),
            ("class:dashboard", board),
        ]
    board = render_dashboard(identity, inp, out, cfg, session_status, username=username)
    return [
        ("class:brand", BANNER),
        ("class:muted", f"  {username}  •  SECURE ACOUSTIC TRANSPORT  •  {session_status}\n"),
        ("class:dashboard", board + "\n"),
    ]


def render(session_status="NOT CONNECTED"):
    """Plain terminal version used before line-oriented operations."""
    from interface.theme import clear_terminal
    clear_terminal()
    for _style, text in header_fragments(session_status=session_status, compact=True):
        print(text, end="")
