"""Authenticated, pinned-identity peer handshake for ECHO-CRYPT text sessions.

Wire exchange (half duplex):
  initiator HELLO -> responder HELLO -> initiator FINISH -> responder FINISH

HELLO packets are signed with Ed25519 and carry ephemeral X25519 material.
Both directional traffic keys are derived from the shared secret and the exact
signed handshake transcript. FINISH packets prove possession of those keys.
"""

import hashlib
import os
import struct
from enum import Enum

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from core.session import Session

HELLO_MAGIC = b"ECH1"
HELLO_VERSION = 1
HELLO_HEADER = struct.Struct("!4sBB32s16s")
HELLO_SIGNATURE_SIZE = 64
HELLO_DOMAIN = b"ECHOCRYPT/HELLO/v1\x00"
TRANSCRIPT_DOMAIN = b"ECHOCRYPT/TRANSCRIPT/v1\x00"
CONTROL_AAD = b"ECHOCRYPT/CONTROL/v1"
FINISH_MAGIC = b"ECHF"


class HandshakeState(str, Enum):
    READY = "READY"
    WAIT_HELLO = "WAIT_HELLO"
    WAIT_REPLY = "WAIT_REPLY"
    WAIT_INIT_FINISH = "WAIT_INIT_FINISH"
    WAIT_RESP_FINISH = "WAIT_RESP_FINISH"
    ESTABLISHED = "ESTABLISHED"
    CLOSED = "CLOSED"


class HandshakeError(ValueError):
    pass


def _raw_public(key) -> bytes:
    return key.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)


def _create_hello(identity_private, role_code: int):
    ephemeral = X25519PrivateKey.generate()
    nonce = os.urandom(16)
    header = HELLO_HEADER.pack(
        HELLO_MAGIC,
        HELLO_VERSION,
        role_code,
        _raw_public(ephemeral.public_key()),
        nonce,
    )
    signature = identity_private.sign(HELLO_DOMAIN + header)
    return header + signature, ephemeral, nonce


def _verify_hello(packet: bytes, peer_public_raw: bytes, expected_fingerprint: str, expected_role: int):
    if not isinstance(packet, bytes) or len(packet) != HELLO_HEADER.size + HELLO_SIGNATURE_SIZE:
        raise HandshakeError("Malformed HELLO packet")
    if len(peer_public_raw) != 32:
        raise HandshakeError("Peer Ed25519 public key must be 32 bytes")
    actual_fingerprint = hashlib.sha256(peer_public_raw).hexdigest()
    if actual_fingerprint.lower() != expected_fingerprint.lower():
        raise HandshakeError("Untrusted peer fingerprint")

    header, signature = packet[:-HELLO_SIGNATURE_SIZE], packet[-HELLO_SIGNATURE_SIZE:]
    magic, version, role_code, ephemeral_raw, nonce = HELLO_HEADER.unpack(header)
    if magic != HELLO_MAGIC or version != HELLO_VERSION:
        raise HandshakeError("Unsupported HELLO format")
    if role_code != expected_role:
        raise HandshakeError("Unexpected peer role")
    try:
        Ed25519PublicKey.from_public_bytes(peer_public_raw).verify(signature, HELLO_DOMAIN + header)
        ephemeral = X25519PublicKey.from_public_bytes(ephemeral_raw)
    except (InvalidSignature, ValueError) as exc:
        raise HandshakeError("HELLO signature or key is invalid") from exc
    return ephemeral, nonce


class PeerHandshake:
    """Stateful, mutual-authentication handshake bound to a pinned peer key."""

    def __init__(self, role: str, identity_private, peer_public_raw: bytes, peer_fingerprint: str):
        if role not in ("initiator", "responder"):
            raise ValueError("Role must be 'initiator' or 'responder'")
        if len(peer_public_raw) != 32:
            raise ValueError("Peer public identity must be 32 bytes")
        if hashlib.sha256(peer_public_raw).hexdigest().lower() != peer_fingerprint.lower():
            raise ValueError("Peer public identity does not match pinned fingerprint")

        self.role = role
        self._role_code = 0 if role == "initiator" else 1
        self._peer_role_code = 1 - self._role_code
        self.identity_private = identity_private
        self.peer_public_raw = bytes(peer_public_raw)
        self.peer_fingerprint = peer_fingerprint.lower()
        self.local_fingerprint = hashlib.sha256(_raw_public(identity_private.public_key())).hexdigest()
        if self.local_fingerprint == self.peer_fingerprint:
            raise ValueError("Local and peer identities must be different")

        self.state = HandshakeState.READY if role == "initiator" else HandshakeState.WAIT_HELLO
        self._local_ephemeral = None
        self._local_nonce = None
        self._peer_ephemeral = None
        self._peer_nonce = None
        self._hello_i = None
        self._hello_r = None
        self._finish_i = None
        self._finish_r = None
        self._transcript_hash = None
        self.session = None

    @property
    def established(self) -> bool:
        return self.state == HandshakeState.ESTABLISHED

    @property
    def retry_packet(self):
        """Return the exact handshake flight that should be retransmitted."""
        if self.role == "initiator":
            if self.state == HandshakeState.WAIT_REPLY:
                return self._hello_i
            if self.state == HandshakeState.WAIT_RESP_FINISH:
                return self._finish_i
        elif self.role == "responder" and self.state == HandshakeState.WAIT_INIT_FINISH:
            return self._hello_r
        return None

    def start(self) -> bytes:
        if self.role != "initiator" or self.state != HandshakeState.READY or self._hello_i is not None:
            raise HandshakeError("Only a new initiator can start the handshake")
        self._hello_i, self._local_ephemeral, self._local_nonce = _create_hello(self.identity_private, 0)
        self.state = HandshakeState.WAIT_REPLY
        return self._hello_i

    def _make_responder_hello(self) -> bytes:
        self._hello_r, self._local_ephemeral, self._local_nonce = _create_hello(self.identity_private, 1)
        return self._hello_r

    def _derive_session(self):
        if not all((self._hello_i, self._hello_r, self._local_ephemeral, self._peer_ephemeral)):
            raise HandshakeError("Handshake transcript is incomplete")
        shared = self._local_ephemeral.exchange(self._peer_ephemeral)
        transcript = TRANSCRIPT_DOMAIN + self._hello_i + self._hello_r
        self._transcript_hash = hashlib.sha256(transcript).digest()
        material = HKDF(
            algorithm=hashes.SHA256(),
            length=96,
            salt=self._transcript_hash,
            info=b"ECHOCRYPT/v1 directional traffic and acoustic hopping keys",
        ).derive(shared)
        initiator_to_responder, responder_to_initiator = material[:32], material[32:64]
        if self.role == "initiator":
            tx_key, rx_key = initiator_to_responder, responder_to_initiator
        else:
            tx_key, rx_key = responder_to_initiator, initiator_to_responder
        hopping_key = material[64:96]
        self.session = Session(tx_key, rx_key, self.peer_fingerprint, hopping_key)

    def _make_finish(self, role_code: int) -> bytes:
        payload = FINISH_MAGIC + bytes([role_code]) + self._transcript_hash
        return self.session.tx.encrypt(payload, CONTROL_AAD)

    def _verify_finish(self, packet: bytes, expected_role_code: int) -> None:
        try:
            payload = self.session.rx.decrypt(packet, CONTROL_AAD)
        except Exception as exc:
            raise HandshakeError("Key confirmation packet could not be authenticated") from exc
        expected = FINISH_MAGIC + bytes([expected_role_code]) + self._transcript_hash
        if payload != expected:
            raise HandshakeError("Key confirmation transcript mismatch")

    def receive(self, packet: bytes):
        if self.state == HandshakeState.CLOSED:
            raise HandshakeError("Handshake is closed")
        if not isinstance(packet, (bytes, bytearray, memoryview)):
            raise TypeError("Handshake packet must be bytes-like")
        packet = bytes(packet)

        # FINISH-R is the last handshake flight. If it was lost, the initiator
        # retries the exact FINISH-I. Only that previously authenticated
        # transcript can replay the cached final packet.
        if (self.role == "responder" and self.state == HandshakeState.ESTABLISHED
                and self._finish_i is not None and packet == self._finish_i):
            return self._finish_r
        if (self.role == "initiator" and self.state == HandshakeState.ESTABLISHED
                and self._finish_r is not None and packet == self._finish_r):
            return None
        if self.state == HandshakeState.ESTABLISHED:
            raise HandshakeError("Handshake is no longer accepting packets")

        # Idempotent retransmission handling: retry must resend the exact prior
        # control frame, not create a new ephemeral key or transcript.
        if (self.role == "responder" and self.state == HandshakeState.WAIT_INIT_FINISH
                and self._hello_i is not None and packet == self._hello_i):
            return self._hello_r
        if (self.role == "initiator" and self.state == HandshakeState.WAIT_RESP_FINISH
                and self._hello_r is not None and packet == self._hello_r):
            return self._finish_i

        if self.role == "responder" and self.state == HandshakeState.WAIT_HELLO:
            self._peer_ephemeral, self._peer_nonce = _verify_hello(
                packet, self.peer_public_raw, self.peer_fingerprint, expected_role=0
            )
            self._hello_i = packet
            response = self._make_responder_hello()
            self._derive_session()
            self.state = HandshakeState.WAIT_INIT_FINISH
            return response

        if self.role == "initiator" and self.state == HandshakeState.WAIT_REPLY:
            self._peer_ephemeral, self._peer_nonce = _verify_hello(
                packet, self.peer_public_raw, self.peer_fingerprint, expected_role=1
            )
            self._hello_r = packet
            self._derive_session()
            self.state = HandshakeState.WAIT_RESP_FINISH
            self._finish_i = self._make_finish(role_code=0)
            return self._finish_i

        if self.role == "responder" and self.state == HandshakeState.WAIT_INIT_FINISH:
            self._verify_finish(packet, expected_role_code=0)
            self._finish_i = packet
            self._finish_r = self._make_finish(role_code=1)
            self.state = HandshakeState.ESTABLISHED
            return self._finish_r

        if self.role == "initiator" and self.state == HandshakeState.WAIT_RESP_FINISH:
            self._verify_finish(packet, expected_role_code=1)
            self._finish_r = packet
            self.state = HandshakeState.ESTABLISHED
            return None

        raise HandshakeError(f"Unexpected packet for state {self.state.value}")

    def send_text(self, text: str, max_packet_size: int = 1024) -> bytes:
        if not self.established or self.session is None:
            raise HandshakeError("Peer session is not established")
        if not isinstance(text, str):
            raise TypeError("Text message must be a string")
        if not isinstance(max_packet_size, int) or max_packet_size < 32:
            raise ValueError("Configured modem packet limit must be at least 32 bytes")
        payload = text.encode("utf-8")
        encrypted_size = len(payload) + 32  # 16-byte packet header + 16-byte AEAD tag
        if encrypted_size > max_packet_size:
            raise ValueError(
                f"Message needs {encrypted_size} packet bytes; configured modem limit is {max_packet_size} bytes"
            )
        return self.session.encrypt_text(text)

    def receive_text(self, packet: bytes) -> str:
        if not self.established or self.session is None:
            raise HandshakeError("Peer session is not established")
        return self.session.decrypt_text(packet)

    def close(self):
        self.session = None
        self._local_ephemeral = None
        self._peer_ephemeral = None
        self._local_nonce = None
        self._peer_nonce = None
        self._hello_i = None
        self._hello_r = None
        self._finish_i = None
        self._finish_r = None
        self._transcript_hash = None
        self.state = HandshakeState.CLOSED
