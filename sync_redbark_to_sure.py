#!/usr/bin/env python3
"""Synchronize RedBark transactions into Sure Finance using mapped accounts."""

from __future__ import annotations

import argparse
from collections import Counter
import json
import logging
import os
import re
import time
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


DEFAULT_MAP_FILE = Path("account_map.json")
DEFAULT_LOG_FILE = Path("logs") / "sync_redbark_to_sure.log"
DEFAULT_TIMEOUT_SECONDS = 30
SURE_PAGE_SIZE = 100
MAX_RETRIES = 5
ENV_FILE = Path(__file__).resolve().with_name(".env")
SYNC_TOKEN_PATTERN = re.compile(r"\[redbark:(?P<id>bank_tx_[^\]]+)\]")
LOGGER = logging.getLogger("sync_redbark_to_sure")


class SyncError(RuntimeError):
    """Raised when the sync cannot continue safely."""


@dataclass(frozen=True)
class MappedAccount:
    sure_account: dict[str, Any]
    redbark_connection: dict[str, Any]
    redbark_account: dict[str, Any]


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
            raise SyncError(f"Invalid .env entry on line {line_number}: {raw_line}")

        key = key.strip()
        value = value.strip()
        if not key:
            raise SyncError(f"Invalid .env entry on line {line_number}: {raw_line}")

        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]

        os.environ.setdefault(key, value)
        loaded_keys += 1

    LOGGER.debug("Loaded %d environment variable(s) from %s", loaded_keys, env_path)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Synchronize RedBark transaction export files into Sure Finance using account_map.json."
        )
    )
    parser.add_argument(
        "--mapfile",
        "--map-file",
        dest="map_file",
        default=str(DEFAULT_MAP_FILE),
        help="Path to the interactive account map JSON. Default: account_map.json",
    )
    parser.add_argument(
        "--redbark-export-dir",
        help=(
            "Directory containing RedBark per-account export JSON files. "
            "Defaults to the directory implied by the map file's redbarkAccountsFile metadata."
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
        "--timeout",
        type=int,
        default=DEFAULT_TIMEOUT_SECONDS,
        help="HTTP timeout in seconds for each request. Default: 30.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Log what would be created without sending POST requests to Sure.",
    )
    return parser.parse_args()


def load_json_file(path: Path) -> Any:
    if not path.is_file():
        raise SyncError(f"File not found: {path}")

    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SyncError(f"Invalid JSON in file: {path}") from exc


def load_map_file(path: Path) -> tuple[dict[str, Any], list[MappedAccount]]:
    payload = load_json_file(path)
    if not isinstance(payload, dict):
        raise SyncError(f"Unexpected map file format in {path}")

    mappings = payload.get("mappings")
    if not isinstance(mappings, list):
        raise SyncError(f"Map file is missing a mappings array in {path}")

    normalized_mappings: list[MappedAccount] = []
    for index, mapping in enumerate(mappings, start=1):
        if not isinstance(mapping, dict):
            raise SyncError(f"Map entry {index} in {path} is not an object")

        sure_account = mapping.get("sureAccount")
        redbark_connection = mapping.get("redbarkConnection")
        redbark_account = mapping.get("redbarkAccount")

        if not isinstance(sure_account, dict) or not isinstance(redbark_connection, dict) or not isinstance(redbark_account, dict):
            raise SyncError(f"Map entry {index} in {path} is missing account metadata")

        normalized_mappings.append(
            MappedAccount(
                sure_account=sure_account,
                redbark_connection=redbark_connection,
                redbark_account=redbark_account,
            )
        )

    if not normalized_mappings:
        raise SyncError(f"Map file contains no mapped accounts: {path}")

    return payload, normalized_mappings


def resolve_redbark_export_dir(map_file_path: Path, map_payload: dict[str, Any], cli_value: str | None) -> Path:
    if cli_value:
        return Path(cli_value)

    redbark_accounts_file = map_payload.get("redbarkAccountsFile")
    if isinstance(redbark_accounts_file, str) and redbark_accounts_file.strip():
        return (map_file_path.parent / Path(redbark_accounts_file)).parent

    return Path("exports")


def load_redbark_export_index(export_dir: Path) -> dict[str, dict[str, Any]]:
    if not export_dir.is_dir():
        raise SyncError(f"RedBark export directory not found: {export_dir}")

    export_index: dict[str, dict[str, Any]] = {}

    for file_path in sorted(export_dir.glob("*.json")):
        if file_path.name == "accounts.json":
            continue

        payload = load_json_file(file_path)
        if not isinstance(payload, dict):
            raise SyncError(f"Unexpected RedBark export format in {file_path}")

        account = payload.get("account")
        transactions = payload.get("transactions")
        if not isinstance(account, dict) or not isinstance(transactions, list):
            raise SyncError(f"RedBark export file missing account/transactions in {file_path}")

        account_id = account.get("id")
        if not isinstance(account_id, str):
            raise SyncError(f"RedBark export account is missing id in {file_path}")

        export_index[account_id] = payload

    if not export_index:
        raise SyncError(f"No RedBark per-account export files were found in {export_dir}")

    return export_index


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
        raise SyncError(f"Invalid JSON returned by {url}") from exc


def sure_request_json(
    base_url: str,
    api_key: str,
    method: str,
    path: str,
    *,
    params: dict[str, Any] | None = None,
    body: dict[str, Any] | None = None,
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
        "User-Agent": "redbark-to-sure-sync/1.0",
    }
    request_body: bytes | None = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        request_body = json.dumps(body).encode("utf-8")

    for attempt in range(1, MAX_RETRIES + 1):
        request = Request(url, headers=headers, data=request_body, method=method)
        LOGGER.debug("%s %s (attempt %d/%d)", method, url, attempt, MAX_RETRIES)

        try:
            with urlopen(request, timeout=timeout) as response:
                LOGGER.debug("%s %s -> HTTP %s", method, url, response.status)
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
                    "%s %s returned HTTP %s. Retrying in %ss.",
                    method,
                    url,
                    exc.code,
                    wait_seconds,
                )
                time.sleep(wait_seconds)
                continue

            raise SyncError(f"{method} {url} failed with HTTP {exc.code}: {message}") from exc
        except URLError as exc:
            if attempt < MAX_RETRIES:
                wait_seconds = min(2 ** (attempt - 1), 30)
                LOGGER.warning(
                    "Network error while calling %s %s: %s. Retrying in %ss.",
                    method,
                    url,
                    exc.reason,
                    wait_seconds,
                )
                time.sleep(wait_seconds)
                continue
            raise SyncError(f"Network error while calling {method} {url}: {exc.reason}") from exc

    raise SyncError(f"{method} {url} exceeded retry limit")


def parse_sure_transaction_collection(payload: Any) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    if isinstance(payload, list):
        return payload, None

    if not isinstance(payload, dict):
        raise SyncError("Unexpected Sure transactions response format")

    transactions = payload.get("transactions")
    if not isinstance(transactions, list):
        raise SyncError("Sure transactions response is missing a transactions array")

    pagination = payload.get("pagination")
    if pagination is not None and not isinstance(pagination, dict):
        raise SyncError("Unexpected Sure transactions pagination format")

    return transactions, pagination


def fetch_sure_transactions(
    base_url: str,
    api_key: str,
    *,
    sure_account_id: str,
    start_date: str,
    end_date: str,
    timeout: int,
) -> list[dict[str, Any]]:
    transactions: list[dict[str, Any]] = []
    page = 1

    while True:
        payload = sure_request_json(
            base_url,
            api_key,
            "GET",
            "/api/v1/transactions",
            params={
                "account_id": sure_account_id,
                "start_date": start_date,
                "end_date": end_date,
                "page": page,
                "per_page": SURE_PAGE_SIZE,
            },
            timeout=timeout,
        )
        page_transactions, pagination = parse_sure_transaction_collection(payload)
        transactions.extend(page_transactions)
        LOGGER.debug(
            "Fetched %d Sure transaction(s) for account %s on page %d",
            len(page_transactions),
            sure_account_id,
            page,
        )

        if pagination is None:
            return transactions

        total_pages = int(pagination.get("total_pages") or page)
        if page >= total_pages:
            return transactions

        page += 1


def create_sure_transaction(
    base_url: str,
    api_key: str,
    *,
    payload: dict[str, Any],
    timeout: int,
) -> dict[str, Any]:
    response = sure_request_json(
        base_url,
        api_key,
        "POST",
        "/api/v1/transactions",
        body=payload,
        timeout=timeout,
    )
    if not isinstance(response, dict):
        raise SyncError("Unexpected Sure create transaction response format")
    return response


def build_sync_token(transaction_id: str) -> str:
    normalized_id = transaction_id.strip()
    if normalized_id.startswith("bank_tx_"):
        return f"[redbark:{normalized_id}]"
    return f"[redbark:bank_tx_{normalized_id}]"


def extract_sync_token(notes: Any) -> str | None:
    if not isinstance(notes, str):
        return None
    match = SYNC_TOKEN_PATTERN.search(notes)
    if match is None:
        return None
    return build_sync_token(match.group("id"))


def normalize_name(value: str) -> str:
    return " ".join(value.lower().split())


def possible_existing_fingerprint(redbark_transaction: dict[str, Any]) -> tuple[str, int, str] | None:
    transaction_date = redbark_transaction.get("date")
    description = redbark_transaction.get("description")
    amount = redbark_transaction.get("amount")

    if not isinstance(transaction_date, str) or not isinstance(description, str) or not isinstance(amount, str):
        return None

    return (
        transaction_date,
        decimal_string_to_cents(amount),
        normalize_name(description),
    )


def decimal_string_to_cents(value: str) -> int:
    try:
        amount = Decimal(value)
    except InvalidOperation as exc:
        raise SyncError(f"Invalid decimal amount: {value}") from exc

    cents = (amount * Decimal("100")).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return int(cents)


def absolute_decimal_string(value: str) -> str:
    try:
        amount = Decimal(value).copy_abs()
    except InvalidOperation as exc:
        raise SyncError(f"Invalid decimal amount: {value}") from exc

    normalized = amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return format(normalized, "f")


def redbark_transaction_nature(redbark_transaction: dict[str, Any]) -> str:
    direction = redbark_transaction.get("direction")
    if isinstance(direction, str):
        normalized = direction.strip().lower()
        if normalized == "credit":
            return "income"
        if normalized == "debit":
            return "expense"

    amount = redbark_transaction.get("amount")
    if not isinstance(amount, str):
        raise SyncError("RedBark transaction is missing amount")

    return "income" if Decimal(amount) > 0 else "expense"


def build_sync_notes(redbark_transaction: dict[str, Any]) -> str:
    transaction_id = redbark_transaction.get("id")
    if not isinstance(transaction_id, str):
        raise SyncError("RedBark transaction is missing id")

    parts = [build_sync_token(transaction_id)]

    category = redbark_transaction.get("category")
    if isinstance(category, str) and category:
        parts.append(f"Category: {category}")

    merchant_name = redbark_transaction.get("merchantName")
    if isinstance(merchant_name, str) and merchant_name:
        parts.append(f"Merchant: {merchant_name}")

    return " | ".join(parts)


def build_sure_create_payload(
    sure_account_id: str,
    redbark_account_payload: dict[str, Any],
    redbark_transaction: dict[str, Any],
) -> dict[str, Any]:
    description = redbark_transaction.get("description")
    transaction_date = redbark_transaction.get("date")
    if not isinstance(description, str) or not description:
        raise SyncError("RedBark transaction is missing description")
    if not isinstance(transaction_date, str) or not transaction_date:
        raise SyncError("RedBark transaction is missing date")

    account = redbark_account_payload.get("account")
    if not isinstance(account, dict):
        raise SyncError("RedBark export payload is missing account object")

    currency = account.get("currency")
    if not isinstance(currency, str) or not currency:
        raise SyncError("RedBark export account is missing currency")

    amount = redbark_transaction.get("amount")
    if not isinstance(amount, str) or not amount:
        raise SyncError("RedBark transaction is missing amount")

    return {
        "transaction": {
            "account_id": sure_account_id,
            "name": description,
            "date": transaction_date,
            "amount": absolute_decimal_string(amount),
            "currency": currency,
            "nature": redbark_transaction_nature(redbark_transaction),
            "notes": build_sync_notes(redbark_transaction),
        }
    }


def sort_redbark_transactions(transactions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        transactions,
        key=lambda transaction: (
            str(transaction.get("date") or ""),
            str(transaction.get("datetime") or ""),
            str(transaction.get("id") or ""),
        ),
    )


def transaction_date_bounds(transactions: list[dict[str, Any]]) -> tuple[str, str] | None:
    dates = [transaction.get("date") for transaction in transactions if isinstance(transaction.get("date"), str)]
    if not dates:
        return None
    return min(dates), max(dates)


def sync_single_mapping(
    mapping: MappedAccount,
    *,
    redbark_export: dict[str, Any],
    sure_base_url: str,
    sure_api_key: str,
    timeout: int,
    dry_run: bool,
) -> dict[str, int]:
    sure_account_id = mapping.sure_account.get("id")
    redbark_account_id = mapping.redbark_account.get("id")
    if not isinstance(sure_account_id, str) or not isinstance(redbark_account_id, str):
        raise SyncError("Mapped account is missing Sure or RedBark account id")

    sure_account_name = str(mapping.sure_account.get("name") or sure_account_id)
    redbark_account_name = str(mapping.redbark_account.get("name") or redbark_account_id)
    redbark_transactions = redbark_export.get("transactions")
    if not isinstance(redbark_transactions, list):
        raise SyncError(f"RedBark export for account {redbark_account_id} is missing transactions")

    LOGGER.info(
        "Syncing RedBark account %s (%s) -> Sure account %s (%s)",
        redbark_account_name,
        redbark_account_id,
        sure_account_name,
        sure_account_id,
    )

    if not redbark_transactions:
        LOGGER.info("RedBark export contains no transactions for mapped account %s", redbark_account_name)
        return {"created": 0, "skipped": 0, "warnings": 0}

    bounds = transaction_date_bounds(redbark_transactions)
    if bounds is None:
        raise SyncError(f"RedBark export for account {redbark_account_id} has transactions without dates")
    start_date, end_date = bounds

    sure_transactions = fetch_sure_transactions(
        sure_base_url,
        sure_api_key,
        sure_account_id=sure_account_id,
        start_date=start_date,
        end_date=end_date,
        timeout=timeout,
    )
    LOGGER.info(
        "Fetched %d existing Sure transaction(s) for account %s between %s and %s",
        len(sure_transactions),
        sure_account_name,
        start_date,
        end_date,
    )

    existing_sync_tokens = {
        token
        for token in (extract_sync_token(transaction.get("notes")) for transaction in sure_transactions)
        if token is not None
    }
    legacy_exact_match_counts: Counter[tuple[str, int, str]] = Counter()
    for transaction in sure_transactions:
        if extract_sync_token(transaction.get("notes")) is not None:
            continue

        signed_amount_cents = transaction.get("signed_amount_cents")
        if not isinstance(signed_amount_cents, int):
            continue

        legacy_exact_match_counts[
            (
                str(transaction.get("date") or ""),
                signed_amount_cents,
                normalize_name(str(transaction.get("name") or "")),
            )
        ] += 1

    created = 0
    skipped = 0
    warnings = 0

    for redbark_transaction in sort_redbark_transactions(redbark_transactions):
        transaction_id = redbark_transaction.get("id")
        if not isinstance(transaction_id, str):
            raise SyncError(f"RedBark transaction in account {redbark_account_id} is missing id")

        sync_token = build_sync_token(transaction_id)
        if sync_token in existing_sync_tokens:
            LOGGER.debug("Skipping RedBark transaction %s because Sure already has sync token %s", transaction_id, sync_token)
            skipped += 1
            continue

        fingerprint = possible_existing_fingerprint(redbark_transaction)
        if fingerprint is not None and legacy_exact_match_counts[fingerprint] > 0:
            LOGGER.warning(
                "Skipping RedBark transaction %s because Sure account %s already has a legacy transaction with matching date, amount, and name but no sync token.",
                transaction_id,
                sure_account_name,
            )
            legacy_exact_match_counts[fingerprint] -= 1
            skipped += 1
            warnings += 1
            continue

        create_payload = build_sure_create_payload(sure_account_id, redbark_export, redbark_transaction)

        if dry_run:
            LOGGER.info(
                "DRY RUN: would create Sure transaction for RedBark transaction %s on account %s",
                transaction_id,
                sure_account_name,
            )
            created += 1
            existing_sync_tokens.add(sync_token)
            continue

        created_transaction = create_sure_transaction(
            sure_base_url,
            sure_api_key,
            payload=create_payload,
            timeout=timeout,
        )
        created_transaction_id = created_transaction.get("id")
        LOGGER.info(
            "Created Sure transaction %s from RedBark transaction %s on account %s",
            created_transaction_id,
            transaction_id,
            sure_account_name,
        )
        created += 1
        existing_sync_tokens.add(sync_token)

    return {"created": created, "skipped": skipped, "warnings": warnings}


def main() -> int:
    args = parse_args()
    setup_logging(DEFAULT_LOG_FILE)
    LOGGER.info("Verbose logging enabled")
    LOGGER.info("Writing detailed logs to %s", DEFAULT_LOG_FILE.resolve())

    load_env_file(ENV_FILE)

    sure_base_url = args.sure_base_url or os.environ.get("SURE_BASE_URL")
    sure_api_key = args.sure_api_key or os.environ.get("SURE_API_KEY")
    if not sure_base_url:
        LOGGER.error("Provide --sure-base-url or set SURE_BASE_URL in the environment or .env.")
        return 1
    if not sure_api_key:
        LOGGER.error("Provide --sure-api-key or set SURE_API_KEY in the environment or .env.")
        return 1

    map_file = Path(args.map_file)

    try:
        map_payload, mappings = load_map_file(map_file)
        redbark_export_dir = resolve_redbark_export_dir(map_file, map_payload, args.redbark_export_dir)
        redbark_export_index = load_redbark_export_index(redbark_export_dir)

        LOGGER.info("Loaded %d mapped account pair(s) from %s", len(mappings), map_file)
        LOGGER.info("Loaded %d RedBark account export file(s) from %s", len(redbark_export_index), redbark_export_dir)
        if args.dry_run:
            LOGGER.info("Dry-run mode enabled; no Sure transactions will be created")

        total_created = 0
        total_skipped = 0
        total_warnings = 0

        mapped_redbark_ids = set()
        for mapping in mappings:
            redbark_account_id = mapping.redbark_account.get("id")
            if not isinstance(redbark_account_id, str):
                raise SyncError("Mapped RedBark account is missing id")
            mapped_redbark_ids.add(redbark_account_id)

            redbark_export = redbark_export_index.get(redbark_account_id)
            if redbark_export is None:
                raise SyncError(
                    f"No RedBark export file found for mapped account {redbark_account_id} in {redbark_export_dir}"
                )

            account_result = sync_single_mapping(
                mapping,
                redbark_export=redbark_export,
                sure_base_url=sure_base_url,
                sure_api_key=sure_api_key,
                timeout=args.timeout,
                dry_run=args.dry_run,
            )
            total_created += account_result["created"]
            total_skipped += account_result["skipped"]
            total_warnings += account_result["warnings"]

        unmapped_exports = sorted(set(redbark_export_index) - mapped_redbark_ids)
        for redbark_account_id in unmapped_exports:
            export_payload = redbark_export_index[redbark_account_id]
            account = export_payload.get("account")
            account_name = account.get("name") if isinstance(account, dict) else redbark_account_id
            LOGGER.info(
                "Skipping unmapped RedBark account %s (%s)",
                account_name,
                redbark_account_id,
            )

    except SyncError as exc:
        LOGGER.error(str(exc))
        return 1

    LOGGER.info(
        "Sync finished. Created %d transaction(s), skipped %d existing transaction(s), emitted %d warning(s).",
        total_created,
        total_skipped,
        total_warnings,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())