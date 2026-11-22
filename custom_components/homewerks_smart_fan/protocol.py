"""Pure protocol helpers for the Homewerks Smart Fan MCU channel (TCP 8899).

No Home Assistant imports, so this module can be unit-tested on its own.

Frame layout (Linkplay MCU passthrough):
    18 96 18 20 | payload length (uint32 LE) | 12 zero bytes | payload
Payload text:
    MCU+PAS+<body>&     e.g. MCU+PAS+{"fan_power":"ON"}&  or  MCU+PAS+GetStatus&

Commands come from the Homewerks app (com.wifiaudio.HomeNetWerks 1.2.0, IOT4NormalMCUUtil):
    {"fan_power":"ON|OFF"}, {"light_power":"ON|OFF"}, {"percentage": 0-100},
    {"colorTemperature": 2200|2700|5500|7000}, {"fan_time":"HH:MM:SS"} (00:00:00 cancels),
    GetStatus (read-only: the device answers with one frame per value).

An empty value such as {"fan_time":""} is NOT a query: the device treats it as a write
(it started the fan with a garbage timer in testing). Use GetStatus to read state.
"""

from __future__ import annotations

import json
import re
import struct
from typing import Any

FRAME_HEADER = b"\x18\x96\x18\x20"
FRAME_PADDING = b"\x00" * 12
HEADER_LEN = len(FRAME_HEADER) + 4 + len(FRAME_PADDING)  # 20 bytes
PAYLOAD_PREFIX = "MCU+PAS+"
PAYLOAD_SUFFIX = "&"
GET_STATUS = "GetStatus"
MAX_PAYLOAD = 4096  # anything bigger is a corrupt length field

MAX_FAN_TIMER_SECONDS = 24 * 3600 - 1  # HH is two digits on the device

_FAN_TIME_RE = re.compile(r"^(\d{1,2}):([0-5]\d):([0-5]\d)$")


def build_frame(body: str) -> bytes:
    """Wrap a body (JSON text or a bare command like GetStatus) in a device frame."""
    payload = f"{PAYLOAD_PREFIX}{body}{PAYLOAD_SUFFIX}".encode()
    return FRAME_HEADER + struct.pack("<I", len(payload)) + FRAME_PADDING + payload


def build_command(data: dict[str, Any]) -> bytes:
    """Build a frame for a JSON write command. Refuses empty values (they are writes)."""
    if not data:
        raise ValueError("empty command")
    for key, value in data.items():
        if value is None or value == "":
            raise ValueError(f"refusing empty value for {key!r}: the device treats it as a write")
    return build_frame(json.dumps(data, separators=(",", ":")))


def build_get_status() -> bytes:
    """Build the read-only status request."""
    return build_frame(GET_STATUS)


def parse_payload(payload: bytes) -> dict[str, Any] | None:
    """Turn one payload (MCU+PAS+{...}&) into a dict, or None if it isn't JSON state."""
    try:
        text = payload.decode("utf-8").strip()
    except UnicodeDecodeError:
        return None
    if text.startswith(PAYLOAD_PREFIX):
        text = text[len(PAYLOAD_PREFIX):]
    if text.endswith(PAYLOAD_SUFFIX):
        text = text[: -len(PAYLOAD_SUFFIX)]
    text = text.strip()
    if not text.startswith("{"):
        return None
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


class FrameBuffer:
    """Reassemble frames from a TCP stream (a read can hold several or part of one)."""

    def __init__(self) -> None:
        self._buf = b""

    def feed(self, data: bytes) -> list[dict[str, Any]]:
        """Add received bytes and return every complete, parsed state payload."""
        self._buf += data
        out: list[dict[str, Any]] = []
        while True:
            start = self._buf.find(FRAME_HEADER)
            if start == -1:
                # Keep only a possible partial header at the tail.
                self._buf = self._buf[-(len(FRAME_HEADER) - 1):] if self._buf else b""
                return out
            if start:
                self._buf = self._buf[start:]
            if len(self._buf) < HEADER_LEN:
                return out
            (length,) = struct.unpack("<I", self._buf[4:8])
            if length > MAX_PAYLOAD:
                # Corrupt length: skip this header and resync on the next one.
                self._buf = self._buf[len(FRAME_HEADER):]
                continue
            end = HEADER_LEN + length
            if len(self._buf) < end:
                return out
            parsed = parse_payload(self._buf[HEADER_LEN:end])
            if parsed is not None:
                out.append(parsed)
            self._buf = self._buf[end:]


def parse_fan_time(value: Any) -> int | None:
    """'HH:MM:SS' -> seconds. Returns None for anything the device shouldn't have sent."""
    if not isinstance(value, str):
        return None
    m = _FAN_TIME_RE.match(value.strip())
    if not m:
        return None
    h, mi, s = (int(x) for x in m.groups())
    return h * 3600 + mi * 60 + s


def format_fan_time(seconds: int) -> str:
    """Seconds -> 'HH:MM:SS', clamped to what the device accepts."""
    seconds = max(0, min(MAX_FAN_TIMER_SECONDS, int(seconds)))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"
