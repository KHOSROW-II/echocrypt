# ECHO-CRYPT — acoustic messaging and data transport

ECHO-CRYPT carries packets through an acoustic/audio path between nearby
compatible devices. It does **not** use a server, sockets, TCP/UDP, an internet
route, or a direct network connection. The chat session owns a persistent
microphone listener while idle, classifies incoming frames automatically, and
routes text, file-transfer and control packets without a manual receive step.
Press Enter to send a message in-place without closing the chat screen.
Local messages appear on the right; messages from the other side appear
on the left.

Broadcast voice-note reception now keeps several bounded synchronization candidates instead of trusting only the strongest preamble correlation. A wrong ECF2/ECF3 interpretation is rejected by the protected header/version checks, then the receiver continues to the next candidate; lower-confidence preambles are considered only when the independently protected header and final frame CRC validate.

The current voice-note format remains `.ecv` and can be played directly from the chat. When `libcodec2` is installed, new voice notes
use the compact ECV3 Codec2 1300 bit/s profile; without it, ECHO-CRYPT falls
back to the built-in ECV1 IMA ADPCM profile. Click `▶ Play voice` under a sent
or received voice bubble, or press F9 to play the most recent voice message.
Playback also writes a standard `.wav` file beside the `.ecv` source.

Broadcast file and voice fragments use packet v2 with a duplicated, independently
CRC-checked ECFH fragment descriptor bound to the encrypted payload. Voice
notes prefer 800-baud transmission and smaller chunks. The manifest is sent
once; missing manifests or exact missing chunk indexes are selectively requested
and only those cached frames are retransmitted. One valid descriptor copy is
enough; if both copies are invalid or disagree, the packet is rejected rather
than interpreted ambiguously.

Stage 9.6 improves synchronization under room noise: the receiver scans the full
continuous-listener pre-roll, refines each supported wire-format preamble
independently, rejects low-confidence random correlations early, and avoids
wasted timing-recovery loops when no preamble is present. A standalone Esc now
uses short terminal parsing timeouts and returns from chat without waiting for
audio-stream shutdown; resource cleanup continues in the background.

Stage 9.3 keeps a single animated activity row visible throughout the chat,
with replace-in-place TX/RX progress (for example `7/19 chunks`). Toolbar
actions are clickable as well as keyboard-bound. Incomplete messages expose a
selective `Request resend` action; Direct requests are authenticated and
Broadcast requests are public. Hold the mouse PTT control for exact press/release
recording, or use Space key-repeat detection / F4 toggle in terminal sessions.

The physical audio path remains half-duplex per device: local microphone capture
is paused during local speaker transmission to avoid self-echo, then resumed.

Stage 9 adds two session modes: authenticated **Direct** conversations and an
open, no-handshake **Broadcast** room. Direct remains end-to-end encrypted.
Broadcast is intentionally public to all compatible nearby ECHO-CRYPT
instances and is not confidential against a protocol-aware listener.

## Stage 4 capabilities

- Arrow-key terminal UI (Up/Down, Enter, Esc) and local Ed25519 identity.
- Mutual signed HELLO authentication with ephemeral X25519 key agreement.
- Transcript-bound HKDF-SHA256 derives separate transmit, receive and shared
  acoustic hopping keys.
- ChaCha20-Poly1305 authenticated encryption for all application payloads.
- Authenticated per-symbol frequency hopping after the handshake. The preamble
  and modem header use the known base tones; the body follows a keyed schedule
  derived independently by both peers.
- ECF3 BFSK framing by default. A distinct 64-bit synchronization sequence
  identifies each wire version. The fixed header retains duplicated
  Hamming(15,11) protection plus independent CRC32 validation. ECF3 adds a
  depth-16 payload interleaver ahead of FEC decoding to spread short burst
  damage across coded symbols. Receivers also accept ECF2 and legacy ECF1.
- Soft-decision Hamming(7,4), rate-1/2 convolutional/Viterbi, Repetition-3
  and raw payload modes. Reliable chat starts with convolutional/Viterbi and
  bounded lower-rate retries; legacy and emergency paths retain their own
  compatible retry profiles.
- Reliable encrypted UTF-8 messages with ACK/retry and duplicate suppression.
- Universal binary transfer for arbitrary files (JPG, PNG, PDF, ZIP, DOCX,
  PPT/PPTX, MP3, MP4, and other byte formats), split into bounded 256-byte
  chunks by default. PowerPoint files are transported as opaque bytes; the app
  does not parse or preview slides.
- Per-chunk encrypted ACKs, SHA-256 integrity verification, filename/path
  validation, collision-safe output names, and resume after interruption.
- Voice notes prefer ECV3 Codec2 at 1300 bit/s when the optional system
  `libcodec2` library is installed. Otherwise the sender falls back to ECV1
  4-bit IMA ADPCM at 3 kHz mono (12 kbit/s before container overhead). Sent and
  received ECV entries expose a clickable `Play voice` action and F9 shortcut;
  playback creates an adjacent standard PCM WAV file. Upsampling improves
  device compatibility, not bandwidth.
- Emergency Push-to-Talk stream: when the optional system `libcodec2` is
  installed, two-second speech bursts use Codec2 700C (700 bit/s) and a 1500-baud
  fixed-tone modem profile. The first attempt uses raw payload FEC plus CRC/AEAD;
  retries automatically step up to Hamming, convolutional/Viterbi, and
  Repetition-3. If Codec2 is
  unavailable, the application falls back to much larger ADPCM bursts.
  The receiver can remain armed and play each completed burst. This is delayed,
  half-duplex PTT, not simultaneous full-duplex audio.
- Experimental FDM Hybrid mode: a separate text BFSK band and media BFSK band
  are combined into one waveform. The same encrypted text packet accompanies
  each media chunk; the receiver displays it once and re-ACKs duplicates.

## Installation and bootstrap profiles — all platforms

Unzip the project, enter its directory, and launch the application directly:

```bash
python echocrypt.py
```

On first launch, `echocrypt.py` performs bootstrap itself. A separate shell or
PowerShell bootstrap script is no longer required. The profile selector offers:

- **Full**: create `.venv` and install core plus audio Python dependencies.
- **Standby**: create `.venv` and install core dependencies; optional audio
  dependencies are offered when an audio feature is first used.
- **Manual**: create `.venv` only; install packages yourself.
- **Rootless (mobile)**: avoid root privileges and host-level `sudo`/APT
  operations. It is intended for Termux, PRoot distributions and mobile shell
  environments.

On native Termux, the launcher detects the actual Python executable/prefix and
automatically selects **Rootless (mobile)**, even if the project directory was
copied from a desktop installation with a stale saved profile. It installs
Termux-native packages with `pkg` (including `python-pip`, `python-numpy`,
`python-cryptography`, `portaudio` and `pulseaudio`), then installs the pure
Python/UI and `sounddevice` bindings with the Termux Python interpreter. It does
not create a venv, run `sudo`/APT, or upgrade pip. The launcher tests real
imports and whether the PortAudio library can load, then reports the number of
visible input/output devices. On later launches it restarts/probes the audio
bridge without reinstalling packages; dependency/PortAudio self-test failure
triggers Rootless repair. It attempts available PulseAudio Android backend
pairs (AAudio, Oboe, then OpenSL ES); backend availability depends on the
Termux build and Android version.

Inside a PRoot distribution, the launcher distinguishes the distribution's
Python from the inherited Termux environment and uses a local, unprivileged
project `.venv`. It installs dependencies only inside that environment and
never calls `sudo`, `apt-get` or the host's `pkg`. The venv's interpreter and
`pip` are smoke-tested; a broken or incomplete venv is rebuilt. Audio will work
only if the PRoot environment can access a PortAudio-compatible device bridge.
A successful Python/PortAudio import test does not mean that Android has granted
microphone access or that a real speaker and microphone are available.

`python echocrypt.py --bootstrap rootless` explicitly selects the mobile/rootless
profile. On native Termux it uses the native packages; on PRoot/mobile Linux it
uses the isolated project venv. The `Manual` profile intentionally installs no
packages and exits after creating the venv; the next launch reports exact pip
commands until the core dependencies are installed.

The old `scripts/bootstrap_linux.sh` and `scripts/bootstrap_windows.ps1` remain
as compatibility wrappers; they simply call `echocrypt.py`. To run tests:

```bash
python echocrypt.py --bootstrap full   # only needed on first setup
.venv/bin/python -m unittest discover -s tests -v  # desktop / generic PRoot
python -m unittest discover -s tests -v              # Termux Rootless
```

On Windows, launch `python echocrypt.py`; the same built-in profile selector
creates `.venv` and installs the selected Python dependencies. The PowerShell
wrapper remains optional.

## Configuration in the terminal UI

Open **Configuration** from the main menu. Audio input and output default to
`System default`, which delegates device selection to PortAudio and therefore
follows the OS-selected default input and output rather than choosing the first
enumerated device. Manual device overrides are available in the same menu.
Audio sample rate, gain and recording window, modem rates and tones, payload
size, ACK timeout and retry counts can also be changed there. Values are
validated before being saved to `config/default.toml`.

Under **Configuration → Audio settings**, two hardware diagnostics are
available:

- **Test speakers** plays a 2-second tone through the LEFT output channel,
  pauses briefly, then plays a 2-second tone through the RIGHT channel. The
  selected output must expose at least two channels; mono outputs are reported
  as unsupported for independent left/right verification.
- **Test microphone** opens a live input stream and shows a changing level bar,
  RMS, peak and dBFS, with no-signal and clipping warnings. It computes levels
  in the audio callback and does not record or save the microphone samples.

Both tests use the configured audio devices. When the device is set to
`SYSTEM DEFAULT`, PortAudio uses the operating system's current default.

The main dashboard clears old terminal output before drawing the supplied
ECHO-CRYPT banner. The dashboard width adapts to the terminal size and long
audio-device names are truncated to fit the current box width.

## First-time setup and saved connections

Perform these steps on both devices:

1. Run `python echocrypt.py`. The application starts inside its project `.venv` automatically and displays the banner/dashboard on every page.
2. Open **Manage Identity → Create identity** (or use **Start Communication → Connections → Create this device's identity**). Export the public identity from **Manage Identity → Export public identity**. Share only `keys/identity.public.json`; never share `keys/identity.ed25519`.
3. On each device, select **Start Communication → Connections → Add a peer public key**. Import the other device's `identity.public.json`, compare the full SHA-256 fingerprint over a separate trusted channel, type `TRUST`, and assign a display name. The application stores the public key in `keys/peers/` and the contact index in `config/contacts.json`. When upgrading from Stage 6, Connections also offers **Import existing keys/peer.public.json**; this still requires fingerprint comparison and an explicit `TRUST` confirmation.
4. Select the saved peer from **Connections**. One side chooses **Transmit first** and the other chooses **Listen first**. These are acoustic handshake order choices, not client/server roles.
5. After mutual authentication, the application opens a chat-style screen. The local username appears in the header and is exchanged inside the encrypted session. The first run assigns `anon-NNN`; change it under **Configuration → Profile / username**. The peer-provided name is a display label only; the pinned cryptographic fingerprint remains the authentication identity.

Peer names and local usernames are display labels; the pinned cryptographic fingerprint is what authenticates the peer. The peer must already be saved and trusted before the handshake. Removing a contact deletes only the local pinned public-key copy; it does not affect the other device.

## Chat screen, continuous reception, and session modes

The chat screen no longer asks the receiver to select TEXT, FILE or VOICE for
every incoming item. `AudioSessionEngine` keeps one input stream alive while
the chat is idle, validates each acoustic frame, and routes it to the matching
protocol handler. Incoming Direct text and file packets are authenticated and
acknowledged automatically. A file is saved only after all chunks and the
final SHA-256 check succeed.

- **Message field + Enter:** send a Direct encrypted text message or an open
  Broadcast text message.
- **Attach file / image [F2]:** send any file type. Direct uses authenticated
  chunk transfer, per-chunk ACK/retry and resume state. Broadcast uses
  numbered chunks, a SHA-256 manifest and optional selective retry requests.
- **Voice note [F4]:** record and send an audio attachment. It uses the active
  session mode's file-transfer path; it is not a full-duplex live voice call.
- **Cancel voice [F7]:** a clickable cancel action is shown while recording,
  encoding, queued or transmitting. It discards an active recording or stops
  queued/future transfer frames; acoustic frames already broadcast cannot be
  recalled from nearby receivers.
- **Emergency [F5]:** retains the older Direct-only delayed PTT workflow.
- **More [F6]:** retains the legacy Direct experimental FDM and resume tools.
- **Disconnect [Esc]:** stops the listener and transmit worker, cancels
  outstanding waiters and closes the Direct session. The chat transcript is
  session-local and is not a persistent archive.

The microphone listener runs automatically while the chat is idle. During a
local transmission it pauses capture, because one speaker/microphone device
cannot reliably separate its own loud playback from a weak incoming signal.
The animated activity indicator is a hollow arrow containing three rotating
circles. The footer uses explicit two-line shortcuts: Enter Send, F2 File,
F4 Voice, Space Hold to talk, F5 Emergency PTT, F6 More, F7 Cancel and Esc
Disconnect. Voice-note activity uses a single replaceable status row with
recording/encoding/queue/transmission phase and chunk progress; detailed debug
trace remains file-only. This remains half-duplex acoustic communication; the
user does not operate the receive side manually.

An attachment above 1000 bits (125 bytes) displays a size/airtime warning in
the chat transcript. The warning does not block sending.

### Direct mode

Direct requires the existing mutual Ed25519/X25519 handshake and uses separate
directional ChaCha20-Poly1305 keys, authenticated ACKs, anti-replay checks and
keyed frequency hopping. A single session engine owns receive, packet routing
and a FIFO transmit queue. This prevents concurrent UI/background sends from
racing the same AEAD nonce counter or overtaking one another. Chat data starts
at 400 symbols/s, then retries at 200 and 100 symbols/s after an absent or
invalid authenticated ACK; each response is sent at a rate the listener is
already monitoring. File data is split into 256-byte chunks by default and
uses the same bounded robust-rate plan.

### Open Broadcast mode

Select **Join open Broadcast room** from Connections. It requires no saved
identity, peer import, handshake, membership approval, private group key or
ACK for each packet. Every nearby compatible ECHO-CRYPT instance can listen
and send. Frames carry a short sender ID, message ID, type, fragment index/count,
length and CRC; these fields are necessary for synchronization, routing,
duplicate suppression and selective recovery, so the header is compact rather
than removed.

Broadcast uses a fixed public compatibility transform shared by all builds.
It requires no key exchange and any compatible ECHO-CRYPT instance can decode
it. The transform is **not secret**: a person who inspects the software or
implements the protocol can decode and forge Broadcast traffic. Its deterministic
frequency-hopping schedule is also public; it improves frequency diversity and
keeps the signal from being a single fixed tone pair, but it does not prevent a
capable receiver from following the waveform. It only keeps an unaware recorder
or generic audio player from directly displaying the payload as application
data; it must not be used for private or sensitive information.

Broadcast has no mandatory delivery ACK. Receivers can request missing text
fragments, file metadata or file chunks by sender/message ID. The sender keeps a
bounded recent-frame cache and retransmits only requested pieces. Text can
appear as an incomplete preview with explicit missing-fragment markers and is
updated in place when repair completes. Files are not committed until their
manifest, chunk layout, length and SHA-256 all validate. The current Broadcast
file-size limit is 8 MiB.

Multiple devices share one acoustic channel. Listen-before-talk plus random
backoff reduces collisions, while sender IDs and message IDs prevent the same
received data from being inserted twice. This cannot eliminate collisions from
hidden devices or two transmitters starting at nearly the same time. Broadcast
is an open group, not a coordinated full-duplex radio network.

## File transfer and resume

In Direct mode, choose **Attach file [F2]** in the chat. The other peer does
not select a receive operation: the background listener detects the
manifest/chunk frame types and routes them automatically. Files are handled as
opaque bytes; there is no content-type restriction, so legacy `.ppt` and modern
`.pptx` presentations transfer like any other file. Files are split into
bounded 256-byte chunks by default, each encrypted independently and
acknowledged before the next chunk is sent. The sender retries at lower symbol
rates after a missing ACK. The receiver writes chunks to a partial file and
state record, then verifies the final SHA-256 before placing the file in
`downloads/`.

If a transfer stops, the sender displays a 32-character transfer ID. On retry,
enter that ID at the sender; the receiver advertises the first missing chunk.
Partial receive data is stored in `.echocrypt-state/incoming/`. Keep both the
sender's outgoing state and receiver's partial state if you intend to resume.

Files are limited to 256 MiB by default. Increase this only after considering
storage capacity and the time required for acoustic transfer.

## Voice messages and emergency PTT

**Voice note [F4]** captures mono audio, low-pass filters and resamples it to
3 kHz, then stores it as a compact ECV1 IMA ADPCM file at 12 kbit/s. Compared
with the previous 4 kHz profile, the encoded payload is about 25% smaller for
the same duration. This deliberately trades some speech brightness for a
shorter acoustic transfer. The received audio is upsampled for common device
playback and can be saved as a standard `.wav`; upsampling does not restore
frequencies removed during recording compression.

**Emergency PTT** repeatedly records two-second bursts, sends each burst, and
lets the receiver remain armed for the next one. When the optional system
`libcodec2` is available, the burst uses Codec2 700C (28 bits per 40 ms frame).
The API's four-byte frame alignment makes the stored ECV2 stream about 213 bytes
for two seconds of speech. Codec2's 700C mode is intentionally very low bitrate
and can sound robotic; test speech intelligibility in the language you use.
With the 1500-baud emergency profile, the
ideal synthetic airtime for its manifest, control frames, data and ACK is about
4.3 seconds per two seconds of speech; real acoustic delay and retries add more.
This is delayed half-duplex push-to-talk, not true real-time or full-duplex
conversation. The emergency profile disables FHSS to conserve airtime, but
ChaCha20-Poly1305 encryption and authentication remain active. If `libcodec2`
is unavailable, the app falls back to ADPCM and the transfer is substantially
slower. Codec2 is dynamically loaded from the system and is not bundled; the
upstream Codec2 project distributes its library under LGPL terms.

## Hybrid FDM

Choose **Send text + file/voice simultaneously (FDM)** and the corresponding
receive option on the other peer. The file manifest is exchanged first. During
the media phase, an encrypted text packet is transmitted on the 1800/2400 Hz
lane while the encrypted media chunk is transmitted on the separate 600/1000 Hz
lane. Both waveforms are summed into one composite audio signal. The text
ciphertext is repeated with each media chunk so both lanes are active together;
the receiver shows the text only once and re-ACKs duplicates.

FDM is experimental. Speaker/microphone frequency response, room acoustics,
AGC, echo cancellation, noise suppression and resampling can cause crosstalk or
loss. The current FDM profile uses fixed bands and Hamming/Repetition-3 FEC; it
does not currently combine FDM with symbol hopping.

## Modem rates and realistic throughput

The Direct chat sender starts at 400 symbols/s and falls back to 200 and 100
symbols/s when an authenticated response is missing. The persistent listener
monitors those rates plus the configured legacy fast rate. This is intentionally
more conservative than sending every file chunk at 800 symbols/s.
Theoretical useful capacity before packet headers, CRC, ACKs and retries is
approximately 114 bit/s at 200 symbols/s, 229 bit/s at 400 symbols/s, 457
bit/s at 800 symbols/s, 686 bit/s at 1200 symbols/s and 857 bit/s at 1500
symbols/s for the listed FEC model.
The emergency mode uses un-FEC-coded payload bits on the first attempt and relies
on CRC/AEAD rejection plus stronger-FEC retries. These are coding-rate
calculations, not measured throughput.

A 1 MiB file therefore takes many hours over this link. Even with Codec2 700C,
voice remains a delayed PTT exchange rather than real-time duplex audio. Do not
assume that selecting 1200 or 1500 baud works through a real microphone/speaker
path: verify both ends with a small file first.

## Security and operational limitations

- This is a prototype; do not use it for life-critical or highly sensitive
  communications without an independent security review and physical tests.
- Ed25519 private identity is still stored unencrypted at rest. Protect
  `keys/identity.ed25519` with OS permissions and backups.
- The peer fingerprint must be verified out of band; receiving a public key
  file alone does not authenticate the intended person.
- FHSS improves frequency diversity/interference tolerance; it is not a
  substitute for encryption and does not hide traffic from a capable observer.
- Direct retries are stop-and-wait. Broadcast uses public selective-retry
  requests but has no congestion-controlled membership or coordinator. There
  is no multipath, secure group-key management or full-duplex audio streaming.
- True live/full-duplex voice is not implemented. Emergency PTT uses delayed,
  two-second half-duplex voice bursts. The code does not promise real-time voice
  at current acoustic rates.
- FDM is synthetic-loopback tested only. No guarantee is made for Zoom, Discord,
  Meet, virtual cables or consumer audio devices.
- `sounddevice`/PortAudio hardware I/O was unavailable in the build environment.
  Codec2 700C was tested against the installed system library here, but both
  peers must have a compatible runtime to encode/decode ECV2 voice clips.
  Automated modem tests synthesize and decode waveforms in software.

## Windows

```powershell
.\scripts\bootstrap_windows.ps1
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
python echocrypt.py
```

The Windows bootstrap presents the same Full, Standby and Manual profiles.

## Tests

```bash
python -m unittest discover -s tests -v
```

Tests cover cryptography, handshake, replay defense, five-second cooldown before
every HELLO-I/HELLO-R initial or retry playback, duplicate HELLO-I recovery, F8
retry, Termux-vs-PRoot detection, Rootless bootstrap selection, import/PortAudio
self-tests, broken-venv repair and Termux no-sudo setup planning, Hamming/Repetition-3/raw and convolutional/Viterbi FEC,
soft decisions, ECF2 header redundancy/CRC,
legacy ECF1 receive compatibility, fine sample-clock recovery, final-symbol
waveform guarding, frequency-offset recovery, keyed frequency hopping,
1500-baud emergency modem frames, hybrid FDM waveform
demultiplexing, ADPCM and optional Codec2 700C voice roundtrips, resumable
binary transfer, filename traversal rejection, and synthetic acoustic loopback.
They do not replace a test between two physical audio devices.

## Stage 7 — Chat-first interface and contact manager

The ASCII banner and compact dashboard are part of every full-screen UI page. The compact header is sized to leave room for the main menu even in a standard 24-row terminal. Operation pages clear stale output before displaying progress; the connected-peer label is used when drawing the progress page. The empty chat view includes a first-use guide, and receive choices explicitly describe which sender action they match. The dashboard includes the generated or configured local username, identity status, audio defaults and transport profile.

`Start Communication` opens a Connections page listing saved peers. Add a peer by importing the other device's public identity and verifying its fingerprint once; the public key is pinned locally. The authenticated session opens a chat-style page with a text input, attachment button, receive button, voice-note action, Emergency Connection PTT action and an experimental FDM submenu. The local username is saved in `config/profile.json`; its default format is `anon-NNN`.

This section describes the older Stage 7 interaction model and is superseded by
Stage 9: the current chat has a persistent background listener and does not
require a matching manual Receive action for each message. The acoustic link is
still half-duplex per device. Emergency remains delayed PTT voice bursts, not a
true real-time call.


## Stage 8.7 — Post-handshake profile reliability

- The encrypted display-name exchange now starts with convolutional/Viterbi FEC.
- A receiver that cannot decode/authenticate one profile copy remains armed for
  the sender's bounded FEC retries instead of exiting the profile exchange.
- ASRE reports ADC clipping before decode and will not grade a clipped channel
  as GOOD. Reduce microphone gain/speaker volume when clipping is reported.

## Stage 8 — Adaptive Signal Recovery Engine (ASRE)

The modem receive path now uses ASRE components under `core/recovery/`:

- **Channel analysis:** preamble tone contrast, normalized bit confidence,
  signal RMS/peak, clipping fraction, approximate carrier-frequency offset,
  preamble timing displacement, and the sample-clock correction that succeeded.
- **Soft decisions:** each BFSK symbol produces a signed confidence metric instead
  of being immediately rounded to a hard zero/one. Hamming and Repetition-3
  consume these soft values; the convolutional decoder uses soft-decision
  Viterbi.
- **Header protection:** ECF2/ECF3 add a CRC32 to the fixed modem header,
  encode it with Hamming(15,11), and transmit two copies. The receiver combines
  both confidence streams and validates the header CRC before trusting payload
  length, FEC mode or hopping seed. ECF3 additionally interleaves payload soft
  decisions at depth 16 before decoding to spread burst errors.
- **Synchronization and frequency recovery:** a non-periodic 64-symbol preamble
  is searched around the detected onset. A bounded frequency-offset search and
  fine sample-clock resampling candidates are then tried; the clock search
  checks small 50–500 ppm corrections before wider fallbacks. Debug output
  reports timing-peak separation and the width of ambiguous timing plateaus.
- **Waveform guards:** the first and final encoded symbols are no longer faded.
  A separate non-data trailing guard carries the fade-out, protecting the final
  FEC/CRC symbols from amplitude tapering.
- **Critical handshake FEC:** HELLO and FINISH control frames begin with
  convolutional rate-1/2 FEC and soft Viterbi decoding. General data transfers
  retain their ACK-driven FEC retry progression.
- **Adaptive profiles:** the successful frame's measured channel grade selects a
  bounded profile for the next receive operation. A balanced fallback remains
  available when a previously good channel suddenly degrades.
- **Retry FEC controller:** the sender changes FEC after an ACK failure. This is
  based on feedback the current protocol actually has; receiver-only channel
  metrics are not assumed to be known by the sender.

The current ASRE implementation does **not** yet include Reed-Solomon coding,
continuous full-duplex channel estimation, or channel metrics embedded in ACKs.
It also cannot recover information that was erased beyond the selected code's
correction capability; the existing authenticated ACK/retry path remains the
end-to-end recovery mechanism.

ECF3 is the default transmit format in Stage 9.2. The receiver accepts ECF3,
ECF2 and legacy ECF1. An older ECF1/ECF2-only peer cannot decode ECF3 frames;
upgrade both communicating devices together before testing Direct or Broadcast.
The ECF2 header layout is retained inside ECF3, but the ECF3 preamble and
interleaved payload require an ECF3-capable receiver.

## Acoustic debug (file-only)

Open `Configuration -> Acoustic debug logging` and turn it ON before entering a chat or starting a receive/handshake attempt. Diagnostic events are written only to `logs/acoustic-debug.log` (rotated after 1 MB). **They are never printed live to the terminal and never rendered inside the chat window.** The chat keeps displaying only its compact one-line activity state (for example, `LISTENING`, `TX`, `RX`, or chunk progress).

To inspect diagnostics, leave the chat and open `Configuration -> View latest acoustic debug log`, or read `logs/acoustic-debug.log` directly. The stored trace includes microphone RMS/peak, tone detection, ECF1/ECF2/ECF3 synchronization, FEC, CRC, recovery attempts, Broadcast routing and delivery. `NO MODEM TONE` means the input stream was alive but expected BFSK frequency bands were not detected; `SYNC`, `FEC`, and `CRC` entries show later decode stages. Debug mode never logs plaintext message bodies, private keys, or session keys.

### Handshake timing and retry (Stage 8.5)

Every outgoing handshake control transmission has a five-second countdown
immediately before the playback call: `HELLO-I`, `HELLO-R`, `FINISH-I`, and
`FINISH-R`, including the existing HELLO retries and cached `FINISH-I` retries.
The countdown explicitly tells the peer operator to press Enter and arm the
microphone. F8 retransmits the cached HELLO-I while waiting for HELLO-R, or the
cached HELLO-R while waiting for FINISH-I. Those retries do not create a new
ephemeral key or handshake transcript. FINISH-R is the final handshake flight;
there is no F8 retry after it because the responder has already entered the
ESTABLISHED state. The cooldown is completed before the audio playback pipeline
is entered.

## Receiver arm window before new reliable data

Before sending a new reliable text DATA frame, the sender prints the frame
length/FEC/airtime estimate and then waits five seconds. The countdown tells
the peer to press Enter and arm its microphone; only after the countdown does
waveform playback begin. Retries repeat the same arm window. File/FDM manifests
use the same window because they begin a manually armed receive operation. ACKs
and subsequent file chunks are not delayed: their peer is already in an
automatic receive loop.
