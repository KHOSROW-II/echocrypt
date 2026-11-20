from __future__ import annotations

from datetime import datetime
from pathlib import Path
from concurrent.futures import TimeoutError as FutureTimeoutError
import re
import time
import threading
import queue

import numpy as np
from prompt_toolkit import prompt

from core.audio import play, record, MicrophoneRecorder
from core.binary_transfer import (ReliableBinaryChannel, BinaryTransferError,
                                  ChunkIntegrityError, ACK_BODY, AEAD_OVERHEAD, DATA_HEAD)
from core.codec import (encode_voice_message, encode_emergency_voice_message,
                        encode_codec2_1300_message, decode_voice_message,
                        write_voice_wav, VOICE_MAGIC, VOICE2_MAGIC, VOICE3_MAGIC)
from core.codec2_backend import Codec2Unavailable
from core.config import load
from core.debug import for_config
from core.reliability import ACK_PACKET_SIZE, ReliableTextChannel
from core.session_engine import AudioSessionEngine
from core.broadcast import (BroadcastCodec, BroadcastType, BroadcastReassembler,
                            BroadcastRetransmitCache, BroadcastError,
                            BROADCAST_HOP_KEY, MEDIA_VOICE,
                            VOICE_CODEC2_1300, FEC_ID_CONVOLUTIONAL,
                            RETRY_NEED_HEADER, RETRY_NEED_DATA,
                            RETRY_REQUEST_V2)
from core.crypto import ReplayError
from core.recovery.fec_controller import AdaptiveFECController
from cryptography.exceptions import InvalidTag
from core.transport import (packet_duration, receive_auto, receive_auto_samples,
                            transmit, transmit_hybrid)
from core.hybrid import demodulate_hybrid
from security.identity import load_private, load_public, export_public
from security.keystore import PRIVATE, PUBLIC, create_identity
from core.contacts import Contact, add_contact, list_contacts, remove_contact
from core.profile import get_username
from interface.dashboard import render as render_app_header
from interface.chat import ChatScreen, add_chat_message
from security.peer_handshake import PeerHandshake
from interface.terminal import Menu




class TransferCancelled(RuntimeError):
    """A user cancelled an outgoing transfer before it completed."""


def _check_cancelled(cancel_event):
    if cancel_event is not None and cancel_event.is_set():
        raise TransferCancelled("cancelled by user")


def _wait_future_or_cancel(future, cancel_event, timeout):
    deadline = time.monotonic() + max(0.0, float(timeout))
    while True:
        _check_cancelled(cancel_event)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Timed out waiting for acoustic transmission")
        try:
            return future.result(timeout=min(0.10, remaining))
        except FutureTimeoutError:
            continue


IMAGE_EXTENSIONS = frozenset({
    ".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".tif", ".tiff",
    ".heic", ".heif", ".avif", ".svg",
})
AUDIO_FILE_EXTENSIONS = frozenset({
    ".wav", ".mp3", ".flac", ".ogg", ".opus", ".m4a", ".aac", ".wma",
    ".aiff", ".aif", ".ecv",
})

# Kept as data so the session feature surface can be tested without audio hardware.
SESSION_MENU_ITEMS = [
    ("send_text", "Send encrypted text message"),
    ("receive_text", "Receive text message"),
    ("send_image", "Send image (JPG / PNG / WEBP / GIF)"),
    ("receive_image", "Receive image"),
    ("send_file", "Send any file (robust 200 baud)"),
    ("receive_file", "Receive any file (robust 200 baud)"),
    ("send_file_fast", "Send any file (fast 800 baud — experimental)"),
    ("receive_file_fast", "Receive any file (fast 800 baud — experimental)"),
    ("send_audio_file", "Send audio file (WAV / MP3 / FLAC / OGG — 1200 baud test)"),
    ("receive_audio_file", "Receive audio file (1200 baud test)"),
    ("voice_note", "Record and send voice message"),
    ("receive_voice", "Receive voice message"),
    ("emergency_voice", "Emergency PTT — transmit voice bursts"),
    ("receive_voice_stream", "Emergency PTT — receive voice bursts"),
    ("send_hybrid", "Send text + image/file/voice simultaneously (FDM test)"),
    ("receive_hybrid", "Receive simultaneous text + media (FDM test)"),
    ("peer", "Show peer fingerprint"),
    ("close", "Close authenticated session"),
]


RETRY_KEY_RESULT = "__ECHOCRYPT_RETRY__"

# User-requested warning threshold: 1000 bits = 125 bytes.
LARGE_FILE_WARNING_BYTES = (1000 + 7) // 8


def _arm_receive_prompt(label, retry_available=False):
    """Wait for Enter to arm RX, or F8 to retransmit the previous handshake frame."""
    from prompt_toolkit.key_binding import KeyBindings

    bindings = KeyBindings()
    @bindings.add("f8")
    def _(event):
        event.app.exit(result=RETRY_KEY_RESULT)

    hint = " [F8: retransmit previous handshake frame]" if retry_available else ""
    result = prompt("Press Enter to ARM the microphone" + hint + ": ", key_bindings=bindings)
    return result != RETRY_KEY_RESULT


def _record_packet(label, cfg, max_packet_bytes=None, start_timeout=None,
                   hop_key=None, symbol_rate=None, retry_callback=None):
    timeout = cfg.listen_timeout_seconds if start_timeout is None else start_timeout
    trace = for_config(cfg)
    render_app_header(session_status=f"LISTENING: {label[:28]}")
    print(f"\n{label}")
    if trace.enabled:
        print("\n[DEBUG] Live receive diagnostics are ON.")
        print("[DEBUG] Wait until the receiver says it is listening before the peer plays audio.")
    while True:
        armed = _arm_receive_prompt(label, retry_available=retry_callback is not None)
        if not armed:
            if retry_callback is None:
                print("No previous handshake frame is available to retransmit; arm the microphone with Enter.")
                continue
            print("\n[F8] Retransmitting the previous handshake frame...")
            retry_callback()
            print("Retry transmission finished. Press Enter to arm the microphone again.")
            continue
        if trace.enabled:
            trace.emit("PROTOCOL", "Receive operation armed", label=label,
                       expected_state="HELLO-I / HELLO-R / FINISH / DATA as shown above")
        try:
            packet = receive_auto(start_timeout=timeout, max_packet_bytes=max_packet_bytes,
                                  hop_key=hop_key, symbol_rate=symbol_rate,
                                  on_debug=trace.emit if trace.enabled else None)
            if trace.enabled:
                trace.emit("PROTOCOL", "Raw modem payload delivered to protocol parser",
                           payload_bytes=len(packet), magic=packet[:4].decode("ascii", errors="replace"))
            return packet
        except Exception as exc:
            if trace.enabled:
                trace.emit("PROTOCOL", "Receive operation ended with error",
                           error=type(exc).__name__, detail=str(exc))
            raise


HANDSHAKE_COOLDOWN_SECONDS = 5
# Backwards-compatible name for code/config integrations that used the old
# HELLO-only constant. The delay now applies to every handshake control frame.
HELLO_COOLDOWN_SECONDS = HANDSHAKE_COOLDOWN_SECONDS

_HANDSHAKE_FRAME_LABELS = frozenset({
    "HELLO-I", "HELLO-R", "FINISH-I", "FINISH-R",
    "HELLO-I RETRY", "HELLO-R RETRY", "FINISH-I RETRY",
})


def _handshake_transmission_cooldown(label, seconds=HANDSHAKE_COOLDOWN_SECONDS):
    """Wait before every HELLO/FINISH playback so the peer can arm RX."""
    seconds = max(0, int(seconds))
    clean_label = str(label).strip().upper()
    if clean_label not in _HANDSHAKE_FRAME_LABELS:
        raise ValueError(f"Cooldown is only valid for handshake frames, got: {label!r}")
    print(
        f"\n{clean_label} transmission cooldown — peer: press Enter to arm the microphone.",
        flush=True,
    )
    for remaining in range(seconds, 0, -1):
        print(f"  {clean_label} starts in {remaining}s...", flush=True)
        time.sleep(1)
    print(f"  Transmitting {clean_label} now.", flush=True)


def _send_handshake_packet(packet, label, cfg, hop_key=None, symbol_rate=None):
    """Single enforced path for all initial and retransmitted HELLO/FINISH frames."""
    _handshake_transmission_cooldown(label, HANDSHAKE_COOLDOWN_SECONDS)
    return _send_packet(packet, label, cfg, hop_key=hop_key,
                        symbol_rate=symbol_rate, fec_mode="conv")


def _send_hello_packet(packet, label, cfg, hop_key=None, symbol_rate=None):
    """Compatibility wrapper; new code should use _send_handshake_packet."""
    return _send_handshake_packet(packet, label, cfg, hop_key=hop_key,
                                  symbol_rate=symbol_rate)



def _receive_auto(cfg, **kwargs):
    """Apply the configured live acoustic diagnostics to every packet receive."""
    trace = for_config(cfg)
    kwargs.setdefault("on_debug", trace.emit if trace.enabled else None)
    return receive_auto(**kwargs)


def _receive_auto_samples(cfg, **kwargs):
    """Apply diagnostics to raw composite-waveform capture used by FDM."""
    trace = for_config(cfg)
    kwargs.setdefault("on_debug", trace.emit if trace.enabled else None)
    return receive_auto_samples(**kwargs)


def _handshake_receive(handshake, packet, expected_label, trace):
    trace.emit("PARSER", "Validating control packet with authenticated handshake state machine",
               expected=expected_label, actual_magic=packet[:4].decode("ascii", errors="replace"),
               bytes=len(packet), state_before=handshake.state.value)
    try:
        result = handshake.receive(packet)
    except Exception as exc:
        trace.emit("PARSER", "Control packet rejected by handshake parser",
                   expected=expected_label, error=type(exc).__name__, detail=str(exc),
                   state=handshake.state.value)
        raise
    trace.emit("PARSER", "Control packet accepted by handshake parser",
               expected=expected_label, state_after=handshake.state.value)
    return result


RELIABLE_DATA_COOLDOWN_SECONDS = 5

def _reliable_data_transmission_cooldown(label, seconds=RELIABLE_DATA_COOLDOWN_SECONDS,
                                         cancel_event=None):
    """Give the peer time to arm its microphone before a reliable DATA frame."""
    seconds = max(0, int(seconds))
    clean_label = str(label).strip().upper()
    print(
        f"\n{clean_label} transmission cooldown — peer: press Enter to arm the microphone.",
        flush=True,
    )
    for remaining in range(seconds, 0, -1):
        _check_cancelled(cancel_event)
        print(f"  {clean_label} starts in {remaining}s...", flush=True)
        if cancel_event is not None:
            if cancel_event.wait(1.0):
                raise TransferCancelled("cancelled by user")
        else:
            time.sleep(1)
    _check_cancelled(cancel_event)
    print(f"  Transmitting {clean_label} now.", flush=True)

def _send_packet(packet, label, cfg, hop_key=None, symbol_rate=None, fec_mode="hamming",
                 pre_transmit_delay=0, cancel_event=None):
    rate = cfg.symbol_rate if symbol_rate is None else int(symbol_rate)
    render_app_header(session_status=f"TRANSMITTING: {label[:24]}")
    duration = packet_duration(len(packet), cfg, fec_mode=fec_mode, symbol_rate=rate)
    print(f"{label}: {len(packet)} bytes | {rate} baud | FEC {fec_mode} | estimated {duration:.2f}s")
    if pre_transmit_delay:
        if cancel_event is None:
            # Preserve the legacy call contract for non-cancellable control
            # paths and older callers that wrap this helper.
            _reliable_data_transmission_cooldown(label, pre_transmit_delay)
        else:
            _reliable_data_transmission_cooldown(
                label, pre_transmit_delay, cancel_event=cancel_event
            )
    _check_cancelled(cancel_event)
    actual = transmit(packet, fec_mode=fec_mode, hop_key=hop_key,
                      symbol_rate=rate, cancel_event=cancel_event)
    print(f"Playback finished ({actual:.2f}s).")

def _send_authenticated_ack(packet, label, cfg, hop_key=None, symbol_rate=None):
    """Send short authenticated ACK control frames with the robust FEC profile."""
    return _send_packet(packet, label, cfg, hop_key, symbol_rate=symbol_rate,
                        fec_mode="conv")


def _wait_for_reliable_text_ack(channel, message_id, cfg, hop_key):
    """Wait for our ACK while safely handling peer DATA crossing the ACK flight.

    The receive bound is the normal maximum packet size, not ACK_PACKET_SIZE:
    a peer retry (or its response DATA) may arrive while we are waiting. Such
    authenticated DATA is ACKed immediately and queued for the ordinary receive
    path instead of being rejected as an oversized ACK.
    """
    deadline = time.monotonic() + float(cfg.ack_timeout_seconds)
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Authenticated ACK receive window expired")
        packet = _receive_auto(
            cfg, start_timeout=remaining,
            max_packet_bytes=cfg.max_payload, hop_key=hop_key,
        )
        try:
            channel.verify_ack(packet, message_id)
            return
        except (InvalidTag, ReplayError, ValueError) as ack_error:
            # A CRC-valid modem frame may be a reverse-direction reliable DATA
            # frame rather than our ACK. Authenticate it with the DATA AAD.
            try:
                incoming = channel.receive(packet)
            except (InvalidTag, ReplayError, ValueError):
                raise ack_error
            if not incoming.duplicate:
                # Preserve the authenticated plaintext before the ACK playback;
                # an audio-device error must not discard an already-valid peer message.
                channel.defer_incoming(incoming)
            _send_authenticated_ack(
                incoming.ack_packet,
                "AUTHENTICATED ACK (CROSSING DATA)", cfg, hop_key,
            )
            if incoming.duplicate:
                print("Peer DATA retry authenticated; duplicate ACK resent.")
            else:
                print("Peer DATA arrived during ACK wait; authenticated, ACKed, and queued.")


def _establish(role, handshake, cfg):
    # HELLO and FINISH frames use fixed base tones. The shared hopping key is
    # activated only after both peers have authenticated the full transcript.
    trace = for_config(cfg)
    trace.emit("PROTOCOL", "Starting authenticated acoustic handshake",
               role=role, initial_state=handshake.state.value,
               peer_fingerprint=handshake.peer_fingerprint[:16] + "…")
    if role == "initiator":
        first = handshake.start()
        trace.emit("SERIALIZE", "Created signed HELLO-I control payload", bytes=len(first),
                   state=handshake.state.value)
        _send_handshake_packet(first, "HELLO-I", cfg)

        def retry_hello_i():
            # The responder may have timed out or may not have armed RX yet.
            # Keep the exact same signed HELLO-I, but provide the same setup window.
            _send_handshake_packet(first, "HELLO-I RETRY", cfg)

        peer_reply = _record_packet(
            "Listening for HELLO-R", cfg,
            retry_callback=retry_hello_i,
        )
        trace.emit("PARSER", "Passing received bytes to PeerHandshake.receive",
                   packet_magic=peer_reply[:4].decode("ascii", errors="replace"), bytes=len(peer_reply),
                   expected="HELLO-R")
        finish_i = _handshake_receive(handshake, peer_reply, "HELLO-R", trace)
        trace.emit("PARSER", "HELLO-R signature verified; X25519 keys derived",
                   state=handshake.state.value)
        _send_handshake_packet(finish_i, "FINISH-I", cfg)
        while True:
            # FINISH-R is the final handshake flight. There is no F8 retry here:
            # the responder has already entered ESTABLISHED after sending it.
            peer_finish = _record_packet(
                "Listening for FINISH-R", cfg,
                retry_callback=lambda: _send_handshake_packet(
                    finish_i, "FINISH-I RETRY", cfg
                ),
            )
            if peer_finish == peer_reply:
                # A delayed/retried HELLO-R: replay the cached FINISH-I safely.
                finish_i = _handshake_receive(handshake, peer_finish, "HELLO-R RETRY", trace)
                _send_handshake_packet(finish_i, "FINISH-I RETRY", cfg)
                continue
            trace.emit("PARSER", "Passing FINISH-R to authenticated control parser",
                       bytes=len(peer_finish), expected="FINISH-R")
            _handshake_receive(handshake, peer_finish, "FINISH-R", trace)
            trace.emit("PARSER", "FINISH-R verified", state=handshake.state.value)
            break
    else:
        peer_hello = _record_packet("Listening for HELLO-I", cfg)
        trace.emit("PARSER", "Passing received bytes to PeerHandshake.receive",
                   packet_magic=peer_hello[:4].decode("ascii", errors="replace"), bytes=len(peer_hello),
                   expected="HELLO-I")
        hello_r = _handshake_receive(handshake, peer_hello, "HELLO-I", trace)
        trace.emit("PARSER", "HELLO-I signature verified; response created",
                   state=handshake.state.value)
        _send_handshake_packet(hello_r, "HELLO-R", cfg)
        while True:
            peer_finish = _record_packet(
                "Listening for FINISH-I", cfg,
                retry_callback=lambda: _send_handshake_packet(hello_r, "HELLO-R RETRY", cfg),
            )
            if peer_finish == peer_hello:
                # Initiator retried HELLO-I; resend the exact cached HELLO-R.
                hello_r = _handshake_receive(handshake, peer_finish, "HELLO-I RETRY", trace)
                _send_handshake_packet(hello_r, "HELLO-R RETRY", cfg)
                continue
            trace.emit("PARSER", "Passing FINISH-I to authenticated control parser",
                       bytes=len(peer_finish), expected="FINISH-I")
            finish_r = _handshake_receive(handshake, peer_finish, "FINISH-I", trace)
            trace.emit("PARSER", "FINISH-I verified; responder confirmation created",
                       state=handshake.state.value)
            _send_handshake_packet(finish_r, "FINISH-R", cfg)
            break

    if not handshake.established:
        raise RuntimeError(f"Handshake ended in unexpected state: {handshake.state.value}")
    trace.emit("PROTOCOL", "Authenticated handshake completed", state=handshake.state.value)
    print("\nAUTHENTICATED ACOUSTIC SESSION ESTABLISHED")
    print("Peer fingerprint:", handshake.peer_fingerprint)
    print("Authenticated per-symbol frequency hopping is now enabled for session traffic.")


def _send_text_with_ack(channel, message, cfg, hop_key, first_fec_mode="conv"):
    outgoing = channel.prepare(message, max_packet_size=cfg.max_payload)
    print("Message ID:", outgoing.message_id_hex)
    last_error = None
    fec_plan = AdaptiveFECController().plan(first_fec_mode, cfg.max_retries + 1)
    for attempt, fec_mode in enumerate(fec_plan, start=1):
        print(f"\nTransmission attempt {attempt}/{cfg.max_retries + 1}")
        _send_packet(outgoing.packet, "RELIABLE DATA", cfg, hop_key, fec_mode=fec_mode,
                     pre_transmit_delay=RELIABLE_DATA_COOLDOWN_SECONDS)
        try:
            render_app_header(session_status="WAITING FOR ENCRYPTED ACK")
            print("\nWaiting for the peer's authenticated acknowledgement...")
            _wait_for_reliable_text_ack(channel, outgoing.message_id, cfg, hop_key)
            print("ACK authenticated. Message delivery confirmed.")
            return
        except Exception as exc:
            last_error = exc
            if attempt <= cfg.max_retries:
                print(f"ACK not confirmed ({exc}); retrying with the next FEC profile...")
    raise TimeoutError(f"No valid ACK after {cfg.max_retries + 1} attempts: {last_error}")


PROFILE_PREFIX = "ECHOCRYPT-PROFILE/1:"


def _receive_session_username(channel, cfg, hop_key, handshake=None):
    """Receive the encrypted profile frame across sender-side FEC retries.

    A modem/CRC or AEAD failure does not end the profile exchange. The sender
    retransmits the same authenticated ciphertext with a different FEC mode;
    RX is re-armed automatically so that retransmission can be captured.
    Application-level profile validation happens only after AEAD succeeds.
    """
    max_attempts = int(cfg.max_retries) + 1
    retry_window = max(
        float(cfg.retry_listen_seconds),
        float(cfg.ack_timeout_seconds) + RELIABLE_DATA_COOLDOWN_SECONDS + 10.0,
    )
    last_error = None

    for attempt in range(1, max_attempts + 1):
        try:
            incoming = channel.pop_pending()
            if incoming is not None:
                packet = None
                print("Using previously authenticated peer profile received during ACK wait.")
            elif attempt == 1:
                packet = _record_packet(
                    "Listening for the peer's encrypted display name", cfg,
                    max_packet_bytes=cfg.max_payload, hop_key=hop_key,
                )
            else:
                print(
                    f"\nThe previous profile frame was not accepted. "
                    f"Listening automatically for FEC retry {attempt}/{max_attempts} "
                    f"(window {retry_window:.0f}s)..."
                )
                packet = _receive_auto(
                    cfg, start_timeout=retry_window,
                    max_packet_bytes=cfg.max_payload, hop_key=hop_key,
                )
        except KeyboardInterrupt:
            raise
        except (ValueError, TimeoutError) as exc:
            last_error = exc
            if attempt < max_attempts:
                print(
                    f"Profile modem frame attempt {attempt}/{max_attempts} failed: {exc}\n"
                    "Keeping the receiver available for the sender's next FEC retry..."
                )
                continue
            break

        try:
            if (incoming is None and handshake is not None
                    and handshake.role == "responder"
                    and packet == getattr(handshake, "_finish_i", None)):
                cached_finish_r = handshake.receive(packet)
                _send_handshake_packet(cached_finish_r, "FINISH-R RETRY", cfg)
                last_error = ValueError("Duplicate FINISH-I acknowledged with cached FINISH-R")
                continue
            if incoming is None:
                incoming = channel.receive(packet)
        except KeyboardInterrupt:
            raise
        except (ValueError, InvalidTag, ReplayError) as exc:
            last_error = exc
            if attempt < max_attempts:
                print(
                    f"Profile authentication attempt {attempt}/{max_attempts} failed: {exc}\n"
                    "Keeping the receiver available for the sender's next FEC retry..."
                )
                continue
            break

        if incoming.duplicate:
            # ACK a cached ciphertext again when the previous ACK was lost.
            _send_authenticated_ack(incoming.ack_packet, "PROFILE ACK (DUPLICATE)", cfg, hop_key)
            print("Duplicate encrypted profile frame acknowledged; waiting for the peer's next frame.")
            last_error = ValueError("Duplicate profile frame acknowledged")
            continue

        # AEAD succeeded. From here, malformed application data is a protocol
        # error rather than a reason to keep accepting further modem copies.
        text = incoming.text or ""
        if not text.startswith(PROFILE_PREFIX):
            raise ValueError("Peer did not send a valid ECHO-CRYPT profile frame")
        username = text[len(PROFILE_PREFIX):]
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{1,23}", username):
            raise ValueError("Peer display name has an invalid format")
        _send_authenticated_ack(incoming.ack_packet, "PROFILE ACK", cfg, hop_key)
        return username

    raise TimeoutError(
        f"Could not receive an authenticated peer display name after {max_attempts} "
        f"attempts: {last_error}"
    ) from last_error


def _exchange_session_usernames(role, handshake, cfg, channel=None):
    """Exchange display labels inside the authenticated encrypted session."""
    channel = channel or ReliableTextChannel(handshake)
    local_name = get_username()
    local_payload = PROFILE_PREFIX + local_name
    if role == "initiator":
        _send_text_with_ack(channel, local_payload, cfg, handshake.session.hopping_key,
                            first_fec_mode="conv")
        peer_name = _receive_session_username(channel, cfg, handshake.session.hopping_key, handshake=handshake)
    else:
        peer_name = _receive_session_username(channel, cfg, handshake.session.hopping_key, handshake=handshake)
        _send_text_with_ack(channel, local_payload, cfg, handshake.session.hopping_key,
                            first_fec_mode="conv")
    return peer_name


def _receive_reliable_text(channel, cfg, hop_key, on_message=None):
    first = True
    received_any = False
    while True:
        timeout = cfg.listen_timeout_seconds if first else cfg.retry_listen_seconds
        try:
            incoming = channel.pop_pending()
            if incoming is None:
                if first:
                    packet = _record_packet("Listening for encrypted DATA", cfg,
                                            start_timeout=timeout, hop_key=hop_key)
                else:
                    packet = _receive_auto(cfg,start_timeout=timeout, max_packet_bytes=cfg.max_payload,
                                          hop_key=hop_key)
                incoming = channel.receive(packet)
            first = False
            _send_authenticated_ack(incoming.ack_packet, "AUTHENTICATED ACK", cfg, hop_key)
            if incoming.duplicate:
                print("Duplicate DATA detected. ACK resent; message was not displayed twice.")
            else:
                received_any = True
                if on_message is not None:
                    on_message(incoming.text)
                print("\n[AUTHENTICATED MESSAGE]\n" + incoming.text + "\n[END MESSAGE]\n")
        except TimeoutError:
            if not received_any:
                print("No acoustic DATA frame was detected before timeout.")
            return
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            first = False
            print("Frame rejected or ACK failed:", exc)
            print("Keeping the acoustic receiver armed for a possible retry...")


def _send_frame_wait_ack(channel, packet, transfer_id, kind, index, cfg,
                         hop_key, symbol_rate, label, first_fec_mode="conv",
                         cancel_event=None):
    last_error = None
    fec_plan = AdaptiveFECController().plan(first_fec_mode, cfg.max_retries + 1)
    for attempt, fec_mode in enumerate(fec_plan, start=1):
        _check_cancelled(cancel_event)
        # The manifest is received through a manual Enter-to-arm prompt. Once
        # accepted, the receiver stays in its automatic chunk receive loop, so
        # per-chunk cooldowns would only waste airtime and reduce throughput.
        arm_window = RELIABLE_DATA_COOLDOWN_SECONDS if kind == 0 else 0
        _send_packet(packet, f"{label} ({attempt}/{cfg.max_retries + 1})", cfg,
                     hop_key, symbol_rate, fec_mode,
                     pre_transmit_delay=arm_window, cancel_event=cancel_event)
        try:
            render_app_header(session_status="WAITING FOR FILE ACK")
            print(f"\nWaiting for ACK: {label}")
            ack = _receive_auto(cfg,start_timeout=cfg.ack_timeout_seconds,
                               max_packet_bytes=128, hop_key=hop_key,
                               symbol_rate=symbol_rate, cancel_event=cancel_event)
            channel.verify_ack(ack, transfer_id, kind, index)
            return
        except Exception as exc:
            last_error = exc
            if attempt <= cfg.max_retries:
                print(f"ACK not received ({exc}); retransmitting the same encrypted frame with the next ASRE FEC profile.")
    raise TimeoutError(f"No valid ACK after {cfg.max_retries + 1} attempts: {last_error}")


def _send_file(channel, cfg, hop_key, symbol_rate, title="Send file", source_override=None,
               ask_resume_id=True, first_fec_mode="conv", resume_id_override=None):
    if source_override is None:
        source_text = prompt("Path to file: ").strip().strip('"')
        if not source_text:
            return
        source = Path(source_text).expanduser()
    else:
        source = Path(source_override).expanduser()
    if resume_id_override is not None:
        resume_id = str(resume_id_override).strip()
    else:
        resume_id = prompt("Resume transfer ID (blank = new transfer): ").strip() if ask_resume_id else ""
    outgoing = channel.create_outgoing(source, transfer_id=resume_id or None)
    m = outgoing.manifest
    manifest_packet = channel.manifest_packet(outgoing, cfg.max_payload)
    chunk_packet_size = DATA_HEAD.size + min(m.chunk_size, m.file_size) + AEAD_OVERHEAD
    estimate = packet_duration(len(manifest_packet), cfg, fec_mode=first_fec_mode,
                               symbol_rate=symbol_rate)
    # ACK and RESUME control frames use convolutional/Viterbi FEC as well.
    estimate += packet_duration(ACK_BODY.size + AEAD_OVERHEAD, cfg, symbol_rate=symbol_rate)
    estimate += packet_duration(24 + AEAD_OVERHEAD, cfg, symbol_rate=symbol_rate)
    estimate += m.total_chunks * (
        packet_duration(chunk_packet_size, cfg, fec_mode=first_fec_mode,
                        symbol_rate=symbol_rate)
        + packet_duration(ACK_BODY.size + AEAD_OVERHEAD, cfg, symbol_rate=symbol_rate)
    )
    print(f"\nFile: {m.filename} | {m.file_size:,} bytes | SHA-256 {m.sha256}")
    print(f"Chunks: {m.total_chunks} × up to {m.chunk_size} bytes")
    print(f"Transfer ID: {m.transfer_id_hex}")
    print(f"Estimated ideal airtime at {symbol_rate} baud: {estimate / 60:.1f} minutes")
    print("Real acoustic transfer can take longer. Sender and receiver must select the same symbol rate.")

    _send_frame_wait_ack(channel, manifest_packet, m.transfer_id, 0, 0xFFFFFFFF,
                         cfg, hop_key, symbol_rate, "FILE MANIFEST", first_fec_mode)
    resume_packet = _receive_auto(cfg,start_timeout=cfg.ack_timeout_seconds,
                                 max_packet_bytes=128, hop_key=hop_key,
                                 symbol_rate=symbol_rate)
    next_missing = channel.verify_resume(resume_packet, m.transfer_id, m.total_chunks)
    if next_missing >= m.total_chunks:
        print("Receiver already has every chunk; no DATA retransmission is needed.")
    elif next_missing:
        print(f"Resuming at chunk {next_missing + 1}/{m.total_chunks}; earlier chunks are already stored by receiver.")
    else:
        print("Receiver is ready for the first chunk.")

    try:
        for index in range(next_missing, m.total_chunks):
            packet = channel.chunk_packet(outgoing, index, cfg.max_payload)
            _send_frame_wait_ack(channel, packet, m.transfer_id, 1, index,
                                 cfg, hop_key, symbol_rate, f"FILE CHUNK {index + 1}/{m.total_chunks}",
                                 first_fec_mode)
            completed = index + 1
            if completed == m.total_chunks or completed % 10 == 0:
                print(f"Progress: {completed}/{m.total_chunks} chunks acknowledged.")
        channel.complete_outgoing(outgoing)
        print("\nAll file chunks were acknowledged by the receiver.")
    except Exception:
        print("Transfer interrupted. Keep this transfer ID for resuming:", m.transfer_id_hex)
        raise


def _prompt_typed_file(media_label, allowed_extensions):
    raw_path = prompt(f"Path to {media_label}: ").strip().strip('"')
    if not raw_path:
        return None
    path = Path(raw_path).expanduser()
    if not path.exists() or not path.is_file():
        raise FileNotFoundError(f"No such file: {path}")
    suffix = path.suffix.lower()
    if suffix not in allowed_extensions:
        allowed = ", ".join(sorted(allowed_extensions))
        raise ValueError(f"Unsupported {media_label} extension {suffix or '[none]'}. Allowed: {allowed}")
    return path


def _send_typed_file(channel, cfg, hop_key, symbol_rate, media_label, allowed_extensions):
    path = _prompt_typed_file(media_label, allowed_extensions)
    if path is None:
        return
    # Images and audio files remain opaque bytes: no conversion or recompression
    # occurs, and the original filename/extension are preserved by the manifest.
    _send_file(channel, cfg, hop_key, symbol_rate,
               title=f"Send {media_label}", source_override=path)


def _receive_file(channel, cfg, hop_key, symbol_rate, title="Receive file", continuous=False,
                  allowed_extensions=None, media_label="file"):
    first_manifest = True
    while True:
        if first_manifest:
            manifest_packet = _record_packet("Listening for encrypted file manifest", cfg,
                                             max_packet_bytes=cfg.max_payload, hop_key=hop_key,
                                             symbol_rate=symbol_rate)
            first_manifest = False
        else:
            manifest_packet = _receive_auto(cfg,start_timeout=cfg.listen_timeout_seconds,
                                           max_packet_bytes=cfg.max_payload, hop_key=hop_key,
                                           symbol_rate=symbol_rate)
        result = channel.receive_manifest(manifest_packet)
        m = result.manifest
        if allowed_extensions is not None and Path(m.filename).suffix.lower() not in allowed_extensions:
            allowed = ", ".join(sorted(allowed_extensions))
            raise ValueError(
                f"Incoming {media_label} has extension {Path(m.filename).suffix or '[none]'}. "
                f"Allowed: {allowed}. Use 'Receive any file' for other formats."
            )
        print(f"Incoming {media_label}: {m.filename} | {m.file_size:,} bytes | {m.total_chunks} chunks")
        _send_authenticated_ack(result.ack_packet, "FILE MANIFEST ACK", cfg, hop_key, symbol_rate)
        _send_packet(result.resume_packet, "FILE RESUME STATUS", cfg, hop_key, symbol_rate)
        if result.completed_path:
            print("This transfer was already completed:", result.completed_path)
            _maybe_play_voice(result.completed_path, cfg)
            if continuous:
                print("Waiting for the next emergency voice burst / file manifest...")
                continue
            return
        if result.next_missing:
            print(f"Continuing from chunk {result.next_missing + 1}/{m.total_chunks}.")

        transfer_complete = False
        while not transfer_complete:
            try:
                packet = _receive_auto(cfg,start_timeout=cfg.listen_timeout_seconds,
                                      max_packet_bytes=cfg.max_payload, hop_key=hop_key,
                                      symbol_rate=symbol_rate)
                incoming = channel.receive_chunk(packet)
                _send_authenticated_ack(incoming.ack_packet, "FILE CHUNK ACK", cfg, hop_key, symbol_rate)
                print(f"Chunk {incoming.index + 1}/{m.total_chunks} acknowledged" +
                      (" (duplicate; not written twice)." if incoming.duplicate else "."))
                print(f"Received {incoming.bytes_received:,}/{m.file_size:,} bytes.")
                if incoming.completed_path:
                    print("\nFILE RECEIVED AND SHA-256 VERIFIED:", incoming.completed_path)
                    _maybe_play_voice(incoming.completed_path, cfg)
                    transfer_complete = True
            except TimeoutError:
                print("No next file frame detected. Partial data is retained for resume.")
                print("Transfer ID:", m.transfer_id_hex)
                return
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                print("File frame rejected:", exc)
                print("Waiting for a retry; accepted chunks remain safely stored.")
        if not continuous:
            return
        print("Waiting for the next emergency voice burst / file manifest...")


def _send_hybrid_file(binary_channel, text_channel, cfg, hop_key, *,
                      source_override=None, text_override=None,
                      resume_id_override=None, cancel_event=None):
    source_text = (
        prompt("Path to file or ECV voice clip: ").strip().strip('"')
        if source_override is None else str(source_override)
    )
    if not source_text:
        return
    message = (
        prompt("Text to transmit simultaneously on the second FDM band: ")
        if text_override is None else str(text_override)
    )
    if not message:
        raise ValueError("Hybrid FDM requires a non-empty text lane.")
    resume_id = (
        prompt("Resume transfer ID (blank = new transfer): ").strip()
        if resume_id_override is None else str(resume_id_override).strip()
    )
    _check_cancelled(cancel_event)
    outgoing = binary_channel.create_outgoing(
        Path(source_text).expanduser(), transfer_id=resume_id or None,
        cancel_event=cancel_event,
    )
    m = outgoing.manifest
    manifest_packet = binary_channel.manifest_packet(outgoing, cfg.max_payload)
    print(f"\nFDM transfer: {m.filename} | {m.file_size:,} bytes | {m.total_chunks} chunks")
    print("The manifest is sent first; the first file-data chunk and encrypted text then share one composite waveform.")
    _send_frame_wait_ack(
        binary_channel, manifest_packet, m.transfer_id, 0, 0xFFFFFFFF,
        cfg, hop_key, cfg.symbol_rate, "FDM FILE MANIFEST",
        cancel_event=cancel_event,
    )
    resume_packet = _receive_auto(
        cfg, start_timeout=cfg.ack_timeout_seconds,
        max_packet_bytes=128, hop_key=hop_key, cancel_event=cancel_event,
    )
    next_missing = binary_channel.verify_resume(resume_packet, m.transfer_id, m.total_chunks)
    text_out = text_channel.prepare(message, max_packet_size=cfg.max_payload)

    if next_missing >= m.total_chunks:
        # Manifest reception already armed the peer's automatic FDM sample capture.
        _check_cancelled(cancel_event)
        composite = transmit_hybrid(
            text_out.packet, None, symbol_rate=cfg.symbol_rate, cancel_event=cancel_event
        )
        print(f"FDM text-only frame transmitted ({composite:.2f}s).")
        ack = _receive_auto(
            cfg, start_timeout=cfg.ack_timeout_seconds,
            max_packet_bytes=ACK_PACKET_SIZE, hop_key=hop_key, cancel_event=cancel_event,
        )
        text_channel.verify_ack(ack, text_out.message_id)
    else:
        for index in range(next_missing, m.total_chunks):
            _check_cancelled(cancel_event)
            media_packet = binary_channel.chunk_packet(outgoing, index, cfg.max_payload)
            last_error = None
            for attempt in range(1, cfg.max_retries + 2):
                _check_cancelled(cancel_event)
                fec_mode = AdaptiveFECController().mode_for_attempt(
                    attempt, "hamming", cfg.max_retries + 1)
                duration = max(
                    packet_duration(len(text_out.packet), cfg, fec_mode=fec_mode),
                    packet_duration(len(media_packet), cfg, fec_mode=fec_mode),
                )
                print(f"\nFDM: text + media lane {index + 1}/{m.total_chunks} | estimated {duration:.2f}s")
                # The same authenticated text ciphertext accompanies every media
                # frame. The receiver is already in automatic sample capture.
                transmit_hybrid(
                    text_out.packet, media_packet, symbol_rate=cfg.symbol_rate,
                    fec_mode=fec_mode, cancel_event=cancel_event,
                )
                try:
                    text_ack = _receive_auto(
                        cfg, start_timeout=cfg.ack_timeout_seconds,
                        max_packet_bytes=ACK_PACKET_SIZE, hop_key=hop_key,
                        cancel_event=cancel_event,
                    )
                    text_channel.verify_ack(text_ack, text_out.message_id)
                    file_ack = _receive_auto(
                        cfg, start_timeout=cfg.ack_timeout_seconds,
                        max_packet_bytes=128, hop_key=hop_key,
                        cancel_event=cancel_event,
                    )
                    binary_channel.verify_ack(file_ack, m.transfer_id, 1, index)
                    last_error = None
                    break
                except Exception as exc:
                    if cancel_event is not None and cancel_event.is_set():
                        raise TransferCancelled("cancelled by user") from exc
                    last_error = exc
                    if attempt <= cfg.max_retries:
                        print("One or both FDM lane ACKs were not confirmed; retrying both exact ciphertexts.")
            if last_error is not None:
                raise TimeoutError(f"FDM dual-lane acknowledgement failed: {last_error}")
            if index + 1 == m.total_chunks or (index + 1) % 10 == 0:
                print(f"Media progress: {index + 1}/{m.total_chunks} chunks acknowledged.")
    binary_channel.complete_outgoing(outgoing)
    print("Hybrid transfer completed. Both lanes used one acoustic path; no network link was created.")


def _receive_hybrid_file(binary_channel, text_channel, cfg, hop_key):
    manifest_packet = _record_packet("Listening for FDM file manifest", cfg,
                                     max_packet_bytes=cfg.max_payload, hop_key=hop_key)
    manifest = binary_channel.receive_manifest(manifest_packet)
    m = manifest.manifest
    print(f"Incoming FDM media: {m.filename} | {m.file_size:,} bytes | {m.total_chunks} chunks")
    _send_authenticated_ack(manifest.ack_packet, "FDM MANIFEST ACK", cfg, hop_key)
    _send_packet(manifest.resume_packet, "FDM RESUME STATUS", cfg, hop_key)

    if manifest.next_missing >= m.total_chunks:
        samples = _receive_auto_samples(cfg,start_timeout=cfg.listen_timeout_seconds,
                                        max_packet_bytes=cfg.max_payload,
                                        symbol_rate=cfg.symbol_rate)
        text_packet, _ = demodulate_hybrid(samples, sample_rate=cfg.sample_rate,
                                            symbol_rate=cfg.symbol_rate,
                                            max_payload=cfg.max_payload,
                                            expect_text=True, expect_media=False)
        incoming_text = text_channel.receive(text_packet)
        _send_authenticated_ack(incoming_text.ack_packet, "FDM TEXT ACK", cfg, hop_key)
        print("\n[HYBRID TEXT]\n" + (incoming_text.text or "[duplicate]") + "\n")
        if manifest.completed_path:
            print("Media file was already complete:", manifest.completed_path)
            _maybe_play_voice(manifest.completed_path, cfg)
        return

    while True:
        samples = _receive_auto_samples(cfg,start_timeout=cfg.listen_timeout_seconds,
                                        max_packet_bytes=cfg.max_payload,
                                        symbol_rate=cfg.symbol_rate)
        text_packet, media_packet = demodulate_hybrid(
            samples, sample_rate=cfg.sample_rate, symbol_rate=cfg.symbol_rate,
            max_payload=cfg.max_payload, expect_text=True, expect_media=True)
        incoming_text = text_channel.receive(text_packet)
        incoming_media = binary_channel.receive_chunk(media_packet)
        # Send text ACK first and file ACK second; sender waits in that order.
        _send_authenticated_ack(incoming_text.ack_packet, "FDM TEXT ACK", cfg, hop_key)
        if not incoming_text.duplicate:
            print("\n[HYBRID TEXT]\n" + (incoming_text.text or "") + "\n")
        else:
            print("Repeated FDM text lane authenticated; display suppressed and ACK renewed.")
        _send_authenticated_ack(incoming_media.ack_packet, "FDM MEDIA ACK", cfg, hop_key)
        print(f"Media chunk {incoming_media.index + 1}/{m.total_chunks} acknowledged.")
        if incoming_media.completed_path:
            print("\nHYBRID MEDIA RECEIVED AND SHA-256 VERIFIED:", incoming_media.completed_path)
            _maybe_play_voice(incoming_media.completed_path, cfg)
            return

def _maybe_play_voice(path: Path, cfg):
    try:
        data = path.read_bytes()
        if not (data.startswith(VOICE_MAGIC) or data.startswith(VOICE2_MAGIC) or
                data.startswith(VOICE3_MAGIC)):
            return
        choice = prompt("This is an ECHO-CRYPT voice message. Play it now? [y/N]: ").strip().lower()
        if choice not in {"y", "yes"}:
            return
        _play_voice_file(path, cfg)
    except Exception as exc:
        print("Could not play received voice message:", exc)


def _play_voice_file(path: Path, cfg):
    """Decode an ECV file, create an adjacent standard WAV, then play it."""
    data = Path(path).read_bytes()
    samples, rate = decode_voice_message(data)
    playback_rate = max(8000, int(rate))
    if playback_rate != rate:
        count = max(1, int(round(len(samples) * playback_rate / rate)))
        source_x = np.arange(len(samples), dtype=np.float64)
        target_x = np.linspace(0, max(0, len(samples) - 1), count, dtype=np.float64)
        samples = np.interp(target_x, source_x, samples).astype(np.float32)
    wav_path = Path(path).with_suffix(".wav")
    write_voice_wav(samples, playback_rate, wav_path)
    play(samples, playback_rate, cfg.output_device)
    return wav_path


def _write_voice_file(cfg, samples, emergency_codec: bool = False) -> Path:
    samples = np.asarray(samples, dtype=np.float32).reshape(-1)
    if samples.size < int(cfg.sample_rate * 0.20):
        raise ValueError("Voice note is too short; hold Space while speaking")
    if samples.size > int(cfg.sample_rate * 60.0):
        samples = samples[:int(cfg.sample_rate * 60.0)]
    if emergency_codec:
        try:
            encoded = encode_emergency_voice_message(samples, cfg.sample_rate)
            codec_label = "Codec2 700C / 700 bit/s"
        except Codec2Unavailable as exc:
            from core.dependencies import offer_codec2_install
            if offer_codec2_install():
                try:
                    encoded = encode_emergency_voice_message(samples, cfg.sample_rate)
                    codec_label = "Codec2 700C / 700 bit/s"
                except Codec2Unavailable as retry_exc:
                    encoded = encode_voice_message(samples, cfg.sample_rate, target_rate=3000)
                    codec_label = "IMA ADPCM / 12 kbit/s (narrowband)"
            else:
                encoded = encode_voice_message(samples, cfg.sample_rate, target_rate=3000)
                codec_label = "IMA ADPCM / 12 kbit/s (narrowband)"
    else:
        try:
            encoded = encode_codec2_1300_message(samples, cfg.sample_rate)
            codec_label = "Codec2 / 1300 bit/s"
        except Codec2Unavailable:
            encoded = encode_voice_message(samples, cfg.sample_rate, target_rate=3000)
            codec_label = "IMA ADPCM / 12 kbit/s fallback"
    folder = Path("outgoing-media")
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"voice-{datetime.now().strftime('%Y%m%d-%H%M%S-%f')}.ecv"
    path.write_bytes(encoded)
    # Never print from a live prompt_toolkit chat: stdout belongs exclusively
    # to the full-screen UI. The caller presents concise metadata in the chat.
    return path


def _record_voice_file(cfg, duration: float, emergency_codec: bool = False) -> Path:
    if not 0.25 <= duration <= 60.0:
        raise ValueError("Voice message duration must be between 0.25 and 60 seconds")
    # Voice capture can be invoked from a full-screen prompt_toolkit chat.
    # Keep stdout exclusively owned by the UI; status is rendered in-chat.
    samples = record(duration, cfg.sample_rate, cfg.input_device)
    return _write_voice_file(cfg, samples, emergency_codec=emergency_codec)


def _send_emergency_voice_stream(channel, cfg):
    print("Emergency PTT: 2-second Codec2 700C bursts when libcodec2 is installed.")
    print("This is half-duplex push-to-talk with transmission delay, not simultaneous full-duplex audio.")
    print("Codec2 is optional; if unavailable, ADPCM fallback is substantially slower.")
    while True:
        path = _record_voice_file(cfg, 2.0, emergency_codec=True)
        print("Emergency profile:", cfg.emergency_symbol_rate,
              "baud, no FHSS, AEAD encryption retained, raw FEC first with stronger retries.")
        _send_file(channel, cfg, None, cfg.emergency_symbol_rate,
                   "Emergency voice burst", source_override=path, ask_resume_id=False,
                   first_fec_mode="raw")
        answer = prompt("Record and send the next 2-second burst? [y/N]: ").strip().lower()
        if answer not in {"y", "yes"}:
            return


def _run_session_action(action, reliable_text, reliable_binary, cfg, hop_key, handshake):
    if action == "send_text":
        message = prompt("Text message (UTF-8): ")
        if message:
            _send_text_with_ack(reliable_text, message, cfg, hop_key)
    elif action == "receive_text":
        _receive_reliable_text(reliable_text, cfg, hop_key)
    elif action == "send_image":
        _send_typed_file(reliable_binary, cfg, hop_key, cfg.symbol_rate,
                         "image", IMAGE_EXTENSIONS)
    elif action == "receive_image":
        _receive_file(reliable_binary, cfg, hop_key, cfg.symbol_rate,
                      allowed_extensions=IMAGE_EXTENSIONS, media_label="image")
    elif action == "send_file":
        _send_file(reliable_binary, cfg, hop_key, cfg.symbol_rate)
    elif action == "receive_file":
        _receive_file(reliable_binary, cfg, hop_key, cfg.symbol_rate)
    elif action == "send_file_fast":
        print("Fast mode requires the peer to select the matching 800-baud receive option.")
        _send_file(reliable_binary, cfg, hop_key, cfg.fast_symbol_rate)
    elif action == "receive_file_fast":
        print("Fast mode is synthetic-test verified only; physical audio reliability is unknown.")
        _receive_file(reliable_binary, cfg, hop_key, cfg.fast_symbol_rate)
    elif action == "send_audio_file":
        _send_typed_file(reliable_binary, cfg, hop_key, cfg.voice_symbol_rate,
                         "audio file", AUDIO_FILE_EXTENSIONS)
    elif action == "receive_audio_file":
        _receive_file(reliable_binary, cfg, hop_key, cfg.voice_symbol_rate,
                      allowed_extensions=AUDIO_FILE_EXTENSIONS, media_label="audio file")
    elif action == "voice_note":
        duration = float(prompt("Voice message duration in seconds [5]: ", default="5").strip())
        path = _record_voice_file(cfg, duration)
        _send_file(reliable_binary, cfg, hop_key, cfg.voice_symbol_rate,
                   "Send voice message", source_override=path)
    elif action == "receive_voice":
        _receive_file(reliable_binary, cfg, hop_key, cfg.voice_symbol_rate,
                      allowed_extensions=AUDIO_FILE_EXTENSIONS, media_label="voice/audio message")
    elif action == "emergency_voice":
        print("The peer must select Emergency PTT — receive voice bursts.")
        _send_emergency_voice_stream(reliable_binary, cfg)
    elif action == "receive_voice_stream":
        print("Emergency receive uses 1500 baud, fixed tones, and no FHSS; AEAD encryption remains active.")
        _receive_file(reliable_binary, cfg, None, cfg.emergency_symbol_rate, continuous=True,
                      allowed_extensions=AUDIO_FILE_EXTENSIONS, media_label="emergency voice burst")
    elif action == "send_hybrid":
        print("Experimental FDM: text and media occupy separate audio bands in one composite waveform.")
        _send_hybrid_file(reliable_binary, reliable_text, cfg, hop_key)
    elif action == "receive_hybrid":
        print("Experimental FDM: select this while the peer selects Send text + media (FDM).")
        _receive_hybrid_file(reliable_binary, reliable_text, cfg, hop_key)
    elif action == "peer":
        print("Pinned peer fingerprint:", handshake.peer_fingerprint)
    else:
        raise ValueError(f"Unknown session action: {action}")


def _operation_page(title, peer_label):
    # Start every blocking transfer on a clean page. The previous version
    # referenced an undefined `peer_name`, which raised before any operation.
    render_app_header(session_status=f"CONNECTED: {peer_label}")
    print(f"\n  {title}\n")


def _pause_operation():
    prompt("\nPress Enter to return to chat...")


def _receive_choice():
    return Menu("RECEIVE / LISTEN  ·  MATCH THE SENDER'S ACTION", [
        ("text", "Text message  ·  sender types and presses Enter"),
        ("file", "Any file / photo / audio  ·  sender presses F2"),
        ("voice", "Voice note  ·  sender presses F4 (voice profile)"),
        ("back", "Back to chat"),
    ], header_status="CONNECTED").run()


def _emergency_choice():
    return Menu("EMERGENCY CONNECTION", [
        ("transmit", "Transmit emergency PTT audio bursts"),
        ("receive", "Receive emergency PTT audio bursts"),
        ("back", "Back to chat"),
    ], header_status="CONNECTED").run()


def _more_choice():
    return Menu("MORE / EXPERIMENTAL MODES", [
        ("resume", "Resume interrupted file transfer"),
        ("send_fast", "Send file — fast 800 baud (experimental)"),
        ("receive_fast", "Receive file — fast 800 baud (experimental)"),
        ("send_hybrid", "Send text + attachment simultaneously (FDM test)"),
        ("receive_hybrid", "Receive simultaneous text + attachment (FDM test)"),
        ("peer", "Show peer fingerprint"),
        ("back", "Back to chat"),
    ], header_status="CONNECTED").run()


def _engine_wait_for_response(engine, packet, verify, cfg, label,
                              first_fec_mode="conv", waiter=None,
                              symbol_rate=None, fallback_rates=None,
                              cancel_event=None):
    """Retry one immutable application ciphertext across bounded PHY profiles."""
    own_waiter = waiter is None
    waiter = waiter or engine.expect_packet(verify)
    last_error = None
    fec_plan = AdaptiveFECController().plan(first_fec_mode, cfg.max_retries + 1)
    base_rate = int(symbol_rate or cfg.symbol_rate)
    rate_plan = tuple(int(rate) for rate in (fallback_rates or (base_rate,)))
    if not rate_plan or any(rate <= 0 for rate in rate_plan):
        raise ValueError("A valid acoustic symbol-rate fallback plan is required")
    try:
        for attempt, fec_mode in enumerate(fec_plan, start=1):
            _check_cancelled(cancel_event)
            rate = rate_plan[min(attempt - 1, len(rate_plan) - 1)]
            future = engine.submit_packet(packet, fec_mode=fec_mode,
                                          symbol_rate=rate,
                                          label=f"{label} ({attempt}/{len(fec_plan)})",
                                          cancel_event=cancel_event)
            duration = packet_duration(len(packet), cfg, fec_mode=fec_mode,
                                       symbol_rate=rate)
            try:
                _wait_future_or_cancel(future, cancel_event, duration + 30.0)
                waiter.wait(timeout=cfg.ack_timeout_seconds, cancel_event=cancel_event)
                return True
            except Exception as exc:
                if cancel_event is not None and cancel_event.is_set():
                    raise TransferCancelled("cancelled by user") from exc
                last_error = exc
                if attempt < len(fec_plan):
                    continue
        raise TimeoutError(f"{label}: no valid acoustic response after {len(fec_plan)} attempts: {last_error}")
    finally:
        if own_waiter:
            engine.cancel_waiter(waiter)


def _reliable_chat_symbol_rate_plan(cfg, preferred=None):
    """Return bounded PHY retries from medium to increasingly robust rates."""
    candidates = [preferred or 400, cfg.symbol_rate, 100]
    if getattr(cfg, "fast_symbol_rate", None):
        candidates.append(cfg.fast_symbol_rate)
    rates = []
    for raw_rate in candidates:
        try:
            rate = int(raw_rate)
            if (rate > 0 and cfg.sample_rate % rate == 0 and
                    cfg.sample_rate // rate >= 32 and rate not in rates):
                rates.append(rate)
        except (TypeError, ValueError, ZeroDivisionError):
            continue
    if not rates:
        raise ValueError("No valid acoustic symbol-rate profile is available")
    return tuple(rates[:3])


def _engine_send_text(channel, engine, text, cfg, progress_callback=None, cancel_event=None):
    outgoing = channel.prepare(text, max_packet_size=cfg.max_payload)

    def verify(packet):
        try:
            return channel.verify_ack(packet, outgoing.message_id)
        except Exception:
            return False

    rates = _reliable_chat_symbol_rate_plan(cfg)
    if progress_callback:
        progress_callback("TEXT", 0, 1, "frames")
    _engine_wait_for_response(engine, outgoing.packet, verify, cfg,
                              "RELIABLE TEXT", first_fec_mode="conv",
                              symbol_rate=rates[0], fallback_rates=rates,
                              cancel_event=cancel_event)
    if progress_callback:
        progress_callback("TEXT", 1, 1, "frames")
    return outgoing.message_id


def _supported_symbol_rates(cfg):
    """Receive reliable chat rates first, while retaining fast-rate compatibility.

    Direct reliable sends and Broadcast prefer 400 baud. Older peers may still
    transmit at the configured fast rate, so retain it as a final fallback.
    """
    candidates = (400, cfg.symbol_rate, 100, cfg.fast_symbol_rate)
    rates = []
    for raw_rate in candidates:
        try:
            rate = int(raw_rate)
            if rate > 0 and cfg.sample_rate % rate == 0 and cfg.sample_rate // rate >= 32:
                if rate not in rates:
                    rates.append(rate)
        except (TypeError, ValueError, ZeroDivisionError):
            continue
    return tuple(rates)


def _file_symbol_rate_plan(cfg):
    """Prefer the fast profile, then fall back to a middle and robust rate."""
    candidates = [cfg.fast_symbol_rate]
    if cfg.sample_rate % 400 == 0 and cfg.sample_rate // 400 >= 32:
        candidates.append(400)
    candidates.append(cfg.symbol_rate)
    return tuple(dict.fromkeys(int(rate) for rate in candidates))


def _engine_send_file(channel, engine, source, cfg, *,
                      progress_callback=None, on_outgoing_created=None,
                      cancel_event=None, transfer_id=None, preferred_symbol_rate=None):
    """Send a file and wait for authenticated confirmation of the remote SHA-256.

    Chunk ACKs prove that each piece was durably received, not that the complete
    file passed its final digest check. The receiver's authenticated completion
    packet is the commit signal; until it arrives, the sender keeps its resume
    state and can replay the manifest to recover a lost completion notice.
    """
    _check_cancelled(cancel_event)
    outgoing = channel.create_outgoing(
        source, transfer_id=transfer_id, cancel_event=cancel_event
    )
    manifest = outgoing.manifest
    if on_outgoing_created is not None:
        on_outgoing_created(outgoing)
    if progress_callback:
        progress_callback(f"{manifest.filename} · manifest", 0, 1, "frames")
    manifest_packet = channel.manifest_packet(outgoing, cfg.max_payload)
    rates = _reliable_chat_symbol_rate_plan(cfg, preferred=preferred_symbol_rate)

    completion_result = {}
    completion_event = threading.Event()

    def verify_completion(packet):
        try:
            received_id, digest = channel.receive_completion_packet(packet)
        except Exception:
            return False
        if received_id != manifest.transfer_id:
            return False
        completion_result["sha256"] = digest
        completion_event.set()
        return True

    # Register before the first manifest. Empty files and already-completed
    # resume IDs can receive their completion notice immediately after META.
    completion_waiter = engine.expect_packet(verify_completion)

    def exchange_manifest():
        resume_result = {"index": None}

        def verify_manifest_ack(packet):
            try:
                return channel.verify_ack(packet, manifest.transfer_id, 0, 0xFFFFFFFF)
            except Exception:
                return False

        def verify_resume(packet):
            try:
                resume_result["index"] = channel.verify_resume(
                    packet, manifest.transfer_id, manifest.total_chunks
                )
                return True
            except Exception:
                return False

        # Both control waiters must exist before META is transmitted because
        # the receiver sends ACK and RESUME back-to-back.
        ack_waiter = engine.expect_packet(verify_manifest_ack)
        resume_waiter = engine.expect_packet(verify_resume)
        try:
            _engine_wait_for_response(
                engine, manifest_packet, verify_manifest_ack, cfg,
                "FILE MANIFEST", first_fec_mode="conv",
                waiter=ack_waiter, symbol_rate=rates[0],
                fallback_rates=rates, cancel_event=cancel_event,
            )
            resume_waiter.wait(
                timeout=cfg.ack_timeout_seconds, cancel_event=cancel_event
            )
        finally:
            engine.cancel_waiter(ack_waiter)
            engine.cancel_waiter(resume_waiter)

        next_missing = resume_result["index"]
        if next_missing is None:
            raise BinaryTransferError("Receiver did not provide a valid file resume map")
        return next_missing

    def send_chunks(start_index):
        if progress_callback:
            progress_callback(
                manifest.filename, start_index, manifest.total_chunks, "chunks"
            )
        for index in range(start_index, manifest.total_chunks):
            _check_cancelled(cancel_event)
            packet = channel.chunk_packet(outgoing, index, cfg.max_payload)

            def verify_chunk_ack(response, expected=index):
                try:
                    return channel.verify_ack(
                        response, manifest.transfer_id, 1, expected
                    )
                except Exception:
                    return False

            _engine_wait_for_response(
                engine, packet, verify_chunk_ack, cfg,
                f"FILE CHUNK {index + 1}/{manifest.total_chunks}",
                first_fec_mode="conv", symbol_rate=rates[0],
                fallback_rates=rates, cancel_event=cancel_event,
            )
            if progress_callback:
                progress_callback(
                    manifest.filename, index + 1, manifest.total_chunks, "chunks"
                )

    try:
        next_missing = exchange_manifest()
        repair_attempts = 0
        while True:
            if next_missing < manifest.total_chunks:
                send_chunks(next_missing)

            try:
                completion_waiter.wait(
                    timeout=cfg.ack_timeout_seconds, cancel_event=cancel_event
                )
                break
            except TimeoutError:
                if completion_event.is_set():
                    break
                if repair_attempts >= cfg.max_retries:
                    raise TimeoutError(
                        "All file chunks were acknowledged, but the receiver's "
                        "authenticated SHA-256 completion notice was not received"
                    )
                repair_attempts += 1
                # A repeated manifest is safe: the receiver returns its current
                # resume point and re-emits completion if the file was committed.
                next_missing = exchange_manifest()

        received_digest = completion_result.get("sha256")
        if received_digest != manifest.sha256:
            raise BinaryTransferError(
                "Receiver completion digest does not match the outgoing file"
            )
        channel.complete_outgoing(outgoing)
        return manifest
    finally:
        engine.cancel_waiter(completion_waiter)


def _engine_resend_chunks(channel, engine, outgoing, indexes, cfg, progress_callback=None,
                          cancel_event=None):
    """Respond to a peer's explicit resend request without a new manifest."""
    manifest = outgoing.manifest
    indexes = tuple(sorted(set(int(i) for i in indexes)))
    if not indexes:
        return 0
    if any(index < 0 or index >= manifest.total_chunks for index in indexes):
        raise BinaryTransferError("Peer requested a chunk outside the original file")
    for position, index in enumerate(indexes, 1):
        _check_cancelled(cancel_event)
        packet = channel.chunk_packet(outgoing, index, cfg.max_payload)
        def verify_ack(response, expected=index):
            try:
                return channel.verify_ack(response, manifest.transfer_id, 1, expected)
            except Exception:
                return False
        rates = _reliable_chat_symbol_rate_plan(cfg)
        _engine_wait_for_response(engine, packet, verify_ack, cfg,
                                  f"RESEND CHUNK {index + 1}/{manifest.total_chunks}",
                                  first_fec_mode="conv", symbol_rate=rates[0],
                                  fallback_rates=rates, cancel_event=cancel_event)
        if progress_callback:
            progress_callback(f"RESEND {manifest.filename}", position, len(indexes), "requested chunks")
    return len(indexes)


def _direct_packet_router(engine_ref, reliable_text, reliable_binary, messages, lock,
                          incoming_transfers=None, on_resend_request=None,
                          on_integrity_failure=None, on_progress=None):
    """Classify authenticated DATA/control packets and expose receive progress."""
    incoming_transfers = incoming_transfers if incoming_transfers is not None else {}

    def add(speaker, body, status=""):
        with lock:
            return add_chat_message(messages, speaker, body, status)

    def report(label, received, total, unit="chunks", active=True):
        if on_progress is not None:
            on_progress(label, received, total, unit, active)

    def send_completion(manifest, engine, symbol_rate):
        """Notify the sender only after the receiver has committed a verified file."""
        packet = reliable_binary.make_completion_packet(
            manifest.transfer_id, manifest.sha256
        )
        engine.submit_packet(
            packet, fec_mode="conv", symbol_rate=symbol_rate,
            label="FILE TRANSFER COMPLETE · SHA-256 VERIFIED",
            channel_access=True,
        )

    def on_packet(packet):
        engine = engine_ref["engine"]
        reply_rate = engine.symbol_rates[0]
        try:
            incoming = reliable_text.receive(packet)
        except Exception:
            incoming = None
        if incoming is not None:
            engine.submit_packet(incoming.ack_packet, fec_mode="conv",
                                 symbol_rate=reply_rate, label="TEXT ACK", channel_access=True)
            if not incoming.duplicate and incoming.text is not None:
                if incoming.text.startswith(PROFILE_PREFIX):
                    return
                add("peer", incoming.text, "received · authenticated")
            return

        try:
            manifest = reliable_binary.receive_manifest(packet)
        except Exception:
            manifest = None
        if manifest is not None:
            engine.submit_packet(manifest.ack_packet, fec_mode="conv",
                                 symbol_rate=reply_rate, label="FILE MANIFEST ACK", channel_access=True)
            engine.submit_packet(manifest.resume_packet, fec_mode="conv",
                                 symbol_rate=reply_rate, label="FILE RESUME STATUS", channel_access=True)
            m = manifest.manifest
            tid = m.transfer_id
            received, total, missing, completed = reliable_binary.transfer_progress(tid)
            with lock:
                entry = incoming_transfers.get(tid)
                if entry is None:
                    is_voice = str(m.filename).casefold().endswith(".ecv")
                    entry = add_chat_message(messages, "peer",
                        (f"Voice message: {m.filename}" if is_voice else
                         f"Attachment: {m.filename} ({m.file_size:,} bytes)"), "")
                    incoming_transfers[tid] = entry
                entry["status"] = (f"received {received}/{total} chunks · complete" if completed
                                   else f"received {received}/{total} chunks")
                if missing and not completed:
                    entry["retry_request"] = {"mode": "direct", "transfer_id": tid,
                                               "missing": tuple(missing)}
                else:
                    entry.pop("retry_request", None)
            report(f"RX FILE {m.filename}", received, total, "chunks", not completed)
            if manifest.completed_path is not None:
                with lock:
                    if manifest.completed_path.suffix.casefold() == ".ecv":
                        entry["body"] = f"Voice message received: {manifest.completed_path.name}"
                        entry["voice_path"] = str(manifest.completed_path)
                        entry["status"] = "complete · SHA-256 verified · Play voice"
                    else:
                        entry["body"] = f"Attachment saved: {manifest.completed_path}"
                        entry["status"] = "complete · SHA-256 verified"
                    entry.pop("retry_request", None)
                # This completion packet is emitted only when the receiver has
                # already verified the final SHA-256 and atomically committed
                # the destination. Duplicate completed manifests re-emit it,
                # allowing the sender to recover a lost completion notice.
                send_completion(manifest.manifest, engine, reply_rate)
            return

        try:
            chunk = reliable_binary.receive_chunk(packet)
        except ChunkIntegrityError as exc:
            # The current DATA frame is authenticated and stored, although the
            # whole-file hash shows that one or more chunks need repair. ACK
            # this valid frame before requesting fresh ciphertext for the bad
            # indexes, otherwise the sender's stop-and-wait loop can stall.
            try:
                engine.submit_packet(
                    exc.ack_packet, fec_mode="conv", symbol_rate=reply_rate,
                    label="FILE CHUNK ACK · INTEGRITY REPAIR", channel_access=True,
                )
            except Exception:
                pass
            if on_integrity_failure is not None:
                on_integrity_failure(exc.transfer_id, exc.indexes)
            return
        except Exception:
            chunk = None
        if chunk is not None:
            engine.submit_packet(chunk.ack_packet, fec_mode="conv",
                                 symbol_rate=reply_rate, label="FILE CHUNK ACK", channel_access=True)
            received, total, missing, completed = reliable_binary.transfer_progress(chunk.transfer_id)
            with lock:
                entry = incoming_transfers.get(chunk.transfer_id)
                if entry is None:
                    entry = add_chat_message(messages, "peer", "Incoming attachment", "")
                    incoming_transfers[chunk.transfer_id] = entry
                entry["status"] = (f"received {received}/{total} chunks · SHA-256 verified"
                                   if completed else f"received {received}/{total} chunks · {len(missing)} missing")
                if missing and not completed:
                    entry["retry_request"] = {"mode": "direct", "transfer_id": chunk.transfer_id,
                                               "missing": tuple(missing)}
                else:
                    entry.pop("retry_request", None)
                if chunk.completed_path is not None:
                    if chunk.completed_path.suffix.casefold() == ".ecv":
                        entry["body"] = f"Voice message received: {chunk.completed_path.name}"
                        entry["voice_path"] = str(chunk.completed_path)
                        entry["status"] = "complete · SHA-256 verified · Play voice"
                    else:
                        entry["body"] = f"Attachment saved: {chunk.completed_path}"
                    completed_manifest = reliable_binary.manifest_for_transfer(
                        chunk.transfer_id
                    )
                    send_completion(completed_manifest, engine, reply_rate)
            report("RX FILE", received, total, "chunks", not completed)
            return

        try:
            request = reliable_binary.receive_resend_request(packet)
        except Exception:
            request = None
        if request is not None:
            if on_resend_request is not None:
                transfer_id, indexes = request
                on_resend_request(transfer_id, indexes)
            return

    return on_packet


def _broadcast_packet_router(engine_ref, codec, reassembler, retransmit_cache,
                             messages, lock, partial_messages, on_progress=None,
                             on_debug=None, on_resend_frames=None):
    def debug(stage, message, **fields):
        if on_debug is not None:
            try:
                on_debug(stage, message, **fields)
            except Exception:
                pass

    def add(body, status="", key=None, retry=None):
        with lock:
            if key is not None and key in partial_messages:
                entry = partial_messages[key]
                entry["body"] = str(body)
                entry["status"] = str(status)
            else:
                entry = add_chat_message(messages, "peer", body, status)
                if key is not None:
                    partial_messages[key] = entry
            if retry is not None:
                entry["retry_request"] = retry
            else:
                entry.pop("retry_request", None)
            return entry

    def report(label, received, total, unit="chunks", active=True):
        if on_progress is not None:
            on_progress(label, received, total, unit, active)

    def on_packet(packet):
        engine = engine_ref["engine"]
        raw = bytes(packet)
        debug("BROADCAST_RX", "Validated modem packet entered open-room protocol router",
              packet_bytes=len(raw), packet_magic=raw[:4].decode("ascii", errors="replace"),
              sender_hint=raw[8:16].hex() if len(raw) >= 16 else "unavailable")
        try:
            delivery = reassembler.feed(raw)
        except Exception as exc:
            debug("BROADCAST_ERROR", "Broadcast frame rejected by codec/reassembler",
                  packet_bytes=len(raw), packet_magic=raw[:4].decode("ascii", errors="replace"),
                  error=type(exc).__name__, detail=str(exc))
            raise
        if delivery is None:
            debug("BROADCAST_FRAGMENT", "Broadcast packet accepted but did not complete an application delivery",
                  packet_bytes=len(raw))
            return
        if isinstance(delivery, tuple) and delivery[0] == "retry_request":
            target_sender, target_mid, target_kind, request_flags, indexes = delivery[1]
            debug("BROADCAST_RETRY", "Selective resend request decoded",
                  target_sender=bytes(target_sender).hex()[:12],
                  message_id=bytes(target_mid).hex(), kind=int(target_kind),
                  requested_header=bool(request_flags & RETRY_NEED_HEADER),
                  requested_data=bool(request_flags & RETRY_NEED_DATA),
                  requested_chunks=len(indexes),
                  requested_indexes=",".join(str(int(x)) for x in indexes[:64]))
            try:
                frames = retransmit_cache.handle_request(delivery[1])
            except Exception as exc:
                debug("BROADCAST_RETRY_ERROR", "Parsed retry request could not be resolved in retransmit cache",
                      error=type(exc).__name__, detail=str(exc),
                      request_kind=int(target_kind), requested_chunks=len(indexes))
                raise
            debug("BROADCAST_RETRY", "Resend cache lookup completed",
                  available_frames=len(frames), requested_chunks=len(indexes))
            resend_jobs = []
            for frame in frames:
                # Retransmit with the PHY profile declared by the cached
                # fragment descriptor, not blindly with the local default.
                cached = codec.decode(frame)
                retry_rate = int(cached.symbol_rate or _reliable_chat_symbol_rate_plan(engine.config)[0])
                if cached.fec_id != FEC_ID_CONVOLUTIONAL:
                    raise ValueError(f"Unsupported cached Broadcast FEC id: {cached.fec_id}")
                resend_jobs.append((bytes(frame), retry_rate, "conv"))
            if on_resend_frames is not None:
                # Production chat owns serialized transmission. Invoke it even
                # on a cache miss so the sender gets an explicit diagnostic.
                on_resend_frames(
                    target_sender, target_mid, target_kind, tuple(indexes),
                    tuple(resend_jobs),
                )
                if resend_jobs:
                    debug("BROADCAST_TX", "Requested broadcast frames handed to serialized resend worker",
                          requested_chunks=len(indexes), queued_frames=len(resend_jobs))
                else:
                    debug("BROADCAST_RETRY", "No requested Broadcast frames are available in the sender cache",
                          target_sender=bytes(target_sender).hex()[:12],
                          message_id=bytes(target_mid).hex(), kind=int(target_kind),
                          requested_chunks=len(indexes))
            else:
                # Compatibility fallback for standalone router callers. Production
                # chat supplies a worker that sends these frames sequentially.
                for index, (frame, retry_rate, retry_fec) in enumerate(resend_jobs, 1):
                    engine.submit_packet(frame, fec_mode=retry_fec,
                                         symbol_rate=retry_rate, hop_key=None,
                                         label=f"BROADCAST RETRY {index}/{len(resend_jobs)} @ {retry_rate}",
                                         channel_access=(index == 1))
                    debug("BROADCAST_TX", "Requested broadcast frame queued for retransmission",
                          frame_number=index, frame_count=len(resend_jobs), frame_bytes=len(frame))
                    report("TX BROADCAST RESEND", index, len(resend_jobs), "frames",
                           index < len(resend_jobs))
            if not frames:
                debug("BROADCAST_RETRY", "No requested broadcast frames remain in resend cache",
                      requested_chunks=len(indexes))
                report("TX BROADCAST RESEND", 0, 0, "frames", False)
            return
        if delivery is None or getattr(delivery, "duplicate", False):
            if getattr(delivery, "duplicate", False):
                debug("BROADCAST_DEDUP", "Duplicate broadcast fragment suppressed",
                      kind=int(delivery.kind), sender_id=delivery.sender_id.hex()[:12],
                      message_id=delivery.message_id.hex(),
                      fragment_index=getattr(delivery, "fragment_index", None))
            return
        sender = delivery.sender_id.hex()[:8]
        mid = delivery.message_id
        debug("BROADCAST_DECODE", "Broadcast payload authenticated and decoded",
              kind=int(delivery.kind), sender_id=sender, message_id=mid.hex(),
              complete=bool(delivery.complete), payload_bytes=len(delivery.payload),
              total_fragments=int(delivery.total_fragments or 0),
              missing_fragments=len(delivery.missing or ()))
        entry_key = (delivery.sender_id, mid, delivery.kind)
        if delivery.kind == BroadcastType.TEXT:
            total = max(1, int(delivery.total_fragments or 1))
            if delivery.complete:
                text = delivery.payload.decode("utf-8", errors="strict")
                add(f"[{sender}] {text}", f"broadcast · received {total}/{total} fragments · complete",
                    key=entry_key)
                debug("BROADCAST_DELIVER", "Complete broadcast text delivered to chat UI",
                      sender_id=sender, message_id=mid.hex(),
                      payload_bytes=len(delivery.payload), fragments=total)
                report("RX TEXT", total, total, "fragments", False)
            else:
                received = max(0, total - len(delivery.missing))
                retry = {"mode": "broadcast", "sender_id": delivery.sender_id,
                         "message_id": mid, "kind": BroadcastType.TEXT,
                         "request_flags": RETRY_NEED_HEADER | RETRY_NEED_DATA,
                         "missing": tuple(delivery.missing)}
                add(f"[{sender}] {delivery.preview or '[partial text]'}",
                    f"received {received}/{total} chunks · {len(delivery.missing)} missing",
                    key=entry_key, retry=retry)
                debug("BROADCAST_PARTIAL", "Partial broadcast text shown in chat UI",
                      sender_id=sender, message_id=mid.hex(), received_fragments=received,
                      total_fragments=total, missing_fragments=len(delivery.missing))
                report("RX TEXT", received, total, "chunks", True)
        elif delivery.kind == BroadcastType.FILE_META:
            meta = reassembler.file_metadata(delivery.sender_id, mid) or {}
            received, total, missing = reassembler.file_progress(delivery.sender_id, mid)
            name = meta.get("filename", "incoming file")
            is_voice = (meta.get("media_kind") == MEDIA_VOICE or
                        str(name).casefold().endswith(".ecv"))
            key = (delivery.sender_id, mid, BroadcastType.FILE_CHUNK)
            retry = None
            if missing:
                retry = {"mode": "broadcast", "sender_id": delivery.sender_id,
                         "message_id": mid, "kind": BroadcastType.FILE_CHUNK,
                         "request_flags": RETRY_NEED_HEADER | RETRY_NEED_DATA,
                         "missing": tuple(missing)}
            title = "Voice message" if is_voice else "File"
            add(f"[{sender}] {title}: {name}", f"received {received}/{total} chunks",
                key=key, retry=retry)
            debug("BROADCAST_FILE", "Broadcast file manifest processed",
                  sender_id=sender, message_id=mid.hex(), filename=name,
                  file_chunks=total, received_chunks=received,
                  missing_chunks=len(missing), manifest_bytes=len(delivery.payload))
            report(f"RX FILE {name}", received, total, "chunks", bool(missing))
            if delivery.path is not None:
                add(f"[{sender}] File received: {delivery.path}",
                    "complete · SHA-256 verified", key=key)
                debug("BROADCAST_FILE", "Broadcast file completed and SHA-256 verified",
                      sender_id=sender, message_id=mid.hex(), path=str(delivery.path),
                      total_chunks=total)
                report("RX FILE", total, total, "chunks", False)
        elif delivery.kind == BroadcastType.FILE_CHUNK and delivery.complete and delivery.path:
            key = (delivery.sender_id, mid, BroadcastType.FILE_CHUNK)
            if delivery.media_kind == MEDIA_VOICE or delivery.path.suffix.casefold() == ".ecv":
                entry = add(f"[{sender}] Voice message received · {delivery.path.name}",
                            "complete · SHA-256 verified · press Play voice", key=key)
                with lock:
                    entry["voice_path"] = str(delivery.path)
            else:
                add(f"[{sender}] File received: {delivery.path}",
                    "complete · SHA-256 verified", key=key)
            total = int(delivery.total_fragments or 0)
            debug("BROADCAST_FILE", "Broadcast file completed and SHA-256 verified",
                  sender_id=sender, message_id=mid.hex(), path=str(delivery.path),
                  total_chunks=total)
            report("RX FILE", total, total, "chunks", False)
        elif delivery.kind == BroadcastType.FILE_CHUNK and not delivery.complete:
            key = (delivery.sender_id, mid, BroadcastType.FILE_CHUNK)
            meta_present = reassembler.has_file_manifest(delivery.sender_id, mid)
            received = max(0, int(delivery.total_fragments or 0) - len(delivery.missing))
            total = int(delivery.total_fragments or 0)
            if meta_present:
                retry_kind = BroadcastType.FILE_CHUNK
                missing = tuple(delivery.missing)
                retry_label = f"{len(missing)} missing"
                request_flags = RETRY_NEED_HEADER | RETRY_NEED_DATA
            else:
                retry_kind = BroadcastType.FILE_META
                missing = (0,)
                retry_label = "manifest missing"
                request_flags = RETRY_NEED_HEADER
            retry = {"mode": "broadcast", "sender_id": delivery.sender_id,
                     "message_id": mid, "kind": retry_kind,
                     "request_flags": request_flags, "missing": missing}
            meta = reassembler.file_metadata(delivery.sender_id, mid) or {}
            name = meta.get("filename", "file data")
            title = "Receiving voice message" if meta.get("media_kind") == MEDIA_VOICE or str(name).casefold().endswith(".ecv") else "Receiving file"
            add(f"[{sender}] {title}: {name}",
                f"received {received}/{total} chunks · {retry_label}",
                key=key, retry=retry)
            debug("BROADCAST_FILE", "Broadcast file chunk stored",
                  sender_id=sender, message_id=mid.hex(), filename=name,
                  received_chunks=received, total_chunks=total,
                  missing_chunks=len(delivery.missing), manifest_present=meta_present)
            report("RX FILE", received, total, "chunks", True)
        elif delivery.kind in {BroadcastType.VOICE, BroadcastType.VOICE_FRAME}:
            total = max(1, int(delivery.total_fragments or 1))
            if delivery.complete:
                add(f"[{sender}] Voice payload received ({len(delivery.payload)} bytes)",
                    f"broadcast · complete {total}/{total} chunks", key=entry_key)
                debug("BROADCAST_VOICE", "Complete broadcast voice payload delivered",
                      sender_id=sender, message_id=mid.hex(),
                      payload_bytes=len(delivery.payload), chunks=total)
                report("RX VOICE", total, total, "chunks", False)
            else:
                received = max(0, total - len(delivery.missing))
                retry = {"mode": "broadcast", "sender_id": delivery.sender_id,
                         "message_id": mid, "kind": delivery.kind,
                         "request_flags": RETRY_NEED_HEADER | RETRY_NEED_DATA,
                         "missing": tuple(delivery.missing)}
                add(f"[{sender}] Receiving voice payload",
                    f"received {received}/{total} chunks", key=entry_key, retry=retry)
                debug("BROADCAST_VOICE", "Partial broadcast voice payload retained",
                      sender_id=sender, message_id=mid.hex(),
                      received_chunks=received, total_chunks=total,
                      missing_chunks=len(delivery.missing))
                report("RX VOICE", received, total, "chunks", True)

    return on_packet


def _broadcast_retry_worker(engine_ref, codec, reassembler, stop_event, on_debug=None):
    last_requested = {}
    while engine_ref["engine"] is not None and engine_ref["engine"].running and not stop_event.is_set():
        engine = engine_ref["engine"]
        now = time.monotonic()
        listener = getattr(engine, "listener", None)
        # Do not inject an automatic request into an active incoming burst. A
        # chunk can take tens of seconds at conservative symbol rates; the
        # receiver must first hear the sender's already queued next chunk.
        if bool(getattr(listener, "busy", False)):
            stop_event.wait(0.5)
            continue
        for target_sender, message_id, kind, missing in reassembler.retry_requests(older_than=12.0):
            key = (target_sender, message_id, kind, tuple(missing))
            if now - last_requested.get(key, 0.0) < 15.0:
                continue
            try:
                request_flags = (RETRY_NEED_HEADER if kind == BroadcastType.FILE_META
                                 else RETRY_NEED_HEADER | RETRY_NEED_DATA)
                if on_debug is not None:
                    on_debug("BROADCAST_RETRY", "Automatic selective resend timer found missing fragments",
                             target_sender=bytes(target_sender).hex()[:12],
                             message_id=bytes(message_id).hex(), kind=int(kind),
                             requested_header=bool(request_flags & RETRY_NEED_HEADER),
                             requested_data=bool(request_flags & RETRY_NEED_DATA),
                             missing_chunks=len(missing),
                             requested_indexes=",".join(str(int(x)) for x in missing[:64]))
                maximum = min(96, max(1, (codec.max_fragment_payload - RETRY_REQUEST_V2.size) // 2))
                retry_rate = _reliable_chat_symbol_rate_plan(engine.config)[0]
                for batch_index in range(0, len(missing), maximum):
                    if stop_event.is_set():
                        break
                    batch = missing[batch_index:batch_index + maximum]
                    request = codec.encode_retry_request(
                        target_sender, message_id, kind, batch,
                        request_flags=request_flags,
                    )
                    duration = packet_duration(
                        len(request), engine.config, fec_mode="conv", symbol_rate=retry_rate
                    )
                    future = engine.submit_packet(
                        request, fec_mode="conv", symbol_rate=retry_rate, hop_key=None,
                        label=f"BROADCAST SELECTIVE RETRY {batch_index // maximum + 1}",
                        cancel_event=stop_event,
                    )
                    _wait_future_or_cancel(future, stop_event, duration + 30.0)
                    if on_debug is not None:
                        on_debug("BROADCAST_RETRY", "Automatic selective resend request transmitted",
                                 batch=batch_index // maximum + 1,
                                 batch_chunks=len(batch), packet_bytes=len(request))
                last_requested[key] = time.monotonic()
            except Exception as exc:
                if on_debug is not None:
                    on_debug("BROADCAST_RETRY_ERROR", "Automatic selective resend request failed",
                             error=type(exc).__name__, detail=str(exc))
        stop_event.wait(1.0)


def _broadcast_send_frames(engine, frames, label, *, copies=1, progress_callback=None,
                           cancel_event=None, symbol_rate=None):
    """Send public frames, reporting progress and honoring user cancellation."""
    # Broadcast has no delivery ACK: prefer the conservative reliable-chat
    # profile rather than blindly selecting the fastest available rate.
    rate = int(symbol_rate or _reliable_chat_symbol_rate_plan(engine.config)[0])
    copies = max(1, min(5, int(copies)))
    total = len(frames)
    completed = 0
    for index, frame in enumerate(frames):
        _check_cancelled(cancel_event)
        duration = packet_duration(len(frame), engine.config, fec_mode="conv", symbol_rate=rate)
        for copy_index in range(copies):
            _check_cancelled(cancel_event)
            engine.send_packet(
                frame, fec_mode="conv", symbol_rate=rate, hop_key=None,
                label=f"{label} {index + 1}/{total} copy {copy_index + 1}/{copies}",
                timeout=duration + 30.0,
                channel_access=True, cancel_event=cancel_event,
            )
            completed += 1
            if progress_callback is not None:
                progress_callback(completed, total * copies, "frames")


def _chat_loop(handshake, cfg, peer_name="Peer", peer_username=None,
               reliable_text=None, *, mode="direct"):
    broadcast_mode = mode == "broadcast"
    if not broadcast_mode and (handshake is None or not handshake.established):
        raise RuntimeError("Direct chat requires an established authenticated session")

    trace = for_config(cfg)
    if trace.enabled:
        trace.emit("SESSION", "Chat diagnostics enabled",
                   mode="BROADCAST" if broadcast_mode else "DIRECT",
                   log_path=str(trace.log_path),
                   privacy="no message plaintext or cryptographic keys")

    messages = []
    username = get_username()
    peer_label = ("OPEN BROADCAST ROOM" if broadcast_mode else
                  f"{peer_name} ({peer_username})" if peer_username and peer_username != peer_name
                  else peer_name)
    messages_lock = threading.RLock()
    engine_ref = {"engine": None}
    partial_broadcast_messages = {}
    incoming_transfers = {}
    direct_outgoing = {}
    direct_cancel_events = {}
    broadcast_cancel_events = {}
    retry_stop = threading.Event()
    retry_thread = None
    disconnect_requested = False
    tx_jobs = queue.Queue(maxsize=64)
    tx_stop = threading.Event()
    tx_state_lock = threading.RLock()
    rx_state_lock = threading.RLock()
    tx_state = {"active": False, "label": "", "progress": "",
                "cancel_event": None, "entry": None, "cancelable": False}
    rx_state = {"active": False, "label": "", "progress": ""}
    ptt_state = {"recorder": None, "started": 0.0, "phase": "idle",
                 "cancel_event": None, "entry": None, "path": None,
                 "duration": 0.0, "lock": threading.RLock()}

    if broadcast_mode:
        codec = BroadcastCodec(max_packet_size=cfg.max_payload)
        reassembler = BroadcastReassembler(codec, max_file_size=8 * 1024 * 1024,
            max_partial_bytes=16 * 1024 * 1024, expiry_seconds=3600.0)
        retransmit_cache = BroadcastRetransmitCache(codec, max_messages=256,
            max_cached_bytes=64 * 1024 * 1024)
        reliable_text_channel = None
        reliable_binary_channel = None
    else:
        reliable_text_channel = reliable_text or ReliableTextChannel(handshake)
        reliable_binary_channel = ReliableBinaryChannel(handshake)
        codec = reassembler = retransmit_cache = None

    def append_message(speaker, body, status=""):
        with messages_lock:
            return add_chat_message(messages, speaker, body, status)

    def remember_broadcast_event(message_id, cancel_event):
        mid = bytes(message_id)
        with messages_lock:
            broadcast_cancel_events[mid] = cancel_event
            while len(broadcast_cancel_events) > 256:
                broadcast_cancel_events.pop(next(iter(broadcast_cancel_events)), None)

    def report_tx(label, current, total, unit="chunks"):
        with tx_state_lock:
            tx_state["active"] = True
            tx_state["label"] = str(label).upper()
            tx_state["progress"] = f"{int(current)}/{int(total)} {unit}"

    def report_rx(label, current, total, unit="chunks", active=True):
        with rx_state_lock:
            rx_state["active"] = bool(active)
            rx_state["label"] = str(label).upper()
            rx_state["progress"] = f"{int(current)}/{int(total)} {unit}"

    def cancel_operation(cancel_event, entry=None):
        if cancel_event is None or cancel_event.is_set():
            return False
        cancel_event.set()
        if entry is not None:
            with messages_lock:
                entry["status"] = "cancelling · stopping transmission"
        current = engine_ref.get("engine")
        if current is not None:
            current.cancel_transmission(cancel_event)
        return True

    def launch_worker(title, entry, operation, success_status, *,
                      cancel_event=None, cancelable=False, cleanup_path=None):
        control = {
            "cancel_event": cancel_event,
            "cancelable": bool(cancelable),
            "cleanup_path": Path(cleanup_path) if cleanup_path is not None else None,
        }
        if control["cancelable"] and cancel_event is not None:
            with messages_lock:
                entry["cancelable"] = True
                entry["cancel_action"] = (
                    lambda ev=cancel_event, item=entry: cancel_operation(ev, item)
                )
        try:
            tx_jobs.put_nowait((title, entry, operation, success_status, control))
        except queue.Full:
            with messages_lock:
                entry["status"] = "FAILED · outgoing queue is full"
                entry["cancelable"] = False
                entry.pop("cancel_action", None)
            if cancelable and cancel_event is not None:
                cancel_event.set()
            raise RuntimeError("Outgoing queue is full")
        return control

    def on_direct_resend_request(transfer_id, indexes):
        tid = bytes(transfer_id)
        with messages_lock:
            outgoing = direct_outgoing.get(tid)
            transfer_cancel_event = direct_cancel_events.get(tid)
        if transfer_cancel_event is not None and transfer_cancel_event.is_set():
            append_message("system", f"Ignored resend request for cancelled transfer {tid.hex()[:12]}",
                           "original sender cancelled this transfer")
            return
        if trace.enabled:
            trace.emit("DIRECT_RETRY", "Authenticated resend request received",
                       transfer_id=bytes(transfer_id).hex(),
                       requested_chunks=len(indexes),
                       requested_indexes=",".join(str(int(x)) for x in tuple(indexes)[:64]),
                       cache_hit=outgoing is not None)
        if outgoing is None:
            append_message("system", f"Peer requested unavailable transfer {bytes(transfer_id).hex()[:12]}",
                           "resend source is no longer cached")
            return
        filename = outgoing.manifest.filename
        entry = append_message("system", f"Peer requested {len(indexes)} chunk(s) of {filename}",
                               "resend queued")
        def operation():
            def progress(label, current, total, unit):
                report_tx(label, current, total, unit)
                with messages_lock:
                    entry["status"] = f"resending {current}/{total} requested chunks"
            sent = _engine_resend_chunks(reliable_binary_channel, engine_ref["engine"],
                                         outgoing, indexes, cfg, progress_callback=progress,
                                         cancel_event=transfer_cancel_event)
            if trace.enabled:
                trace.emit("DIRECT_RETRY", "Requested Direct chunks resent and authenticated ACKs received",
                           transfer_id=bytes(transfer_id).hex(), chunks_sent=sent)
        launch_worker("direct-resend", entry, operation,
                      "requested chunks resent · authenticated ACKs received",
                      cancel_event=transfer_cancel_event,
                      cancelable=transfer_cancel_event is not None)

    def queue_broadcast_resend(target_sender, target_mid, target_kind, indexes, resend_jobs):
        mid = bytes(target_mid)
        with messages_lock:
            transfer_cancel_event = broadcast_cancel_events.get(mid)
        if transfer_cancel_event is not None and transfer_cancel_event.is_set():
            append_message("system", f"Ignored Broadcast resend request for cancelled transfer {mid.hex()[:12]}",
                           "original sender cancelled this transfer")
            return
        if not resend_jobs:
            append_message("system", "Broadcast resend request received, but requested frames are not cached.",
                           "source cache miss or expired transfer")
            return
        entry = append_message(
            "system",
            f"Peer requested {len(resend_jobs)} Broadcast frame(s) for {bytes(target_mid).hex()[:12]}",
            "resend queued · Broadcast has no delivery ACK",
        )
        def operation():
            current = engine_ref["engine"]
            total = len(resend_jobs)
            for index, (frame, rate, fec_mode) in enumerate(resend_jobs, 1):
                if transfer_cancel_event is not None and transfer_cancel_event.is_set():
                    raise TransferCancelled("Original Broadcast transfer was cancelled")
                duration = packet_duration(
                    len(frame), current.config, fec_mode=fec_mode, symbol_rate=rate
                )
                current.send_packet(
                    frame, fec_mode=fec_mode, symbol_rate=rate, hop_key=None,
                    label=f"BROADCAST RETRY {index}/{total} @ {rate}",
                    timeout=duration + 30.0,
                    channel_access=True,
                    cancel_event=transfer_cancel_event,
                )
                report_tx("BROADCAST RESEND", index, total, "frames")
                with messages_lock:
                    entry["status"] = f"retransmitted {index}/{total} frames · no delivery receipt"
        launch_worker(
            "broadcast-resend", entry, operation,
            "requested Broadcast frames retransmitted · delivery remains unconfirmed",
            cancel_event=transfer_cancel_event,
            cancelable=transfer_cancel_event is not None,
        )

    def on_direct_integrity_failure(transfer_id, indexes):
        tid = bytes(transfer_id)
        requested = tuple(sorted(set(int(index) for index in indexes)))
        if not requested:
            return
        entry = append_message(
            "system",
            f"File integrity check failed for {tid.hex()[:12]}",
            f"requesting fresh ciphertext for {len(requested)} damaged chunk(s)",
        )
        def operation():
            maximum = max(1, (cfg.max_payload - AEAD_OVERHEAD - 22) // 4)
            batches = [requested[i:i + maximum] for i in range(0, len(requested), maximum)]
            for batch_index, batch in enumerate(batches, 1):
                request = reliable_binary_channel.make_resend_request_packet(
                    tid, batch, max_packet_size=cfg.max_payload
                )
                current = engine_ref["engine"]
                rate = current.symbol_rates[0]
                duration = packet_duration(
                    len(request), current.config, fec_mode="conv", symbol_rate=rate
                )
                current.send_packet(
                    request, fec_mode="conv", symbol_rate=rate,
                    label=f"FILE INTEGRITY REPAIR REQUEST {batch_index}/{len(batches)}",
                    timeout=duration + 30.0, channel_access=True,
                )
            with messages_lock:
                entry["status"] = (
                    f"repair request transmitted · {len(requested)} chunks · waiting for peer resend"
                )
        launch_worker(
            "file-integrity-repair", entry, operation,
            "integrity repair request sent · waiting for retransmitted chunks",
        )

    if broadcast_mode:
        packet_handler = _broadcast_packet_router(
            engine_ref, codec, reassembler, retransmit_cache,
            messages, messages_lock, partial_broadcast_messages, on_progress=report_rx,
            on_debug=trace.emit if trace.enabled else None,
            on_resend_frames=queue_broadcast_resend,
        )
    else:
        packet_handler = _direct_packet_router(
            engine_ref, reliable_text_channel, reliable_binary_channel,
            messages, messages_lock, incoming_transfers=incoming_transfers,
            on_resend_request=on_direct_resend_request,
            on_integrity_failure=on_direct_integrity_failure, on_progress=report_rx,
        )

    def on_engine_event(event):
        if trace.enabled:
            trace.emit("ENGINE_EVENT", "Acoustic session engine event",
                       kind=event.kind, message=event.message,
                       metadata=event.metadata)
        if event.kind in {"audio_error", "listener_error", "transmit_error", "protocol_error"}:
            append_message("system", event.message, event.kind.replace("_", " ").upper())

    def start_engine():
        engine = AudioSessionEngine(
            cfg,
            hop_key=BROADCAST_HOP_KEY if broadcast_mode else handshake.session.hopping_key,
            symbol_rates=_supported_symbol_rates(cfg),
            max_packet_bytes=cfg.max_payload,
            on_packet=packet_handler,
            on_event=on_engine_event,
            channel_access="broadcast" if broadcast_mode else True,
            on_debug=trace.emit if trace.enabled else None,
        )
        engine_ref["engine"] = engine
        engine.start()
        return engine

    def tx_worker():
        while not tx_stop.is_set():
            try:
                job = tx_jobs.get(timeout=0.2)
            except queue.Empty:
                continue
            control = None
            cancel_event = None
            entry = None
            try:
                if job is None:
                    return
                title, entry, operation, success_status, control = job
                cancel_event = control.get("cancel_event")
                with tx_state_lock:
                    tx_state.update(
                        active=True, label=title.upper(), progress="0/1 steps",
                        cancel_event=cancel_event, entry=entry,
                        cancelable=bool(control.get("cancelable") and cancel_event is not None),
                    )
                if cancel_event is not None:
                    with ptt_state["lock"]:
                        if ptt_state.get("cancel_event") is cancel_event:
                            ptt_state["phase"] = "sending"
                            ptt_state["entry"] = entry
                with messages_lock:
                    entry["status"] = f"sending · {title}"
                try:
                    if tx_stop.is_set():
                        raise RuntimeError("session is disconnecting")
                    _check_cancelled(cancel_event)
                    if control is not None:
                        control["started"] = True
                    # Successful completion is authoritative. A cancel
                    # arriving after the operation's final ACK/playback must not
                    # retroactively turn a completed delivery into "cancelled".
                    operation()
                    with messages_lock:
                        entry["status"] = success_status
                except Exception as exc:
                    cancelled = isinstance(exc, TransferCancelled) or (
                        cancel_event is not None and cancel_event.is_set())
                    with messages_lock:
                        if cancelled and control.get("cancelable") and control.get("started"):
                            entry["status"] = (
                                "CANCELLED · transmission stopped; partial data may remain at receivers"
                            )
                        elif cancelled and control.get("cancelable"):
                            entry["status"] = "CANCELLED · transmission was not sent"
                        elif cancelled:
                            entry["status"] = f"CANCELLED · {title}"
                        else:
                            entry["status"] = f"FAILED · {exc}"
                    if cancelled and control.get("cleanup_path") is not None:
                        try:
                            control["cleanup_path"].unlink(missing_ok=True)
                        except OSError:
                            pass
            finally:
                with tx_state_lock:
                    tx_state.update(active=False, label="", progress="",
                                    cancel_event=None, entry=None, cancelable=False)
                if cancel_event is not None:
                    with ptt_state["lock"]:
                        if ptt_state.get("cancel_event") is cancel_event:
                            ptt_state.update(recorder=None, started=0.0, phase="idle",
                                             cancel_event=None, entry=None, path=None, duration=0.0)
                if control is not None and control.get("cancelable") and entry is not None:
                    with messages_lock:
                        entry["cancelable"] = False
                        entry.pop("cancel_action", None)
                tx_jobs.task_done()


    tx_thread = threading.Thread(target=tx_worker, name="echocrypt-chat-tx-fifo", daemon=True)
    tx_thread.start()
    engine = start_engine()
    append_message("system", "Continuous acoustic listener started; incoming traffic is routed automatically.",
                   "no manual receive/ARM operation")
    if broadcast_mode:
        if trace.enabled:
            trace.emit("BROADCAST", "Open Broadcast diagnostics pipeline attached",
                       sender_id=codec.sender_id.hex(), max_packet_size=codec.max_packet_size,
                       max_fragment_payload=codec.max_fragment_payload,
                       retry_cache_messages=256, retry_cache_bytes=64 * 1024 * 1024)
        retry_thread = threading.Thread(
            target=_broadcast_retry_worker,
            args=(engine_ref, codec, reassembler, retry_stop,
                  trace.emit if trace.enabled else None),
            name="echocrypt-broadcast-retry", daemon=True,
        )
        retry_thread.start()
        append_message("system", "Open broadcast room: nearby compatible ECHO-CRYPT devices can join.",
                       "public room · no handshake · no ACK · no private group key")

    def activity_provider():
        with ptt_state["lock"]:
            recorder = ptt_state.get("recorder")
            phase = ptt_state.get("phase", "idle")
            cancelable_voice = phase != "idle" and ptt_state.get("cancel_event") is not None
            if recorder is not None:
                elapsed = max(0, int(time.monotonic() - ptt_state["started"]))
                return {"label": "VOICE RECORDING", "progress": f"{elapsed:02d}s / 60s",
                        "cancelable": True}
            if phase in {"preparing", "queued", "sending", "cancelling"}:
                duration = float(ptt_state.get("duration", 0.0))
                label = {"preparing": "VOICE ENCODING", "queued": "VOICE QUEUED",
                         "sending": "VOICE SENDING", "cancelling": "VOICE CANCELLING"}.get(phase, "VOICE")
                if phase == "sending":
                    with tx_state_lock:
                        tx_label = str(tx_state.get("label", ""))
                        tx_progress = str(tx_state.get("progress", ""))
                    if tx_progress:
                        return {"label": label, "progress": tx_progress,
                                "detail": tx_label, "cancelable": cancelable_voice}
                return {"label": label, "progress": f"{duration:.1f}s note",
                        "cancelable": cancelable_voice}
        with tx_state_lock:
            tx_active = bool(tx_state["active"])
            tx_label = str(tx_state["label"])
            tx_progress = str(tx_state["progress"])
        with rx_state_lock:
            rx_active = bool(rx_state["active"])
            rx_label = str(rx_state["label"])
            rx_progress = str(rx_state["progress"])
        if tx_active and rx_active:
            with tx_state_lock:
                can_cancel = bool(tx_state.get("cancelable"))
            return {"label": f"TX {tx_label}", "progress": tx_progress,
                    "detail": f"RX {rx_label} {rx_progress}",
                    "cancelable": can_cancel}
        if tx_active:
            with tx_state_lock:
                can_cancel = bool(tx_state.get("cancelable"))
            return {"label": f"TX {tx_label}", "progress": tx_progress,
                    "cancelable": can_cancel}
        if rx_active:
            return {"label": f"RX {rx_label}", "progress": rx_progress}
        current = engine_ref.get("engine")
        listener = getattr(current, "listener", None) if current is not None else None
        if bool(getattr(listener, "busy", False)):
            return {"label": "DEMODULATING AUDIO", "progress": "frame detected"}
        if current is not None and current.running:
            return {"label": "LISTENING", "progress": "background receiver active"}
        return {"label": "DISCONNECTED", "progress": ""}

    def stop_tx_worker():
        tx_stop.set()
        with tx_state_lock:
            active_event = tx_state.get("cancel_event")
        if active_event is not None:
            active_event.set()
        if tx_thread.is_alive():
            tx_thread.join(timeout=3.0)
        while True:
            try:
                pending = tx_jobs.get_nowait()
            except queue.Empty:
                break
            try:
                if pending is not None:
                    _title, pending_entry, _operation, _success, control = pending
                    if control.get("cancel_event") is not None:
                        control["cancel_event"].set()
                    with messages_lock:
                        pending_entry["status"] = "CANCELLED · session disconnected"
            finally:
                tx_jobs.task_done()

    def send_text(message):
        entry = append_message("me", message, "queued · cancellable")
        cancel_event = threading.Event()
        if broadcast_mode:
            def operation():
                _check_cancelled(cancel_event)
                frames = codec.encode_text(message)
                mid = codec.decode(frames[0]).message_id
                remember_broadcast_event(mid, cancel_event)
                retransmit_cache.remember(mid, BroadcastType.TEXT, frames)
                broadcast_rate = _reliable_chat_symbol_rate_plan(cfg)[0]
                if trace.enabled:
                    trace.emit("BROADCAST_TX", "Broadcast text encoded and cached",
                               message_id=mid.hex(), payload_bytes=len(message.encode("utf-8")),
                               fragments=len(frames), copies=3, symbol_rate=broadcast_rate,
                               fragment_payload_limit=codec.max_fragment_payload)
                report_tx("BROADCAST TEXT", 0, len(frames) * 3, "frames")
                try:
                    _broadcast_send_frames(engine_ref["engine"], frames, "BROADCAST TEXT", copies=3,
                        symbol_rate=broadcast_rate,
                        progress_callback=lambda current, total, unit: report_tx("BROADCAST TEXT", current, total, unit),
                        cancel_event=cancel_event)
                finally:
                    if cancel_event.is_set():
                        retransmit_cache.forget(mid, BroadcastType.TEXT)
                if trace.enabled:
                    trace.emit("BROADCAST_TX", "Broadcast text transmission sequence completed",
                               message_id=mid.hex(), fragments=len(frames), copies=3)
            launch_worker("broadcast-text", entry, operation,
                          "broadcast on air · delivery is not acknowledged",
                          cancel_event=cancel_event, cancelable=True)
        else:
            def operation():
                _engine_send_text(reliable_text_channel, engine_ref["engine"],
                                  message, cfg,
                                  progress_callback=lambda label, current, total, unit: report_tx(label, current, total, unit),
                                  cancel_event=cancel_event)
            launch_worker("direct-text", entry, operation,
                          "delivered · authenticated ACK",
                          cancel_event=cancel_event, cancelable=True)

    def send_file(path, description="attachment", *, cancel_event=None,
                  cancelable=True, cleanup_on_cancel=False, voice_duration=None,
                  resume_id_override=None, preferred_symbol_rate=None):
        if cancel_event is None:
            cancel_event = threading.Event()
        cancelable = bool(cancelable)
        size_bytes = path.stat().st_size
        if size_bytes > LARGE_FILE_WARNING_BYTES:
            append_message("system",
                f"File-size warning: {path.name} is {size_bytes:,} bytes "
                f"({size_bytes * 8:,} bits; {size_bytes / (1024 * 1024):.2f} MiB). "
                "It exceeds the 1000-bit (125-byte) warning threshold. "
                "Acoustic transfer may take a long time; keep both devices listening.",
                "large attachment · > 1000 bits")
        if broadcast_mode:
            if size_bytes > 8 * 1024 * 1024:
                raise ValueError("Broadcast file limit is 8 MiB; use Direct for larger files")
            if description == "voice note" and voice_duration is not None:
                entry_body = f"Voice message · {voice_duration:.1f}s · {size_bytes:,} bytes"
            else:
                entry_body = f"{description}: {path.name}"
            entry = append_message("me", entry_body, "preparing broadcast")
            if description == "voice note":
                with messages_lock:
                    entry["voice_path"] = str(path)
            def operation():
                _check_cancelled(cancel_event)
                engine = engine_ref["engine"]
                voice_transfer = description == "voice note"
                broadcast_rate = _reliable_chat_symbol_rate_plan(engine.config)[0]
                # No-ACK Broadcast uses a conservative rate for every frame,
                # including voice, so receivers have a realistic chance to decode it.
                chunk_size = 192 if voice_transfer else 512
                mid, frames = codec.encode_file(
                    path, chunk_size=chunk_size,
                    symbol_rate=broadcast_rate, fec_mode="conv", hopping=True,
                    cancel_event=cancel_event,
                )
                _check_cancelled(cancel_event)
                remember_broadcast_event(mid, cancel_event)
                retransmit_cache.remember(mid, BroadcastType.FILE_META, frames[:1])
                chunk_frames = frames[1:]
                if chunk_frames:
                    retransmit_cache.remember(mid, BroadcastType.FILE_CHUNK, chunk_frames)
                if trace.enabled:
                    trace.emit("BROADCAST_TX", "Broadcast file encoded and resend cache populated",
                               message_id=mid.hex(), filename=path.name,
                               file_bytes=size_bytes, total_frames=len(frames),
                               chunk_frames=len(chunk_frames), manifest_frames=1,
                               symbol_rate=broadcast_rate)
                manifest_label = "BROADCAST VOICE NOTE MANIFEST" if voice_transfer else "BROADCAST FILE MANIFEST"
                chunks_label = "BROADCAST VOICE NOTE" if voice_transfer else f"BROADCAST FILE {path.name}"
                try:
                    report_tx(manifest_label, 0, 1, "frames")
                    _broadcast_send_frames(engine_ref["engine"], frames[:1],
                        manifest_label, copies=1, symbol_rate=broadcast_rate,
                        progress_callback=lambda current, total, unit: report_tx(manifest_label, current, total, unit),
                        cancel_event=cancel_event)
                    if frames[1:]:
                        _check_cancelled(cancel_event)
                        report_tx(chunks_label, 0, len(chunk_frames), "chunks")
                        _broadcast_send_frames(engine_ref["engine"], chunk_frames,
                            chunks_label, copies=1,
                            progress_callback=lambda current, total, unit: report_tx(chunks_label, current, total, "chunks"),
                            cancel_event=cancel_event, symbol_rate=broadcast_rate)

                finally:
                    if cancel_event.is_set():
                        retransmit_cache.forget(mid)
                if trace.enabled:
                    trace.emit("BROADCAST_TX", "Broadcast file transmission sequence completed",
                               message_id=mid.hex(), filename=path.name,
                               total_frames=len(frames), file_bytes=size_bytes)
            if cancelable:
                with messages_lock:
                    entry["status"] = "queued · cancellable"
                    entry["cancelable"] = True
                    entry["cancel_action"] = lambda ev=cancel_event, item=entry: cancel_operation(ev, item)
                with ptt_state["lock"]:
                    if ptt_state.get("cancel_event") is cancel_event:
                        ptt_state.update(phase="queued", entry=entry, path=path)
            launch_worker("broadcast-file", entry, operation,
                          "broadcast on air · no delivery receipt",
                          cancel_event=cancel_event, cancelable=cancelable,
                          cleanup_path=path if cleanup_on_cancel else None)
        else:
            if description == "voice note" and voice_duration is not None:
                entry = append_message("me", f"Voice message · {voice_duration:.1f}s · {size_bytes:,} bytes",
                                       "queued · cancellable" if cancelable else "sending · chunked transfer")
                with messages_lock:
                    entry["voice_path"] = str(path)
            else:
                entry = append_message("me", f"{description}: {path.name}", "sending · chunked transfer")
            def remember_outgoing(outgoing):
                with messages_lock:
                    tid = outgoing.manifest.transfer_id
                    direct_outgoing[tid] = outgoing
                    direct_cancel_events[tid] = cancel_event
                    while len(direct_outgoing) > 64:
                        oldest = next(iter(direct_outgoing))
                        direct_outgoing.pop(oldest, None)
                        direct_cancel_events.pop(oldest, None)
            def operation():
                manifest = _engine_send_file(reliable_binary_channel,
                    engine_ref["engine"], path, cfg,
                    progress_callback=lambda label, current, total, unit: report_tx(
                        ("VOICE NOTE MANIFEST" if "manifest" in label.casefold() else "VOICE NOTE")
                        if description == "voice note" else label,
                        current, total, unit),
                    on_outgoing_created=remember_outgoing,
                    cancel_event=cancel_event,
                    transfer_id=resume_id_override,
                    preferred_symbol_rate=preferred_symbol_rate)
                if description != "voice note":
                    with messages_lock:
                        entry["body"] = f"{description}: {manifest.filename} ({manifest.file_size:,} bytes)"
            if cancelable:
                with messages_lock:
                    entry["cancelable"] = True
                    entry["cancel_action"] = lambda ev=cancel_event, item=entry: cancel_operation(ev, item)
                with ptt_state["lock"]:
                    if ptt_state.get("cancel_event") is cancel_event:
                        ptt_state.update(phase="queued", entry=entry, path=path)
            launch_worker("direct-file", entry, operation,
                          "delivered · all chunks ACKed · SHA-256 verified",
                          cancel_event=cancel_event, cancelable=cancelable,
                          cleanup_path=path if cleanup_on_cancel else None)
        return entry

    def request_resend(entry):
        with messages_lock:
            raw_request_info = entry.get("retry_request")
            request_info = dict(raw_request_info) if isinstance(raw_request_info, dict) else None
            request_count = int(entry.get("retry_count", 0)) + 1
            entry["retry_count"] = request_count
        if not request_info:
            raise RuntimeError("This message has no missing data to request")
        missing = tuple(request_info.get("missing", ()))
        if not missing:
            raise RuntimeError("There are no missing chunks to request")
        if request_info.get("mode") == "direct":
            tid = bytes(request_info["transfer_id"])
            maximum = max(1, (cfg.max_payload - AEAD_OVERHEAD - 22) // 4)
            batches = [missing[i:i + maximum] for i in range(0, len(missing), maximum)]
            def operation():
                report_tx("DIRECT RESEND REQUEST", 0, len(batches), "requests")
                for batch_index, batch in enumerate(batches, 1):
                    request_packet = reliable_binary_channel.make_resend_request_packet(
                        tid, batch, max_packet_size=cfg.max_payload)
                    future = engine_ref["engine"].submit_packet(
                        request_packet, fec_mode="conv",
                        label=f"DIRECT RESEND REQUEST #{request_count} {batch_index}/{len(batches)}")
                    future.result(timeout=60.0)
                    report_tx("DIRECT RESEND REQUEST", batch_index, len(batches), "requests")
                with messages_lock:
                    entry["status"] = f"resend request #{request_count} sent · {len(missing)} missing chunks"
        else:
            target_sender = bytes(request_info["sender_id"])
            target_mid = bytes(request_info["message_id"])
            retry_kind = BroadcastType(request_info["kind"])
            # Broadcast requests are bounded to one short control frame each.
            # Split very large missing sets so repeated requests cannot truncate
            # or permanently starve higher fragment indexes.
            maximum = min(96, max(1, (codec.max_fragment_payload - RETRY_REQUEST_V2.size) // 2))
            batches = [missing[i:i + maximum] for i in range(0, len(missing), maximum)]
            def operation():
                if trace.enabled:
                    trace.emit("BROADCAST_RETRY", "Manual selective resend requested by chat user",
                               request_number=request_count,
                               target_sender=target_sender.hex()[:12],
                               message_id=target_mid.hex(), kind=int(retry_kind),
                               missing_chunks=len(missing),
                               requested_indexes=",".join(str(int(x)) for x in missing[:64]))
                report_tx("BROADCAST RESEND REQUEST", 0, len(batches), "requests")
                for batch_index, batch in enumerate(batches, 1):
                    request_packet = codec.encode_retry_request(
                        target_sender, target_mid, retry_kind, batch,
                        request_flags=int(request_info.get(
                            "request_flags",
                            RETRY_NEED_HEADER if retry_kind == BroadcastType.FILE_META
                            else RETRY_NEED_HEADER | RETRY_NEED_DATA,
                        )),
                    )
                    future = engine_ref["engine"].submit_packet(
                        request_packet, fec_mode="conv", symbol_rate=engine_ref["engine"].symbol_rates[0],
                        hop_key=None, label=f"BROADCAST RESEND REQUEST {batch_index}/{len(batches)}")
                    future.result(timeout=60.0)
                    if trace.enabled:
                        trace.emit("BROADCAST_RETRY", "Manual resend request transmitted",
                                   request_number=request_count, batch=batch_index,
                                   batch_count=len(batches), requested_chunks=len(batch))
                    report_tx("BROADCAST RESEND REQUEST", batch_index, len(batches), "requests")
                with messages_lock:
                    entry["status"] = (
                        f"resend request #{request_count} transmitted · {len(missing)} chunks requested "
                        "· sender response cannot be confirmed in Broadcast"
                    )
        launch_worker("resend-request", entry, operation,
                      f"resend request #{request_count} transmitted")

    def start_ptt_recording():
        current = engine_ref.get("engine")
        if current is None or not current.running:
            raise RuntimeError("Audio session is not active")
        with ptt_state["lock"]:
            if ptt_state.get("phase") != "idle":
                raise RuntimeError("A voice operation is already active")
            current.listener.pause()
            try:
                recorder = MicrophoneRecorder(cfg.sample_rate, cfg.input_device,
                                               blocksize=1024, max_seconds=60.0).start()
            except Exception:
                current.listener.resume()
                raise
            ptt_state.update(recorder=recorder, started=time.monotonic(),
                             phase="recording", cancel_event=threading.Event(),
                             entry=None, path=None, duration=0.0)

    def stop_ptt_recording(*, discard=False):
        current = engine_ref.get("engine")
        with ptt_state["lock"]:
            recorder = ptt_state.get("recorder")
            cancel_event = ptt_state.get("cancel_event")
            ptt_state["recorder"] = None
            if discard and cancel_event is not None:
                cancel_event.set()
            ptt_state["phase"] = "cancelling" if discard else "preparing"
            ptt_state["started"] = 0.0
        if recorder is None:
            if discard:
                with ptt_state["lock"]:
                    ptt_state.update(phase="idle", cancel_event=None, entry=None,
                                     path=None, duration=0.0)
            return
        try:
            samples = recorder.stop()
        finally:
            if current is not None and current.running:
                current.listener.resume()
        if discard or (cancel_event is not None and cancel_event.is_set()):
            with ptt_state["lock"]:
                ptt_state.update(phase="idle", cancel_event=None, entry=None,
                                 path=None, duration=0.0)
            return

        duration = len(samples) / float(cfg.sample_rate)
        try:
            path = _write_voice_file(cfg, samples)
            if cancel_event is not None and cancel_event.is_set():
                path.unlink(missing_ok=True)
                with ptt_state["lock"]:
                    ptt_state.update(phase="idle", cancel_event=None, entry=None,
                                     path=None, duration=0.0)
                return
            size_bytes = path.stat().st_size
            entry = send_file(path, "voice note", cancel_event=cancel_event,
                              cancelable=True, cleanup_on_cancel=True,
                              voice_duration=duration)
            with ptt_state["lock"]:
                if ptt_state.get("cancel_event") is cancel_event:
                    ptt_state["duration"] = duration
        except Exception:
            if cancel_event is not None and cancel_event.is_set():
                try:
                    if 'path' in locals():
                        path.unlink(missing_ok=True)
                except OSError:
                    pass
                with ptt_state["lock"]:
                    ptt_state.update(phase="idle", cancel_event=None, entry=None,
                                     path=None, duration=0.0)
                return
            if 'path' in locals():
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass
            with ptt_state["lock"]:
                ptt_state.update(phase="idle", cancel_event=None, entry=None,
                                 path=None, duration=0.0)
            raise

    def cancel_active_operation():
        with ptt_state["lock"]:
            phase = ptt_state.get("phase", "idle")
            voice_event = ptt_state.get("cancel_event")
            voice_entry = ptt_state.get("entry")
            recorder_active = ptt_state.get("recorder") is not None
        if phase != "idle" and voice_event is not None:
            if recorder_active:
                # Discard an active recording instead of accidentally sending it.
                stop_ptt_recording(discard=True)
                return True
            return cancel_operation(voice_event, voice_entry)

        with tx_state_lock:
            active_event = tx_state.get("cancel_event")
            active_entry = tx_state.get("entry")
        if active_event is not None:
            return cancel_operation(active_event, active_entry)
        # F7 also works while a cancellable send is still waiting in the
        # application FIFO and has not yet become the engine's active job.
        with messages_lock:
            queued_action = next(
                (item.get("cancel_action") for item in reversed(messages)
                 if item.get("cancelable") and callable(item.get("cancel_action"))),
                None,
            )
        if queued_action is not None:
            return bool(queued_action())
        return False


    try:
        while engine_ref["engine"] is not None and engine_ref["engine"].running:
            def play_voice_entry(entry):
                with messages_lock:
                    path_value = entry.get("voice_path")
                if not path_value:
                    raise RuntimeError("Voice file is not available on this device")
                # Return the generated WAV path to ChatScreen. The UI updates
                # entry state on its own event loop, never from this worker.
                return _play_voice_file(Path(path_value), cfg)

            action, payload = ChatScreen(
                peer_label, username, messages, on_send=send_text,
                activity_provider=activity_provider, on_retry=request_resend,
                on_play_voice=play_voice_entry,
                messages_lock=messages_lock,
                on_ptt_start=start_ptt_recording,
                on_ptt_stop=lambda: stop_ptt_recording(discard=False),
                on_ptt_cancel=lambda: stop_ptt_recording(discard=True),
                on_cancel=cancel_active_operation,
            ).run() or ("disconnect", None)
            engine = engine_ref["engine"]
            if action == "disconnect":
                disconnect_requested = True
                retry_stop.set()
                # Mark the session stopped immediately, but let PortAudio and
                # worker-thread cleanup finish off the UI thread. ESC must not
                # wait for stream shutdown or thread joins before returning.
                engine.stop(wait=False)
                render_app_header(session_status="BROADCAST CLOSED" if broadcast_mode else "NOT CONNECTED")
                print("\nBroadcast room closed." if broadcast_mode else
                      "\nSession closed. Audio cleanup is finishing in the background.")
                # ESC is a direct navigation action; do not add an extra
                # Enter-to-continue pause after leaving the chat.
                return

            if action == "send":
                message = str(payload or "").strip()
                if message:
                    send_text(message)
                continue

            if action == "attach":
                raw = prompt("File path (any file type): ").strip().strip('"')
                if not raw:
                    continue
                path = Path(raw).expanduser()
                if not path.is_file():
                    append_message("system", f"File not found: {path}")
                    continue
                try:
                    send_file(path)
                except Exception as exc:
                    append_message("system", f"Attachment rejected: {exc}")
                continue

            if action == "voice":
                append_message("system", "Use F4 to toggle voice recording or hold Space / the voice button while speaking.")
                continue

            if action == "emergency":
                if broadcast_mode:
                    append_message("system", "Emergency PTT is a legacy Direct-only mode. Use Space for a voice note in this room.")
                    continue
                engine.stop()
                choice = _emergency_choice()
                try:
                    if choice == "transmit":
                        _send_emergency_voice_stream(reliable_binary_channel, cfg)
                        append_message("system", "Emergency PTT transmission finished.")
                    elif choice == "receive":
                        _receive_file(reliable_binary_channel, cfg, handshake.session.hopping_key,
                                      cfg.emergency_symbol_rate, title="Receive emergency voice",
                                      continuous=True, allowed_extensions=AUDIO_FILE_EXTENSIONS,
                                      media_label="emergency voice burst")
                        append_message("system", "Emergency PTT receive operation finished.")
                except Exception as exc:
                    append_message("system", f"Emergency PTT failed: {exc}")
                engine = start_engine()
                continue

            if action == "more":
                if broadcast_mode:
                    append_message("system", "Broadcast uses one shared robust profile. Use F2 to send any file or F4/Space for voice notes.")
                    continue
                choice = _more_choice()
                if choice == "peer":
                    append_message("system", f"Peer fingerprint: {handshake.peer_fingerprint}")
                    continue
                if choice in {"resume", "send_fast"}:
                    try:
                        if choice == "resume":
                            raw = prompt("Original file path: ").strip().strip('"')
                            transfer_id = prompt("Transfer ID: ").strip()
                            if raw and transfer_id:
                                source = Path(raw).expanduser()
                                send_file(source, "attachment",
                                          resume_id_override=transfer_id)
                        else:
                            raw = prompt("Path to file (fast 800 baud): ").strip().strip('"')
                            if raw:
                                source = Path(raw).expanduser()
                                if not source.is_file():
                                    raise FileNotFoundError(source)
                                send_file(source, "attachment",
                                          preferred_symbol_rate=cfg.fast_symbol_rate)
                    except Exception as exc:
                        append_message("system", f"Experimental operation failed: {exc}")
                    continue

                if choice == "send_hybrid":
                    try:
                        raw = prompt("Path to file or ECV voice clip: ").strip().strip('"')
                        if not raw:
                            continue
                        source = Path(raw).expanduser()
                        if not source.is_file():
                            raise FileNotFoundError(source)
                        message = prompt("Text to transmit simultaneously on the second FDM band: ")
                        if not message:
                            raise ValueError("Hybrid FDM requires a non-empty text lane.")
                        transfer_id = prompt("Resume transfer ID (blank = new transfer): ").strip()
                        cancel_event = threading.Event()
                        entry = append_message(
                            "me", f"FDM: {source.name} + text",
                            "queued · cancellable · experimental dual-band transfer",
                        )
                        def hybrid_operation():
                            previous = engine_ref["engine"]
                            previous.stop(wait=True)
                            try:
                                _send_hybrid_file(
                                    reliable_binary_channel, reliable_text_channel, cfg,
                                    handshake.session.hopping_key, source_override=source,
                                    text_override=message, resume_id_override=transfer_id,
                                    cancel_event=cancel_event,
                                )
                            finally:
                                if not tx_stop.is_set() and not disconnect_requested:
                                    engine_ref["engine"] = start_engine()
                        launch_worker(
                            "hybrid-text-media", entry, hybrid_operation,
                            "FDM text and file transfer completed · dual ACKs verified",
                            cancel_event=cancel_event, cancelable=True,
                        )
                    except Exception as exc:
                        append_message("system", f"Hybrid transfer rejected: {exc}")
                    continue

                if choice in {"receive_hybrid", "receive_fast"}:
                    engine.stop()
                    try:
                        action_map = {"receive_fast": "receive_file_fast"}
                        _run_session_action(action_map.get(choice, choice), reliable_text_channel,
                                            reliable_binary_channel, cfg,
                                            handshake.session.hopping_key, handshake)
                    except Exception as exc:
                        append_message("system", f"Experimental operation failed: {exc}")
                    engine = start_engine()
                    continue
    finally:
        retry_stop.set()
        if disconnect_requested:
            # All potentially blocking shutdown work is deliberately detached
            # from the chat's key-handling/UI thread. The engine's stop(wait=False)
            # already cleared its running flag; wait=True here joins only inside
            # this dedicated cleanup worker.
            def finish_disconnect_cleanup():
                try:
                    stop_ptt_recording(discard=True)
                except Exception:
                    pass
                current = engine_ref.get("engine")
                if current is not None:
                    current.stop(wait=True)
                stop_tx_worker()
                if retry_thread is not None and retry_thread.is_alive():
                    retry_thread.join(timeout=3.0)
                if handshake is not None and handshake.established:
                    try:
                        handshake.close()
                    except Exception:
                        pass
            threading.Thread(
                target=finish_disconnect_cleanup,
                name="echocrypt-disconnect-cleanup", daemon=True,
            ).start()
        else:
            try:
                stop_ptt_recording(discard=True)
            except Exception:
                pass
            current = engine_ref.get("engine")
            if current is not None and current.running:
                current.stop()
            stop_tx_worker()
            if retry_thread is not None and retry_thread.is_alive():
                retry_thread.join(timeout=2.0)


def _add_peer_flow():
    render_app_header(session_status="ADD SAVED PEER")
    print("\nImport the OTHER device's exported identity.public.json. Never import a private key.")
    raw_path = prompt("Public identity JSON path: ").strip().strip('"')
    if not raw_path:
        return
    _peer_raw, fingerprint = load_public(raw_path)
    print("\nFingerprint:", fingerprint)
    print("Compare this full fingerprint with the other user over an independent trusted channel.")
    if prompt("Type TRUST to pin this identity: ").strip() != "TRUST":
        print("Peer was not added; fingerprint was not confirmed.")
        prompt("Press Enter to continue...")
        return
    suggested = Path(raw_path).expanduser().stem[:24]
    name = prompt(f"Display name [{suggested}]: ").strip() or suggested
    contact = add_contact(raw_path, name, confirmed_fingerprint=fingerprint)
    print(f"Saved peer: {contact.name}  [{contact.short_fingerprint}]")
    print("The public key is pinned locally. The private key was not copied.")
    prompt("Press Enter to return to Connections...")


def _connect_to_contact(contact: Contact):
    if not PRIVATE.exists():
        render_app_header(session_status="LOCAL IDENTITY REQUIRED")
        print("Create your local identity first. Choose 'Create local identity' in Connections.")
        prompt("Press Enter to return...")
        return
    order = Menu(f"CONNECT TO {contact.name.upper()}", [
        ("initiator", "Transmit first — I start the acoustic handshake"),
        ("responder", "Listen first — wait for the other device"),
        ("back", "Back to Connections"),
    ]).run()
    if order in (None, "back"):
        return
    try:
        peer_public_raw, peer_fingerprint = load_public(contact.identity_path)
        if peer_fingerprint != contact.fingerprint:
            raise ValueError("Saved peer identity fingerprint changed; remove and re-add this peer.")
        cfg = load()
        handshake = PeerHandshake(order, load_private(PRIVATE), peer_public_raw, peer_fingerprint)
        render_app_header(session_status=f"HANDSHAKE: {contact.name}")
        print(f"\nConnecting acoustically to {contact.name} ({contact.short_fingerprint})")
        print("No server, internet, sockets or direct network connection are used.")
        print("Both users must choose opposite handshake roles and be ready at the same time.")
        _establish(order, handshake, cfg)
        print("\nExchanging encrypted display names...")
        text_channel = ReliableTextChannel(handshake)
        peer_username = _exchange_session_usernames(order, handshake, cfg, text_channel)
        _chat_loop(handshake, cfg, contact.name, peer_username, text_channel)
    except KeyboardInterrupt:
        render_app_header(session_status="CONNECTION CANCELLED")
        print("\nConnection attempt cancelled.")
        prompt("Press Enter to return to Connections...")
    except Exception as exc:
        render_app_header(session_status="CONNECTION FAILED")
        print("\nCONNECTION FAILED:", exc)
        print("Check that the other device is listening, audio defaults are correct, and both modem profiles match.")
        prompt("Press Enter to return to Connections...")


def _connection_menu_items(contacts, local_identity_exists, legacy_peer_available=False):
    items = []
    # First-run order: create this device's identity before exchanging public keys.
    if not local_identity_exists:
        items.append(("create_identity", "◇ Create this device's identity"))
    items.extend(
        (f"peer:{contact.fingerprint}",
         f"● {contact.name}   ·   {contact.short_fingerprint}")
        for contact in contacts
    )
    if legacy_peer_available and not contacts:
        items.append(("import_legacy", "⇧ Import existing keys/peer.public.json"))
    items.append(("add", "＋ Add a peer public key"))
    if local_identity_exists:
        items.append(("export_identity", "⇧ Export my public identity"))
    if contacts:
        items.append(("remove", "− Remove a saved peer"))
    items.append(("broadcast", "◉ Join open Broadcast room (no handshake)"))
    items.append(("back", "Back to main menu"))
    return items


def _import_legacy_peer_flow():
    legacy_path = Path("keys/peer.public.json")
    if not legacy_path.is_file():
        raise FileNotFoundError("No existing keys/peer.public.json was found.")
    render_app_header(session_status="IMPORT LEGACY PEER KEY")
    _peer_raw, peer_fingerprint = load_public(legacy_path)
    print("Existing Stage 6 peer public key found.")
    print("Fingerprint:", peer_fingerprint)
    print("Compare the full fingerprint with the other user over an independent trusted channel.")
    if prompt("Type TRUST to pin this key in Connections: ").strip() != "TRUST":
        print("Import cancelled; the key was not trusted or saved.")
        prompt("Press Enter to return to Connections...")
        return
    name = prompt("Display name [Peer]: ").strip() or "Peer"
    contact = add_contact(legacy_path, name, confirmed_fingerprint=peer_fingerprint)
    print(f"Imported saved peer: {contact.name}  [{contact.short_fingerprint}]")
    prompt("Press Enter to return to Connections...")


def _open_broadcast_room():
    cfg = load()
    render_app_header(session_status="OPEN BROADCAST")
    print("\nJoining the open acoustic Broadcast room.")
    print("No saved peer, identity handshake, private key or server is required.")
    print("Any compatible ECHO-CRYPT instance within microphone range may receive and send.")
    print("Broadcast has no message-level ACK; delivery cannot be guaranteed.")
    _chat_loop(None, cfg, "Open Broadcast", None, mode="broadcast")


def connections_menu():
    while True:
        try:
            contacts = list_contacts()
        except Exception as exc:
            render_app_header(session_status="CONNECTIONS DATABASE ERROR")
            print("Saved peers database could not be read:", exc)
            prompt("Press Enter to return to the main menu...")
            return
        legacy_peer = Path("keys/peer.public.json").is_file() and not contacts
        items = _connection_menu_items(contacts, PRIVATE.exists(), legacy_peer_available=legacy_peer)
        title = ("CONNECTIONS  /  SAVED PEERS" if contacts
                 else "CONNECTIONS  /  FIRST SETUP: CREATE IDENTITY, THEN ADD A PEER KEY")
        choice = Menu(title, items).run()
        if choice in (None, "back"):
            return
        if choice == "broadcast":
            try:
                _open_broadcast_room()
            except Exception as exc:
                render_app_header(session_status="BROADCAST FAILED")
                print("Could not start Broadcast:", exc)
                prompt("Press Enter to return to Connections...")
        elif choice == "add":
            try:
                _add_peer_flow()
            except Exception as exc:
                render_app_header(session_status="ADD PEER FAILED")
                print("Could not add peer:", exc)
                prompt("Press Enter to return to Connections...")
        elif choice == "import_legacy":
            try:
                _import_legacy_peer_flow()
            except Exception as exc:
                render_app_header(session_status="LEGACY IMPORT FAILED")
                print("Could not import existing peer key:", exc)
                prompt("Press Enter to return to Connections...")
        elif choice == "create_identity":
            render_app_header(session_status="CREATE LOCAL IDENTITY")
            try:
                fingerprint = create_identity()
                print("Local identity created. Fingerprint:", fingerprint)
                print("Next: choose 'Export my public identity' in Connections and share that public JSON.")
            except Exception as exc:
                print("Identity creation failed:", exc)
            prompt("Press Enter to return to Connections...")
        elif choice == "export_identity":
            render_app_header(session_status="EXPORT PUBLIC IDENTITY")
            try:
                fingerprint = export_public(load_private(PRIVATE), PUBLIC)
                print("Public identity exported: keys/identity.public.json")
                print("Fingerprint:", fingerprint)
                print("Share this file only. Never share identity.ed25519.")
            except Exception as exc:
                print("Public identity export failed:", exc)
            prompt("Press Enter to return to Connections...")
        elif choice == "remove":
            removable = [(c.fingerprint, f"{c.name}  ·  {c.short_fingerprint}") for c in contacts]
            removable.append(("back", "Cancel"))
            selected = Menu("REMOVE SAVED PEER", removable).run()
            if selected not in (None, "back"):
                target = next((c for c in contacts if c.fingerprint == selected), None)
                if target:
                    render_app_header(session_status="REMOVE SAVED PEER")
                if target and prompt(f"Remove {target.name}? Type REMOVE to confirm: ").strip() == "REMOVE":
                    remove_contact(selected)
                    print("Pinned peer removed. This does not affect the other device.")
                    prompt("Press Enter to return to Connections...")
        elif choice.startswith("peer:"):
            fingerprint = choice.split(":", 1)[1]
            contact = next((c for c in contacts if c.fingerprint == fingerprint), None)
            if contact:
                _connect_to_contact(contact)


def start_communication():
    connections_menu()
