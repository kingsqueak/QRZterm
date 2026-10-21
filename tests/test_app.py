import asyncio
import functools
import io

from PIL import Image as PILImage

from qrzterm.app import QrzApp, main
from qrzterm.config import Account, load_account
from qrzterm.qrz import Client, FetchResult, Field, LogResult, LookupResponse, QrzError


def _png() -> bytes:
    buffer = io.BytesIO()
    PILImage.new("RGB", (12, 16), (180, 48, 36)).save(buffer, format="PNG")
    return buffer.getvalue()


class FakeClient(Client):
    def __init__(self) -> None:
        super().__init__()
        self.username = "KC2RGW"
        self.password = "secret"
        self.logbook_key = "logkey"
        self.lookup_result = LookupResponse(
            callsign=[
                Field("call", "AA7BQ"),
                Field("fname", "Fred"),
                Field("name", "Lloyd"),
                Field("addr2", "Phoenix"),
                Field("state", "AZ"),
                Field("grid", "DM32af"),
                Field("lotw", "1"),
                Field("image", "https://cdn.example/aa7bq.jpg"),
            ],
            key="session",
            lookup_count="4",
        )
        self.fetch_result = FetchResult(
            result="OK",
            count=2,
            records=[
                {
                    "call": "AA7BQ",
                    "qso_date": "20250101",
                    "time_on": "1200",
                    "band": "20m",
                    "mode": "SSB",
                },
                {
                    "call": "AA7BQ",
                    "qso_date": "20240101",
                    "time_on": "080000",
                    "band": "40m",
                    "mode": "CW",
                },
            ],
        )
        self.image_bytes = _png()
        self.inserted: list[bytes] = []
        self.fail_lookup: QrzError | None = None

    async def lookup(self, callsign: str) -> LookupResponse:
        if self.fail_lookup is not None:
            raise self.fail_lookup
        assert callsign == "AA7BQ"
        return self.lookup_result

    async def check_log(self, callsign: str) -> FetchResult:
        assert callsign == "AA7BQ"
        return self.fetch_result

    async def insert_qso(self, adif: bytes) -> LogResult:
        self.inserted.append(adif)
        return LogResult(result="OK", log_id="99")

    async def fetch_image(self, url: str) -> bytes:
        assert url == "https://cdn.example/aa7bq.jpg"
        return self.image_bytes


def _app(fake: FakeClient, **kwargs) -> QrzApp:
    account = Account(username="KC2RGW", password="secret", station_call="KC2RGW", logbook_key="logkey")
    return QrzApp(client=fake, account=account, image_renderer="unicode", **kwargs)


async def _settle(pilot, predicate, steps: int = 40) -> None:
    for _ in range(steps):
        if predicate():
            return
        await pilot.pause()
    raise AssertionError("the screen did not settle")


def _drive(fn):
    """Run a Textual pilot test on a fresh loop. pytest-asyncio is not a dependency."""

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        return asyncio.run(fn(*args, **kwargs))

    return wrapper


@_drive
async def test_lookup_shows_fields_photo_and_worked_badge():
    fake = FakeClient()
    app = _app(fake)
    async with app.run_test(size=(160, 42)) as pilot:
        app.query_one("#call").value = "aa7bq"
        await pilot.pause()
        await pilot.click("#lookup")
        await _settle(pilot, lambda: app.badge_kind == "worked" and app.query_one("#photo").display)
        assert "AA7BQ found." in app.status_text
        assert "Lookups in the last 24 h: 4" in app.status_text
        rows = []
        for table_id in ("#fields", "#fields-2"):
            table = app.query_one(table_id)
            rows += [tuple(str(cell) for cell in table.get_row_at(index)) for index in range(table.row_count)]
        assert rows[0] == ("Callsign", "AA7BQ")
        assert ("Name", "Fred Lloyd") in rows
        assert ("LoTW", "Yes") in rows
        assert ("Grid", "DM32af") in rows
        assert not any(label in {"First name", "Last name"} for label, _value in rows)
        assert app.query_one("#fields-2").row_count > 0
        banner = app.query_one("#banner")
        assert "empty" not in banner.classes
        assert "Phoenix, AZ" in "".join(segment.text for segment in app.console.render(banner.content))
        assert app.query_one("#badge").content == "WORKED"
        assert "Last worked 2025-01-01 12:00 UTC" in str(app.query_one("#worked").content)
        assert "2 QSOs in your log" in str(app.query_one("#worked").content)
        assert "worked" in app.query_one("#call").classes
        app.query_one("#call").value = "W1AW"
        await pilot.pause()
        assert app.badge_kind == "idle"


@_drive
async def test_not_in_the_log_and_a_failed_lookup():
    fake = FakeClient()
    fake.fetch_result = FetchResult(result="FAIL", reason="no log entries found", count=0, records=[])
    app = _app(fake)
    async with app.run_test(size=(160, 42)) as pilot:
        app.query_one("#call").value = "AA7BQ"
        await pilot.pause()
        app.action_lookup()
        await _settle(pilot, lambda: app.badge_kind == "miss")
        assert app.query_one("#badge").content == "NOT IN LOG"
        assert str(app.query_one("#worked").content) == "Not in your QRZ log"

        fake.fail_lookup = QrzError("Not found: AA7BQ", "AA7BQ")
        app.action_lookup()
        await _settle(pilot, lambda: app.status_error and "Not found" in app.status_text)
        assert "AA7BQ: Not found: AA7BQ" == app.status_text


@_drive
async def test_log_contact_sends_the_qso():
    fake = FakeClient()
    app = _app(fake)
    async with app.run_test(size=(160, 42)) as pilot:
        app.query_one("#call").value = "AA7BQ"
        app.query_one("#freq").value = "14.195"
        await pilot.pause()
        app.action_lookup()
        await _settle(pilot, lambda: app.badge_kind == "worked")
        app.action_log()
        await _settle(pilot, lambda: bool(fake.inserted) and "Logged to QRZ" in app.status_text)
        record = fake.inserted[0]
        assert b"<call:5>AA7BQ" in record
        assert b"<band:3>20m" in record
        assert b"<mode:3>SSB" in record
        assert b"<submode:3>USB" in record
        assert b"<station_callsign:6>KC2RGW" in record
        assert b"<gridsquare:6>DM32af" in record
        assert b"<name:10>Fred Lloyd" in record
        assert "log ID 99" in app.status_text
        assert app.badge_kind == "worked"


@_drive
async def test_account_screen_saves_a_private_file(tmp_path):
    fake = FakeClient()
    path = tmp_path / "config.json"
    app = _app(fake, config_path=path)
    async with app.run_test(size=(160, 42)) as pilot:
        await pilot.click("#account")
        await pilot.pause()
        screen = app.screen
        screen.query_one("#username").value = "KC2RGW"
        screen.query_one("#password").value = "new-secret"
        screen.query_one("#station").value = "kc2rgw"
        screen.query_one("#key").value = "abc123"
        screen.query_one("#grid").value = "fn43CB"
        await pilot.click("#save")
        await _settle(pilot, lambda: path.exists())
        loaded = load_account(path)
        assert loaded.username == "KC2RGW"
        assert loaded.password == "new-secret"
        assert loaded.station_call == "KC2RGW"
        assert loaded.logbook_key == "abc123"
        assert loaded.grid == "FN43cb"
        assert fake.logbook_key == "abc123"


@_drive
async def test_without_a_logbook_key_the_badge_says_so():
    fake = FakeClient()
    fake.logbook_key = ""
    account = Account(username="KC2RGW", password="secret", station_call="KC2RGW", logbook_key="")
    app = QrzApp(client=fake, account=account, image_renderer="unicode")
    async with app.run_test(size=(160, 42)) as pilot:
        app.query_one("#call").value = "AA7BQ"
        await pilot.pause()
        app.action_lookup()
        await _settle(pilot, lambda: app.badge_kind == "nokey")
        assert "Logbook API key" in str(app.query_one("#worked").content)
        assert app.query_one("#photo").display


def test_version_does_not_start_the_ui():
    import pytest

    with pytest.raises(SystemExit) as caught:
        main(["--version"])
    assert caught.value.code == 0
