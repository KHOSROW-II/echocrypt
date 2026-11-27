# ECHOCRYPT Selected Tab Audio (Chromium)

This extension captures audio from **one browser tab selected by the user** and streams PCM locally into ECHOCRYPT. It does not capture the whole desktop, other browser tabs, the physical microphone, or other applications.

## What it does

- Click the extension icon while the target call/meeting tab is active to start capture.
- Click it again to stop; clicking from another tab switches capture to that tab.
- The captured tab audio is passed through to the browser's normal audio output so it remains audible.
- A separate PCM copy is sent to the local ECHOCRYPT native-messaging host and placed into the dedicated `echocrypt_tab_rx` virtual sink.
- ECHOCRYPT receives only the selected tab through **ECHOCRYPT Browser Tab Monitor**.
- No Meet, Zoom, Discord, or other service API is used.

## Requirements

- Linux desktop with PulseAudio or PipeWire-Pulse.
- Chromium / Google Chrome 116 or newer.
- ECHOCRYPT virtual endpoints created from **Configuration → Audio settings → Use Chromium selected-tab capture as ECHOCRYPT RX**.
- `pactl` and `pacat` available in the same user audio session.

## Install

1. Open `chrome://extensions` (or `chrome://extensions` in Chromium/Brave).
2. Enable Developer mode and choose **Load unpacked**.
3. Select the repository's `browser_extension/` directory.
4. Copy the extension ID shown by the browser.
5. Register the native host for the browser in use:

   ```bash
   bash scripts/install_browser_tab_host.sh YOUR_EXTENSION_ID chrome
   ```

   Replace `chrome` with `chromium` or `brave` when appropriate.
6. Fully restart the browser after registering the host.
7. In ECHOCRYPT, choose **Configuration → Audio settings → Use Chromium selected-tab capture as ECHOCRYPT RX**.
8. In the target call website, set its microphone to **ECHOCRYPT Virtual Microphone**. Leave its speaker output on the normal system/default output.
9. Return to the target tab and click the ECHOCRYPT extension icon. The badge shows `RX` while capture is active. Click again to stop.

Do **not** select **ECHOCRYPT Virtual Speaker** as the website's speaker while using tab capture. That is the separate generic virtual-speaker route and would duplicate the incoming stream. Use one RX route at a time.

## Local data path

```text
Selected Chromium tab audio
       |
       v
chrome.tabCapture (user clicks extension)
       |
       +-----------------------------> AudioContext destination -> normal speakers
       |
       v
48 kHz mono PCM AudioWorklet
       |
       v
Chrome Native Messaging (local stdio only)
       |
       v
ECHOCRYPT native host -> pacat -> echocrypt_tab_rx sink
       |
       v
ECHOCRYPT Browser Tab Monitor -> ECHOCRYPT RX
```

## Privacy and compatibility boundaries

- Capture starts only after the user clicks the extension and targets the active tab at that moment.
- The native host accepts messages only from the extension ID registered during installation.
- No TCP/UDP listener, localhost WebSocket server, cloud relay, or provider API is used by the extension bridge.
- The extension captures tab output, not the physical microphone. Configure the website's microphone separately to use ECHOCRYPT Virtual Microphone for outgoing modem audio.
- The call provider still processes the audio waveform. Codec compression, noise suppression, echo cancellation, automatic gain control, and other processing can corrupt modem tones. Real two-endpoint tests through the selected provider are still required.
- This initial extension supports Chromium-based browsers only. Firefox, Windows, and Android/Termux require separate backends.
- Browser audio capture is sampled at 48 kHz. Ultrasonic modem tones are not supported through this browser tab route.

Uninstall the native host registration with:

```bash
bash scripts/uninstall_browser_tab_host.sh chrome
```
