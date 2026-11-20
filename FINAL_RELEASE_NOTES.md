# ECHO-CRYPT — Final build notes

## Broadcast voice receive fix

The supplied acoustic traces showed that the sender completed the `.ecv` file
transmission and the receiver accepted the manifest, but subsequent voice
fragments were being rejected during modem synchronization. One real capture
had an ECF3 preamble score of 0.289, below the old fixed 0.30 cutoff. Other
attempts selected a stronger false ECF2 correlation and failed its protected
header CRC. The receiver then retried timing offsets around that incorrect
candidate instead of examining the weaker ECF3 candidate.

The modem now:

- searches a bounded set of separated preamble candidates for each supported
  wire format;
- considers candidates at normalized scores from 0.24 upward;
- verifies the protected header CRC and wire-format magic/version before
  considering the candidate valid;
- proceeds to the next candidate after a false sync, and accepts a packet only
  after the final payload CRC succeeds;
- avoids timing-recovery retries when a protected header decodes under the
  wrong wire format.

This does not disable CRC/FEC checks, lower AEAD standards, or convert corrupted
payloads into accepted messages. It improves the chance of finding the correct
frame boundary; irrecoverably damaged chunks still need selective retransmission.

## Verification

- 36 focused tests passed: ASRE/modem, Stage 9.8 Broadcast voice transport, and
  reliable-text transport.
- Python compilation checks passed for `core`, `interface`, `security`, and the
  application entry point.
- No physical two-device speaker/microphone test has been performed for this
  final archive. The fix is validated by regression tests and the supplied
  acoustic diagnostics, not by claiming a new hardware transfer succeeded.

Open Broadcast remains a public nearby room without a private group key, as
specified. The application continues to use acoustic audio transfer only.
