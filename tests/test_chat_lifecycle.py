from __future__ import annotations

import asyncio

from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from interface.chat import ChatScreen, add_chat_message


class _FakeApp:
    def __init__(self):
        self.invalidations = 0
        self.tasks = []
        self.exit_result = None

    def invalidate(self):
        self.invalidations += 1

    def create_background_task(self, coroutine):
        self.tasks.append(coroutine)

    def exit(self, result=None):
        self.exit_result = result


def _screen(messages=None, **kwargs):
    pipe = create_pipe_input()
    chat = ChatScreen(
        "peer", "local", messages,
        input=pipe, output=DummyOutput(), **kwargs,
    )
    chat._test_input_pipe = pipe
    chat._test_input_pipe.__enter__()
    chat._app = _FakeApp()
    return chat


def _close(chat):
    chat._test_input_pipe.__exit__(None, None, None)


def test_add_chat_message_stringifies_fields_and_caps_history():
    messages = []
    entry = add_chat_message(messages, "peer", 123, 7)

    assert entry is messages[0]
    assert entry["body"] == "123"
    assert entry["status"] == "7"
    assert len(entry["time"]) == 8

    protected = {"speaker": "system", "body": "keep", "cancelable": True, "cancel_action": lambda: None}
    messages = [protected] + [{"speaker": "peer", "body": str(i)} for i in range(80)]
    add_chat_message(messages, "peer", "new")

    assert messages[0] is protected
    assert messages[-1]["body"] == "new"
    assert len(messages) == 81


def test_chat_initialization_preserves_pending_cancelable_messages():
    protected = {"speaker": "system", "body": "pending", "cancelable": True, "cancel_action": lambda: None}
    messages = [protected] + [{"speaker": "peer", "body": str(i)} for i in range(90)]
    chat = _screen(messages)

    try:
        assert messages[0] is protected
        assert messages[-1]["body"] == "89"
        assert len(messages) == 81
    finally:
        _close(chat)


def test_submit_message_ignores_blank_text_and_sends_trimmed_text():
    sent = []
    chat = _screen(on_send=sent.append)
    try:
        chat.submit_message("   ")
        assert sent == []
        chat.submit_message("  hello world  ")
        assert sent == ["hello world"]
        assert chat._app.invalidations == 1
    finally:
        _close(chat)


def test_submit_message_records_callback_failure():
    def fail(_text):
        raise RuntimeError("queue offline")

    chat = _screen(on_send=fail)
    try:
        chat.submit_message("hello")
        assert chat.messages[-1]["speaker"] == "system"
        assert "queue offline" in chat.messages[-1]["body"]
        assert chat.messages[-1]["status"] == "SEND FAILED"
    finally:
        _close(chat)


def test_submit_message_without_callback_exits_with_payload():
    chat = _screen()
    try:
        chat.submit_message("hello")
        assert chat._app.exit_result == ("send", "hello")
    finally:
        _close(chat)


def test_toggle_ptt_starts_and_stops_recording_lifecycle():
    calls = []
    chat = _screen(
        on_ptt_start=lambda: calls.append("start"),
        on_ptt_stop=lambda: calls.append("stop"),
    )
    try:
        chat.toggle_ptt()
        assert chat._ptt_active is True
        assert calls == ["start"]

        chat.toggle_ptt()
        assert chat._ptt_active is False
        assert len(chat._app.tasks) == 1
        asyncio.run(chat._app.tasks.pop())
        assert calls == ["start", "stop"]
        assert chat._ptt_lock is False
    finally:
        _close(chat)


def test_ptt_start_failure_is_reported_without_marking_capture_active():
    def fail():
        raise RuntimeError("microphone unavailable")

    chat = _screen(on_ptt_start=fail)
    try:
        chat.toggle_ptt()
        assert chat._ptt_active is False
        assert "microphone unavailable" in chat.messages[-1]["body"]
        assert chat.messages[-1]["status"] == "VOICE ERROR"
    finally:
        _close(chat)


def test_disconnect_discards_active_ptt_and_exits():
    calls = []
    chat = _screen(
        on_ptt_start=lambda: calls.append("start"),
        on_ptt_cancel=lambda: calls.append("cancel"),
        on_ptt_stop=lambda: calls.append("stop"),
    )
    try:
        chat.toggle_ptt()
        chat._finish("disconnect")
        assert chat._app.exit_result == ("disconnect", None)
        assert chat._ptt_active is False
        assert len(chat._app.tasks) == 1
        asyncio.run(chat._app.tasks.pop())
        assert calls == ["start", "cancel"]
    finally:
        _close(chat)


def test_retry_callback_failure_updates_message_status():
    entry = {"speaker": "peer", "body": "retry", "status": ""}

    def fail(_entry):
        raise RuntimeError("peer not reachable")

    chat = _screen(on_retry=fail)
    try:
        chat._invoke_retry(entry)
        assert "peer not reachable" in entry["status"]
        assert chat._app.invalidations == 1
    finally:
        _close(chat)


def test_play_voice_callback_updates_status_and_saved_wav_path():
    entry = {"speaker": "peer", "body": "voice", "voice_path": "voice.ecv"}

    chat = _screen(on_play_voice=lambda _entry: "/tmp/voice.wav")
    try:
        chat._invoke_play_voice(entry)
        assert entry["status"] == "playing voice message"
        assert len(chat._app.tasks) == 1
        asyncio.run(chat._app.tasks.pop())
        assert entry["wav_path"] == "/tmp/voice.wav"
        assert "voice.wav" in entry["status"]
    finally:
        _close(chat)


def test_play_voice_failure_is_reported():
    entry = {"speaker": "peer", "body": "voice", "voice_path": "voice.ecv"}

    def fail(_entry):
        raise RuntimeError("decoder failed")

    chat = _screen(on_play_voice=fail)
    try:
        chat._invoke_play_voice(entry)
        asyncio.run(chat._app.tasks.pop())
        assert "decoder failed" in entry["status"]
    finally:
        _close(chat)


def test_message_snapshot_returns_copies_when_requested():
    chat = _screen([{"speaker": "peer", "body": "original"}])
    try:
        snapshot = chat._message_snapshot(copy_entries=True)
        snapshot[0][1]["body"] = "changed"
        assert chat.messages[0]["body"] == "original"
    finally:
        _close(chat)
