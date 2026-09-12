from __future__ import annotations

import json
import sys

import pytest

from stock_monitor import __main__
from stock_monitor.diagnostics import DiagnosticResult
from stock_monitor.instruments import InstrumentSyncResult
from stock_monitor.settings import Credentials


def run_cli(monkeypatch, capsys, *arguments: str) -> tuple[int, dict[str, object]]:
    monkeypatch.setattr(sys, "argv", ["stock-monitor", *arguments])
    with pytest.raises(SystemExit) as exit_info:
        __main__.main()
    output = capsys.readouterr()
    assert output.err == ""
    return exit_info.value.code, json.loads(output.out)


def test_missing_credentials_returns_safe_json(monkeypatch, capsys, tmp_path) -> None:
    exit_code, output = run_cli(
        monkeypatch,
        capsys,
        "--credentials-file",
        str(tmp_path / "missing.toml"),
        "diagnose-toss",
    )

    assert exit_code == 2
    assert output == {
        "ok": False,
        "stage": "configuration",
        "code": "credentials_not_found",
        "exit_code": 2,
    }


def test_unsafe_credentials_returns_exit_3(monkeypatch, capsys, tmp_path) -> None:
    path = tmp_path / "credentials.toml"
    path.write_text('client_id = "private-id"\nclient_secret = "private-secret"\n')
    path.chmod(0o644)

    exit_code, output = run_cli(
        monkeypatch,
        capsys,
        "--credentials-file",
        str(path),
        "diagnose-toss",
    )

    assert exit_code == 3
    assert output["code"] == "credentials_permissions_must_be_0600"
    assert "private" not in json.dumps(output)


def test_diagnostic_exit_code_is_forwarded(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        __main__,
        "load_credentials",
        lambda path: Credentials(client_id="private-id", client_secret="private-secret"),
    )
    monkeypatch.setattr(
        __main__,
        "run_toss_diagnostic",
        lambda credentials: DiagnosticResult(
            ok=False,
            stage="market_data",
            code="rate_limited",
            exit_code=12,
            retry_after_seconds="2",
        ),
    )

    exit_code, output = run_cli(monkeypatch, capsys, "diagnose-toss")

    assert exit_code == 12
    assert output["retry_after_seconds"] == "2"
    assert "private" not in json.dumps(output)


def test_instrument_sync_result_is_printed(monkeypatch, capsys, tmp_path) -> None:
    monkeypatch.setattr(
        __main__,
        "load_credentials",
        lambda path: Credentials(client_id="private-id", client_secret="private-secret"),
    )
    monkeypatch.setattr(__main__, "initialize_database", lambda path: None)
    monkeypatch.setattr(
        __main__,
        "sync_instruments",
        lambda credentials, path: InstrumentSyncResult(
            ok=True,
            stage="complete",
            code="instrument_sync_ok",
            exit_code=0,
            markets=7,
            instruments=123,
        ),
    )

    exit_code, output = run_cli(
        monkeypatch,
        capsys,
        "--database-file",
        str(tmp_path / "stock.db"),
        "sync-instruments",
    )

    assert exit_code == 0
    assert output["markets"] == 7
    assert output["instruments"] == 123


def test_instrument_sync_uses_database_environment(monkeypatch, capsys, tmp_path) -> None:
    expected_path = tmp_path / "custom.db"
    seen: list[object] = []
    monkeypatch.setenv("STOCK_MONITOR_DATABASE_FILE", str(expected_path))
    monkeypatch.setattr(
        __main__,
        "load_credentials",
        lambda path: Credentials(client_id="private-id", client_secret="private-secret"),
    )
    monkeypatch.setattr(__main__, "initialize_database", seen.append)
    monkeypatch.setattr(
        __main__,
        "sync_instruments",
        lambda credentials, path: InstrumentSyncResult(True, "complete", "ok", 0, 7, 1),
    )

    exit_code, _ = run_cli(monkeypatch, capsys, "sync-instruments")

    assert exit_code == 0
    assert seen == [expected_path]
