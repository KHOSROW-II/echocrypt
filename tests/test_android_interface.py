"""Tests for the Android-specific terminal menu layer."""
import unittest

from prompt_toolkit.input import DummyInput
from prompt_toolkit.output import DummyOutput

from interface.android import AndroidMenu, activate
from interface import terminal


class AndroidMenuTests(unittest.TestCase):
    def test_menu_is_full_screen_and_mouse_enabled(self):
        menu = AndroidMenu(
            "ANDROID TEST",
            [("one", "First action"), ("back", "Back")],
            input=DummyInput(),
            output=DummyOutput(),
        )
        self.assertTrue(menu.app.full_screen)
        self.assertTrue(menu.app.mouse_support())
        self.assertEqual(menu.app.ttimeoutlen, 0.06)
        self.assertEqual(menu.app.timeoutlen, 0.12)

    def test_activate_replaces_shared_menu_class(self):
        original = terminal.Menu
        try:
            activate()
            self.assertIs(terminal.Menu, AndroidMenu)
        finally:
            terminal.Menu = original


if __name__ == "__main__":
    unittest.main()
