# RedBark-Sure-Sync

RedBark-Sure-Sync is a small Python toolkit for exporting RedBark banking data, exporting Sure Finance data, interactively mapping accounts, synchronizing RedBark transactions into Sure, and auditing the result for duplicate imports.

The repository is intentionally split into separate scripts instead of one large application. Each script owns one job, writes its own log, and can be run independently for troubleshooting.

## What It Does

- Exports RedBark transactions into one JSON file per RedBark account.
- Exports Sure transactions into one JSON file per Sure account.
- Writes account catalog files for both systems.
- Builds an interactive map between Sure accounts and RedBark accounts.
- Synchronizes RedBark transactions into Sure using RedBark as the authoritative source.
- Audits Sure for duplicate RedBark sync markers.
- Orchestrates export, sync, and duplicate audit in one scheduled-safe command.

## Requirements

- Python 3.11+.
- No third-party Python packages are required. The scripts use the standard library only.
- A root-level `.env` file with the required API settings.
- Docker is optional, but supported for the orchestrated sync workflow.

Example `.env` shape:

```env
REDBARK_API_KEY=your_redbark_api_key
SURE_BASE_URL=https://your-sure-instance.example
SURE_API_KEY=your_sure_api_key
DUPLICATE_AUDIT_WEBHOOK_URL=https://discord.com/api/webhooks/...
```

Notes:

- `DUPLICATE_AUDIT_WEBHOOK_URL` is optional.
- If the duplicate audit finds duplicates and the webhook is missing, the audit logs that alerting is unavailable but still fails because duplicates were found.
- Secrets stay in `.env`, which is gitignored.

## Repository Layout

- `redbark_export_transactions.py`: Export RedBark accounts and transactions into `exports/`.
- `sure_export_transactions.py`: Export Sure accounts and transactions into `sure_exports/`.
- `generate_account_map.py`: Interactively create `account_map.json`.
- `sync_redbark_to_sure.py`: Read the RedBark export files and create missing Sure transactions.
- `audit_redbark_to_sure_duplicates.py`: Check mapped Sure accounts for duplicate RedBark sync markers.
- `orchestrate_redbark_sync.py`: Run export, sync, and duplicate audit in sequence.
- `exports/`: RedBark account catalog and per-account transaction exports.
- `sure_exports/`: Sure account catalog and per-account transaction exports.
- `logs/`: Per-script log files and the orchestrator lock file.

## Generated Artifacts

The scripts treat some directories as managed outputs.

- `exports/accounts.json`: RedBark account catalog.
- `exports/*.json`: One RedBark transaction export per account.
- `sure_exports/accounts.json`: Sure account catalog.
- `sure_exports/*.json`: One Sure transaction export per account.
- `account_map.json`: Interactive map between Sure and RedBark accounts.
- `logs/*.log`: One log file per script, overwritten on each run of that script.
- `logs/orchestrate_redbark_sync.lock`: Single-instance lock for the orchestrator.

## Typical Workflow

### 1. Export RedBark

```powershell
.\.venv\Scripts\python.exe redbark_export_transactions.py 30
```

This writes:

- `exports/accounts.json`
- one JSON file per RedBark account in `exports/`

### 2. Export Sure

```powershell
.\.venv\Scripts\python.exe sure_export_transactions.py 30
```

This writes:

- `sure_exports/accounts.json`
- one JSON file per Sure account in `sure_exports/`

### 3. Build the Account Map

```powershell
.\.venv\Scripts\python.exe generate_account_map.py
```

This script is interactive. It does not auto-match accounts. It reads:

- `exports/accounts.json`
- `sure_exports/accounts.json`

It writes:

- `account_map.json`

### 4. Dry-Run the Sync

```powershell
.\.venv\Scripts\python.exe sync_redbark_to_sure.py --dry-run
```

This verifies what would be created in Sure without posting new transactions.

### 5. Run the Duplicate Audit

```powershell
.\.venv\Scripts\python.exe audit_redbark_to_sure_duplicates.py
```

This checks mapped Sure accounts for repeated RedBark sync markers.

### 6. Run the Full Orchestrator

```powershell
.\.venv\Scripts\python.exe orchestrate_redbark_sync.py 4
```

This runs three steps in order:

1. RedBark export
2. RedBark-to-Sure sync
3. Duplicate audit

## Scheduled Usage

The intended scheduled command is the orchestrator, not the sync script by itself.

Example hourly run with a 4-day lookback:

```powershell
.\.venv\Scripts\python.exe orchestrate_redbark_sync.py 4
```

Why this is safe:

- The orchestrator refreshes the RedBark export before syncing.
- The sync is idempotent for marker-backed transactions.
- The orchestrator uses a lock file to prevent overlapping runs.
- The duplicate audit runs after sync and can alert if something abnormal appears.

Scheduler recommendations:

- Run the orchestrator every hour.
- Keep the 4-day lookback if you want late-arriving transactions to be rechecked.
- Also configure your scheduler to avoid starting a second instance while one is still running.

## Docker

The repository includes a `Dockerfile` for local image builds and a GitHub release workflow that publishes a ready-to-run image to GitHub Container Registry.

The supported Docker operator path is two raw `docker run` commands. You do not need a host-side Python wrapper.

- `--env-file` stays a Docker option for loading the host `.env` file.
- `--mapfile` is a container argument that tells the sync which mounted map file to use.

The flow is:

1. generate the map file once
2. schedule the sync command with the same `.env` file and saved map file

### 1. Generate the Map File

Choose a host runtime directory first. The example below uses the current directory so the generated files land beside your shell session.

Run the container in interactive `map` mode:

```powershell
docker run -it --rm --env-file .env -v "${PWD}:/runtime" -v "${PWD}\logs:/app/logs" ghcr.io/sunnyskye/redbark-sure-sync:latest map 30 --mapfile /runtime/account_map.json --redbark-export-dir /runtime/exports --sure-export-dir /runtime/sure_exports
```

What this does:

- runs the container in interactive `map` mode
- fetches the RedBark and Sure account catalogs needed for mapping
- launches the interactive account mapper
- writes the generated map file to the mounted host path at `account_map.json`
- stores bootstrap `exports/`, `sure_exports/`, and `logs/` on the host

### 2. Run the Scheduled Sync

Run the scheduled-safe orchestrator with the saved map file:

```powershell
docker run --rm --env-file .env -v "${PWD}\account_map.json:/app/account_map.json:ro" -v "${PWD}\exports:/app/exports" -v "${PWD}\logs:/app/logs" ghcr.io/sunnyskye/redbark-sure-sync:latest 4 --mapfile /app/account_map.json
```

Dry run:

```powershell
docker run --rm --env-file .env -v "${PWD}\account_map.json:/app/account_map.json:ro" -v "${PWD}\exports:/app/exports" -v "${PWD}\logs:/app/logs" ghcr.io/sunnyskye/redbark-sure-sync:latest 4 --mapfile /app/account_map.json --dry-run
```

What this does:

- mounts the selected map file read-only into the container
- keeps `exports/` and `logs/` on the host
- runs `orchestrate_redbark_sync.py` inside the container
- makes the scheduled command explicit about both the env file and the map file

This is the intended command to hand to cron, launchd, or Task Scheduler.

### Runtime Files

Before running the scheduled sync, make sure the host has these runtime files or directories:

- `.env`
- `account_map.json`
- `exports/`
- `logs/`
- `sure_exports/` only matters for the first-time map command

`account_map.json` remains a runtime artifact. It is intentionally ignored by git and should stay outside published release contents.

Build the image locally:

```powershell
docker build -t redbark-sure-sync .
```

If you use the local image instead of GHCR, the same two commands become:

```powershell
docker run -it --rm --env-file .env -v "${PWD}:/runtime" -v "${PWD}\logs:/app/logs" redbark-sure-sync map 30 --mapfile /runtime/account_map.json --redbark-export-dir /runtime/exports --sure-export-dir /runtime/sure_exports
```

```powershell
docker run --rm --env-file .env -v "${PWD}\account_map.json:/app/account_map.json:ro" -v "${PWD}\exports:/app/exports" -v "${PWD}\logs:/app/logs" redbark-sure-sync 4 --mapfile /app/account_map.json
```

Docker cannot open a host file picker for you. To choose a different map-file location, change the host side of the bind mount and keep `--mapfile` pointed at the in-container path.

If you prefer not to use `--env-file`, you can pass the environment variables directly with `-e`, but `--env-file .env` remains the intended path.

## How Sync Deduplication Works

RedBark is treated as the source of truth.

For each synced Sure transaction, the sync script writes a stable marker into `notes`:

```text
[redbark:bank_tx_<transaction_id>]
```

The sync then uses that marker to decide whether a RedBark transaction is already present in Sure.

Important behavior:

- Marker-backed reruns are skipped.
- Unmapped RedBark accounts are skipped.
- `account_map.json` is required.
- The sync does not auto-create or infer account mappings.

## Duplicate Audit Behavior

The duplicate audit script:

- reads `account_map.json`
- fetches Sure transactions for each mapped Sure account
- inspects `notes` for repeated `[redbark:bank_tx_<id>]` markers
- exits with code `1` if duplicates are found

Webhook behavior:

- If duplicates are found and `DUPLICATE_AUDIT_WEBHOOK_URL` exists, the script sends a Discord notification.
- If duplicates are found and the webhook is missing or fails, the script logs that problem and still reports the duplicate audit failure.
- Missing webhook configuration alone is not treated as a separate hard failure.

## Logs

Each script writes its own log file:

- `logs/redbark_export_transactions.log`
- `logs/sure_export_transactions.log`
- `logs/sync_redbark_to_sure.log`
- `logs/audit_redbark_to_sure_duplicates.log`
- `logs/orchestrate_redbark_sync.log`

These logs are the first place to look when diagnosing a problem.

## Troubleshooting

### The orchestrator exits because the account map is missing

Run:

```powershell
.\.venv\Scripts\python.exe generate_account_map.py
```

### The orchestrator says another run is already in progress

Check:

- `logs/orchestrate_redbark_sync.lock`
- your scheduler settings

### The sync creates nothing

Possible reasons:

- the RedBark lookback window contains no new transactions
- the transactions were already synced earlier
- the mapped RedBark accounts currently have no transactions in the exported window

### The duplicate audit fails

Check:

- `logs/audit_redbark_to_sure_duplicates.log`
- the Discord alert, if configured
- the affected Sure transactions and their `notes` values

## Recommended Commands

Dry-run orchestration with the same 4-day lookback used in production:

```powershell
.\.venv\Scripts\python.exe orchestrate_redbark_sync.py 4 --dry-run
```

Manual duplicate audit:

```powershell
.\.venv\Scripts\python.exe audit_redbark_to_sure_duplicates.py
```

RedBark-only export refresh:

```powershell
.\.venv\Scripts\python.exe redbark_export_transactions.py 4
```

## Design Notes

This repo currently prefers:

- separate scripts over one merged program
- explicit file-based artifacts over hidden state
- standard-library-only Python
- simple logs and JSON outputs that are easy to inspect by hand

If you extend the project, keep that shape unless there is a strong reason not to.