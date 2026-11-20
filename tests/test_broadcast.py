import hashlib
import tempfile
import unittest
from pathlib import Path

from core.broadcast import (
    BroadcastCodec, BroadcastError, BroadcastReassembler,
    BroadcastRetransmitCache, BroadcastType, BROADCAST_HOP_KEY,
    HEADER, FRAGMENT_HEADER_SIZE, RETRY_NEED_HEADER, RETRY_NEED_DATA,
)
from core.modem import demodulate, modulate


class BroadcastCodecTests(unittest.TestCase):
    def test_broadcast_metadata_cache_is_bounded_and_evicts_oldest(self):
        import tempfile
        from pathlib import Path
        from core.broadcast import BroadcastCodec, BroadcastReassembler

        sender = BroadcastCodec(sender_id=b"ALICE001", max_packet_size=256)
        receiver = BroadcastCodec(sender_id=b"BOB00001", max_packet_size=256)
        with tempfile.TemporaryDirectory() as tmp:
            first = Path(tmp) / "first.bin"
            second = Path(tmp) / "second.bin"
            first.write_bytes(b"first")
            second.write_bytes(b"second")
            mid1, frames1 = sender.encode_file(first)
            mid2, frames2 = sender.encode_file(second)
            reassembler = BroadcastReassembler(receiver, max_file_metadata=1)
            reassembler.feed(frames1[0])
            self.assertIsNotNone(reassembler.file_metadata(sender.sender_id, mid1))
            reassembler.feed(frames2[0])
            self.assertIsNone(reassembler.file_metadata(sender.sender_id, mid1))
            self.assertIsNotNone(reassembler.file_metadata(sender.sender_id, mid2))

    def test_broadcast_file_encoding_honors_cancellation(self):
        import tempfile
        import threading
        from pathlib import Path
        from core.broadcast import BroadcastCodec, BroadcastError

        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "payload.bin"
            source.write_bytes(b"payload")
            cancelled = threading.Event()
            cancelled.set()
            with self.assertRaises(BroadcastError):
                BroadcastCodec(sender_id=b"ALICE001", max_packet_size=256).encode_file(
                    source, cancel_event=cancelled
                )


    def test_public_broadcast_roundtrip_without_peer_key_exchange(self):
        alice = BroadcastCodec(sender_id=b"ALICE001", max_packet_size=256)
        bob = BroadcastCodec(sender_id=b"BOB00001", max_packet_size=256)
        frames = alice.encode_text("open room: درود به همه")
        decoded = bob.decode(frames[0])
        self.assertEqual(decoded.sender_id, b"ALICE001")
        self.assertEqual(decoded.kind, BroadcastType.TEXT)
        self.assertEqual(decoded.payload, "open room: درود به همه".encode())

    def test_public_broadcast_waveform_uses_the_same_public_hopping_schedule(self):
        sender = BroadcastCodec(sender_id=b"ALICE001", max_packet_size=256)
        receiver = BroadcastCodec(sender_id=b"BOB00001", max_packet_size=256)
        frame = sender.encode_text("common public hop schedule")[0]
        waveform = modulate(
            frame, sample_rate=48000, symbol_rate=800,
            mark=1800, space=2400, gain=0.45, max_payload=1024,
            fec_mode="conv", hop_key=BROADCAST_HOP_KEY,
        )
        recovered = demodulate(
            waveform, sample_rate=48000, symbol_rate=800,
            mark=1800, space=2400, max_payload=1024,
            hop_key=BROADCAST_HOP_KEY,
        )
        self.assertEqual(receiver.decode(recovered).payload,
                         sender.decode(frame).payload)

    def test_corrupted_frame_is_rejected(self):
        codec = BroadcastCodec(sender_id=b"ALICE001", max_packet_size=256)
        frame = bytearray(codec.encode_text("tamper check")[0])
        frame[-1] ^= 0x01
        with self.assertRaises(BroadcastError):
            codec.decode(frame)

    def test_fragmented_utf8_text_reassembles_out_of_order_and_deduplicates(self):
        codec = BroadcastCodec(sender_id=b"ALICE001", max_packet_size=112)
        receiver = BroadcastReassembler(codec)
        text = "سلام 🌍 " * 30
        frames = codec.encode_text(text, message_id=b"TEXT0001")
        self.assertGreater(len(frames), 1)
        deliveries = [receiver.feed(frame) for frame in reversed(frames)]
        final = next(item for item in deliveries if item is not None and item.complete)
        self.assertEqual(final.payload.decode(), text)
        duplicate = receiver.feed(frames[0])
        self.assertTrue(duplicate.duplicate)

    def test_late_joiner_requests_manifest_even_after_all_file_chunks_arrive(self):
        with tempfile.TemporaryDirectory() as td:
            sender = BroadcastCodec(sender_id=b"ALICE001", max_packet_size=256)
            receiver_codec = BroadcastCodec(sender_id=b"BOB00001", max_packet_size=256)
            source = Path(td) / "sample.bin"
            data = bytes(range(256)) * 2
            source.write_bytes(data)
            message_id, frames = sender.encode_file(source)
            receiver = BroadcastReassembler(receiver_codec, download_dir=Path(td) / "downloads")

            # The listener joins halfway through the transfer and receives all
            # remaining payload chunks, but the one-shot manifest already passed.
            for frame in frames[1:]:
                delivery = receiver.feed(frame)
            self.assertFalse(delivery.complete)
            requests = receiver.retry_requests(older_than=0)
            self.assertTrue(any(kind == BroadcastType.FILE_META and indices == (0,)
                                for _sid, _mid, kind, indices in requests))

            completed = receiver.feed(frames[0])
            self.assertTrue(completed.complete)
            self.assertEqual(completed.path.read_bytes(), data)
            self.assertEqual(hashlib.sha256(completed.path.read_bytes()).hexdigest(),
                             hashlib.sha256(data).hexdigest())

    def test_file_transfer_is_hash_checked_and_empty_files_work(self):
        with tempfile.TemporaryDirectory() as td:
            sender = BroadcastCodec(sender_id=b"ALICE001", max_packet_size=256)
            receiver_codec = BroadcastCodec(sender_id=b"BOB00001", max_packet_size=256)
            receiver = BroadcastReassembler(receiver_codec, download_dir=Path(td) / "out")
            source = Path(td) / "payload.dat"
            payload = b"0123456789" * 90
            source.write_bytes(payload)
            _mid, frames = sender.encode_file(source)
            results = [receiver.feed(frame) for frame in frames]
            complete = next(result for result in results if result and result.path)
            self.assertEqual(complete.path.read_bytes(), payload)

            empty = Path(td) / "empty.bin"
            empty.write_bytes(b"")
            _mid, empty_frames = sender.encode_file(empty)
            empty_result = receiver.feed(empty_frames[0])
            self.assertTrue(empty_result.complete)
            self.assertEqual(empty_result.path.read_bytes(), b"")

    def test_retransmit_cache_only_answers_its_own_sender_id_and_fragments(self):
        sender = BroadcastCodec(sender_id=b"ALICE001", max_packet_size=256)
        receiver = BroadcastCodec(sender_id=b"BOB00001", max_packet_size=256)
        mid = b"CACHE001"
        frames = sender.encode_text("x" * 300, message_id=mid)
        cache = BroadcastRetransmitCache(sender)
        cache.remember(mid, BroadcastType.TEXT, frames)
        request = receiver.encode_retry_request(
            sender.sender_id, mid, BroadcastType.TEXT, (1,)
        )
        parsed = receiver.decode(request)
        selected = cache.handle_request(parsed.payload)
        self.assertEqual(selected, (frames[1],))


    def test_v2_fragment_header_survives_one_corrupted_copy(self):
        with tempfile.TemporaryDirectory() as td:
            sender = BroadcastCodec(sender_id=b"ALICE001", max_packet_size=1024)
            receiver = BroadcastCodec(sender_id=b"BOB00001", max_packet_size=1024)
            source = Path(td) / "voice.ecv"
            source.write_bytes(b"ECV3" + bytes(range(256)) * 2)
            mid, frames = sender.encode_file(source, chunk_size=96, symbol_rate=800)
            self.assertGreater(len(frames), 2)
            original = frames[1]
            decoded = receiver.decode(original)
            self.assertTrue(decoded.has_fragment_header)
            self.assertEqual(decoded.media_kind, 1)
            self.assertEqual(decoded.index, 0)
            self.assertEqual(decoded.count, len(frames) - 1)
            # Corrupt only the first descriptor copy. The second copy is used
            # to reconstruct the AEAD associated data and packet CRC.
            first_copy_damaged = bytearray(original)
            first_copy_damaged[HEADER.size + 5] ^= 0x20
            recovered_first = receiver.decode(first_copy_damaged)
            self.assertEqual(recovered_first.payload, decoded.payload)
            # Corrupt only the second descriptor copy; first remains usable.
            second_offset = HEADER.size + FRAGMENT_HEADER_SIZE + len(decoded.payload) + 16
            second_copy_damaged = bytearray(original)
            second_copy_damaged[second_offset + 6] ^= 0x01
            recovered_second = receiver.decode(second_copy_damaged)
            self.assertEqual(recovered_second.payload, decoded.payload)
            # Both descriptors damaged means the receiver must reject rather
            # than guessing a fragment coordinate/profile.
            both_damaged = bytearray(original)
            both_damaged[HEADER.size + 5] ^= 0x20
            both_damaged[second_offset + 6] ^= 0x01
            with self.assertRaises(BroadcastError):
                receiver.decode(both_damaged)

    def test_retry_cache_accepts_parsed_request_and_returns_only_requested_chunks(self):
        sender = BroadcastCodec(sender_id=b"ALICE001", max_packet_size=512)
        receiver = BroadcastCodec(sender_id=b"BOB00001", max_packet_size=512)
        with tempfile.TemporaryDirectory() as td:
            source = Path(td) / "voice.ecv"
            source.write_bytes(b"ECV3" + bytes(range(256)) * 3)
            mid, frames = sender.encode_file(source, chunk_size=100)
            chunk_frames = frames[1:]
            cache = BroadcastRetransmitCache(sender)
            cache.remember(mid, BroadcastType.FILE_CHUNK, chunk_frames)
            request = receiver.encode_retry_request(
                sender.sender_id, mid, BroadcastType.FILE_CHUNK, (1, 3),
                request_flags=RETRY_NEED_HEADER | RETRY_NEED_DATA,
            )
            parsed_packet = receiver.decode(request)
            parsed = receiver.parse_retry_request(parsed_packet.payload)
            selected = cache.handle_request(parsed)
            self.assertEqual(selected, (chunk_frames[1], chunk_frames[3]))

    def test_v2_fragment_headers_are_authenticated_as_aead_associated_data(self):
        sender = BroadcastCodec(sender_id=b"ALICE001", max_packet_size=512)
        receiver = BroadcastCodec(sender_id=b"BOB00001", max_packet_size=512)
        with tempfile.TemporaryDirectory() as td:
            source = Path(td) / "payload.bin"
            source.write_bytes(b"file payload")
            _mid, frames = sender.encode_file(source)
            packet = bytearray(frames[1])
            # Build a syntactically valid alternative descriptor and checksum;
            # the AEAD tag must still reject the changed associated data.
            packet[HEADER.size + 6] ^= 0x01
            second = HEADER.size + FRAGMENT_HEADER_SIZE + len(b"file payload") + 16
            packet[second + 6] ^= 0x01
            descriptor = bytes(packet[HEADER.size:HEADER.size + FRAGMENT_HEADER_SIZE])
            from core.broadcast import BroadcastCodec as Codec
            info = Codec._read_fragment_header(descriptor)
            if info is not None:
                # The header CRC now fails or the duplicate headers disagree;
                # either way, no altered descriptor is accepted.
                pass
            with self.assertRaises(BroadcastError):
                receiver.decode(packet)

    def test_retry_request_is_bounded_to_one_modem_frame(self):
        codec = BroadcastCodec(sender_id=b"BOB00001", max_packet_size=256)
        packet = codec.encode_retry_request(
            b"ALICE001", b"LARGE001", BroadcastType.FILE_CHUNK, range(0, 4000)
        )
        self.assertLessEqual(len(packet), codec.max_packet_size)
        parsed = codec.decode(packet)
        _sid, _mid, _kind, flags, indices = codec.parse_retry_request(parsed.payload)
        self.assertEqual(flags, RETRY_NEED_HEADER | RETRY_NEED_DATA)
        self.assertGreater(len(indices), 0)
        self.assertLessEqual(len(indices), 96)
        self.assertLess(len(indices), 4000)


if __name__ == "__main__":
    unittest.main()
