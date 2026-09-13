#!/usr/bin/env python3
"""Bounded, secret-safe client for stock report reads and writes."""

from __future__ import annotations

import argparse
import io
import json
import math
import os
import re
import stat
import sys
import tomllib
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

DEFAULT_CONFIG = Path("~/.config/stock-monitor/report-client.toml")
TIMEOUT_SECONDS = 15
MAX_REQUEST_BYTES = 256 * 1024
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_DART_CORP_ZIP_BYTES = 16 * 1024 * 1024
MAX_DART_CORP_XML_BYTES = 64 * 1024 * 1024
MAX_DART_CORP_MATCHES = 20
MAX_REPORT_JSON_BYTES = 32_000
DATE = re.compile(r"^\d{8}$")
SOURCE_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
STANCE = {"favorable", "balanced", "cautious", "insufficient"}
CONFIDENCE = {"high", "medium", "low"}
CHANGE_LABEL = {"initial", "view_changed", "view_reinforced", "facts_updated", "unchanged"}
ITEM_KIND = {"fact", "inference", "opinion", "unknown"}


class ClientError(Exception):
    """A stable error that contains no configuration secrets."""


@dataclass(frozen=True)
class Config:
    base_url: str
    writer_token: str = field(repr=False)
    dart_api_key: str = field(repr=False)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self,
        req: Any,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> None:
        return None


def load_config(path: Path) -> Config:
    resolved = path.expanduser()
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(resolved, flags)
        with os.fdopen(descriptor, "rb") as handle:
            file_stat = os.fstat(handle.fileno())
            if not stat.S_ISREG(file_stat.st_mode):
                raise ClientError("config_not_regular_file")
            if stat.S_IMODE(file_stat.st_mode) != 0o600:
                raise ClientError("config_permissions_must_be_0600")
            if hasattr(os, "getuid") and file_stat.st_uid != os.getuid():
                raise ClientError("config_owner_mismatch")
            data = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError):
        raise ClientError("config_unavailable") from None

    if not isinstance(data, dict):
        raise ClientError("invalid_config")
    base_url = data.get("base_url")
    writer_token = data.get("writer_token")
    dart_api_key = data.get("dart_api_key")
    if (
        not isinstance(base_url, str)
        or not isinstance(writer_token, str)
        or len(writer_token) < 32
        or writer_token != writer_token.strip()
    ):
        raise ClientError("invalid_config")
    if not isinstance(dart_api_key, str) or (
        dart_api_key and (len(dart_api_key) != 40 or dart_api_key != dart_api_key.strip())
    ):
        raise ClientError("invalid_config")
    return Config(
        base_url=validate_base_url(base_url),
        writer_token=writer_token,
        dart_api_key=dart_api_key,
    )


def validate_base_url(value: str) -> str:
    parsed = urllib.parse.urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ClientError("invalid_base_url")
    return value.rstrip("/")


def validate_http_url(value: Any) -> None:
    if not isinstance(value, str):
        raise ClientError("invalid_source_url")
    parsed = urllib.parse.urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise ClientError("invalid_source_url")


def endpoint(base_url: str, *parts: str) -> str:
    encoded = "/".join(urllib.parse.quote(part, safe="") for part in parts)
    return f"{base_url}/{encoded}"


def open_bytes(request: urllib.request.Request, *, max_bytes: int) -> bytes:
    opener = urllib.request.build_opener(NoRedirect)
    try:
        with opener.open(request, timeout=TIMEOUT_SECONDS) as response:
            length = response.headers.get("Content-Length")
            if length is not None:
                try:
                    if int(length) > max_bytes:
                        raise ClientError("response_too_large")
                except ValueError:
                    raise ClientError("invalid_content_length") from None
            body = response.read(max_bytes + 1)
    except urllib.error.HTTPError as exc:
        raise ClientError(f"http_{exc.code}") from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise ClientError("transport_error") from None
    if len(body) > max_bytes:
        raise ClientError("response_too_large")
    return body


def open_json(request: urllib.request.Request, *, max_bytes: int = MAX_RESPONSE_BYTES) -> Any:
    try:
        return json.loads(open_bytes(request, max_bytes=max_bytes))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ClientError("invalid_json_response") from None


def api_request(config: Config, method: str, path: tuple[str, ...], payload: Any = None) -> Any:
    body = None
    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {config.writer_token}",
    }
    if payload is not None:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
        if len(body) > MAX_REQUEST_BYTES:
            raise ClientError("request_too_large")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(
        endpoint(config.base_url, *path),
        data=body,
        headers=headers,
        method=method,
    )
    return open_json(request)


def decode_json_document(raw: bytes) -> dict[str, Any]:
    if len(raw) > MAX_REQUEST_BYTES:
        raise ClientError("request_too_large")
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ClientError("invalid_json_input") from None
    if not isinstance(value, dict):
        raise ClientError("report_must_be_object")
    return value


def require_string(
    value: Any,
    code: str,
    *,
    maximum: int,
    nullable: bool = False,
) -> None:
    if nullable and value is None:
        return
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value.strip()) > maximum
        or "\x00" in value
    ):
        raise ClientError(code)


def validate_timestamp(value: Any, code: str, *, nullable: bool = False) -> None:
    if nullable and value is None:
        return
    require_string(value, code, maximum=40)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ClientError(code) from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ClientError(code)


def validate_price(value: Any) -> None:
    require_string(value, "invalid_price", maximum=32)
    try:
        price = Decimal(value)
    except InvalidOperation:
        raise ClientError("invalid_price") from None
    if not price.is_finite() or price < 0:
        raise ClientError("invalid_price")


def validate_snapshot_value(value: Any, *, depth: int = 0) -> None:
    if depth > 6:
        raise ClientError("invalid_snapshot")
    if value is None or isinstance(value, (bool, int)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ClientError("invalid_snapshot")
        return
    if isinstance(value, str):
        if len(value) > 4_000 or "\x00" in value:
            raise ClientError("invalid_snapshot")
        return
    if isinstance(value, dict):
        if len(value) > 50:
            raise ClientError("invalid_snapshot")
        for key, item in value.items():
            require_string(key, "invalid_snapshot", maximum=64)
            validate_snapshot_value(item, depth=depth + 1)
        return
    if isinstance(value, list):
        if len(value) > 50:
            raise ClientError("invalid_snapshot")
        for item in value:
            validate_snapshot_value(item, depth=depth + 1)
        return
    raise ClientError("invalid_snapshot")


def require_bounded_json(value: Any, code: str) -> None:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    if len(encoded) > MAX_REPORT_JSON_BYTES:
        raise ClientError(code)


def validate_report(report: dict[str, Any]) -> None:
    required = {
        "run_id",
        "market",
        "symbol",
        "analyzed_at",
        "price",
        "currency",
        "title",
        "summary",
        "short_term_stance",
        "medium_term_stance",
        "confidence",
        "change_label",
        "sections",
        "sources",
        "snapshot",
    }
    if set(report) != required:
        raise ClientError("invalid_report_fields")
    require_string(report["market"], "invalid_market", maximum=16)
    require_string(report["symbol"], "invalid_symbol", maximum=64)
    validate_timestamp(report["analyzed_at"], "invalid_analyzed_at")
    require_string(report["title"], "invalid_title", maximum=200)
    require_string(report["summary"], "invalid_summary", maximum=5_000)
    try:
        uuid.UUID(report["run_id"])
    except (ValueError, TypeError, AttributeError):
        raise ClientError("invalid_run_id") from None
    validate_price(report["price"])
    require_string(report["currency"], "invalid_currency", maximum=3)
    if not report["currency"].isascii() or not report["currency"].isalpha():
        raise ClientError("invalid_currency")
    if report["short_term_stance"] not in STANCE or report["medium_term_stance"] not in STANCE:
        raise ClientError("invalid_stance")
    if report["confidence"] not in CONFIDENCE:
        raise ClientError("invalid_confidence")
    if report["change_label"] not in CHANGE_LABEL:
        raise ClientError("invalid_change_label")
    if (
        not isinstance(report["sections"], list)
        or not 1 <= len(report["sections"]) <= 12
        or not isinstance(report["sources"], list)
        or not 1 <= len(report["sources"]) <= 20
    ):
        raise ClientError("invalid_report_collections")
    if not isinstance(report["snapshot"], dict):
        raise ClientError("invalid_snapshot")
    validate_snapshot_value(report["snapshot"])
    require_bounded_json(report["snapshot"], "invalid_snapshot")

    source_keys: set[str] = set()
    market_data_keys: set[str] = set()
    for source in report["sources"]:
        if not isinstance(source, dict):
            raise ClientError("invalid_source")
        expected = {
            "source_key",
            "kind",
            "title",
            "publisher",
            "url",
            "published_at",
            "retrieved_at",
        }
        if set(source) != expected:
            raise ClientError("invalid_source_fields")
        require_string(source["source_key"], "invalid_source_key", maximum=128)
        if SOURCE_KEY.fullmatch(source["source_key"]) is None:
            raise ClientError("invalid_source_key")
        require_string(source["kind"], "invalid_source_kind", maximum=32)
        require_string(source["title"], "invalid_source_title", maximum=300)
        require_string(source["publisher"], "invalid_source_publisher", maximum=120)
        if source["source_key"] in source_keys:
            raise ClientError("duplicate_source_key")
        source_keys.add(source["source_key"])
        if source["kind"] == "market_data":
            market_data_keys.add(source["source_key"])
        validate_http_url(source["url"])
        validate_timestamp(
            source["published_at"],
            "invalid_source_published_at",
            nullable=True,
        )
        validate_timestamp(source["retrieved_at"], "invalid_source_retrieved_at")

    linked_source_keys: set[str] = set()
    for section in report["sections"]:
        if not isinstance(section, dict) or set(section) != {"title", "items"}:
            raise ClientError("invalid_section")
        require_string(section["title"], "invalid_section_title", maximum=120)
        if not isinstance(section["items"], list) or not 1 <= len(section["items"]) <= 12:
            raise ClientError("invalid_section_items")
        for item in section["items"]:
            if not isinstance(item, dict) or set(item) != {"kind", "text", "source_keys"}:
                raise ClientError("invalid_section_item")
            if item["kind"] not in ITEM_KIND:
                raise ClientError("invalid_item_kind")
            require_string(item["text"], "invalid_item_text", maximum=4_000)
            if not isinstance(item["source_keys"], list) or any(
                not isinstance(key, str) or key not in source_keys for key in item["source_keys"]
            ):
                raise ClientError("unknown_source_key")
            if len(item["source_keys"]) != len(set(item["source_keys"])):
                raise ClientError("duplicate_item_source_key")
            if item["kind"] != "unknown" and not item["source_keys"]:
                raise ClientError("claim_requires_source")
            linked_source_keys.update(item["source_keys"])
    if not market_data_keys & linked_source_keys:
        raise ClientError("market_data_source_required")
    require_bounded_json(report["sections"], "invalid_sections")


def dart_disclosures(config: Config, corp_code: str, begin: str, end: str) -> Any:
    if not config.dart_api_key:
        raise ClientError("dart_api_key_unavailable")
    if not (
        corp_code.isdigit()
        and len(corp_code) == 8
        and DATE.fullmatch(begin)
        and DATE.fullmatch(end)
    ):
        raise ClientError("invalid_dart_query")
    try:
        begin_date = datetime.strptime(begin, "%Y%m%d").date()
        end_date = datetime.strptime(end, "%Y%m%d").date()
    except ValueError:
        raise ClientError("invalid_dart_query") from None
    if begin_date > end_date:
        raise ClientError("invalid_dart_query")
    query = urllib.parse.urlencode(
        {
            "crtfc_key": config.dart_api_key,
            "corp_code": corp_code,
            "bgn_de": begin,
            "end_de": end,
            "sort": "date",
            "sort_mth": "desc",
            "page_count": 100,
        }
    )
    request = urllib.request.Request(
        f"https://opendart.fss.or.kr/api/list.json?{query}",
        headers={"Accept": "application/json", "User-Agent": "stock-monitor-report-client/1.0"},
        method="GET",
    )
    return open_json(request)


def parse_dart_corp_codes(payload: bytes, query: str) -> dict[str, Any]:
    normalized_query = normalize_dart_corp_query(query)
    try:
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            members = [
                member
                for member in archive.infolist()
                if Path(member.filename).name.casefold() == "corpcode.xml"
            ]
            if len(members) != 1 or members[0].file_size > MAX_DART_CORP_XML_BYTES:
                raise ClientError("invalid_dart_corp_archive")
            with archive.open(members[0]) as handle:
                xml = handle.read(MAX_DART_CORP_XML_BYTES + 1)
    except (OSError, zipfile.BadZipFile, RuntimeError):
        raise ClientError("invalid_dart_corp_archive") from None
    if len(xml) > MAX_DART_CORP_XML_BYTES:
        raise ClientError("dart_corp_xml_too_large")
    try:
        root = ElementTree.fromstring(xml)
    except ElementTree.ParseError:
        raise ClientError("invalid_dart_corp_xml") from None

    exact_stock: list[dict[str, str]] = []
    exact_name: list[dict[str, str]] = []
    partial_name: list[dict[str, str]] = []
    for item in root.findall("list"):
        corp_code = (item.findtext("corp_code") or "").strip()
        corp_name = (item.findtext("corp_name") or "").strip()
        stock_code = (item.findtext("stock_code") or "").strip()
        modify_date = (item.findtext("modify_date") or "").strip()
        if not (corp_code and corp_name and stock_code):
            continue
        match = {
            "corp_code": corp_code,
            "corp_name": corp_name,
            "stock_code": stock_code,
            "modify_date": modify_date,
        }
        if stock_code.casefold() == normalized_query:
            exact_stock.append(match)
        elif corp_name.casefold() == normalized_query:
            exact_name.append(match)
        elif normalized_query in corp_name.casefold():
            partial_name.append(match)
    selected = exact_stock or exact_name or partial_name
    matches = selected[:MAX_DART_CORP_MATCHES]
    total = len(selected)
    return {"matches": matches, "truncated": total > len(matches)}


def normalize_dart_corp_query(query: Any) -> str:
    if not isinstance(query, str):
        raise ClientError("invalid_dart_corp_query")
    normalized = query.strip().casefold()
    if not 1 <= len(normalized) <= 80 or "\x00" in normalized:
        raise ClientError("invalid_dart_corp_query")
    return normalized


def dart_corp_code(config: Config, query: str) -> dict[str, Any]:
    if not config.dart_api_key:
        raise ClientError("dart_api_key_unavailable")
    normalize_dart_corp_query(query)
    url = "https://opendart.fss.or.kr/api/corpCode.xml?" + urllib.parse.urlencode(
        {"crtfc_key": config.dart_api_key}
    )
    request = urllib.request.Request(
        url,
        headers={"Accept": "application/zip", "User-Agent": "stock-monitor-report-client/1.0"},
        method="GET",
    )
    payload = open_bytes(request, max_bytes=MAX_DART_CORP_ZIP_BYTES)
    return parse_dart_corp_codes(payload, query)


def redact_secrets(value: Any, config: Config | None) -> Any:
    secrets = (
        ()
        if config is None
        else tuple(secret for secret in (config.writer_token, config.dart_api_key) if secret)
    )
    if isinstance(value, str):
        for secret in secrets:
            value = value.replace(secret, "[REDACTED]")
        return value
    if isinstance(value, list):
        return [redact_secrets(item, config) for item in value]
    if isinstance(value, dict):
        return {
            redact_secrets(key, config): redact_secrets(item, config) for key, item in value.items()
        }
    return value


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description=__doc__)
    value.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    commands = value.add_subparsers(dest="command", required=True)
    commands.add_parser("targets")
    context = commands.add_parser("context")
    context.add_argument("market")
    context.add_argument("symbol")
    commands.add_parser("validate")
    commands.add_parser("submit")
    corp = commands.add_parser("dart-corp-code")
    corp.add_argument("query")
    dart = commands.add_parser("dart-disclosures")
    dart.add_argument("corp_code")
    dart.add_argument("--begin", required=True)
    dart.add_argument("--end", required=True)
    return value


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        config = None if args.command == "validate" else load_config(args.config)
        if args.command == "targets":
            assert config is not None
            result = api_request(config, "GET", ("internal", "v1", "report-targets"))
        elif args.command == "context":
            assert config is not None
            result = api_request(
                config,
                "GET",
                ("internal", "v1", "report-context", args.market, args.symbol),
            )
        elif args.command in {"validate", "submit"}:
            report = decode_json_document(sys.stdin.buffer.read(MAX_REQUEST_BYTES + 1))
            validate_report(report)
            if args.command == "validate":
                result = {"valid": True}
            else:
                assert config is not None
                result = api_request(config, "POST", ("internal", "v1", "reports"), report)
        elif args.command == "dart-corp-code":
            assert config is not None
            result = dart_corp_code(config, args.query)
        else:
            assert config is not None
            result = dart_disclosures(config, args.corp_code, args.begin, args.end)
        print(json.dumps(redact_secrets(result, config), ensure_ascii=False, separators=(",", ":")))
        return 0
    except ClientError as exc:
        print(f"report_client_error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
