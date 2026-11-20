"""Responsive terminal theme and dashboard rendering."""
import os
import shutil
import sys

BANNER = r"""  ______ _____ _    _  ____     _____ _______     _______ _______
 |  ____/ ____| |  | |/ __ \   / ____|  __ \ \   / /  __ \__   __|
 | |__ | |    | |__| | |  | | | |    | |__) \ \_/ /| |__) | | |
 |  __|| |    |  __  | |  | | | |    |  _  / \   / |  ___/  | |
 | |___| |____| |  | | |__| | | |____| | \ \  | |  | |      | |
 |______\_____|_|  |_|\____/   \_____|_|  \_\ |_|  |_|      |_|
"""


def clear_terminal():
    """Clear old output before drawing the banner, on Windows and Unix terminals."""
    if not sys.stdout.isatty():
        return
    command = "cls" if os.name == "nt" else "clear"
    if os.name != "nt" and not os.environ.get("TERM"):
        print("\033[2J\033[H", end="", flush=True)
        return
    result = os.system(command)
    if result != 0:
        # ANSI fallback for terminals where the shell clear utility is unavailable.
        print("\033[2J\033[H", end="", flush=True)


def _truncate(value, width):
    value = str(value)
    if width <= 0:
        return ""
    if len(value) <= width:
        return value
    if width == 1:
        return "…"
    return value[:width - 1] + "…"


def dashboard(identity, inp, out, cfg=None, session_status="NOT CONNECTED", username="anon-000"):
    values = [
        ("Status       : ", identity),
        ("Username     : ", username),
        ("Encryption   : ", "X25519 / ChaCha20-Poly1305"),
        ("Session      : ", session_status),
        ("Input        : ", inp),
        ("Output       : ", out),
        ("Codec        : ", "TEXT / ADPCM / C2 VOICE"),
        ("Modem        : ", f"BFSK / {cfg.symbol_rate} baud" if cfg else "BFSK"),
        ("Frequency    : ", f"{cfg.mark_hz} / {cfg.space_hz} Hz" if cfg else "1800 / 2400 Hz"),
        ("FEC          : ", "Hamming / Repetition-3"),
        ("Hopping      : ", "KEYED FHSS (SESSION)"),
        ("Hybrid       : ", "DUAL-BAND FDM (TEST)"),
    ]
    terminal_columns = shutil.get_terminal_size((100, 30)).columns
    max_box_width = max(24, min(110, terminal_columns - 2))
    wanted = max([len("IDENTITY"), len("AUDIO"), len("TRANSPORT")] +
                 [len(label) + len(str(value)) for label, value in values]) + 6
    width = min(max_box_width, max(24, wanted))
    content_width = width - 4

    def border(left, fill, right):
        return left + fill * (width - 2) + right

    def line(text=""):
        return "│ " + _truncate(text, content_width).ljust(content_width) + " │"

    def field(label, value):
        available = max(1, content_width - len(label))
        return line(label + _truncate(value, available))

    lines = [border("┌", "─", "┐"), line("IDENTITY")]
    lines.extend(field(label, value) for label, value in values[:4])
    lines.append(border("├", "─", "┤"))
    lines.append(line("AUDIO"))
    lines.extend(field(label, value) for label, value in values[4:7])
    lines.append(border("├", "─", "┤"))
    lines.append(line("TRANSPORT"))
    lines.extend(field(label, value) for label, value in values[7:])
    lines.append(border("└", "─", "┘"))
    return "\n".join(lines)
