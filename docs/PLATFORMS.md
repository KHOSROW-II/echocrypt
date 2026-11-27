# Desktop and browser platform matrix

## Desktop targets
- Linux x64: standalone ECHOCRYPT console binary, native runner build.
- Windows x64: standalone ECHOCRYPT console executable.
- macOS Apple Silicon and Intel: standalone console binaries, native architecture runners.

These are CLI executables, not a GUI installer, MSI, or DMG application bundle. Code signing and notarization are not configured, so SmartScreen/Gatekeeper warnings may appear.

Frozen builds store mutable configuration in the operating system's per-user ECHOCRYPT configuration directory. They do not write beside the executable.

## Browser packages
- Chrome and Edge: Chromium Manifest V3 adapter based on tabCapture/offscreen document.
- Firefox: Manifest V2 adapter captures supported HTML audio/video media elements with captureStream/mozCaptureStream. It is not a general capture of every possible tab sound, protected media, WebAudio-only content or browser chrome.
- Each browser has a distinct Native Messaging registration and origin allowlist.

## Audio routing
Linux uses PulseAudio/PipeWire-Pulse null sinks. Windows/macOS require user-installed virtual audio endpoints. Configure the RX capture endpoint as the ECHOCRYPT input and a separate TX playback endpoint as ECHOCRYPT output. The Native Messaging host audio_output_device must point to the RX virtual playback endpoint. Keep full-duplex disabled for external routes to avoid feedback. The browser tab remains audible through its normal speaker output.

No driver is silently installed. Real browser/provider/driver validation is required before claiming production-ready audio routing.

## Release gate
The release workflow has a seven-target test matrix: Python 3.10-3.13 on Ubuntu, Windows, macOS arm64 and macOS x64. All test jobs must pass before build jobs can start. The release job depends on both test and build and creates/updates a GitHub Draft Release only.
