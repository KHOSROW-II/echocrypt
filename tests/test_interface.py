import unittest

from prompt_toolkit import Application
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from interface.terminal import MENU_STYLE, Menu, build_menu_fragments


class TerminalMenuTests(unittest.TestCase):
    def test_fragments_put_style_before_visible_text(self):
        items = [("start", "Start Communication"), ("exit", "Exit")]
        fragments = build_menu_fragments("MAIN MENU", items, 0)

        self.assertEqual(fragments[0], ("class:title", "\n  ◆ MAIN MENU\n\n"))
        self.assertEqual(fragments[1], ("class:selected", "  › Start Communication\n"))
        self.assertEqual(fragments[2], ("", "    Exit\n"))
        self.assertEqual(fragments[-1][0], "class:help")

    def test_all_menu_style_tokens_are_valid(self):
        items = [("start", "Start Communication"), ("exit", "Exit")]
        fragments = build_menu_fragments("MAIN MENU", items, 0)
        # Asking prompt_toolkit to resolve each non-empty style catches invalid
        # style/color strings before opening a full-screen terminal UI.
        for style, _text in fragments:
            if style:
                MENU_STYLE.get_attrs_for_style_str(style)

    def test_menu_renders_and_enter_selects_item(self):
        with create_pipe_input() as pipe:
            menu = Menu("MAIN MENU", [("start", "Start Communication"), ("exit", "Exit")])
            app = Application(
                layout=menu.app.layout,
                key_bindings=menu.app.key_bindings,
                full_screen=True,
                style=MENU_STYLE,
                input=pipe,
                output=DummyOutput(),
            )
            pipe.send_text("\r")
            self.assertEqual(app.run(), "start")

    def test_invalid_empty_menu_is_rejected(self):
        with self.assertRaises(ValueError):
            Menu("EMPTY", [])


if __name__ == "__main__":
    unittest.main()
