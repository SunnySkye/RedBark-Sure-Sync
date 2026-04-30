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
REDBARK_SURE_ACCOUNT_MAP_BASE64=
```

Notes:

- `DUPLICATE_AUDIT_WEBHOOK_URL` is optional.
- `REDBARK_SURE_ACCOUNT_MAP_BASE64` is optional. If set, the sync and duplicate audit can use the account map from the environment instead of reading `account_map.json` from disk.
- If the duplicate audit finds duplicates and the webhook is missing, the audit logs that alerting is unavailable but still fails because duplicates were found.
- Secrets stay in `.env`, which is gitignored.

## Repository Layout

- `redbark_export_transactions.py`: Export RedBark accounts and transactions into `exports/`.
- `sure_export_transactions.py`: Export Sure accounts and transactions into `sure-transactions/`.
- `generate_account_map.py`: Interactively create `account_map.json`.
- `sync_redbark_to_sure.py`: Read the RedBark export files and create missing Sure transactions.
- `audit_redbark_to_sure_duplicates.py`: Check mapped Sure accounts for duplicate RedBark sync markers.
- `orchestrate_redbark_sync.py`: Run export, sync, and duplicate audit in sequence.
- `exports/`: RedBark account catalog and per-account transaction exports.
- `sure-transactions/`: Sure account catalog and per-account transaction exports.
- `logs/`: Per-script log files and the orchestrator lock file.

## Generated Artifacts

The scripts treat some directories as managed outputs.

- `exports/accounts.json`: RedBark account catalog.
- `exports/*.json`: One RedBark transaction export per account.
- `sure-transactions/accounts.json`: Sure account catalog.
- `sure-transactions/*.json`: One Sure transaction export per account.
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

- `sure-transactions/accounts.json`
- one JSON file per Sure account in `sure-transactions/`

### 3. Build the Account Map

```powershell
.\.venv\Scripts\python.exe generate_account_map.py
```

This script is interactive. It does not auto-match accounts. It reads:

- `exports/accounts.json`
- `sure-transactions/accounts.json`

If either catalog is missing, the script now offers to run the existing export scripts for you so a fresh environment can bootstrap the required `accounts.json` files before mapping starts.

It writes:

- `account_map.json`

After the file is written, the script also prints a `REDBARK_SURE_ACCOUNT_MAP_BASE64=...` line that you can copy into `.env` for Docker or other environments where mounting `account_map.json` is inconvenient.

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

If you use the published image, the container can run on an arbitrary Linux machine with Docker. A host-side repo checkout is not required for the normal sync path.

The supported Docker operator path is two raw `docker run` commands. You do not need a host-side Python wrapper.

- `--env-file` stays a Docker option for loading the host `.env` file.
- `--mapfile` is a container argument that tells the sync which mounted map file to use.
- `REDBARK_SURE_ACCOUNT_MAP_BASE64` is an optional alternative to mounting `account_map.json`.

The flow is:

1. generate the map file once
2. schedule the sync command with the same `.env` file and saved map file

Choose one host runtime directory first. That directory holds the local `account_map.json`, `exports/`, `sure-transactions/`, and `logs/` folders used by both commands.

The examples below use a deliberate runtime directory choice instead of the shell's current working directory.

On Linux/macOS shells, start by choosing one path you actually want to keep using:

```sh
export RUNTIME_DIR="$HOME/redbark-sure-sync"
export ENV_FILE="$HOME/redbark-sure-sync.env"
export IMAGE="ghcr.io/sunnyskye/redbark-sure-sync:latest"
```

If you are using PowerShell, choose an equivalent absolute path such as `$RUNTIME_DIR = "$HOME\redbark-sure-sync"`.

### Docker Env File

Before running the first Docker command, create the env file at `$ENV_FILE`.

If you keep the commands exactly as written below, that means creating this host file:

```text
$HOME/redbark-sure-sync.env
```

Example contents:

```env
REDBARK_API_KEY=your_redbark_api_key
SURE_BASE_URL=https://your-sure-instance.example
SURE_API_KEY=your_sure_api_key
DUPLICATE_AUDIT_WEBHOOK_URL=https://discord.com/api/webhooks/...
```

Notes:

- `DUPLICATE_AUDIT_WEBHOOK_URL` is optional.
- `REDBARK_SURE_ACCOUNT_MAP_BASE64` is optional. Paste the line printed by `generate_account_map.py` if you want Docker to receive the map through `--env-file` instead of a bind-mounted `account_map.json`.
- The env file can live anywhere on the host as long as `--env-file` points to that exact path.
- If you prefer a different location, change `ENV_FILE` and keep using the updated value in both Docker commands.

### Linux Quick Start

On a fresh Linux machine with Docker installed and running, the normal orchestrator path is:

```sh
export RUNTIME_DIR="$HOME/redbark-sure-sync"
export ENV_FILE="$HOME/redbark-sure-sync.env"
export IMAGE="ghcr.io/sunnyskye/redbark-sure-sync:latest"

mkdir -p "$RUNTIME_DIR/exports" "$RUNTIME_DIR/logs"
docker pull "$IMAGE"
docker run --rm \
	--env-file "$ENV_FILE" \
	-v "$RUNTIME_DIR/exports:/app/exports" \
	-v "$RUNTIME_DIR/logs:/app/logs" \
	"$IMAGE" 4
```

What that command does:

- runs `orchestrate_redbark_sync.py` inside the container
- refreshes the RedBark export into the host `exports/` directory
- syncs into Sure
- runs the duplicate audit
- leaves the logs on the host in `logs/`

This exact command assumes your env file already contains `REDBARK_SURE_ACCOUNT_MAP_BASE64`. If it does not, generate `account_map.json` once with the interactive map flow below and mount it into the container.

Dry run on Linux:

```sh
docker run --rm \
	--env-file "$ENV_FILE" \
	-v "$RUNTIME_DIR/exports:/app/exports" \
	-v "$RUNTIME_DIR/logs:/app/logs" \
	"$IMAGE" 4 --dry-run
```

### 1. Generate the Map File

The example below uses the local runtime directory stored in `$RUNTIME_DIR`.

Run the container in interactive `map` mode:

```sh
mkdir -p "$RUNTIME_DIR/logs"
docker run -it --rm --env-file "$ENV_FILE" -v "$RUNTIME_DIR:/runtime" -v "$RUNTIME_DIR/logs:/app/logs" "$IMAGE" map 30 --mapfile /runtime/account_map.json --redbark-export-dir /runtime/exports --sure-export-dir /runtime/sure-transactions
```

What this does:

- runs the container in interactive `map` mode
- fetches the RedBark and Sure account catalogs needed for mapping
- launches the interactive account mapper
- writes the local file `"$RUNTIME_DIR/account_map.json"`
- stores bootstrap exports in `"$RUNTIME_DIR/exports"`
- stores Sure bootstrap exports in `"$RUNTIME_DIR/sure-transactions"`
- stores logs in `"$RUNTIME_DIR/logs"`

### 2. Run the Scheduled Sync

Mount that same local map file back into the container:

```sh
docker run --rm --env-file "$ENV_FILE" -v "$RUNTIME_DIR/account_map.json:/app/account_map.json:ro" -v "$RUNTIME_DIR/exports:/app/exports" -v "$RUNTIME_DIR/logs:/app/logs" "$IMAGE" 4 --mapfile /app/account_map.json
```

If your env file already includes `REDBARK_SURE_ACCOUNT_MAP_BASE64`, you can run the orchestrator without mounting `account_map.json` at all:

```sh
docker run --rm --env-file "$ENV_FILE" -v "$RUNTIME_DIR/exports:/app/exports" -v "$RUNTIME_DIR/logs:/app/logs" "$IMAGE" 4
```

Dry run:

```sh
docker run --rm --env-file "$ENV_FILE" -v "$RUNTIME_DIR/account_map.json:/app/account_map.json:ro" -v "$RUNTIME_DIR/exports:/app/exports" -v "$RUNTIME_DIR/logs:/app/logs" "$IMAGE" 4 --mapfile /app/account_map.json --dry-run
```

What this does:

- mounts the selected map file read-only into the container
- keeps `exports/` and `logs/` on the host
- runs `orchestrate_redbark_sync.py` inside the container
- makes the scheduled command explicit about both the env file and the map file

This is the intended command to hand to cron, launchd, or Task Scheduler.

### Runtime Files

After step 1 completes, your host runtime directory should contain:

- `$RUNTIME_DIR/account_map.json`
- `$RUNTIME_DIR/exports/`
- `$RUNTIME_DIR/sure-transactions/`
- `$RUNTIME_DIR/logs/`

The scheduled sync command in step 2 must mount that exact local file path back into `/app/account_map.json`.

If `REDBARK_SURE_ACCOUNT_MAP_BASE64` is set in the env file, the sync and duplicate audit can read the mapping from the environment instead, so mounting `account_map.json` becomes optional.

Your env file stays outside the runtime directory and is passed through Docker with `--env-file "$ENV_FILE"`.

For cron, systemd, or other schedulers, prefer the fully expanded absolute path instead of relying on `$RUNTIME_DIR` being set in that environment.

`account_map.json` remains a runtime artifact. It is intentionally ignored by git and should stay outside published release contents.

Build the image locally:

```sh
docker build -t redbark-sure-sync .
```

If the Linux machine does not have a local repo checkout, skip the local build and use `docker pull "$IMAGE"` instead.

If you use the local image instead of GHCR, the same two commands become:

```sh
mkdir -p "$RUNTIME_DIR/logs"
docker run -it --rm --env-file "$ENV_FILE" -v "$RUNTIME_DIR:/runtime" -v "$RUNTIME_DIR/logs:/app/logs" redbark-sure-sync map 30 --mapfile /runtime/account_map.json --redbark-export-dir /runtime/exports --sure-export-dir /runtime/sure-transactions
```

```sh
docker run --rm --env-file "$ENV_FILE" -v "$RUNTIME_DIR/account_map.json:/app/account_map.json:ro" -v "$RUNTIME_DIR/exports:/app/exports" -v "$RUNTIME_DIR/logs:/app/logs" redbark-sure-sync 4 --mapfile /app/account_map.json
```

Docker cannot open a host file picker for you. To use a different local path, set `RUNTIME_DIR` to the host directory you want and keep `--mapfile` pointed at the in-container path.

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