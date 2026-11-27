# ECHO-CRYPT Roadmap

This roadmap separates implemented behavior from work that still needs evidence. A roadmap item is not a claim that the feature already works.

## Current baseline

The current `main` branch contains an experimental acoustic messaging client with:

- BFSK acoustic framing and ECF wire-format support.
- Continuous audio reception and a session-oriented application flow.
- Direct authenticated communication using Ed25519 identities, ephemeral X25519 key agreement, HKDF-SHA256, and directional ChaCha20-Poly1305.
- Open, intentionally unauthenticated Broadcast.
- Text, file, and voice-note workflows.
- Chunk tracking, retries/repair mechanisms, resume state, and SHA-256 file validation.
- ASRE recovery modules for synchronization, timing/frequency recovery, soft decoding, and FEC selection.
- A Python SDK and a terminal interface.

These are codebase capabilities, not a guarantee of success across all acoustic hardware. Physical-device interoperability remains a separate validation requirement.

## Phase 1 — Interaction and platform stability

**Goal:** Make the existing client predictable on desktop and mobile terminals.

- [x] Reduce lone-ESC response delay in the chat application.
- [x] Apply short ESC-sequence and key-chord timeouts to menu applications.
- [x] Use a compact header on narrow terminal widths.
- [x] Remove fixed wide minimums from chat history layout.
- [x] Add regression tests for menu timeout configuration.
- [ ] Test keyboard behavior in Linux terminals, Windows Terminal, and Termux.
- [ ] Test viewport resize while the chat is active.
- [ ] Verify touch keyboards and external Bluetooth keyboards on Android.

**Exit criteria:** ESC consistently returns/disconnects promptly, ANSI navigation keys still work, and no horizontal overflow occurs at common 32-, 40-, 48-, 80-, and 120-column widths.

## Phase 2 — Protocol and transfer reliability

**Goal:** Demonstrate complete message and file lifecycles, not just successful packet decoding.

- [ ] Build deterministic end-to-end tests for Direct text, Broadcast text, Direct file, Broadcast file, and voice-note delivery.
- [ ] Exercise packet loss, duplicated frames, reordered chunks, truncated transfers, corrupted manifests, and receiver restart.
- [ ] Verify cancellation at queued, active, waiting-for-ACK, and resume states.
- [ ] Verify no cancelled transfer silently continues scheduling new chunks.
- [ ] Verify receiver commits files only after final digest validation.
- [ ] Test large-file resource bounds and malformed length fields.

**Exit criteria:** Tests demonstrate either verified completion or a clear terminal failure state; no partial/corrupt file is reported as complete.

## Phase 3 — Acoustic interoperability

**Goal:** Validate real speakers and microphones under controlled conditions.

- [ ] Two-device Linux test with different audio hardware.
- [ ] Linux-to-Windows and Windows-to-Linux interoperability.
- [ ] Android/Termux capture and playback validation where the audio backend supports it.
- [ ] Measure packet error rate, synchronization success, retry count, throughput, and end-to-end latency.
- [ ] Repeat at multiple distances, volume levels, orientations, and room-noise conditions.
- [ ] Test OS audio processing, echo cancellation, automatic gain control, and resampling effects.

**Exit criteria:** Publish reproducible measurements and clearly state supported hardware/software combinations. Synthetic loopback alone does not pass this phase.

## Phase 4 — ASRE calibration and bounded recovery

**Goal:** Improve recovery without accepting corrupted data.

- [ ] Build a labelled acoustic corpus with clean, noisy, clipped, reverberant, and resampled recordings.
- [ ] Measure false-sync rate and false-accept rate in addition to successful decode rate.
- [ ] Tune candidate search thresholds against the corpus, not anecdotal samples alone.
- [ ] Verify FEC selection is bounded in CPU, memory, and execution time.
- [ ] Add regression fixtures for each fixed real-world capture.
- [ ] Keep CRC/AEAD validation mandatory after recovery.

**Exit criteria:** Demonstrated improvement in recovery rate with no increase in invalid-frame acceptance in the regression corpus.

## Phase 5 — Security hardening

**Goal:** Reduce risks before broader deployment.

- [ ] Independent review of handshake transcript binding, key derivation, nonce construction, replay windows, and protocol state transitions.
- [ ] Add encrypted-at-rest storage for private identity keys, with documented recovery and migration.
- [ ] Review sensitive debug/log paths and ensure payload/key material is never written.
- [ ] Threat-model acoustic replay, injection, jamming, traffic analysis, and physical access.
- [ ] Document Broadcast spoofing and collision risks prominently.
- [ ] Define protocol versioning and downgrade behavior.

**Exit criteria:** Security review findings are addressed and the key-storage migration has tested backup/recovery behavior. No security audit should be claimed before one is completed.

## Phase 6 — Performance and usability

**Goal:** Make slow acoustic transfer observable and manageable.

- [ ] Profile CPU and memory on low-power Android/Linux devices.
- [ ] Add clear estimated/observed throughput and transfer progress.
- [ ] Improve queue visibility and per-item cancellation feedback.
- [ ] Benchmark codec choices and symbol rates across actual devices.
- [ ] Evaluate optional FDM only after baseline single-channel reliability is measured.
- [ ] Keep half-duplex behavior explicit until a tested full-duplex design exists.

**Exit criteria:** Published benchmarks, predictable resource use, and transparent transfer state on desktop and mobile terminals.

## Phase 7 — Release readiness

**Goal:** Produce a release supported by evidence.

- [ ] Run the full automated test suite in CI on supported Python versions and operating systems.
- [ ] Run static compilation/lint checks and dependency/security review.
- [ ] Complete the physical-device test matrix.
- [ ] Verify installation/bootstrap from a clean Linux, Windows, and supported Termux environment.
- [ ] Review README, architecture, security policy, and migration notes together.
- [ ] Tag a release only after all required exit criteria are met.

## Explicit non-goals for the current baseline

- Native Android GUI application.
- Full-duplex, real-time voice calls.
- Confidential or authenticated Broadcast.
- Guaranteed delivery in arbitrary acoustic environments.
- Anonymity from frequency hopping.

These require separate designs and evidence; they must not be implied by the current feature set.

## Phase 8 — Generic virtual audio and selected-tab capture

- [x] Linux PulseAudio/PipeWire-Pulse virtual TX microphone and RX speaker endpoints.
- [x] Route only explicitly selected application audio into the ECHOCRYPT RX monitor.
- [x] Optional per-endpoint loopback so the selected application remains audible on physical speakers.
- [x] Keep ECHOCRYPT TX/RX isolated and allow full-duplex modem I/O only on the virtual route.
- [x] Dedicated machine-local worker hardware approval, invalidated by device/rate/tone changes.
- [x] Implement Chromium extension + Native Messaging host for one user-selected tab, with a dedicated PCM sink and normal-speaker pass-through.
- [ ] Physically validate tab capture through real meeting providers and add Firefox support.
- [ ] Add Windows virtual audio backend and installer.
- [ ] Design an Android-native audio backend; stock Termux PortAudio/OpenSL ES must be tested separately.

**Exit criteria:** The selected application must carry ECHOCRYPT packets between two independent endpoints without disturbing unrelated system audio. Test actual conferencing codecs and noise-suppression settings. A successful local virtual loopback is not proof that remote audio services preserve modem frames.
