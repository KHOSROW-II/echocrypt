# ECHO-CRYPT Developer API

The public Python API is provided by the \`echocrypt_sdk\` package. It is
application-independent: it does not import the terminal interface or chat UI.

ECHO-CRYPT moves data through microphone and speaker audio. It does not open
network sockets, use TCP/UDP, or require a relay server.

## Install

From a source checkout:

\`\`\`bash
python -m pip install ".[audio]"
\`\`\`

The \`audio\` extra installs the PortAudio-backed \`sounddevice\` package.
Linux systems may also need their distribution's PortAudio runtime/development
package. If you only need packet codecs and protocol types, the audio extra is
not required.

## 1. Low-level acoustic packets

\`AcousticClient\` sends and receives opaque byte packets. This layer provides
modem framing, FEC, synchronization, CRC, a continuous receiver, a serialized
transmit queue, events, and cancellable local playback handles.

It does **not** provide encryption, peer identity, replay protection, or
remote-delivery guarantees.

\`\`\`python
from echocrypt_sdk import AcousticClient, AcousticConfig

config = AcousticConfig(
    symbol_rate=200,
    symbol_rates=(200, 800),
    max_packet_bytes=1024,
    channel_access=True,
)

with AcousticClient(config) as client:
    client.on(
        "packet_received",
        lambda event: print("RX:", event.data.hex()),
    )

    transmission = client.send_async(
        b"my-application-frame",
        fec_mode="conv",
        symbol_rate=200,
        label="telemetry",
    )
    local_result = transmission.wait()
    print("Local playback seconds:", local_result.duration_seconds)
\`\`\`

The receive callback runs on the SDK event-dispatch thread, not on the modem
decoder thread. Keep callbacks short. Use a worker queue for blocking work.

To cancel a queued or currently playing frame, call
\`transmission.cancel()\`. Cancellation cannot retract samples already emitted
through the speaker.

## 2. Authenticated Direct communication

Direct mode mutually authenticates Ed25519 identities, performs ephemeral
X25519 key agreement, derives directional AEAD keys and a session hopping key,
and uses ChaCha20-Poly1305 with replay protection.

Exchange public identity files over a trusted channel and compare the complete
SHA-256 fingerprints independently. Never send a private identity file to a
peer.

\`\`\`python
from echocrypt_sdk import AcousticClient, AcousticConfig, DirectPeer
from security.identity import load_private, load_public
from security.peer_handshake import PeerHandshake

local_private = load_private("keys/identity.ed25519")
peer_public, peer_fingerprint = load_public("contacts/peer.json")

client = AcousticClient(
    AcousticConfig(symbol_rate=200, symbol_rates=(200, 800))
).start()

handshake = PeerHandshake(
    "initiator",
    local_private,
    peer_public,
    peer_fingerprint,
)
peer = DirectPeer(client, handshake)

try:
    peer.connect(timeout=30)
    sent = peer.send_text("authenticated hello")
    print("ACK:", sent.acknowledged, sent.message_id)

    received = peer.receive_text(timeout=30)
    print(received.text)

    # Application-defined authenticated payloads.
    peer.send_json("iot.sensor.reading", {"sensor": "room-7", "value": 21.5}).wait()
finally:
    peer.close()
    client.close()
\`\`\`

The other device uses the same pattern with role \`"responder"\` and calls
\`peer.accept(timeout=30)\` instead of \`connect()\`.

\`send_text()\` uses authenticated stop-and-wait delivery and retransmits the
same ciphertext on timeout. \`send_bytes()\` and \`send_json()\` provide
authenticated application payloads; the receiving side reads them with
\`receive_bytes()\` or \`receive_json()\`.

For custom binary protocols, register a fast callback with
\`peer.add_packet_handler(callback)\`. It receives authenticated plaintext
bytes and should return \`True\` when it consumes the payload. The callback runs
on the SDK event-dispatch thread.

### Direct hopping and receive profiles

After a successful handshake, DirectPeer installs the session hopping key for
both default transmission and reception. Handshake control frames explicitly
disable hopping because the session key does not exist yet.

The acoustic decoder can retain multiple receive profiles simultaneously.
This allows a client to listen for fixed-frequency traffic, its active Direct
session, and the public Broadcast profile without replacing one profile with
another.

## 3. Authenticated file transfer

\`SecureFileTransfer\` uses the authenticated Direct session and the binary
transfer protocol. It provides:

- A signed-session-encrypted manifest and independently encrypted chunks.
- Authenticated ACK and resume status.
- Persistent receiver state and sender resume state.
- Stop-and-wait retries.
- Selective repair requests after integrity failures.
- Final receiver-side SHA-256 verification.
- An authenticated completion packet emitted only after the receiver commits
  the verified file.

Both peers must create a \`SecureFileTransfer\` instance after Direct is
connected. The receiving instance automatically processes incoming manifests,
chunks, acknowledgements, resume packets, and repair requests.

\`\`\`python
from echocrypt_sdk import SecureFileTransfer

with SecureFileTransfer(
    peer,
    receive_root="downloads",
    state_root=".echocrypt-state",
) as files:
    result = files.send_file(
        "archive.zip",
        chunk_size=256,
        ack_timeout=15,
        retries=3,
        on_progress=lambda done, total: print(f"{done}/{total}"),
    )

    if result.completed:
        print("Verified remotely:", result.sha256)
        files.finish_transfer(result.transfer_id)
\`\`\`

Do not call \`finish_transfer()\` until \`send_file()\` returns a completed
result. That call removes the local sender resume state. Receiver-side state is
retained for recovery and application-level retention policies.

The \`send_file()\` result means the remote peer authenticated the completion
packet and reported the expected SHA-256 digest. It is stronger than local
playback completion or a chunk ACK.

## 4. Open Broadcast

Broadcast is an open, unauthenticated public room. It requires no identity
exchange or handshake. The transform and hopping schedule are public constants
embedded in every compatible build. **Broadcast is not confidential and does
not cryptographically authenticate the sender.**

\`\`\`python
from echocrypt_sdk import AcousticClient, AcousticConfig, BroadcastAPI

client = AcousticClient(
    AcousticConfig(symbol_rate=200, symbol_rates=(200, 800))
).start()

room = BroadcastAPI(
    max_packet_size=1024,
    download_dir="downloads/broadcast",
)
results = []

room.attach(client, on_result=results.append)
try:
    encoded, transmissions = room.send_text(client, "Public status update")
    print("Broadcast ID:", encoded.message_id_hex)

    # Send a file/voice attachment in public Broadcast.
    encoded_file, transmissions = room.send_file(
        client,
        "public-document.pdf",
        symbol_rate=200,
    )

    # Ask senders to retransmit missing fragments.
    room.request_missing(client, older_than=2.0, symbol_rate=200)
finally:
    room.close()
    client.close()
\`\`\`

\`attach()\` registers the public receive hopping profile and subscribes to
validated acoustic packets. Retry responses are transmitted automatically.
Call \`request_missing()\` periodically to ask for missing fragments. A
successful local Broadcast transmission is not proof that any peer received it.

The SDK's default Broadcast file limit is 16 MiB. The receiver buffers incomplete
file fragments in memory until the final digest check, so `max_file_size` must
not exceed `max_partial_bytes`. Increase both limits deliberately if an
application accepts the associated memory cost.

For interoperability, all participants must support the selected symbol rate
in their \`AcousticConfig.symbol_rates\`. When using \`encode_file()\` separately
from \`send_file()\`, pass the same \`symbol_rate\` to encoding and transmission.

## 5. Events and lifecycle

- Call \`AcousticClient.start()\` before sending or creating protocol peers.
- Call \`close()\` on \`DirectPeer\`, \`SecureFileTransfer\`, and \`BroadcastAPI\`
  when their lifetime ends.
- Event subscriptions return a closeable subscription object.
- \`Transmission.wait()\` reports local playback completion only.
- Direct text ACKs confirm authenticated message receipt.
- File completion confirms remote SHA-256 verification.
- Broadcast has no delivery acknowledgement.

## Security and limitations

ECHO-CRYPT is experimental and has not received an independent security
audit. Direct mode protects message confidentiality and authenticity between
pinned identities, but private identity keys are currently stored unencrypted
at rest. Broadcast is public. Frequency hopping is not anonymity and does not
prevent a capable observer from recording the acoustic channel.

Automated unit tests and synthetic loopback do not replace tests with two
physical devices, different speaker/microphone hardware, and realistic room
conditions. Do not use this project for life-critical communications.
