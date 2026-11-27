# Linux virtual audio endpoints

## Purpose

The virtual audio backend makes ECHOCRYPT look like a generic microphone and speaker to any desktop application with selectable audio devices. ECHOCRYPT does not integrate with a provider API and does not depend on Meet, Zoom, Discord, or a particular website.

## Signal routing

```text
ECHOCRYPT TX -> echocrypt_tx sink -> monitor source: ECHOCRYPT Virtual Microphone
                                             |
                                             v
                               Any application microphone input

Any application speaker output -> echocrypt_rx sink -> monitor source: ECHOCRYPT Virtual Speaker Monitor
                                                       |
                                                       v
                                                  ECHOCRYPT RX
                                                       |
                                                       +--> optional loopback to current physical speaker
```

Only the application explicitly selecting **ECHOCRYPT Virtual Speaker** is routed through the RX monitor. Other applications and system defaults are not changed. The physical-speaker loopback mirrors only the RX virtual sink.

## Requirements and setup

- Linux desktop session.
- A running PulseAudio server or PipeWire with its PulseAudio compatibility service.
- `pactl` command (on Ubuntu, normally provided by `pulseaudio-utils`).
- A PortAudio build that can see the PulseAudio-compatible endpoints.

In ECHOCRYPT open:

**Configuration → Audio settings → Create / configure ECHOCRYPT virtual microphone + speaker**

In the target call/browser application select:
- **Microphone:** ECHOCRYPT Virtual Microphone
- **Speaker:** ECHOCRYPT Virtual Speaker

The optional loopback allows the user to hear the call through the current physical default output. Removing the endpoints through ECHOCRYPT unloads only module IDs recorded as created by ECHOCRYPT.

## Security and compatibility boundaries

The audio service is a carrier, not the ECHOCRYPT security layer. Direct mode encrypts/authenticates the application payload before it becomes a modem waveform. The provider may still inspect, record, transcode, denoise, or suppress audio; this can destroy the modem signal. Broadcast is public and must not be used for sensitive content.

The backend is currently Linux-only. It does not work in stock Termux, and it does not install a Windows kernel audio driver. It uses generic operating-system audio endpoints, not a Meet-specific integration.

## Selected browser-tab capture

A browser extension is a separate optional approach for RX. Chrome's tab-capture API can capture audio from a user-selected tab, while Native Messaging can connect an extension to a local native process. An eventual ECHOCRYPT extension would forward only bounded PCM blocks from the chosen tab into a local bridge. This can avoid routing all browser/system output to a virtual speaker, but it requires an extension and native host, explicit tab-capture permission, secure origin/port validation, bounded queues, and lifecycle handling. This extension is not implemented yet.

## Validation

A successful local virtual route proves that the virtual TX/RX nodes can exchange modem-band PCM locally. It does not prove that an Internet call provider preserves the signal. Test two real clients through the actual provider and record packet recovery rate, latency, audio processing settings, and device names.
