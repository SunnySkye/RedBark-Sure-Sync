#!/usr/bin/env python3
"""Audit Sure Finance for duplicate RedBark sync markers."""

from __future__ import annotations

import argparse
import json
import logging
import os
from datetime import date
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from sync_redbark_to_sure import (
    ACCOUNT_MAP_BASE64_ENV_VAR,
    ENV_FILE,
    SyncError,
    extract_sync_token,
    fetch_sure_transactions,
    load_env_file,
    load_map_file,
)


DEFAULT_MAP_FILE = Path("account_map.json")
DEFAULT_LOG_FILE = Path("logs") / "audit_redbark_to_sure_duplicates.log"
DEFAULT_TIMEOUT_SECONDS = 30
DEFAULT_AUDIT_START_DATE = "2000-01-01"
DISCORD_WEBHOOK_ENV_VAR = "DUPLICATE_AUDIT_WEBHOOK_URL"
DISCORD_MESSAGE_LIMIT = 1900
LOGGER = logging.getLogger("audit_redbark_to_sure_duplicates")


class DuplicateAuditError(RuntimeError):
    """Raised when the duplicate audit cannot complete."""


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


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Audit Sure Finance for duplicate RedBark sync markers."
    )
    parser.add_argument(
        "--mapfile",
        "--map-file",
        dest="map_file",
        default=str(DEFAULT_MAP_FILE),
        help=(
            "Path to the interactive account map JSON. Default: account_map.json. "
            f"If the file is missing, falls back to {ACCOUNT_MAP_BASE64_ENV_VAR} from the environment or .env."
        ),
    )
    parser.add_argument(
        "--sure-base-url",
        help="Sure base URL. If omitted, the script uses SURE_BASE_URL from the environment or .env.",
    )
    parser.add_argument(
        "--sure-api-key",
        help="Sure API key. If omitted, the script uses SURE_API_KEY from the environment or .env.",
    )
    parser.add_argument(
        "--duplicate-webhook-url",
        help=(
            "Optional Discord webhook URL override. "
            f"If omitted, the script uses {DISCORD_WEBHOOK_ENV_VAR} from the environment or .env."
        ),
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=DEFAULT_TIMEOUT_SECONDS,
        help="HTTP timeout in seconds for Sure and Discord requests. Default: 30.",
    )
    return parser.parse_args()


def require_env_value(value: str | None, message: str) -> str:
    if value:
        return value
    raise DuplicateAuditError(message)


def truncate_for_discord(message: str) -> str:
    if len(message) <= DISCORD_MESSAGE_LIMIT:
        return message
    return message[: DISCORD_MESSAGE_LIMIT - 3] + "..."


def send_discord_webhook(webhook_url: str, message: str, *, timeout: int) -> None:
    payload = {"content": truncate_for_discord(message)}
    request = Request(
        webhook_url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "redbark-duplicate-audit/1.0",
        },
        method="POST",
    )

    try:
        with urlopen(request, timeout=timeout) as response:
            if response.status not in {200, 204}:
                raise DuplicateAuditError(
                    f"Discord webhook returned unexpected HTTP {response.status}"
                )
    except HTTPError as exc:
        body_text = exc.read().decode("utf-8", errors="replace").strip()
        raise DuplicateAuditError(
            f"Discord webhook notification failed with HTTP {exc.code}: {body_text or 'Unknown error'}"
        ) from exc
    except URLError as exc:
        raise DuplicateAuditError(
            f"Discord webhook notification failed: {exc.reason}"
        ) from exc


def maybe_notify_duplicates(webhook_url: str | None, message: str, *, timeout: int) -> None:
    if not webhook_url:
        LOGGER.error(
            "Duplicate audit found repeated RedBark markers in Sure, but %s is not configured.",
            DISCORD_WEBHOOK_ENV_VAR,
        )
        return

    try:
        send_discord_webhook(webhook_url, message, timeout=timeout)
    except DuplicateAuditError as exc:
        LOGGER.error(str(exc))
        return

    LOGGER.info("Sent duplicate audit notification to Discord webhook")


def build_duplicate_notification(
    duplicate_accounts: list[dict[str, object]],
    *,
    checked_accounts: int,
    checked_marker_transactions: int,
    audit_end_date: str,
) -> str:
    total_duplicate_markers = sum(int(account["duplicate_marker_count"]) for account in duplicate_accounts)
    lines = [
        "Duplicate RedBark sync markers detected in Sure.",
        f"Audit window: {DEFAULT_AUDIT_START_DATE} to {audit_end_date}",
        f"Mapped accounts checked: {checked_accounts}",
        f"Marker-backed transactions checked: {checked_marker_transactions}",
        f"Duplicate markers found: {total_duplicate_markers}",
    ]

    for account in duplicate_accounts[:5]:
        lines.append(
            f"{account['account_name']} ({account['account_id']}): {account['duplicate_marker_count']} duplicate marker(s)"
        )
        duplicate_examples = account["duplicate_examples"]
        if not isinstance(duplicate_examples, list):
            continue

        for example in duplicate_examples[:3]:
            token = str(example.get("token") or "unknown-token")
            sure_ids = ", ".join(str(item) for item in example.get("sure_ids", [])[:4])
            lines.append(f"{token}: {sure_ids}")

    if len(duplicate_accounts) > 5:
        lines.append(f"Additional affected accounts: {len(duplicate_accounts) - 5}")

    return "\n".join(lines)


def run_duplicate_audit(
    *,
    map_file: Path,
    sure_base_url: str,
    sure_api_key: str,
    duplicate_webhook_url: str | None,
    timeout: int,
) -> bool:
    audit_end_date = date.today().isoformat()

    try:
        _, mappings = load_map_file(map_file)
    except SyncError as exc:
        raise DuplicateAuditError(str(exc)) from exc

    duplicate_accounts: list[dict[str, object]] = []
    checked_marker_transactions = 0

    for mapping in mappings:
        sure_account_id = mapping.sure_account.get("id")
        sure_account_name = str(mapping.sure_account.get("name") or sure_account_id)
        if not isinstance(sure_account_id, str):
            raise DuplicateAuditError("Mapped Sure account is missing id")

        LOGGER.info(
            "Running duplicate audit for Sure account %s (%s)",
            sure_account_name,
            sure_account_id,
        )

        try:
            transactions = fetch_sure_transactions(
                sure_base_url,
                sure_api_key,
                sure_account_id=sure_account_id,
                start_date=DEFAULT_AUDIT_START_DATE,
                end_date=audit_end_date,
                timeout=timeout,
            )
        except SyncError as exc:
            raise DuplicateAuditError(
                f"Duplicate audit failed while reading Sure account {sure_account_name}: {exc}"
            ) from exc

        token_index: dict[str, list[dict[str, object]]] = {}
        for transaction in transactions:
            token = extract_sync_token(transaction.get("notes"))
            if token is None:
                continue

            checked_marker_transactions += 1
            token_index.setdefault(token, []).append(
                {
                    "sure_id": transaction.get("id"),
                    "date": transaction.get("date"),
                    "name": transaction.get("name"),
                    "signed_amount_cents": transaction.get("signed_amount_cents"),
                }
            )

        duplicate_examples = []
        for token, items in token_index.items():
            if len(items) <= 1:
                continue

            duplicate_examples.append(
                {
                    "token": token,
                    "count": len(items),
                    "sure_ids": [item.get("sure_id") for item in items],
                }
            )

        if duplicate_examples:
            duplicate_accounts.append(
                {
                    "account_id": sure_account_id,
                    "account_name": sure_account_name,
                    "duplicate_marker_count": len(duplicate_examples),
                    "duplicate_examples": duplicate_examples,
                }
            )

    if not duplicate_accounts:
        LOGGER.info(
            "Duplicate audit passed. Checked %d marker-backed Sure transaction(s) across %d mapped account(s).",
            checked_marker_transactions,
            len(mappings),
        )
        return False

    duplicate_message = build_duplicate_notification(
        duplicate_accounts,
        checked_accounts=len(mappings),
        checked_marker_transactions=checked_marker_transactions,
        audit_end_date=audit_end_date,
    )
    LOGGER.error(duplicate_message)
    maybe_notify_duplicates(duplicate_webhook_url, duplicate_message, timeout=timeout)
    return True


def main() -> int:
    args = parse_args()
    setup_logging(DEFAULT_LOG_FILE)
    LOGGER.info("Verbose logging enabled")
    LOGGER.info("Writing detailed logs to %s", DEFAULT_LOG_FILE.resolve())

    load_env_file(ENV_FILE)

    try:
        map_file = Path(args.map_file)
        sure_base_url = require_env_value(
            args.sure_base_url or os.environ.get("SURE_BASE_URL"),
            "Provide --sure-base-url or set SURE_BASE_URL in the environment or .env.",
        )
        sure_api_key = require_env_value(
            args.sure_api_key or os.environ.get("SURE_API_KEY"),
            "Provide --sure-api-key or set SURE_API_KEY in the environment or .env.",
        )
        duplicate_webhook_url = args.duplicate_webhook_url or os.environ.get(DISCORD_WEBHOOK_ENV_VAR)

        duplicates_found = run_duplicate_audit(
            map_file=map_file,
            sure_base_url=sure_base_url,
            sure_api_key=sure_api_key,
            duplicate_webhook_url=duplicate_webhook_url,
            timeout=args.timeout,
        )
    except DuplicateAuditError as exc:
        LOGGER.error(str(exc))
        return 1

    if duplicates_found:
        LOGGER.error("Duplicate audit failed; repeated RedBark markers were found in Sure")
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())