#!/usr/bin/env python3
"""Container entrypoint for Docker-based RedBark-to-Sure workflows."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path


DEFAULT_TIMEOUT_SECONDS = 30
DEFAULT_BOOTSTRAP_DAYS = 30
DEFAULT_BOOTSTRAP_MAP_FILE = Path("/runtime/account_map.json")
DEFAULT_BOOTSTRAP_REDBARK_EXPORT_DIR = Path("/runtime/exports")
DEFAULT_BOOTSTRAP_SURE_EXPORT_DIR = Path("/runtime/sure-transactions")
IMAGE_REFERENCE_HINT = os.environ.get(
    "REDBARK_SURE_SYNC_IMAGE_HINT",
    "ghcr.io/sunnyskye/redbark-sure-sync:latest",
)


def positive_days(value: str) -> int:
    try:
        days = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer number of days") from exc

    if days < 1:
        raise argparse.ArgumentTypeError("must be at least 1 day")
    return days


def resolve_path(project_root: Path, raw_path: str) -> Path:
    path = Path(raw_path)
    if not path.is_absolute():
        path = project_root / path
    return path.resolve()


def require_script(project_root: Path, name: str) -> Path:
    path = project_root / name
    if not path.is_file():
        raise RuntimeError(f"Required script not found: {path}")
    return path


def run_python_script(project_root: Path, script_name: str, args: list[str]) -> int:
    script_path = require_script(project_root, script_name)
    command = [sys.executable, str(script_path), *args]
    return subprocess.run(command, cwd=project_root, check=False).returncode


def parse_map_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Fetch RedBark and Sure account catalogs, then launch the interactive "
            "account map generator inside the container."
        )
    )
    parser.add_argument(
        "days",
        nargs="?",
        default=DEFAULT_BOOTSTRAP_DAYS,
        type=positive_days,
        help=(
            "Number of days back to use for the bootstrap exports. "
            f"Default: {DEFAULT_BOOTSTRAP_DAYS}."
        ),
    )
    parser.add_argument(
        "--mapfile",
        "--map-file",
        dest="map_file",
        default=str(DEFAULT_BOOTSTRAP_MAP_FILE),
        help=(
            "Path where the generated account map JSON will be written. "
            f"Default: {DEFAULT_BOOTSTRAP_MAP_FILE}"
        ),
    )
    parser.add_argument(
        "--redbark-export-dir",
        default=str(DEFAULT_BOOTSTRAP_REDBARK_EXPORT_DIR),
        help=(
            "Directory where the RedBark bootstrap export will be written. "
            f"Default: {DEFAULT_BOOTSTRAP_REDBARK_EXPORT_DIR}"
        ),
    )
    parser.add_argument(
        "--sure-export-dir",
        default=str(DEFAULT_BOOTSTRAP_SURE_EXPORT_DIR),
        help=(
            "Directory where the Sure bootstrap export will be written. "
            f"Default: {DEFAULT_BOOTSTRAP_SURE_EXPORT_DIR}"
        ),
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=DEFAULT_TIMEOUT_SECONDS,
        help=f"HTTP timeout in seconds passed to both bootstrap exports. Default: {DEFAULT_TIMEOUT_SECONDS}.",
    )
    parser.add_argument(
        "--api-key",
        help="Optional RedBark API key override for the bootstrap export.",
    )
    parser.add_argument(
        "--sure-base-url",
        help="Optional Sure base URL override for the bootstrap export.",
    )
    parser.add_argument(
        "--sure-api-key",
        help="Optional Sure API key override for the bootstrap export.",
    )
    return parser.parse_args(argv)


def print_container_help() -> None:
    print("RedBark-Sure-Sync Docker entrypoint")
    print()
    print("The published image can run on an arbitrary Linux machine with Docker and an env file.")
    print("You do not need a repo checkout on the host when you use the GHCR image.")
    print()
    print("Usage:")
    print("  docker run --rm ... <image> [orchestrator-args]")
    print("  docker run -it --rm ... <image> map [map-args]")
    print()
    print("Modes:")
    print("  default  Run orchestrate_redbark_sync.py with the provided arguments.")
    print("  map      Fetch account catalogs and launch generate_account_map.py interactively.")
    print()
    print("Choose one host runtime directory first, for example /absolute/path/to/redbark-runtime.")
    print("Choose one host env file path too, for example /absolute/path/to/redbark-sure-sync.env.")
    print()
    print("Linux quick start:")
    print(f"  docker pull {IMAGE_REFERENCE_HINT}")
    print(
        "  docker run --rm --env-file \"/absolute/path/to/redbark-sure-sync.env\" "
        "-v \"/absolute/path/to/redbark-runtime/exports:/app/exports\" "
        "-v \"/absolute/path/to/redbark-runtime/logs:/app/logs\" "
        f"{IMAGE_REFERENCE_HINT} 4"
    )
    print()
    print("First-time setup example:")
    print(
        "  docker run -it --rm --env-file \"/absolute/path/to/redbark-sure-sync.env\" -v \"/absolute/path/to/redbark-runtime:/runtime\" "
        f"-v \"/absolute/path/to/redbark-runtime/logs:/app/logs\" {IMAGE_REFERENCE_HINT} "
        "map 30 --mapfile /runtime/account_map.json "
        "--redbark-export-dir /runtime/exports --sure-export-dir /runtime/sure-transactions"
    )
    print()
    print("Normal sync example with an env-based account map:")
    print(
        "  docker run --rm --env-file \"/absolute/path/to/redbark-sure-sync.env\" "
        f"-v \"/absolute/path/to/redbark-runtime/exports:/app/exports\" "
        f"-v \"/absolute/path/to/redbark-runtime/logs:/app/logs\" "
        f"{IMAGE_REFERENCE_HINT} 4"
    )
    print("  That command runs orchestrate_redbark_sync.py with a 4-day lookback.")
    print()
    print("Normal sync example with a mounted account map file:")
    print(
        "  docker run --rm --env-file \"/absolute/path/to/redbark-sure-sync.env\" "
        f"-v \"/absolute/path/to/redbark-runtime/account_map.json:/app/account_map.json:ro\" "
        f"-v \"/absolute/path/to/redbark-runtime/exports:/app/exports\" "
        f"-v \"/absolute/path/to/redbark-runtime/logs:/app/logs\" "
        f"{IMAGE_REFERENCE_HINT} 4 --mapfile /app/account_map.json"
    )


def run_map_mode(project_root: Path, argv: list[str]) -> int:
    args = parse_map_args(argv)
    map_file = resolve_path(project_root, args.map_file)
    redbark_export_dir = resolve_path(project_root, args.redbark_export_dir)
    sure_export_dir = resolve_path(project_root, args.sure_export_dir)

    print("Bootstrapping RedBark and Sure account catalogs for interactive mapping...")

    redbark_args = [
        str(args.days),
        "--output-dir",
        str(redbark_export_dir),
        "--timeout",
        str(args.timeout),
    ]
    if args.api_key:
        redbark_args.extend(["--api-key", args.api_key])

    sure_args = [
        str(args.days),
        "--output-dir",
        str(sure_export_dir),
        "--timeout",
        str(args.timeout),
    ]
    if args.sure_base_url:
        sure_args.extend(["--sure-base-url", args.sure_base_url])
    if args.sure_api_key:
        sure_args.extend(["--sure-api-key", args.sure_api_key])

    map_args = [
        "--redbark-accounts-file",
        str(redbark_export_dir / "accounts.json"),
        "--sure-accounts-file",
        str(sure_export_dir / "accounts.json"),
        "--output-file",
        str(map_file),
    ]

    for script_name, script_args in (
        ("redbark_export_transactions.py", redbark_args),
        ("sure_export_transactions.py", sure_args),
        ("generate_account_map.py", map_args),
    ):
        exit_code = run_python_script(project_root, script_name, script_args)
        if exit_code != 0:
            return exit_code

    print()
    print(f"Interactive account map created at {map_file}")
    print("You can now rerun the container in normal sync mode.")
    return 0


def run_sync_mode(project_root: Path, argv: list[str]) -> int:
    return run_python_script(project_root, "orchestrate_redbark_sync.py", argv)


def main() -> int:
    project_root = Path(__file__).resolve().parent
    argv = sys.argv[1:]

    if argv and argv[0] in {"help", "--help", "-h"}:
        print_container_help()
        return 0

    if argv and argv[0] == "map":
        return run_map_mode(project_root, argv[1:])

    return run_sync_mode(project_root, argv)


if __name__ == "__main__":
    raise SystemExit(main())