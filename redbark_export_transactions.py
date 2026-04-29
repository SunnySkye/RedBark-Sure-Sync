#!/usr/bin/env python3
"""Export Redbark transactions into one JSON file per account.

Built against the Redbark REST API docs:
- https://docs.redbark.co/api-reference/overview
- https://docs.redbark.co/api-reference/connections
- https://docs.redbark.co/api-reference/accounts
- https://docs.redbark.co/api-reference/transactions
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


BASE_URL = "https://api.redbark.co"
DEFAULT_OUTPUT_DIR = Path("exports")
DEFAULT_LOG_FILE = Path("logs") / "redbark_export_transactions.log"
DEFAULT_TIMEOUT_SECONDS = 30
ACCOUNTS_PAGE_SIZE = 200
TRANSACTIONS_PAGE_SIZE = 500
MAX_RETRIES = 5
ENV_FILE = Path(__file__).resolve().with_name(".env")
LOGGER = logging.getLogger("redbark_export_transactions")


class RedbarkApiError(RuntimeError):
    """Raised when the Redbark API returns an error or invalid payload."""


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


def load_env_file(env_path: Path) -> None:
    if not env_path.is_file():
        LOGGER.debug("No .env file found at %s", env_path)
        return

    loaded_keys = 0
    for line_number, raw_line in enumerate(env_path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue

        if line.startswith("export "):
            line = line[7:].lstrip()

        key, separator, value = line.partition("=")
        if separator != "=":
            raise RedbarkApiError(f"Invalid .env entry on line {line_number}: {raw_line}")

        key = key.strip()
        value = value.strip()
        if not key:
            raise RedbarkApiError(f"Invalid .env entry on line {line_number}: {raw_line}")

        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]

        os.environ.setdefault(key, value)
        loaded_keys += 1

    LOGGER.debug("Loaded %d environment variable(s) from %s", loaded_keys, env_path)


def positive_days(value: str) -> int:
    try:
        days = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer number of days") from exc

    if days < 1:
        raise argparse.ArgumentTypeError("must be at least 1 day")
    return days


def resolve_timeframe(days: int) -> tuple[str, str]:
    now = datetime.now(timezone.utc).replace(microsecond=0)
    from_datetime = now - timedelta(days=days)
    return (
        from_datetime.isoformat().replace("+00:00", "Z"),
        now.isoformat().replace("+00:00", "Z"),
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Fetch Redbark transactions for the last N days and write one JSON file per account."
        )
    )
    parser.add_argument(
        "--api-key",
        help="Redbark API key. If omitted, the script uses the REDBARK_API_KEY environment variable.",
    )
    parser.add_argument(
        "days",
        nargs="?",
        default=1,
        type=positive_days,
        help="Number of days back from now to fetch. Example: 1 for the last day, 2 for the last two days.",
    )
    parser.add_argument(
        "--output-dir",
        default=str(DEFAULT_OUTPUT_DIR),
        help=(
            "Directory managed by this script. Existing JSON files in the directory are removed "
            "after a successful fetch and before new files are written."
        ),
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=DEFAULT_TIMEOUT_SECONDS,
        help="HTTP timeout in seconds for each request. Default: 30.",
    )
    return parser.parse_args()


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug or "unknown"


def output_filename(connection: dict[str, Any], account: dict[str, Any]) -> str:
    institution_name = connection.get("institutionName") or "institution"
    account_name = account.get("name") or account.get("id") or "account"
    account_id = str(account.get("id") or "unknown")
    return f"{slugify(institution_name)}__{slugify(account_name)}__{account_id}.json"


def parse_error_message(body_text: str) -> str:
    try:
        payload = json.loads(body_text)
    except json.JSONDecodeError:
        return body_text.strip() or "Unknown error"

    error = payload.get("error")
    if not isinstance(error, dict):
        return body_text.strip() or "Unknown error"

    message = error.get("message") or "Unknown error"
    details = error.get("details")
    if isinstance(details, list) and details:
        return f"{message} ({'; '.join(str(item) for item in details)})"
    return str(message)


def parse_json_response(raw_bytes: bytes, url: str) -> dict[str, Any]:
    try:
        return json.loads(raw_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RedbarkApiError(f"Invalid JSON returned by {url}") from exc


def request_json(
    api_key: str,
    path: str,
    *,
    params: dict[str, Any] | None = None,
    timeout: int,
) -> dict[str, Any]:
    url = f"{BASE_URL}{path}"
    if params:
        query_string = urlencode({key: value for key, value in params.items() if value is not None})
        url = f"{url}?{query_string}"

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Accept": "application/json",
        "User-Agent": "redbark-account-export/1.0",
    }

    for attempt in range(1, MAX_RETRIES + 1):
        request = Request(url, headers=headers, method="GET")
        LOGGER.debug("GET %s (attempt %d/%d)", url, attempt, MAX_RETRIES)

        try:
            with urlopen(request, timeout=timeout) as response:
                LOGGER.debug("GET %s -> HTTP %s", url, response.status)
                return parse_json_response(response.read(), url)
        except HTTPError as exc:
            body_text = exc.read().decode("utf-8", errors="replace")
            message = parse_error_message(body_text)
            should_retry = exc.code in {429, 503} and attempt < MAX_RETRIES

            if should_retry:
                retry_after = exc.headers.get("Retry-After")
                if retry_after and retry_after.isdigit():
                    wait_seconds = int(retry_after)
                else:
                    wait_seconds = min(2 ** (attempt - 1), 30)
                LOGGER.warning(
                    "Request to %s returned HTTP %s. Retrying in %ss.",
                    url,
                    exc.code,
                    wait_seconds,
                )
                time.sleep(wait_seconds)
                continue

            raise RedbarkApiError(f"Request to {url} failed with HTTP {exc.code}: {message}") from exc
        except URLError as exc:
            if attempt < MAX_RETRIES:
                wait_seconds = min(2 ** (attempt - 1), 30)
                LOGGER.warning(
                    "Network error while calling %s: %s. Retrying in %ss.",
                    url,
                    exc.reason,
                    wait_seconds,
                )
                time.sleep(wait_seconds)
                continue
            raise RedbarkApiError(f"Network error while calling {url}: {exc.reason}") from exc

    raise RedbarkApiError(f"Request to {url} exceeded retry limit")


def fetch_connections(api_key: str, *, timeout: int) -> list[dict[str, Any]]:
    payload = request_json(api_key, "/v1/connections", timeout=timeout)
    data = payload.get("data")
    if not isinstance(data, list):
        raise RedbarkApiError("Unexpected /v1/connections response: missing data array")
    LOGGER.info("Fetched %d connection(s)", len(data))
    return data


def fetch_accounts(api_key: str, *, timeout: int) -> list[dict[str, Any]]:
    accounts: list[dict[str, Any]] = []
    offset = 0

    while True:
        payload = request_json(
            api_key,
            "/v1/accounts",
            params={"limit": ACCOUNTS_PAGE_SIZE, "offset": offset},
            timeout=timeout,
        )
        page = payload.get("data")
        pagination = payload.get("pagination")

        if not isinstance(page, list) or not isinstance(pagination, dict):
            raise RedbarkApiError("Unexpected /v1/accounts response format")

        accounts.extend(page)
        LOGGER.debug("Fetched %d account(s) at offset %d", len(page), offset)
        if not pagination.get("hasMore"):
            LOGGER.info("Fetched %d total account(s)", len(accounts))
            return accounts

        offset += ACCOUNTS_PAGE_SIZE


def fetch_transactions(
    api_key: str,
    *,
    connection_id: str,
    account_id: str,
    from_date: str,
    to_date: str,
    timeout: int,
) -> list[dict[str, Any]]:
    transactions: list[dict[str, Any]] = []
    offset = 0

    while True:
        payload = request_json(
            api_key,
            "/v1/transactions",
            params={
                "connectionId": connection_id,
                "accountId": account_id,
                "from": from_date,
                "to": to_date,
                "limit": TRANSACTIONS_PAGE_SIZE,
                "offset": offset,
            },
            timeout=timeout,
        )
        page = payload.get("data")
        pagination = payload.get("pagination")

        if not isinstance(page, list) or not isinstance(pagination, dict):
            raise RedbarkApiError("Unexpected /v1/transactions response format")

        transactions.extend(page)
        LOGGER.debug(
            "Fetched %d transaction(s) for account %s at offset %d",
            len(page),
            account_id,
            offset,
        )
        if not pagination.get("hasMore"):
            LOGGER.info(
                "Fetched %d total transaction(s) for account %s",
                len(transactions),
                account_id,
            )
            return transactions

        offset += TRANSACTIONS_PAGE_SIZE


def clean_output_dir(output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    existing_exports = list(output_dir.glob("*.json"))
    for path in existing_exports:
        path.unlink()
    LOGGER.info("Removed %d existing export file(s) from %s", len(existing_exports), output_dir)


def build_exports(
    connections: list[dict[str, Any]],
    accounts: list[dict[str, Any]],
    transactions_by_account: dict[str, list[dict[str, Any]]],
    *,
    from_date: str,
    to_date: str,
) -> list[tuple[str, dict[str, Any]]]:
    exports: list[tuple[str, dict[str, Any]]] = []
    connection_index = {connection["id"]: connection for connection in connections if "id" in connection}

    for account in accounts:
        account_id = account.get("id")
        connection_id = account.get("connectionId")
        if not isinstance(account_id, str) or not isinstance(connection_id, str):
            raise RedbarkApiError("Account payload is missing id or connectionId")

        connection = connection_index.get(connection_id)
        if connection is None:
            raise RedbarkApiError(f"Account {account_id} refers to unknown connection {connection_id}")

        exports.append(
            (
                output_filename(connection, account),
                {
                    "exportedAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                    "timeframe": {"from": from_date, "to": to_date},
                    "connection": connection,
                    "account": account,
                    "transactionCount": len(transactions_by_account[account_id]),
                    "transactions": transactions_by_account[account_id],
                },
            )
        )

    exports.sort(key=lambda item: item[0])
    return exports


def build_account_catalog(
    connections: list[dict[str, Any]],
    accounts: list[dict[str, Any]],
    *,
    from_date: str,
    to_date: str,
) -> dict[str, Any]:
    connection_index = {connection["id"]: connection for connection in connections if "id" in connection}
    account_entries: list[dict[str, Any]] = []

    for account in accounts:
        connection_id = account.get("connectionId")
        if not isinstance(connection_id, str):
            raise RedbarkApiError("Account payload is missing connectionId")

        connection = connection_index.get(connection_id)
        if connection is None:
            raise RedbarkApiError(f"Account refers to unknown connection {connection_id}")

        account_entries.append(
            {
                "connection": connection,
                "account": account,
            }
        )

    return {
        "exportedAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "timeframe": {"from": from_date, "to": to_date},
        "accountCount": len(account_entries),
        "accounts": account_entries,
    }


def write_exports(
    output_dir: Path,
    exports: list[tuple[str, dict[str, Any]]],
    *,
    account_catalog: dict[str, Any],
) -> None:
    LOGGER.info("Writing %d export file(s) to %s", len(exports), output_dir)
    clean_output_dir(output_dir)

    accounts_file_path = output_dir / "accounts.json"
    with accounts_file_path.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(account_catalog, handle, indent=2)
        handle.write("\n")
    LOGGER.debug("Wrote account catalog file %s", accounts_file_path)

    for filename, payload in exports:
        file_path = output_dir / filename
        with file_path.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, indent=2)
            handle.write("\n")
        LOGGER.debug("Wrote export file %s", file_path)


def main() -> int:
    args = parse_args()
    setup_logging(DEFAULT_LOG_FILE)
    LOGGER.info("Verbose logging enabled")
    LOGGER.info("Writing detailed logs to %s", DEFAULT_LOG_FILE.resolve())
    load_env_file(ENV_FILE)
    api_key = args.api_key or os.environ.get("REDBARK_API_KEY")
    if not api_key:
        LOGGER.error("Provide --api-key or set REDBARK_API_KEY in the environment or .env.")
        return 1

    output_dir = Path(args.output_dir)
    from_date, to_date = resolve_timeframe(args.days)
    LOGGER.info(
        "Starting Redbark export for the last %d day(s): %s to %s",
        args.days,
        from_date,
        to_date,
    )

    try:
        connections = fetch_connections(api_key, timeout=args.timeout)
        banking_connections = [
            connection
            for connection in connections
            if connection.get("category") == "banking" and connection.get("status") == "active"
        ]
        LOGGER.info("Found %d active banking connection(s)", len(banking_connections))
        banking_connection_ids = {
            connection_id
            for connection_id in (connection.get("id") for connection in banking_connections)
            if isinstance(connection_id, str)
        }

        accounts = fetch_accounts(api_key, timeout=args.timeout)
        accounts_to_export = [
            account
            for account in accounts
            if account.get("connectionId") in banking_connection_ids
        ]
        LOGGER.info("Found %d banking account(s) to export", len(accounts_to_export))
        accounts_to_export.sort(
            key=lambda account: (
                str(account.get("institutionName") or ""),
                str(account.get("name") or ""),
                str(account.get("id") or ""),
            )
        )

        transactions_by_account: dict[str, list[dict[str, Any]]] = {}
        for index, account in enumerate(accounts_to_export, start=1):
            account_id = account.get("id")
            connection_id = account.get("connectionId")
            if not isinstance(account_id, str) or not isinstance(connection_id, str):
                raise RedbarkApiError("Account payload is missing id or connectionId")

            LOGGER.info(
                "Fetching transactions for account %d/%d: %s (%s)",
                index,
                len(accounts_to_export),
                account.get("name") or account_id,
                account_id,
            )

            transactions_by_account[account_id] = fetch_transactions(
                api_key,
                connection_id=connection_id,
                account_id=account_id,
                from_date=from_date,
                to_date=to_date,
                timeout=args.timeout,
            )

        exports = build_exports(
            banking_connections,
            accounts_to_export,
            transactions_by_account,
            from_date=from_date,
            to_date=to_date,
        )
        account_catalog = build_account_catalog(
            banking_connections,
            accounts_to_export,
            from_date=from_date,
            to_date=to_date,
        )
        write_exports(output_dir, exports, account_catalog=account_catalog)
    except RedbarkApiError as exc:
        LOGGER.error(str(exc))
        return 1

    total_transactions = sum(payload["transactionCount"] for _, payload in exports)
    LOGGER.info(
        "Wrote %d account file(s) with %d transaction(s) to %s",
        len(exports),
        total_transactions,
        output_dir,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
