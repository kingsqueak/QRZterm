"""QRZterm's terminal UI.

Lookup, the worked indicator, and Log Contact follow the QRZ tab in 590Ctrl.
Frequency and mode are typed, or follow the radio over TCI. Power is typed.
"""

from __future__ import annotations

import argparse
import io
import os
import webbrowser
from dataclasses import replace
from datetime import timezone

from PIL import Image as PILImage
from PIL import UnidentifiedImageError
from rich.console import Group
from rich.table import Table
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
    field_display_value,
    field_label,
    format_mhz,
    full_name,
    grouped_fields,
    local_time,
    normalize_url,
    parse_mhz,
    path_from,
    prepare_qso,
    qth_line,
    report_for_mode_name,
    split_groups,
    worked_summary,
)
from qrzterm.tci import (
    DEFAULT_PORT,
    AddressError,
    Reading,
    form_mode,
    is_digital_name,
    listen,
    parse_address,
)

DEFAULT_TCI_ADDRESS = f"localhost:{DEFAULT_PORT}"
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
YES_NO_FIELDS = {"lotw", "eqsl", "mqsl", "DST"}
LABEL_STYLE = "#9a978c"
LINK_STYLE = "underline #9ec9ff"
TABLE_IDS = ("fields", "fields-2")

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
            yield Label("Station grid")
            yield Input(
                value=self._account.grid,
                placeholder="Such as FN43cb. Shows distance and heading.",
                id="grid",
                max_length=8,
                restrict=r"[A-Za-z0-9]*",
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
                replace(
                    self._account,
                    username=self.query_one("#username", Input).value.strip(),
                    password=self.query_one("#password", Input).value,
                    remember_password=self.query_one("#remember", Checkbox).value,
                    station_call=self.query_one("#station", Input).value.strip().upper(),
                    logbook_key=self.query_one("#key", Input).value.strip(),
                    grid=_normalize_grid(self.query_one("#grid", Input).value),
                )
            )


class RadioScreen(ModalScreen[Account | None]):
    """Whether to follow the radio over TCI, and where it is."""

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    DEFAULT_CSS = """
    RadioScreen {
        align: center middle;
    }
    #radio-dialog {
        width: 60;
        height: auto;
        padding: 1 2;
        border: thick $primary;
        background: $surface;
    }
    #radio-dialog Input {
        width: 100%;
    }
    #radio-title {
        text-style: bold;
        color: $primary;
    }
    #radio-note {
        color: $text-muted;
        margin: 1 0;
    }
    #radio-note.error {
        color: $error;
    }
    #radio-buttons {
        height: auto;
        align: right middle;
    }
    #radio-buttons Button {
        margin-left: 1;
    }
    """

    NOTE = "Fills in the frequency and mode. QRZterm only listens, so it can't tune or key the radio."

    def __init__(self, account: Account) -> None:
        super().__init__()
        self._account = account

    def compose(self) -> ComposeResult:
        with Vertical(id="radio-dialog"):
            yield Static("Radio (TCI)", id="radio-title", markup=False)
            yield Checkbox("Follow the radio", value=self._account.tci_enabled, id="tci-enabled")
            yield Label("TCI address")
            yield Input(
                value=self._account.tci_address,
                placeholder=DEFAULT_TCI_ADDRESS,
                id="tci-address",
            )
            yield Static(self.NOTE, id="radio-note", markup=False)
            with Horizontal(id="radio-buttons"):
                yield Button("Save", id="radio-save", variant="primary")
                yield Button("Cancel", id="radio-cancel")

    def on_mount(self) -> None:
        self.query_one("#tci-address", Input).focus()

    def action_cancel(self) -> None:
        self.dismiss(None)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "radio-cancel":
            event.stop()
            self.dismiss(None)
        elif event.button.id == "radio-save":
            event.stop()
            enabled = self.query_one("#tci-enabled", Checkbox).value
            address = self.query_one("#tci-address", Input).value.strip()
            note = self.query_one("#radio-note", Static)
            try:
                if enabled or address:
                    parse_address(address or DEFAULT_TCI_ADDRESS)
            except AddressError as exc:
                note.update(str(exc))
                note.add_class("error")
                return
            self.dismiss(replace(self._account, tci_enabled=enabled, tci_address=address))


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
    #radio {
        width: auto;
        height: 1;
        padding: 0 1;
        color: #8d8a80;
    }
    #radio.up {
        color: #7dffb3;
    }
    #radio.down {
        color: #e0a030;
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
    #summary-row {
        height: auto;
    }
    #banner {
        width: 1fr;
        height: auto;
        min-height: 12;
        padding: 0 1;
        border: round $primary 60%;
    }
    #banner.empty {
        color: $text-muted;
        border: round #3a4038;
        content-align: center middle;
    }
    #photo-pane {
        width: 36;
        height: auto;
        margin-left: 1;
        align: center top;
    }
    #photo {
        width: 34;
        height: 10;
    }
    #photo-placeholder {
        width: 100%;
        height: 10;
        content-align: center middle;
        color: $text-muted;
        border: round #3a4038;
    }
    #open {
        width: 100%;
        margin-top: 1;
    }
    #tables {
        height: 1fr;
        margin-top: 1;
    }
    #tables DataTable {
        width: 1fr;
        height: 1fr;
    }
    #fields {
        margin-right: 1;
    }
    """
    BINDINGS = [
        Binding("f2", "lookup", "Look up", priority=True),
        Binding("f5", "log", "Log", priority=True),
        Binding("f3", "account", "Account", priority=True),
        Binding("f4", "radio", "Radio", priority=True),
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
        self._active_table = TABLE_IDS[0]
        self._image_url = ""
        self._just_logged: set[str] = set()
        self._radio: Reading | None = None
        self._radio_shown: dict[str, str] = {}
        self._last_digital = "FT8"
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
            yield Button(
                "Radio…",
                id="radio-setup",
                compact=True,
                tooltip="Follow the radio's frequency and mode over TCI",
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
            yield Static("", id="radio", markup=False)
        yield Static("", id="worked", classes="idle", markup=False)
        yield Static("", id="status", markup=False)
        with Vertical(id="results"):
            with Horizontal(id="summary-row"):
                yield Static(EMPTY_BANNER, id="banner", classes="empty", markup=False)
                with Vertical(id="photo-pane"):
                    yield Static("No photo", id="photo-placeholder", markup=False)
                    photo = self._image_class(id="photo")
                    photo.display = False
                    yield photo
                    yield Button("Open link", id="open", disabled=True, compact=True, tooltip="Open the highlighted call, email, web page, or image")
            with Horizontal(id="tables"):
                for table_id in TABLE_IDS:
                    yield DataTable(id=table_id, cursor_type="row", zebra_stripes=True, show_row_labels=False)
        yield Footer()

    def on_mount(self) -> None:
        self.register_theme(STATION_THEME)
        self.theme = "station"
        self.sub_title = "QRZ.com"
        for table in self._tables():
            table.add_column("Field", width=18, key="field")
            table.add_column("Value", key="value")
        if self.load_warning:
            self._set_status(self.load_warning, error=True)
        self._start_radio()
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
            "radio-setup": self.action_radio,
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
        if is_digital_name(event.value):
            self._last_digital = event.value
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
        if event.data_table.id == self._active_table:
            self._sync_open(event.data_table)

    def on_descendant_focus(self, event) -> None:
        # Open link follows whichever field table was used last, so clicking
        # the button (which takes focus) still opens that table's row.
        if isinstance(event.widget, DataTable) and event.widget.id in TABLE_IDS:
            self._active_table = event.widget.id
            self._sync_open(event.widget)

    def on_descendant_blur(self, event) -> None:
        # A frequency box isn't changed while it's being typed in.
        if getattr(event.widget, "id", None) in {"freq", "rx-freq"}:
            self._apply_radio()

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        self._open_key(event.row_key.value or "")

    def action_account(self) -> None:
        if isinstance(self.screen, ModalScreen):
            return
        self.push_screen(self._draft_screen(), self._apply_account)

    def action_radio(self) -> None:
        if isinstance(self.screen, ModalScreen):
            return
        self.push_screen(RadioScreen(self.account), self._apply_radio_setup)

    def action_lookup(self) -> None:
        if isinstance(self.screen, ModalScreen):
            return
        call = self._call()
        if not call:
            self._set_status("Enter a callsign.", error=True)
            return
        if not self.client.has_credentials:
            self.push_screen(self._draft_screen(), self._lookup_after_account)
            return
        self._begin_lookup(call)

    def action_log(self) -> None:
        if isinstance(self.screen, ModalScreen):
            return
        if not self.client.has_logbook_key or not station_callsign(self.account):
            self.push_screen(self._draft_screen(), self._log_after_account)
            return
        self._start_log()

    def action_open_field(self) -> None:
        table = self.query_one(f"#{self._active_table}", DataTable)
        if table.row_count == 0:
            return
        row_key, _column_key = table.coordinate_to_cell_key(table.cursor_coordinate)
        self._open_key(row_key.value or "")

    def _draft_screen(self) -> AccountScreen:
        return AccountScreen(replace(self.account, password=self.client.password))

    def _apply_account(self, account: Account | None) -> None:
        self.call_after_refresh(self._apply_radio)
        if account is None:
            return
        self.account = account
        self.client.set_credentials(account.username, account.password)
        self.client.set_logbook_key(account.logbook_key)
        try:
            save_account(account, self.config_path)
        except OSError as exc:
            self._set_status(f"Couldn't save the account: {exc}", error=True)

    def _apply_radio_setup(self, account: Account | None) -> None:
        if account is None:
            self.call_after_refresh(self._apply_radio)
            return
        account = replace(account, password=self.client.password)
        self.account = account
        try:
            save_account(account, self.config_path)
        except OSError as exc:
            self._set_status(f"Couldn't save the radio settings: {exc}", error=True)
        self.call_after_refresh(self._start_radio)

    def _start_radio(self) -> None:
        self.workers.cancel_group(self, "tci")
        self._radio = None
        self._radio_shown = {}
        if not self.account.tci_enabled:
            self._set_radio_link("off", "TCI off")
            return
        try:
            host, port = parse_address(self.account.tci_address or DEFAULT_TCI_ADDRESS)
        except AddressError as exc:
            self._set_radio_link("down", "TCI bad address")
            self._set_status(str(exc), error=True)
            return
        self._set_radio_link("down", "TCI connecting")
        self.run_worker(
            listen(host, port, self._on_radio_reading, self._on_radio_link),
            group="tci",
            exclusive=True,
            exit_on_error=False,
        )

    def _on_radio_link(self, up: bool, detail: str) -> None:
        if up:
            self._set_radio_link("up", f"TCI {detail}".strip())
        else:
            self._set_radio_link("down", "TCI no link")

    def _set_radio_link(self, kind: str, text: str) -> None:
        # The label is on the main screen, which a dialog may be covering.
        label = self.screen_stack[0].query_one("#radio", Static)
        label.update(text)
        label.set_class(kind == "up", "up")
        label.set_class(kind == "down", "down")

    def _on_radio_reading(self, reading: Reading) -> None:
        self._radio = reading
        self._apply_radio()

    def _apply_radio(self) -> None:
        """Copy what changed on the radio into the form.

        Only a change is copied, because some servers repeat their state
        constantly and a hand edit has to stand until the radio moves. With a
        dialog open this waits until the dialog closes.
        """
        reading = self._radio
        if reading is None or len(self.screen_stack) > 1:
            return
        self._set_radio_box("freq", format_mhz(reading.tx_hz) if reading.tx_hz else "")
        self._set_radio_box("rx-freq", format_mhz(reading.rx_hz) if reading.rx_hz else "")
        if reading.modulation == self._radio_shown.get("mode"):
            return
        self._radio_shown["mode"] = reading.modulation
        select = self.query_one("#mode", Select)
        current = select.value if isinstance(select.value, str) else ""
        wanted = form_mode(reading.modulation, current, self._last_digital)
        if wanted and wanted != current:
            select.value = wanted

    def _set_radio_box(self, widget_id: str, text: str) -> None:
        if text == self._radio_shown.get(widget_id, ""):
            return
        box = self.query_one(f"#{widget_id}", Input)
        if box.has_focus:
            return
        box.value = text
        self._radio_shown[widget_id] = text

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
        self._field_values.clear()
        seen: dict[str, int] = {}
        columns = split_groups(grouped_fields(response))
        for table, items in zip(self._tables(), columns):
            table.clear()
            for item in items:
                key = item.name
                seen[key] = seen.get(key, 0) + 1
                if seen[key] > 1:
                    key = f"{item.name}#{seen[key]}"
                self._field_values[key] = (item.name, item.value)
                table.add_row(
                    Text(field_label(item.name), style=LABEL_STYLE),
                    _value_text(item.name, item.value),
                    key=key,
                )
        banner = self.query_one("#banner", Static)
        banner.update(_banner(response, self.account.grid))
        banner.remove_class("empty")
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
        for table in self._tables():
            table.clear()
        banner = self.query_one("#banner", Static)
        banner.update(EMPTY_BANNER)
        banner.add_class("empty")
        self._clear_photo("No photo")
        open_button = self.query_one("#open", Button)
        open_button.disabled = True

    def _tables(self) -> list[DataTable]:
        return [self.query_one(f"#{table_id}", DataTable) for table_id in TABLE_IDS]

    def _sync_open(self, table: DataTable) -> None:
        """Enable Open link when the table's current row can be opened."""
        item = None
        if table.row_count:
            row_key, _column_key = table.coordinate_to_cell_key(table.cursor_coordinate)
            item = self._field_values.get(row_key.value or "")
        openable = bool(item and item[0] in OPEN_FIELDS and item[1])
        self.query_one("#open", Button).disabled = not openable

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


EMPTY_BANNER = "Look up a callsign to see the station here."


def _normalize_grid(text: str) -> str:
    """FN43CB or fn43cb becomes FN43cb."""
    text = text.strip()
    return text[:4].upper() + text[4:].lower()


def _value_text(name: str, value: str) -> Text:
    shown = field_display_value(name, value)
    if name in YES_NO_FIELDS:
        return Text(shown, style="#7dffb3" if shown == "Yes" else "#8d8a80")
    if name == "call":
        return Text(shown, style="bold")
    if name in OPEN_FIELDS:
        return Text(shown, style=LINK_STYLE)
    return Text(shown)


def _banner(response: LookupResponse, my_grid: str) -> Group:
    """The callsign, name, and what an operator glances at during a QSO."""
    head = Text()
    head.append(f" {response.value('call')} ", style="bold reverse #9ec9ff")
    name = full_name(response)
    if name:
        head.append("  ")
        head.append(name, style="bold")
    if response.value("xref"):
        head.append(f"   looked up as {response.value('xref')}", style="#8d8a80")

    zones = " · ".join(
        f"{label} {response.value(name)}"
        for label, name in (("CQ", "cqzone"), ("ITU", "ituzone"), ("IOTA", "iota"))
        if response.value(name)
    )
    land = response.value("land")
    if land and response.value("dxcc"):
        land += f" ({response.value('dxcc')})"
    qsl = Text()
    for name, label in (("lotw", "LoTW"), ("eqsl", "eQSL"), ("mqsl", "Paper")):
        if response.value(name):
            yes = field_display_value(name, response.value(name)) == "Yes"
            qsl.append(f"{label} ", style=LABEL_STYLE)
            qsl.append("✓  " if yes else "✗  ", style="#7dffb3" if yes else "#8d8a80")
    license_parts = []
    if response.value("class"):
        license_parts.append(f"Class {response.value('class')}")
    if response.value("expdate"):
        license_parts.append(f"expires {response.value('expdate')}")

    facts = Table.grid(padding=(0, 1))
    facts.add_column(style=LABEL_STYLE, width=10, no_wrap=True)
    facts.add_column(overflow="fold")
    for label, value in (
        ("QTH", qth_line(response)),
        ("Grid", response.value("grid")),
        ("DXCC", " · ".join(part for part in (land, zones) if part)),
        ("Local time", local_time(response)),
        ("Path", path_from(my_grid, response)),
        ("QSL", qsl),
        ("License", " · ".join(license_parts)),
    ):
        if value:
            facts.add_row(label, value)
    if response.message:
        facts.add_row("Note", Text(response.message, style="#e0a030"))
    return Group(head, Text(""), facts)


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
