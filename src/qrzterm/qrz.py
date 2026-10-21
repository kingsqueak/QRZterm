"""QRZ.com XML lookups and Logbook API.

The field list, ADIF record, and session handling follow 590Ctrl's QRZ tab
(`src/core/Qrz.cpp`) and the QRZ XML Interface Specification 1.34
(https://www.qrz.com/docs/xml/current_spec.html) plus the Logbook API
(https://www.qrz.com/docs/logbook/QRZLogbookAPI.html).

ADIF field lengths are bytes of UTF-8 when a record is written. When a
record is read, the length is taken as characters, which is what 590Ctrl's
parser does and what QRZ's logbook answers need.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from urllib.parse import quote, unquote_to_bytes
import xml.etree.ElementTree as ET

import httpx

from qrzterm import __version__

XML_URL = "https://xmldata.qrz.com/xml/current/"
LOGBOOK_URL = "https://logbook.qrz.com/api"
TIMEOUT_SECONDS = 15.0
MAX_IMAGE_BYTES = 8 * 1024 * 1024

# Callsign fields in the spec's order, with the names shown in the table.
KNOWN_FIELDS: tuple[tuple[str, str], ...] = (
    ("call", "Callsign"),
    ("xref", "Looked up as"),
    ("aliases", "Aliases"),
    ("p_call", "Previous call"),
    ("name_fmt", "Name"),
    ("fname", "First name"),
    ("name", "Last name"),
    ("nickname", "Nickname"),
    ("born", "Born"),
    ("attn", "Attention"),
    ("addr1", "Address"),
    ("addr2", "City"),
    ("state", "State"),
    ("zip", "Postal code"),
    ("country", "Country"),
    ("ccode", "Country code"),
    ("county", "County"),
    ("fips", "FIPS"),
    ("land", "DXCC entity"),
    ("dxcc", "DXCC"),
    ("grid", "Grid"),
    ("lat", "Latitude"),
    ("lon", "Longitude"),
    ("geoloc", "Location source"),
    ("cqzone", "CQ zone"),
    ("ituzone", "ITU zone"),
    ("iota", "IOTA"),
    ("TimeZone", "Time zone"),
    ("GMTOffset", "GMT offset"),
    ("DST", "Observes DST"),
    ("MSA", "Metro area"),
    ("AreaCode", "Area code"),
    ("class", "License class"),
    ("codes", "License codes"),
    ("efdate", "License effective"),
    ("expdate", "License expires"),
    ("email", "Email"),
    ("url", "Web page"),
    ("qslmgr", "QSL manager"),
    ("lotw", "LoTW"),
    ("eqsl", "eQSL"),
    ("mqsl", "Paper QSL"),
    ("bio", "Bio size"),
    ("biodate", "Bio updated"),
    ("image", "Image"),
    ("imageinfo", "Image info"),
    ("u_views", "Page views"),
    ("user", "Managed by"),
    ("serial", "QRZ serial"),
    ("moddate", "Last modified"),
)
_FIELD_LABELS = dict(KNOWN_FIELDS)
_FIELD_RANK = {name: index for index, (name, _) in enumerate(KNOWN_FIELDS)}
_YES_NO_FIELDS = {"eqsl", "mqsl", "lotw", "DST"}

# HF ranges match 590Ctrl (the TS-590's coverage). VHF and UHF follow the
# ADIF band edges so a contact off HF still gets a band.
_BANDS: tuple[tuple[int, int, str], ...] = (
    (1_800_000, 2_000_000, "160m"),
    (3_500_000, 4_000_000, "80m"),
    (5_060_000, 5_450_000, "60m"),
    (7_000_000, 7_300_000, "40m"),
    (10_100_000, 10_150_000, "30m"),
    (14_000_000, 14_350_000, "20m"),
    (18_068_000, 18_168_000, "17m"),
    (21_000_000, 21_450_000, "15m"),
    (24_890_000, 24_990_000, "12m"),
    (28_000_000, 29_700_000, "10m"),
    (50_000_000, 54_000_000, "6m"),
    (472_000, 479_000, "630m"),
    (135_700, 137_800, "2190m"),
    (70_000_000, 71_000_000, "4m"),
    (144_000_000, 148_000_000, "2m"),
    (222_000_000, 225_000_000, "1.25m"),
    (420_000_000, 450_000_000, "70cm"),
    (902_000_000, 928_000_000, "33cm"),
    (1_240_000_000, 1_300_000_000, "23cm"),
)

# USB and LSB are explicit because this app has no radio to take the
# sideband from. SSB is kept for a record with no sideband.
MODE_NAMES: tuple[str, ...] = (
    "USB",
    "LSB",
    "SSB",
    "CW",
    "AM",
    "FM",
    "RTTY",
    "FT8",
    "FT4",
    "PSK31",
    "PSK",
    "MFSK",
    "OLIVIA",
    "THOR",
    "HELL",
    "PKT",
)


class QrzError(Exception):
    def __init__(self, message: str, callsign: str = "") -> None:
        super().__init__(message)
        self.callsign = callsign


class LookupSuperseded(Exception):
    """A newer lookup started before this one finished."""


class LogInputError(ValueError):
    """The QSO form is missing something the logbook needs."""


@dataclass
class Field:
    name: str
    value: str


@dataclass
class LookupResponse:
    callsign: list[Field] = field(default_factory=list)
    key: str = ""
    error: str = ""
    message: str = ""
    subscription_expiry: str = ""
    lookup_count: str = ""

    def value(self, name: str) -> str:
        for item in self.callsign:
            if item.name == name:
                return item.value
        return ""


@dataclass
class LogResult:
    result: str = ""
    log_id: str = ""
    reason: str = ""

    def ok(self) -> bool:
        return self.result in {"OK", "REPLACE"}


@dataclass
class FetchResult:
    result: str = ""
    reason: str = ""
    count: int = 0
    records: list[dict[str, str]] = field(default_factory=list)


@dataclass
class AdifMode:
    mode: str
    submode: str = ""


@dataclass
class Qso:
    call: str
    station_call: str
    time_on: datetime
    tx_hz: int = 0
    rx_hz: int = 0
    mode: str = ""
    submode: str = ""
    rst_sent: str = ""
    rst_rcvd: str = ""
    tx_power: int | None = None
    comment: str = ""
    name: str = ""
    qth: str = ""
    grid: str = ""
    country: str = ""
    state: str = ""
    county: str = ""
    dxcc: str = ""
    cq_zone: str = ""
    itu_zone: str = ""


def field_label(name: str) -> str:
    return _FIELD_LABELS.get(name, name)


def field_rank(name: str) -> int:
    return _FIELD_RANK.get(name, len(KNOWN_FIELDS))


def field_display_value(name: str, value: str) -> str:
    if name in _YES_NO_FIELDS:
        if value in {"1", "Y"}:
            return "Yes"
        if value in {"0", "N"}:
            return "No"
    return value


def display_fields(response: LookupResponse) -> list[Field]:
    """Spec order, then any field QRZ adds later, in the order it was sent."""
    present = [item for item in response.callsign if item.value]
    return sorted(present, key=lambda item: field_rank(item.name))


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def parse_lookup_response(xml: bytes) -> LookupResponse | None:
    if xml.startswith(b"\xef\xbb\xbf"):
        xml = xml[3:]
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return None
    if _local(root.tag) != "QRZDatabase":
        return None
    response = LookupResponse()
    for section in root:
        section_name = _local(section.tag)
        if section_name not in {"Callsign", "Session"}:
            continue
        for child in section:
            name = _local(child.tag)
            text = "".join(child.itertext()).strip()
            if section_name == "Callsign":
                response.callsign.append(Field(name, text))
            elif name == "Key":
                response.key = text
            elif name == "Error":
                response.error = text
            elif name == "Message":
                response.message = text
            elif name == "SubExp":
                response.subscription_expiry = text
            elif name == "Count":
                response.lookup_count = text
    return response


def parse_log_response(body: bytes) -> LogResult:
    result = LogResult()
    for pair in body.strip().split(b"&"):
        if b"=" not in pair:
            continue
        name, raw = pair.split(b"=", 1)
        name = name.strip().upper()
        value = unquote_to_bytes(raw.replace(b"+", b" ")).decode("utf-8", "replace").strip()
        if name == b"RESULT":
            result.result = value.upper()
        elif name in {b"LOGID", b"LOGIDS"}:
            result.log_id = value
        elif name == b"REASON":
            result.reason = value
    return result


def parse_adif(adi: str) -> list[dict[str, str]]:
    records: list[dict[str, str]] = []
    record: dict[str, str] = {}
    pos = 0
    while True:
        pos = adi.find("<", pos)
        if pos < 0:
            break
        close = adi.find(">", pos)
        if close < 0:
            break
        parts = adi[pos + 1 : close].split(":")
        name = parts[0].strip().lower()
        pos = close + 1
        if name == "eor":
            if record:
                records.append(record)
            record = {}
        elif name == "eoh":
            record = {}
        elif len(parts) >= 2:
            try:
                length = int(parts[1].strip())
            except ValueError:
                continue
            if length < 0:
                continue
            record[name] = adi[pos : pos + length]
            pos += length
    return records


def _leading_int(raw: bytes) -> int:
    text = raw.strip()
    digits: list[str] = []
    for byte in text:
        character = chr(byte)
        if character.isdigit():
            digits.append(character)
        else:
            break
    return int("".join(digits) or "0")


def parse_fetch_response(body: bytes) -> FetchResult:
    """Read a FETCH answer. ADIF may be HTML-entity or percent encoded."""
    fetch = FetchResult()
    head = body.strip()
    adif = b""
    at = head.find(b"ADIF=")
    if at >= 0:
        adif = head[at + 5 :]
        head = head[:at]
        # Anything after the last end-of-record is a field that followed it.
        lower = adif.lower()
        for marker in (b"<eor>", b"&lt;eor&gt;", b"%3ceor%3e"):
            end = lower.rfind(marker)
            if end >= 0:
                head += b"&" + adif[end + len(marker) :]
                adif = adif[: end + len(marker)]
                break
    status = parse_log_response(head)
    fetch.result = status.result
    fetch.reason = status.reason
    for pair in head.split(b"&"):
        stripped = pair.strip()
        if stripped.upper().startswith(b"COUNT="):
            fetch.count = _leading_int(stripped[6:])
    if b"%3C" in adif or b"%3c" in adif:
        adif = unquote_to_bytes(adif)
    text = adif.decode("utf-8", "replace")
    if "&lt;" in text:
        text = (
            text.replace("&lt;", "<")
            .replace("&gt;", ">")
            .replace("&quot;", '"')
            .replace("&#39;", "'")
            .replace("&amp;", "&")
        )
    fetch.records = parse_adif(text)
    return fetch


def _qso_stamp(qso: dict[str, str]) -> str:
    time_on = qso.get("time_on", "")
    if len(time_on) < 6:
        time_on = time_on.ljust(6, "0")
    return qso.get("qso_date", "") + time_on


def latest_qso(records: list[dict[str, str]]) -> dict[str, str] | None:
    latest: dict[str, str] | None = None
    for qso in records:
        if latest is None or _qso_stamp(qso) > _qso_stamp(latest):
            latest = qso
    return latest


def format_qso_when(qso: dict[str, str]) -> str:
    date = qso.get("qso_date", "")
    time_on = qso.get("time_on", "")
    if len(date) != 8 or not date.isdigit():
        return ""
    pretty = f"{date[:4]}-{date[4:6]}-{date[6:8]}"
    hour = time_on[:4]
    if len(hour) == 4 and hour.isdigit():
        pretty += f" {hour[:2]}:{hour[2:4]}"
    return pretty


def worked_summary(result: FetchResult) -> tuple[bool, str]:
    """Whether this call is in the log, and the line to show for it."""
    last = latest_qso(result.records)
    if last is None:
        return False, "Not in your QRZ log"
    mode = last.get("mode", "")
    submode = last.get("submode", "")
    # FT4 reads better than its ADIF mode, MFSK. SSB reads better than USB.
    shown_mode = submode if submode and mode.upper() != "SSB" else mode
    band_mode = " ".join(part for part in (last.get("band", ""), shown_mode) if part).strip()
    parts: list[str] = []
    when = format_qso_when(last)
    if when:
        parts.append(f"Last worked {when} UTC")
    if band_mode:
        parts.append(band_mode)
    count = max(result.count, len(result.records))
    if count > 1:
        parts.append(f"{count} QSOs in your log")
    return True, " · ".join(parts)


def form_encode(fields: list[tuple[str, str]]) -> bytes:
    """application/x-www-form-urlencoded. Encodes everything outside unreserved."""
    parts = [f"{name}={quote(value, safe='')}" for name, value in fields]
    return "&".join(parts).encode("ascii")


def adif_band(hz: int) -> str:
    for low, high, name in _BANDS:
        if low <= hz <= high:
            return name
    return ""


def adif_mode_named(name: str, sideband: str = "") -> AdifMode:
    upper = name.strip().upper()
    if upper == "SSB":
        side = sideband.strip().upper()
        if side not in {"LSB", "USB"}:
            side = ""
        return AdifMode(upper, side)
    if upper in {"USB", "LSB"}:
        return AdifMode("SSB", upper)
    if upper == "FT4":
        return AdifMode("MFSK", upper)
    if upper == "PSK31":
        return AdifMode("PSK", upper)
    return AdifMode(upper, "")


def default_report(adif_mode: str) -> str:
    if adif_mode in {"CW", "RTTY"}:
        return "599"
    if adif_mode in {"SSB", "FM", "AM"}:
        return "59"
    return ""


def report_for_mode_name(name: str) -> str:
    upper = name.strip().upper()
    if upper in {"CW", "RTTY"}:
        return "599"
    if upper in {"SSB", "USB", "LSB", "FM", "AM"}:
        return "59"
    return ""


def normalize_url(url: str) -> str:
    text = url.strip()
    if text.startswith("//"):
        return "https:" + text
    if text.startswith(("http://", "https://")):
        return text
    return "https://" + text


def parse_mhz(text: str) -> int:
    """Hertz from a frequency box.

    A value with a decimal point is MHz (14.195). A whole number of 1000 or
    more, with no decimal point, is kHz (14074). A smaller whole number is
    MHz (14). A ``mhz``, ``khz``, or ``hz`` suffix overrides that.
    """
    raw = text.strip().lower().replace(" ", "")
    if not raw:
        return 0
    unit = ""
    for suffix in ("mhz", "khz", "hz"):
        if raw.endswith(suffix):
            unit = suffix
            raw = raw[: -len(suffix)]
            break
    if not raw:
        return 0
    if unit == "" and "." not in raw and "," not in raw:
        if not raw.isdigit():
            return 0
        whole = int(raw)
        if whole >= 1000:
            return whole * 1_000
        return whole * 1_000_000
    if raw.count(",") == 1 and "." not in raw:
        raw = raw.replace(",", ".")
    try:
        value = Decimal(raw)
    except InvalidOperation:
        return 0
    if value <= 0:
        return 0
    scale = {"hz": 1, "khz": 1_000, "": 1_000_000, "mhz": 1_000_000}[unit]
    hz = (value * scale).to_integral_value(ROUND_HALF_UP)
    return int(hz)


def format_mhz(hz: int) -> str:
    sign = "-" if hz < 0 else ""
    whole, frac = divmod(abs(int(hz)), 1_000_000)
    return f"{sign}{whole}.{frac:06d}"


def format_mhz_short(hz: int) -> str:
    sign = "-" if hz < 0 else ""
    kilohertz = (abs(int(hz)) + 500) // 1000
    whole, frac = divmod(kilohertz, 1000)
    return f"{sign}{whole}.{frac:03d}"


def parse_power(text: str) -> int | None:
    raw = text.strip().lower()
    if not raw:
        return None
    if raw.endswith("w"):
        raw = raw[:-1].strip()
    if not raw.isdigit():
        raise LogInputError("Power should be a whole number of watts.")
    return int(raw)


def apply_lookup(qso: Qso, lookup: LookupResponse) -> None:
    call = lookup.value("call")
    xref = lookup.value("xref")
    wanted = qso.call.strip()
    if not wanted:
        return
    folded = wanted.casefold()
    if folded not in {call.casefold(), xref.casefold()}:
        return
    qso.name = f"{lookup.value('fname')} {lookup.value('name')}".strip()
    qso.qth = lookup.value("addr2")
    qso.grid = lookup.value("grid")
    qso.country = lookup.value("land")
    qso.state = lookup.value("state")
    qso.county = lookup.value("county")
    qso.dxcc = lookup.value("dxcc")
    qso.cq_zone = lookup.value("cqzone")
    qso.itu_zone = lookup.value("ituzone")


def _append_adif(out: bytearray, name: str, value: str) -> None:
    data = value.strip().encode("utf-8")
    if not data:
        return
    out += f"<{name}:{len(data)}>".encode("ascii") + data


def adif_record(qso: Qso) -> bytes:
    when = qso.time_on
    utc = when.replace(tzinfo=timezone.utc) if when.tzinfo is None else when.astimezone(timezone.utc)
    out = bytearray()
    _append_adif(out, "call", qso.call.upper())
    _append_adif(out, "qso_date", utc.strftime("%Y%m%d"))
    _append_adif(out, "time_on", utc.strftime("%H%M%S"))
    _append_adif(out, "band", adif_band(qso.tx_hz))
    _append_adif(out, "freq", format_mhz(qso.tx_hz))
    if qso.rx_hz > 0 and qso.rx_hz != qso.tx_hz:
        _append_adif(out, "band_rx", adif_band(qso.rx_hz))
        _append_adif(out, "freq_rx", format_mhz(qso.rx_hz))
    _append_adif(out, "mode", qso.mode.upper())
    _append_adif(out, "submode", qso.submode.upper())
    _append_adif(out, "rst_sent", qso.rst_sent)
    _append_adif(out, "rst_rcvd", qso.rst_rcvd)
    _append_adif(out, "station_callsign", qso.station_call.upper())
    if qso.tx_power is not None:
        _append_adif(out, "tx_pwr", str(qso.tx_power))
    _append_adif(out, "name", qso.name)
    _append_adif(out, "qth", qso.qth)
    _append_adif(out, "gridsquare", qso.grid)
    _append_adif(out, "country", qso.country)
    _append_adif(out, "dxcc", qso.dxcc)
    _append_adif(out, "state", qso.state)
    # ADIF wants the county as "ST,County".
    if qso.state and qso.county:
        _append_adif(out, "cnty", f"{qso.state},{qso.county}")
    _append_adif(out, "cqz", qso.cq_zone)
    _append_adif(out, "ituz", qso.itu_zone)
    _append_adif(out, "comment", qso.comment)
    return bytes(out) + b"<eor>"


def prepare_qso(
    *,
    call: str,
    station_call: str,
    logbook_key: str,
    tx_mhz: str,
    rx_mhz: str = "",
    mode_name: str,
    rst_sent: str = "",
    rst_rcvd: str = "",
    power: str = "",
    comment: str = "",
    lookup: LookupResponse | None = None,
    time_on: datetime | None = None,
) -> Qso:
    wanted = call.strip().upper()
    if not wanted:
        raise LogInputError("Enter the callsign to log.")
    station = station_call.strip().upper()
    if not station or not logbook_key.strip():
        raise LogInputError("Logging needs your station callsign and QRZ Logbook API key (Account…).")
    tx_hz = parse_mhz(tx_mhz)
    if tx_hz <= 0:
        raise LogInputError("Enter the frequency in MHz, for example 14.195.")
    if not adif_band(tx_hz):
        raise LogInputError(
            f"{format_mhz_short(tx_hz)} MHz is outside the amateur bands, so there's no band to log."
        )
    rx_hz = 0
    if rx_mhz.strip():
        rx_hz = parse_mhz(rx_mhz)
        if rx_hz <= 0:
            raise LogInputError("Couldn't read the RX frequency. Use MHz, or leave it blank.")
    mode = adif_mode_named(mode_name)
    if not mode.mode:
        raise LogInputError("Pick a mode to log.")
    qso = Qso(
        call=wanted,
        station_call=station,
        time_on=time_on or datetime.now(timezone.utc),
        tx_hz=tx_hz,
        rx_hz=rx_hz,
        mode=mode.mode,
        submode=mode.submode,
        rst_sent=rst_sent.strip(),
        rst_rcvd=rst_rcvd.strip(),
        tx_power=parse_power(power),
        comment=comment.strip(),
    )
    if lookup is not None:
        apply_lookup(qso, lookup)
    return qso


class Client:
    """Holds the QRZ session and talks to the XML service and the logbook.

    Logs in on the first lookup and again when QRZ drops the session.
    A newer lookup supersedes one still in flight.
    """

    def __init__(
        self,
        *,
        agent: str | None = None,
        xml_url: str = XML_URL,
        logbook_url: str = LOGBOOK_URL,
        http: httpx.AsyncClient | None = None,
    ) -> None:
        self.agent = agent or f"QRZterm/{__version__}"
        self.xml_url = xml_url
        self.logbook_url = logbook_url
        self.username = ""
        self.password = ""
        self.logbook_key = ""
        self.session_key = ""
        self._http = http
        self._owns_http = http is None
        self._generation = 0
        self._callsign = ""

    @property
    def has_credentials(self) -> bool:
        return bool(self.username and self.password)

    @property
    def has_logbook_key(self) -> bool:
        return bool(self.logbook_key)

    def set_credentials(self, username: str, password: str) -> None:
        username = username.strip()
        if username == self.username and password == self.password:
            return
        self.username = username
        self.password = password
        self.session_key = ""

    def set_logbook_key(self, key: str) -> None:
        self.logbook_key = key.strip()

    async def aclose(self) -> None:
        if self._owns_http and self._http is not None:
            await self._http.aclose()
            self._http = None

    async def _http_client(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=TIMEOUT_SECONDS, follow_redirects=True)
        return self._http

    def _ensure_current(self, generation: int) -> None:
        if generation != self._generation:
            raise LookupSuperseded()

    async def _request(
        self,
        method: str,
        url: str,
        *,
        content: bytes | None = None,
        params: dict[str, str] | None = None,
        logbook: bool = False,
    ) -> bytes:
        http = await self._http_client()
        headers = {"User-Agent": self.agent}
        if content is not None:
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        where = "the QRZ Logbook" if logbook else "QRZ"
        try:
            response = await http.request(method, url, content=content, params=params, headers=headers)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise QrzError(f"Could not reach {where}: {exc}", self._callsign) from exc
        return response.content

    async def lookup(self, callsign: str) -> LookupResponse:
        call = callsign.strip().upper()
        self._generation += 1
        generation = self._generation
        self._callsign = call
        if not self.has_credentials:
            raise QrzError("Enter your QRZ username and password under Account.", call)
        if not self.session_key:
            await self._login(generation)
        self._ensure_current(generation)
        return await self._request_lookup(generation, may_relogin=True)

    async def _login(self, generation: int) -> None:
        body = await self._request(
            "POST",
            self.xml_url,
            content=form_encode(
                [
                    ("username", self.username),
                    ("password", self.password),
                    ("agent", self.agent),
                ]
            ),
        )
        self._ensure_current(generation)
        response = parse_lookup_response(body)
        if response is None:
            raise QrzError("QRZ sent an answer that isn't valid XML.", self._callsign)
        if not response.key:
            self.session_key = ""
            raise QrzError(response.error or "QRZ login failed.", self._callsign)
        self.session_key = response.key

    async def _request_lookup(self, generation: int, may_relogin: bool) -> LookupResponse:
        body = await self._request(
            "GET",
            self.xml_url,
            params={"s": self.session_key, "callsign": self._callsign},
        )
        self._ensure_current(generation)
        response = parse_lookup_response(body)
        if response is None:
            raise QrzError("QRZ sent an answer that isn't valid XML.", self._callsign)
        if not response.key:
            self.session_key = ""
            if may_relogin:
                await self._login(generation)
                self._ensure_current(generation)
                return await self._request_lookup(generation, may_relogin=False)
            raise QrzError(response.error or "QRZ login failed.", self._callsign)
        self.session_key = response.key
        if not response.callsign:
            raise QrzError(response.error or f"{self._callsign} was not found.", self._callsign)
        return response

    async def insert_qso(self, adif: bytes) -> LogResult:
        if not self.has_logbook_key:
            raise QrzError("Enter your QRZ Logbook API key under Account.")
        body = await self._request(
            "POST",
            self.logbook_url,
            content=form_encode(
                [
                    ("KEY", self.logbook_key),
                    ("ACTION", "INSERT"),
                    ("ADIF", adif.decode("utf-8")),
                ]
            ),
            logbook=True,
        )
        result = parse_log_response(body)
        if result.ok():
            return result
        if result.result == "AUTH":
            raise QrzError(
                "The QRZ Logbook refused the API key (it needs a QRZ subscription and the "
                "key of the logbook for this station callsign)."
            )
        raise QrzError(result.reason or "The QRZ Logbook did not accept the QSO.")

    async def check_log(self, callsign: str) -> FetchResult:
        call = callsign.strip().upper()
        self._callsign = call
        if not self.has_logbook_key or not call:
            raise QrzError("Enter your QRZ Logbook API key under Account.", call)
        body = await self._request(
            "POST",
            self.logbook_url,
            content=form_encode(
                [
                    ("KEY", self.logbook_key),
                    ("ACTION", "FETCH"),
                    ("OPTION", f"CALL:{call}"),
                ]
            ),
            logbook=True,
        )
        result = parse_fetch_response(body)
        if result.result == "AUTH":
            raise QrzError("The QRZ Logbook refused the API key.", call)
        reason = result.reason.lower()
        if result.result == "FAIL" and not result.records and any(
            word in reason for word in ("key", "invalid", "auth")
        ):
            raise QrzError(result.reason, call)
        return result

    async def fetch_image(self, url: str) -> bytes:
        body = await self._request("GET", normalize_url(url))
        if len(body) > MAX_IMAGE_BYTES:
            raise QrzError("The image from QRZ is too large.")
        return body
