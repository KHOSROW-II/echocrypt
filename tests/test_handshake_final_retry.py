import hashlib
import unittest

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from security.peer_handshake import PeerHandshake


def raw_public(key):
    return key.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)


class FinalHandshakeRetryTests(unittest.TestCase):
    def test_lost_finish_r_can_be_recovered_by_replaying_exact_finish_i(self):
        initiator_identity = Ed25519PrivateKey.generate()
        responder_identity = Ed25519PrivateKey.generate()
        initiator_public = raw_public(initiator_identity.public_key())
        responder_public = raw_public(responder_identity.public_key())
        initiator = PeerHandshake(
            "initiator", initiator_identity, responder_public,
            hashlib.sha256(responder_public).hexdigest(),
        )
        responder = PeerHandshake(
            "responder", responder_identity, initiator_public,
            hashlib.sha256(initiator_public).hexdigest(),
        )

        hello_i = initiator.start()
        hello_r = responder.receive(hello_i)
        finish_i = initiator.receive(hello_r)
        finish_r = responder.receive(finish_i)

        self.assertTrue(responder.established)
        self.assertFalse(initiator.established)
        # The first FINISH-R is deliberately treated as lost.
        self.assertEqual(responder.receive(finish_i), finish_r)
        self.assertIsNone(initiator.receive(finish_r))
        # A duplicate final confirmation remains harmless at the initiator.
        self.assertIsNone(initiator.receive(finish_r))


if __name__ == "__main__":
    unittest.main()
