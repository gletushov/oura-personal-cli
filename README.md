# Oura Personal CLI

An independent, portable Oura API v2 client for personal data collection. Phone-friendly OAuth with PKCE, rotating credentials, separate owner profiles, SQLite history, incremental synchronization and signed webhooks. Python 3.11+ on Linux and macOS. No runtime dependencies.

[Русская документация](README.ru.md)

This is not an official Oura product or Oura's MCP server. It makes no LLM calls and sends no user-facing messages.

## Features

- Browser consent works on a phone while the CLI runs on a server.
- Account pinning and separate configuration, grants and databases for every owner.
- Serialized, atomic refresh-token rotation; uncertain exchanges require reauthorization.
- Complete pagination, transactional collection updates and overlapping incremental sync.
- Primary sleep separated from naps; source, units and measurement freshness remain explicit.
- Durable application-wide webhook intake routed into owner-specific queues.
- Signature, timestamp and account validation; duplicate suppression, commit-before-ACK and recovery.
- Subscription creation and renewal, JSON/CSV export, legacy archive import and diagnostics.
- A packaged Hermes skill with phone-login instructions and privacy boundaries.

## Install

Download the wheel from [Releases](https://github.com/gletushov/oura-personal-cli/releases), then:

```sh
python3 -m venv .venv
.venv/bin/pip install ./oura_personal_cli-0.1.0-py3-none-any.whl
.venv/bin/oura --help
```

Or install a tagged source revision:

```sh
pip install 'git+https://github.com/gletushov/oura-personal-cli.git@v0.1.0'
```

## Owner profiles

```sh
oura profile create alice --timezone Asia/Dubai
oura --owner alice status --json
```

State defaults to `~/.local/share/oura-cli/`; override with `--home` or `OURA_CLI_HOME`. Each `owners/NAME/` directory has its own configuration, secrets and database. Select `--owner NAME` when multiple profiles exist. Directory permissions are 0700; data and credential files are 0600. For different people use separate OS users or containers: profile separation is not a security boundary against the same OS administrator.

## Phone/browser OAuth

Register an Oura OAuth application and run the HTTPS relay described below. The redirect URI must exactly match `https://YOUR-RELAY/oauth/callback`.

Create a private JSON file outside your repository, mode 0600:

```json
{
  "client_id": "YOUR_APP_ID",
  "client_secret": "YOUR_APP_SECRET",
  "relay_url": "https://YOUR-RELAY",
  "relay_key": "YOUR_PRIVATE_ENROLLMENT_KEY"
}
```

```sh
oura auth configure --file /private/path/app.json
oura auth start --json
# Open authorize_url on your phone and consent on Oura's website.
oura auth finish --json
oura doctor --live --json
```

No passwords, authorization codes or tokens need to be pasted into a chat. The relay receives a short-lived authorization code; the initiating CLI retrieves it using an independent polling secret and exchanges it directly with Oura. The PKCE verifier and user access/refresh tokens remain at that CLI. Sessions expire after ten minutes. Repeat `auth finish` while pending; use `auth cancel` to start over. `auth login --no-browser` combines the interactive steps for a terminal.

Existing account pins cannot be silently replaced. A network-ambiguous exchange of a single-use refresh token is not blindly repeated. `auth logout --confirm-owner NAME` removes the local grant while preserving history; revoke provider access through Oura's account interface.

To migrate an existing grant from a private env file:

```sh
oura auth import-legacy --env-file /private/path/old.env
```

Legacy access tokens do not acquire refresh capability automatically. If Oura returns a different user ID during migration, first verify the account through the provider; never clear an account pin merely to suppress the error. Explicitly verified historical IDs may be recorded in `historical_account_ids` for archive import only. They do not authorize live API access or OAuth account replacement.

## Read and synchronize

```sh
oura sync --json
oura sleep --date 2024-11-24 --json
oura readiness --date 2024-11-24 --json
oura read daily_stress --start 2024-11-01 --end 2024-11-24 --json
oura battery --json
oura vo2 --json
oura export daily_sleep --format csv --output /private/path/sleep.csv
```

The first sync fetches approximately a month. Later runs overlap the last two days and fetch changes since the last successful window. Use explicit `--start/--end` for backfills. Heart-rate and battery time series are chunked into ranges of at most 30 days. Date parameters use the profile timezone; datetime ranges include the requested local end day.

Records are updated by provider ID, or timestamp for time series. A failed page never commits a partial collection. Missing dated records are reconciled within an authoritative fetched window; history outside that window is retained. One endpoint failure preserves successful endpoints and produces a nonzero exit status.

Sleep durations are seconds, HRV milliseconds and heart rate bpm. Null remains unknown. Download time (`fetched_at`) is separate from measurement date (`latest_day`); a recent request does not make old data current. Sleep/readiness availability depends on synchronization from Oura's mobile app.

```sh
oura import-archive --directory /private/path/old-daily-snapshots
oura sync --legacy-output /private/path/old-data --quiet
```

Archive import verifies the account in every dated `personal_info.json` before writing; originals remain in place. Legacy output preserves a cached monthly baseline even when network sync is incremental. Existing personal_info files are not replaced.

Record responses use `schema_version: 1`. `--quiet` suppresses successful output. Exit codes: 0 success; 2 usage/configuration/local state; 3 API/network/auth; 4 login pending; 5 partial failure; 130 interrupted.

## HTTPS relay and webhooks

Keep the relay configuration outside Git, mode 0600:

```json
{
  "public_url": "https://YOUR-RELAY",
  "registration_key": "GENERATE_A_RANDOM_32_BYTE_SECRET",
  "client_id": "YOUR_APP_ID",
  "client_secret": "YOUR_APP_SECRET",
  "state_dir": "/private/path/relay-state"
}
```

```sh
oura relay serve --config /private/path/relay.json --listen 127.0.0.1 --port 18765
# Put an HTTPS reverse proxy in front of this loopback listener.
oura webhooks enable --endpoint sleep --endpoint daily_readiness
oura webhooks status --json
oura webhooks worker --interval 30
oura webhooks maintain --json
```

One relay serves one OAuth application. Distribute its enrollment credential privately, separately from the code. Each installation gets an independent queue/polling secret. The common application callback is `/webhooks`; signed events are delivered only to queues pinned to the matching user ID. Events for unregistered users are acknowledged without retention.

HMAC covers the exact request bytes and timestamp; the allowed timestamp skew is five minutes. The relay stores event metadata, not biometric documents or user tokens. The owner CLI fetches documents using its own grant, commits to SQLite, then acknowledges the event. Duplicates, retries and restarts preserve this ordering. Delete events create tombstones; an authoritative 404 after a queued update is handled as deletion. Transient failures remain queued.

Subscriptions cover create/update/delete and are renewed before expiration. Other callback URLs are not modified. Idle workers do not poll Oura; subscription maintenance runs hourly. Oura does not offer heartbeat/battery webhook types, so retain periodic `sync` for these streams and missed-event reconciliation. `oura webhooks disable` stops delivery for the selected owner while preserving shared app subscriptions and every other owner’s queue.

## Hermes skill

```sh
oura skill install --destination /path/to/hermes/profile/skills
```

Install the CLI in the agent's PATH and use that owner's state directory. Automatic collection and automatic reports are separate choices; this package does not enable reports or messaging.

## Oura terms and scope

The [Oura API/MCP Agreement](https://cloud.ouraring.com/legal/api-agreement), effective June 8, 2026, section 4(d), restricts providing API data to LLMs and identifies Oura's own MCP route. This package is a deterministic API client. A third-party MCP wrapper is not Oura's official MCP and does not establish permission for that use. Operators must resolve the applicable data/model access route separately.

Official references: [API v2](https://cloud.ouraring.com/v2/docs), [OAuth](https://cloud.ouraring.com/docs/authentication), [webhooks](https://cloud.ouraring.com/v2/docs#tag/Webhook-Subscription-Routes).

## Development

See [CONTRIBUTING.md](CONTRIBUTING.md). Tests exercise real CLI processes, local HTTP servers and SQLite using synthetic accounts and measurements. They do not substitute for real owner consent or provider-originated webhook acceptance.

MIT license. No affiliation with or endorsement by Ōura.
