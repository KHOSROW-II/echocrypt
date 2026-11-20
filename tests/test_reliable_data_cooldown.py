import unittest
from types import SimpleNamespace
from unittest.mock import patch

from interface import communication


class _FakeTextChannel:
    def __init__(self):
        self.outgoing = SimpleNamespace(packet=b"ciphertext", message_id_hex="aabb", message_id=b"id")
        self.verified = False

    def prepare(self, message, max_packet_size):
        self.message = message
        return self.outgoing

    def verify_ack(self, ack_packet, message_id):
        self.verified = (ack_packet == b"authenticated-ack" and message_id == b"id")


class _FakeBinaryChannel:
    def __init__(self):
        self.verified = False

    def verify_ack(self, ack, transfer_id, kind, index):
        self.verified = (ack == b"authenticated-ack" and transfer_id == b"transfer"
                         and kind == 1 and index == 0)


class ReliableDataCooldownTests(unittest.TestCase):
    def test_reliable_data_waits_five_seconds_after_estimate_and_before_transmit(self):
        events = []
        cfg = SimpleNamespace(symbol_rate=200, output_device=None)

        def cooldown(label, seconds):
            events.append(("cooldown", label, seconds))

        def transmit(packet, **kwargs):
            events.append(("transmit", packet))
            return 11.09

        with patch.object(communication, "render_app_header"), \
             patch.object(communication, "packet_duration", return_value=11.09), \
             patch.object(communication, "_reliable_data_transmission_cooldown", side_effect=cooldown), \
             patch.object(communication, "transmit", side_effect=transmit), \
             patch("builtins.print"):
            communication._send_packet(
                b"data", "RELIABLE DATA", cfg, fec_mode="repetition3",
                pre_transmit_delay=communication.RELIABLE_DATA_COOLDOWN_SECONDS,
            )

        self.assertEqual(events, [
            ("cooldown", "RELIABLE DATA", 5),
            ("transmit", b"data"),
        ])

    def test_actual_reliable_text_send_path_requests_five_second_cooldown(self):
        channel = _FakeTextChannel()
        cfg = SimpleNamespace(max_retries=0, max_payload=4096, symbol_rate=200,
                              ack_timeout_seconds=20)
        sent = []
        with patch.object(communication.AdaptiveFECController, "plan", return_value=["repetition3"]), \
             patch.object(communication, "_send_packet", side_effect=lambda *a, **kw: sent.append((a, kw))), \
             patch.object(communication, "render_app_header"), \
             patch.object(communication, "_receive_auto", return_value=b"authenticated-ack"), \
             patch("builtins.print"):
            communication._send_text_with_ack(channel, "test message", cfg, b"hop")

        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0][0][1], "RELIABLE DATA")
        self.assertEqual(sent[0][1]["pre_transmit_delay"], 5)
        self.assertTrue(channel.verified)

    def test_file_manifest_gets_arm_window_but_chunks_do_not(self):
        cfg = SimpleNamespace(max_retries=0, ack_timeout_seconds=20, symbol_rate=200)
        for kind, label, expected_delay in (
            (0, "FILE MANIFEST", 5),
            (1, "FILE CHUNK 1/1", 0),
        ):
            channel = _FakeBinaryChannel()
            sent = []
            with self.subTest(kind=kind), \
                 patch.object(communication.AdaptiveFECController, "plan", return_value=["repetition3"]), \
                 patch.object(communication, "_send_packet", side_effect=lambda *a, **kw: sent.append((a, kw))), \
                 patch.object(communication, "render_app_header"), \
                 patch.object(communication, "_receive_auto", return_value=b"authenticated-ack"), \
                 patch("builtins.print"):
                communication._send_frame_wait_ack(
                    channel, b"file-frame", b"transfer", kind, 0, cfg, b"hop", 200,
                    label, "repetition3",
                )
            self.assertEqual(len(sent), 1)
            self.assertEqual(sent[0][1]["pre_transmit_delay"], expected_delay)


    def test_real_reliable_data_path_waits_five_seconds_before_playback(self):
        cfg = SimpleNamespace(symbol_rate=200, output_device=None)
        started = communication.time.monotonic()
        observed = {}
        printed = []

        def playback(packet, **kwargs):
            observed["elapsed"] = communication.time.monotonic() - started
            observed["packet"] = packet
            printed.append("<PLAYBACK>")
            return 11.09

        with patch.object(communication, "render_app_header"), \
             patch.object(communication, "packet_duration", return_value=11.09), \
             patch.object(communication, "transmit", side_effect=playback), \
             patch("builtins.print", side_effect=lambda *args, **kwargs: printed.append(" ".join(map(str, args)))):
            communication._send_packet(
                b"reliable-payload", "RELIABLE DATA", cfg, fec_mode="repetition3",
                pre_transmit_delay=5,
            )

        self.assertEqual(observed["packet"], b"reliable-payload")
        self.assertGreaterEqual(observed["elapsed"], 5.0)
        self.assertLess(observed["elapsed"], 6.5)
        estimate_index = next(i for i, line in enumerate(printed) if "estimated 11.09s" in line)
        countdown_index = next(i for i, line in enumerate(printed) if "starts in 5s" in line)
        playback_index = printed.index("<PLAYBACK>")
        self.assertLess(estimate_index, countdown_index)
        self.assertLess(countdown_index, playback_index)

    def test_ack_transmissions_do_not_get_data_arm_delay(self):
        cfg = SimpleNamespace(symbol_rate=200, output_device=None)
        with patch.object(communication, "render_app_header"), \
             patch.object(communication, "packet_duration", return_value=0.1), \
             patch.object(communication, "_reliable_data_transmission_cooldown") as cooldown, \
             patch.object(communication, "transmit", return_value=0.1), \
             patch("builtins.print"):
            communication._send_packet(b"ack", "AUTHENTICATED ACK", cfg)
        cooldown.assert_not_called()


if __name__ == "__main__":
    unittest.main()
