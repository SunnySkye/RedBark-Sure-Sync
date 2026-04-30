#!/usr/bin/env python3
"""Interactively create a JSON map between Sure and RedBark accounts."""

from __future__ import annotations

import argparse
import base64
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DEFAULT_REDBARK_ACCOUNTS_FILE = Path("exports") / "accounts.json"
DEFAULT_SURE_ACCOUNTS_FILE = Path("sure-transactions") / "accounts.json"
DEFAULT_OUTPUT_FILE = Path("account_map.json")
ACCOUNT_MAP_BASE64_ENV_VAR = "REDBARK_SURE_ACCOUNT_MAP_BASE64"
DEFAULT_BOOTSTRAP_DAYS = 1


class AccountMapError(RuntimeError):
    """Raised when the account catalogs are missing or invalid."""


class AccountMapAborted(RuntimeError):
    """Raised when the user aborts the interactive mapping session."""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Interactively map Sure accounts to RedBark accounts and write a JSON map file."
    )
    parser.add_argument(
        "--redbark-accounts-file",
        default=str(DEFAULT_REDBARK_ACCOUNTS_FILE),
        help="Path to the RedBark account catalog JSON. Default: exports/accounts.json",
    )
    parser.add_argument(
        "--sure-accounts-file",
        default=str(DEFAULT_SURE_ACCOUNTS_FILE),
        help="Path to the Sure account catalog JSON. Default: sure-transactions/accounts.json",
    )
    parser.add_argument(
        "--output-file",
        default=str(DEFAULT_OUTPUT_FILE),
        help="Path to the generated account map JSON. Default: account_map.json",
    )
    return parser.parse_args()


def load_json_file(path: Path) -> Any:
    if not path.is_file():
        raise AccountMapError(f"File not found: {path}")

    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise AccountMapError(f"Invalid JSON in file: {path}") from exc


def load_redbark_accounts(path: Path) -> list[dict[str, Any]]:
    payload = load_json_file(path)
    if not isinstance(payload, dict):
        raise AccountMapError(f"Unexpected RedBark catalog format in {path}")

    accounts = payload.get("accounts")
    if not isinstance(accounts, list):
        raise AccountMapError(f"RedBark catalog is missing an accounts array in {path}")

    normalized: list[dict[str, Any]] = []
    for entry in accounts:
        if not isinstance(entry, dict):
            raise AccountMapError(f"RedBark catalog contains a non-object account entry in {path}")

        connection = entry.get("connection")
        account = entry.get("account")
        if not isinstance(connection, dict) or not isinstance(account, dict):
            raise AccountMapError(f"RedBark catalog entry is missing connection/account objects in {path}")

        normalized.append({"connection": connection, "account": account})

    return normalized


def load_sure_accounts(path: Path) -> list[dict[str, Any]]:
    payload = load_json_file(path)
    if not isinstance(payload, dict):
        raise AccountMapError(f"Unexpected Sure catalog format in {path}")

    accounts = payload.get("accounts")
    if not isinstance(accounts, list):
        raise AccountMapError(f"Sure catalog is missing an accounts array in {path}")

    for entry in accounts:
        if not isinstance(entry, dict):
            raise AccountMapError(f"Sure catalog contains a non-object account entry in {path}")

    return accounts


def sure_account_label(account: dict[str, Any]) -> str:
    name = str(account.get("name") or "Unknown Sure Account")
    balance = str(account.get("balance") or "Unknown balance")
    currency = str(account.get("currency") or "Unknown currency")
    classification = str(account.get("classification") or "unknown classification")
    account_type = str(account.get("account_type") or "unknown type")
    account_id = str(account.get("id") or "unknown id")
    return (
        f"{name} | {balance} | {currency} | {classification} | "
        f"{account_type} | {account_id}"
    )


def redbark_account_label(entry: dict[str, Any]) -> str:
    account = entry["account"]
    connection = entry["connection"]
    name = str(account.get("name") or "Unknown RedBark Account")
    institution = str(connection.get("institutionName") or account.get("institutionName") or "Unknown institution")
    account_number = str(account.get("accountNumber") or "Unknown account number")
    currency = str(account.get("currency") or "Unknown currency")
    account_type = str(account.get("type") or "unknown type")
    account_id = str(account.get("id") or "unknown id")
    return f"{name} | {institution} | {account_number} | {currency} | {account_type} | {account_id}"


def print_header(title: str) -> None:
    print()
    print(title)
    print("-" * len(title))


def prompt_yes_no(prompt: str, *, default: bool) -> bool:
    suffix = "[Y/n]" if default else "[y/N]"

    while True:
        answer = input(f"{prompt} {suffix}: ").strip().lower()
        if not answer:
            return default
        if answer in {"y", "yes"}:
            return True
        if answer in {"n", "no"}:
            return False
        print("Enter 'y' or 'n'.")


def prompt_bootstrap_days(default_days: int = DEFAULT_BOOTSTRAP_DAYS) -> int:
    while True:
        answer = input(
            "How many days of transactions should the bootstrap exports pull? "
            f"[{default_days}]: "
        ).strip()
        if not answer:
            return default_days

        try:
            days = int(answer)
        except ValueError:
            print("Enter a whole number of days greater than or equal to 1.")
            continue

        if days < 1:
            print("Enter a whole number of days greater than or equal to 1.")
            continue

        return days


def run_python_script(script_name: str, script_args: list[str]) -> None:
    project_root = Path(__file__).resolve().parent
    script_path = project_root / script_name
    if not script_path.is_file():
        raise AccountMapError(f"Required helper script not found: {script_path}")

    command = [sys.executable, str(script_path), *script_args]
    print()
    print(f"Running {script_name}...")
    exit_code = subprocess.run(command, cwd=project_root, check=False).returncode
    if exit_code != 0:
        raise AccountMapError(f"{script_name} failed with exit code {exit_code}.")


def generate_catalog(script_name: str, requested_catalog_file: Path, days: int) -> None:
    output_dir = requested_catalog_file.parent
    generated_catalog_file = output_dir / "accounts.json"

    run_python_script(
        script_name,
        [
            str(days),
            "--output-dir",
            str(output_dir),
        ],
    )

    if not generated_catalog_file.is_file():
        raise AccountMapError(
            f"{script_name} completed but did not create {generated_catalog_file}"
        )

    if requested_catalog_file.name != "accounts.json":
        requested_catalog_file.parent.mkdir(parents=True, exist_ok=True)
        requested_catalog_file.write_text(
            generated_catalog_file.read_text(encoding="utf-8"),
            encoding="utf-8",
        )


def ensure_account_catalogs(
    redbark_accounts_file: Path,
    sure_accounts_file: Path,
) -> None:
    missing_catalogs: list[tuple[str, Path, str]] = []

    if not redbark_accounts_file.is_file():
        missing_catalogs.append(
            ("RedBark", redbark_accounts_file, "redbark_export_transactions.py")
        )
    if not sure_accounts_file.is_file():
        missing_catalogs.append(
            ("Sure", sure_accounts_file, "sure_export_transactions.py")
        )

    if not missing_catalogs:
        return

    print_header("Missing Account Catalogs")
    for catalog_name, catalog_path, _ in missing_catalogs:
        print(f"- {catalog_name} account catalog not found: {catalog_path}")

    print()
    print("generate_account_map.py can run the existing export scripts to create the missing accounts.json file(s).")
    print("Those scripts also refresh the per-account transaction export files.")

    if not sys.stdin.isatty():
        raise AccountMapError(
            "Account catalog files are missing and automatic recovery requires an interactive terminal. "
            "Run redbark_export_transactions.py and sure_export_transactions.py first, or rerun generate_account_map.py interactively."
        )

    if not prompt_yes_no("Generate the missing account catalog file(s) now?", default=True):
        missing_paths = ", ".join(str(catalog_path) for _, catalog_path, _ in missing_catalogs)
        raise AccountMapError(
            f"Required account catalog file(s) are missing: {missing_paths}"
        )

    days = prompt_bootstrap_days()

    for _, catalog_path, script_name in missing_catalogs:
        generate_catalog(script_name, catalog_path, days)

    print()
    print("Missing account catalog generation complete.")


def choose_mappings(
    sure_accounts: list[dict[str, Any]],
    redbark_accounts: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    mappings: list[dict[str, Any]] = []
    unmapped_sure_accounts: list[dict[str, Any]] = []
    remaining_redbark_accounts = list(redbark_accounts)

    print_header("Interactive Account Mapping")
    print("This script will not auto-match accounts.")
    print("For each Sure account, choose a RedBark account number, 's' to skip, or 'q' to quit without saving.")

    for index, sure_account in enumerate(sure_accounts, start=1):
        print_header(f"Sure Account {index} of {len(sure_accounts)}")
        print(sure_account_label(sure_account))

        if not remaining_redbark_accounts:
            print("No RedBark accounts remain. This Sure account will be left unmapped.")
            unmapped_sure_accounts.append(sure_account)
            continue

        while True:
            print()
            print("Available RedBark accounts:")
            for option_index, redbark_entry in enumerate(remaining_redbark_accounts, start=1):
                print(f"  {option_index}. {redbark_account_label(redbark_entry)}")

            choice = input("Select RedBark account number, 's' to skip, or 'q' to quit: ").strip().lower()

            if choice == "q":
                raise AccountMapAborted("Aborted without writing map file.")

            if choice == "s":
                unmapped_sure_accounts.append(sure_account)
                break

            if choice.isdigit():
                selected_index = int(choice)
                if 1 <= selected_index <= len(remaining_redbark_accounts):
                    selected_entry = remaining_redbark_accounts.pop(selected_index - 1)
                    mappings.append(
                        {
                            "sureAccount": sure_account,
                            "redbarkConnection": selected_entry["connection"],
                            "redbarkAccount": selected_entry["account"],
                        }
                    )
                    break

            print("Invalid selection. Enter a listed number, 's', or 'q'.")

    return mappings, unmapped_sure_accounts, remaining_redbark_accounts


def print_summary(
    mappings: list[dict[str, Any]],
    unmapped_sure_accounts: list[dict[str, Any]],
    unmapped_redbark_accounts: list[dict[str, Any]],
) -> None:
    print_header("Mapping Summary")

    if mappings:
        print("Mapped accounts:")
        for entry in mappings:
            print(
                f"  Sure: {sure_account_label(entry['sureAccount'])}\n"
                f"     -> RedBark: {redbark_account_label({'connection': entry['redbarkConnection'], 'account': entry['redbarkAccount']})}"
            )
    else:
        print("No account mappings were selected.")

    print()
    print(f"Unmapped Sure accounts: {len(unmapped_sure_accounts)}")
    for account in unmapped_sure_accounts:
        print(f"  - {sure_account_label(account)}")

    print()
    print(f"Unmapped RedBark accounts: {len(unmapped_redbark_accounts)}")
    for entry in unmapped_redbark_accounts:
        print(f"  - {redbark_account_label(entry)}")


def build_map_file(
    *,
    redbark_accounts_file: Path,
    sure_accounts_file: Path,
    mappings: list[dict[str, Any]],
    unmapped_sure_accounts: list[dict[str, Any]],
    unmapped_redbark_accounts: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "mapFileType": "sure-redbark-account-map",
        "generatedAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "generatedInteractively": True,
        "redbarkAccountsFile": str(redbark_accounts_file),
        "sureAccountsFile": str(sure_accounts_file),
        "mappingCount": len(mappings),
        "mappings": mappings,
        "unmappedSureAccounts": unmapped_sure_accounts,
        "unmappedRedbarkAccounts": [
            {
                "connection": entry["connection"],
                "account": entry["account"],
            }
            for entry in unmapped_redbark_accounts
        ],
    }


def confirm_write(output_file: Path) -> bool:
    if output_file.exists():
        answer = input(f"{output_file} already exists and will be overwritten. Continue? [y/N]: ").strip().lower()
        return answer in {"y", "yes"}

    answer = input(f"Write account map to {output_file}? [y/N]: ").strip().lower()
    return answer in {"y", "yes"}


def write_map_file(output_file: Path, payload: dict[str, Any]) -> None:
    output_file.parent.mkdir(parents=True, exist_ok=True)
    output_file.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def encode_map_payload(payload: dict[str, Any]) -> str:
    compact_json = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return base64.b64encode(compact_json).decode("ascii")


def main() -> int:
    args = parse_args()
    redbark_accounts_file = Path(args.redbark_accounts_file)
    sure_accounts_file = Path(args.sure_accounts_file)
    output_file = Path(args.output_file)

    try:
        ensure_account_catalogs(redbark_accounts_file, sure_accounts_file)
        redbark_accounts = load_redbark_accounts(redbark_accounts_file)
        sure_accounts = load_sure_accounts(sure_accounts_file)
        mappings, unmapped_sure_accounts, unmapped_redbark_accounts = choose_mappings(
            sure_accounts,
            redbark_accounts,
        )
        print_summary(mappings, unmapped_sure_accounts, unmapped_redbark_accounts)

        if not confirm_write(output_file):
            print("Map file was not written.")
            return 1

        payload = build_map_file(
            redbark_accounts_file=redbark_accounts_file,
            sure_accounts_file=sure_accounts_file,
            mappings=mappings,
            unmapped_sure_accounts=unmapped_sure_accounts,
            unmapped_redbark_accounts=unmapped_redbark_accounts,
        )
        write_map_file(output_file, payload)
        encoded_payload = encode_map_payload(payload)
    except AccountMapAborted as exc:
        print(str(exc))
        return 1
    except AccountMapError as exc:
        print(str(exc))
        return 1

    print(f"Account map written to {output_file}")
    print()
    print("To pass the account map through Docker or a .env file instead of mounting account_map.json, add this line:")
    print(f"{ACCOUNT_MAP_BASE64_ENV_VAR}={encoded_payload}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())