# Android audio support and Termux

## What EchoCrypt needs

EchoCrypt does not just need permission to make a recording. Its receive engine opens a long-lived mono `sounddevice.InputStream` and consumes raw PCM blocks continuously. The modem frame detector and BFSK decoder operate on those blocks.

A command that records a short AAC/M4A file is useful for checking that Android's recorder can access the microphone, but it is not a drop-in live input backend for EchoCrypt.

## Why stock Termux can fail

Android requires `android.permission.RECORD_AUDIO` for applications that capture microphone audio through OpenSL ES (see the [Android NDK OpenSL ES permission notes](https://developer.android.com/ndk/guides/audio/opensl/opensl-prog-notes)).

In the upstream Termux source currently referenced by this project review:

- The main [Termux app manifest](https://github.com/termux/termux-app/blob/master/app/src/main/AndroidManifest.xml) does not itself declare `RECORD_AUDIO`.
- The [Termux:API app manifest](https://github.com/termux/termux-api/blob/master/app/src/main/AndroidManifest.xml) does declare `RECORD_AUDIO`. Both current upstream manifests request the same `sharedUserId` value. Android keeps runtime grants for shared UIDs as a shared grant set, so installing a compatible Termux:API package signed for the same Termux distribution can make that permission available to the shared UID. This depends on the installed APK pair and Android's permission state; do not assume it is either granted or unavailable without checking. See [Android's permission-state notes](https://android.googlesource.com/platform/frameworks/base/%2Bshow/d51fbb5b4694ea02a2240255e549d2335a47533c/core/java/android/permission/Permissions.md).
- The Termux [PortAudio package build](https://github.com/termux/termux-packages/blob/master/packages/portaudio/build.sh) disables the PulseAudio backend and adds an OpenSL ES host API. Therefore, creating a PulseAudio source through Termux scripts does not necessarily make that source available to the PortAudio backend used by `sounddevice`.

These are properties of the cited upstream sources at the time of review. A forked Termux APK or a future package build may differ; inspect the manifest and actual host APIs of the installed build before relying on it.

## Termux:API permission setup

The EchoCrypt bootstrap installs the Termux-side `termux-api` command package. The Android **Termux:API companion app** must also be installed separately, using the same trusted distribution/source and signing key as the main Termux app. The API app checks `RECORD_AUDIO` and opens Android's runtime permission activity when the permission is missing. The matching Termux and Termux:API packages share the configured Android UID; the actual permission grant must still be verified on the device.

In EchoCrypt, open **Configuration → Audio settings → Request Android microphone permission**. The application asks Termux:API to make a one-second temporary AAC recording. If Android displays a permission prompt, allow it, return to EchoCrypt, and retry the action. A successful temporary recording confirms that the API permission path works. EchoCrypt then separately tests the live PortAudio input stream; that test is mandatory because a file recording is not the same as continuous PCM input.

The standard Termux PortAudio package is built with OpenSL ES support and without its PulseAudio host API. The separate PulseAudio daemon warning is therefore not the right health check for this PortAudio build. EchoCrypt now tests the actual live `InputStream` rather than relying only on the number of enumerated input devices.

## What to check on the device

1. Install the matching **Termux:API** Android app from the same distribution/source as Termux; mismatched signatures/builds can break the shared-UID relationship.
2. Open Android Settings and inspect the Microphone permission for Termux/Termux:API. Grant it if Android exposes the permission. Also check Android's global microphone privacy toggle.
3. Close other apps that may be using the microphone.
4. In Termux, inspect the PortAudio host APIs and try opening the same live input stream EchoCrypt uses:

```bash
python - <<'PY'
import sounddevice as sd

print("Host APIs:")
for i, host in enumerate(sd.query_hostapis()):
    print(i, host["name"])

print("\nInput devices:")
for i, device in enumerate(sd.query_devices()):
    if device["max_input_channels"] > 0:
        api = sd.query_hostapis()[device["hostapi"]]["name"]
        print(i, device["name"], "|", api)

print("\nOpening live microphone stream...")
stream = sd.InputStream(
    samplerate=48000,
    channels=1,
    dtype="float32",
    blocksize=1024,
)
try:
    stream.start()
    print("INPUT STREAM OPENED")
finally:
    try:
        stream.stop()
    finally:
        stream.close()
PY
```

If this direct test returns the same OpenSL ES `-9999` error, the fault is below EchoCrypt's modem and Broadcast layers. Changing BFSK frequencies, symbol rate, keys, or protocol settings will not fix it.

You may also run `termux-info` and report the Android version, device model, Termux release/source, and the output above when diagnosing a device-specific failure.

## Practical support boundary

- On Linux and desktop operating systems, use an audio backend that can open a live PCM microphone stream.
- On Android, use a host application/build that declares and receives `RECORD_AUDIO` and exposes a compatible live PCM input backend.
- Do not count a successful `termux-microphone-record` file capture as proof that PortAudio's live `InputStream` works.
- Stock Termux support for EchoCrypt's live acoustic receiver remains unverified and must not be advertised as hardware-supported until a physical-device stream-open test passes.
