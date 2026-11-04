import asyncio

import pytest
from websockets.asyncio.server import serve

from qrzterm.app import QrzApp, RadioScreen, AccountScreen
from qrzterm.config import Account, load_account
from qrzterm.qrz import Client
from qrzterm.tci import AddressError, Reading, TciState, form_mode, listen, parse_address
from textual.widgets import Input, Select, Static

# One frame per command, as Zeus sends them.
ZEUS_GREETING = (
    "protocol:ExpertSDR3,2.0;", "device:Zeus;", "trx_count:2;",
    "modulations_list:AM,SAM,DSB,LSB,USB,CWL,CWU,FM,DIGL,DIGU,SPEC,DRM;",
    "vfo:0,0,7222000;", "vfo:0,1,7222000;", "vfo:1,0,9500000;", "vfo:1,1,9500000;",
    "modulation:0,LSB;", "modulation:1,LSB;", "split_enable:0,false;",
    "tx_frequency:7222000;", "tx_frequency_ex:7222000,b40m,false,false;", "start;", "ready;",
)


def test_zeus_greeting_is_one_reading_and_repeats_are_quiet():
    state = TciState()
    readings = [r for r in map(state.feed, ZEUS_GREETING) if r]
    assert readings == [Reading(7_222_000, 0, "LSB")]
    assert state.device == "Zeus"
    assert state.feed("modulation:0,LSB;") is None
    assert state.feed("rx_smeter:0,2,-99;") is None
    assert state.feed("tx_power:0.0;") is None


def test_second_receiver_is_ignored():
    state = TciState()
    state.feed("".join(ZEUS_GREETING))
    assert state.feed("vfo:1,0,14074000;modulation:1,DIGU;") is None


def test_server_that_sends_several_commands_in_a_frame_or_split_across_frames():
    state = TciState()
    assert state.feed("VFO:0,0,14070000;MODULATION:0,digu;rea") is None
    assert state.feed("dy;") == Reading(14_070_000, 0, "DIGU")


def test_split_logs_tx_frequency_and_rx_vfo():
    state = TciState()
    state.feed("".join(ZEUS_GREETING))
    assert state.feed("tx_frequency:7215000;split_enable:0,true;") == Reading(7_215_000, 7_222_000, "LSB")
    assert state.feed("split_enable:0,false;") == Reading(7_222_000, 0, "LSB")


def test_split_without_tx_frequency_uses_vfo_b():
    state = TciState()
    state.feed("vfo:0,0,14200000;vfo:0,1,14210000;split_enable:0,true;modulation:0,USB;ready;")
    assert state.reading() == Reading(14_210_000, 14_200_000, "USB")


def test_garbage_is_ignored():
    state = TciState()
    state.feed("".join(ZEUS_GREETING))
    assert state.feed("vfo:0,0,abc;vfo:0;tx_frequency:;modulation:x,USB;") is None


@pytest.mark.parametrize(
    "text, expected",
    [
        ("localhost:50001", ("localhost", 50001)),
        ("192.168.16.5", ("192.168.16.5", 40001)),
        ("[::1]:50001", ("::1", 50001)),
        ("::1", ("::1", 40001)),
    ],
)
def test_parse_address(text, expected):
    assert parse_address(text) == expected


@pytest.mark.parametrize("text", ["", ":50001", "host:abc", "host:0", "host:70000"])
def test_parse_address_rejects(text):
    with pytest.raises(AddressError):
        parse_address(text)


@pytest.mark.parametrize(
    "modulation, expected",
    [
        ("LSB", "LSB"), ("USB", "USB"), ("CWL", "CW"), ("CWU", "CW"), ("cw", "CW"),
        ("SAM", "AM"), ("NFM", "FM"), ("SPEC", None), ("DRM", None), ("DSB", None), ("WFM", None),
    ],
)
def test_form_mode(modulation, expected):
    assert form_mode(modulation, "USB", "FT8") == expected


def test_digital_keeps_a_digital_pick_and_otherwise_uses_the_last_one():
    assert form_mode("DIGU", "RTTY", "FT8") == "RTTY"
    assert form_mode("DIGU", "USB", "FT4") == "FT4"
    assert form_mode("DIGL", "CW", "FT8") == "FT8"


class FakeRadio:
    """A TCI server that records anything a client sends it."""

    def __init__(self) -> None:
        self.sockets = set()
        self.received: list = []
        self.server = None

    async def __aenter__(self):
        self.server = await serve(self._handle, "127.0.0.1", 0, compression=None)
        self.port = self.server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *exc):
        self.server.close()
        await self.server.wait_closed()

    async def _handle(self, socket):
        self.sockets.add(socket)
        for frame in ZEUS_GREETING:
            await socket.send(frame)
        try:
            async for message in socket:
                self.received.append(message)
        finally:
            self.sockets.discard(socket)

    async def send(self, text: str) -> None:
        for socket in list(self.sockets):
            await socket.send(text)


def _app(port: int, tmp_path) -> QrzApp:
    account = Account(
        username="KC2RGW", password="secret", logbook_key="k",
        tci_enabled=True, tci_address=f"127.0.0.1:{port}",
    )
    return QrzApp(account=account, config_path=tmp_path / "config.json", image_renderer="unicode")


async def _until(pilot, predicate) -> None:
    for _ in range(200):
        if predicate():
            return
        await pilot.pause(0.01)
    raise AssertionError("the screen did not settle")


def _value(app, name) -> str:
    return app.screen_stack[0].query_one(name, Input).value


def _mode(app) -> str:
    return app.screen_stack[0].query_one("#mode", Select).value


def test_form_follows_the_radio_and_the_client_sends_nothing(tmp_path):
    async def run():
        async with FakeRadio() as radio:
            app = _app(radio.port, tmp_path)
            async with app.run_test(size=(120, 42)) as pilot:
                await _until(pilot, lambda: _value(app, "#freq") == "7.222000")
                assert _mode(app) == "LSB"
                assert str(app.query_one("#band", Static).content) == "40m"
                assert "Zeus" in str(app.query_one("#radio", Static).content)

                await radio.send("vfo:0,0,14074000;")
                await radio.send("modulation:0,DIGU;")
                await _until(pilot, lambda: _mode(app) == "FT8")
                assert _value(app, "#freq") == "14.074000"
                assert str(app.query_one("#band", Static).content) == "20m"

                await radio.send("modulation:0,CWU;")
                await _until(pilot, lambda: _mode(app) == "CW")
                await _until(pilot, lambda: _value(app, "#rst-sent") == "599")

                await radio.send("tx_frequency:14080000;")
                await radio.send("split_enable:0,true;")
                await _until(pilot, lambda: _value(app, "#rx-freq") == "14.074000")
                assert _value(app, "#freq") == "14.080000"
            assert radio.received == []

    asyncio.run(run())


def test_hand_edits_stand_until_the_radio_changes(tmp_path):
    async def run():
        async with FakeRadio() as radio:
            app = _app(radio.port, tmp_path)
            async with app.run_test(size=(120, 42)) as pilot:
                await _until(pilot, lambda: _mode(app) == "LSB")
                app.query_one("#mode", Select).value = "SSB"
                await pilot.pause()
                await radio.send("modulation:0,LSB;")
                await radio.send("modulation:0,LSB;")
                await asyncio.sleep(0.05)
                await pilot.pause()
                assert _mode(app) == "SSB"
                await radio.send("modulation:0,USB;")
                await _until(pilot, lambda: _mode(app) == "USB")

    asyncio.run(run())


def test_frequency_box_waits_while_it_has_focus(tmp_path):
    async def run():
        async with FakeRadio() as radio:
            app = _app(radio.port, tmp_path)
            async with app.run_test(size=(120, 42)) as pilot:
                await _until(pilot, lambda: _value(app, "#freq") == "7.222000")
                app.query_one("#freq", Input).focus()
                await pilot.pause()
                await radio.send("vfo:0,0,7100000;")
                await asyncio.sleep(0.05)
                await pilot.pause()
                assert _value(app, "#freq") == "7.222000"
                app.query_one("#call", Input).focus()
                await _until(pilot, lambda: _value(app, "#freq") == "7.100000")

    asyncio.run(run())


def test_radio_changes_during_a_dialog_arrive_after_it_closes(tmp_path):
    async def run():
        async with FakeRadio() as radio:
            app = _app(radio.port, tmp_path)
            async with app.run_test(size=(120, 42)) as pilot:
                await _until(pilot, lambda: _value(app, "#freq") == "7.222000")
                await pilot.press("f3")
                await _until(pilot, lambda: isinstance(app.screen, AccountScreen))
                await radio.send("vfo:0,0,21074000;")
                await radio.send("modulation:0,DIGU;")
                await asyncio.sleep(0.05)
                await pilot.pause()
                await pilot.press("f2")  # Must do nothing behind a dialog.
                await pilot.press("escape")
                await _until(pilot, lambda: _value(app, "#freq") == "21.074000")
                await _until(pilot, lambda: _mode(app) == "FT8")

    asyncio.run(run())


def test_link_loss_keeps_the_form_and_reconnects(tmp_path):
    async def run():
        async with FakeRadio() as radio:
            links = []
            readings = []
            task = asyncio.create_task(
                listen("127.0.0.1", radio.port, readings.append, lambda up, d: links.append(up), retry=0.01)
            )
            for _ in range(200):
                if radio.sockets:
                    break
                await asyncio.sleep(0.01)
            await asyncio.sleep(0.05)
            for socket in list(radio.sockets):
                await socket.close()
            for _ in range(200):
                if links[:3] == [True, False, True]:
                    break
                await asyncio.sleep(0.01)
            task.cancel()
            assert links[:3] == [True, False, True]
            assert readings[0] == Reading(7_222_000, 0, "LSB")

    asyncio.run(run())


def test_radio_setup_saves_and_account_dialog_keeps_it(tmp_path):
    async def run():
        path = tmp_path / "config.json"
        client = Client()
        client.set_credentials("KC2RGW", "secret")
        account = Account(username="KC2RGW", password="secret")
        app = QrzApp(client=client, account=account, config_path=path, image_renderer="unicode")
        async with app.run_test(size=(120, 42)) as pilot:
            assert str(app.query_one("#radio", Static).content) == "TCI off"
            await pilot.press("f4")
            await _until(pilot, lambda: isinstance(app.screen, RadioScreen))
            app.screen.query_one("#tci-enabled").value = True
            app.screen.query_one("#tci-address", Input).value = "127.0.0.1:1"
            await pilot.click("#radio-save")
            await _until(pilot, lambda: not isinstance(app.screen, RadioScreen))
            await pilot.press("f3")
            await _until(pilot, lambda: isinstance(app.screen, AccountScreen))
            await pilot.click("#save")
            await _until(pilot, lambda: not isinstance(app.screen, AccountScreen))
            await _until(pilot, lambda: "TCI" in str(app.query_one("#radio", Static).content))
            assert "off" not in str(app.query_one("#radio", Static).content)
        saved = load_account(path)
        assert saved.tci_enabled and saved.tci_address == "127.0.0.1:1"
        assert saved.password == "secret"

    asyncio.run(run())


def test_link_label_fits_an_80_column_terminal(tmp_path):
    async def run():
        async with FakeRadio() as radio:
            app = _app(radio.port, tmp_path)
            async with app.run_test(size=(80, 24)) as pilot:
                await _until(pilot, lambda: "Zeus" in str(app.query_one("#radio", Static).content))
                await pilot.pause()
                region = app.query_one("#radio", Static).region
                assert region.width >= len("TCI Zeus") and region.right <= 80

    asyncio.run(run())


def test_radio_setup_refuses_a_bad_port(tmp_path):
    async def run():
        app = QrzApp(account=Account(), config_path=tmp_path / "c.json", image_renderer="unicode")
        async with app.run_test(size=(120, 42)) as pilot:
            await pilot.press("f4")
            await _until(pilot, lambda: isinstance(app.screen, RadioScreen))
            app.screen.query_one("#tci-address", Input).value = "localhost:notaport"
            await pilot.click("#radio-save")
            await pilot.pause()
            assert isinstance(app.screen, RadioScreen)

    asyncio.run(run())
