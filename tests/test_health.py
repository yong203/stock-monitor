from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from stock_monitor.app import app


def write_credentials(path: Path) -> None:
    path.write_text('client_id = "test-id"\nclient_secret = "test-secret"\n')
    path.chmod(0o600)


def test_live_is_available_without_configuration() -> None:
    response = TestClient(app).get("/health/live")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_configuration_reports_missing_file(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("STOCK_MONITOR_CREDENTIALS_FILE", str(tmp_path / "missing.toml"))

    response = TestClient(app).get("/health/configuration")

    assert response.status_code == 503
    assert response.json() == {"status": "missing", "reason": "toss_not_configured"}


def test_configuration_does_not_claim_toss_connection(monkeypatch, tmp_path: Path) -> None:
    path = tmp_path / "credentials.toml"
    write_credentials(path)
    monkeypatch.setenv("STOCK_MONITOR_CREDENTIALS_FILE", str(path))

    response = TestClient(app).get("/health/configuration")

    assert response.status_code == 200
    assert response.json() == {"status": "configured", "toss": "not_checked"}
