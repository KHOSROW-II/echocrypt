"""Authenticated, resumable, stop-and-wait binary transfer over acoustic frames.

No socket/network transport is used. Callers carry each returned packet with
core.transport over the configured audio devices.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import struct
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from core.crypto import ReplayError

META_MAGIC = b"ECFM"
DATA_MAGIC = b"ECFD"
ACK_MAGIC = b"ECFA"
RESUME_MAGIC = b"ECFR"
REQUEST_MAGIC = b"ECRQ"
META_AAD = b"ECHOCRYPT/FILE-META/v1"
DATA_AAD = b"ECHOCRYPT/FILE-DATA/v1"
ACK_AAD = b"ECHOCRYPT/FILE-ACK/v1"
RESUME_AAD = b"ECHOCRYPT/FILE-RESUME/v1"
REQUEST_AAD = b"ECHOCRYPT/FILE-RESEND-REQUEST/v1"

TRANSFER_ID_SIZE = 16
META_HEAD = struct.Struct("!4s16sH")
DATA_HEAD = struct.Struct("!4s16sIH")
ACK_BODY = struct.Struct("!4s16sBI")
RESUME_BODY = struct.Struct("!4s16sI")
REQUEST_HEAD = struct.Struct("!4s16sH")
REQUEST_INDEX = struct.Struct("!I")
AEAD_OVERHEAD = 32  # 16-byte SecureChannel header + 16-byte Poly1305 tag
DEFAULT_CHUNK_SIZE = 256
DEFAULT_MAX_FILE_SIZE = 256 * 1024 * 1024


class BinaryTransferError(ValueError):
    """Malformed, unauthenticated, or inconsistent file transfer."""


class ChunkIntegrityError(BinaryTransferError):
    """Final file hash failed; these chunk indexes must be retransmitted."""

    def __init__(self, transfer_id: bytes, indexes):
        self.transfer_id = bytes(transfer_id)
        self.indexes = tuple(sorted({int(index) for index in indexes}))
        super().__init__(
            "Completed file SHA-256 mismatch; requesting fresh ciphertext for "
            + ", ".join(str(index) for index in self.indexes)
        )


@dataclass(frozen=True)
class TransferManifest:
    transfer_id: bytes
    filename: str
    file_size: int
    sha256: str
    chunk_size: int
    total_chunks: int

    @property
    def transfer_id_hex(self) -> str:
        return self.transfer_id.hex()


@dataclass(frozen=True)
class OutgoingTransfer:
    manifest: TransferManifest
    source: Path
    state_path: Path


@dataclass(frozen=True)
class ManifestResult:
    manifest: TransferManifest
    ack_packet: bytes
    resume_packet: bytes
    next_missing: int
    completed_path: Path | None


@dataclass(frozen=True)
class ChunkResult:
    transfer_id: bytes
    index: int
    duplicate: bool
    ack_packet: bytes
    completed_path: Path | None
    bytes_received: int


def _file_sha256(path: Path, cancel_event=None) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            if cancel_event is not None and cancel_event.is_set():
                raise InterruptedError("File preparation cancelled")
            digest.update(block)
    if cancel_event is not None and cancel_event.is_set():
        raise InterruptedError("File preparation cancelled")
    return digest.hexdigest()


def _safe_filename(value: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 240:
        raise BinaryTransferError("Invalid filename")
    if "\x00" in value or "/" in value or "\\" in value or value in {".", ".."}:
        raise BinaryTransferError("Unsafe filename rejected")
    if Path(value).name != value:
        raise BinaryTransferError("Path components are not allowed in transferred filenames")
    cleaned = re.sub(r"[^\w.() \-\[\]]", "_", value, flags=re.UNICODE).strip(" .")
    if not cleaned or cleaned in {".", ".."}:
        raise BinaryTransferError("Filename is empty after sanitization")
    return cleaned[:180]


def _atomic_json(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    fd = os.open(temp, flags, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        stream.write(json.dumps(obj, ensure_ascii=False, separators=(",", ":")))
        stream.flush()
        os.fsync(stream.fileno())
    try:
        os.chmod(temp, 0o600)
    except OSError:
        pass
    os.replace(temp, path)


class ReliableBinaryChannel:
    """Persistent receiver and reliable sender for arbitrary binary files.

    File bytes are independently AEAD-protected in small acoustic DATA frames.
    Stop-and-wait ACKs provide retry safety. A receiver stores each accepted
    chunk before acknowledging it; after reconnect it advertises the first
    missing chunk, allowing the sender to resume without retransmitting the
    completed prefix.
    """

    def __init__(self, handshake, receive_root="downloads", state_root=".echocrypt-state",
                 max_file_size: int = DEFAULT_MAX_FILE_SIZE, duplicate_cache_size: int = 128):
        if not isinstance(max_file_size, int) or not 0 <= max_file_size <= 2**31:
            raise ValueError("max_file_size must be between 0 and 2 GiB")
        if not isinstance(duplicate_cache_size, int) or not 1 <= duplicate_cache_size <= 4096:
            raise ValueError("duplicate_cache_size must be between 1 and 4096")
        self.handshake = handshake
        self.receive_root = Path(receive_root)
        self.state_root = Path(state_root)
        self.incoming_state = self.state_root / "incoming"
        self.outgoing_state = self.state_root / "outgoing"
        self.max_file_size = max_file_size
        self.duplicate_cache_size = duplicate_cache_size
        self._authenticated_packets: dict[bytes, tuple[str, bytes, int]] = {}
        self._authenticated_order: list[bytes] = []
        for folder in (self.state_root, self.incoming_state, self.outgoing_state):
            folder.mkdir(parents=True, exist_ok=True, mode=0o700)
            try:
                os.chmod(folder, 0o700)
            except OSError:
                pass

    def _session(self):
        session = getattr(self.handshake, "session", None)
        if not getattr(self.handshake, "established", False) or session is None:
            raise BinaryTransferError("Authenticated acoustic session is not established")
        return session

    def create_outgoing(self, source, chunk_size: int | None = None,
                        transfer_id: str | bytes | None = None, cancel_event=None) -> OutgoingTransfer:
        path = Path(source).expanduser().resolve()
        if not path.is_file():
            raise FileNotFoundError(f"Not a regular file: {path}")
        size = path.stat().st_size
        if size > self.max_file_size:
            raise BinaryTransferError(f"File exceeds configured limit ({self.max_file_size} bytes)")
        safe_name = _safe_filename(path.name)
        digest = _file_sha256(path, cancel_event=cancel_event)
        if transfer_id is None or transfer_id == "":
            tid = uuid.uuid4().bytes
        elif isinstance(transfer_id, str):
            try:
                tid = bytes.fromhex(transfer_id.replace("-", ""))
            except ValueError as exc:
                raise ValueError("Resume transfer ID must be 32 hexadecimal characters") from exc
        else:
            tid = bytes(transfer_id)
        if len(tid) != TRANSFER_ID_SIZE:
            raise ValueError("Transfer ID must be 16 bytes / 32 hexadecimal characters")
        state_path = self.outgoing_state / f"{tid.hex()}.json"
        if cancel_event is not None and cancel_event.is_set():
            raise InterruptedError("File preparation cancelled")
        # Preserve the original chunk layout when resuming a Stage 9.x transfer.
        # New transfers use smaller acoustic frames for better recovery.
        if state_path.exists():
            prior = json.loads(state_path.read_text(encoding="utf-8"))
            if chunk_size is None:
                chunk_size = int(prior.get("chunk_size", DEFAULT_CHUNK_SIZE))
        elif chunk_size is None:
            chunk_size = DEFAULT_CHUNK_SIZE
        if not isinstance(chunk_size, int) or not 128 <= chunk_size <= 900:
            raise ValueError("chunk_size must be between 128 and 900 bytes")
        total = (size + chunk_size - 1) // chunk_size
        manifest = TransferManifest(tid, safe_name, size, digest, chunk_size, total)
        state = {
            "transfer_id": tid.hex(), "filename": safe_name, "file_size": size,
            "sha256": digest, "chunk_size": chunk_size, "total_chunks": total,
            "source": str(path),
        }
        if state_path.exists():
            prior = json.loads(state_path.read_text(encoding="utf-8"))
            for key in ("filename", "file_size", "sha256", "chunk_size", "total_chunks"):
                if prior.get(key) != state[key]:
                    raise BinaryTransferError("Resume ID belongs to a different file or transfer layout")
        else:
            _atomic_json(state_path, state)
        try:
            os.chmod(state_path, 0o600)
        except OSError:
            pass
        return OutgoingTransfer(manifest, path, state_path)

    def manifest_packet(self, outgoing: OutgoingTransfer, max_packet_size: int = 1024) -> bytes:
        m = outgoing.manifest
        payload = json.dumps({
            "filename": m.filename, "file_size": m.file_size, "sha256": m.sha256,
            "chunk_size": m.chunk_size, "total_chunks": m.total_chunks,
        }, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        plain = META_HEAD.pack(META_MAGIC, m.transfer_id, len(payload)) + payload
        if len(plain) + AEAD_OVERHEAD > max_packet_size:
            raise BinaryTransferError("File manifest exceeds modem frame capacity")
        return self._session().tx.encrypt(plain, META_AAD)

    def chunk_packet(self, outgoing: OutgoingTransfer, index: int,
                     max_packet_size: int = 1024) -> bytes:
        m = outgoing.manifest
        if not isinstance(index, int) or not 0 <= index < m.total_chunks:
            raise IndexError("Chunk index out of range")
        offset = index * m.chunk_size
        expected = min(m.chunk_size, m.file_size - offset)
        with outgoing.source.open("rb") as stream:
            stream.seek(offset)
            chunk = stream.read(expected)
        if len(chunk) != expected:
            raise BinaryTransferError("Source file changed while transfer was in progress")
        plain = DATA_HEAD.pack(DATA_MAGIC, m.transfer_id, index, len(chunk)) + chunk
        if len(plain) + AEAD_OVERHEAD > max_packet_size:
            raise BinaryTransferError("Chunk exceeds modem frame capacity")
        return self._session().tx.encrypt(plain, DATA_AAD)

    def _ack_packet(self, transfer_id: bytes, kind: int, index: int) -> bytes:
        plain = ACK_BODY.pack(ACK_MAGIC, transfer_id, kind, index)
        return self._session().tx.encrypt(plain, ACK_AAD)

    def _resume_packet(self, transfer_id: bytes, next_missing: int) -> bytes:
        plain = RESUME_BODY.pack(RESUME_MAGIC, transfer_id, next_missing)
        return self._session().tx.encrypt(plain, RESUME_AAD)

    def verify_ack(self, packet: bytes, transfer_id: bytes, kind: int, index: int) -> bool:
        plain = self._session().rx.decrypt(bytes(packet), ACK_AAD)
        if len(plain) != ACK_BODY.size:
            raise BinaryTransferError("Malformed file ACK")
        magic, got_id, got_kind, got_index = ACK_BODY.unpack(plain)
        if (magic, got_id, got_kind, got_index) != (ACK_MAGIC, transfer_id, kind, index):
            raise BinaryTransferError("File ACK does not match outstanding frame")
        return True

    def verify_resume(self, packet: bytes, transfer_id: bytes, total_chunks: int) -> int:
        plain = self._session().rx.decrypt(bytes(packet), RESUME_AAD)
        if len(plain) != RESUME_BODY.size:
            raise BinaryTransferError("Malformed resume status")
        magic, got_id, next_missing = RESUME_BODY.unpack(plain)
        if magic != RESUME_MAGIC or got_id != transfer_id or next_missing > total_chunks:
            raise BinaryTransferError("Resume status is invalid for this transfer")
        return next_missing

    def _state_paths(self, tid: bytes) -> tuple[Path, Path]:
        stem = tid.hex()
        return self.incoming_state / f"{stem}.json", self.incoming_state / f"{stem}.part"

    def _load_state(self, tid: bytes) -> tuple[dict, Path, Path]:
        state_path, part_path = self._state_paths(tid)
        if not state_path.exists():
            raise BinaryTransferError("No matching file manifest has been received")
        state = json.loads(state_path.read_text(encoding="utf-8"))
        if state.get("transfer_id") != tid.hex():
            raise BinaryTransferError("Stored transfer state ID mismatch")
        return state, state_path, part_path

    def receive_manifest(self, packet: bytes) -> ManifestResult:
        digest = hashlib.sha256(bytes(packet)).digest()
        cached = self._authenticated_packets.get(digest)
        if cached and cached[0] == "manifest":
            tid, _idx = cached[1], cached[2]
            state, _, _ = self._load_state(tid)
            manifest = self._manifest_from_state(state)
            next_missing = self._next_missing(state, manifest.total_chunks)
            final = Path(state["final_path"]) if state.get("completed") else None
            return ManifestResult(manifest, self._ack_packet(tid, 0, 0xFFFFFFFF),
                                  self._resume_packet(tid, next_missing), next_missing, final)

        plain = self._session().rx.decrypt(bytes(packet), META_AAD)
        if len(plain) < META_HEAD.size:
            raise BinaryTransferError("Truncated file manifest")
        magic, tid, meta_len = META_HEAD.unpack(plain[:META_HEAD.size])
        if magic != META_MAGIC or meta_len != len(plain) - META_HEAD.size:
            raise BinaryTransferError("Invalid file manifest framing")
        try:
            obj = json.loads(plain[META_HEAD.size:].decode("utf-8"))
            filename = _safe_filename(obj["filename"])
            size = int(obj["file_size"])
            chunk_size = int(obj["chunk_size"])
            total_chunks = int(obj["total_chunks"])
            sha = str(obj["sha256"])
        except (KeyError, TypeError, ValueError, UnicodeError, json.JSONDecodeError) as exc:
            raise BinaryTransferError("Invalid file manifest fields") from exc
        if not 0 <= size <= self.max_file_size:
            raise BinaryTransferError("Incoming file exceeds the configured size limit")
        if not 128 <= chunk_size <= 900:
            raise BinaryTransferError("Unsupported incoming chunk size")
        if not re.fullmatch(r"[0-9a-f]{64}", sha):
            raise BinaryTransferError("Invalid SHA-256 digest in manifest")
        expected_total = (size + chunk_size - 1) // chunk_size
        if total_chunks != expected_total:
            raise BinaryTransferError("Manifest chunk count does not match file size")
        state_path, part_path = self._state_paths(tid)
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state = {
            "transfer_id": tid.hex(), "filename": filename, "file_size": size,
            "sha256": sha, "chunk_size": chunk_size, "total_chunks": total_chunks,
            "chunks": {}, "completed": False,
        }
        if state_path.exists():
            old = json.loads(state_path.read_text(encoding="utf-8"))
            current = {k: state[k] for k in ("transfer_id", "filename", "file_size", "sha256", "chunk_size", "total_chunks")}
            existing = {k: old.get(k) for k in current}
            if current != existing:
                raise BinaryTransferError("Transfer ID was reused for a different file manifest")
            state = old
        else:
            if size:
                with part_path.open("wb") as stream:
                    stream.truncate(size)
            else:
                part_path.touch(mode=0o600)
            try:
                os.chmod(part_path, 0o600)
            except OSError:
                pass
            _atomic_json(state_path, state)
        manifest = self._manifest_from_state(state)
        if manifest.total_chunks == 0 and not state.get("completed"):
            self.receive_root.mkdir(parents=True, exist_ok=True)
            destination = self._unique_destination(self.receive_root / manifest.filename)
            part_path.touch(exist_ok=True)
            os.replace(part_path, destination)
            state["completed"] = True
            state["final_path"] = str(destination.resolve())
            _atomic_json(state_path, state)
        next_missing = self._next_missing(state, manifest.total_chunks)
        completed = Path(state["final_path"]) if state.get("completed") else None
        self._remember_packet(digest, "manifest", tid, 0xFFFFFFFF)
        return ManifestResult(manifest, self._ack_packet(tid, 0, 0xFFFFFFFF),
                              self._resume_packet(tid, next_missing), next_missing, completed)

    def _remember_packet(self, digest: bytes, kind: str, tid: bytes, index: int) -> None:
        self._authenticated_packets[digest] = (kind, tid, index)
        self._authenticated_order.append(digest)
        while len(self._authenticated_order) > self.duplicate_cache_size:
            old = self._authenticated_order.pop(0)
            self._authenticated_packets.pop(old, None)

    @staticmethod
    def _manifest_from_state(state: dict) -> TransferManifest:
        return TransferManifest(bytes.fromhex(state["transfer_id"]), state["filename"],
                                int(state["file_size"]), state["sha256"],
                                int(state["chunk_size"]), int(state["total_chunks"]))

    @staticmethod
    def _next_missing(state: dict, total: int) -> int:
        chunks = state.get("chunks", {})
        index = 0
        while index < total and str(index) in chunks:
            index += 1
        return index

    def receive_chunk(self, packet: bytes) -> ChunkResult:
        packet = bytes(packet)
        digest = hashlib.sha256(packet).digest()
        cached = self._authenticated_packets.get(digest)
        if cached and cached[0] == "chunk":
            tid, index = cached[1], cached[2]
            state, _, _ = self._load_state(tid)
            path = Path(state["final_path"]) if state.get("completed") else None
            ack = self._ack_packet(tid, 1, index)
            return ChunkResult(tid, index, True, ack, path, sum(
                min(int(state["chunk_size"]), int(state["file_size"]) - i * int(state["chunk_size"]))
                for i in range(len(state.get("chunks", {})))
            ))

        plain = self._session().rx.decrypt(packet, DATA_AAD)
        if len(plain) < DATA_HEAD.size:
            raise BinaryTransferError("Truncated file DATA frame")
        magic, tid, index, data_len = DATA_HEAD.unpack(plain[:DATA_HEAD.size])
        chunk = plain[DATA_HEAD.size:]
        if magic != DATA_MAGIC or data_len != len(chunk):
            raise BinaryTransferError("Invalid file DATA framing")
        state, state_path, part_path = self._load_state(tid)
        m = self._manifest_from_state(state)
        if not 0 <= index < m.total_chunks:
            raise BinaryTransferError("Chunk index is outside the transfer")
        expected_len = min(m.chunk_size, m.file_size - index * m.chunk_size)
        if data_len != expected_len:
            raise BinaryTransferError("Chunk size does not match manifest")
        chunk_hash = hashlib.sha256(chunk).hexdigest()
        prior_hash = state.setdefault("chunks", {}).get(str(index))
        duplicate = prior_hash is not None
        if duplicate and prior_hash != chunk_hash:
            raise BinaryTransferError("A chunk index was reused with different contents")
        if not duplicate and not state.get("completed"):
            if not part_path.exists():
                raise BinaryTransferError("Partial file is missing; resume cannot continue")
            try:
                os.chmod(part_path, 0o600)
            except OSError:
                pass
            with part_path.open("r+b") as stream:
                stream.seek(index * m.chunk_size)
                stream.write(chunk)
                stream.flush()
                os.fsync(stream.fileno())
            state["chunks"][str(index)] = chunk_hash
            _atomic_json(state_path, state)
        completed_path = Path(state["final_path"]) if state.get("completed") else None
        if not state.get("completed") and len(state.get("chunks", {})) == m.total_chunks:
            if _file_sha256(part_path) != m.sha256:
                invalid_indexes = []
                with part_path.open("rb") as stream:
                    for raw_index, expected_hash in state.get("chunks", {}).items():
                        chunk_index = int(raw_index)
                        stream.seek(chunk_index * m.chunk_size)
                        expected_size = min(
                            m.chunk_size, m.file_size - chunk_index * m.chunk_size
                        )
                        actual = stream.read(expected_size)
                        if hashlib.sha256(actual).hexdigest() != expected_hash:
                            invalid_indexes.append(chunk_index)
                if not invalid_indexes:
                    # The manifest and chunk journal disagree. Force a clean
                    # replay instead of retaining a non-completable partial.
                    invalid_indexes = list(range(m.total_chunks))
                for invalid_index in invalid_indexes:
                    state["chunks"].pop(str(invalid_index), None)
                _atomic_json(state_path, state)
                raise ChunkIntegrityError(tid, invalid_indexes)
            self.receive_root.mkdir(parents=True, exist_ok=True)
            destination = self._unique_destination(self.receive_root / m.filename)
            os.replace(part_path, destination)
            state["completed"] = True
            state["final_path"] = str(destination.resolve())
            _atomic_json(state_path, state)
            completed_path = destination
        self._remember_packet(digest, "chunk", tid, index)
        bytes_received = sum(
            min(m.chunk_size, m.file_size - i * m.chunk_size)
            for i in (int(k) for k in state.get("chunks", {}).keys())
        )
        return ChunkResult(tid, index, duplicate,
                           self._ack_packet(tid, 1, index), completed_path, bytes_received)

    def transfer_progress(self, transfer_id: bytes):
        """Return (received_count, total_count, missing_indexes, completed)."""
        tid = bytes(transfer_id)
        if len(tid) != TRANSFER_ID_SIZE:
            raise BinaryTransferError("Transfer ID must contain exactly 16 bytes")
        state, _state_path, _part_path = self._load_state(tid)
        total = int(state["total_chunks"])
        received_indexes = {int(index) for index in state.get("chunks", {}).keys()}
        missing = tuple(index for index in range(total) if index not in received_indexes)
        return len(received_indexes), total, missing, bool(state.get("completed"))

    def make_resend_request_packet(self, transfer_id: bytes, missing_indexes,
                                   max_packet_size: int = 1024) -> bytes:
        """Build an authenticated, repeatable request for selected missing chunks."""
        tid = bytes(transfer_id)
        indexes = tuple(sorted(set(int(index) for index in missing_indexes)))
        if len(tid) != TRANSFER_ID_SIZE:
            raise BinaryTransferError("Transfer ID must contain exactly 16 bytes")
        if not indexes or any(index < 0 or index > 0xFFFFFFFF for index in indexes):
            raise BinaryTransferError("A resend request requires valid missing chunk indexes")
        if len(indexes) > 0xFFFF:
            raise BinaryTransferError("Resend request contains too many chunk indexes")
        plain = REQUEST_HEAD.pack(REQUEST_MAGIC, tid, len(indexes)) + b"".join(
            REQUEST_INDEX.pack(index) for index in indexes
        )
        if len(plain) + AEAD_OVERHEAD > max_packet_size:
            raise BinaryTransferError("Resend request exceeds acoustic frame capacity")
        return self._session().tx.encrypt(plain, REQUEST_AAD)

    def receive_resend_request(self, packet: bytes):
        """Authenticate and parse a peer's missing-chunk request."""
        plain = self._session().rx.decrypt(bytes(packet), REQUEST_AAD)
        if len(plain) < REQUEST_HEAD.size:
            raise BinaryTransferError("Truncated resend request")
        magic, tid, count = REQUEST_HEAD.unpack(plain[:REQUEST_HEAD.size])
        expected = REQUEST_HEAD.size + count * REQUEST_INDEX.size
        if magic != REQUEST_MAGIC or count < 1 or len(plain) != expected:
            raise BinaryTransferError("Invalid resend request framing")
        indexes = tuple(REQUEST_INDEX.unpack_from(plain, REQUEST_HEAD.size + i * REQUEST_INDEX.size)[0]
                        for i in range(count))
        if len(set(indexes)) != len(indexes):
            raise BinaryTransferError("Resend request contains duplicate chunk indexes")
        return tid, indexes

    @staticmethod
    def _unique_destination(path: Path) -> Path:
        if not path.exists():
            return path
        stem, suffix = path.stem, path.suffix
        for counter in range(1, 10000):
            candidate = path.with_name(f"{stem} ({counter}){suffix}")
            if not candidate.exists():
                return candidate
        raise BinaryTransferError("Could not choose a unique destination filename")

    def complete_outgoing(self, outgoing: OutgoingTransfer) -> None:
        try:
            outgoing.state_path.unlink(missing_ok=True)
        except OSError:
            pass
