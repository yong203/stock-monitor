from __future__ import annotations

import errno
import os
import re
import stat
import tomllib
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_CREDENTIALS_FILE = Path("~/.config/stock-monitor/credentials.toml")
DEFAULT_DATABASE_FILE = Path("~/.local/share/stock-monitor/stock.db")
REPORT_WRITER_TOKEN = re.compile(r"^[A-Za-z0-9_-]{32,256}$")


class CredentialsError(Exception):
    """A credentials file cannot be used safely."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class UnsafeCredentialsError(CredentialsError):
    """A credentials file has unsafe ownership, type, or permissions."""


@dataclass(frozen=True)
class Credentials:
    client_id: str = field(repr=False)
    client_secret: str = field(repr=False)
    report_writer_token: str | None = field(default=None, repr=False)


def credentials_path() -> Path:
    configured = os.environ.get("STOCK_MONITOR_CREDENTIALS_FILE")
    return Path(configured or DEFAULT_CREDENTIALS_FILE).expanduser()


def database_path() -> Path:
    configured = os.environ.get("STOCK_MONITOR_DATABASE_FILE")
    return Path(configured or DEFAULT_DATABASE_FILE).expanduser()


def load_credentials(path: Path) -> Credentials:
    resolved_path = path.expanduser()
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW

    try:
        descriptor = os.open(resolved_path, flags)
    except FileNotFoundError as error:
        raise CredentialsError("credentials_not_found") from error
    except OSError as error:
        if error.errno == errno.ELOOP:
            raise UnsafeCredentialsError("credentials_symlink_not_allowed") from error
        raise CredentialsError("credentials_unreadable") from error

    try:
        _validate_file(os.fstat(descriptor))
        file = os.fdopen(descriptor, "rb")
    except Exception:
        with suppress(OSError):
            os.close(descriptor)
        raise

    with file:
        try:
            raw = tomllib.load(file)
        except (tomllib.TOMLDecodeError, UnicodeDecodeError) as error:
            raise CredentialsError("credentials_invalid_toml") from error

    client_id = raw.get("client_id")
    client_secret = raw.get("client_secret")
    report_writer_token = raw.get("report_writer_token")
    if not isinstance(client_id, str) or not client_id.strip():
        raise CredentialsError("client_id_missing")
    if not isinstance(client_secret, str) or not client_secret.strip():
        raise CredentialsError("client_secret_missing")
    if report_writer_token is not None and (
        not isinstance(report_writer_token, str)
        or REPORT_WRITER_TOKEN.fullmatch(report_writer_token) is None
    ):
        raise CredentialsError("report_writer_token_invalid")
    return Credentials(
        client_id=client_id,
        client_secret=client_secret,
        report_writer_token=report_writer_token,
    )


def _validate_file(file_stat: os.stat_result) -> None:
    if not stat.S_ISREG(file_stat.st_mode):
        raise UnsafeCredentialsError("credentials_not_regular_file")
    if stat.S_IMODE(file_stat.st_mode) != 0o600:
        raise UnsafeCredentialsError("credentials_permissions_must_be_0600")
    if hasattr(os, "getuid") and file_stat.st_uid != os.getuid():
        raise UnsafeCredentialsError("credentials_owner_mismatch")
