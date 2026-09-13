from __future__ import annotations

import os
from pathlib import Path

import pytest

from stock_monitor.settings import CredentialsError, UnsafeCredentialsError, load_credentials


def write_credentials(path: Path, content: str | None = None) -> None:
    path.write_text(
        content or 'client_id = "test-id"\nclient_secret = "test-secret"\n',
        encoding="utf-8",
    )
    path.chmod(0o600)


def test_load_credentials_from_safe_file(tmp_path: Path) -> None:
    path = tmp_path / "credentials.toml"
    write_credentials(path)

    credentials = load_credentials(path)

    assert credentials.client_id == "test-id"
    assert credentials.client_secret == "test-secret"
    assert credentials.report_writer_token is None
    assert "test-id" not in repr(credentials)
    assert "test-secret" not in repr(credentials)


def test_loads_optional_report_writer_token_without_exposing_it(tmp_path: Path) -> None:
    path = tmp_path / "credentials.toml"
    token = "r" * 32
    write_credentials(
        path,
        f'client_id = "test-id"\nclient_secret = "test-secret"\nreport_writer_token = "{token}"\n',
    )

    credentials = load_credentials(path)

    assert credentials.report_writer_token == token
    assert token not in repr(credentials)


@pytest.mark.parametrize(
    ("content", "code"),
    [
        ('client_id = "test-id"', "client_secret_missing"),
        ('client_secret = "test-secret"', "client_id_missing"),
        (
            'client_id = "test-id"\nclient_secret = "test-secret"\nreport_writer_token = "short"',
            "report_writer_token_invalid",
        ),
        (
            'client_id = "test-id"\nclient_secret = "test-secret"\n'
            'report_writer_token = "                                "',
            "report_writer_token_invalid",
        ),
        ("client_id = [", "credentials_invalid_toml"),
    ],
)
def test_rejects_invalid_content(tmp_path: Path, content: str, code: str) -> None:
    path = tmp_path / "credentials.toml"
    write_credentials(path, content)

    with pytest.raises(CredentialsError, match=code):
        load_credentials(path)


def test_rejects_missing_file(tmp_path: Path) -> None:
    with pytest.raises(CredentialsError, match="credentials_not_found"):
        load_credentials(tmp_path / "missing.toml")


def test_rejects_invalid_utf8(tmp_path: Path) -> None:
    path = tmp_path / "credentials.toml"
    path.write_bytes(b"client_id = \xff")
    path.chmod(0o600)

    with pytest.raises(CredentialsError, match="credentials_invalid_toml"):
        load_credentials(path)


def test_rejects_open_permissions(tmp_path: Path) -> None:
    path = tmp_path / "credentials.toml"
    write_credentials(path)
    path.chmod(0o644)

    with pytest.raises(UnsafeCredentialsError, match="permissions_must_be_0600"):
        load_credentials(path)


def test_rejects_symlink(tmp_path: Path) -> None:
    target = tmp_path / "target.toml"
    link = tmp_path / "credentials.toml"
    write_credentials(target)
    os.symlink(target, link)

    with pytest.raises(UnsafeCredentialsError, match="symlink_not_allowed"):
        load_credentials(link)


def test_rejects_directory(tmp_path: Path) -> None:
    path = tmp_path / "credentials.toml"
    path.mkdir(mode=0o600)

    with pytest.raises(UnsafeCredentialsError, match="not_regular_file"):
        load_credentials(path)


def test_rejects_owner_mismatch(monkeypatch, tmp_path: Path) -> None:
    path = tmp_path / "credentials.toml"
    write_credentials(path)
    monkeypatch.setattr(os, "getuid", lambda: path.stat().st_uid + 1)

    with pytest.raises(UnsafeCredentialsError, match="owner_mismatch"):
        load_credentials(path)
