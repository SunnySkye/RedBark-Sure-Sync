#!/usr/bin/env python3
"""Run the RedBark export and Sure sync in one scheduled-safe workflow."""

from __future__ import annotations

import argparse
import logging
import os
import subprocess
import sys
from pathlib import Path


DEFAULT_MAP_FILE = Path("account_map.json")
DEFAULT_OUTPUT_DIR = Path("exports")
DEFAULT_LOG_FILE = Path("logs") / "orchestrate_redbark_sync.log"
DEFAULT_LOCK_FILE = Path("logs") / "orchestrate_redbark_sync.lock"
DEFAULT_DUPLICATE_AUDIT_SCRIPT = Path("audit_redbark_to_sure_duplicates.py")
DEFAULT_TIMEOUT_SECONDS = 30
LOGGER = logging.getLogger("orchestrate_redbark_sync")


class OrchestratorError(RuntimeError):
    """Raised when the export and sync workflow cannot continue."""


class SingleInstanceLock:
    """Prevent overlapping orchestrator runs in scheduled environments."""

    def __init__(self, lock_file: Path) -> None:
        self.lock_file = lock_file
        self.handle = None

    def __enter__(self) -> SingleInstanceLock:
        self.lock_file.parent.mkdir(parents=True, exist_ok=True)
        self.handle = self.lock_file.open("a+", encoding="utf-8")
        self.handle.write("0")
        self.handle.flush()
        self.handle.seek(0)

        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.handle.close()
            self.handle = None
            raise OrchestratorError(
                f"Another orchestrator run is already in progress. Lock file: {self.lock_file}"
            ) from exc

        self.handle.seek(0)
        self.handle.write(f"pid={os.getpid()}\n")
        self.handle.truncate()
        self.handle.flush()
        return self

    def __exit__(self, exc_type, exc, exc_tb) -> None:
        if self.handle is None:
            return

        try:
            self.handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
        finally:
            self.handle.close()
            self.handle = None


def setup_logging(log_file: Path) -> None:
    log_file.parent.mkdir(parents=True, exist_ok=True)

    formatter = logging.Formatter("%(asctime)s | %(levelname)s | %(message)s")

    LOGGER.handlers.clear()
    LOGGER.setLevel(logging.DEBUG)
    LOGGER.propagate = False

    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.DEBUG)
    console_handler.setFormatter(formatter)

    file_handler = logging.FileHandler(log_file, mode="w", encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(formatter)

    LOGGER.addHandler(console_handler)
    LOGGER.addHandler(file_handler)


def positive_days(value: str) -> int:
    try:
        days = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer number of days") from exc

    if days < 1:
        raise argparse.ArgumentTypeError("must be at least 1 day")
    return days


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the RedBark export and RedBark-to-Sure sync in one go."
    )
    parser.add_argument(
        "days",
        nargs="?",
        default=1,
        type=positive_days,
        help="Number of days back from now to fetch from RedBark before syncing. Default: 1.",
    )
    parser.add_argument(
        "--map-file",
        default=str(DEFAULT_MAP_FILE),
        help="Path to the required account map JSON file. Default: account_map.json",
    )
    parser.add_argument(
        "--output-dir",
        default=str(DEFAULT_OUTPUT_DIR),
        help="Directory used for the RedBark export and then read by the sync. Default: exports",
    )
    parser.add_argument(
        "--api-key",
        help="Optional RedBark API key override for the export step.",
    )
    parser.add_argument(
        "--sure-base-url",
        help="Optional Sure base URL override for the sync step.",
    )
    parser.add_argument(
        "--sure-api-key",
        help="Optional Sure API key override for the sync step.",
    )
    parser.add_argument(
        "--duplicate-webhook-url",
        help=(
            "Optional Discord webhook URL override passed through to the duplicate audit script. "
            "If omitted, the audit script uses DUPLICATE_AUDIT_WEBHOOK_URL from the environment or .env."
        ),
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=DEFAULT_TIMEOUT_SECONDS,
        help="HTTP timeout in seconds passed to both steps. Default: 30.",
    )
    parser.add_argument(
        "--lock-file",
        default=str(DEFAULT_LOCK_FILE),
        help="Path to the orchestrator lock file used to prevent overlapping runs.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run the export normally, then run the sync in dry-run mode.",
    )
    return parser.parse_args()


def resolve_path(project_root: Path, raw_path: str) -> Path:
    path = Path(raw_path)
    if not path.is_absolute():
        path = project_root / path
    return path.resolve()


def require_file(path: Path, description: str) -> Path:
    if not path.is_file():
        raise OrchestratorError(f"{description} not found: {path}")
    return path


def run_step(name: str, command: list[str], *, cwd: Path) -> None:
    LOGGER.info("Starting %s", name)
    LOGGER.debug("Running command: %s", subprocess.list2cmdline(command))

    completed = subprocess.run(command, cwd=cwd, check=False)
    if completed.returncode != 0:
        raise OrchestratorError(f"{name} failed with exit code {completed.returncode}")

    LOGGER.info("Completed %s", name)


def main() -> int:
    args = parse_args()
    setup_logging(DEFAULT_LOG_FILE)
    LOGGER.info("Verbose logging enabled")
    LOGGER.info("Writing detailed logs to %s", DEFAULT_LOG_FILE.resolve())

    project_root = Path(__file__).resolve().parent

    try:
        lock_file = resolve_path(project_root, args.lock_file)
        map_file = require_file(resolve_path(project_root, args.map_file), "Account map file")
        output_dir = resolve_path(project_root, args.output_dir)
        export_script = require_file(project_root / "redbark_export_transactions.py", "RedBark export script")
        sync_script = require_file(project_root / "sync_redbark_to_sure.py", "Sync script")
        duplicate_audit_script = require_file(
            project_root / DEFAULT_DUPLICATE_AUDIT_SCRIPT,
            "Duplicate audit script",
        )

        LOGGER.info("Validated required account map file at %s", map_file)
        LOGGER.info("Using single-instance lock file at %s", lock_file)

        export_command = [
            sys.executable,
            str(export_script),
            str(args.days),
            "--output-dir",
            str(output_dir),
            "--timeout",
            str(args.timeout),
        ]
        if args.api_key:
            export_command.extend(["--api-key", args.api_key])

        sync_command = [
            sys.executable,
            str(sync_script),
            "--map-file",
            str(map_file),
            "--redbark-export-dir",
            str(output_dir),
            "--timeout",
            str(args.timeout),
        ]
        if args.sure_base_url:
            sync_command.extend(["--sure-base-url", args.sure_base_url])
        if args.sure_api_key:
            sync_command.extend(["--sure-api-key", args.sure_api_key])
        if args.dry_run:
            sync_command.append("--dry-run")

        audit_command = [
            sys.executable,
            str(duplicate_audit_script),
            "--map-file",
            str(map_file),
            "--timeout",
            str(args.timeout),
        ]
        if args.sure_base_url:
            audit_command.extend(["--sure-base-url", args.sure_base_url])
        if args.sure_api_key:
            audit_command.extend(["--sure-api-key", args.sure_api_key])
        if args.duplicate_webhook_url:
            audit_command.extend(["--duplicate-webhook-url", args.duplicate_webhook_url])

        with SingleInstanceLock(lock_file):
            run_step("RedBark export", export_command, cwd=project_root)
            run_step("RedBark to Sure sync", sync_command, cwd=project_root)
            run_step("RedBark duplicate audit", audit_command, cwd=project_root)

    except OrchestratorError as exc:
        LOGGER.error(str(exc))
        return 1

    LOGGER.info("Export and sync completed successfully")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())