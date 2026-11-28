# ECHOCRYPT Android

Standalone native Android app. No Termux, terminal emulator, Python runtime, or companion app is required.

## First implementation milestone
- Kotlin + Jetpack Compose application shell.
- Runtime microphone permission.
- Native 48 kHz mono PCM microphone capture via AudioRecord.
- Native 1,800 Hz output diagnostic via AudioTrack.
- Input level meter and explicit audio errors.

**Not implemented yet:** ECHOCRYPT modem/framing, Direct/Broadcast session orchestration, cryptographic handshake, file transfer, voice messages, or protocol interoperability. This is an Android platform foundation, not yet a functional acoustic messenger.

## Build
Requirements: Android SDK 35, JDK 17, Gradle 8.9+.
From this directory run `gradle assembleDebug`.
APK output: `app/build/outputs/apk/debug/app-debug.apk`.

## Boundaries
- MainActivity: UI and runtime permission.
- audio/AndroidAudioEngine: Android PCM device I/O.
- Protocol code must be integrated behind a UI-independent API; Compose must not own modem, crypto, transport, or file-transfer rules.

All files in this milestone are under the android/ directory. Desktop Python modules and CLI are untouched.
