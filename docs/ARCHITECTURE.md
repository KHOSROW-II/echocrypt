# ECHO-CRYPT Architecture

## 1. Purpose and boundaries

ECHO-CRYPT is a client-side acoustic data transport. A device encodes application frames as audio and emits them through its speaker; another compatible client captures microphone audio, detects frames, decodes them, validates them, and dispatches the payload.

The transport does not depend on IP networking, a relay, a broker, or a central server. The acoustic environment is the channel. This does not make the channel private: confidentiality and peer authentication are provided only by the Direct security protocol.

The current implementation is an experimental Python application and SDK. Automated tests and synthetic loopback are not substitutes for validation between physical devices.

## 2. Layered data path

### Transmit path

1. **Application/UI or SDK** creates a text, binary, voice, or control payload.
2. **Session/protocol layer** selects Direct or Broadcast semantics.
3. **Security layer** encrypts and authenticates Direct payloads. Broadcast intentionally remains public and unauthenticated.
4. **Transport layer** fragments payloads, tracks transfer identifiers, schedules acknowledgements/retries where the mode supports them, and suppresses duplicate work.
5. **Modem/framing layer** adds synchronization, wire-format headers, integrity fields, and forward-error-correction data.
6. **Signal layer** modulates symbols into a waveform and queues playback through the selected audio output.

### Receive path

1. **Audio capture** continuously supplies microphone samples to the receive pipeline.
2. **Synchronization and candidate search** locate possible preambles and frame boundaries.
3. **Demodulation and recovery** estimate symbols and attempt bounded timing/frequency recovery.
4. **FEC and integrity checks** correct recoverable errors and reject frames that fail protected-header or payload integrity checks.
5. **Protocol dispatch** routes validated frames to Direct, Broadcast, transfer, voice, or control handlers.
6. **Security and application delivery** authenticate/decrypt Direct traffic before exposing plaintext to the application.
7. **Reliability feedback** sends authenticated acknowledgements or selective repair requests where the selected protocol supports them.

A decoded waveform is not automatically a delivered message. Local playback completion, packet decoding, authenticated message acknowledgement, and verified file completion are distinct events.

## 3. Specialized subsystems

| Subsystem | Main implementation | Responsibility |
|---|---|---|
| Audio I/O | `core/audio.py` | Device selection, capture/playback lifecycle, audio stream handling, and local device operations. |
| Acoustic modem | `core/modem.py`, `core/codec.py` | BFSK modulation/demodulation, frame encoding/decoding, supported wire-format handling, and packet-level checks. |
| Session coordination | `core/session.py`, `core/session_engine.py` | Session state, coordination of receive/transmit work, and application-facing lifecycle integration. |
| Transport | `core/transport.py`, `core/reliability.py` | Packet delivery workflow, acknowledgement/retry behavior, and bounded reliability policies. |
| Direct security | `security/identity.py`, `security/peer_handshake.py`, `security/handshake.py`, `core/crypto.py` | Ed25519 identity, pinned peer trust, ephemeral X25519 agreement, transcript-bound key derivation, directional AEAD, and replay defense. |
| Binary transfer | `core/binary_transfer.py` | Manifest/chunk transfer, resume state, chunk acknowledgements, repair, cancellation, and final digest validation. |
| Broadcast | `core/broadcast.py` | Open public room framing, fragment tracking, duplicate suppression, bounded retransmission cache, and selective missing-fragment requests. |
| Adaptive recovery (ASRE) | `core/recovery/` | Channel analysis, soft decoding, timing/frequency recovery, interleaving, adaptive FEC selection, profile generation, and recovery metrics. |
| Voice codec | `core/codec2_backend.py`, `core/codec.py` | Voice-note encoding/decoding using optional Codec2 where available and an ADPCM fallback. Voice notes are store-and-forward, not live calls. |
| Public SDK | `echocrypt_sdk/` | UI-independent client API, protocol wrappers, events, configuration, and cancellable transmission handles. |
| Terminal UI | `interface/` | Menus, dashboard, chat presentation, user actions, settings, and diagnostics. It must not own cryptographic or modem protocol rules. |

### Adaptive Signal Recovery Engine (ASRE)

ASRE is a receiver-side recovery subsystem, not an encryption feature. It attempts to recover symbol timing, frequency offsets, and damaged-but-correctable frames using bounded candidate searches and configured FEC strategies. It must never accept a frame merely because a recovery score is high: protected headers, format/version checks, and final integrity validation remain mandatory.

Recovery cannot reconstruct arbitrary information after the signal has lost too much information. When integrity validation fails, the correct response is rejection and protocol-level retransmission where available—not delivery of guessed bytes.

## 4. Direct and Broadcast are intentionally different

### Direct

Direct mode is for trusted peers. Public identity fingerprints are exchanged out-of-band and pinned. The handshake authenticates identities, performs ephemeral key agreement, derives directional traffic keys, and establishes replay-aware authenticated communication.

Text delivery can use authenticated acknowledgements and retries. File transfer uses encrypted chunks, resumable state, selective repair, and receiver-side SHA-256 validation before authenticated completion.

### Broadcast

Broadcast is an open room. There is no handshake, shared secret, private group key, or cryptographic sender identity. Its transform and hopping schedule are public protocol behavior. It is suitable only for information intended to be public. Fragment recovery and duplicate suppression improve delivery but do not create confidentiality or prove who sent a message.

## 5. File-transfer lifecycle

1. Sender creates a transfer identifier and manifest containing bounded metadata.
2. Receiver validates the manifest and establishes transfer state.
3. File bytes are divided into indexed chunks.
4. Chunks are transmitted and tracked independently.
5. Receiver validates each chunk and records received state.
6. Missing or damaged chunks are retried or selectively requested, depending on mode.
7. Receiver reconstructs the file and verifies the expected SHA-256 digest.
8. Only after successful verification can Direct mode report authenticated completion.
9. Sender may then remove its local resume state; cancellation stops future queued work but cannot retract audio already emitted.

A local transmission handle reports local queue/playback state; it is not proof of remote receipt.

## 6. Voice-note lifecycle

Voice is transported as an attachment/container rather than as a continuous real-time call. The sender captures audio, encodes it using the available voice codec, transfers the resulting container, and the receiver decodes it for playback. The optional Codec2 backend depends on a compatible system library; the fallback codec is used when it is unavailable. Emergency PTT is also a delayed burst workflow and remains half-duplex.

## 7. UI and mobile layout

The current UI is terminal-based. Mobile support means narrow-screen terminal compatibility, not a native Android graphical application.

Termux's rootless bootstrap can install Python dependencies, but that does not prove native microphone access works. The receive engine requires a live mono PCM input stream through PortAudio. The current upstream Termux app manifest does not declare Android's `RECORD_AUDIO` permission, while the separate Termux:API app declares it for its own recording service. That separate permission does not authorize PortAudio running inside the Termux app. Termux:API's file-based recording command is not a substitute for EchoCrypt's continuous PCM stream. See [Android audio and Termux notes](ANDROID_AUDIO.md).

- Menus and chat should respond to the terminal's current dimensions.
- Narrow terminals use a compact one-line wordmark instead of the wide ASCII banner.
- Chat bubbles must wrap within the available columns; fixed desktop-width minimums are prohibited.
- Keyboard escape-sequence parsing must use short, explicit timeouts so a lone ESC is responsive without breaking ANSI arrow/function-key sequences.
- Core protocol, crypto, transfer, and audio logic stays outside the UI layer.

## 8. Security and operational boundaries

- Direct mode has not received an independent security audit.
- The current private identity key is stored unencrypted at rest; protect the local key file.
- Broadcast has no confidentiality or cryptographic sender authentication.
- Frequency hopping is not anonymity and does not prevent recording or capable analysis of the acoustic channel.
- Audio codecs, microphones, speaker processing, echo cancellation, room acoustics, clipping, and collisions affect reliability.
- The current audio path is half-duplex. Full-duplex live voice is not implemented.
- Do not use the prototype for life-critical communications.

## 9. Development and verification

Run unit tests with:

```bash
python -m unittest discover -s tests -v
```

For a release candidate, also run syntax/compile checks, test Direct and Broadcast separately, test cancellation and resume paths, and perform two-physical-device tests with different speaker/microphone combinations. Record actual device, OS, sample-rate, room, distance, and result details. Do not label a feature hardware-verified based only on mocked or synthetic tests.
