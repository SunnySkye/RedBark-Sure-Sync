#!/usr/bin/env python3
"""Export Sure Finance transactions into one JSON file per account."""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


DEFAULT_OUTPUT_DIR = Path("sure-transactions")
DEFAULT_LOG_FILE = Path("logs") / "sure_export_transactions.log"
DEFAULT_TIMEOUT_SECONDS = 30
PAGE_SIZE = 100
MAX_RETRIES = 5
ENV_FILE = Path(__file__).resolve().with_name(".env")
LOGGER = logging.getLogger("sure_export_transactions")


class SureApiError(RuntimeError):
    """Raised when the Sure API returns an error or invalid payload."""


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
            raise SureApiError(f"Invalid .env entry on line {line_number}: {raw_line}")

        key = key.strip()
        value = value.strip()
        if not key:
            raise SureApiError(f"Invalid .env entry on line {line_number}: {raw_line}")

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


def resolve_date_range(days: int) -> tuple[str, str]:
    end_date = date.today()
    start_date = end_date - timedelta(days=days - 1)
    return start_date.isoformat(), end_date.isoformat()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Fetch Sure Finance transactions for the last N days and write one JSON file per account."
        )
    )
    parser.add_argument(
        "--base-url",
        help="Sure base URL. If omitted, the script uses the SURE_BASE_URL environment variable.",
    )
    parser.add_argument(
        "--api-key",
        help="Sure API key. If omitted, the script uses the SURE_API_KEY environment variable.",
    )
    parser.add_argument(
        "days",
        nargs="?",
        default=1,
        type=positive_days,
        help="Number of calendar days to fetch, inclusive of today. Example: 1 for today, 7 for the last seven days.",
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


def output_filename(account: dict[str, Any]) -> str:
    account_name = account.get("name") or account.get("id") or "account"
    account_id = str(account.get("id") or "unknown")
    return f"{slugify(account_name)}__{account_id}.json"


def parse_error_message(body_text: str) -> str:
    try:
        payload = json.loads(body_text)
    except json.JSONDecodeError:
        return body_text.strip() or "Unknown error"

    if isinstance(payload, dict):
        message = payload.get("message") or payload.get("error") or "Unknown error"
        details = payload.get("errors")
        if isinstance(details, list) and details:
            return f"{message} ({'; '.join(str(item) for item in details)})"
        return str(message)

    return body_text.strip() or "Unknown error"


def parse_json_response(raw_bytes: bytes, url: str) -> Any:
    try:
        return json.loads(raw_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SureApiError(f"Invalid JSON returned by {url}") from exc


def request_json(
    base_url: str,
    api_key: str,
    path: str,
    *,
    params: dict[str, Any] | None = None,
    timeout: int,
) -> Any:
    normalized_base_url = base_url.rstrip("/")
    url = f"{normalized_base_url}{path}"
    if params:
        query_string = urlencode({key: value for key, value in params.items() if value is not None}, doseq=True)
        url = f"{url}?{query_string}"

    headers = {
        "X-Api-Key": api_key,
        "Accept": "application/json",
        "User-Agent": "sure-account-export/1.0",
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
            should_retry = exc.code in {429, 500, 502, 503, 504} and attempt < MAX_RETRIES

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

            raise SureApiError(f"Request to {url} failed with HTTP {exc.code}: {message}") from exc
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
            raise SureApiError(f"Network error while calling {url}: {exc.reason}") from exc

    raise SureApiError(f"Request to {url} exceeded retry limit")


def parse_paginated_collection(payload: Any, collection_key: str) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    if isinstance(payload, list):
        return payload, None

    if not isinstance(payload, dict):
        raise SureApiError(f"Unexpected response format for {collection_key}")

    collection = payload.get(collection_key)
    if not isinstance(collection, list):
        raise SureApiError(f"Unexpected response format for {collection_key}: missing {collection_key} array")

    pagination = payload.get("pagination")
    if pagination is not None and not isinstance(pagination, dict):
        raise SureApiError(f"Unexpected pagination format for {collection_key}")

    return collection, pagination


def fetch_accounts(base_url: str, api_key: str, *, timeout: int) -> list[dict[str, Any]]:
    accounts: list[dict[str, Any]] = []
    page = 1

    while True:
        payload = request_json(
            base_url,
            api_key,
            "/api/v1/accounts",
            params={"page": page, "per_page": PAGE_SIZE},
            timeout=timeout,
        )
        page_accounts, pagination = parse_paginated_collection(payload, "accounts")
        accounts.extend(page_accounts)
        LOGGER.debug("Fetched %d account(s) on page %d", len(page_accounts), page)

        if pagination is None:
            LOGGER.info("Fetched %d total account(s) without pagination metadata", len(accounts))
            return accounts

        total_pages = int(pagination.get("total_pages") or page)
        if page >= total_pages:
            LOGGER.info("Fetched %d total account(s) across %d page(s)", len(accounts), total_pages)
            return accounts

        page += 1


def fetch_transactions_for_account(
    base_url: str,
    api_key: str,
    *,
    account_id: str,
    start_date: str,
    end_date: str,
    timeout: int,
) -> list[dict[str, Any]]:
    transactions: list[dict[str, Any]] = []
    page = 1

    while True:
        payload = request_json(
            base_url,
            api_key,
            "/api/v1/transactions",
            params={
                "account_id": account_id,
                "start_date": start_date,
                "end_date": end_date,
                "page": page,
                "per_page": PAGE_SIZE,
            },
            timeout=timeout,
        )
        page_transactions, pagination = parse_paginated_collection(payload, "transactions")
        transactions.extend(page_transactions)
        LOGGER.debug(
            "Fetched %d transaction(s) for account %s on page %d",
            len(page_transactions),
            account_id,
            page,
        )

        if pagination is None:
            LOGGER.info(
                "Fetched %d total transaction(s) for account %s without pagination metadata",
                len(transactions),
                account_id,
            )
            return transactions

        total_pages = int(pagination.get("total_pages") or page)
        if page >= total_pages:
            LOGGER.info(
                "Fetched %d total transaction(s) for account %s across %d page(s)",
                len(transactions),
                account_id,
                total_pages,
            )
            return transactions

        page += 1


def clean_output_dir(output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    existing_exports = list(output_dir.glob("*.json"))
    for path in existing_exports:
        path.unlink()
    LOGGER.info("Removed %d existing export file(s) from %s", len(existing_exports), output_dir)


def build_exports(
    accounts: list[dict[str, Any]],
    transactions_by_account: dict[str, list[dict[str, Any]]],
    *,
    base_url: str,
    days: int,
    start_date: str,
    end_date: str,
) -> list[tuple[str, dict[str, Any]]]:
    exports: list[tuple[str, dict[str, Any]]] = []

    for account in accounts:
        account_id = account.get("id")
        if not isinstance(account_id, str):
            raise SureApiError("Account payload is missing id")

        exports.append(
            (
                output_filename(account),
                {
                    "exportedAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                    "source": {
                        "service": "Sure Finance",
                        "baseUrl": base_url.rstrip("/"),
                    },
                    "timeframe": {
                        "days": days,
                        "startDate": start_date,
                        "endDate": end_date,
                    },
                    "account": account,
                    "transactionCount": len(transactions_by_account[account_id]),
                    "transactions": transactions_by_account[account_id],
                },
            )
        )

    exports.sort(key=lambda item: item[0])
    return exports


def build_account_catalog(
    accounts: list[dict[str, Any]],
    *,
    base_url: str,
    days: int,
    start_date: str,
    end_date: str,
) -> dict[str, Any]:
    return {
        "exportedAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "source": {
            "service": "Sure Finance",
            "baseUrl": base_url.rstrip("/"),
        },
        "timeframe": {
            "days": days,
            "startDate": start_date,
            "endDate": end_date,
        },
        "accountCount": len(accounts),
        "accounts": accounts,
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
    base_url = args.base_url or os.environ.get("SURE_BASE_URL")
    api_key = args.api_key or os.environ.get("SURE_API_KEY")

    if not base_url:
        LOGGER.error("Provide --base-url or set SURE_BASE_URL in the environment or .env.")
        return 1

    if not api_key:
        LOGGER.error("Provide --api-key or set SURE_API_KEY in the environment or .env.")
        return 1

    output_dir = Path(args.output_dir)
    start_date, end_date = resolve_date_range(args.days)
    LOGGER.info(
        "Starting Sure export for the last %d day(s): %s to %s",
        args.days,
        start_date,
        end_date,
    )

    try:
        accounts = fetch_accounts(base_url, api_key, timeout=args.timeout)
        LOGGER.info("Found %d account(s) to export", len(accounts))

        transactions_by_account: dict[str, list[dict[str, Any]]] = {}
        for index, account in enumerate(accounts, start=1):
            account_id = account.get("id")
            if not isinstance(account_id, str):
                raise SureApiError("Account payload is missing id")

            LOGGER.info(
                "Fetching transactions for account %d/%d: %s (%s)",
                index,
                len(accounts),
                account.get("name") or account_id,
                account_id,
            )
            transactions_by_account[account_id] = fetch_transactions_for_account(
                base_url,
                api_key,
                account_id=account_id,
                start_date=start_date,
                end_date=end_date,
                timeout=args.timeout,
            )

        exports = build_exports(
            accounts,
            transactions_by_account,
            base_url=base_url,
            days=args.days,
            start_date=start_date,
            end_date=end_date,
        )
        account_catalog = build_account_catalog(
            accounts,
            base_url=base_url,
            days=args.days,
            start_date=start_date,
            end_date=end_date,
        )
        write_exports(output_dir, exports, account_catalog=account_catalog)
    except SureApiError as exc:
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