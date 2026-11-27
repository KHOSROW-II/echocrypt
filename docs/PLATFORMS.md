# Desktop and browser platform matrix

## Official desktop targets

ECHOCRYPT publishes native 64-bit builds for these six operating-system / CPU combinations:

| Operating system | CPU architecture | Portable archive | Native installer |
|---|---|---|---|
| Linux | x86-64 (amd64) | ZIP | DEB and RPM |
| Linux | ARM64 (aarch64) | ZIP | DEB and RPM |
| Windows | x86-64 (amd64) | ZIP | MSI |
| Windows | ARM64 (aarch64) | ZIP | MSI |
| macOS | Intel x86-64 | ZIP | PKG |
| macOS | Apple Silicon ARM64 | ZIP | PKG |

Each target is built on a native GitHub-hosted runner. The matrix is tested independently before packaging; the release job refuses to proceed unless every target succeeds. See the [installation guide](INSTALLATION.md) for per-platform commands and optional browser host registration.

These are CLI and Native Messaging host distributions, not a graphical desktop application. Native installers place the executable, host, browser extensions, and documentation in platform-appropriate locations. Portable ZIP archives remain available for users who prefer manual installation.

## Architecture boundaries

The official release matrix covers the six mainstream 64-bit desktop targets above. It does not claim support for every CPU architecture or legacy ABI.

The following are not currently official release targets:
- 32-bit Linux (i686) and 32-bit ARM (ARMv7).
- 32-bit Windows.
- RISC-V 64, PowerPC 64 LE, and IBM Z.
- macOS versions or CPU generations outside the supported Apple / Intel 64-bit platforms.

Those targets require their own native dependency builds, runtime validation, and installation testing. PyInstaller alone being capable of targeting an architecture is not sufficient to call the complete application supported.

## Package formats and runtime dependencies

- Debian / Ubuntu: DEB; requires the distribution's PortAudio runtime (libportaudio2).
- Fedora / RHEL-family: RPM; requires the distribution's PortAudio runtime (portaudio).
- Windows: MSI; the bundled CLI and Native Messaging host are installed under Program Files.
- macOS: PKG; installs the CLI, native host, extensions, and documentation under /usr/local.

Portable ZIP files include the standalone CLI, Native Messaging host, browser extension archives, scripts, and platform documentation. Browser and audio routing setup still requires the appropriate browser registration and audio backend configuration.

## Browser packages

Standalone extension archives are published alongside the desktop installers:

| Browser | Release asset | Manifest |
|---|---|---|
| Chrome / Chromium | echocrypt-chrome.zip | Manifest V3 |
| Microsoft Edge | echocrypt-edge.zip | Manifest V3 |
| Mozilla Firefox | echocrypt-firefox.zip | Manifest V2 |

Chrome and Edge use the Chromium adapter based on tabCapture and an offscreen document. Firefox uses a separate adapter that captures supported HTML audio/video media elements with captureStream / mozCaptureStream. It is not a general capture of every possible tab sound, protected media, WebAudio-only content, or browser chrome.

The archives are developer-mode / unpacked-installation packages, not signed Chrome Web Store, Edge Add-ons, or Mozilla Add-ons submissions. Each browser has a distinct Native Messaging registration and origin allowlist.

## Audio routing

Linux uses PulseAudio / PipeWire-Pulse virtual sinks. Windows and macOS audio routing requires a user-configured virtual audio endpoint. The installer does not silently install kernel drivers or alter the system's default audio devices.

Hardware-specific playback, recording, microphone permissions, browser/provider behavior, and virtual audio drivers require validation on the actual target machine. CI verifies package creation and software tests; it cannot prove physical audio compatibility.

## Signing and notarization

Current packages are unsigned:
- Windows MSI packages are not Authenticode-signed.
- macOS PKG packages are not Developer ID-signed or notarized.
- Linux packages are not repository/GPG-signed.

Windows SmartScreen and macOS Gatekeeper may show warnings. Signing and notarization require release credentials that are not stored in the repository workflow.

## Release gate

The release workflow runs unit / contract tests across the supported operating systems and architectures before packaging. Pull Requests also build all six native desktop packages and validate the browser extension archives, but do not publish release assets. Main/tag builds produce portable ZIPs, native installers, and standalone browser extension ZIPs; the release gate verifies package file formats and checksums before creating or updating a GitHub Draft Release. Nothing is published automatically.
