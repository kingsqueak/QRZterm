"""QRZterm's terminal UI.

Lookup, the worked indicator, and Log Contact follow the QRZ tab in 590Ctrl.
Frequency, mode, and power are typed here; 590Ctrl takes those from the radio.
"""

from __future__ import annotations

import argparse
import io
import os
import webbrowser
from datetime import timezone

from PIL import Image as PILImage
from PIL import UnidentifiedImageError
from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.theme import Theme
from textual.widgets import Button, Checkbox, DataTable, Footer, Header, Input, Label, Select, Static

from qrzterm import __version__
from qrzterm.config import Account, ConfigError, load_account, save_account, station_callsign
from qrzterm.qrz import (
    MODE_NAMES,
    Client,
    LogInputError,
    LookupResponse,
    LookupSuperseded,
    QrzError,
    adif_band,
    adif_record,
    display_fields,
    field_display_value,
    field_label,
    normalize_url,
    parse_mhz,
    prepare_qso,
    report_for_mode_name,
    worked_summary,
)

OPEN_FIELDS = {"call", "email", "url", "image"}
BADGE_LABELS = {
    "idle": "—",
    "checking": "CHECKING",
    "worked": "WORKED",
    "miss": "NOT IN LOG",
    "failed": "FAILED",
    "nokey": "NO KEY",
}
BADGE_KINDS = tuple(BADGE_LABELS)

STATION_THEME = Theme(
    name="station",
    primary="#3b82f6",
    secondary="#2563eb",
    accent="#9ec9ff",
    warning="#e0a030",
    error="#e74c3c",
    success="#3dcc7a",
    foreground="#e8e4d8",
    background="#1a1d1a",
    surface="#242824",
    panel="#2a2e2a",
    dark=True,
)


class AccountScreen(ModalScreen[Account | None]):
    """Username, password, station callsign, and logbook key."""

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    DEFAULT_CSS = """
    AccountScreen {
        align: center middle;
    }
    #dialog {
        width: 68;
        height: auto;
        padding: 1 2;
        border: thick $primary;
        background: $surface;
    }
    #dialog Input {
        width: 100%;
    }
    #dialog-title {
        text-style: bold;
        color: $primary;
    }
    #note {
        color: $text-muted;
        margin: 1 0;
    }
    #dialog-buttons {
        height: auto;
        align: right middle;
    }
    #dialog-buttons Button {
        margin-left: 1;
    }
    """

    def __init__(self, account: Account) -> None:
        super().__init__()
        self._account = account

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Static("QRZ account", id="dialog-title", markup=False)
            yield Label("Username")
            yield Input(value=self._account.username, id="username")
            yield Label("Password")
            yield Input(value=self._account.password, password=True, id="password")
            yield Checkbox(
                "Remember password (saved unencrypted)",
                value=self._account.remember_password,
                id="remember",
            )
            yield Label("Station callsign")
            yield Input(
                value=self._account.station_call,
                placeholder="Same as the username",
                id="station",
            )
            yield Label("Logbook API key")
            yield Input(
                value=self._account.logbook_key,
                placeholder="Settings, then API, on your QRZ logbook",
                id="key",
                tooltip="Each station callsign has its own logbook and key. Logging through the API needs a QRZ subscription.",
            )
            yield Static(
                "Lookups use the QRZ XML service. Without a subscription QRZ returns only part of each record. "
                "Logging through the API needs a subscription.",
                id="note",
                markup=False,
            )
            with Horizontal(id="dialog-buttons"):
                yield Button("Save", id="save", variant="primary")
                yield Button("Cancel", id="cancel")

    def on_mount(self) -> None:
        self.query_one("#username", Input).focus()

    def action_cancel(self) -> None:
        self.dismiss(None)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel":
            event.stop()
            self.dismiss(None)
        elif event.button.id == "save":
            event.stop()
            self.dismiss(
                Account(
                    username=self.query_one("#username", Input).value.strip(),
                    password=self.query_one("#password", Input).value,
                    remember_password=self.query_one("#remember", Checkbox).value,
                    station_call=self.query_one("#station", Input).value.strip().upper(),
                    logbook_key=self.query_one("#key", Input).value.strip(),
                )
            )


class QrzApp(App[None]):
    TITLE = "QRZterm"
    CSS = """
    Screen {
        background: $background;
    }
    #lookup-row, #qso-row, #comment-row {
        height: 1;
        align: left middle;
        padding: 0 1;
    }
    #lookup-row Input.-textual-compact, #qso-row Input.-textual-compact, #comment-row Input.-textual-compact {
        padding: 0 1;
    }
    #lookup-row Input:focus, #qso-row Input:focus, #comment-row Input:focus {
        background: $primary;
        color: $text;
    }
    #mode:focus > SelectCurrent {
        background: $primary;
        color: $text;
    }
    #call {
        width: 20;
        text-style: bold;
    }
    #call.worked {
        color: #7dffb3;
        background: #1a3324;
    }
    #call.worked:focus {
        background: $primary;
        color: $text;
    }
    #badge {
        width: auto;
        min-width: 10;
        height: 1;
        content-align: center middle;
        text-style: bold;
        margin: 0 1;
        padding: 0 1;
        background: #242824;
        color: #8d8a80;
    }
    #badge.worked {
        background: #1e3d2c;
        color: #7dffb3;
    }
    #badge.checking, #badge.nokey {
        color: #e0a030;
    }
    #badge.miss {
        color: #b7b3a6;
    }
    #badge.failed {
        color: #e74c3c;
    }
    #lookup-row Button {
        margin-right: 1;
    }
    #mode {
        width: 16;
    }
    #freq, #rx-freq {
        width: 12;
    }
    #band {
        width: 8;
        padding: 0 1;
        color: $primary;
        text-style: bold;
    }
    #rst-sent, #rst-rcvd, #power {
        width: 8;
    }
    #comment {
        width: 1fr;
    }
    #qso-row Label, #comment-row Label {
        margin: 0 1 0 1;
        color: $text-muted;
    }
    #worked, #status {
        padding: 0 1;
        height: auto;
    }
    #worked.worked {
        color: #7dffb3;
        text-style: bold;
    }
    #worked.miss {
        color: #b7b3a6;
    }
    #worked.checking, #worked.nokey {
        color: #e0a030;
    }
    #worked.failed, #status.error {
        color: #e74c3c;
    }
    #status.error {
        text-style: bold;
    }
    #results {
        height: 1fr;
        padding: 0 1 1 1;
    }
    #fields {
        width: 1fr;
        height: 1fr;
    }
    #photo-pane {
        width: 36;
        height: 1fr;
        margin-left: 1;
        align: center top;
    }
    #photo {
        width: 34;
        height: auto;
    }
    #photo-placeholder {
        width: 100%;
        height: 8;
        content-align: center middle;
        color: $text-muted;
        border: round #3a4038;
    }
    #open {
        width: 100%;
        margin-top: 1;
    }
    """
    BINDINGS = [
        Binding("f2", "lookup", "Look up", priority=True),
        Binding("f5", "log", "Log", priority=True),
        Binding("f3", "account", "Account", priority=True),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(
        self,
        client: Client | None = None,
        account: Account | None = None,
        initial_call: str | None = None,
        config_path=None,
        load_warning: str = "",
        image_renderer: str = "auto",
    ) -> None:
        # Probe the terminal before Textual takes stdin. textual-image does
        # that when its widget module is imported.
        self._image_class = _image_widget(image_renderer)
        super().__init__()
        self.config_path = config_path
        self.load_warning = load_warning
        self.initial_call = (initial_call or "").strip().upper()
        self.image_renderer = image_renderer
        if client is None and account is None:
            try:
                self.account = load_account(config_path)
            except ConfigError as exc:
                self.account = Account()
                self.load_warning = self.load_warning or str(exc)
            self.client = Client()
            self.client.set_credentials(self.account.username, self.account.password)
            self.client.set_logbook_key(self.account.logbook_key)
        else:
            self.account = account or Account()
            self.client = client or Client()
        self._generation = 0
        self._log_token = 0
        self._shown_call = ""
        self._last_mode = "USB"
        self._normalizing = False
        self._last_lookup: LookupResponse | None = None
        self._field_values: dict[str, tuple[str, str]] = {}
        self._image_url = ""
        self._just_logged: set[str] = set()
        self.badge_kind = "idle"
        self.status_text = ""
        self.status_error = False

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal(id="lookup-row"):
            yield Input(
                placeholder="Callsign",
                id="call",
                max_length=20,
                restrict=r"[A-Za-z0-9/]*",
                compact=True,
                tooltip="Callsign to look up. Enter looks it up.",
            )
            yield Static(BADGE_LABELS["idle"], id="badge", classes="idle", markup=False)
            yield Button(
                "Look Up",
                id="lookup",
                variant="primary",
                compact=True,
                tooltip="Look up the callsign on QRZ.com",
            )
            yield Button(
                "Log Contact",
                id="log",
                variant="success",
                compact=True,
                tooltip="Add a QSO with this callsign to your QRZ logbook",
            )
            yield Button(
                "Account…",
                id="account",
                compact=True,
                tooltip="QRZ username, password, station callsign, and logbook key",
            )
        with Horizontal(id="qso-row"):
            yield Label("Mode")
            yield Select(
                [(name, name) for name in MODE_NAMES],
                value="USB",
                allow_blank=False,
                id="mode",
                compact=True,
                tooltip="USB and LSB are logged as SSB. FT4 is logged as MFSK, PSK31 as PSK.",
            )
            yield Label("MHz")
            yield Input(
                placeholder="14.195",
                id="freq",
                compact=True,
                tooltip="Megahertz, such as 14.195. A whole number of 1000 or more is kilohertz (14074).",
            )
            yield Static("", id="band", markup=False)
            yield Label("RST")
            yield Input(value="59", id="rst-sent", compact=True, tooltip="Report sent")
            yield Label("/")
            yield Input(value="59", id="rst-rcvd", compact=True, tooltip="Report received")
            yield Label("W")
            yield Input(placeholder="100", id="power", compact=True, tooltip="Transmit power in watts. Optional.")
        with Horizontal(id="comment-row"):
            yield Label("RX")
            yield Input(
                placeholder="split",
                id="rx-freq",
                compact=True,
                tooltip="Receive frequency in MHz when split. Leave blank otherwise.",
            )
            yield Input(placeholder="Comment (optional)", id="comment", compact=True)
        yield Static("", id="worked", classes="idle", markup=False)
        yield Static("", id="status", markup=False)
        with Horizontal(id="results"):
            yield DataTable(id="fields", cursor_type="row", zebra_stripes=True, show_row_labels=False)
            with Vertical(id="photo-pane"):
                yield Static("No photo", id="photo-placeholder", markup=False)
                photo = self._image_class(id="photo")
                photo.display = False
                yield photo
                yield Button("Open link", id="open", disabled=True, tooltip="Open the highlighted call, email, web page, or image")
        yield Footer()

    def on_mount(self) -> None:
        self.register_theme(STATION_THEME)
        self.theme = "station"
        self.sub_title = "QRZ.com"
        table = self.query_one("#fields", DataTable)
        table.add_column("Field", width=20, key="field")
        table.add_column("Value", key="value")
        if self.load_warning:
            self._set_status(self.load_warning, error=True)
        call = self.query_one("#call", Input)
        if self.initial_call:
            call.value = self.initial_call
            self.action_lookup()
        else:
            call.focus()

    async def on_unmount(self) -> None:
        close = getattr(self.client, "aclose", None)
        if close is not None:
            await close()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        action = {
            "lookup": self.action_lookup,
            "log": self.action_log,
            "account": self.action_account,
            "open": self.action_open_field,
        }.get(event.button.id or "")
        if action is not None:
            action()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "call":
            self.action_lookup()

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "freq":
            hz = parse_mhz(event.value)
            self.query_one("#band", Static).update(adif_band(hz) if hz else "")
            return
        if event.input.id != "call" or self._normalizing:
            return
        upper = event.value.upper()
        if upper != event.value:
            self._normalizing = True
            cursor = event.input.cursor_position
            event.input.value = upper
            event.input.cursor_position = min(cursor, len(upper))
            self._normalizing = False
            return
        if upper != self._shown_call:
            self._set_badge("idle", "")

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id != "mode" or not isinstance(event.value, str):
            return
        if not self.is_mounted:
            self._last_mode = event.value
            return
        new_report = report_for_mode_name(event.value)
        old_report = report_for_mode_name(self._last_mode)
        for widget_id in ("rst-sent", "rst-rcvd"):
            box = self.query_one(f"#{widget_id}", Input)
            if box.value.strip() in {"", old_report}:
                box.value = new_report
        self._last_mode = event.value

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        key = event.row_key.value or ""
        item = self._field_values.get(key)
        openable = bool(item and item[0] in OPEN_FIELDS and item[1])
        self.query_one("#open", Button).disabled = not openable

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        self._open_key(event.row_key.value or "")

    def action_account(self) -> None:
        if isinstance(self.screen, AccountScreen):
            return
        draft = Account(
            username=self.account.username,
            password=self.client.password,
            remember_password=self.account.remember_password,
            station_call=self.account.station_call,
            logbook_key=self.account.logbook_key,
        )
        self.push_screen(AccountScreen(draft), self._apply_account)

    def action_lookup(self) -> None:
        call = self._call()
        if not call:
            self._set_status("Enter a callsign.", error=True)
            return
        if isinstance(self.screen, AccountScreen):
            return
        if not self.client.has_credentials:
            self.push_screen(self._draft_screen(), self._lookup_after_account)
            return
        self._begin_lookup(call)

    def action_log(self) -> None:
        if isinstance(self.screen, AccountScreen):
            return
        if not self.client.has_logbook_key or not station_callsign(self.account):
            self.push_screen(self._draft_screen(), self._log_after_account)
            return
        self._start_log()

    def action_open_field(self) -> None:
        table = self.query_one("#fields", DataTable)
        if table.row_count == 0:
            return
        row_key, _column_key = table.coordinate_to_cell_key(table.cursor_coordinate)
        self._open_key(row_key.value or "")

    def _draft_screen(self) -> AccountScreen:
        return AccountScreen(
            Account(
                username=self.account.username,
                password=self.client.password,
                remember_password=self.account.remember_password,
                station_call=self.account.station_call,
                logbook_key=self.account.logbook_key,
            )
        )

    def _apply_account(self, account: Account | None) -> None:
        if account is None:
            return
        self.account = account
        self.client.set_credentials(account.username, account.password)
        self.client.set_logbook_key(account.logbook_key)
        try:
            save_account(account, self.config_path)
        except OSError as exc:
            self._set_status(f"Couldn't save the account: {exc}", error=True)

    def _lookup_after_account(self, account: Account | None) -> None:
        self._apply_account(account)
        if account is None or not self.client.has_credentials:
            return
        call = self._call()
        if call:
            self._begin_lookup(call)

    def _log_after_account(self, account: Account | None) -> None:
        self._apply_account(account)
        if account is None:
            return
        if not self.client.has_logbook_key or not station_callsign(self.account):
            self._set_status(
                "Logging needs your station callsign and QRZ Logbook API key (Account…).",
                error=True,
            )
            return
        self._start_log()

    def _call(self) -> str:
        return self.query_one("#call", Input).value.strip().upper()

    def _begin_lookup(self, call: str) -> None:
        self._generation += 1
        generation = self._generation
        self._shown_call = call
        self._clear_results()
        self.query_one("#lookup", Button).disabled = True
        self._set_status(f"Looking up {call}…")
        if self.client.has_logbook_key:
            self._set_badge("checking", "Checking your QRZ log…")
            self.run_worker(
                self._do_check(call, generation),
                group="logcheck",
                exclusive=True,
                exit_on_error=False,
            )
        else:
            self._set_badge("idle", "")
        self.run_worker(
            self._do_lookup(call, generation),
            group="lookup",
            exclusive=True,
            exit_on_error=False,
        )

    async def _do_lookup(self, call: str, generation: int) -> None:
        try:
            try:
                response = await self.client.lookup(call)
            except LookupSuperseded:
                return
            except QrzError as exc:
                if generation == self._generation:
                    self._set_status(f"{call}: {exc}", error=True)
                return
            if generation != self._generation:
                return
            self._show_lookup(response)
            if not self.client.has_logbook_key:
                self._set_badge(
                    "nokey",
                    "Add a Logbook API key under Account to see if you've worked this station.",
                )
        finally:
            if generation == self._generation and self.is_running:
                self.query_one("#lookup", Button).disabled = False

    async def _do_check(self, call: str, generation: int) -> None:
        try:
            result = await self.client.check_log(call)
        except QrzError as exc:
            if generation != self._generation or call != self._call():
                return
            if call in self._just_logged:
                self._set_badge("worked", f"Logged to QRZ. Log check failed: {exc}")
            else:
                self._set_badge("failed", f"Log check failed: {exc}")
            return
        if generation != self._generation or call != self._call():
            return
        worked, detail = worked_summary(result)
        if worked:
            self._set_badge("worked", detail)
        elif call in self._just_logged:
            self._set_badge("worked", "Logged to QRZ. Not listed in a log search yet.")
        else:
            self._set_badge("miss", detail)

    def _show_lookup(self, response: LookupResponse) -> None:
        self._last_lookup = response
        table = self.query_one("#fields", DataTable)
        table.clear()
        self._field_values.clear()
        seen: dict[str, int] = {}
        for item in display_fields(response):
            key = item.name
            seen[key] = seen.get(key, 0) + 1
            if seen[key] > 1:
                key = f"{item.name}#{seen[key]}"
            self._field_values[key] = (item.name, item.value)
            table.add_row(
                Text(field_label(item.name)),
                Text(field_display_value(item.name, item.value)),
                key=key,
            )
        parts = [f"{response.value('call')} found."]
        if response.message:
            parts.append(response.message)
        if response.lookup_count:
            parts.append(f"Lookups in the last 24 h: {response.lookup_count}")
        if response.subscription_expiry:
            parts.append(f"Subscription: {response.subscription_expiry}")
        self._set_status(" · ".join(parts))
        self.query_one("#open", Button).disabled = True
        image = response.value("image")
        if image:
            self._image_url = image
            self.run_worker(
                self._load_image(image, self._generation),
                group="image",
                exclusive=True,
                exit_on_error=False,
            )
        else:
            self._image_url = ""
            self._clear_photo("No photo")

    async def _load_image(self, url: str, generation: int) -> None:
        try:
            data = await self.client.fetch_image(url)
            image = PILImage.open(io.BytesIO(data))
            image.load()
        except (QrzError, OSError, UnidentifiedImageError):
            if generation == self._generation and url == self._image_url:
                self._clear_photo("Image didn't load")
            return
        if generation != self._generation or url != self._image_url:
            return
        photo = self.query_one("#photo")
        photo.image = image
        photo.display = True
        self.query_one("#photo-placeholder", Static).display = False

    def _start_log(self) -> None:
        mode = self.query_one("#mode", Select).value
        mode_name = mode if isinstance(mode, str) else ""
        try:
            qso = prepare_qso(
                call=self._call(),
                station_call=station_callsign(self.account),
                logbook_key=self.client.logbook_key,
                tx_mhz=self.query_one("#freq", Input).value,
                rx_mhz=self.query_one("#rx-freq", Input).value,
                mode_name=mode_name,
                rst_sent=self.query_one("#rst-sent", Input).value,
                rst_rcvd=self.query_one("#rst-rcvd", Input).value,
                power=self.query_one("#power", Input).value,
                comment=self.query_one("#comment", Input).value,
                lookup=self._last_lookup,
            )
        except LogInputError as exc:
            self._set_status(str(exc), error=True)
            return
        self._log_token += 1
        token = self._log_token
        self.query_one("#log", Button).disabled = True
        when = qso.time_on if qso.time_on.tzinfo else qso.time_on.replace(tzinfo=timezone.utc)
        when = when.astimezone(timezone.utc)
        self._set_status(
            f"Logging {qso.call} on {adif_band(qso.tx_hz)} {mode_name.upper()} at {when.strftime('%H:%M')} UTC…"
        )
        self.run_worker(self._do_log(qso, token), group="log", exclusive=True, exit_on_error=False)

    async def _do_log(self, qso, token: int) -> None:
        try:
            try:
                result = await self.client.insert_qso(adif_record(qso))
            except QrzError as exc:
                if token == self._log_token:
                    self._set_status(f"Not logged: {exc}", error=True)
                return
            if token != self._log_token:
                return
            if result.log_id:
                self._set_status(f"Logged to QRZ (log ID {result.log_id}).")
            else:
                self._set_status("Logged to QRZ.")
            self._just_logged.add(qso.call.upper())
            self._shown_call = qso.call.upper()
            self._set_badge("worked", "Logged just now")
            if self.client.has_logbook_key:
                self.run_worker(
                    self._do_check(qso.call.upper(), self._generation),
                    group="logcheck",
                    exclusive=True,
                    exit_on_error=False,
                )
        finally:
            if token == self._log_token and self.is_running:
                self.query_one("#log", Button).disabled = False

    def _clear_results(self) -> None:
        self._last_lookup = None
        self._image_url = ""
        self._field_values.clear()
        table = self.query_one("#fields", DataTable)
        table.clear()
        self._clear_photo("No photo")
        open_button = self.query_one("#open", Button)
        open_button.disabled = True

    def _clear_photo(self, message: str) -> None:
        photo = self.query_one("#photo")
        photo.image = None
        photo.display = False
        placeholder = self.query_one("#photo-placeholder", Static)
        placeholder.update(message)
        placeholder.display = True

    def _set_badge(self, kind: str, detail: str) -> None:
        self.badge_kind = kind
        badge = self.query_one("#badge", Static)
        badge.update(BADGE_LABELS[kind])
        detail_widget = self.query_one("#worked", Static)
        detail_widget.update(detail)
        for name in BADGE_KINDS:
            badge.set_class(name == kind, name)
            detail_widget.set_class(name == kind, name)
        self.query_one("#call", Input).set_class(kind == "worked", "worked")

    def _set_status(self, text: str, *, error: bool = False) -> None:
        self.status_text = text
        self.status_error = error
        widget = self.query_one("#status", Static)
        widget.update(text)
        widget.set_class(error, "error")

    def _open_key(self, key: str) -> None:
        item = self._field_values.get(key)
        if item is None:
            return
        name, value = item
        if name not in OPEN_FIELDS or not value:
            return
        if name == "call":
            from urllib.parse import quote

            target = "https://www.qrz.com/db/" + quote(value, safe="/")
        elif name == "email":
            target = "mailto:" + value
        else:
            target = normalize_url(value)
        if not webbrowser.open(target):
            self._set_status(f"Couldn't open {target}", error=True)


def _image_widget(renderer: str):
    import textual_image.widget as images

    return {
        "auto": images.Image,
        "halfcell": images.HalfcellImage,
        "unicode": images.UnicodeImage,
        "sixel": images.SixelImage,
        "tgp": images.TGPImage,
    }[renderer]


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Look up callsigns on QRZ.com, show the station photo, and log the QSO."
    )
    parser.add_argument("callsign", nargs="?", help="Callsign to look up on startup")
    parser.add_argument("--version", action="version", version=f"QRZterm {__version__}")
    parser.add_argument(
        "--image",
        choices=("auto", "halfcell", "unicode", "sixel", "tgp"),
        default=None,
        help="How to draw the QRZ photo. auto picks Sixel, Kitty graphics, or Unicode blocks. "
        "QRZTERM_IMAGE sets the default.",
    )
    args = parser.parse_args(argv)
    renderer = args.image or os.environ.get("QRZTERM_IMAGE", "auto")
    if renderer not in {"auto", "halfcell", "unicode", "sixel", "tgp"}:
        parser.error(f"unknown image renderer {renderer!r}")
    try:
        account = load_account()
        warning = ""
    except ConfigError as exc:
        account = Account()
        warning = str(exc)
    client = Client()
    client.set_credentials(account.username, account.password)
    client.set_logbook_key(account.logbook_key)
    app = QrzApp(
        client=client,
        account=account,
        initial_call=args.callsign,
        load_warning=warning,
        image_renderer=renderer,
    )
    app.run()
