"""Touch-friendly, full-screen terminal menus for Android/Termux.

The application remains a terminal program. This module only replaces menu
navigation on Android; communication, audio, security and protocol code stay
shared with the desktop interface.
"""
from __future__ import annotations

import shutil

from prompt_toolkit import Application
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import Layout
from prompt_toolkit.layout.containers import Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.mouse_events import MouseEventType
from prompt_toolkit.styles import Style

ANDROID_STYLE = Style.from_dict({
    "brand": "bold #c5a875",
    "muted": "#8b9096",
    "title": "bold #d5bd91",
    "selected": "bold #f0dfbd bg:#34383e",
    "button": "bold #d8c39b bg:#252a30",
    "help": "#92979d",
})

def _width() -> int:
    return max(28, min(72, shutil.get_terminal_size((40, 24)).columns))

class AndroidMenu:
    """Large tap targets plus arrow/Enter controls for narrow phone terminals."""

    def __init__(self, title, items, *, header=True,
                 header_status="NOT CONNECTED", input=None, output=None):
        self.title = str(title)
        self.items = list(items)
        if not self.items:
            raise ValueError("Menu must contain at least one item")
        self.index = 0
        kb = KeyBindings()

        @kb.add("up")
        @kb.add("k")
        def _up(event):
            self.index = (self.index - 1) % len(self.items)
            event.app.invalidate()

        @kb.add("down")
        @kb.add("j")
        def _down(event):
            self.index = (self.index + 1) % len(self.items)
            event.app.invalidate()

        @kb.add("enter")
        def _select(event):
            event.app.exit(result=self.items[self.index][0])

        @kb.add("escape")
        @kb.add("c-c")
        def _back(event):
            event.app.exit(result=None)

        def draw():
            width = _width()
            inner = width - 4
            lines = [
                ("class:brand", "  ECHOCRYPT  /  ANDROID\n"),
                ("class:muted", f"  {header_status[:inner]}\n" if header else ""),
                ("class:title", f"\n  {self.title[:inner]}\n\n"),
            ]
            for index, (value, label) in enumerate(self.items):
                selected = index == self.index
                marker = "  ● " if selected else "  ○ "
                text = str(label)
                available = max(8, inner - 4)
                if len(text) > available:
                    text = text[:available - 1] + "…"
                # Extra vertical padding makes each row a practical touch target.
                row = f"  {marker}{text.ljust(available)}  \n"
                lines.append(("class:button" if selected else "", "\n"))
                lines.append(("class:button" if selected else "", row, self._click(index)))
                lines.append(("class:button" if selected else "", "  " + " " * (inner - 2) + "\n"))
            lines.extend([
                ("class:help", "\n  TAP A ROW  ·  ↑/↓ MOVE  ·  ENTER SELECT\n"),
                ("class:help", "  ESC BACK\n"),
            ])
            return lines

        self.app = Application(
            layout=Layout(Window(FormattedTextControl(draw), wrap_lines=True)),
            key_bindings=kb,
            full_screen=True,
            erase_when_done=True,
            style=ANDROID_STYLE,
            input=input,
            output=output,
            mouse_support=True,
        )
        # Keep Back responsive while still allowing ANSI sequences to complete.
        self.app.ttimeoutlen = 0.06
        self.app.timeoutlen = 0.12

    def _click(self, index):
        def handler(event):
            if event.event_type == MouseEventType.MOUSE_UP:
                self.index = index
                event.app.invalidate()
                event.app.exit(result=self.items[index][0])
        return handler

    def run(self):
        return self.app.run()


def activate() -> None:
    """Install Android menus before interface.menu imports its Menu symbol."""
    from interface import terminal
    terminal.Menu = AndroidMenu
