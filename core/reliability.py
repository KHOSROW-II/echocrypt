"""Stop-and-wait reliable text delivery over the existing acoustic channel.

This module has no sockets, network clients, servers, or internet transport.
It only creates authenticated DATA/ACK packets; core.transport carries them
through the configured audio input/output devices.
"""

import hashlib
import os
from collections import OrderedDict, deque
from dataclasses import dataclass

from core.crypto import ReplayError

DATA_MAGIC = b"ECTD"
ACK_MAGIC = b"ECA1"
TEXT_AAD = b"ECHOCRYPT/RELIABLE-TEXT/v1"
ACK_AAD = b"ECHOCRYPT/ACK/v1"
MESSAGE_ID_SIZE = 8
ACK_PLAINTEXT_SIZE = len(ACK_MAGIC) + MESSAGE_ID_SIZE
# SecureChannel wire packet = encrypted plaintext + 16-byte header + 16-byte tag.
ACK_PACKET_SIZE = ACK_PLAINTEXT_SIZE + 32


class ReliableProtocolError(ValueError):
    """Malformed or unexpected reliable-transfer packet."""


@dataclass(frozen=True)
class OutgoingMessage:
    message_id: bytes
    packet: bytes

    @property
    def message_id_hex(self) -> str:
        return self.message_id.hex()


@dataclass(frozen=True)
class IncomingMessage:
    message_id: bytes
    text: str | None
    duplicate: bool
    ack_packet: bytes


class ReliableTextChannel:
    """Authenticated stop-and-wait transfer over a PeerHandshake session.

    Retries must retransmit the exact same DATA ciphertext. The receiver keeps
    a bounded cache of previously authenticated ciphertexts so a duplicate
    can be ACKed again without delivering the text twice or tripping the AEAD
    replay window.
    """

    def __init__(self, handshake, duplicate_cache_size: int = 128):
        if not isinstance(duplicate_cache_size, int) or not 1 <= duplicate_cache_size <= 4096:
            raise ValueError("duplicate_cache_size must be between 1 and 4096")
        self.handshake = handshake
        self.duplicate_cache_size = duplicate_cache_size
        self._received = OrderedDict()  # SHA-256(ciphertext) -> (message_id, text)
        self._message_ids = {}  # message_id -> SHA-256(ciphertext)
        self._pending_incoming = deque()  # authenticated DATA received while waiting for an ACK

    def _session(self):
        if not getattr(self.handshake, "established", False) or self.handshake.session is None:
            raise ReliableProtocolError("Authenticated acoustic session is not established")
        return self.handshake.session

    def defer_incoming(self, incoming: IncomingMessage) -> None:
        """Queue an authenticated DATA message encountered during an ACK wait.

        A bidirectional stop-and-wait exchange can cross in flight: while one
        peer waits for its ACK, the other peer may already be sending its DATA.
        The caller ACKs that packet immediately and defers application delivery
        until the normal receive path resumes.
        """
        if not isinstance(incoming, IncomingMessage):
            raise TypeError("incoming must be an IncomingMessage")
        if incoming.duplicate:
            return
        if len(self._pending_incoming) >= self.duplicate_cache_size:
            raise ReliableProtocolError("Pending authenticated DATA queue is full")
        self._pending_incoming.append(incoming)

    def pop_pending(self) -> IncomingMessage | None:
        """Return the oldest already-authenticated incoming message, if any."""
        if not self._pending_incoming:
            return None
        return self._pending_incoming.popleft()

    def prepare(self, text: str, max_packet_size: int = 1024) -> OutgoingMessage:
        session = self._session()
        if not isinstance(text, str):
            raise TypeError("Text message must be a string")
        encoded = text.encode("utf-8")
        if not encoded:
            raise ValueError("Empty messages are not sent")
        if not isinstance(max_packet_size, int) or max_packet_size < ACK_PACKET_SIZE:
            raise ValueError("max_packet_size is too small for reliable packets")

        message_id = os.urandom(MESSAGE_ID_SIZE)
        plaintext = DATA_MAGIC + message_id + encoded
        packet_size = len(plaintext) + 32
        if packet_size > max_packet_size:
            maximum_text = max_packet_size - 32 - len(DATA_MAGIC) - MESSAGE_ID_SIZE
            raise ValueError(
                f"Message needs {packet_size} packet bytes; modem limit is "
                f"{max_packet_size}. Maximum UTF-8 payload is {maximum_text} bytes."
            )
        packet = session.tx.encrypt(plaintext, TEXT_AAD)
        return OutgoingMessage(message_id, packet)

    def _make_ack(self, message_id: bytes) -> bytes:
        if len(message_id) != MESSAGE_ID_SIZE:
            raise ReliableProtocolError("Invalid message identifier")
        return self._session().tx.encrypt(ACK_MAGIC + message_id, ACK_AAD)

    def receive(self, packet: bytes) -> IncomingMessage:
        session = self._session()
        packet = bytes(packet)
        digest = hashlib.sha256(packet).digest()

        # An exact duplicate was previously AEAD-authenticated. Re-ACK it,
        # but do not display or process the plaintext a second time.
        cached = self._received.get(digest)
        if cached is not None:
            message_id, _text = cached
            self._received.move_to_end(digest)
            return IncomingMessage(message_id, None, True, self._make_ack(message_id))

        plaintext = session.rx.decrypt(packet, TEXT_AAD)
        if len(plaintext) < len(DATA_MAGIC) + MESSAGE_ID_SIZE:
            raise ReliableProtocolError("Truncated reliable DATA payload")
        if plaintext[:len(DATA_MAGIC)] != DATA_MAGIC:
            raise ReliableProtocolError("Unexpected reliable DATA packet type")
        message_id = plaintext[len(DATA_MAGIC):len(DATA_MAGIC) + MESSAGE_ID_SIZE]
        payload = plaintext[len(DATA_MAGIC) + MESSAGE_ID_SIZE:]
        if not payload:
            raise ReliableProtocolError("Empty reliable DATA payload")

        previous_digest = self._message_ids.get(message_id)
        if previous_digest is not None and previous_digest != digest:
            raise ReliableProtocolError("Message identifier was reused with different ciphertext")
        try:
            text = payload.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise ReliableProtocolError("Authenticated DATA is not valid UTF-8") from exc

        self._received[digest] = (message_id, text)
        self._message_ids[message_id] = digest
        while len(self._received) > self.duplicate_cache_size:
            old_digest, (old_id, _old_text) = self._received.popitem(last=False)
            if self._message_ids.get(old_id) == old_digest:
                del self._message_ids[old_id]

        return IncomingMessage(message_id, text, False, self._make_ack(message_id))

    def verify_ack(self, packet: bytes, expected_message_id: bytes) -> bool:
        session = self._session()
        if len(expected_message_id) != MESSAGE_ID_SIZE:
            raise ValueError("Invalid expected message identifier")
        plaintext = session.rx.decrypt(bytes(packet), ACK_AAD)
        if len(plaintext) != ACK_PLAINTEXT_SIZE or plaintext[:len(ACK_MAGIC)] != ACK_MAGIC:
            raise ReliableProtocolError("Malformed ACK packet")
        received_id = plaintext[len(ACK_MAGIC):]
        if received_id != expected_message_id:
            raise ReliableProtocolError("ACK does not match the outstanding message")
        return True
