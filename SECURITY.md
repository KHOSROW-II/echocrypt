# Security Policy

## Project security status

ECHO-CRYPT is an experimental prototype. It has not undergone an independent security audit and must not be treated as a production-grade secure communications product.

The security properties differ by communication mode:

- **Direct:** Intended to provide authenticated, end-to-end encrypted sessions between trusted peers. This does not eliminate implementation, endpoint, key-storage, or acoustic-layer risks.
- **Broadcast:** Public by design. It provides no confidentiality and no cryptographic sender authentication. Do not send private, personal, or sensitive information in Broadcast mode.

The local Ed25519 identity private key is currently stored unencrypted at rest. Anyone who obtains that file may be able to impersonate the identity. Protect the device and the private-key file accordingly.

## Reporting a vulnerability

Please do not publish exploitable vulnerability details in a public issue.

Use GitHub's **Report a vulnerability** option in the repository's Security tab / private vulnerability reporting flow, if it is enabled. If that option is unavailable, contact the repository maintainer privately through the contact methods listed on the maintainer's GitHub profile before disclosing technical details publicly.

When reporting, include:

- A concise description of the affected component and impact.
- The commit, release, or source revision you tested.
- Exact reproduction steps and a minimal proof of concept, where safe.
- Relevant logs or traces after removing private keys, identity files, message contents, and other sensitive data.
- Whether the issue affects Direct, Broadcast, or both.

Do not attach private identity keys, session keys, real user messages, or other secrets.

## Scope and expectations

Security reports are welcome for issues involving authentication, key agreement, encryption, nonce handling, replay protection, packet parsing, file handling, denial of service, and sensitive-data exposure.

This is a community project without a guaranteed response or remediation SLA. Please allow reasonable time for triage and coordinated disclosure. A report does not imply that a fix, bounty, or release date is guaranteed.

## Safe testing

Only test systems and devices you own or have explicit permission to assess. Avoid transmitting malformed or high-volume traffic in shared acoustic environments where it could disrupt other users.
