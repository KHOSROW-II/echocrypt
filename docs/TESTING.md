# ECHOCRYPT Test Strategy

ECHOCRYPT uses pytest as the test runner. Existing `unittest.TestCase` suites are
also discovered and executed by pytest, preserving the integration tests while
adding parametrized function-level regression coverage.

## Run locally

Install the development extra and run the complete suite:

```bash
python -m pip install -e ".[dev]"
python -m pytest -q
```

Run with line coverage for core, security, and SDK packages:

```bash
python -m pytest -q \
  --cov=core --cov=security --cov=echocrypt_sdk \
  --cov-report=term-missing --cov-report=xml:coverage.xml
```

Run focused suites while iterating:

```bash
python -m pytest -q tests/test_pytest_functional.py
python -m pytest -q tests/test_binary_transfer.py tests/test_reliability.py
python -m pytest -q tests/test_session_engine.py tests/test_worker_validation.py
```

## CI coverage

Both Actions workflows run pytest, which collects the pytest functions and
existing unittest test cases. The cross-platform matrix runs the same suite on
Linux across supported Python versions, Windows, and both macOS architectures.
It also compiles Python sources, validates browser manifests, builds extension
archives, and checks JavaScript and shell syntax.

Every platform build depends on the complete test matrix. Draft release creation
depends on both the complete test matrix and every platform build. The release
job refuses to modify an already-published release; it only creates or updates
Draft releases.

Hardware-free CI cannot prove real speaker/microphone loopback quality,
browser-specific capture permissions, or physical-device latency. Those need
a separate test using the actual devices and browsers.
