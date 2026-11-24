# Contributing

Python 3.11+ on Linux/macOS. Runtime uses the standard library.

Create a virtual environment; install requirements-dev.txt and the package in editable mode.
Run `python -m pytest -q`, `ruff check .` and `python -m build --no-isolation`.
Tests run actual CLI processes, SQLite and local HTTP servers with synthetic provider accounts.

New behavior needs a failing test that reaches the intended behavior, followed by the implementation.
Preserve account pinning, single-use refresh serialization, pagination completeness and transactional ingestion.
Only archive import accepts explicitly operator-verified historical identity aliases; live access stays strict.
Webhooks authenticate the original body, route by user ID, persist metadata before ACK and never store biometrics in the relay.

Do not use real credentials or health records in tests, issues, screenshots or source control.
Operational receipts, deployment-specific paths and real consent/testing belong outside the repository.
Changing code must not silently change an existing user's data, authorization, schedules or notification choices.
