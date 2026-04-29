#!/usr/bin/env python3
"""Interactively create a JSON map between Sure and RedBark accounts."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DEFAULT_REDBARK_ACCOUNTS_FILE = Path("exports") / "accounts.json"
DEFAULT_SURE_ACCOUNTS_FILE = Path("sure_exports") / "accounts.json"
DEFAULT_OUTPUT_FILE = Path("account_map.json")


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
        help="Path to the Sure account catalog JSON. Default: sure_exports/accounts.json",
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


def main() -> int:
    args = parse_args()
    redbark_accounts_file = Path(args.redbark_accounts_file)
    sure_accounts_file = Path(args.sure_accounts_file)
    output_file = Path(args.output_file)

    try:
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
    except AccountMapAborted as exc:
        print(str(exc))
        return 1
    except AccountMapError as exc:
        print(str(exc))
        return 1

    print(f"Account map written to {output_file}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())