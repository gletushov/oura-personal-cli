# Oura Personal CLI

Read README.md and CONTRIBUTING.md. Runtime code is in src/oura_cli and behavior tests in tests.
Use behavior tests first, review their expected failures, implement a complete change and run related checks.
Run python -m pytest -q and ruff check .; inspect a freshly built wheel before release.
Never commit credentials, user records, local state, private operator notes or server-specific configuration.
Keep AGENTS.md and CLAUDE.md aligned when changing project rules.
No automatic model calls or user-facing sends. OAuth authorization belongs to each owner.
Actual provider OAuth/webhook delivery needs separate live acceptance; synthetic tests do not prove it.
