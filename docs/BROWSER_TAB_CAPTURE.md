# Selected browser-tab audio capture

ECHOCRYPT provides an optional Chromium extension for receiving only the audio output of a user-selected browser tab. This is separate from the generic virtual-speaker route.

The extension uses Chromium's tab-capture API after the user clicks the extension action. An offscreen document receives the stream, converts it to 48 kHz mono PCM, forwards it through a persistent Chrome Native Messaging connection, and a local host writes the samples into the dedicated `echocrypt_tab_rx` PulseAudio/PipeWire-Pulse sink. ECHOCRYPT reads only its monitor source.

The extension also connects its captured stream to the AudioContext destination, preserving playback through the browser's ordinary output. It does not redirect or capture the rest of the system.

See [browser_extension/README.md](../browser_extension/README.md) for installation and operation.

## Important routing rule

When using selected-tab capture:
- Website microphone: **ECHOCRYPT Virtual Microphone**.
- Website speaker: normal system/default output.
- ECHOCRYPT RX: **ECHOCRYPT Browser Tab Monitor**.

Do not set the website speaker to **ECHOCRYPT Virtual Speaker** at the same time; that is the alternative generic route and would duplicate the RX audio.

The extension does not integrate with any specific service API. It captures the chosen tab's rendered audio regardless of whether the site is a meeting application, a browser-based call, or another web audio source.
