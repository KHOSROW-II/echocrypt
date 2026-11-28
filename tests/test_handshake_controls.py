import time
import unittest
from unittest.mock import patch

from interface import communication


class FakeTrace:
    enabled = False

    def emit(self, *args, **kwargs):
        pass


class FakeHandshake:
    peer_fingerprint = "0123456789abcdef"

    def __init__(self, role):
        self.role = role
        self.state = type("State", (), {"value": "WAIT"})()
        self.established = True

    def start(self):
        return b"HELLO-I-CACHED"


class HandshakeControlTests(unittest.TestCase):
    ALL_CONTROL_LABELS = (
        "HELLO-I", "HELLO-I RETRY", "HELLO-R", "HELLO-R RETRY",
        "FINISH-I", "FINISH-I RETRY", "FINISH-R",
    )

    def test_cooldown_waits_full_five_one_second_ticks_for_every_control_frame(self):
        for label in self.ALL_CONTROL_LABELS:
            with self.subTest(label=label), \
                 patch.object(communication.time, "sleep") as sleep, \
                 patch("builtins.print"):
                communication._handshake_transmission_cooldown(label, 5)
            self.assertEqual([call.args[0] for call in sleep.call_args_list], [1, 1, 1, 1, 1])

    def test_cooldown_rejects_non_handshake_frame(self):
        for label in ("DATA", "ACK", "VOICE", "HELLO", "FINISH"):
            with self.subTest(label=label), self.assertRaises(ValueError):
                communication._handshake_transmission_cooldown(label, 5)

    def test_real_send_helper_waits_before_playback_for_hello_and_finish(self):
        for label in self.ALL_CONTROL_LABELS:
            events = []
            cfg = type("Cfg", (), {"symbol_rate": 200})()
            with self.subTest(label=label), \
                 patch.object(communication.time, "sleep", side_effect=lambda value: events.append(("sleep", value))), \
                 patch.object(communication, "_send_packet", side_effect=lambda packet, item, *a, **kw: events.append(("play", item))), \
                 patch("builtins.print"):
                communication._send_handshake_packet(b"cached", label, cfg)
            self.assertEqual(events, [("sleep", 1)] * 5 + [("play", label)])

    def test_finish_r_playback_is_at_least_five_real_seconds_after_send_entry(self):
        cfg = type("Cfg", (), {"symbol_rate": 200})()
        started = time.monotonic()
        observed = {}

        def playback(packet, label, *args, **kwargs):
            observed["label"] = label
            observed["elapsed"] = time.monotonic() - started

        with patch.object(communication, "_send_packet", side_effect=playback), \
             patch("builtins.print"):
            communication._send_handshake_packet(b"FINISH-R", "FINISH-R", cfg)

        self.assertEqual(observed["label"], "FINISH-R")
        # Allow only sub-nanosecond float representation error at the 5s boundary.
        self.assertGreaterEqual(observed["elapsed"] + 1e-9, 5.0)
        self.assertLess(observed["elapsed"], 6.5)

    def test_initiator_waits_before_hello_i_finish_i_and_both_retries(self):
        events = []
        cfg = type("Cfg", (), {"listen_timeout_seconds": 3, "debug_enabled": False})()
        hs = FakeHandshake("initiator")

        def cooldown(label, seconds):
            events.append(("cooldown", label, seconds))

        def send(packet, label, *args, **kwargs):
            events.append(("play", label))

        def receive(label, *args, retry_callback=None, **kwargs):
            events.append(("listen", label))
            if retry_callback and label == "Listening for HELLO-R":
                retry_callback()
            return b"HELLO-R" if label == "Listening for HELLO-R" else b"FINISH-R"

        def handshake_receive(_hs, packet, label, _trace):
            events.append(("parse", label))
            return b"FINISH-I-CACHED" if label in {"HELLO-R", "HELLO-R RETRY"} else None

        with patch.object(communication, "for_config", return_value=FakeTrace()), \
             patch.object(communication, "_handshake_transmission_cooldown", side_effect=cooldown), \
             patch.object(communication, "_send_packet", side_effect=send), \
             patch.object(communication, "_record_packet", side_effect=receive), \
             patch.object(communication, "_handshake_receive", side_effect=handshake_receive), \
             patch("builtins.print"):
            communication._establish("initiator", hs, cfg)

        for label in ("HELLO-I", "HELLO-I RETRY", "FINISH-I"):
            self.assertLess(events.index(("cooldown", label, 5)), events.index(("play", label)))

    def test_responder_waits_before_hello_r_finish_r_and_hello_retry(self):
        events = []
        cfg = type("Cfg", (), {"listen_timeout_seconds": 3, "debug_enabled": False})()
        hs = FakeHandshake("responder")

        def cooldown(label, seconds):
            events.append(("cooldown", label, seconds))

        def send(packet, label, *args, **kwargs):
            events.append(("play", label))

        def receive(label, *args, retry_callback=None, **kwargs):
            events.append(("listen", label))
            if label == "Listening for FINISH-I" and retry_callback:
                retry_callback()  # Simulate F8: resend cached HELLO-R with cooldown.
            return b"HELLO-I" if label == "Listening for HELLO-I" else b"FINISH-I"

        def handshake_receive(_hs, packet, label, _trace):
            events.append(("parse", label))
            return b"HELLO-R-CACHED" if label == "HELLO-I" else b"FINISH-R-CACHED"

        with patch.object(communication, "for_config", return_value=FakeTrace()), \
             patch.object(communication, "_handshake_transmission_cooldown", side_effect=cooldown), \
             patch.object(communication, "_send_packet", side_effect=send), \
             patch.object(communication, "_record_packet", side_effect=receive), \
             patch.object(communication, "_handshake_receive", side_effect=handshake_receive), \
             patch("builtins.print"):
            communication._establish("responder", hs, cfg)

        for label in ("HELLO-R", "HELLO-R RETRY", "FINISH-R"):
            self.assertLess(events.index(("cooldown", label, 5)), events.index(("play", label)))

    def test_duplicate_hello_i_resends_hello_r_then_finish_r_also_waits(self):
        events = []
        cfg = type("Cfg", (), {"listen_timeout_seconds": 3, "debug_enabled": False})()
        hs = FakeHandshake("responder")
        finish_receive_calls = 0

        def cooldown(label, seconds):
            events.append(("cooldown", label, seconds))

        def send(packet, label, *args, **kwargs):
            events.append(("play", label))

        def receive(label, *args, retry_callback=None, **kwargs):
            nonlocal finish_receive_calls
            if label == "Listening for HELLO-I":
                return b"HELLO-I"
            finish_receive_calls += 1
            return b"HELLO-I" if finish_receive_calls == 1 else b"FINISH-I"

        def handshake_receive(_hs, packet, label, _trace):
            events.append(("parse", label))
            return b"FINISH-R-CACHED" if label == "FINISH-I" else b"HELLO-R-CACHED"

        with patch.object(communication, "for_config", return_value=FakeTrace()), \
             patch.object(communication, "_handshake_transmission_cooldown", side_effect=cooldown), \
             patch.object(communication, "_send_packet", side_effect=send), \
             patch.object(communication, "_record_packet", side_effect=receive), \
             patch.object(communication, "_handshake_receive", side_effect=handshake_receive), \
             patch("builtins.print"):
            communication._establish("responder", hs, cfg)

        for label in ("HELLO-R", "HELLO-R RETRY", "FINISH-R"):
            self.assertLess(events.index(("cooldown", label, 5)), events.index(("play", label)))
        self.assertEqual([e[1] for e in events if e[0] == "play"],
                         ["HELLO-R", "HELLO-R RETRY", "FINISH-R"])

    def test_f8_retries_previous_handshake_frame_then_rearms(self):
        cfg = type("Cfg", (), {"listen_timeout_seconds": 3, "debug_enabled": False})()
        sent = []
        with patch.object(communication, "_arm_receive_prompt", side_effect=[False, True]), \
             patch.object(communication, "render_app_header"), \
             patch.object(communication, "receive_auto", return_value=b"ECH1payload") as receive, \
             patch("builtins.print"):
            packet = communication._record_packet(
                "Listening for FINISH-R", cfg,
                retry_callback=lambda: sent.append("retransmitted"),
            )
        self.assertEqual(packet, b"ECH1payload")
        self.assertEqual(sent, ["retransmitted"])
        receive.assert_called_once()


if __name__ == "__main__":
    unittest.main()
