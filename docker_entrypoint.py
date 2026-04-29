#!/usr/bin/env python3
"""Container entrypoint for Docker-based RedBark-to-Sure workflows."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path


DEFAULT_TIMEOUT_SECONDS = 30
DEFAULT_SYNC_MAP_FILE = Path("account_map.json")
DEFAULT_BOOTSTRAP_DAYS = 30
DEFAULT_BOOTSTRAP_MAP_FILE = Path("/runtime/account_map.json")
DEFAULT_BOOTSTRAP_REDBARK_EXPORT_DIR = Path("/runtime/exports")
DEFAULT_BOOTSTRAP_SURE_EXPORT_DIR = Path("/runtime/sure_exports")
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
    print("Usage:")
    print("  docker run --rm ... <image> [orchestrator-args]")
    print("  docker run -it --rm ... <image> map [map-args]")
    print()
    print("Modes:")
    print("  default  Run orchestrate_redbark_sync.py with the provided arguments.")
    print("  map      Fetch account catalogs and launch generate_account_map.py interactively.")
    print()
    print("First-time setup example:")
    print(
        "  docker run -it --rm --env-file .env -v \"${PWD}:/runtime\" "
        f"-v \"${{PWD}}\\logs:/app/logs\" {IMAGE_REFERENCE_HINT} "
        "map 30 --mapfile /runtime/account_map.json "
        "--redbark-export-dir /runtime/exports --sure-export-dir /runtime/sure_exports"
    )
    print()
    print("Normal sync example:")
    print(
        "  docker run --rm --env-file .env "
        f"-v \"${{PWD}}\\account_map.json:/app/account_map.json:ro\" "
        f"-v \"${{PWD}}\\exports:/app/exports\" -v \"${{PWD}}\\logs:/app/logs\" "
        f"{IMAGE_REFERENCE_HINT} 4 --mapfile /app/account_map.json"
    )


def format_sync_args(args: list[str]) -> str:
    if not args:
        return "1"
    return " ".join(args)


def option_value(args: list[str], option_name: str) -> str | None:
    prefix = f"{option_name}="
    for index, argument in enumerate(args):
        if argument == option_name:
            if index + 1 < len(args):
                return args[index + 1]
            return None
        if argument.startswith(prefix):
            return argument[len(prefix) :]
    return None


def print_missing_map_guidance(map_file: Path, sync_args: list[str]) -> None:
    print(f"Account map file not found: {map_file}", file=sys.stderr)
    print(file=sys.stderr)
    print(
        "This container cannot prebuild account_map.json during docker build because the "
        "file depends on live API data and your manual account choices.",
        file=sys.stderr,
    )
    print(
        "Generate it once with the interactive map mode, save it on the host, then rerun "
        "the sync command.",
        file=sys.stderr,
    )
    print(file=sys.stderr)
    print("First-time account-map setup in PowerShell:", file=sys.stderr)
    print(
        "  docker run -it --rm --env-file .env -v \"${PWD}:/runtime\" "
        f"-v \"${{PWD}}\\logs:/app/logs\" {IMAGE_REFERENCE_HINT} "
        "map 30 --mapfile /runtime/account_map.json "
        "--redbark-export-dir /runtime/exports --sure-export-dir /runtime/sure_exports",
        file=sys.stderr,
    )
    print(file=sys.stderr)
    print("Then rerun your sync/orchestrator command:", file=sys.stderr)
    print(
        "  docker run --rm --env-file .env "
        f"-v \"${{PWD}}\\account_map.json:/app/account_map.json:ro\" "
        f"-v \"${{PWD}}\\exports:/app/exports\" -v \"${{PWD}}\\logs:/app/logs\" "
        f"{IMAGE_REFERENCE_HINT} {format_sync_args(sync_args)} --mapfile /app/account_map.json",
        file=sys.stderr,
    )
    print(file=sys.stderr)
    print(
        "If you want to store runtime files somewhere else, mount that host folder to "
        "/runtime and keep --mapfile, --redbark-export-dir, and --sure-export-dir under /runtime.",
        file=sys.stderr,
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
    map_file_arg = (
        option_value(argv, "--mapfile")
        or option_value(argv, "--map-file")
        or str(DEFAULT_SYNC_MAP_FILE)
    )
    map_file = resolve_path(project_root, map_file_arg)
    if not map_file.is_file():
        print_missing_map_guidance(map_file, argv)
        return 1
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