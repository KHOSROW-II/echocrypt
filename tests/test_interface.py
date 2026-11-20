import threading
import unittest
from concurrent.futures import ThreadPoolExecutor

from prompt_toolkit import Application
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from interface.chat import ChatScreen, SPINNER_FRAMES
from interface.terminal import MENU_STYLE, Menu, build_menu_fragments


class TerminalMenuTests(unittest.TestCase):
    def test_chat_spinner_has_many_states_without_arrow_decoration(self):
        self.assertEqual(len(SPINNER_FRAMES), 10)
        self.assertGreaterEqual(len(set(SPINNER_FRAMES)), 8)
        self.assertTrue(all("▷" not in frame for frame in SPINNER_FRAMES))
        self.assertTrue(all("⟦" not in frame and "⟧" not in frame for frame in SPINNER_FRAMES))

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

    def test_chat_history_snapshot_and_append_share_the_rx_lock(self):
        class TrackingLock:
            def __init__(self):
                self._lock = threading.RLock()
                self.entries = 0

            def __enter__(self):
                self._lock.acquire()
                self.entries += 1
                return self

            def __exit__(self, exc_type, exc, traceback):
                self._lock.release()

        messages = []
        shared_lock = TrackingLock()
        with create_pipe_input() as pipe:
            chat = ChatScreen(
                "peer", "local", messages,
                messages_lock=shared_lock,
                input=pipe, output=DummyOutput(),
            )
            before = shared_lock.entries

            def writer():
                for index in range(300):
                    chat._append_message("peer", f"message {index}")

            def reader():
                for _ in range(300):
                    snapshot = chat._message_snapshot(copy_entries=True)
                    self.assertTrue(all(
                        isinstance(view.get("body"), str) for _entry, view in snapshot
                    ))

            with ThreadPoolExecutor(max_workers=2) as pool:
                write_future = pool.submit(writer)
                read_future = pool.submit(reader)
                write_future.result(timeout=5)
                read_future.result(timeout=5)

            self.assertGreater(shared_lock.entries, before)
            self.assertLessEqual(len(messages), 80)
            self.assertEqual(messages[-1]["body"], "message 299")

    def test_menu_escape_timeouts_are_short(self):
        menu = Menu("MAIN MENU", [("back", "Back")])
        self.assertLessEqual(menu.app.ttimeoutlen, 0.08)
        self.assertLessEqual(menu.app.timeoutlen, 0.15)

    def test_invalid_empty_menu_is_rejected(self):
        with self.assertRaises(ValueError):
            Menu("EMPTY", [])


if __name__ == "__main__":
    unittest.main()
