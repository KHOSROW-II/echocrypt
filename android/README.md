# ECHOCRYPT Android

Standalone native Android app. No Termux, terminal emulator, Python runtime, or companion app is required.

## Current implementation

Android CI installs an explicit SDK package set; the generated APK is a development artifact, not a signed production release.

- Kotlin + Jetpack Compose native application shell.
- Runtime microphone permission and native 48 kHz mono PCM capture via AudioRecord.
- Native AudioTrack playback and output diagnostic.
- ECF3 BFSK frame encoder ported to Kotlin, including protected header, CRC32, Hamming(15,11) header coding, terminated rate-1/2 convolutional payload FEC, both interleavers, ECF3 sync marker, and 48 kHz waveform generation.
- UI action to transmit a diagnostic ECF3 frame through the Android audio output.
- Android unit tests for deterministic frame generation and ECF3 symbol/sample layout.

## Important limitations

This is not yet a complete acoustic messenger. Receive-side modem detection/demodulation, ASRE recovery, authenticated frequency hopping, encrypted session/handshake, peer identity, Direct/Broadcast orchestration, file transfer, and voice messaging are not connected yet. The diagnostic frame sent from the UI is explicitly **unencrypted** and is not a secure message.

Frequency hopping is deliberately not emitted by this encoder until the authenticated hop-key lifecycle is integrated. It must never silently fall back from a requested hopped mode.

## Build

Requirements: Android SDK 35, JDK 17, Gradle 8.9+.
From this directory run `gradle testDebugUnitTest assembleDebug`.
APK output: `app/build/outputs/apk/debug/app-debug.apk`.

## Architecture

- `MainActivity.kt`: presentation, permission flow, and UI actions.
- `audio/AndroidAudioEngine.kt`: Android PCM capture and playback.
- `protocol/NativeModem.kt`: pure Kotlin ECF3 transmit framing and BFSK modulation.
- Protocol components must remain independent of Compose. The desktop Python implementation remains untouched; cross-platform compatibility must be demonstrated with shared test vectors and real-device loopback/interoperability tests before calling the Android client compatible.

All Android implementation work in this milestone is under `android/`.
