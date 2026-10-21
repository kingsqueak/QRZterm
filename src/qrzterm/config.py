"""Account file for QRZterm.

The password is stored unencrypted, same as 590Ctrl, so the file is kept
private to the user (mode 0600).
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path


class ConfigError(Exception):
    """The account file exists but could not be read."""


@dataclass
class Account:
    username: str = ""
    password: str = ""
    remember_password: bool = True
    station_call: str = ""
    logbook_key: str = ""


def config_path() -> Path:
    override = os.environ.get("QRZTERM_CONFIG")
    if override:
        return Path(override)
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else Path.home() / ".config"
    return base / "qrzterm" / "config.json"


def station_callsign(account: Account) -> str:
    """Station call logged with each QSO. The QRZ username is the default."""
    call = account.station_call.strip().upper()
    return call or account.username.strip().upper()


def load_account(path: Path | None = None) -> Account:
    path = path or config_path()
    if not path.exists():
        return Account()
    try:
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw)
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigError(f"Couldn't read {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"Couldn't read {path}: expected an object.")
    remember = bool(data.get("remember_password", True))
    password = str(data.get("password", "")) if remember else ""
    return Account(
        username=str(data.get("username", "")).strip(),
        password=password,
        remember_password=remember,
        station_call=str(data.get("station_call", "")).strip().upper(),
        logbook_key=str(data.get("logbook_key", "")).strip(),
    )


def save_account(account: Account, path: Path | None = None) -> None:
    path = path or config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    payload = {
        "username": account.username.strip(),
        "remember_password": bool(account.remember_password),
        "station_call": account.station_call.strip().upper(),
        "logbook_key": account.logbook_key.strip(),
    }
    if account.remember_password:
        payload["password"] = account.password
    text = json.dumps(payload, indent=2) + "\n"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, text.encode("utf-8"))
    finally:
        os.close(fd)
    os.chmod(path, 0o600)
