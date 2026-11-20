"""Authenticated, resumable file-transfer API for Direct peers."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import math
import queue
import threading
import time

from cryptography.exceptions import InvalidTag

from core.binary_transfer import (
    BinaryTransferError,
    ChunkIntegrityError,
    ReliableBinaryChannel,
)
from core.crypto import ReplayError
from .exceptions import ClientClosedError, ReceiveTimeout


@dataclass(frozen=True)
class SecureFileSendResult:
    transfer_id: str
    filename: str
    bytes_sent: int
    acknowledged_chunks: int
    total_chunks: int
    sha256: str
    completed: bool


class SecureFileTransfer:
    """Authenticated Direct file transfer with resume, ACKs and SHA-256 completion.

    The receiver writes accepted chunks to a restricted temporary file before
    acknowledging them. A final authenticated completion packet is sent only
    after the receiver has verified the complete SHA-256 digest and committed
    the destination file.
    """

    def __init__(
        self,
        peer,
        *,
        receive_root="downloads",
        state_root=".echocrypt-state",
        max_file_size=256 * 1024 * 1024,
    ):
        if not getattr(peer, "connected", False):
            raise RuntimeError("SecureFileTransfer requires an established DirectPeer")
        self.peer = peer
        self._channel = ReliableBinaryChannel(
            peer.handshake,
            receive_root=receive_root,
            state_root=state_root,
            max_file_size=max_file_size,
        )
        self._events = queue.Queue(maxsize=256)
        self._pending_lock = threading.RLock()
        self._send_lock = threading.Lock()
        self._pending_ack = None
        self._pending_resume = None
        self._pending_completion = {}
        self._active_transmission = None
        self._control_transmission = None
        self._control_queue = queue.Queue(maxsize=64)
        self._outgoing = {}
        self._closed = False
        self._control_thread = threading.Thread(
            target=self._control_loop,
            name="echocrypt-sdk-file-control",
            daemon=True,
        )
        self._control_thread.start()
        # Register only after every callback dependency and worker is ready.
        self._subscription = peer.add_raw_packet_handler(self._handle_raw_packet)

    def _publish(self, kind, transfer_id=None, result=None, error=None):
        if self._closed:
            return
        event = {
            "kind": str(kind),
            "transfer_id": bytes(transfer_id).hex() if transfer_id is not None else None,
            "result": result,
            "error": error,
        }
        try:
            self._events.put_nowait(event)
        except queue.Full:
            try:
                self._events.get_nowait()
            except queue.Empty:
                pass
            try:
                self._events.put_nowait(event)
            except queue.Full:
                pass

    def _send_control(self, packet, label):
        return self.peer.send_secure_frame(bytes(packet), label=label)

    def _queue_controls(self, controls):
        """Queue an ordered sequence of protocol responses off the RX dispatcher."""
        if self._closed:
            return False
        batch = tuple((bytes(packet), str(label)) for packet, label in controls)
        if not batch:
            return True
        try:
            self._control_queue.put_nowait(batch)
            return True
        except queue.Full:
            self._publish(
                "control_queue_full",
                error=RuntimeError("Secure file control-response queue is full"),
            )
            return False

    def _control_loop(self):
        while True:
            item = self._control_queue.get()
            if item is None:
                self._control_queue.task_done()
                return
            try:
                for packet, label in item:
                    if self._closed:
                        break
                    transmission = self._send_control(packet, label)
                    with self._pending_lock:
                        self._control_transmission = transmission
                    while not transmission.done:
                        if self._closed:
                            transmission.cancel()
                            break
                        time.sleep(0.02)
                    if self._closed:
                        break
                    transmission.wait()
            except Exception as exc:
                self._publish("control_send_error", error=exc)
            finally:
                with self._pending_lock:
                    self._control_transmission = None
                self._control_queue.task_done()

    def _completion_packet(self, manifest):
        return self._channel.make_completion_packet(
            manifest.transfer_id, manifest.sha256
        )

    def _handle_raw_packet(self, packet):
        if self._closed:
            return False

        # Each protocol packet has a distinct AEAD AAD. InvalidTag means this
        # is not that packet type and does not consume the replay counter.
        try:
            transfer_id, kind, index = self._channel.receive_ack(packet)
        except (InvalidTag, ReplayError):
            pass
        except BinaryTransferError as exc:
            self._publish("ack_error", error=exc)
            return True
        else:
            with self._pending_lock:
                pending = self._pending_ack
            if pending is not None:
                expected_id, expected_kind, expected_index, event = pending
                if (transfer_id, kind, index) == (
                    expected_id, expected_kind, expected_index
                ):
                    event.set()
                else:
                    self._publish("unexpected_ack", transfer_id, {
                        "kind": kind, "index": index,
                    })
            return True

        try:
            transfer_id, next_missing = self._channel.receive_resume(packet)
        except (InvalidTag, ReplayError):
            pass
        except BinaryTransferError as exc:
            self._publish("resume_error", error=exc)
            return True
        else:
            with self._pending_lock:
                pending = self._pending_resume
            if pending is not None:
                expected_id, total, event, value = pending
                if transfer_id == expected_id and next_missing <= total:
                    value["next_missing"] = next_missing
                    event.set()
                else:
                    self._publish("unexpected_resume", transfer_id, {
                        "next_missing": next_missing,
                    })
            return True

        try:
            transfer_id, digest = self._channel.receive_completion_packet(packet)
        except (InvalidTag, ReplayError):
            pass
        except BinaryTransferError as exc:
            self._publish("completion_error", error=exc)
            return True
        else:
            with self._pending_lock:
                outgoing = self._outgoing.get(transfer_id)
            if outgoing is None or digest != outgoing.manifest.sha256:
                self._publish("invalid_completion", transfer_id, {"sha256": digest})
                return True
            with self._pending_lock:
                event = self._pending_completion.get(transfer_id)
            if event is not None:
                event.set()
            self._publish("remote_file_completed", transfer_id, {
                "sha256": digest,
                "filename": outgoing.manifest.filename,
            })
            return True

        try:
            transfer_id, indexes = self._channel.receive_resend_request(packet)
        except (InvalidTag, ReplayError):
            pass
        except BinaryTransferError as exc:
            self._publish("repair_request_error", error=exc)
            return True
        else:
            with self._pending_lock:
                outgoing = self._outgoing.get(transfer_id)
            self._publish("repair_request_received", transfer_id, {"indexes": indexes})
            if outgoing is not None:
                threading.Thread(
                    target=self._resend_requested_chunks,
                    args=(outgoing, indexes),
                    name="echocrypt-sdk-file-repair",
                    daemon=True,
                ).start()
            return True

        try:
            manifest = self._channel.receive_manifest(packet)
        except (InvalidTag, ReplayError):
            pass
        except BinaryTransferError as exc:
            self._publish("manifest_error", error=exc)
            return True
        else:
            controls = [
                (manifest.ack_packet, "DIRECT FILE MANIFEST ACK"),
                (manifest.resume_packet, "DIRECT FILE RESUME"),
            ]
            if manifest.completed_path is not None:
                controls.append((
                    self._completion_packet(manifest.manifest),
                    "DIRECT FILE COMPLETE",
                ))
            self._queue_controls(controls)
            self._publish(
                "manifest_received",
                manifest.manifest.transfer_id,
                manifest,
            )
            if manifest.completed_path is not None:
                self._publish(
                    "file_completed",
                    manifest.manifest.transfer_id,
                    {"path": manifest.completed_path, "sha256": manifest.manifest.sha256},
                )
            return True

        try:
            chunk = self._channel.receive_chunk(packet)
        except ChunkIntegrityError as exc:
            request = self._channel.make_resend_request_packet(
                exc.transfer_id, exc.indexes,
                max_packet_size=self.peer.client.config.max_packet_bytes,
            )
            self._queue_controls((
                (exc.ack_packet, "DIRECT FILE CHUNK ACK"),
                (request, "DIRECT FILE REPAIR REQUEST"),
            ))
            self._publish(
                "integrity_repair_required",
                exc.transfer_id,
                {"missing_indexes": exc.indexes},
                exc,
            )
            return True
        except (InvalidTag, ReplayError):
            return False
        except BinaryTransferError as exc:
            self._publish("chunk_error", error=exc)
            return True
        else:
            controls = [(chunk.ack_packet, f"DIRECT FILE CHUNK ACK {chunk.index}")]
            if chunk.completed_path is not None:
                manifest = self._channel.manifest_for_transfer(chunk.transfer_id)
                controls.append((
                    self._completion_packet(manifest), "DIRECT FILE COMPLETE"
                ))
            self._queue_controls(controls)
            self._publish("chunk_received", chunk.transfer_id, chunk)
            if chunk.completed_path is not None:
                self._publish(
                    "file_completed",
                    chunk.transfer_id,
                    {"path": chunk.completed_path, "sha256": manifest.sha256},
                )
            return True

    def _send_and_wait(
        self,
        packet,
        transfer_id,
        kind,
        index,
        *,
        ack_timeout=15.0,
        retries=3,
        expect_resume=False,
        total_chunks=0,
        cancel_event=None,
    ):
        if isinstance(retries, bool) or not isinstance(retries, int) or not 0 <= retries <= 10:
            raise ValueError("retries must be between 0 and 10")
        if isinstance(ack_timeout, bool) or not isinstance(ack_timeout, (int, float)) \
                or not math.isfinite(float(ack_timeout)) or ack_timeout <= 0:
            raise ValueError("ack_timeout must be finite and positive")

        acquired = False
        while not acquired:
            if self._closed:
                raise RuntimeError("SecureFileTransfer is closed")
            if cancel_event is not None and cancel_event.is_set():
                raise InterruptedError("Secure file transfer cancelled")
            acquired = self._send_lock.acquire(timeout=0.05)

        ack_event = threading.Event()
        resume_event = threading.Event() if expect_resume else None
        resume_value = {}
        tid = bytes(transfer_id)
        try:
            if self._closed:
                raise RuntimeError("SecureFileTransfer is closed")
            with self._pending_lock:
                self._pending_ack = (tid, int(kind), int(index), ack_event)
                self._pending_resume = (
                    (tid, int(total_chunks), resume_event, resume_value)
                    if expect_resume else None
                )
            for attempt in range(retries + 1):
                if self._closed:
                    raise RuntimeError("SecureFileTransfer was closed during transfer")
                if cancel_event is not None and cancel_event.is_set():
                    raise InterruptedError("Secure file transfer cancelled")
                tx = self._send_control(
                    packet, f"DIRECT FILE {kind}:{index} attempt {attempt + 1}"
                )
                with self._pending_lock:
                    self._active_transmission = tx
                    closed_during_submit = self._closed
                if closed_during_submit:
                    tx.cancel()
                    raise RuntimeError("SecureFileTransfer was closed during transmission")
                while not tx.done:
                    if self._closed:
                        tx.cancel()
                        raise RuntimeError("SecureFileTransfer was closed during transmission")
                    if cancel_event is not None and cancel_event.is_set():
                        tx.cancel()
                        raise InterruptedError("Secure file transfer cancelled")
                    time.sleep(0.02)
                tx.wait()
                with self._pending_lock:
                    if self._active_transmission is tx:
                        self._active_transmission = None
                deadline = time.monotonic() + float(ack_timeout)
                while time.monotonic() < deadline:
                    if self._closed:
                        raise RuntimeError("SecureFileTransfer was closed while awaiting ACK")
                    if cancel_event is not None and cancel_event.is_set():
                        raise InterruptedError("Secure file transfer cancelled")
                    if ack_event.is_set() and (
                        resume_event is None or resume_event.is_set()
                    ):
                        return resume_value.get("next_missing")
                    ack_event.wait(min(0.05, max(0.0, deadline - time.monotonic())))
                    if resume_event is not None and not resume_event.is_set():
                        resume_event.wait(0.001)
            raise TimeoutError(
                f"No authenticated ACK after {retries + 1} attempts"
            )
        finally:
            with self._pending_lock:
                if self._pending_ack is not None and self._pending_ack[3] is ack_event:
                    self._pending_ack = None
                if self._pending_resume is not None and self._pending_resume[2] is resume_event:
                    self._pending_resume = None
                self._active_transmission = None
            if acquired:
                self._send_lock.release()

    def prepare(self, source, *, chunk_size=None, transfer_id=None, cancel_event=None):
        if self._closed:
            raise RuntimeError("SecureFileTransfer is closed")
        with self._pending_lock:
            if len(self._outgoing) >= 128:
                raise BinaryTransferError("Too many active outgoing file transfers")
        outgoing = self._channel.create_outgoing(
            source,
            chunk_size=chunk_size,
            transfer_id=transfer_id,
            cancel_event=cancel_event,
        )
        with self._pending_lock:
            self._outgoing[outgoing.manifest.transfer_id] = outgoing
        return outgoing

    def send_manifest(
        self, outgoing, *, ack_timeout=15.0, retries=3, cancel_event=None
    ):
        packet = self._channel.manifest_packet(
            outgoing, max_packet_size=self.peer.client.config.max_packet_bytes
        )
        next_missing = self._send_and_wait(
            packet,
            outgoing.manifest.transfer_id,
            0,
            0xFFFFFFFF,
            ack_timeout=ack_timeout,
            retries=retries,
            expect_resume=True,
            total_chunks=outgoing.manifest.total_chunks,
            cancel_event=cancel_event,
        )
        return int(next_missing)

    def send_chunk(
        self, outgoing, index, *, ack_timeout=15.0, retries=3, cancel_event=None
    ):
        packet = self._channel.chunk_packet(
            outgoing, index,
            max_packet_size=self.peer.client.config.max_packet_bytes,
        )
        self._send_and_wait(
            packet,
            outgoing.manifest.transfer_id,
            1,
            index,
            ack_timeout=ack_timeout,
            retries=retries,
            cancel_event=cancel_event,
        )
        return packet

    def send_file(
        self,
        source,
        *,
        chunk_size=None,
        transfer_id=None,
        ack_timeout=15.0,
        retries=3,
        cancel_event=None,
        on_progress=None,
    ):
        """Send a file and wait for authenticated remote SHA-256 completion."""
        if isinstance(retries, bool) or not isinstance(retries, int) or not 0 <= retries <= 10:
            raise ValueError("retries must be between 0 and 10")
        if isinstance(ack_timeout, bool) or not isinstance(ack_timeout, (int, float)) \
                or not math.isfinite(float(ack_timeout)) or ack_timeout <= 0:
            raise ValueError("ack_timeout must be finite and positive")
        outgoing = self.prepare(
            source,
            chunk_size=chunk_size,
            transfer_id=transfer_id,
            cancel_event=cancel_event,
        )
        manifest = outgoing.manifest
        completion_event = threading.Event()
        with self._pending_lock:
            self._pending_completion[manifest.transfer_id] = completion_event
        try:
            next_missing = self.send_manifest(
                outgoing,
                ack_timeout=ack_timeout,
                retries=retries,
                cancel_event=cancel_event,
            )
            acknowledged = next_missing
            for index in range(next_missing, manifest.total_chunks):
                self.send_chunk(
                    outgoing,
                    index,
                    ack_timeout=ack_timeout,
                    retries=retries,
                    cancel_event=cancel_event,
                )
                acknowledged = index + 1
                if on_progress is not None:
                    on_progress(acknowledged, manifest.total_chunks)

            # The receiver emits this authenticated notice only after SHA-256
            # verification and commit. If the notice is lost, send a fresh
            # manifest: the receiver responds with its persisted resume offset
            # and re-emits completion if the file was already committed.
            for attempt in range(retries + 1):
                deadline = time.monotonic() + float(ack_timeout)
                while time.monotonic() < deadline:
                    if self._closed:
                        raise RuntimeError("SecureFileTransfer was closed during transfer")
                    if completion_event.is_set():
                        return SecureFileSendResult(
                            manifest.transfer_id_hex,
                            manifest.filename,
                            manifest.file_size,
                            acknowledged,
                            manifest.total_chunks,
                            manifest.sha256,
                            True,
                        )
                    if cancel_event is not None and cancel_event.is_set():
                        raise InterruptedError("Secure file transfer cancelled")
                    completion_event.wait(min(0.1, max(0.0, deadline - time.monotonic())))
                if attempt < retries:
                    retry_manifest = self._channel.manifest_packet(
                        outgoing,
                        max_packet_size=self.peer.client.config.max_packet_bytes,
                    )
                    resume_offset = self._send_and_wait(
                        retry_manifest,
                        manifest.transfer_id,
                        0,
                        0xFFFFFFFF,
                        ack_timeout=ack_timeout,
                        retries=retries,
                        expect_resume=True,
                        total_chunks=manifest.total_chunks,
                        cancel_event=cancel_event,
                    )
                    for index in range(int(resume_offset), manifest.total_chunks):
                        self.send_chunk(
                            outgoing,
                            index,
                            ack_timeout=ack_timeout,
                            retries=retries,
                            cancel_event=cancel_event,
                        )
                        acknowledged = index + 1
                        if on_progress is not None:
                            on_progress(acknowledged, manifest.total_chunks)
            raise TimeoutError(
                "All chunks were acknowledged, but authenticated remote "
                "SHA-256 completion was not received"
            )
        finally:
            with self._pending_lock:
                self._pending_completion.pop(manifest.transfer_id, None)

    def _resend_requested_chunks(self, outgoing, indexes):
        try:
            for index in indexes:
                self.send_chunk(outgoing, index)
                self._publish(
                    "repair_chunk_sent",
                    outgoing.manifest.transfer_id,
                    {"index": index},
                )
        except Exception as exc:
            self._publish(
                "repair_send_error",
                outgoing.manifest.transfer_id,
                {"indexes": tuple(indexes)},
                exc,
            )

    def receive_event(self, timeout=None):
        if timeout is not None and (
            isinstance(timeout, bool) or not isinstance(timeout, (int, float))
            or not math.isfinite(float(timeout)) or timeout < 0
        ):
            raise ValueError("timeout must be a finite non-negative number or None")

        deadline = None if timeout is None else time.monotonic() + float(timeout)
        while True:
            if self._closed and self._events.empty():
                raise ClientClosedError("SecureFileTransfer has been closed")
            if deadline is None:
                wait_for = 0.05
            else:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    try:
                        return self._events.get_nowait()
                    except queue.Empty as exc:
                        if self._closed:
                            raise ClientClosedError(
                                "SecureFileTransfer has been closed"
                            ) from exc
                        raise ReceiveTimeout(
                            "No secure file-transfer event arrived"
                        ) from exc
                wait_for = min(0.05, remaining)
            try:
                return self._events.get(timeout=wait_for)
            except queue.Empty:
                if deadline is not None and time.monotonic() >= deadline:
                    if self._closed and self._events.empty():
                        raise ClientClosedError("SecureFileTransfer has been closed")
                    raise ReceiveTimeout("No secure file-transfer event arrived")

    def progress(self, transfer_id):
        return self._channel.transfer_progress(bytes(transfer_id))

    def finish_transfer(self, transfer_id):
        """Remove sender resume state after confirmed remote completion."""
        tid = bytes.fromhex(transfer_id.replace("-", "")) if isinstance(
            transfer_id, str
        ) else bytes(transfer_id)
        with self._pending_lock:
            outgoing = self._outgoing.pop(tid, None)
        if outgoing is None:
            raise BinaryTransferError("No active outgoing transfer matches this ID")
        self._channel.complete_outgoing(outgoing)

    def close(self):
        if self._closed:
            return
        self._closed = True
        self._subscription.close()
        with self._pending_lock:
            pending_ack = self._pending_ack
            pending_resume = self._pending_resume
            active_transmission = self._active_transmission
            control_transmission = self._control_transmission
            self._pending_ack = None
            self._pending_resume = None
            for event in self._pending_completion.values():
                event.set()
            self._pending_completion.clear()
        # Wake any transfer thread blocked on protocol acknowledgements and
        # interrupt the currently queued/playing acoustic frame, if possible.
        if pending_ack is not None:
            pending_ack[3].set()
        if pending_resume is not None:
            pending_resume[2].set()
        if active_transmission is not None:
            try:
                active_transmission.cancel()
            except Exception:
                pass
        if control_transmission is not None:
            try:
                control_transmission.cancel()
            except Exception:
                pass

        # Drop queued response batches; no protocol response is useful after
        # close. The active transmission is cancelled above before the worker
        # is joined.
        while True:
            try:
                self._control_queue.get_nowait()
                self._control_queue.task_done()
            except queue.Empty:
                break
        try:
            self._control_queue.put_nowait(None)
        except queue.Full:
            pass
        if self._control_thread is not threading.current_thread():
            self._control_thread.join(timeout=1.0)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()
        return False
