# Installing ECHOCRYPT

ECHOCRYPT is a terminal application. Installers include the standalone CLI, the Native Messaging host, browser extensions, setup scripts, and reference documentation.

## Choose the package for your architecture

| Platform | Architecture | Native installer | Portable archive |
|---|---|---|---|
| Linux | x86-64 | echocrypt-linux-x64.deb or echocrypt-linux-x64.rpm | echocrypt-linux-x64.zip |
| Linux | ARM64 | echocrypt-linux-arm64.deb or echocrypt-linux-arm64.rpm | echocrypt-linux-arm64.zip |
| Windows | x86-64 | echocrypt-windows-x64.msi | echocrypt-windows-x64.zip |
| Windows | ARM64 | echocrypt-windows-arm64.msi | echocrypt-windows-arm64.zip |
| macOS | Intel x86-64 | echocrypt-macos-x64.pkg | echocrypt-macos-x64.zip |
| macOS | Apple Silicon ARM64 | echocrypt-macos-arm64.pkg | echocrypt-macos-arm64.zip |

Download packages from the Assets section of the matching GitHub Release. Verify each file against SHA256SUMS before installation.

## Linux

Debian / Ubuntu:

    sudo apt install ./echocrypt-linux-x64.deb

Replace x64 with arm64 when installing on an ARM64 machine.

Fedora / RHEL-family:

    sudo dnf install ./echocrypt-linux-x64.rpm

Replace x64 with arm64 on ARM64 systems. The packages declare the distribution PortAudio runtime dependency. The package installs the CLI as /usr/bin/echocrypt and the Native Messaging host under /usr/lib/echocrypt.

Start the application with:

    echocrypt

## Windows

Open the matching MSI package and follow Windows Installer, or use an elevated terminal:

    msiexec /i echocrypt-windows-x64.msi

Use the ARM64 package on Windows on ARM. The installer places ECHOCRYPT under Program Files and adds a Start Menu shortcut. The application is a console program; launch the shortcut or run ECHOCRYPT.exe from its install directory.

## macOS

Install the package for the CPU architecture of the Mac:

    sudo installer -pkg echocrypt-macos-arm64.pkg -target /

Use the macos-x64 package on an Intel Mac. The CLI is installed as /usr/local/bin/echocrypt and the Native Messaging host under /usr/local/lib/echocrypt.

The packages are currently unsigned and not notarized. macOS Gatekeeper may warn that the developer cannot be verified. Do not bypass a warning unless you have verified the downloaded file and its checksum.

## Portable ZIP

Extract the ZIP archive for your operating system and architecture. It contains:
- The standalone ECHOCRYPT CLI.
- The ECHOCRYPT Native Messaging host.
- Chrome, Edge, and Firefox extension archives.
- Browser host registration scripts.
- Platform, audio-routing, and browser-capture documentation.

On Linux and macOS, run the ECHOCRYPT executable from the extracted platform directory. On Windows, run ECHOCRYPT.exe.

## Optional browser Native Messaging setup

Browser integration is optional. Install the matching browser extension from the package's extensions directory. Obtain the extension ID from the browser's extensions page, then register the native host using the included register_browser_host.py script and the full path to the installed Native Messaging host.

Linux package paths:

    python3 /usr/share/echocrypt/scripts/register_browser_host.py --browser chrome --extension-id EXTENSION_ID --native-host /usr/lib/echocrypt/ECHOCRYPT-NativeHost

macOS package paths:

    python3 /usr/local/share/echocrypt/scripts/register_browser_host.py --browser chrome --extension-id EXTENSION_ID --native-host /usr/local/lib/echocrypt/ECHOCRYPT-NativeHost

On Windows, use the installed Python interpreter and the corresponding paths under Program Files:

    py "C:\Program Files\ECHOCRYPT\scripts\register_browser_host.py" --browser chrome --extension-id EXTENSION_ID --native-host "C:\Program Files\ECHOCRYPT\ECHOCRYPT-NativeHost.exe"

Restart the browser after registering the host. Firefox uses its own extension ID and manifest registration. Selected-tab audio capture currently requires Linux PulseAudio / PipeWire-Pulse routing; the presence of a Windows or macOS installer does not imply that this browser capture path is available on those systems.

## Audio runtime and hardware

Linux packages depend on the PortAudio runtime supplied by the distribution. Windows and macOS audio devices, virtual audio endpoints, and loopback routing are not silently installed or reconfigured. Select and validate the audio devices in ECHOCRYPT's configuration menu.

CI validates software tests and installer package structure. It does not test a user's physical microphone, speaker, room acoustics, conferencing application, or virtual audio driver.

## Signing status

Current release packages are unsigned:
- Windows MSI: no Authenticode signature.
- macOS PKG: no Developer ID signature or notarization.
- Linux DEB / RPM: no repository signing key.

Download only from the official repository release page and verify SHA256SUMS.
