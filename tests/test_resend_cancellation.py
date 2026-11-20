import threading
import unittest
from types import SimpleNamespace

from interface.communication import TransferCancelled, _engine_resend_chunks


class ResendCancellationTests(unittest.TestCase):
    def test_direct_resend_does_not_create_or_queue_chunks_after_cancellation(self):
        cancelled = threading.Event()
        cancelled.set()
        outgoing = SimpleNamespace(
            manifest=SimpleNamespace(total_chunks=3, filename="sample.bin")
        )

        class Channel:
            def chunk_packet(self, *_args, **_kwargs):
                raise AssertionError("chunk packet must not be created after cancellation")

        with self.assertRaises(TransferCancelled):
            _engine_resend_chunks(
                Channel(), engine=object(), outgoing=outgoing,
                indexes=(0, 2), cfg=object(), cancel_event=cancelled,
            )


if __name__ == "__main__":
    unittest.main()
