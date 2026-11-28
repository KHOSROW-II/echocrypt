# ECHO-CRYPT

[![CI](https://github.com/KHOSROW-II/echocrypt/actions/workflows/tests.yml/badge.svg?branch=main)](https://github.com/KHOSROW-II/echocrypt/actions/workflows/tests.yml)
![Python](https://img.shields.io/badge/Python-3.10%2B-blue)
![License](https://img.shields.io/badge/License-MIT-green)
![Direct](https://img.shields.io/badge/Direct-E2E%20encrypted-success)
![Broadcast](https://img.shields.io/badge/Broadcast-Public-orange)
![Virtual Audio](https://img.shields.io/badge/Virtual%20Audio-Linux%20PipeWire%2FPulseAudio-blue)
![Tab Capture](https://img.shields.io/badge/Tab%20Capture-Chromium%20Native%20Messaging-orange)
![Workers](https://img.shields.io/badge/Parallel%20Workers-Hardware--gated-purple)

### Acoustic Messaging & Data Transport

ECHO-CRYPT is an experimental acoustic communication system that transfers encrypted messages, files, and voice notes directly between nearby compatible devices using speakers and microphones.

Its native acoustic transport uses speakers and microphones without requiring IP networking, a relay, a broker, or a central server. Optional carrier mode can route the waveform through an ordinary Internet call application; in that mode the external application needs its own Internet connection, but ECHOCRYPT still does not depend on a provider API.

The project combines digital signal processing, authenticated encryption, error correction, synchronization, and adaptive acoustic recovery into a single communication system.

---

## Features

- **Acoustic communication:** Direct device-to-device data transmission through audio.
- **Persistent reception:** Automatically monitors incoming audio and identifies supported packet types.
- **Interactive chat:** Chat-style interface with automatic message reception and transmission.
- **End-to-end encryption:** Authenticated Direct sessions using ChaCha20-Poly1305.
- **Open Broadcast:** Public communication without identity exchange or group keys.
- **Binary transfers:** Transfer arbitrary files, including documents, images, archives, and media.
- **Voice messages:** Record, transmit, receive, and play `.ecv` voice attachments.
- **Reliable transport:** Chunk tracking, acknowledgements, retries, duplicate suppression, and integrity verification.
- **Adaptive signal recovery:** Signal analysis, soft-decision decoding, synchronization recovery, and adaptive FEC.
- **Frequency hopping:** Keyed frequency hopping in authenticated Direct sessions.
- **Terminal interface:** Interactive keyboard-driven interface with live transmission and reception status.
- **Audio diagnostics:** Microphone and speaker tests, acoustic metrics, and file-based debugging.
- **Generic virtual audio:** Optional Linux virtual microphone/speaker endpoints for any application that supports selecting audio devices.
- **Selected browser-tab RX:** Chromium extension captures one user-selected tab and feeds its PCM directly into ECHOCRYPT through Native Messaging.
- **Isolated full-duplex modem I/O:** Separate virtual TX/RX routes without pausing capture during transmission.
- **Hardware-gated workers:** Parallel Direct chunk preparation is enabled only after a passing device-specific modem-band test.

## Communication Modes

### Direct

Direct mode provides authenticated, encrypted communication between two trusted devices.

It uses:

- Ed25519 identities and mutual authentication.
- Ephemeral X25519 key agreement.
- Transcript-bound HKDF-SHA256 key derivation.
- Directional ChaCha20-Poly1305 encryption keys.
- Authenticated acknowledgements and replay protection.
- Key-derived frequency hopping.
- Reliable message and file delivery with bounded retries.


#### Direct security profiles

When connecting to a saved peer, choose a negotiated Direct profile:

| Profile | Initial modem rate | Traffic padding | Keyed frequency hopping | Retry budget |
|---|---:|---:|---|---:|
| Low | 800 baud | Off | Off | 1 |
| Medium | 400 baud | 64-byte blocks | On | 3 |
| Secure | 200 baud | 256-byte blocks | On | 5 |
| Custom | User-defined | 0/32/64/128/256-byte blocks | User-defined | 0–10 |

Every profile retains the same authenticated cryptographic primitives: Ed25519 identity signatures, ephemeral X25519 key agreement, HKDF-SHA256, and directional ChaCha20-Poly1305 channels. Padding makes ciphertext lengths less precise; keyed hopping changes the acoustic symbol schedule. Neither feature makes the cipher itself stronger, and neither guarantees anonymity or resistance to a capable acoustic observer.

The selected profile is carried inside the signed HELLO exchange. Different preset selections negotiate the stronger preset; Custom must match exactly on both devices. The new Direct HELLO protocol is version 2, so **both devices must update to the same current release/commit** before opening a Direct session. Version 1 peers are not wire-compatible with version 2.

Both devices must import and explicitly trust each other's public identity before establishing a session.

### Broadcast

Broadcast is an open acoustic communication mode for nearby compatible ECHO-CRYPT instances.

It requires no:

- Identity exchange.
- Handshake.
- Membership approval.
- Private group key.
- Mandatory delivery acknowledgement.

Broadcast traffic is intentionally public. Its compatibility transform and frequency schedule are not secret. Anyone implementing the protocol can potentially decode, inspect, or forge Broadcast traffic.

The current Broadcast PHY uses a fixed-frequency 800-baud profile without hopping. Both endpoints should run the same current build; receivers no longer spend decode time probing older Broadcast baud-rate profiles. File manifests are repeated and missing chunks can be selectively requested, but Broadcast still has no mandatory delivery acknowledgement.

Do not use Broadcast for private or sensitive information.

Receivers can selectively request missing fragments from the sender's bounded retransmission cache.

---

## How It Works

ECHO-CRYPT converts application data into framed digital signals and transmits them through an audio output device.

The receiving device continuously monitors its microphone input, detects candidate frames, synchronizes with the signal, decodes the packet, validates its integrity, and routes it to the appropriate application handler.

The communication pipeline includes:

1. Application message or attachment.
2. Encryption and authentication (Direct mode).
3. Packet framing and header protection.
4. Forward error correction and interleaving.
5. Acoustic modulation and transmission.
6. Synchronization and signal recovery.
7. Frame validation and decoding.
8. Decryption and application-level delivery.

The modem primarily uses BFSK modulation.

Supported wire formats include ECF3, ECF2, and legacy ECF1 reception. ECF3 is the default transmission format.

### Adaptive Signal Recovery Engine (ASRE)

ASRE improves reception under imperfect acoustic conditions through:

- Signal quality and clipping analysis.
- Soft-decision symbol decoding.
- Preamble and synchronization recovery.
- Frequency-offset correction.
- Sample-clock timing recovery.
- Adaptive FEC selection.
- Interleaving against burst errors.
- Bounded decoding candidates and integrity validation.

Error correction has finite limits. When a frame cannot be recovered, the transport reliability mechanisms handle retransmission where supported.

---

## Messaging and Transfers

### Text

Text messages are transmitted as framed acoustic packets.

Direct messages are encrypted and acknowledged. Broadcast messages are public and use fragment tracking and selective recovery.

### File Transfers

ECHO-CRYPT supports arbitrary binary files without requiring application-level parsing.

Files are divided into bounded chunks and transferred using protocol-specific reliability mechanisms.

Direct mode provides:

- Encrypted chunks.
- Per-chunk acknowledgements.
- Retry handling.
- Transfer resume state.
- SHA-256 integrity verification.

Broadcast mode provides:

- Numbered fragments.
- Manifest-based transfer.
- Selective retransmission requests.
- Duplicate suppression.
- Final SHA-256 verification.

The current default limits are:

| Mode | Default limit |
|---|---:|
| Direct | 256 MiB |
| Broadcast | 8 MiB |
| Default chunk size | 256 bytes |

Actual transfer time depends heavily on acoustic conditions, modem rate, FEC overhead, and retransmissions.

### Voice Messages

Voice notes use the `.ecv` container format.

- ECV3 uses Codec2 at 1300 bit/s when the optional system `libcodec2` library is available.
- ECV1 uses the built-in IMA ADPCM fallback when Codec2 is unavailable.

Voice messages can be played directly from the chat interface. Playback also produces a standard WAV file.

Voice notes are file transfers, not live voice calls.

### Emergency PTT

Emergency Push-to-Talk provides delayed, short voice bursts.

When Codec2 is available, it uses the 700C profile. Otherwise, it falls back to ADPCM.

This mode is half-duplex and does not provide real-time conversation.

---

## Developer API

ECHO-CRYPT includes a public Python SDK for application developers:

- Low-level acoustic packet transport and event subscriptions.
- Authenticated Direct messaging, custom encrypted payloads, and JSON topics.
- Resumable Direct file transfer with authenticated completion.
- Open Broadcast messaging, file distribution, and selective retransmission.

Install from the repository with `python -m pip install ".[audio]"`.

**API reference and examples:** [Developer API Guide](docs/DEVELOPER_API.md)

**System architecture:** [Architecture and specialized subsystems](docs/ARCHITECTURE.md)  
**Development plan:** [Roadmap](docs/ROADMAP.md)

## Installation

### Requirements

- Python 3.
- A compatible operating system.
- Microphone and speaker.
- Audio input/output support through PortAudio.
- Optional system `libcodec2` for Codec2 voice profiles.

### Linux, Windows, and Mobile

Extract the repository and enter its directory:

```bash
cd ECHOCRYPT
```

Launch the application:

```bash
python echocrypt.py
```

On first launch, the built-in bootstrap system offers four installation profiles:

| Profile | Description |
|---|---|
| Full | Installs core and audio dependencies. |
| Standby | Installs core dependencies and offers audio dependencies when needed. |
| Manual | Creates the virtual environment without installing packages. |
| Rootless | Uses unprivileged installation for mobile environments. |

To explicitly select a profile:

```bash
python echocrypt.py --bootstrap full
```

For Termux:

```bash
python echocrypt.py --bootstrap rootless
```

On native Termux launches, ECHOCRYPT loads the Android-specific full-screen CLI menu. Menu entries are presented as large rows; tap a row when the terminal emulator forwards touch as a mouse event, or use the arrow keys and Enter. Esc goes back. This is still a terminal interface, not an Android APK or native Android GUI. Termux's own display/immersive settings control whether Android system bars are hidden.

The bootstrap installs the Termux-side `termux-api` command package. Also install the **Termux:API Android companion app** from the same trusted distribution/signing source as Termux. Then open **Configuration → Audio settings → Request Android microphone permission** and allow the request. Termux:API is used to request the permission and make a short temporary test recording; EchoCrypt's continuous receiver still uses a live PortAudio microphone stream.

The launcher handles dependency checks and environment setup according to the detected platform.

**Android/Termux limitation:** Rootless bootstrap and a working terminal UI do not guarantee live microphone capture. EchoCrypt requires a continuous raw PCM input stream. The upstream Termux app itself does not declare `RECORD_AUDIO`, although Termux:API does and the upstream packages use a shared UID; the effective permission grant and PortAudio stream opening must still be verified on-device. See [Android audio and Termux notes](docs/ANDROID_AUDIO.md).

---

## Initial Configuration

Perform the following steps on both devices.

1. Launch ECHO-CRYPT.
2. Open **Manage Identity** and create a local identity.
3. Export the public identity file.
4. Exchange public identity files with the other device.
5. Import the other device's public identity.
6. Compare the complete SHA-256 fingerprints through a separate trusted channel.
7. Confirm the identity using the application's trust procedure.
8. Select the saved peer and establish a Direct session.

**Never share your private identity file.**

The private Ed25519 key is stored locally at:

```text
keys/identity.ed25519
```

Protect this file and maintain secure backups.

For Broadcast, select **Join open Broadcast room**. No identity setup or handshake is required.

---

## Interface and Controls

The desktop terminal interface adapts to narrow terminal widths. Android/Termux uses a separate full-screen menu layer with large tap targets, touch selection where the terminal emulator forwards mouse events, and arrow/Enter/Esc keyboard navigation. The Android menu layer does not fork the communication, cryptography, audio, or protocol implementation. The chat view remains the shared full-screen chat interface.

The chat interface supports:

| Key | Action |
|---|---|
| Enter | Send message |
| F2 | Attach file |
| F4 | Record voice note |
| Space | Hold to talk |
| F5 | Emergency PTT |
| F6 | More options |
| F7 | Cancel voice operation |
| F9 | Play most recent voice message |
| Esc | Disconnect |

The receiver operates automatically while the chat is idle.

The audio path is half-duplex per device: microphone capture pauses during local speaker transmission and resumes afterward.

---

## Configuration and Diagnostics

Open **Configuration** from the main menu to adjust:

- Audio input and output devices.
- Sample rate and microphone gain.
- Recording window.
- Modem frequencies and transmission rates.
- Payload size.
- Acknowledgement timeout.
- Retry limits.

Audio diagnostics include:

- Left/right speaker verification.
- Live microphone level monitoring.
- RMS and peak measurements.
- Clipping detection.
- Acoustic debug logging.

Acoustic debug traces are stored locally:

```text
logs/acoustic-debug.log
```

Debug logging does not intentionally record plaintext message bodies, private keys, or session keys.

For physical-layer troubleshooting, raw microphone bursts can be saved as PCM16 WAV files. This is **off by default** and requires acoustic debug logging to be enabled:

```bash
ECHOCrypt_CAPTURE_RX_WAV=1
ECHOCrypt_CAPTURE_RX_WAV_LIMIT=20
ECHOCrypt_CAPTURE_RX_WAV_DIR=logs/acoustic-captures
```

Set these variables before launching ECHOCRYPT. The capture limit defaults to 20 files and is capped at 100. WAV files contain raw microphone audio, so they may include nearby speech or other audible material; review them before sharing and delete them when no longer needed.

---

## Development and Testing

Run the automated test suite:

```bash
python -m unittest discover -s tests -v
```

The test suite covers cryptographic operations, authentication, packet framing, FEC, synchronization, signal recovery, reliable transfers, voice encoding, bootstrap behavior, and synthetic acoustic loopback.

Automated waveform tests do not replace testing between two physical devices.

Hardware compatibility, microphone permissions, room acoustics, speaker response, and operating-system audio processing must be verified independently.

---

## Architecture graphs

The diagrams below describe application-independent routes. ECHOCRYPT does not call Meet, Zoom, Discord, or any provider API. Any desktop application exposing microphone and speaker selection can use the generic virtual endpoints.

### Direct encrypted messaging

```text
Application / SDK
       |
       v
Pinned peer identities + signed Handshake
       |
       v
Directional AEAD encryption (ChaCha20-Poly1305)
       |
       v
ECF framing + integrity + FEC
       |
       v
BFSK modulation + keyed frequency hopping
       |
       v
Physical speaker OR ECHOCRYPT Virtual Microphone
       |
       v
Acoustic path OR an application used as an audio carrier
       |
       v
Peer RX -> synchronization -> demodulation -> FEC/CRC
       |
       v
AEAD authentication/decryption -> replay checks -> application
```

Direct encryption is provided by ECHOCRYPT, not by the conferencing provider. The provider may still record, transcode, denoise, or suppress the audio waveform. If it alters the modem signal, delivery can fail; ECHOCRYPT ciphertext is confidential only when the receiving peer recovers and authenticates it.

### Public Broadcast

```text
Public text / file
       |
       v
Broadcast framing + fragment identifiers
       |
       v
CRC / FEC (no Direct handshake and no private key)
       |
       v
BFSK waveform -> speaker / virtual microphone
       |
       v
Open acoustic or audio-carrier channel
       |
       v
Receiver demodulation -> fragment reassembly -> file SHA-256
```

Broadcast is public and must not carry confidential information.

### File transfer and preparation workers

```text
File -> manifest + resume state
               |
               v
     Bounded parallel packet preparation
       |           |           |
       v           v           v
    Chunk 0     Chunk 1     Chunk 2 ...
       \           |           /
        \          |          /
         v         v         v
          Single ordered acoustic TX queue
                       |
                       v
          Send chunk -> wait authenticated ACK
                       |
                       v
         Receiver validates -> repair/resume -> SHA-256
```

Workers prepare chunks concurrently. They do not play overlapping waveforms; the acoustic TX queue remains serialized.

### Voice notes

```text
Microphone -> capture -> Codec2 / ADPCM -> .ecv container
      |
      v
Direct encrypted file-transfer channel
      |
      v
Receiver verifies chunks + SHA-256 -> decode -> WAV playback
```

Voice notes are store-and-forward, not live voice calls.

### Generic virtual audio endpoints (Linux)

```text
ECHOCRYPT TX
     |
     v
ECHOCRYPT Virtual Microphone TX (virtual sink)
     |
     v
ECHOCRYPT Virtual Microphone (monitor source)
     |
     v
Any call / meeting / browser application's microphone input
     |
     v
Internet / application's audio transport
     |
     v
Remote peer's call / meeting / browser application
     |
     v
ECHOCRYPT Virtual Speaker (virtual sink)
     |
     +-------------------------------> Physical speaker (optional per-sink monitor)
     |
     v
ECHOCRYPT Virtual Speaker Monitor (monitor source)
     |
     v
ECHOCRYPT RX
```

Only the application explicitly routed to **ECHOCRYPT Virtual Speaker** is mirrored into ECHOCRYPT RX. Other desktop audio streams and system defaults are not changed. The optional monitor loopback mirrors that one sink to the current physical output.

### Separate worker hardware gates

```text
     AUDIO-PATH APPROVAL                 CPU / RAM CAPACITY
Selected mic + speaker + tones       Effective CPU quota + RAM limit
              |                                  |
              v                                  v
     Full-duplex modem-band test         Compute local worker cap (1..8)
              |                                  |
              v                                  |
Every hopping tone detected?                     |
        /             \\                         |
       NO              YES                       |
       |                |                        |
       v                v                        |
 Workers forced to 1   Save local audio approval |
       |                |                        |
       +----------------+------------------------+
                        |
                        v
         Signed Direct Handshake advertises
          min(configured count, local cap)
                        |
                        v
          Effective count = min(local, peer)
```

The audio approval is bound to the PortAudio-reported input/output devices, sample rate, playback gain, and active modem tone set. Changing those settings invalidates it. Separately, the resource cap uses logical CPU availability, Linux cgroup CPU quotas where present, and the tighter visible RAM limit (cgroup or physical RAM). It reserves 512 MiB for the system and budgets 64 MiB per preparation worker. Unknown RAM falls back to the CPU cap. Either gate can reduce the local count to one; neither gate ever enables more than eight workers.

### Receive and recovery

```text
Input PCM -> ContinuousAudioListener -> burst detection
       |
       v
Preamble/frame synchronization -> BFSK demodulation
       |
       v
ASRE recovery -> header CRC -> FEC -> payload integrity
       |
       v
Direct AEAD/replay verification OR public Broadcast routing
       |
       v
Chat / file / voice-note delivery
```

### Selected Chromium browser-tab capture

```text
User-selected meeting / call tab
       |
       v
Chromium tabCapture (extension action click)
       |
       +-------------------------------> AudioContext -> normal speakers
       |
       v
48 kHz mono PCM AudioWorklet
       |
       v
Chrome Native Messaging (local stdio)
       |
       v
ECHOCRYPT native host -> pacat -> echocrypt_tab_rx sink
       |
       v
ECHOCRYPT Browser Tab Monitor
       |
       v
ECHOCRYPT RX
```

Only the selected tab is captured. Its audio is also passed through to the browser's normal output so it remains audible. Other tabs, desktop applications, the physical microphone, and system-wide audio routing are not captured or changed. The extension has no Meet/Zoom/Discord API integration; it works with the audio rendered by the selected Chromium tab. See [Selected browser-tab capture](docs/BROWSER_TAB_CAPTURE.md).

## Limitations

ECHO-CRYPT is an experimental communication prototype.

- Acoustic bandwidth is significantly lower than conventional networking.
- Large file transfers can take hours.
- Noise, reverberation, clipping, and device audio processing can prevent successful decoding.
- Simultaneous Broadcast transmissions can collide.
- Broadcast provides no confidentiality or cryptographic sender authentication.
- Direct private identity keys are currently stored unencrypted at rest.
- Frequency hopping does not provide anonymity or protection against capable observers.
- FDM remains experimental and has not been validated across physical devices.
- Full-duplex live voice conversation is not implemented. Isolated full-duplex modem I/O is available when the Linux virtual endpoints are configured.
- Selected-tab capture is Chromium-only in this implementation and has not yet been validated across real Internet-call providers or their noise-suppression pipelines.
- Browser-tab RX uses 48 kHz PCM and does not support the ultrasonic modem mode.

Do not rely on ECHO-CRYPT for life-critical or highly sensitive communications without independent security review and physical-device validation.

---

## Project Status

ECHO-CRYPT is an actively developed experimental acoustic communication project.

Its native acoustic mode exchanges data directly through ordinary audio hardware without an IP transport. An optional carrier mode can route the same ECHOCRYPT waveform through a user-selected third-party call application. In that mode, the call application supplies the Internet transport, while ECHOCRYPT remains independent of that provider's API or SDK.

## Security

Please read the [Security Policy](SECURITY.md) before using ECHO-CRYPT or reporting a vulnerability. ECHO-CRYPT is experimental: Direct mode has not received an independent security audit, and Broadcast mode is public and provides no confidentiality or cryptographic sender authentication. The current private identity key is stored unencrypted at rest.

---

## License

ECHO-CRYPT is distributed under the MIT License. You may use, copy, modify, merge, publish, distribute, sublicense, and sell copies of the software, subject to the conditions in [LICENSE](LICENSE). The software is provided without warranty.

## Optional ultrasonic mode

Ultrasonic operation is disabled by default. Enable it under **Configuration → Modem and frequency settings → Ultrasonic acoustic mode**. Enabling the setting first runs a local full-duplex loopback test: ECHO-CRYPT plays low-level tones covering the configured frequency-hopping range through the selected output and checks whether the selected microphone captures each one.

The default near-ultrasonic tones are **20.7 kHz** and **22.2 kHz**. With the modem's ±600 Hz hopping offsets, the active range is approximately 20.1–22.8 kHz. At a 48 kHz sample rate the Nyquist frequency is 24 kHz, so the configured tones leave a guard band below Nyquist. The application rejects unsafe frequency/sample-rate combinations.

A loopback pass is only a local path check; it is not a laboratory measurement or a guarantee of good range, sensitivity, or remote compatibility. The speaker, microphone, their analog filters, audio codec, and the acoustic path must all preserve the selected frequencies. Both Direct peers must enable the setting and pass their own local test. The signed Direct handshake negotiates ultrasonic support; if either peer has it disabled, the session stays on the ordinary audible modem frequencies. Broadcast does not use ultrasonic mode.

## Optional Linux virtual microphone and speaker

Open **Configuration → Audio settings → Create / configure ECHOCRYPT virtual microphone + speaker**. ECHOCRYPT creates two isolated PipeWire-Pulse / PulseAudio endpoints and configures its own TX/RX devices. In any call, meeting, browser, or conferencing application, choose **ECHOCRYPT Virtual Microphone** as its microphone and **ECHOCRYPT Virtual Speaker** as its speaker. No provider API or Meet-specific integration is used.

The RX virtual sink can be mirrored to the current physical speakers so the user still hears the selected call. Only audio routed to that sink is mirrored; unrelated desktop audio and system defaults are untouched. ECHOCRYPT uses separate TX/RX endpoints and enables full-duplex modem I/O for this route.

The first backend is Linux-only and requires a running PulseAudio or PipeWire-Pulse server plus `pactl`. Stock Termux and Windows need separate audio backends. See [Virtual audio setup and routing](docs/VIRTUAL_AUDIO.md).

## Selected Chromium tab RX

For the most isolated receive path, open **Configuration → Audio settings → Use Chromium selected-tab capture as ECHOCRYPT RX**. This creates a dedicated browser-tab input sink that is separate from the generic virtual speaker.

Then install the unpacked extension from `browser_extension/`, register the native host using the extension ID, and restart Chromium. In the target call website, set the **microphone** to **ECHOCRYPT Virtual Microphone** and leave its **speaker** on the normal system/default output. Click the ECHOCRYPT extension icon while the target tab is active; click again to stop. ECHOCRYPT receives only that tab's audio while the browser keeps playing it through the normal output. Full installation steps are in [docs/BROWSER_TAB_CAPTURE.md](docs/BROWSER_TAB_CAPTURE.md).

Do not select **ECHOCRYPT Virtual Speaker** as the website's speaker at the same time as selected-tab capture. Those are two alternative RX routes and using both would duplicate the received audio. Browser-tab capture requires Chromium 116+, PulseAudio/PipeWire-Pulse, `pactl`, and `pacat`. It is currently Linux-only.

## Optional parallel Direct file and voice-note preparation

Parallel preparation is disabled by default. First run **Configuration → Direct transfer and worker settings → Run dedicated microphone/speaker modem-path test**. Only a passing audio-path test can approve the device pair. A separate CPU/RAM gate caps the requested count using logical CPU/cgroup capacity and available memory. During an authenticated Direct handshake, each peer advertises its locally approved and resource-capped worker limit; the session uses the lower count. A device without audio-path approval, or with hardware capacity for only one worker, advertises one.

Workers read file chunks and prepare their authenticated encrypted packets concurrently ahead of transmission. Packets are still emitted in order through the single acoustic transmit queue, and each chunk still requires its authenticated ACK before the next chunk is sent. This avoids unsafe simultaneous playback on one speaker and preserves resumability, replay protection, integrity checks, and the existing stop-and-wait reliability model. The setting applies to Direct file attachments and file-backed voice notes; open Broadcast and live push-to-talk audio do not use this worker pool.

Both optional capabilities are advertised inside the existing signed HELLO header. With both features disabled and one worker selected, the default capability fields retain their legacy values. When either feature is enabled, both devices must run a compatible ECHO-CRYPT version so the peer can understand the negotiated capability fields.


## Cross-platform binaries and installers

Official 64-bit desktop builds cover Linux x86-64 and ARM64, Windows x86-64 and ARM64, and macOS Intel and Apple Silicon. Every target receives a portable ZIP plus a native installer:

| Platform | x86-64 | ARM64 |
|---|---|---|
| Linux | ZIP + DEB + RPM | ZIP + DEB + RPM |
| Windows | ZIP + MSI | ZIP + MSI |
| macOS | ZIP + PKG (Intel) | ZIP + PKG (Apple Silicon) |

The installers include the standalone CLI, Native Messaging host, browser extensions, and documentation. They are CLI distributions, not GUI desktop applications. Packages are currently unsigned; Windows MSI packages are not Authenticode-signed and macOS PKG packages are not notarized.

The official matrix does not currently include 32-bit Linux/Windows, ARMv7, RISC-V, PowerPC, or IBM Z. See [platform support](docs/PLATFORMS.md) for runtime requirements and the exact support boundary. See the [installation guide](docs/INSTALLATION.md) for platform-specific install commands and optional browser host registration.

Build browser packages locally:

    python scripts/build_browser_extensions.py --browser all --output dist/extensions

The release workflow runs native tests on supported operating systems and architectures before building ZIPs and native installers. It verifies the exact release asset set and SHA-256 checksums, then creates or updates a GitHub **Draft** Release. Release Notes use GitHub's generated changes since the previous release and a direct-download table; raw repository history is not dumped into the notes. Nothing is published automatically.
