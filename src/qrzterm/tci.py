"""Read the radio's frequency and mode over TCI.

TCI is text commands over a WebSocket. A server sends its whole state when a
client connects, ends that with ``ready;``, and then sends changes.

This client only listens. It never sends a TCI command, so it can't tune or
key the radio.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass

from websockets.asyncio.client import connect
from websockets.exceptions import WebSocketException

DEFAULT_PORT = 40001  # ExpertSDR's default. Other servers pick their own.
RETRY_SECONDS = 3.0
TRX = 0

# Radio modulation to the Mode list. A digital modulation doesn't say which
# digital mode is on the air, so those are handled in form_mode().
_MODES = {
    "USB": "USB",
    "LSB": "LSB",
    "CW": "CW",
    "CWL": "CW",
    "CWU": "CW",
    "CWR": "CW",
    "AM": "AM",
    "SAM": "AM",
    "FM": "FM",
    "NFM": "FM",
}
_DIGITAL = {"DIGU", "DIGL", "DIGITAL", "DATA"}
_NOT_DIGITAL_NAMES = {"USB", "LSB", "SSB", "CW", "AM", "FM"}


class AddressError(ValueError):
    """The TCI address can't be used."""


def parse_address(text: str) -> tuple[str, int]:
    """Host and port from ``host``, ``host:port``, or ``[v6]:port``."""
    raw = text.strip()
    if not raw:
        raise AddressError("Enter the radio's address, such as localhost:40001.")
    if raw.startswith("["):
        host, _, rest = raw[1:].partition("]")
        port_text = rest.removeprefix(":")
    elif raw.count(":") > 1:
        host, port_text = raw, ""
    else:
        host, _, port_text = raw.partition(":")
    if not host:
        raise AddressError("The address needs a host name.")
    if not port_text:
        return host, DEFAULT_PORT
    if not port_text.isdigit() or not 1 <= int(port_text) <= 65535:
        raise AddressError(f"{port_text} isn't a port number.")
    return host, int(port_text)


def is_digital_name(name: str) -> bool:
    return name.upper() not in _NOT_DIGITAL_NAMES


def form_mode(modulation: str, current: str, last_digital: str) -> str | None:
    """The Mode list entry for a radio modulation, or None to leave it alone.

    DIGU can be FT8, RTTY, or anything else, so a digital pick already in the
    list stays. Otherwise the last digital mode picked is used.
    """
    upper = modulation.upper()
    if upper in _DIGITAL:
        return current if is_digital_name(current) else last_digital
    return _MODES.get(upper)


@dataclass(frozen=True)
class Reading:
    tx_hz: int = 0
    rx_hz: int = 0  # Set only when split.
    modulation: str = ""


class TciState:
    """What the server has said so far, for one connection."""

    def __init__(self) -> None:
        self.device = ""
        self.ready = False
        self._partial = ""
        self._vfo = [0, 0]
        self._tx_frequency = 0
        self._split = False
        self._modulation = ""
        self._published: Reading | None = None

    def reading(self) -> Reading:
        vfo_a, vfo_b = self._vfo
        if self._split:
            return Reading(self._tx_frequency or vfo_b, vfo_a, self._modulation)
        return Reading(vfo_a, 0, self._modulation)

    def feed(self, text: str) -> Reading | None:
        """Take a text frame. Returns the new reading if it changed.

        Nothing is returned before ``ready;``, so the burst of state on
        connect arrives as one reading.
        """
        chunks = (self._partial + text).split(";")
        self._partial = chunks.pop()
        for chunk in chunks:
            self._command(chunk.strip())
        if not self.ready:
            return None
        reading = self.reading()
        if reading == self._published:
            return None
        self._published = reading
        return reading

    def _command(self, command: str) -> None:
        name, _, rest = command.partition(":")
        name = name.lower()
        args = [part.strip() for part in rest.split(",")]
        try:
            if name == "ready":
                self.ready = True
            elif name == "device":
                self.device = rest.strip()
            elif name == "vfo" and len(args) >= 3 and int(args[0]) == TRX:
                channel = int(args[1])
                if channel in (0, 1):
                    self._vfo[channel] = int(args[2])
            elif name == "tx_frequency" and rest:
                self._tx_frequency = int(args[0])
            elif name == "modulation" and len(args) >= 2 and int(args[0]) == TRX:
                self._modulation = args[1].upper()
            elif name == "split_enable" and len(args) >= 2 and int(args[0]) == TRX:
                self._split = args[1].lower() == "true"
        except ValueError:
            return


async def listen(
    host: str,
    port: int,
    on_reading: Callable[[Reading], None],
    on_link: Callable[[bool, str], None],
    retry: float = RETRY_SECONDS,
) -> None:
    """Follow the radio until cancelled, reconnecting after any failure.

    ``on_link`` gets (True, device) once the server is ready and
    (False, reason) when a connection fails or drops.
    """
    target = f"[{host}]" if ":" in host else host
    uri = f"ws://{target}:{port}"
    while True:
        state = TciState()
        reason = "connection closed"
        try:
            async with connect(uri, open_timeout=5, compression=None, proxy=None) as socket:
                async for frame in socket:
                    if not isinstance(frame, str):
                        continue
                    was_ready = state.ready
                    reading = state.feed(frame)
                    if state.ready and not was_ready:
                        on_link(True, state.device)
                    if reading is not None:
                        on_reading(reading)
        except (OSError, TimeoutError, WebSocketException) as exc:
            reason = str(exc) or type(exc).__name__
        on_link(False, reason)
        await asyncio.sleep(retry)
