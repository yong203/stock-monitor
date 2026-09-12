from __future__ import annotations

import errno
import os
import stat
import tomllib
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_CREDENTIALS_FILE = Path("~/.config/stock-monitor/credentials.toml")


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


def credentials_path() -> Path:
    configured = os.environ.get("STOCK_MONITOR_CREDENTIALS_FILE")
    return Path(configured or DEFAULT_CREDENTIALS_FILE).expanduser()


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
    if not isinstance(client_id, str) or not client_id.strip():
        raise CredentialsError("client_id_missing")
    if not isinstance(client_secret, str) or not client_secret.strip():
        raise CredentialsError("client_secret_missing")
    return Credentials(client_id=client_id, client_secret=client_secret)


def _validate_file(file_stat: os.stat_result) -> None:
    if not stat.S_ISREG(file_stat.st_mode):
        raise UnsafeCredentialsError("credentials_not_regular_file")
    if stat.S_IMODE(file_stat.st_mode) != 0o600:
        raise UnsafeCredentialsError("credentials_permissions_must_be_0600")
    if hasattr(os, "getuid") and file_stat.st_uid != os.getuid():
        raise UnsafeCredentialsError("credentials_owner_mismatch")
