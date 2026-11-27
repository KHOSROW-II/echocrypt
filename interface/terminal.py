"""Full-screen, responsive arrow-key menu pages with persistent app header."""
from prompt_toolkit import Application
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import Layout
from prompt_toolkit.layout.containers import Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.styles import Style

MENU_STYLE = Style.from_dict({
    "brand": "bold #9a8058",
    "muted": "#777b80",
    "dashboard": "#96999b",
    "title": "bold #c0a16d",
    "selected": "bold #d6c4a1 bg:#292d32",
    "help": "#73777d",
})


def build_menu_fragments(title, items, index):
    lines = [("class:title", f"\n  ◆ {title}\n\n")]
    for i, (_value, label) in enumerate(items):
        prefix = "  › " if i == index else "    "
        style = "class:selected" if i == index else ""
        lines.append((style, prefix + label + "\n"))
    lines.append(("class:help", "\n  [↑/↓] Navigate   [ENTER] Select   [ESC] Back\n"))
    return lines


class Menu:
    def __init__(self, title, items, *, header=True, header_status="NOT CONNECTED",
                 input=None, output=None):
        self.title, self.items = title, list(items)
        self.header = bool(header)
        self.header_status = str(header_status)
        if not self.items:
            raise ValueError("Menu must contain at least one item")
        self.index = 0
        kb = KeyBindings()

        @kb.add("up")
        def _(event):
            self.index = (self.index - 1) % len(self.items)
            event.app.invalidate()

        @kb.add("down")
        def _(event):
            self.index = (self.index + 1) % len(self.items)
            event.app.invalidate()

        @kb.add("enter")
        def _(event):
            event.app.exit(result=self.items[self.index][0])

        @kb.add("escape")
        @kb.add("c-c")
        def _(event):
            event.app.exit(result=None)

        cached_header = []
        if self.header:
            from interface.dashboard import header_fragments
            try:
                cached_header = header_fragments(session_status=self.header_status, compact=True)
            except Exception as exc:
                cached_header = [("class:muted", f"ECHO-CRYPT status unavailable: {exc}\n")]

        self.header_fragments = list(cached_header)

        def draw():
            return list(self.header_fragments) + build_menu_fragments(self.title, self.items, self.index)

        self.app = Application(
            layout=Layout(Window(FormattedTextControl(draw), wrap_lines=True)),
            key_bindings=kb,
            full_screen=True,
            erase_when_done=True,
            style=MENU_STYLE,
            input=input,
            output=output,
        )
        # prompt_toolkit buffers a lone ESC while waiting to see whether it is
        # the prefix of an ANSI escape sequence. Its separate key-chord timeout
        # can add another delay. Keep both short for immediate Back/Exit.
        self.app.ttimeoutlen = 0.06
        self.app.timeoutlen = 0.12

    def run(self):
        return self.app.run()
