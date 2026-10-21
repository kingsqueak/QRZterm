import stat

import pytest

from qrzterm.config import Account, ConfigError, load_account, save_account, station_callsign


def test_round_trip_and_private_mode(tmp_path):
    path = tmp_path / "qrzterm" / "config.json"
    save_account(
        Account(
            username="kc2rgw",
            password="secret",
            remember_password=True,
            station_call="kc2rgw",
            logbook_key="abc",
            grid="FN43cb",
        ),
        path,
    )
    mode = stat.S_IMODE(path.stat().st_mode)
    assert mode == 0o600
    loaded = load_account(path)
    assert loaded.username == "kc2rgw"
    assert loaded.password == "secret"
    assert loaded.station_call == "KC2RGW"
    assert loaded.logbook_key == "abc"
    assert loaded.grid == "FN43cb"
    assert station_callsign(loaded) == "KC2RGW"


def test_forgotten_password_is_not_written(tmp_path):
    path = tmp_path / "config.json"
    save_account(
        Account(username="kc2rgw", password="secret", remember_password=False, station_call=""),
        path,
    )
    text = path.read_text(encoding="utf-8")
    assert "secret" not in text
    loaded = load_account(path)
    assert loaded.password == ""
    assert station_callsign(loaded) == "KC2RGW"


def test_corrupt_file_raises(tmp_path):
    path = tmp_path / "config.json"
    path.write_text("{", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_account(path)


def test_missing_file_is_an_empty_account(tmp_path):
    loaded = load_account(tmp_path / "missing.json")
    assert loaded.username == ""
    assert loaded.password == ""
