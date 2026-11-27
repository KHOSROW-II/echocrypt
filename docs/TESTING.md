# ECHOCRYPT Test Strategy

ECHOCRYPT uses pytest as its Python test runner. Pytest discovers both pytest
functions and the existing `unittest.TestCase` integration suites, so earlier
tests remain in the same release gate.

## Run locally

```bash
python -m pip install -e ".[dev]"
python -m pytest -q
```

Generate application-wide branch coverage:

```bash
python -m pytest -q \
  --cov-branch \
  --cov=core --cov=security --cov=echocrypt_sdk \
  --cov=interface --cov=browser_extension --cov=scripts --cov=echocrypt \
  --cov-report=term-missing \
  --cov-report=xml:coverage.xml \
  --cov-report=html:htmlcov
```

The report includes protocol and cryptography code, SDK, UI, bootstrap,
Native Messaging, browser-host utilities and Python scripts. The separate
Node.js suite executes browser background-worker flows, verifies tab ownership,
and checks PCM message routing. Python coverage does not count JavaScript
lines, so browser JavaScript execution remains a separate report.

Run focused suites:

```bash
python -m pytest -q tests/test_pytest_functional.py
python -m pytest -q tests/test_application_surfaces.py
python -m pytest -q tests/test_native_host_lifecycle.py
python -m pytest -q tests/test_release_packaging.py
node --test tests/browser_extension_contract.test.cjs
```

## Cross-platform release gate

The complete seven-environment test matrix must pass before any platform build
starts. All four native platform builds must succeed before Draft release
creation can run.

Each platform is delivered as one uniquely named ZIP containing the desktop
binary, Native Host, browser extension archives, documentation and registration
helpers. The release validator requires exactly these four ZIPs plus
`RELEASE_NOTES.md` and `SHA256SUMS`; directories are never passed to
`gh release upload`.

The release job creates and pushes the Git tag first, verifies that it points
to the tested commit, and then creates the GitHub Draft with `--verify-tag`.
It checks the exact six asset names and verifies that the release remains a
Draft. Published releases are never modified or automatically published.

## Hardware validation

Hardware-free CI cannot prove real speaker/microphone loopback quality,
browser-specific permissions, device driver behavior or physical-device
latency. Those still require tests on the actual audio devices and browsers.
