from __future__ import annotations

import importlib.util
import io
import json
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

SCRIPT = Path(__file__).with_name("report_client.py")
SPEC = importlib.util.spec_from_file_location("report_client", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
client = importlib.util.module_from_spec(SPEC)
sys.modules["report_client"] = client
SPEC.loader.exec_module(client)


def valid_report() -> dict[str, object]:
    return {
        "run_id": "6bbf4c89-82c7-4ca7-b6e4-5fd8b0dcb05d",
        "market": "NASDAQ",
        "symbol": "AAPL",
        "analyzed_at": "2026-09-13T21:00:00+09:00",
        "price": "234.56",
        "currency": "USD",
        "title": "현재 관점",
        "summary": "근거 기반 요약",
        "short_term_stance": "balanced",
        "medium_term_stance": "favorable",
        "confidence": "medium",
        "change_label": "initial",
        "sections": [
            {
                "title": "사실",
                "items": [{"kind": "fact", "text": "공시된 사실", "source_keys": ["sec-1"]}],
            }
        ],
        "sources": [
            {
                "source_key": "sec-1",
                "kind": "market_data",
                "title": "현재가",
                "publisher": "토스증권",
                "url": "https://developers.tossinvest.com/",
                "published_at": None,
                "retrieved_at": "2026-09-13T21:00:00+09:00",
            }
        ],
        "snapshot": {},
    }


def dart_corp_archive() -> bytes:
    xml = """<?xml version="1.0" encoding="UTF-8"?>
<result>
  <list>
    <corp_code>00126380</corp_code><corp_name>삼성전자</corp_name>
    <stock_code>005930</stock_code><modify_date>20240101</modify_date>
  </list>
  <list>
    <corp_code>00164779</corp_code><corp_name>삼성증권</corp_name>
    <stock_code>016360</stock_code><modify_date>20240102</modify_date>
  </list>
  <list>
    <corp_code>00000001</corp_code><corp_name>삼성비상장</corp_name>
    <stock_code> </stock_code><modify_date>20240103</modify_date>
  </list>
</result>"""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("CORPCODE.xml", xml)
    return buffer.getvalue()


class ReportClientTest(unittest.TestCase):
    def test_loads_expected_config_without_exposing_values(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "client.toml"
            path.write_text(
                'base_url = "http://192.168.0.29:8000"\n'
                'writer_token = "private-token-private-token-123456"\n'
                'dart_api_key = "1234567890123456789012345678901234567890"\n'
            )
            path.chmod(0o600)
            config = client.load_config(path)
        self.assertEqual(config.base_url, "http://192.168.0.29:8000")
        self.assertEqual(config.writer_token, "private-token-private-token-123456")

    def test_rejects_non_http_base_url(self) -> None:
        with self.assertRaisesRegex(client.ClientError, "invalid_base_url"):
            client.validate_base_url("file:///tmp/socket")

    def test_rejects_whitespace_only_writer_token(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "client.toml"
            path.write_text(
                'base_url = "http://192.168.0.29:8000"\n'
                f'writer_token = "{" " * 32}"\n'
                'dart_api_key = ""\n'
            )
            path.chmod(0o600)
            with self.assertRaisesRegex(client.ClientError, "invalid_config"):
                client.load_config(path)

    def test_rejects_non_ascii_writer_token(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "client.toml"
            path.write_text(
                'base_url = "http://192.168.0.29:8000"\n'
                f'writer_token = "{"가" * 32}"\n'
                'dart_api_key = ""\n'
            )
            path.chmod(0o600)
            with self.assertRaisesRegex(client.ClientError, "invalid_config"):
                client.load_config(path)

    def test_rejects_unsafe_config_permissions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "client.toml"
            path.write_text(
                'base_url = "http://192.168.0.29:8000"\n'
                f'writer_token = "{"w" * 32}"\n'
                'dart_api_key = ""\n'
            )
            path.chmod(0o644)
            with self.assertRaisesRegex(
                client.ClientError,
                "config_permissions_must_be_0600",
            ):
                client.load_config(path)

    def test_rejects_source_url_credentials(self) -> None:
        with self.assertRaisesRegex(client.ClientError, "invalid_source_url"):
            client.validate_http_url("https://user:password@example.com/report")

    def test_encodes_path_segments(self) -> None:
        self.assertEqual(
            client.endpoint("http://phone:8000", "internal", "v1", "context", "A/B"),
            "http://phone:8000/internal/v1/context/A%2FB",
        )

    def test_accepts_valid_report(self) -> None:
        client.validate_report(valid_report())

    def test_rejects_unlinked_fact(self) -> None:
        report = valid_report()
        report["sections"][0]["items"][0]["source_keys"] = []  # type: ignore[index]
        with self.assertRaisesRegex(client.ClientError, "claim_requires_source"):
            client.validate_report(report)

    def test_allows_unsourced_unknown_but_not_unconfirmed_kind(self) -> None:
        report = valid_report()
        item = {"kind": "unknown", "text": "공식 자료 확인 불가", "source_keys": []}
        report["sections"][0]["items"].append(item)  # type: ignore[index]
        client.validate_report(report)
        item["kind"] = "unconfirmed"
        with self.assertRaisesRegex(client.ClientError, "invalid_item_kind"):
            client.validate_report(report)

    def test_rejects_missing_quote_price(self) -> None:
        report = valid_report()
        report["price"] = None
        with self.assertRaisesRegex(client.ClientError, "invalid_price"):
            client.validate_report(report)

    def test_requires_linked_market_data_source(self) -> None:
        report = valid_report()
        report["sources"][0]["kind"] = "regulator"  # type: ignore[index]
        with self.assertRaisesRegex(client.ClientError, "market_data_source_required"):
            client.validate_report(report)

    def test_rejects_unsafe_source_key(self) -> None:
        report = valid_report()
        report["sources"][0]["source_key"] = "unsafe key"  # type: ignore[index]
        with self.assertRaisesRegex(client.ClientError, "invalid_source_key"):
            client.validate_report(report)

    def test_validate_command_does_not_require_config(self) -> None:
        completed = subprocess.run(
            [sys.executable, str(SCRIPT), "validate"],
            input=json.dumps(valid_report()),
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(json.loads(completed.stdout), {"valid": True})

    def test_redacts_configured_secrets(self) -> None:
        config = client.Config(
            "http://phone:8000",
            "w" * 32,
            "d" * 40,
        )
        self.assertEqual(
            client.redact_secrets({"value": f"before-{config.writer_token}-after"}, config),
            {"value": "before-[REDACTED]-after"},
        )

    def test_rejects_oversized_input(self) -> None:
        with self.assertRaisesRegex(client.ClientError, "request_too_large"):
            client.decode_json_document(b" " * (client.MAX_REQUEST_BYTES + 1))

    def test_rejects_snapshot_outside_server_limits(self) -> None:
        report = valid_report()
        report["snapshot"] = {"items": list(range(51))}
        with self.assertRaisesRegex(client.ClientError, "invalid_snapshot"):
            client.validate_report(report)

        report["snapshot"] = {str(index): "x" * 4_000 for index in range(9)}
        with self.assertRaisesRegex(client.ClientError, "invalid_snapshot"):
            client.validate_report(report)

    def test_rejects_sections_over_server_json_limit(self) -> None:
        report = valid_report()
        report["sections"][0]["items"] = [  # type: ignore[index]
            {
                "kind": "fact",
                "text": "x" * 4_000,
                "source_keys": ["sec-1"],
            }
            for _ in range(9)
        ]
        with self.assertRaisesRegex(client.ClientError, "invalid_sections"):
            client.validate_report(report)

    def test_bounds_response_body_and_timeout(self) -> None:
        class Response:
            headers: dict[str, str] = {}

            def __enter__(self) -> Response:
                return self

            def __exit__(self, *_: object) -> None:
                return None

            def read(self, size: int) -> bytes:
                return b"x" * size

        class Opener:
            def open(self, request: object, *, timeout: int) -> Response:
                self.request = request
                self.timeout = timeout
                return Response()

        opener = Opener()
        request = client.urllib.request.Request("https://example.test/data")
        with (
            mock.patch.object(client.urllib.request, "build_opener", return_value=opener),
            self.assertRaisesRegex(client.ClientError, "response_too_large"),
        ):
            client.open_bytes(request, max_bytes=4)
        self.assertEqual(opener.timeout, client.TIMEOUT_SECONDS)

    def test_resolves_dart_company_by_stock_code_or_name(self) -> None:
        stock = client.parse_dart_corp_codes(dart_corp_archive(), "005930")
        name = client.parse_dart_corp_codes(dart_corp_archive(), "삼성")
        self.assertEqual(stock["matches"][0]["corp_code"], "00126380")
        self.assertEqual([item["stock_code"] for item in name["matches"]], ["005930", "016360"])
        self.assertFalse(name["truncated"])

    def test_rejects_invalid_dart_company_archive(self) -> None:
        with self.assertRaisesRegex(client.ClientError, "invalid_dart_corp_archive"):
            client.parse_dart_corp_codes(b"not-a-zip", "005930")


if __name__ == "__main__":
    unittest.main()
