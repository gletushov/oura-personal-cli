---
name: oura-cli
description: Read the current owner's Oura ring data, check freshness, sync and diagnose access through the installed portable CLI. Use for sleep, recovery, readiness, activity or Oura connection questions.
---

Use health records only in the owner's authorized personal context; access to a bot does not imply permission to share its owner's biometrics with other callers or groups.

Use `oura --help` and `oura status --json` to discover the installation. An instance with multiple owners requires explicit `--owner`; never choose a different person's profile to obtain data. Report the owner, source, measurement day and freshness. `fetched_at` is download time, not measurement time; missing/null values are unknown.

Read: `oura sleep --date YYYY-MM-DD --json`, `oura readiness --date YYYY-MM-DD --json`, `oura read daily_stress --date YYYY-MM-DD --json`. Sleep output separates primary sleep and naps; durations are seconds, HRV milliseconds. `oura sync --json` refreshes local data deterministically. `oura doctor --live --json` checks access without exposing credentials.

Phone login: `oura auth start --json` returns an authorization URL. The owner opens it in their phone browser and consents on Oura; then run `oura auth finish --json`. A pending result means the owner has not completed consent yet. Do not ask for a password, token, client secret or copied OAuth code in chat. Configure credentials only from the operator's private file/stdin. An existing account pin must not be bypassed.

Webhook commands: `oura webhooks status --json`, `oura webhooks pull --json`. Only `enable` creates provider subscriptions; it requires a configured application and relay. Silent collection and automatic user-facing reports are separate; this skill does not enable reports or authorize sends.

Oura API/MCP Agreement (https://cloud.ouraring.com/legal/api-agreement), effective 8 June 2026, section 4(d), restricts API data being supplied to LLMs and identifies Oura's own MCP route. This package is an independent deterministic API client, not the official Oura MCP and not vendor-approved. Before supplying biometric documents to a model, the operator must resolve the applicable access/permission route. Status/diagnostics can be checked without exposing biometric values.
