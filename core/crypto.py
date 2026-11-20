import os
import struct
import threading

from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305

MAGIC = b"ECP1"
HEAD = struct.Struct("!4s4sQ")
REPLAY_WINDOW = 1024


class ReplayError(ValueError):
    """Raised when a packet is duplicated or outside the replay window."""


class SecureChannel:
    """One-way AEAD channel with a bounded replay window.

    Use a distinct key for each traffic direction. The receiver pins the first
    successfully authenticated nonce prefix, preventing unbounded per-prefix
    replay state growth.
    """

    def __init__(self, key: bytes, replay_window: int = REPLAY_WINDOW):
        if not isinstance(key, (bytes, bytearray)) or len(key) != 32:
            raise ValueError("Key must be exactly 32 bytes")
        if not isinstance(replay_window, int) or not 1 <= replay_window <= 65536:
            raise ValueError("Replay window must be between 1 and 65536")
        self.aead = ChaCha20Poly1305(bytes(key))
        self.prefix = os.urandom(4)
        self.counter = 0
        # Session TX is shared by the background receiver (ACK generation)
        # and the UI sender queue. Serialize nonce allocation to prevent reuse.
        self._lock = threading.RLock()
        self.replay_window = replay_window
        self._rx_prefix = None
        self._highest = -1
        self._bitmap = 0
        self._mask = (1 << replay_window) - 1

    def encrypt(self, data: bytes, aad: bytes = b"") -> bytes:
        if not isinstance(data, (bytes, bytearray, memoryview)):
            raise TypeError("Plaintext must be bytes-like")
        if not isinstance(aad, (bytes, bytearray, memoryview)):
            raise TypeError("AAD must be bytes-like")
        with self._lock:
            if self.counter >= 2**64:
                raise OverflowError("Rekey required")
            number = self.counter
            self.counter += 1
            header = HEAD.pack(MAGIC, self.prefix, number)
            nonce = self.prefix + struct.pack("!Q", number)
            return header + self.aead.encrypt(nonce, bytes(data), header + bytes(aad))

    def _check_replay(self, number: int) -> None:
        if self._highest < 0 or number > self._highest:
            return
        distance = self._highest - number
        if distance >= self.replay_window:
            raise ReplayError("Packet is older than the replay window")
        if self._bitmap & (1 << distance):
            raise ReplayError("Replay detected")

    def _mark_received(self, number: int) -> None:
        if self._highest < 0:
            self._highest = number
            self._bitmap = 1
        elif number > self._highest:
            shift = number - self._highest
            self._bitmap = 1 if shift >= self.replay_window else ((self._bitmap << shift) | 1) & self._mask
            self._highest = number
        else:
            self._bitmap |= 1 << (self._highest - number)

    def decrypt(self, packet: bytes, aad: bytes = b"") -> bytes:
        if not isinstance(packet, (bytes, bytearray, memoryview)):
            raise TypeError("Packet must be bytes-like")
        if not isinstance(aad, (bytes, bytearray, memoryview)):
            raise TypeError("AAD must be bytes-like")
        packet = bytes(packet)
        if len(packet) < HEAD.size + 16:
            raise ValueError("Truncated packet")
        with self._lock:
            magic, prefix, number = HEAD.unpack(packet[:HEAD.size])
            if magic != MAGIC:
                raise ValueError("Bad packet magic")
            if self._rx_prefix is not None and prefix != self._rx_prefix:
                raise ValueError("Unexpected nonce prefix for this receive channel")
            self._check_replay(number)
            nonce = prefix + struct.pack("!Q", number)
            # Only commit prefix and replay state after AEAD authentication succeeds.
            plaintext = self.aead.decrypt(nonce, packet[HEAD.size:], packet[:HEAD.size] + bytes(aad))
            if self._rx_prefix is None:
                self._rx_prefix = prefix
            self._mark_received(number)
            return plaintext
