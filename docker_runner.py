#!/usr/bin/env python3
"""Host-side helper for running the RedBark-Sure-Sync Docker image."""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path, PurePosixPath


DEFAULT_IMAGE = os.environ.get(
    "REDBARK_SURE_SYNC_IMAGE",
    "ghcr.io/sunnyskye/redbark-sure-sync:latest",
)
DEFAULT_MAP_DAYS = 30
DEFAULT_SYNC_DAYS = 4
DEFAULT_MAP_FILE_NAME = "account_map.json"
CONTAINER_RUNTIME_ROOT = PurePosixPath("/runtime")
CONTAINER_LOGS_DIR = PurePosixPath("/app/logs")
CONTAINER_EXPORTS_DIR = PurePosixPath("/app/exports")
CONTAINER_SURE_EXPORTS_DIR = PurePosixPath("/runtime/sure_exports")
CONTAINER_SYNC_MAP_FILE = PurePosixPath("/app/account_map.json")


def positive_days(value: str) -> int:
    try:
        days = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer number of days") from exc

    if days < 1:
        raise argparse.ArgumentTypeError("must be at least 1 day")
    return days


def resolve_existing_file(raw_path: str, description: str) -> Path:
    path = Path(raw_path).expanduser().resolve()
    if not path.is_file():
        raise RuntimeError(f"{description} not found: {path}")
    return path


def normalize_map_file_path(raw_value: str, default_path: Path) -> Path:
    explicit_directory = raw_value.endswith(("/", "\\"))
    candidate = Path(raw_value).expanduser() if raw_value else default_path
    if explicit_directory or (candidate.exists() and candidate.is_dir()):
        candidate = candidate / default_path.name
    return candidate.resolve()


def prompt_for_map_file() -> Path:
    default_path = (Path.cwd() / DEFAULT_MAP_FILE_NAME).resolve()
    if not sys.stdin.isatty():
        raise RuntimeError("Map generation requires --mapfile when stdin is not interactive.")

    response = input(
        f"Where should the generated account map be saved? [{default_path}] "
    ).strip()
    return normalize_map_file_path(response, default_path)


def ensure_directory(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def ensure_docker_available() -> None:
    if shutil.which("docker") is None:
        raise RuntimeError("Docker CLI not found on PATH.")


def bind_mount_args(source: Path, target: PurePosixPath, *, read_only: bool = False) -> list[str]:
    mount_spec = f"type=bind,source={source},target={target}"
    if read_only:
        mount_spec += ",readonly"
    return ["--mount", mount_spec]


def passthrough_args(raw_args: list[str]) -> list[str]:
    if raw_args and raw_args[0] == "--":
        return raw_args[1:]
    return raw_args


def run_command(command: list[str]) -> int:
    return subprocess.run(command, check=False).returncode


def print_map_summary(image: str, env_file: Path, map_file: Path) -> None:
    runtime_root = map_file.parent
    print(f"Docker image: {image}")
    print(f"Env file: {env_file}")
    print(f"Map file will be written to: {map_file}")
    print(f"Bootstrap exports and logs will be stored under: {runtime_root}")


def print_sync_summary(image: str, env_file: Path, map_file: Path) -> None:
    runtime_root = map_file.parent
    print(f"Docker image: {image}")
    print(f"Env file: {env_file}")
    print(f"Using account map: {map_file}")
    print(f"Exports and logs will be stored under: {runtime_root}")


def run_map(args: argparse.Namespace) -> int:
    ensure_docker_available()
    env_file = resolve_existing_file(args.envfile, "Env file")
    map_file = (
        normalize_map_file_path(args.mapfile, (Path.cwd() / DEFAULT_MAP_FILE_NAME).resolve())
        if args.mapfile
        else prompt_for_map_file()
    )
    runtime_root = ensure_directory(map_file.parent)
    logs_dir = ensure_directory(runtime_root / "logs")
    ensure_directory(runtime_root / "exports")
    ensure_directory(runtime_root / "sure_exports")
    container_map_file = CONTAINER_RUNTIME_ROOT / map_file.name

    print_map_summary(args.image, env_file, map_file)

    command = [
        "docker",
        "run",
        "-it",
        "--rm",
        "--env-file",
        str(env_file),
        *bind_mount_args(runtime_root, CONTAINER_RUNTIME_ROOT),
        *bind_mount_args(logs_dir, CONTAINER_LOGS_DIR),
        args.image,
        "map",
        str(args.days),
        "--map-file",
        str(container_map_file),
        "--redbark-export-dir",
        str(CONTAINER_RUNTIME_ROOT / "exports"),
        "--sure-export-dir",
        str(CONTAINER_SURE_EXPORTS_DIR),
        *passthrough_args(args.map_args),
    ]
    return run_command(command)


def run_sync(args: argparse.Namespace) -> int:
    ensure_docker_available()
    env_file = resolve_existing_file(args.envfile, "Env file")
    map_file = resolve_existing_file(args.mapfile, "Account map file")
    runtime_root = map_file.parent
    logs_dir = ensure_directory(runtime_root / "logs")
    exports_dir = ensure_directory(runtime_root / "exports")

    print_sync_summary(args.image, env_file, map_file)

    command = [
        "docker",
        "run",
        "--rm",
        "--env-file",
        str(env_file),
        *bind_mount_args(map_file, CONTAINER_SYNC_MAP_FILE, read_only=True),
        *bind_mount_args(exports_dir, CONTAINER_EXPORTS_DIR),
        *bind_mount_args(logs_dir, CONTAINER_LOGS_DIR),
        args.image,
        str(args.days),
        "--map-file",
        str(CONTAINER_SYNC_MAP_FILE),
    ]
    if args.dry_run:
        command.append("--dry-run")
    command.extend(passthrough_args(args.sync_args))
    return run_command(command)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run the RedBark-Sure-Sync Docker image without manually building bind mounts."
        )
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    map_parser = subparsers.add_parser(
        "map",
        help="Generate an account map on the host with an interactive container run.",
    )
    map_parser.add_argument(
        "--envfile",
        "--env-file",
        required=True,
        dest="envfile",
        help="Host path to the .env file passed to docker --env-file.",
    )
    map_parser.add_argument(
        "--mapfile",
        "--map-file",
        dest="mapfile",
        help="Host path where the generated account map JSON should be saved. Prompts when omitted.",
    )
    map_parser.add_argument(
        "--days",
        type=positive_days,
        default=DEFAULT_MAP_DAYS,
        help=f"Bootstrap lookback window in days. Default: {DEFAULT_MAP_DAYS}.",
    )
    map_parser.add_argument(
        "--image",
        default=DEFAULT_IMAGE,
        help=f"Docker image to run. Default: {DEFAULT_IMAGE}",
    )
    map_parser.add_argument(
        "map_args",
        nargs=argparse.REMAINDER,
        help="Additional arguments passed through to the container map mode after --.",
    )
    map_parser.set_defaults(handler=run_map)

    sync_parser = subparsers.add_parser(
        "sync",
        help="Run the scheduled-safe sync/orchestrator in Docker.",
    )
    sync_parser.add_argument(
        "--envfile",
        "--env-file",
        required=True,
        dest="envfile",
        help="Host path to the .env file passed to docker --env-file.",
    )
    sync_parser.add_argument(
        "--mapfile",
        "--map-file",
        required=True,
        dest="mapfile",
        help="Host path to the account map JSON used for the sync run.",
    )
    sync_parser.add_argument(
        "--days",
        type=positive_days,
        default=DEFAULT_SYNC_DAYS,
        help=f"Lookback window in days for each sync run. Default: {DEFAULT_SYNC_DAYS}.",
    )
    sync_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run the export normally, then run the sync step in dry-run mode.",
    )
    sync_parser.add_argument(
        "--image",
        default=DEFAULT_IMAGE,
        help=f"Docker image to run. Default: {DEFAULT_IMAGE}",
    )
    sync_parser.add_argument(
        "sync_args",
        nargs=argparse.REMAINDER,
        help="Additional arguments passed through to the container orchestrator after --.",
    )
    sync_parser.set_defaults(handler=run_sync)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        return args.handler(args)
    except RuntimeError as exc:
        print(exc, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())