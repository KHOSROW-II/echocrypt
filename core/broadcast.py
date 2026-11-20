"""Open-room ECHO-CRYPT broadcast framing.

Broadcast has no peer handshake, membership list, or user-provisioned key.
Compatibility is provided by a shared protocol transform embedded in every
ECHO-CRYPT build. Because that transform is public and shipped with the app,
it is NOT confidential encryption against anyone who inspects the software or
implements the protocol. It mainly prevents an unaware audio recorder from
interpreting the waveform as readable application data.
"""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, field
from enum import IntEnum
import hashlib
import json
import os
from pathlib import Path
import re
import struct
import threading
import time
import uuid
import zlib

from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305

MAGIC = b"ECBR"
VERSION = 1
VERSION_WITH_FRAGMENT_HEADER = 2
HEADER = struct.Struct("!4sBBBB8s8sHHI")
CRC = struct.Struct("!I")
AEAD_TAG_SIZE = 16

# Every file/voice data frame (and its manifest) carries a small application
# fragment descriptor twice: once before the fragment and once after it. Each
# copy has its own CRC32. The copies are separated by the payload so a short
# burst that damages one edge does not necessarily destroy both descriptors.
# ECF3 also has its own duplicated, FEC-protected modem header; this descriptor
# is the application-level equivalent and binds the chunk index/type/profile.
FRAGMENT_MAGIC = b"ECFH"
FRAGMENT_VERSION = 1
FRAGMENT_HEADER_BASE = struct.Struct("!4sBBBBHHHHB")
FRAGMENT_HEADER = struct.Struct("!4sBBBBHHHHBI")
FRAGMENT_HEADER_SIZE = FRAGMENT_HEADER.size
FRAGMENT_ENVELOPE_SIZE = FRAGMENT_HEADER_SIZE * 2

MEDIA_FILE = 0
MEDIA_VOICE = 1
VOICE_CODEC_NONE = 0
VOICE_CODEC_IMA_ADPCM = 1
VOICE_CODEC2_700C = 2
VOICE_CODEC2_1300 = 3
FEC_ID_CONVOLUTIONAL = 3
FRAGMENT_FLAG_FHSS = 0x01

RETRY_REQUEST_MAGIC = b"ERQ2"
RETRY_REQUEST_V2 = struct.Struct("!4s8s8sBBH")
RETRY_NEED_HEADER = 0x01
RETRY_NEED_DATA = 0x02

# Public interoperability constant, deliberately not represented as a secret.
# Same protocol build on every device means no key exchange or setup.
BROADCAST_COMPAT_KEY = hashlib.sha256(
    b"ECHO-CRYPT/BROADCAST/PUBLIC-COMPATIBILITY-TRANSFORM/v1"
).digest()
# Public, deterministic FHSS schedule shared by every compatible Broadcast
# instance. This is not a secret and provides no anti-eavesdropping guarantee.
BROADCAST_HOP_KEY = hashlib.sha256(
    b"ECHO-CRYPT/BROADCAST/PUBLIC-FREQUENCY-SCHEDULE/v1"
).digest()


class BroadcastType(IntEnum):
    TEXT = 1
    FILE_META = 2
    FILE_CHUNK = 3
    RETRY_REQUEST = 4
    VOICE = 5
    VOICE_FRAME = 6


class BroadcastError(ValueError):
    pass


MAX_BROADCAST_MANIFEST_BYTES = 4096


def _decompress_manifest(data: bytes) -> bytes:
    """Decompress a public manifest with a strict output bound.

    Broadcast frames are unauthenticated and attacker-controlled. Calling
    zlib.decompress() without a limit lets a tiny compressed packet allocate
    an arbitrarily large buffer before the manifest's file-size checks run.
    """
    decoder = zlib.decompressobj()
    try:
        output = decoder.decompress(bytes(data), MAX_BROADCAST_MANIFEST_BYTES + 1)
        if len(output) > MAX_BROADCAST_MANIFEST_BYTES or decoder.unconsumed_tail:
            raise BroadcastError("Compressed broadcast manifest exceeds the size limit")
        tail = decoder.flush()
    except zlib.error as exc:
        raise BroadcastError("Invalid compressed broadcast manifest") from exc
    if len(output) + len(tail) > MAX_BROADCAST_MANIFEST_BYTES:
        raise BroadcastError("Compressed broadcast manifest exceeds the size limit")
    if not decoder.eof or decoder.unused_data:
        raise BroadcastError("Compressed broadcast manifest is truncated or has trailing data")
    return output + tail


@dataclass(frozen=True)
class BroadcastFrame:
    kind: BroadcastType
    sender_id: bytes
    message_id: bytes
    index: int
    count: int
    payload: bytes
    media_kind: int = MEDIA_FILE
    codec_id: int = VOICE_CODEC_NONE
    fec_id: int = FEC_ID_CONVOLUTIONAL
    symbol_rate: int = 0
    fragment_flags: int = 0
    has_fragment_header: bool = False


@dataclass
class _PartialMessage:
    kind: BroadcastType
    sender_id: bytes
    message_id: bytes
    count: int
    fragments: dict[int, bytes] = field(default_factory=dict)
    media_kind: int = MEDIA_FILE
    codec_id: int = VOICE_CODEC_NONE
    symbol_rate: int = 0
    fragment_flags: int = 0
    created_at: float = field(default_factory=time.monotonic)
    updated_at: float = field(default_factory=time.monotonic)

    @property
    def missing(self):
        return tuple(i for i in range(self.count) if i not in self.fragments)

    @property
    def complete(self):
        return len(self.fragments) == self.count


@dataclass(frozen=True)
class BroadcastDelivery:
    kind: BroadcastType
    sender_id: bytes
    message_id: bytes
    payload: bytes
    complete: bool
    missing: tuple[int, ...] = ()
    preview: str | None = None
    path: Path | None = None
    duplicate: bool = False
    total_fragments: int = 0
    media_kind: int = MEDIA_FILE
    codec_id: int = VOICE_CODEC_NONE


class BroadcastCodec:
    """Versioned, no-handshake broadcast packet codec."""

    def __init__(self, sender_id: bytes | None = None, *, max_packet_size=1024):
        self.sender_id = bytes(sender_id or os.urandom(8))
        if len(self.sender_id) != 8:
            raise ValueError("Broadcast sender ID must be exactly 8 bytes")
        if not isinstance(max_packet_size, int) or max_packet_size < HEADER.size + 24:
            raise ValueError("Broadcast modem packet size is too small")
        self.max_packet_size = max_packet_size
        self.key = BROADCAST_COMPAT_KEY
        self.aead = ChaCha20Poly1305(self.key)

    @property
    def max_fragment_payload(self):
        return self.max_packet_size - HEADER.size - AEAD_TAG_SIZE - CRC.size

    @staticmethod
    def _nonce(sender_id, message_id, kind, index):
        material = (b"ECHO-CRYPT/BROADCAST/NONCE/v1" + bytes(sender_id)
                    + bytes(message_id) + bytes([int(kind)]) + struct.pack("!H", int(index)))
        return hashlib.blake2s(material, digest_size=12).digest()

    @staticmethod
    def _build_fragment_header(*, media_kind, codec_id, index, count,
                               payload_length, symbol_rate, fec_id=FEC_ID_CONVOLUTIONAL,
                               flags=FRAGMENT_FLAG_FHSS):
        base = FRAGMENT_HEADER_BASE.pack(
            FRAGMENT_MAGIC, FRAGMENT_VERSION, int(media_kind), int(codec_id),
            int(fec_id), int(index), int(count), int(payload_length),
            int(symbol_rate), int(flags),
        )
        checksum = zlib.crc32(base) & 0xFFFFFFFF
        return FRAGMENT_HEADER.pack(
            FRAGMENT_MAGIC, FRAGMENT_VERSION, int(media_kind), int(codec_id),
            int(fec_id), int(index), int(count), int(payload_length),
            int(symbol_rate), int(flags), checksum,
        )

    @classmethod
    def _wrap_fragment(cls, payload, *, media_kind, codec_id, index, count,
                       symbol_rate, fec_id=FEC_ID_CONVOLUTIONAL,
                       flags=FRAGMENT_FLAG_FHSS):
        data = bytes(payload)
        descriptor = cls._build_fragment_header(
            media_kind=media_kind, codec_id=codec_id, index=index, count=count,
            payload_length=len(data), symbol_rate=symbol_rate,
            fec_id=fec_id, flags=flags,
        )
        return descriptor + data + descriptor

    @staticmethod
    def _read_fragment_header(raw):
        if len(raw) != FRAGMENT_HEADER_SIZE:
            return None
        try:
            values = FRAGMENT_HEADER.unpack(raw)
            (magic, version, media_kind, codec_id, fec_id, index, count,
             payload_length, symbol_rate, flags, received_crc) = values
            base = FRAGMENT_HEADER_BASE.pack(
                magic, version, media_kind, codec_id, fec_id, index, count,
                payload_length, symbol_rate, flags,
            )
        except struct.error:
            return None
        if magic != FRAGMENT_MAGIC or version != FRAGMENT_VERSION:
            return None
        if zlib.crc32(base) & 0xFFFFFFFF != received_crc:
            return None
        if media_kind not in {MEDIA_FILE, MEDIA_VOICE}:
            return None
        if codec_id not in {VOICE_CODEC_NONE, VOICE_CODEC_IMA_ADPCM,
                            VOICE_CODEC2_700C, VOICE_CODEC2_1300}:
            return None
        if fec_id != FEC_ID_CONVOLUTIONAL or symbol_rate not in {100, 200, 400, 800}:
            return None
        if count < 1 or index >= count or flags & ~FRAGMENT_FLAG_FHSS:
            return None
        return {
            "media_kind": media_kind, "codec_id": codec_id, "fec_id": fec_id,
            "index": index, "count": count, "payload_length": payload_length,
            "symbol_rate": symbol_rate, "flags": flags,
        }

    @classmethod
    def _unwrap_fragment(cls, payload, *, outer_index, outer_count):
        raw = bytes(payload)
        if len(raw) < FRAGMENT_ENVELOPE_SIZE:
            # Legacy ECBR frames did not carry the per-fragment application
            # descriptor. Keep them readable during the transition period.
            return None, raw
        first = cls._read_fragment_header(raw[:FRAGMENT_HEADER_SIZE])
        second = cls._read_fragment_header(raw[-FRAGMENT_HEADER_SIZE:])
        if first is None and second is None:
            # Allow legacy manifests/chunks whose payload merely happens to be
            # short enough; new-format magic with invalid copies is rejected.
            if raw.startswith(FRAGMENT_MAGIC) or raw[-FRAGMENT_HEADER_SIZE:].startswith(FRAGMENT_MAGIC):
                raise BroadcastError("Both duplicated fragment headers are invalid")
            return None, raw
        if first is not None and second is not None and first != second:
            raise BroadcastError("Duplicated fragment headers disagree")
        descriptor = first or second
        data = raw[FRAGMENT_HEADER_SIZE:-FRAGMENT_HEADER_SIZE]
        if descriptor["payload_length"] != len(data):
            raise BroadcastError("Fragment header payload length mismatch")
        if descriptor["index"] != int(outer_index) or descriptor["count"] != int(outer_count):
            raise BroadcastError("Fragment header index/count differs from ECBR transport header")
        return descriptor, data

    def encode(self, kind, payload, *, message_id=None, index=0, count=1,
               sender_id=None, fragment_header=None):
        """Encode one broadcast frame.

        Version 2 places a CRC-protected fragment descriptor twice outside the
        encrypted payload. The two copies are independently checked. A single
        damaged copy can therefore be discarded while the valid descriptor is
        still bound to the AEAD associated data. Legacy v1 packets remain
        supported for text and older file transfers.
        """
        try:
            packet_type = BroadcastType(kind)
        except ValueError as exc:
            raise BroadcastError("Unknown broadcast frame type") from exc
        data = bytes(payload)
        sid = bytes(self.sender_id if sender_id is None else sender_id)
        mid = bytes(message_id or os.urandom(8))
        if len(sid) != 8 or len(mid) != 8:
            raise BroadcastError("Sender and message IDs must be exactly 8 bytes")
        if not 1 <= count <= 65535 or not 0 <= index < count:
            raise BroadcastError("Invalid broadcast fragment index/count")
        encrypted_len = len(data) + AEAD_TAG_SIZE
        version = VERSION_WITH_FRAGMENT_HEADER if fragment_header is not None else VERSION
        descriptor = None
        if fragment_header is not None:
            descriptor_values = dict(fragment_header)
            descriptor = self._build_fragment_header(
                media_kind=descriptor_values["media_kind"],
                codec_id=descriptor_values["codec_id"],
                index=index, count=count, payload_length=len(data),
                symbol_rate=descriptor_values["symbol_rate"],
                fec_id=descriptor_values.get("fec_id", FEC_ID_CONVOLUTIONAL),
                flags=descriptor_values.get("flags", FRAGMENT_FLAG_FHSS),
            )
        extra = 2 * FRAGMENT_HEADER_SIZE if descriptor is not None else 0
        total = HEADER.size + encrypted_len + CRC.size + extra
        if total > self.max_packet_size:
            raise BroadcastError(
                f"Broadcast frame is {total} bytes; maximum is {self.max_packet_size}"
            )
        header = HEADER.pack(MAGIC, version, int(packet_type), 0, 0,
                             sid, mid, int(index), int(count), encrypted_len)
        nonce = self._nonce(sid, mid, packet_type, index)
        aad = header if descriptor is None else header + descriptor
        encrypted = self.aead.encrypt(nonce, data, aad)
        if descriptor is None:
            body = header + encrypted
            return body + CRC.pack(zlib.crc32(body) & 0xFFFFFFFF)
        # CRC covers the canonical descriptor once. Either physical copy can
        # supply that canonical value during receive if the other was damaged.
        body = header + descriptor + encrypted
        checksum = CRC.pack(zlib.crc32(body) & 0xFFFFFFFF)
        return header + descriptor + encrypted + descriptor + checksum

    def decode(self, packet) -> BroadcastFrame:
        raw = bytes(packet)
        if len(raw) < HEADER.size + AEAD_TAG_SIZE + CRC.size:
            raise BroadcastError("Truncated broadcast packet")
        try:
            magic, version, kind, flags, reserved, sid, mid, index, count, size = HEADER.unpack(
                raw[:HEADER.size]
            )
            packet_type = BroadcastType(kind)
        except (struct.error, ValueError) as exc:
            raise BroadcastError("Malformed broadcast header") from exc
        if magic != MAGIC or version not in {VERSION, VERSION_WITH_FRAGMENT_HEADER}:
            raise BroadcastError("Unsupported broadcast protocol version")
        if flags or reserved:
            raise BroadcastError("Unsupported broadcast header flags")
        if count < 1 or index >= count:
            raise BroadcastError("Invalid broadcast fragment coordinates")
        header = raw[:HEADER.size]
        nonce = self._nonce(sid, mid, packet_type, index)
        if version == VERSION_WITH_FRAGMENT_HEADER:
            expected = HEADER.size + FRAGMENT_HEADER_SIZE + size + FRAGMENT_HEADER_SIZE + CRC.size
            if size < AEAD_TAG_SIZE or len(raw) != expected:
                raise BroadcastError("Broadcast v2 packet length does not match its header")
            first_at = HEADER.size
            cipher_at = first_at + FRAGMENT_HEADER_SIZE
            second_at = cipher_at + size
            first = self._read_fragment_header(raw[first_at:cipher_at])
            second = self._read_fragment_header(raw[second_at:second_at + FRAGMENT_HEADER_SIZE])
            if first is None and second is None:
                raise BroadcastError("Both duplicated broadcast fragment headers are invalid")
            if first is not None and second is not None and first != second:
                raise BroadcastError("Duplicated broadcast fragment headers disagree")
            descriptor_info = first or second
            descriptor_bytes = raw[first_at:cipher_at] if first is not None else raw[second_at:second_at + FRAGMENT_HEADER_SIZE]
            if (descriptor_info["index"] != index or descriptor_info["count"] != count or
                    descriptor_info["payload_length"] != size - AEAD_TAG_SIZE):
                raise BroadcastError("Broadcast fragment descriptor conflicts with transport header")
            encrypted = raw[cipher_at:second_at]
            received_crc = CRC.unpack(raw[-CRC.size:])[0]
            canonical_body = header + descriptor_bytes + encrypted
            if zlib.crc32(canonical_body) & 0xFFFFFFFF != received_crc:
                raise BroadcastError("Broadcast v2 transport CRC mismatch")
            aad = header + descriptor_bytes
            try:
                plaintext = self.aead.decrypt(nonce, encrypted, aad)
            except Exception as exc:
                raise BroadcastError("Broadcast compatibility transform authentication failed") from exc
            return BroadcastFrame(
                packet_type, sid, mid, index, count, plaintext,
                media_kind=descriptor_info["media_kind"],
                codec_id=descriptor_info["codec_id"],
                fec_id=descriptor_info["fec_id"],
                symbol_rate=descriptor_info["symbol_rate"],
                fragment_flags=descriptor_info["flags"],
                has_fragment_header=True,
            )
        if size != len(raw) - HEADER.size - CRC.size:
            raise BroadcastError("Broadcast packet length does not match its header")
        body, received_crc = raw[:-CRC.size], CRC.unpack(raw[-CRC.size:])[0]
        if zlib.crc32(body) & 0xFFFFFFFF != received_crc:
            raise BroadcastError("Broadcast transport CRC mismatch")
        try:
            plaintext = self.aead.decrypt(nonce, raw[HEADER.size:-CRC.size], header)
        except Exception as exc:
            raise BroadcastError("Broadcast compatibility transform authentication failed") from exc
        return BroadcastFrame(packet_type, sid, mid, index, count, plaintext)

    @staticmethod
    def _split_utf8(text, max_bytes):
        chunks, current, used = [], [], 0
        for char in text:
            encoded = char.encode("utf-8")
            if used + len(encoded) > max_bytes and current:
                chunks.append("".join(current).encode("utf-8"))
                current, used = [], 0
            if len(encoded) > max_bytes:
                raise BroadcastError("A UTF-8 code point exceeds the fragment capacity")
            current.append(char)
            used += len(encoded)
        if current or not chunks:
            chunks.append("".join(current).encode("utf-8"))
        return chunks

    def encode_text(self, text, *, message_id=None):
        if not isinstance(text, str) or not text:
            raise BroadcastError("Broadcast text must be a non-empty string")
        mid = bytes(message_id or os.urandom(8))
        chunks = self._split_utf8(text, self.max_fragment_payload)
        if len(chunks) > 65535:
            raise BroadcastError("Broadcast text exceeds the fragment-count limit")
        return [
            self.encode(BroadcastType.TEXT, chunk, message_id=mid, index=i, count=len(chunks))
            for i, chunk in enumerate(chunks)
        ]

    def encode_file(self, path, *, chunk_size=None, message_id=None,
                    symbol_rate=800, fec_mode="conv", hopping=True, cancel_event=None):
        source = Path(path).expanduser()
        if not source.is_file():
            raise FileNotFoundError(source)
        name = source.name
        if not name or len(name) > 200 or name in {".", ".."}:
            raise BroadcastError("Invalid broadcast filename")
        if cancel_event is not None and cancel_event.is_set():
            raise BroadcastError("Broadcast file encoding cancelled")
        size = source.stat().st_size
        media_kind = MEDIA_VOICE if source.suffix.casefold() == ".ecv" else MEDIA_FILE
        if media_kind == MEDIA_VOICE:
            with source.open("rb") as stream:
                voice_magic = stream.read(4)
        else:
            voice_magic = b""
        codec_id = {
            b"ECV1": VOICE_CODEC_IMA_ADPCM,
            b"ECV2": VOICE_CODEC2_700C,
            b"ECV3": VOICE_CODEC2_1300,
        }.get(voice_magic, VOICE_CODEC_NONE)
        if media_kind == MEDIA_VOICE and codec_id == VOICE_CODEC_NONE:
            raise BroadcastError("Unknown .ecv voice codec; refusing to send an unreadable voice note")
        max_chunk = self.max_fragment_payload - FRAGMENT_ENVELOPE_SIZE
        suggested_chunk = 192 if media_kind == MEDIA_VOICE else 512
        chunk_size = min(max_chunk, int(chunk_size or suggested_chunk))
        if chunk_size < 1:
            raise BroadcastError("No room for broadcast file payload")
        total = (size + chunk_size - 1) // chunk_size
        if total > 65535:
            raise BroadcastError("Broadcast file requires too many chunks")
        digest = hashlib.sha256()
        with source.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                if cancel_event is not None and cancel_event.is_set():
                    raise BroadcastError("Broadcast file encoding cancelled")
                digest.update(block)
        if cancel_event is not None and cancel_event.is_set():
            raise BroadcastError("Broadcast file encoding cancelled")
        mid = bytes(message_id or os.urandom(8))
        # A compact, compressed manifest keeps small transfers usable on
        # lower max-packet profiles. Per-fragment headers duplicate the codec,
        # FEC, hopping, symbol-rate and fragment coordinates; only file-level
        # properties need to remain in the manifest.
        meta = {"n": name, "s": size, "h": digest.hexdigest(),
                "c": chunk_size, "t": total}
        manifest = b"Z" + zlib.compress(
            json.dumps(meta, separators=(",", ":")).encode("utf-8"), level=9
        )
        descriptor = {
            "media_kind": media_kind, "codec_id": codec_id,
            "symbol_rate": int(symbol_rate), "fec_id": FEC_ID_CONVOLUTIONAL,
            "flags": FRAGMENT_FLAG_FHSS if hopping else 0,
        }
        frames = [self.encode(BroadcastType.FILE_META, manifest,
                              message_id=mid, fragment_header=descriptor)]
        with source.open("rb") as stream:
            for index in range(total):
                if cancel_event is not None and cancel_event.is_set():
                    raise BroadcastError("Broadcast file encoding cancelled")
                chunk = stream.read(chunk_size)
                frames.append(self.encode(
                    BroadcastType.FILE_CHUNK, chunk, message_id=mid,
                    index=index, count=max(1, total), fragment_header=descriptor,
                ))
        return mid, frames

    def encode_retry_request(self, target_sender_id, target_message_id, kind, missing,
                             request_flags=None):
        target_sender_id = bytes(target_sender_id)
        target_message_id = bytes(target_message_id)
        if len(target_sender_id) != 8 or len(target_message_id) != 8:
            raise BroadcastError("Retry target IDs must be exactly 8 bytes")
        indices = tuple(sorted({int(i) for i in missing}))
        if not indices or any(not 0 <= i <= 65535 for i in indices):
            raise BroadcastError("Invalid retry-request fragment list")
        # The request is itself one acoustic frame. Bound each request to the
        # actual modem payload size; remaining indices are requested on later
        # passes after the first repair changes the missing set.
        base_size = RETRY_REQUEST_V2.size
        # Stay below the receiver's bounded TX queue so one malicious or
        # unusually damaged file cannot overflow it with a single request.
        maximum = min(96, (self.max_fragment_payload - base_size) // 2)
        if maximum < 1:
            raise BroadcastError("Modem frame cannot hold a selective retry request")
        indices = indices[:maximum]
        packet_type = BroadcastType(kind)
        if request_flags is None:
            request_flags = (RETRY_NEED_HEADER if packet_type == BroadcastType.FILE_META
                             else RETRY_NEED_HEADER | RETRY_NEED_DATA)
        request_flags = int(request_flags)
        if not request_flags or request_flags & ~(RETRY_NEED_HEADER | RETRY_NEED_DATA):
            raise BroadcastError("Invalid selective retry request flags")
        if packet_type == BroadcastType.FILE_META and request_flags & RETRY_NEED_DATA:
            raise BroadcastError("A manifest request cannot request chunk payload data")
        body = RETRY_REQUEST_V2.pack(
            RETRY_REQUEST_MAGIC, bytes(target_sender_id), bytes(target_message_id),
            int(packet_type), request_flags, len(indices),
        )
        body += struct.pack("!" + "H" * len(indices), *indices)
        return self.encode(BroadcastType.RETRY_REQUEST, body)

    @staticmethod
    def parse_retry_request(payload):
        raw = bytes(payload)
        if raw.startswith(RETRY_REQUEST_MAGIC):
            if len(raw) < RETRY_REQUEST_V2.size:
                raise BroadcastError("Truncated broadcast retry request")
            _magic, target_sid, mid, kind, flags, count = RETRY_REQUEST_V2.unpack(
                raw[:RETRY_REQUEST_V2.size]
            )
            offset = RETRY_REQUEST_V2.size
            if count == 0 or count > 4096 or len(raw) != offset + count * 2:
                raise BroadcastError("Malformed broadcast retry-request index list")
            if not flags or flags & ~(RETRY_NEED_HEADER | RETRY_NEED_DATA):
                raise BroadcastError("Malformed broadcast retry-request flags")
            try:
                packet_type = BroadcastType(kind)
            except ValueError as exc:
                raise BroadcastError("Unknown retry-request target type") from exc
            if packet_type == BroadcastType.FILE_META and flags & RETRY_NEED_DATA:
                raise BroadcastError("Manifest retry request contains an invalid data flag")
            indices = struct.unpack("!" + "H" * count, raw[offset:])
            return target_sid, mid, packet_type, flags, tuple(indices)
        base = struct.Struct("!8s8sBH")
        if len(raw) < base.size:
            raise BroadcastError("Truncated broadcast retry request")
        target_sid, mid, kind, count = base.unpack(raw[:base.size])
        if count == 0 or count > 4096 or len(raw) != base.size + count * 2:
            raise BroadcastError("Malformed broadcast retry-request index list")
        try:
            packet_type = BroadcastType(kind)
        except ValueError as exc:
            raise BroadcastError("Unknown retry-request target type") from exc
        indices = struct.unpack("!" + "H" * count, raw[base.size:])
        # Legacy request: requesting a missing frame implies its descriptor and
        # data together. This lets new senders repair older receivers.
        flags = RETRY_NEED_HEADER if packet_type == BroadcastType.FILE_META else (
            RETRY_NEED_HEADER | RETRY_NEED_DATA
        )
        return target_sid, mid, packet_type, flags, tuple(indices)


class BroadcastReassembler:
    """Duplicate-safe message/file reassembly with selective retry support."""

    def __init__(self, codec: BroadcastCodec, *, download_dir="downloads/broadcast",
                 max_file_size=16 * 1024 * 1024, max_partial_bytes=16 * 1024 * 1024,
                 max_partials=64, max_file_metadata=256, expiry_seconds=90.0):
        self.codec = codec
        self._lock = threading.RLock()
        self.download_dir = Path(download_dir)
        self.max_file_size = int(max_file_size)
        self.max_partial_bytes = int(max_partial_bytes)
        self.max_partials = int(max_partials)
        self.max_file_metadata = int(max_file_metadata)
        self.expiry_seconds = float(expiry_seconds)
        if (self.max_file_size < 0 or self.max_partial_bytes < 1 or
                self.max_partials < 1 or self.max_file_metadata < 1 or
                self.expiry_seconds <= 0):
            raise ValueError("Invalid broadcast reassembly limits")
        if self.max_file_size > self.max_partial_bytes:
            raise ValueError(
                "max_file_size cannot exceed max_partial_bytes because file "
                "fragments are retained in memory until final verification"
            )
        self._partials = OrderedDict()
        self._file_meta = {}
        self._file_meta_updated = {}
        self._completed = OrderedDict()

    @staticmethod
    def _safe_name(value):
        if not isinstance(value, str) or not value or len(value) > 200:
            raise BroadcastError("Invalid broadcast filename")
        if "\x00" in value or "/" in value or "\\" in value or value in {".", ".."}:
            raise BroadcastError("Unsafe broadcast filename")
        name = re.sub(r"[^\w.() \-\[\]]", "_", value, flags=re.UNICODE).strip(" .")
        if not name:
            raise BroadcastError("Broadcast filename is empty after sanitization")
        return name

    def _drop_file_metadata(self, sender_id, message_id):
        """Drop metadata and its dedupe marker so a retry can restore it."""
        sid, mid = bytes(sender_id), bytes(message_id)
        self._file_meta.pop((sid, mid), None)
        self._file_meta_updated.pop((sid, mid), None)
        self._completed.pop((sid, mid, BroadcastType.FILE_META), None)

    def _expire(self, now):
        for key, partial in list(self._partials.items()):
            if now - partial.updated_at > self.expiry_seconds:
                self._partials.pop(key, None)
        for key, updated in list(self._file_meta_updated.items()):
            if now - updated > self.expiry_seconds:
                self._drop_file_metadata(*key)

    def _make_room(self, incoming_size, *, protect_key=None, new_partial=False):
        if incoming_size < 0 or incoming_size > self.max_partial_bytes:
            raise BroadcastError("Broadcast fragment exceeds the reassembly memory limit")
        def used():
            return sum(sum(len(x) for x in p.fragments.values()) for p in self._partials.values())
        while ((new_partial and len(self._partials) >= self.max_partials) or
               used() + incoming_size > self.max_partial_bytes):
            candidates = [key for key in self._partials if key != protect_key]
            if not candidates:
                raise BroadcastError("Broadcast reassembly memory limit exceeded")
            key = candidates[0]
            old = self._partials.pop(key)
            if old.kind == BroadcastType.FILE_CHUNK:
                self._drop_file_metadata(old.sender_id, old.message_id)

    def _partial(self, frame):
        key = (frame.sender_id, frame.message_id, frame.kind)
        partial = self._partials.get(key)
        if partial is None:
            self._make_room(len(frame.payload), new_partial=True)
            partial = _PartialMessage(frame.kind, frame.sender_id,
                                      frame.message_id, frame.count,
                                      media_kind=frame.media_kind,
                                      codec_id=frame.codec_id,
                                      symbol_rate=frame.symbol_rate,
                                      fragment_flags=frame.fragment_flags)
            self._partials[key] = partial
        elif partial.count != frame.count:
            raise BroadcastError("Fragment count changed within a message")
        elif (partial.media_kind, partial.codec_id, partial.symbol_rate,
              partial.fragment_flags) != (
                  frame.media_kind, frame.codec_id, frame.symbol_rate,
                  frame.fragment_flags
              ):
            raise BroadcastError("Fragment descriptors changed within a message")
        prior = partial.fragments.get(frame.index)
        if prior is not None and prior != frame.payload:
            raise BroadcastError("Broadcast fragment index reused with different data")
        if prior is None:
            self._make_room(len(frame.payload), protect_key=key)
            partial.fragments[frame.index] = frame.payload
        partial.updated_at = time.monotonic()
        self._partials.move_to_end(key)
        return key, partial, prior is not None

    @staticmethod
    def _text_preview(partial):
        parts = []
        for index in range(partial.count):
            fragment = partial.fragments.get(index)
            if fragment is None:
                parts.append(f"⟦missing {index + 1}⟧")
            else:
                parts.append(fragment.decode("utf-8", errors="replace"))
        return "".join(parts)

    def has_file_manifest(self, sender_id, message_id):
        with self._lock:
            return (bytes(sender_id), bytes(message_id)) in self._file_meta

    def file_metadata(self, sender_id, message_id):
        with self._lock:
            meta = self._file_meta.get((bytes(sender_id), bytes(message_id)))
            return dict(meta) if meta is not None else None

    def file_progress(self, sender_id, message_id):
        with self._lock:
            sid, mid = bytes(sender_id), bytes(message_id)
            partial = self._partials.get((sid, mid, BroadcastType.FILE_CHUNK))
            meta = self._file_meta.get((sid, mid))
            if partial is None:
                if meta is None:
                    return 0, 0, ()
                total = int(meta["total_chunks"])
                return 0, total, tuple(range(total))
            return len(partial.fragments), partial.count, partial.missing

    def _complete_file_if_ready(self, sender_id, message_id, now):
        key = (sender_id, message_id, BroadcastType.FILE_CHUNK)
        partial = self._partials.get(key)
        meta = self._file_meta.get((sender_id, message_id))
        if partial is None or meta is None or not partial.complete:
            return None
        if partial.count != meta["total_chunks"]:
            raise BroadcastError("File fragment count differs from manifest")
        for index, chunk in partial.fragments.items():
            expected = (meta["chunk_size"] if index < partial.count - 1 else
                        meta["size"] - meta["chunk_size"] * (partial.count - 1))
            if len(chunk) != expected:
                raise BroadcastError("Broadcast file chunk length differs from manifest")
        payload = b"".join(partial.fragments[i] for i in range(partial.count))
        if len(payload) != meta["size"]:
            raise BroadcastError("Reassembled broadcast file size mismatch")
        if hashlib.sha256(payload).hexdigest() != meta["sha256"]:
            raise BroadcastError("Reassembled broadcast file SHA-256 mismatch")
        if (partial.media_kind != int(meta.get("media_kind", MEDIA_FILE)) or
                partial.codec_id != int(meta.get("codec_id", VOICE_CODEC_NONE))):
            raise BroadcastError("File chunk media/codec descriptor differs from manifest")
        path = self._write_file(meta["filename"], payload)
        self._partials.pop(key, None)
        self._file_meta.pop((sender_id, message_id), None)
        self._file_meta_updated.pop((sender_id, message_id), None)
        self._remember_completed(key, now)
        return BroadcastDelivery(BroadcastType.FILE_CHUNK, sender_id, message_id,
                                 b"", True, path=path, total_fragments=partial.count,
                                 media_kind=partial.media_kind, codec_id=partial.codec_id)

    def feed(self, packet) -> BroadcastDelivery | tuple[str, object] | None:
        with self._lock:
            return self._feed_unlocked(packet)

    def _feed_unlocked(self, packet) -> BroadcastDelivery | tuple[str, object] | None:
        frame = self.codec.decode(packet)
        now = time.monotonic()
        self._expire(now)
        if frame.kind in {BroadcastType.RETRY_REQUEST, BroadcastType.FILE_META} and (
            frame.index != 0 or frame.count != 1
        ):
            raise BroadcastError("Control and manifest frames must be single-fragment packets")
        if frame.kind == BroadcastType.RETRY_REQUEST:
            return ("retry_request", self.codec.parse_retry_request(frame.payload))
        descriptor = None
        if frame.kind in {BroadcastType.FILE_META, BroadcastType.FILE_CHUNK}:
            if frame.has_fragment_header:
                descriptor = {
                    "media_kind": frame.media_kind, "codec_id": frame.codec_id,
                    "fec_id": frame.fec_id, "symbol_rate": frame.symbol_rate,
                    "flags": frame.fragment_flags,
                }
            else:
                descriptor, unwrapped = self.codec._unwrap_fragment(
                    frame.payload, outer_index=frame.index, outer_count=frame.count
                )
                if descriptor is not None:
                    frame = BroadcastFrame(
                        frame.kind, frame.sender_id, frame.message_id,
                        frame.index, frame.count, unwrapped,
                        media_kind=descriptor["media_kind"],
                        codec_id=descriptor["codec_id"],
                        fec_id=descriptor["fec_id"],
                        symbol_rate=descriptor["symbol_rate"],
                        fragment_flags=descriptor["flags"],
                    )
        key = (frame.sender_id, frame.message_id, frame.kind)
        if key in self._completed:
            self._completed.move_to_end(key)
            return BroadcastDelivery(frame.kind, frame.sender_id, frame.message_id,
                                     b"", True, duplicate=True)
        key, partial, duplicate = self._partial(frame)

        if frame.kind == BroadcastType.FILE_META:
            try:
                manifest_bytes = frame.payload
                if manifest_bytes.startswith(b"Z"):
                    manifest_bytes = _decompress_manifest(manifest_bytes[1:])
                    compact = json.loads(manifest_bytes.decode("utf-8"))
                    meta = {"filename": compact["n"], "size": compact["s"],
                            "sha256": compact["h"], "chunk_size": compact["c"],
                            "total_chunks": compact["t"]}
                else:
                    meta = json.loads(manifest_bytes.decode("utf-8"))
                filename = self._safe_name(meta["filename"])
                size_value = meta["size"]
                chunk_value = meta["chunk_size"]
                total_value = meta["total_chunks"]
                if any(isinstance(value, bool) or not isinstance(value, int)
                       for value in (size_value, chunk_value, total_value)):
                    raise TypeError("Manifest numeric fields must be integers")
                size = size_value
                chunk_size = chunk_value
                total = total_value
                digest = str(meta["sha256"])
                media_kind = meta.get("media_kind", frame.media_kind)
                codec_id = meta.get("codec_id", frame.codec_id)
                fragment_header_version = meta.get(
                    "fragment_header_version", FRAGMENT_VERSION if descriptor is not None else 0
                )
                if (isinstance(media_kind, bool) or not isinstance(media_kind, int) or
                        media_kind not in {MEDIA_FILE, MEDIA_VOICE}):
                    raise TypeError("Manifest media_kind is invalid")
                if (isinstance(codec_id, bool) or not isinstance(codec_id, int) or
                        codec_id not in {VOICE_CODEC_NONE, VOICE_CODEC_IMA_ADPCM,
                                         VOICE_CODEC2_700C, VOICE_CODEC2_1300}):
                    raise TypeError("Manifest codec_id is invalid")
                if (isinstance(fragment_header_version, bool) or
                        not isinstance(fragment_header_version, int) or
                        fragment_header_version not in {0, FRAGMENT_VERSION}):
                    raise TypeError("Unsupported fragment header version")
                if descriptor is not None and (
                    media_kind != frame.media_kind or codec_id != frame.codec_id
                ):
                    raise BroadcastError("Manifest media/codec differs from duplicated header")
            except (KeyError, TypeError, ValueError, UnicodeError, json.JSONDecodeError) as exc:
                raise BroadcastError("Malformed broadcast file manifest") from exc
            if not 0 <= size <= self.max_file_size:
                raise BroadcastError("Broadcast file exceeds receiver size limit")
            effective_max = self.codec.max_fragment_payload - (
                FRAGMENT_ENVELOPE_SIZE if fragment_header_version else 0
            )
            if (chunk_size < 1 or chunk_size > effective_max or
                    not 0 <= total <= 65535 or
                    total != ((size + chunk_size - 1) // chunk_size)):
                raise BroadcastError("Broadcast file chunk layout is inconsistent")
            if not re.fullmatch(r"[0-9a-f]{64}", digest):
                raise BroadcastError("Invalid broadcast file SHA-256")
            if media_kind == MEDIA_VOICE:
                if not filename.casefold().endswith(".ecv") or codec_id == VOICE_CODEC_NONE:
                    raise BroadcastError("Voice manifest must identify a supported ECV codec")
            elif codec_id != VOICE_CODEC_NONE:
                raise BroadcastError("Non-voice file manifest has a voice codec")
            meta_obj = {"filename": filename, "size": size, "chunk_size": chunk_size,
                        "total_chunks": total, "sha256": digest,
                        "media_kind": media_kind, "codec_id": codec_id,
                        "symbol_rate": int(meta.get("symbol_rate", frame.symbol_rate or 400)),
                        "fec_id": int(meta.get("fec_id", frame.fec_id)),
                        "hopping": bool(meta.get("hopping", bool(frame.fragment_flags & FRAGMENT_FLAG_FHSS))),
                        "fragment_header_version": fragment_header_version}
            meta_key = (frame.sender_id, frame.message_id)
            if meta_key not in self._file_meta and len(self._file_meta) >= self.max_file_metadata:
                oldest_key = min(self._file_meta_updated, key=self._file_meta_updated.get)
                self._drop_file_metadata(*oldest_key)
            self._file_meta[meta_key] = meta_obj
            self._file_meta_updated[meta_key] = now
            self._remember_completed(key, now)
            self._partials.pop(key, None)
            if size == 0:
                path = self._write_file(filename, b"")
                self._file_meta.pop(meta_key, None)
                self._file_meta_updated.pop(meta_key, None)
                return BroadcastDelivery(frame.kind, frame.sender_id, frame.message_id,
                                         b"", True, path=path, media_kind=media_kind,
                                         codec_id=codec_id)
            completed = self._complete_file_if_ready(frame.sender_id, frame.message_id, now)
            if completed is not None:
                return completed
            return BroadcastDelivery(frame.kind, frame.sender_id, frame.message_id,
                                     b"", True, duplicate=duplicate,
                                     media_kind=media_kind, codec_id=codec_id)

        if frame.kind == BroadcastType.TEXT:
            preview = self._text_preview(partial)
            if not partial.complete:
                return BroadcastDelivery(frame.kind, frame.sender_id, frame.message_id,
                                         b"", False, partial.missing, preview=preview,
                                         duplicate=duplicate, total_fragments=partial.count)
            payload = b"".join(partial.fragments[i] for i in range(partial.count))
            try:
                payload.decode("utf-8", errors="strict")
            except UnicodeDecodeError as exc:
                raise BroadcastError("Reassembled broadcast text is not valid UTF-8") from exc
            self._partials.pop(key, None)
            self._remember_completed(key, now)
            return BroadcastDelivery(frame.kind, frame.sender_id, frame.message_id,
                                     payload, True, preview=payload.decode("utf-8"),
                                     total_fragments=partial.count)

        if frame.kind == BroadcastType.FILE_CHUNK:
            if not partial.complete:
                return BroadcastDelivery(frame.kind, frame.sender_id, frame.message_id,
                                         b"", False, partial.missing, duplicate=duplicate,
                                         total_fragments=partial.count,
                                         media_kind=partial.media_kind,
                                         codec_id=partial.codec_id)
            completed = self._complete_file_if_ready(frame.sender_id, frame.message_id, now)
            if completed is not None:
                return completed
            return BroadcastDelivery(frame.kind, frame.sender_id, frame.message_id,
                                     b"", False, (0,), duplicate=duplicate,
                                     total_fragments=partial.count,
                                     media_kind=partial.media_kind,
                                     codec_id=partial.codec_id)

        if frame.kind in {BroadcastType.VOICE, BroadcastType.VOICE_FRAME}:
            if not partial.complete:
                return BroadcastDelivery(frame.kind, frame.sender_id, frame.message_id,
                                         b"", False, partial.missing, duplicate=duplicate,
                                         total_fragments=partial.count)
            payload = b"".join(partial.fragments[i] for i in range(partial.count))
            self._partials.pop(key, None)
            self._remember_completed(key, now)
            return BroadcastDelivery(frame.kind, frame.sender_id, frame.message_id,
                                     payload, True, total_fragments=partial.count)
        raise BroadcastError("Unsupported broadcast data type")

    def _remember_completed(self, key, now):
        self._completed[key] = now
        self._completed.move_to_end(key)
        while len(self._completed) > 2048:
            self._completed.popitem(last=False)

    def _write_file(self, filename, payload):
        self.download_dir.mkdir(parents=True, exist_ok=True)
        safe = self._safe_name(filename)
        destination = self.download_dir / safe
        if destination.exists():
            stem, suffix = destination.stem, destination.suffix
            counter = 1
            while (self.download_dir / f"{stem} ({counter}){suffix}").exists():
                counter += 1
            destination = self.download_dir / f"{stem} ({counter}){suffix}"
        temporary = destination.with_name(destination.name + f".{uuid.uuid4().hex}.part")
        try:
            with temporary.open("xb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)
        return destination

    def retry_requests(self, *, older_than=None):
        """Return a snapshot of missing fragments, safe during concurrent RX."""
        with self._lock:
            return self._retry_requests_unlocked(older_than=older_than)

    def _retry_requests_unlocked(self, *, older_than=None):
        now = time.monotonic()
        self._expire(now)
        age = self.expiry_seconds if older_than is None else float(older_than)
        requests = []
        for partial in self._partials.values():
            if now - partial.updated_at < age:
                continue
            if partial.kind == BroadcastType.FILE_CHUNK:
                # A receiver joining mid-transfer may have missed the manifest.
                # Even if every data chunk arrived, metadata is still required
                # to validate size/hash/name and commit the file.
                if (partial.sender_id, partial.message_id) not in self._file_meta:
                    requests.append((partial.sender_id, partial.message_id,
                                     BroadcastType.FILE_META, (0,)))
                if partial.missing:
                    requests.append((partial.sender_id, partial.message_id,
                                     BroadcastType.FILE_CHUNK, partial.missing))
            elif not partial.complete:
                requests.append((partial.sender_id, partial.message_id,
                                 partial.kind, partial.missing))
        return requests



class BroadcastRetransmitCache:
    """Bounded sender cache for public, selective retransmission requests."""

    def __init__(self, codec: BroadcastCodec, max_messages=64, max_cached_bytes=16 * 1024 * 1024):
        self.codec = codec
        self.max_messages = int(max_messages)
        self.max_cached_bytes = int(max_cached_bytes)
        self._lock = threading.RLock()
        self._messages = OrderedDict()

    def remember(self, message_id, kind, frames):
        with self._lock:
            return self._remember_unlocked(message_id, kind, frames)

    def _remember_unlocked(self, message_id, kind, frames):
        mid = bytes(message_id)
        if len(mid) != 8:
            raise BroadcastError("Invalid message ID for retransmit cache")
        packets = tuple(bytes(frame) for frame in frames)
        if not packets or sum(map(len, packets)) > self.max_cached_bytes:
            raise BroadcastError("Broadcast message exceeds retransmit-cache limits")
        self._messages[(mid, BroadcastType(kind))] = packets
        self._messages.move_to_end((mid, BroadcastType(kind)))
        while len(self._messages) > self.max_messages or sum(
            sum(len(packet) for packet in rows) for rows in self._messages.values()
        ) > self.max_cached_bytes:
            self._messages.popitem(last=False)

    def forget(self, message_id, kind=None):
        """Remove cached frames for a completed/cancelled message."""
        with self._lock:
            return self._forget_unlocked(message_id, kind)

    def _forget_unlocked(self, message_id, kind=None):
        mid = bytes(message_id)
        if len(mid) != 8:
            raise BroadcastError("Invalid message ID for retransmit-cache removal")
        if kind is not None:
            return int(self._messages.pop((mid, BroadcastType(kind)), None) is not None)
        removed = 0
        for key in tuple(self._messages):
            if key[0] == mid:
                self._messages.pop(key, None)
                removed += 1
        return removed

    def handle_request(self, payload):
        with self._lock:
            return self._handle_request_unlocked(payload)

    def _handle_request_unlocked(self, payload):
        # The live packet router already parses authenticated retry packets.
        # Accept that structured tuple directly as well as raw bytes. The old
        # implementation called bytes(tuple_of_bytes) here, which raises
        # TypeError: 'bytes' object cannot be interpreted as an integer.
        if isinstance(payload, tuple):
            if len(payload) == 5:
                target_sender, mid, kind, request_flags, indices = payload
            elif len(payload) == 4:  # compatibility with pre-ERQ2 internal callers
                target_sender, mid, kind, indices = payload
                request_flags = RETRY_NEED_HEADER | RETRY_NEED_DATA
            else:
                raise BroadcastError("Malformed parsed retry-request descriptor")
            target_sender = bytes(target_sender)
            mid = bytes(mid)
            kind = BroadcastType(kind)
            indices = tuple(int(index) for index in indices)
            request_flags = int(request_flags)
            if len(target_sender) != 8 or len(mid) != 8 or not indices:
                raise BroadcastError("Malformed parsed retry-request target")
            if request_flags & ~(RETRY_NEED_HEADER | RETRY_NEED_DATA) or not request_flags:
                raise BroadcastError("Malformed parsed retry-request flags")
        else:
            target_sender, mid, kind, request_flags, indices = self.codec.parse_retry_request(payload)
        if target_sender != self.codec.sender_id:
            return ()
        packets = self._messages.get((mid, kind))
        if packets is None:
            return ()
        selected = []
        for index in indices:
            if index >= len(packets):
                raise BroadcastError("Retry request asks for a fragment outside cached message")
            selected.append(packets[index])
        self._messages.move_to_end((mid, kind))
        return tuple(selected)
