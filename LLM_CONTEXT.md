# LLM Context

This file is optimized for future LLMs and automation working in this repository.

## Purpose

- Export RedBark transactions and account catalogs.
- Export Sure transactions and account catalogs.
- Create a manual account map from Sure accounts to RedBark accounts.
- Sync RedBark transactions into Sure.
- Audit Sure for duplicate RedBark sync markers.
- Orchestrate export, sync, and audit in one scheduler-safe entrypoint.

## Repository Style

- Prefer separate scripts over merged workflows.
- Prefer standard-library-only Python.
- Prefer explicit JSON artifacts on disk over implicit in-memory state.
- Keep secrets in `.env`; do not hardcode API keys or webhook URLs.
- Avoid auto-matching accounts; mapping is intentionally interactive and manual.

## Runtime Assumptions

- OS target in current usage: Windows.
- Python environment commonly used in this repo: `.venv`.
- Scripts are designed to be runnable directly via CLI.
- Logs are overwritten per script run.

## External Systems

- RedBark API
  - base URL: `https://api.redbark.co`
  - auth: `Authorization: Bearer <REDBARK_API_KEY>`
- Sure Finance API
  - base URL: `SURE_BASE_URL` from `.env`
  - auth: `X-Api-Key: <SURE_API_KEY>`
- Discord webhook
  - env var: `DUPLICATE_AUDIT_WEBHOOK_URL`
  - used only by the duplicate audit when duplicates are found

## Environment Variables

- `REDBARK_API_KEY`: required for RedBark export.
- `SURE_BASE_URL`: required for Sure export, sync, and duplicate audit.
- `SURE_API_KEY`: required for Sure export, sync, and duplicate audit.
- `DUPLICATE_AUDIT_WEBHOOK_URL`: optional for duplicate alerting.

## Entry Points

| File | Responsibility | Inputs | Outputs |
| --- | --- | --- | --- |
| `redbark_export_transactions.py` | Export RedBark account catalog and per-account transaction files | `.env`, RedBark API, day lookback | `exports/accounts.json`, `exports/*.json`, `logs/redbark_export_transactions.log` |
| `sure_export_transactions.py` | Export Sure account catalog and per-account transaction files | `.env`, Sure API, day lookback | `sure_exports/accounts.json`, `sure_exports/*.json`, `logs/sure_export_transactions.log` |
| `generate_account_map.py` | Interactively map Sure accounts to RedBark accounts | `exports/accounts.json`, `sure_exports/accounts.json` | `account_map.json` |
| `sync_redbark_to_sure.py` | Create missing Sure transactions from RedBark exports | `.env`, `account_map.json`, `exports/*.json` | Sure transactions, `logs/sync_redbark_to_sure.log` |
| `audit_redbark_to_sure_duplicates.py` | Detect duplicate RedBark sync markers in Sure | `.env`, `account_map.json`, Sure API | exit code, optional Discord alert, `logs/audit_redbark_to_sure_duplicates.log` |
| `orchestrate_redbark_sync.py` | Run export, sync, and audit in sequence with overlap protection | `.env`, `account_map.json`, lock file | managed exports, sync attempt, audit result, `logs/orchestrate_redbark_sync.log` |

## Docker Packaging

- `Dockerfile` packages the orchestrator as the container entrypoint.
- Container entrypoint: `python docker_entrypoint.py`
- GitHub Actions release workflow publishes the image to `ghcr.io/sunnyskye/redbark-sure-sync` on version tags.
- Recommended runtime model: choose one host runtime directory, write `account_map.json` there with `docker run ... image map ... --mapfile /runtime/account_map.json`, then mount that same local file into the scheduled sync command with `--mapfile /app/account_map.json`.
- `--env-file` is the Docker CLI input for secrets; `--mapfile` is the container argument for the mounted account map file.
- First-time setup uses `docker_entrypoint.py map` to bootstrap account catalogs and launch `generate_account_map.py` interactively.
- Runtime mounts should include:
  - `account_map.json` -> `/app/account_map.json` (read-only)
  - host `exports/` -> `/app/exports`
  - host `logs/` -> `/app/logs`
- First-time map generation can mount the operator's working directory to `/runtime` and write:
  - `/runtime/account_map.json`
  - `/runtime/exports/`
  - `/runtime/sure_exports/`
- The image should not bake `.env` or runtime artifacts.
- `.dockerignore` excludes secrets, logs, exports, caches, and local virtualenvs from the build context.
- `account_map.json` is a runtime artifact and should not be committed.

- Recommended Docker command for the first-time map flow:

```sh
export RUNTIME_DIR="$HOME/redbark-sure-sync"
mkdir -p "$RUNTIME_DIR/logs"
docker run -it --rm --env-file .env -v "$RUNTIME_DIR:/runtime" -v "$RUNTIME_DIR/logs:/app/logs" ghcr.io/sunnyskye/redbark-sure-sync:latest map 30 --mapfile /runtime/account_map.json --redbark-export-dir /runtime/exports --sure-export-dir /runtime/sure_exports
```

The local map file created by that command is `"$RUNTIME_DIR/account_map.json"`.

Recommended Docker command for scheduled syncs:

```sh
docker run --rm --env-file .env -v "$RUNTIME_DIR/account_map.json:/app/account_map.json:ro" -v "$RUNTIME_DIR/exports:/app/exports" -v "$RUNTIME_DIR/logs:/app/logs" ghcr.io/sunnyskye/redbark-sure-sync:latest 4 --mapfile /app/account_map.json
```

Recommended Docker dry-run command:

```sh
docker run --rm --env-file .env -v "$RUNTIME_DIR/account_map.json:/app/account_map.json:ro" -v "$RUNTIME_DIR/exports:/app/exports" -v "$RUNTIME_DIR/logs:/app/logs" ghcr.io/sunnyskye/redbark-sure-sync:latest 4 --mapfile /app/account_map.json --dry-run
```

## Key Artifacts

- `account_map.json`
  - required by sync and audit
  - contains `mappings`
  - each mapping contains:
    - `sureAccount`
    - `redbarkConnection`
    - `redbarkAccount`
- `exports/accounts.json`
  - RedBark account catalog
- `sure_exports/accounts.json`
  - Sure account catalog
- `exports/*.json`
  - RedBark per-account transaction exports
- `sure_exports/*.json`
  - Sure per-account transaction exports

## Sync Rules

- RedBark is the authoritative source.
- Sync scope is restricted to mapped accounts only.
- Unmapped RedBark accounts are skipped.
- Sync requires `account_map.json`.
- Sync writes a stable marker into Sure transaction notes.

Marker format:

```text
[redbark:bank_tx_<transaction_id>]
```

Deduplication rule:

- If Sure already contains the same marker, the sync skips that RedBark transaction.

## Duplicate Audit Rules

- Audit reads mapped Sure accounts from `account_map.json`.
- Audit scans Sure transactions from `2000-01-01` through today.
- Audit looks only at transactions whose `notes` contain a RedBark marker.
- Duplicate condition: the same marker appears more than once in the same mapped Sure account set.

Exit semantics:

- exit code `0`: no duplicate markers found
- exit code `1`: duplicate markers found or the audit could not complete

Webhook semantics:

- If duplicates are found and `DUPLICATE_AUDIT_WEBHOOK_URL` exists, send a Discord notification.
- If duplicates are found and the webhook is missing, log an error and continue reporting the duplicate failure.
- Missing webhook by itself is not a separate hard failure.

## Orchestrator Rules

- Entry sequence:
  1. RedBark export
  2. RedBark-to-Sure sync
  3. Duplicate audit
- The orchestrator does not inline business logic for sync or audit.
- The orchestrator shells out to separate scripts for diagnosis and isolation.
- The orchestrator requires `account_map.json` before starting.
- The orchestrator uses a single-instance lock file to prevent overlap.

Lock file:

- `logs/orchestrate_redbark_sync.lock`

Recommended schedule:

- hourly
- `4` day lookback is a valid operating mode

## Common Commands

Windows examples:

```powershell
.\.venv\Scripts\python.exe redbark_export_transactions.py 30
.\.venv\Scripts\python.exe sure_export_transactions.py 30
.\.venv\Scripts\python.exe generate_account_map.py
.\.venv\Scripts\python.exe sync_redbark_to_sure.py --dry-run
.\.venv\Scripts\python.exe audit_redbark_to_sure_duplicates.py
.\.venv\Scripts\python.exe orchestrate_redbark_sync.py 4
.\.venv\Scripts\python.exe orchestrate_redbark_sync.py 4 --dry-run
```

## Logs

- `logs/redbark_export_transactions.log`
- `logs/sure_export_transactions.log`
- `logs/sync_redbark_to_sure.log`
- `logs/audit_redbark_to_sure_duplicates.log`
- `logs/orchestrate_redbark_sync.log`

## Safe Modification Guidance

- Preserve separate entrypoints unless explicitly asked to merge them.
- Preserve the marker format in Sure notes.
- Preserve the requirement for a manual account map.
- Preserve the orchestrator lock.
- Preserve `.env` as the secret boundary.
- If changing file formats, update all downstream readers.
- If adding automation, prefer a new script over folding multiple responsibilities into one file.

## Known Operational Expectations

- A dry-run orchestrator still runs export and duplicate audit; only the sync step becomes non-posting.
- Export directories are managed outputs and may be overwritten on each successful export run.
- Duplicate audit is intended as a post-sync sanity check, not a repair tool.
- Human operators are expected to inspect logs and JSON artifacts directly during troubleshooting.