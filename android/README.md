# ECHOCRYPT Android

Standalone native Android client. No Termux, terminal emulator, Python runtime, or companion app is required.

## Current implementation

- Kotlin + Jetpack Compose application shell.
- Runtime microphone permission and native 48 kHz mono PCM capture through `AudioRecord`.
- Native `AudioTrack` playback and full-duplex capture/playback session.
- Native ECF3 BFSK transmit encoder and receive decoder ported to Kotlin:
  - protected 11-byte header with duplicated Hamming(15,11) coding and header CRC;
  - terminated rate-1/2 convolutional payload FEC (K=7, polynomials `0x79` and `0x5B`);
  - depth-8 inner and depth-16 outer interleavers;
  - 48 kHz waveform generation and incremental stream receive.
- ECH1 v2 mutually authenticated peer handshake using Ed25519 signatures and ephemeral X25519.
- ECP1 encrypted application channel using ChaCha20-Poly1305 and directional session keys.
- Local device identity protected by an Android Keystore-backed encrypted vault.
- Trusted-peer management and Direct encrypted text messaging.
- Reliable stop-and-wait packet layer with fragmentation, encrypted ACKs, bounded reassembly, retries, replay/duplicate handling, and timeouts.
- Encrypted file transfer with persistent resume, final SHA-256 verification, and application-level delivery confirmation.

## Resumable file transfer

File-transfer application packets use the `ECT1` envelope inside the encrypted reliable packet layer:

1. `MANIFEST` identifies a random transfer ID, sanitized filename, length, fixed chunk size, chunk count, and SHA-256 digest.
2. The receiver replies with a `RESUME` bitmap describing the chunks already written and durably recorded.
3. `CHUNK` packets carry 1,400-byte file blocks. The receiver syncs the partial file and atomically persists its bitmap before treating the block as stored.
4. `FINISH` asks the receiver to verify the complete file. The receiver streams SHA-256 over the assembled file and commits it under a unique destination name only when the digest matches.
5. `COMPLETE` is an application-level confirmation sent only after the committed file and completion record exist. The sender retains its staged source until this response is received. A lost final response is recovered by retrying the manifest/finish exchange.
6. `REJECT` reports a final validation or storage failure. The sender keeps its staged source so the transfer can be retried.

Outgoing source copies, incoming `.part` files, and chunk bitmaps are stored in app-private storage. Transfer state is separated by trusted peer fingerprint. A new authenticated Direct session with the same peer automatically exchanges the receiver bitmap and resumes from missing blocks. After a link timeout, further file traffic pauses rather than continuously transmitting; use the resume action or establish a fresh Direct session to continue.

Limits: one pending outgoing transfer per trusted peer, up to two active incoming transfers, and a maximum file size of 10 MiB. Received files remain in app-private storage until exported through Android's Storage Access Framework. The app does not request broad storage permissions.

## Reliability and limits

- The modem profile currently used by Direct is 200 baud, and the reliable packet layer uses stop-and-wait ARQ. File transfer is therefore intentionally conservative and can be very slow, especially for multi-megabyte files.
- A transport ACK means a reliable message was received by the remote protocol layer; it does **not** mean the file has been committed. The sender reports success only after the receiver's authenticated `COMPLETE` response.
- A transfer can resume after a process restart or link interruption, provided both devices retain their app-private storage and reconnect to the same trusted peer.
- Reconnection is not an automatic background radio/network reconnect: the user must bring Direct back to an authenticated session with the same peer. Resume negotiation then runs automatically.
- File contents are integrity-checked with SHA-256 in addition to authenticated encryption. SHA-256 is not a substitute for authentication; peer authentication and message authentication are provided by the established Direct session.

## Build and CI

Requirements: Android SDK 35, JDK 17, Gradle 8.9+.

From this directory run:

```bash
gradle testDebugUnitTest assembleDebug
```

APK output: `app/build/outputs/apk/debug/app-debug.apk`.

The GitHub Actions Android workflow runs JVM unit tests and assembles a debug APK on pushes to the `android` branch. The uploaded APK is an **unsigned debug artifact**, not a production-signed release. CI/unit tests do not replace real-device microphone, speaker, acoustic loopback, and two-device interoperability tests.

## Architecture

- `MainActivity.kt`: presentation, permission flow, file picker/exporter, and UI actions.
- `audio/AndroidAudioEngine.kt`: PCM capture and playback primitives.
- `audio/DuplexAudioSession.kt`: concurrent capture/playback queues and optional AEC/noise suppression.
- `protocol/NativeModem.kt`: ECF3 transmit framing and BFSK modulation.
- `protocol/NativeDemodulator.kt`: ECF3 receive decoder, header validation, FEC and CRC checks.
- `protocol/NativeStreamReceiver.kt`: incremental PCM scanner and candidate-frame extraction.
- `protocol/NativePeerHandshake.kt`: authenticated peer handshake and encrypted session.
- `protocol/ReliablePacketLayer.kt`: encrypted reliable messages and ACK/retry behavior.
- `protocol/NativeFileTransfer.kt`: persistent ECT1 file-transfer state machine.

The desktop Python implementation is not modified by Android development. Cross-platform interoperability must be demonstrated with shared test vectors and real-device loopback/interoperability tests before describing the Android client as fully interoperable.

## ECF3 wire-format interoperability note

The native modem follows the Python ECF3 framing: the fixed 11-byte header is protected separately by its duplicated Hamming(15,11) code and header CRC; the convolutional payload codeword contains only the application payload plus the frame CRC over `header || payload`. ECF3 applies the depth-16 burst interleaver outside the depth-8 convolutional interleaver. Frequency hopping is not emitted by this encoder until the authenticated hop-key lifecycle is integrated; a requested hopped mode must never silently fall back to an unhopped mode.