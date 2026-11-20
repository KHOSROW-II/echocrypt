"""Interactive terminal chat UI with one-line activity, clickable actions and PTT."""
from __future__ import annotations

import asyncio
from datetime import datetime
import shutil
import threading
import textwrap
import time

from prompt_toolkit.application import Application
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.filters import has_focus, Condition
from prompt_toolkit.layout import Layout
from prompt_toolkit.layout.containers import HSplit, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.styles import Style
from prompt_toolkit.mouse_events import MouseEventType
from prompt_toolkit.widgets import Frame, TextArea

from interface.dashboard import header_fragments

CHAT_STYLE = Style.from_dict({
    "brand": "bold #9a8058", "muted": "#777b80", "dashboard": "#96999b",
    "chat.title": "bold #c0a16d", "chat.peer": "bold #a9b7aa",
    "chat.me": "bold #c5a976", "chat.body": "#c3c5c6",
    "chat.system": "italic #81878d", "chat.input": "#d6d1c5",
    "chat.activity": "bold #c0a16d", "chat.listen": "#89948b",
    "chat.footer": "#b7a17a", "chat.button": "bold #c6ae81",
    "chat.retry": "bold #d8ad72", "chat.error": "bold #d17c70",
})

# Three circles chase one another inside a hollow arrow housing. The status
# occupies exactly one terminal row; frames replace each other in place.
SPINNER_FRAMES = (
    "⟦ ● ○ ○ ▷ ⟧", "⟦ ○ ● ○ ▷ ⟧", "⟦ ○ ○ ● ▷ ⟧",
    "⟦ ○ ○ ◉ ▷ ⟧", "⟦ ○ ◉ ○ ▷ ⟧", "⟦ ◉ ○ ○ ▷ ⟧",
)
CHAT_FOOTER_TEXT = (
    "  [Enter] Send   [F2] Attach file   [F4] Voice note\n"
    "  [Space hold] Push-to-talk   [F5] Emergency PTT   [F6] More   [F7] Cancel   [F9] Play voice   [Esc] Disconnect\n"
)


class ChatScreen:
    """Full-screen chat. All toolbar commands are clickable and keyboard-bound.

    `on_ptt_start` and `on_ptt_stop` own the microphone lifecycle. For terminal
    keyboards without key-release events, Space uses key-repeat/idle detection;
    mouse PTT uses actual mouse-down/mouse-up events.
    """

    def __init__(self, peer_name: str, username: str, messages=None, *,
                 on_send=None, activity_provider=None, on_retry=None,
                 on_play_voice=None,
                 on_ptt_start=None, on_ptt_stop=None, on_ptt_cancel=None,
                 on_cancel=None, messages_lock=None, input=None, output=None):
        self.peer_name = str(peer_name)
        self.username = str(username)
        self.messages = messages if messages is not None else []
        self.messages_lock = messages_lock if messages_lock is not None else threading.RLock()
        self.on_send = on_send
        self.activity_provider = activity_provider
        self.on_retry = on_retry
        self.on_play_voice = on_play_voice
        self.on_ptt_start = on_ptt_start
        self.on_ptt_stop = on_ptt_stop
        self.on_ptt_cancel = on_ptt_cancel
        self.on_cancel = on_cancel
        self._ptt_active = False
        self._ptt_source = None
        self._ptt_last_key = 0.0
        self._ptt_seen_repeat = False
        self._ptt_watcher_running = False
        self._ptt_lock = False
        with self.messages_lock:
            if len(self.messages) > 80:
                recent = list(self.messages[-80:])
                recent_ids = {id(entry) for entry in recent}
                protected = [
                    entry for entry in self.messages[:-80]
                    if entry.get("cancelable") and callable(entry.get("cancel_action"))
                    and id(entry) not in recent_ids
                ]
                self.messages[:] = protected + recent

        self.input = TextArea(height=1, prompt="  Message  › ", multiline=False,
                              wrap_lines=False, style="class:chat.input")
        self.result = None
        kb = KeyBindings()

        def stop_ptt(*, discard=False):
            if not self._ptt_active or self._ptt_lock:
                return
            self._ptt_lock = True
            self._ptt_active = False
            source = self._ptt_source
            self._ptt_source = None
            self._app.invalidate()

            async def finish_capture():
                try:
                    callback = self.on_ptt_cancel if discard else self.on_ptt_stop
                    if callback is not None:
                        await asyncio.to_thread(callback)
                except Exception as exc:
                    self._append_message("system", f"Voice note failed: {exc}", "VOICE ERROR")
                finally:
                    self._ptt_lock = False
                    self._app.invalidate()
            self._app.create_background_task(finish_capture())

        self._stop_ptt = stop_ptt

        def start_ptt(source, app=None):
            if self._ptt_active:
                if source == "keyboard" and self._ptt_source == "keyboard":
                    self._ptt_seen_repeat = True
                    self._ptt_last_key = time.monotonic()
                return
            if self._ptt_lock:
                return
            try:
                if self.on_ptt_start is None:
                    raise RuntimeError("Push-to-talk is unavailable")
                self.on_ptt_start()
                self._ptt_active = True
                self._ptt_source = source
                self._ptt_last_key = time.monotonic()
                self._ptt_seen_repeat = False
                self._app.invalidate()
                if source == "keyboard" and app is not None and not self._ptt_watcher_running:
                    self._ptt_watcher_running = True
                    app.create_background_task(watch_keyboard_hold())
            except Exception as exc:
                self._append_message("system", f"Could not start voice recording: {exc}", "VOICE ERROR")
                self._app.invalidate()

        async def watch_keyboard_hold():
            try:
                while self._ptt_active and self._ptt_source == "keyboard":
                    await asyncio.sleep(0.06)
                    quiet = time.monotonic() - self._ptt_last_key
                    # Most terminal emulators emit repeated Space key events once
                    # the OS keyboard-repeat delay elapses. After that, a short
                    # idle gap approximates physical key release.
                    release_after = 0.34 if self._ptt_seen_repeat else 0.78
                    if quiet >= release_after:
                        stop_ptt()
                        break
            finally:
                self._ptt_watcher_running = False

        self._start_ptt = start_ptt

        def finish(action, payload=None):
            if self._ptt_active:
                stop_ptt(discard=True)
            self.result = (action, payload)
            self.input.buffer.reset()
            self._app.exit(result=self.result)

        self._finish = finish

        def submit_message(text=None):
            value = str(self.input.text if text is None else text).strip()
            if not value:
                return
            if self.on_send is None:
                finish("send", value)
                return
            self.input.buffer.reset()
            try:
                self.on_send(value)
            except Exception as exc:
                self._append_message("system", f"Could not queue message: {exc}", "SEND FAILED")
            self._app.invalidate()

        self.submit_message = submit_message

        @kb.add("enter", filter=has_focus(self.input))
        def _(event):
            submit_message()

        @kb.add("f2")
        def _(event): finish("attach")

        def toggle_ptt(app=None):
            if self.on_ptt_start is None:
                finish("voice")
            elif self._ptt_active:
                stop_ptt()
            else:
                start_ptt("toggle", app or self._app)

        self.toggle_ptt = toggle_ptt

        def cancel_voice_action():
            if self._ptt_active:
                stop_ptt(discard=True)
                return
            if self.on_cancel is not None:
                try:
                    self.on_cancel()
                except Exception as exc:
                    self._append_message("system", f"Could not cancel voice operation: {exc}", "CANCEL FAILED")
                self._app.invalidate()

        self.cancel_voice_action = cancel_voice_action

        @kb.add("f4")
        def _(event):
            toggle_ptt(event.app)

        @kb.add(" ", filter=Condition(lambda: not self.input.text.strip()))
        def _(event):
            start_ptt("keyboard", event.app)

        @kb.add("f7")
        def _(event): cancel_voice_action()

        @kb.add("f5")
        def _(event): finish("emergency")

        @kb.add("f6")
        def _(event): finish("more")

        @kb.add("f8")
        def _(event):
            target = next(
                (entry for entry, view in reversed(self._message_snapshot(copy_entries=True))
                 if view.get("retry_request")), None
            )
            if target is not None:
                self._invoke_retry(target)

        @kb.add("f9")
        def _(event):
            target = next(
                (entry for entry, view in reversed(self._message_snapshot(copy_entries=True))
                 if view.get("voice_path")), None
            )
            if target is not None:
                self._invoke_play_voice(target)

        @kb.add("escape")
        @kb.add("c-c")
        def _(event): finish("disconnect")

        self._app = None
        cached_header = header_fragments(session_status=f"CONNECTED: {self.peer_name}", compact=True)

        def header():
            return cached_header

        def mouse_retry(entry):
            def handler(mouse_event):
                if mouse_event.event_type == MouseEventType.MOUSE_UP:
                    self._invoke_retry(entry)
            return handler

        def mouse_cancel(entry):
            def handler(mouse_event):
                if mouse_event.event_type == MouseEventType.MOUSE_UP:
                    with self.messages_lock:
                        action = entry.get("cancel_action")
                    if callable(action):
                        action()
                    self._app.invalidate()
            return handler

        def mouse_play_voice(entry):
            def handler(mouse_event):
                if mouse_event.event_type == MouseEventType.MOUSE_UP:
                    self._invoke_play_voice(entry)
            return handler

        def history_fragments():
            width = max(48, shutil.get_terminal_size((100, 24)).columns)
            inner = max(18, min(68, int((width - 8) * 0.68)))
            snapshot = self._message_snapshot(copy_entries=True)
            if not snapshot:
                return [
                    ("class:chat.system", "  No messages yet.\n"),
                    ("class:chat.system", "  The acoustic listener is active in the background.\n"),
                    ("class:chat.system", "  Type a message and press Enter; attach files with F2.\n"),
                ]
            output = [("class:chat.title", "  CONVERSATION\n")]
            recent = snapshot[-24:]
            recent_ids = {id(entry) for entry, _view in recent}
            protected = [
                (entry, view) for entry, view in snapshot[:-24]
                if view.get("cancelable") and callable(view.get("cancel_action"))
                and id(entry) not in recent_ids
            ]
            for entry, view in protected + recent:
                speaker = view.get("speaker", "system")
                body = str(view.get("body", ""))
                stamp = str(view.get("time", ""))
                status = str(view.get("status", ""))
                if speaker == "system":
                    output.append(("class:chat.system", f"  • {stamp}  {body}\n"))
                    if status:
                        output.append(("class:muted", f"      {status}\n"))
                    continue
                owner = self.username if speaker == "me" else self.peer_name
                style = "class:chat.me" if speaker == "me" else "class:chat.peer"
                lines = []
                for paragraph in body.splitlines() or [""]:
                    lines.extend(textwrap.wrap(paragraph, width=inner,
                        replace_whitespace=False, drop_whitespace=True,
                        break_long_words=True, break_on_hyphens=False) or [""])
                lines = lines[:30]
                pad = max(2, width - min(width - 2, inner + 6)) if speaker == "me" else 2
                output.append((style, f"{' ' * pad}╭─ {owner}  {stamp}\n"))
                for line in lines:
                    output.append((style, f"{' ' * pad}│ {line}\n"))
                if status:
                    output.append(("class:muted", f"{' ' * pad}╰─ {status}\n"))
                else:
                    output.append((style, f"{' ' * pad}╰─\n"))
                retry = view.get("retry_request")
                if retry and self.on_retry is not None:
                    output.append(("class:chat.retry", f"{' ' * pad}↻  Request resend [F8]\n", mouse_retry(entry)))
                if view.get("voice_path") and self.on_play_voice is not None:
                    output.append(("class:chat.button", f"{' ' * pad}▶  Play voice [F9]\n", mouse_play_voice(entry)))
                if view.get("cancelable") and callable(view.get("cancel_action")):
                    output.append(("class:chat.error", f"{' ' * pad}×  Cancel sending [F7]\n", mouse_cancel(entry)))
            return output

        def activity_fragments():
            data = None
            if self.activity_provider is not None:
                try:
                    data = self.activity_provider()
                except Exception:
                    data = None
            if isinstance(data, dict):
                label = str(data.get("label", "LISTENING"))
                progress = str(data.get("progress", ""))
                detail = str(data.get("detail", ""))
            elif isinstance(data, tuple):
                label = str(data[0]) if data else "LISTENING"
                progress = str(data[2]) if len(data) > 2 and data[2] else ""
                detail = ""
            else:
                label, progress, detail = "LISTENING · READY", "", ""
            frame = SPINNER_FRAMES[int(time.monotonic() * 7) % len(SPINNER_FRAMES)]
            parts = [frame, f"  {label}"]
            if progress:
                parts.append(progress)
            if detail:
                parts.append(detail)
            return [("class:chat.activity", "  ".join(parts)[:max(30, shutil.get_terminal_size((100, 24)).columns - 4)] + "\n")]

        def action_fragment(label, callback, *, ptt=False):
            if ptt:
                def handler(mouse_event):
                    if mouse_event.event_type == MouseEventType.MOUSE_DOWN:
                        start_ptt("mouse")
                    elif mouse_event.event_type == MouseEventType.MOUSE_UP and self._ptt_source == "mouse":
                        stop_ptt()
                return ("class:chat.button", f" ⟦ {label} ⟧ ", handler)
            def handler(mouse_event):
                if mouse_event.event_type == MouseEventType.MOUSE_UP:
                    callback()
            return ("class:chat.button", f" ⟦ {label} ⟧ ", handler)

        def toolbar_fragments():
            data = None
            if self.activity_provider is not None:
                try:
                    data = self.activity_provider()
                except Exception:
                    data = None
            cancelable = bool(isinstance(data, dict) and data.get("cancelable"))
            fragments = [
                action_fragment("ENTER Send", lambda: submit_message()),
                ("", " "), action_fragment("F2 Attach file", lambda: finish("attach")),
                ("", " "), action_fragment("F4 Voice note", lambda: toggle_ptt(self._app)),
                ("", "\n  "), action_fragment("SPACE Hold to talk", lambda: None, ptt=True),
                ("", " "), action_fragment("F5 Emergency PTT", lambda: finish("emergency")),
                ("", " "), action_fragment("F6 More", lambda: finish("more")),
            ]
            if cancelable:
                fragments.extend([
                    ("", " "), action_fragment("F7 Cancel", cancel_voice_action),
                ])
            fragments.extend([
                ("", " "), action_fragment("ESC Disconnect", lambda: finish("disconnect")),
            ])
            return fragments

        layout = HSplit([
            Window(FormattedTextControl(header), wrap_lines=False),
            Window(FormattedTextControl([("class:chat.title", f"  ◈ CHAT  /  {self.peer_name}\n")]), height=1),
            Window(FormattedTextControl(history_fragments), wrap_lines=True),
            Window(FormattedTextControl(activity_fragments), height=1, wrap_lines=False),
            Frame(self.input, title="Message"),
            Window(FormattedTextControl(toolbar_fragments), height=2, wrap_lines=False),
        ])
        self._app = Application(
            layout=Layout(layout, focused_element=self.input),
            key_bindings=kb, full_screen=True, erase_when_done=True,
            refresh_interval=0.12, style=CHAT_STYLE, input=input, output=output,
            mouse_support=True,
        )
        # A lone ESC is otherwise buffered for prompt_toolkit's default 500 ms
        # so it can distinguish ESC from the start of an ANSI arrow/function
        # key sequence. 80 ms is enough for ordinary terminal escape sequences
        # while making Disconnect feel immediate.
        self._app.ttimeoutlen = 0.06
        # KeyProcessor has a separate one-second chord timeout. Keeping it
        # short prevents a lone ESC from waiting for that second timer too.
        self._app.timeoutlen = 0.12

    def _invoke_retry(self, entry):
        if self.on_retry is None:
            return
        try:
            self.on_retry(entry)
        except Exception as exc:
            with self.messages_lock:
                entry["status"] = f"Retry request failed: {exc}"
        self._app.invalidate()

    def _invoke_play_voice(self, entry):
        with self.messages_lock:
            if self.on_play_voice is None or not entry.get("voice_path"):
                return
        async def play_in_background():
            try:
                wav_path = await asyncio.to_thread(self.on_play_voice, entry)
                with self.messages_lock:
                    if wav_path is not None:
                        entry["wav_path"] = str(wav_path)
                        name = getattr(wav_path, "name", str(wav_path).rsplit("/", 1)[-1])
                        entry["status"] = f"played · standard WAV saved: {name}"
                    else:
                        entry["status"] = "voice message played"
            except Exception as exc:
                with self.messages_lock:
                    entry["status"] = f"VOICE PLAYBACK FAILED · {exc}"
            finally:
                self._app.invalidate()
        with self.messages_lock:
            entry["status"] = "playing voice message"
        self._app.create_background_task(play_in_background())
        self._app.invalidate()

    def _message_snapshot(self, *, copy_entries=False):
        """Take a consistent snapshot while RX/TX workers may update history."""
        with self.messages_lock:
            if copy_entries:
                return [(entry, dict(entry)) for entry in self.messages]
            return list(self.messages)

    def _append_message(self, speaker, body, status=""):
        with self.messages_lock:
            return add_chat_message(self.messages, speaker, body, status)

    def run(self):
        return self._app.run()


def add_chat_message(messages, speaker: str, body: str, status: str = ""):
    entry = {"speaker": speaker, "body": str(body),
             "time": datetime.now().strftime("%H:%M:%S"), "status": str(status)}
    messages.append(entry)
    if len(messages) > 80:
        recent = list(messages[-80:])
        recent_ids = {id(item) for item in recent}
        protected = [
            item for item in messages[:-80]
            if item.get("cancelable") and callable(item.get("cancel_action"))
            and id(item) not in recent_ids
        ]
        messages[:] = protected + recent
    return entry
