## Android Native integration — 2026-11-28

- Added the standalone Kotlin/Jetpack Compose Android client on the independent
  `android` development branch. Android is built as a separate source input;
  this release does not merge the Android branch into `main`.
- Android Direct uses the native ECF3 acoustic modem, ECH1 authenticated peer
  handshake, ECP1 encrypted channel, and encrypted reliable packet transport.
- Added persistent resumable file transfer with receiver-side chunk maps,
  final SHA-256 verification, and authenticated application-level COMPLETE
  confirmation. Maximum file size is 10 MiB.
- Added a bounded FIFO message queue and a two-fragment sliding window for
  reliable transport. The receiver reassembles out-of-order fragments; the
  sender retransmits missing fragments within a bounded retry policy.
- Android CI passed on commit
  `ed110361a19338040ea1798687951cf2ef45b514`; native unit tests and the
  unsigned Debug APK build completed successfully.
- The APK is a development build, not a production-signed package. Real-device
  two-phone acoustic transfer, resume-under-interruption, and throughput
  measurements have not yet been validated. The Direct modem currently uses
  the conservative 200-baud profile and a two-fragment window.

## Final — Broadcast voice receive synchronization fix

- The acoustic modem now retains a bounded set of independently refined ECF3,
  ECF2 and legacy ECF1 preamble candidates. A stronger false cross-format peak
  can no longer hide a weaker real preamble later in the same capture.
- Candidate discovery accepts normalized preamble scores down to 0.24, but this
  is not an integrity bypass: the version-matched protected header and final
  payload CRC must still validate before the packet reaches the Broadcast
  router. Invalid candidates are discarded and the next candidate is tested.
- A header that passes its CRC but belongs to the wrong wire format no longer
  triggers expensive sample-clock recovery retries.
- Added a regression test reproducing the observed ECF2 false-winner / ECF3
  valid-candidate sequence, including a valid preamble score of 0.289 that was
  previously rejected by the fixed 0.30 threshold.
- Verification: 36 focused modem, Broadcast voice and reliable-text tests pass;
  Python compilation checks pass. Physical two-device audio testing has not
  been performed for this build.

## Stage 9.8 — Compact voice notes and duplicated per-fragment headers

- Broadcast `.ecv` notes now prefer Codec2 1300 bit/s (ECV3) when `libcodec2`
  is available. If it is not installed, the sender uses the built-in IMA ADPCM
  ECV1 fallback. Codec2 is therefore an optional size/speed improvement, not a
  runtime requirement for the rest of ECHO-CRYPT.
- Voice Broadcast uses 800 baud when supported and 192-byte data fragments;
  the manifest is sent once rather than retransmitted blindly. Missing manifest
  or data fragments are requested selectively and only the requested frames
  are returned from the resend cache.
- Added Broadcast packet v2 with a per-fragment ECFH descriptor duplicated on
  both sides of the encrypted fragment. Each descriptor copy has its own CRC;
  one damaged copy can be discarded, while the surviving descriptor is bound
  into AEAD associated data. Both invalid or disagreeing copies are rejected.
- Retry request parsing now carries request flags and exact fragment indexes
  through the live router and retransmission cache. Fixed a live-path type error
  that rejected an already-parsed request tuple (`bytes` cannot be interpreted
  as an integer).
- Added playback actions for sent and received `.ecv` chat entries: click
  `Play voice` under the bubble or press F9. Decoding creates an adjacent
  standard WAV file and plays it through the selected output device.
- Moved voice playback completion updates back onto the UI event loop, so the
  generated WAV path/status is shown reliably without a worker-thread UI update.
- Added modem-to-modem synthetic waveform coverage at 800 baud with noise,
  repeated-header corruption tests, selective-retry tests, Codec2 payload-size
  checks, and chat playback-action tests.
- Verification is software/synthetic-audio only; no physical two-device
  speaker/microphone transfer is claimed.

## Stage 9.6 — Noise-tolerant preamble acquisition and immediate Escape

- Fixed a receive-side synchronization failure seen in real Broadcast traces: broadband room noise could satisfy the relative-energy gate before the actual modem burst, while the demodulator searched only 12 symbols beyond that false early activity point.
- The demodulator now scans the complete 850 ms continuous-listener pre-roll window and performs coarse/fine preamble refinement independently for ECF3, ECF2 and ECF1. A strong noise peak for one format can no longer prevent refinement of another format at a different sample offset.
- Added a minimum normalized preamble confidence of 0.30. Weak random correlations (for example 0.16–0.19 in the reported traces) are rejected before parsing a header or spending time on timing-recovery permutations. A legitimate 800-symbol/s case at 0.349 remains accepted by regression coverage.
- ASRE stops timing-ratio retries immediately when no credible preamble exists; the session may then move on to another supported symbol rate. This avoids a retry storm on room noise without weakening CRC or AEAD validation.
- Reduced prompt-toolkit Escape parsing timeouts (`ttimeoutlen=60 ms`, `timeoutlen=120 ms`) so a standalone Esc is not held while waiting for a possible terminal escape sequence.
- Escape now marks the session stopped immediately and performs PortAudio/thread teardown in a background cleanup worker. Removed the extra Enter-to-continue pause on leaving chat.
- Added regression tests for a valid 400-baud frame after broadband pre-roll noise, rejection of noise-only captures without clock-recovery retry storms, fast Escape handling, and safe restart after asynchronous shutdown.
- Verification is software/synthetic-audio only; no two-device speaker/microphone hardware result is claimed.

## Stage 9.4 — Broadcast diagnostics fix

- Fixed the missing diagnostics callback in the continuous `AudioSessionEngine` receive path.
- Added microphone/listener, demodulator, FEC/CRC, frame-routing and Broadcast protocol logs.
- Added Broadcast TX, fragment/reassembly, duplicate suppression, file verification and retry-request diagnostics.
- Preserved metadata-only logging; no message plaintext or cryptographic keys are logged.
- Added end-to-end Broadcast diagnostics regression coverage.

# Changelog

## Stage 9.7 — Voice-note UI cleanup and cancellation

- Removed the remaining recording status `print()` from `_record_voice_file`;
  both the recording helper and ECV encoder now keep stdout clear for the
  full-screen chat UI.
- Broadcast and Direct progress rows now identify the activity as `VOICE NOTE`
  or `VOICE NOTE MANIFEST` rather than exposing the temporary `.ecv` filename
  as a generic attachment label.
- Added a single cancellation path for F7 and the clickable Cancel controls
  while recording, encoding, queued, or transmitting a voice note.
- Cancelling during recording discards captured samples before encoding.
  Cancelling during preparation removes the generated temporary ECV once the
  encoder returns; cancelling a queued job prevents it from going on air.
- Cancelling an active acoustic transfer interrupts local playback where
  possible and stops future chunks. Broadcast chunks already transmitted are
  irreversible and may remain partially available to nearby receivers.
- Added regression tests for silent voice recording helpers, F7 discard while
  recording, clickable cancellation, queued/active playback cancellation, and
  Broadcast/Direct transfer cancellation checkpoints.
- Verification is software-only. No physical microphone/speaker transfer is
  claimed.

## Stage 9.5 — File-only acoustic diagnostics

- Removed live `print()` output from `AcousticDebug.emit()`. Diagnostic callbacks now append to `logs/acoustic-debug.log` only; they do not write into stdout or the prompt-toolkit chat screen.
- Preserved log rotation and the Settings viewer for the latest stored entries.
- Replaced the obsolete settings description that promised live console diagnostics with explicit file-only behavior.
- Simplified the in-chat debug notice to a single status message; individual diagnostic lines remain off-screen.
- Added regression coverage proving enabled acoustic and Broadcast diagnostics persist to disk without calling `print()`.

## Stage 9.2 — Acoustic reliability, readable chat and media sizing

- Added ECF3 framing with a distinct synchronization marker and depth-16
  payload interleaving, while retaining ECF1/ECF2 receive compatibility.
- Direct chat uses a FIFO transmit queue, convolutional/Viterbi-first FEC and
  bounded 400/200/100-symbol/s retry rates; automatic replies use a monitored
  rate to avoid ACK timeout spent probing an unrelated symbol rate.
- Reduced default binary chunk size to 256 bytes and modem transmit gain to
  0.35; neither change substitutes for real hardware tuning.
- Added the hollow-arrow three-circle spinner and readable two-line chat footer.
- Voice notes default to 3 kHz mono IMA ADPCM (12 kbit/s), reducing encoded
  payload by about 25% versus the previous 4 kHz profile. Playback is upsampled
  to 8 kHz for common audio-device support.
- Added a non-blocking warning above 1000 bits (125 bytes) and explicit PPT/PPTX
  coverage in the opaque binary file-transfer path.
- Added tests for ECF3 burst recovery, ECF2/ECF1 compatibility, PPT/PPTX bytes,
  voice-note payload size, spinner rendering and two-engine acoustic transfer.

## Stage 8.8 — Reliable ACK deadlock and crossing DATA fix

- Authenticated ACK control frames now use Convolutional/Viterbi FEC instead of
  the default Hamming profile. This applies to profile, reliable text, file,
  FDM and cross-traffic ACK sends routed through the centralized helper.
- The reliable-text ACK waiter now receives up to the configured packet limit
  rather than the 44-byte ACK size. A peer DATA frame arriving during the ACK
  window is authenticated as DATA, ACKed immediately, and queued for the
  normal application receive path; duplicate DATA is re-ACKed without duplicate
  delivery.
- ACK waits use a single monotonic deadline while processing crossed DATA,
  preventing each crossed frame from resetting the entire timeout.
- Added real PeerHandshake/ReliableTextChannel integration coverage for
  simultaneous bidirectional profile messages, ACK verification, hopped
  Convolutional ACK frames, and a clipped synthetic waveform.
- Full test suite: 150 tests passed. These are software/synthetic-channel tests;
  no two-device hardware result is claimed.

## Stage 8.7 — Profile exchange retry and clipping diagnostics

- Fixed post-handshake encrypted display-name reception: a failed Hamming/CRC
  or AEAD decode no longer aborts the one-shot receive. The receiver re-arms
  automatically for the sender's next FEC retry, with bounded retry count and
  a listen window covering ACK timeout plus the five-second send cooldown.
- Encrypted profile-name messages now begin with convolutional/Viterbi FEC.
  Retry progression is mode-aware (`conv -> repetition3 -> hamming`) and
  retransmits the same encrypted ciphertext rather than creating new plaintext
  or consuming a new AEAD nonce.
- Duplicate profile frames are ACKed again and not delivered twice.
- ASRE reports clipping before decode and classifies >=1% clipping as WEAK,
  preventing strong preamble tone contrast from hiding a saturated input path.
- Added regression tests for retry listening, bounded failure, FEC progression,
  and clipping metrics.



## Stage 8.6 — Reliable DATA cooldown coverage

- Fixed the established-session `RELIABLE DATA` path: packet size/FEC/airtime
  are displayed first, then a five-second receiver-arm countdown completes
  before the modem playback function is called. Applies to every text message
  attempt, including retries and the encrypted profile exchange.
- File/FDM manifest attempts also receive the arm window because their receiver
  waits at an Enter-to-arm prompt. Subsequent file chunks, ACKs, resume status,
  and FDM payload frames remain immediate because the corresponding receiver
  path is already automatically armed; this avoids adding five seconds to every
  chunk and degrading throughput.
- Added timing and call-path regression tests, including a monotonic-clock check
  for at least five seconds before reliable DATA playback.


## Stage 8.5 — Full HELLO/FINISH cooldown coverage

- Fixed the Stage 8.4 omission: `FINISH-I` and `FINISH-R` had bypassed the
  five-second cooldown and were sent directly to the audio playback path.
- Introduced one centralized sender for every handshake control packet. The
  five-second countdown now runs before HELLO-I, HELLO-R, FINISH-I and FINISH-R
  playback, including cached retries.
- Preserved F8 retries at HELLO receive stages. FINISH-R is terminal; no F8
  retry is advertised after the responder has entered ESTABLISHED, because a
  new post-handshake duplicate-control path would be required for safe replay.
- Added regression tests that assert five one-second waits precede playback
  for every handshake label, plus a real monotonic-clock test for FINISH-R.
- Full unit/integration suite: 134 tests passed. Audio output remains mocked in
  these timing tests; acoustic validation still requires two physical devices.


## Stage 8.4 — Enforced HELLO cooldown and bootstrap audit

- Centralized all outgoing `HELLO-I` and `HELLO-R` transmissions (initial, F8
  retry and duplicate-HELLO recovery) through one sender helper. It performs
  five one-second waits immediately before calling the actual playback path.
  FINISH frames are intentionally not delayed.
- Added regression tests that spy on the real wait calls and assert that all five
  waits happen before playback for each HELLO label. Added coverage for the
  responder's duplicate-HELLO-I resend branch.
- Corrected Termux detection to verify the running Python executable/prefix,
  preventing a PRoot interpreter from being misclassified merely because it
  inherited Termux environment variables.
- Fixed dependency self-test parsing so only explicit missing-import markers
  are reported. A failed interpreter probe now fails closed instead of looking
  like an empty missing-dependency list.
- Rebuilds an incomplete/corrupt project `.venv` and verifies that both its
  Python executable and pip actually run before installing or launching.
- On each native Termux launch, refreshes/probes the PulseAudio bridge without
  reinstalling packages; a failed PortAudio self-test triggers Rootless repair.
- Updated Rootless bootstrap guidance and notes: Termux uses native packages
  including `python-pip`; PRoot uses an unprivileged local venv; audio backend
  discovery and PortAudio/device checks are reported separately.
- Full suite and physical Termux audio hardware tests are reported separately;
  no Android device was available in the build environment.


## Stage 8.3 — Initiator cooldown and Rootless mobile bootstrap

- Moved the five-second preparation countdown to the Initiator before its
  first HELLO-I. The prompt tells the peer on the Responder side to press Enter
  and arm the microphone. Responder now sends HELLO-R immediately after
  validating HELLO-I.
- F8 retry of HELLO-I uses the same cached signed packet and repeats the
  five-second setup window; it does not create a new ephemeral key or transcript.
- Added a built-in bootstrap selector to `echocrypt.py`; first launch installs
  the selected profile without requiring the separate bootstrap scripts.
- Added Rootless (mobile). Termux uses its own package manager for native
  NumPy/Cryptography/PortAudio/PulseAudio dependencies and avoids sudo, APT,
  pip self-upgrade and a venv that would hide Termux-managed packages. Generic
  mobile/PRoot environments use an unprivileged project venv without invoking
  host package managers.
- Existing bootstrap scripts now act only as compatibility wrappers.
- Added unit tests for cooldown direction/order, retry timing, Termux detection,
  Rootless package planning and bootstrap argument handling.


## Stage 8.1 — Acoustic capture recovery fixes

- Moved waveform fade-out out of the final encoded symbol and into a non-data
  trailing guard interval, so the final FEC/CRC bits are transmitted at full
  amplitude. Removed the fade-in that previously weakened the first preamble
  symbol.
- Expanded timing recovery with ordered 50–500 ppm candidates near nominal,
  while retaining wider bounded fallbacks. This targets independent audio
  device clock drift instead of jumping immediately by 1000 ppm.
- Replaced the uninformative preamble score-median diagnostic with peak-gap and
  timing-plateau measurements, exposing ambiguous timing peaks in debug logs.
- Critical HELLO and FINISH handshake frames now start with convolutional
  rate-1/2 FEC and soft Viterbi decoding. General data-transfer retry policy
  remains unchanged.
- Updated packet-duration estimates for the non-data trailing guard.
- Added regression coverage for 118-byte convolutional frames under synthetic
  noise, ±100 ppm clock-offset recovery, and full-amplitude final data symbols.
- Physical speaker/microphone verification remains outstanding.

## Stage 8.0 — Adaptive Signal Recovery Engine (ASRE)

- Added the `core/recovery/` package with channel metrics, bounded recovery
  profiles, carrier-frequency estimation, sample-clock retry orchestration,
  soft-decision FEC, interleaving and a retry-oriented FEC controller.
- New transmissions use ECF2 framing: a 64-symbol pseudo-random preamble, a
  duplicated Hamming(15,11) protected header, and an independent CRC32 on the
  header before its length/FEC/hopping values are trusted.
- Added soft-decision Hamming(7,4), rate-1/2 convolutional encoding and soft
  Viterbi decoding, plus depth-8 block interleaving. Existing raw and
  Repetition-3 modes remain available.
- Added bounded carrier-frequency offset estimation and adaptive sample-clock
  candidates. The channel grade selects the next receive profile, with a
  balanced fallback in case channel quality changes suddenly.
- Sender retry progression now uses Hamming → convolutional/Viterbi →
  Repetition-3; raw-first emergency traffic progresses raw → Hamming →
  convolutional/Viterbi → Repetition-3. Retries remain stop-and-wait and depend
  on authenticated ACK success/failure.
- The receiver remains compatible with ECF1 frames. ECF2 transmitters require
  ECF2-capable peers; Stage 7.3 receivers cannot decode ECF2.
- Added ASRE tests for soft decoding, Viterbi, interleaver inversion, duplicated
  header recovery, ECF1 compatibility, carrier offset, channel metrics and FEC
  retry planning. Physical microphone/speaker tests are still outstanding.
- Reed-Solomon coding and channel-metric feedback in ACKs are not part of this
  stage.

## Stage 7.2 — Audio hardware diagnostics

- Added a stereo speaker test under both Audio Configuration and Configuration → Audio Settings. It plays a two-second tone on the LEFT channel, then a two-second tone on the RIGHT channel, with a short pause between them.
- Detects mono/unavailable output devices and reports that independent left/right testing is not possible instead of reporting a false success.
- Added a live microphone level meter showing a responsive level bar, RMS, peak, dBFS and clipping/no-signal warnings.
- The microphone monitor uses a live input callback and does not retain, record or save microphone samples.
- Both tests follow the currently configured device selection, including the operating-system default when set to SYSTEM DEFAULT.
- Added hardware-test unit coverage using mocked audio backends; physical speaker/microphone behavior still needs testing on the user's machine.


## Stage 7.1 — Screen layout and chat usability fixes

- Fixed a runtime `NameError` in the transfer-operation page (`peer_name` was referenced instead of the supplied `peer_label`), which could stop send/receive actions before they began.
- Reduced the full-screen header to a five-row live dashboard under the six-line ASCII banner. The main menu now occupies 23 text rows including its header, leaving it visible in a common 24-row terminal instead of pushing the banner above the viewport.
- Kept the session status inside the compact dashboard so there is no extra tagline row on constrained terminals.
- Added an explicit first-use chat guide for text, file/photo, voice-note and Emergency PTT actions; clarified that the peer must arm the matching receive mode first.
- Improved the Connections first-run order and added a safe Stage 6 peer-key import path. The old `keys/peer.public.json` is imported only after the user re-checks the fingerprint and types `TRUST`.
- Ensured connection/add-peer failures return to a freshly rendered header rather than leaving prior progress text mixed with the next screen.
- Added tests for the 24-row layout, operation-page peer label, and fingerprint-confirmed legacy-key import.

## Stage 7 — Chat-first terminal experience

- Main-menu pages now carry the ECHO-CRYPT ASCII banner and a compact live status dashboard inside the same full-screen view. This prevents the banner from disappearing when the menu takes over the terminal.
- Added page refreshes before line-oriented operations so stale command output does not accumulate between menus and tasks.
- `Start Communication` now opens a saved **Connections** page. Users can import a peer's public identity, verify and pin its fingerprint once, choose a saved peer, and remove outdated peers.
- Added a chat-style authenticated-session UI with a message input, clickable controls and shortcuts: Enter sends text, F2 attaches any file (including images), F3 opens receive/listen choices, F4 records a voice note, F5 opens Emergency Connection (PTT), F6 opens experimental FDM modes, and Esc disconnects.
- Added persistent local username settings. The first run generates `anon-NNN`; users can edit it under Configuration → Profile / username. The local chat transcript is kept for the current session and cleared on disconnect.
- Added tests for profile validation, peer pinning/removal, chat input, keyboard shortcuts, connection-page entries and persistent header rendering.
- Acoustic communication remains half-duplex: the receiver must choose the matching Receive action before the sender transmits. Emergency PTT is delayed voice bursts, not full-duplex live calling.


## Stage 7.3 — Acoustic receive diagnostics and timing recovery

- Added a GUI toggle for acoustic live debug and a viewer for the latest debug log.
- Added a live microphone RMS meter with MARK/SPACE tone-band detection status.
- Added receive pipeline tracing: audio capture, activity detection, preamble correlation, bit extraction, frame header, FEC, CRC, packet delivery, and handshake parser/state.
- Debug output is opt-in and never records plaintext payloads, private keys, or session keys.
- Replaced broadband-only packet triggering with tone-aware triggering and pre-roll trimming.
- Expanded initial sync timing search and added bounded sample-clock correction retries for independent playback/capture devices.
- Clarified the receive prompt: Enter arms the microphone; the peer should transmit only after the receiver reports listening.

## Stage 8.2 — Handshake cooldown and manual retry

- Added a visible five-second countdown on the responder after accepting HELLO-I and before transmitting HELLO-R, giving the initiator time to arm its microphone.
- Added F8 during HELLO-R / FINISH-I receive prompts to retransmit the exact previous handshake control frame, then re-arm the receiver.
- Made HELLO-I and HELLO-R retransmissions idempotent within the existing handshake state machine; cached control frames are reused so retries do not create new ephemeral keys, transcripts, or AEAD nonces.
- Added handling for delayed duplicate HELLO control frames while waiting for the next handshake step.
- Added tests for the cooldown and duplicate-control retry semantics.

## Stage 9 — Continuous chat and open Broadcast

- Replaced per-message manual receive operations in the chat workflow with a
  persistent acoustic listener and a central `AudioSessionEngine` that routes
  decoded packets to Direct text/file handlers or Broadcast reassembly.
- Added a serialized TX queue, asynchronous response waiters, listener pause /
  resume around local playback, multi-rate receive support, channel-busy sensing
  and randomized backoff. Pending transmit jobs and response waiters now fail
  explicitly on session shutdown instead of remaining unresolved.
- Corrected activity detection to monitor every configured FHSS tone offset;
  hopped payload symbols no longer disappear from the activity detector simply
  because they moved away from the two base tones.
- Added an open Broadcast room available without saved peer keys or handshake.
  It uses a public compatibility transform, sender/message IDs, compact frame
  metadata, CRC, fragmented UTF-8 text, arbitrary-file chunks, SHA-256 checks,
  duplicate suppression and optional selective retransmission requests.
- Broadcast file recovery now requests missing manifests even when all chunks
  arrived before the listener joined. Retry-control packet sizes are bounded to
  the modem frame capacity and TX queue. Metadata and partial state expire.
- Direct file chunks prefer the fast symbol-rate profile and fall back to
  slower rates after missing authenticated ACKs. Retries reuse the same
  encrypted application ciphertext.
- Updated chat bubbles to left/right alignment and keep the background-updated
  message list live while the interface refreshes. Stage 9.1 removes the manual
  Receive/F3 action entirely.
- Added Broadcast, session engine, concurrent nonce-allocation and chat-menu
  tests. A full software test result is recorded in the Stage 9 release notes.

### Security and physical-test limitations

Broadcast's shared transform is public and offers no confidentiality or sender
authentication against anyone who knows/implements the protocol. It is for
open compatibility, not private communication. Listen-before-talk cannot
eliminate hidden-terminal collisions. No physical two-device speaker/microphone
certification is claimed for this release.


## Stage 9.1 — Persistent chat input and Broadcast redundancy

- Text sends now remain inside the active chat screen; no screen teardown per message.
- Removed the manual F3 Receive action. Background RX is always active while idle.
- Broadcast uses three copies for short text, two for file manifests, and selective retry for missing file chunks; duplicate copies are suppressed by message/fragment identity.
- Broadcast listen-before-talk uses a shorter randomized contention profile.
- Added an end-to-end UI test proving Enter sends without closing the chat screen.

## Stage 9.3 — Persistent progress, clickable controls and repeatable resend

- Added an always-present one-row animated hollow-arrow spinner; progress updates replace the same row rather than scrolling the chat.
- Unified TX/RX status reporting, including chunk counts, missing data and concurrent TX/RX visibility.
- Made all chat toolbar actions mouse-clickable while preserving keyboard shortcuts.
- Added Space Push-to-talk recording, F4 voice-note toggle, bounded microphone recording and listener pause/resume.
- Added per-message selective resend controls and repeatable Direct/Broadcast missing-chunk requests.
- Added an end-to-end Direct missing-chunk test that completes a partial file and verifies SHA-256.
