import os
import struct
import threading

from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305

MAGIC = b"ECP1"
HEAD = struct.Struct("!4s4sQ")
REPLAY_WINDOW = 1024
PADDING_MAGIC = b"ECPD"
PADDING_HEAD = struct.Struct("!4sI")
ALLOWED_PADDING_BLOCKS = (0, 32, 64, 128, 256)


class ReplayError(ValueError):
    """Raised when a packet is duplicated or outside the replay window."""


class SecureChannel:
    """One-way AEAD channel with a bounded replay window.

    Use a distinct key for each traffic direction. The receiver pins the first
    successfully authenticated nonce prefix, preventing unbounded per-prefix
    replay state growth.
    """

    def __init__(self, key: bytes, replay_window: int = REPLAY_WINDOW,
                 padding_block: int = 0):
        if not isinstance(key, (bytes, bytearray)) or len(key) != 32:
            raise ValueError("Key must be exactly 32 bytes")
        if not isinstance(replay_window, int) or not 1 <= replay_window <= 65536:
            raise ValueError("Replay window must be between 1 and 65536")
        if isinstance(padding_block, bool) or not isinstance(padding_block, int) or padding_block not in ALLOWED_PADDING_BLOCKS:
            raise ValueError(f"Padding block must be one of {ALLOWED_PADDING_BLOCKS}")
        self.padding_block = padding_block
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

    def encrypted_size(self, plaintext_size: int) -> int:
        """Return the exact ciphertext size for a plaintext length."""
        if isinstance(plaintext_size, bool) or not isinstance(plaintext_size, int) or plaintext_size < 0:
            raise ValueError("plaintext_size must be a non-negative integer")
        protected_size = plaintext_size
        if self.padding_block:
            protected_size += PADDING_HEAD.size
            protected_size = (
                (protected_size + self.padding_block - 1) // self.padding_block
            ) * self.padding_block
        return HEAD.size + protected_size + 16

    def _pad(self, data: bytes) -> bytes:
        if not self.padding_block:
            return data
        protected_size = self.encrypted_size(len(data)) - HEAD.size - 16
        prefix = PADDING_HEAD.pack(PADDING_MAGIC, len(data))
        body = prefix + data
        padding = protected_size - len(body)
        return body + (os.urandom(padding) if padding else b"")

    def _unpad(self, data: bytes) -> bytes:
        if not self.padding_block:
            return data
        if len(data) < PADDING_HEAD.size or len(data) % self.padding_block:
            raise ValueError("Malformed padded secure-channel plaintext")
        magic, original_size = PADDING_HEAD.unpack(data[:PADDING_HEAD.size])
        available = len(data) - PADDING_HEAD.size
        if magic != PADDING_MAGIC or original_size > available:
            raise ValueError("Invalid secure-channel padding envelope")
        return data[PADDING_HEAD.size:PADDING_HEAD.size + original_size]

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
            protected = self._pad(bytes(data))
            return header + self.aead.encrypt(nonce, protected, header + bytes(aad))

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
            # Padding is removed only after AEAD authentication succeeds.
            plaintext = self._unpad(plaintext)
            if self._rx_prefix is None:
                self._rx_prefix = prefix
            self._mark_received(number)
            return plaintext
