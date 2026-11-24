"""Tests for the MCU protocol helpers and the API's state handling (no Home Assistant needed)."""

from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path
import struct
import sys
import types

import pytest

PKG_DIR = Path(__file__).resolve().parents[1] / "custom_components" / "homewerks_smart_fan"
PKG = "hwfan_under_test"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(f"{PKG}.{name}", PKG_DIR / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[f"{PKG}.{name}"] = mod
    spec.loader.exec_module(mod)
    return mod


# A bare package so the API's relative imports resolve without importing __init__ (which needs HA).
pkg = types.ModuleType(PKG)
pkg.__path__ = [str(PKG_DIR)]
sys.modules[PKG] = pkg
const = _load("const")
protocol = _load("protocol")
api_mod = _load("api")


def frame(payload: bytes) -> bytes:
    return b"\x18\x96\x18\x20" + struct.pack("<I", len(payload)) + b"\x00" * 12 + payload


# Exact replies captured from the fan (firmware 4.6.603649.83) for MCU+PAS+GetStatus&.
GETSTATUS_REPLY = b"".join(
    frame(p)
    for p in (
        b'MCU+PAS+{"light_power": "OFF"}&',
        b'MCU+PAS+{"percentage": 75}&',
        b'MCU+PAS+{ "colorTemperature": 2700 }&',
        b'MCU+PAS+{"fan_power": "OFF"}&',
        b'MCU+PAS+{"fan_time": "00:00:00"}&',
    )
)


def test_build_frame_layout():
    f = protocol.build_get_status()
    assert f[:4] == b"\x18\x96\x18\x20"
    (length,) = struct.unpack("<I", f[4:8])
    assert f[8:20] == b"\x00" * 12
    assert f[20:] == b"MCU+PAS+GetStatus&"
    assert length == len(b"MCU+PAS+GetStatus&")


def test_build_command_json():
    f = protocol.build_command({"fan_time": "00:20:00"})
    assert f[20:] == b'MCU+PAS+{"fan_time":"00:20:00"}&'


@pytest.mark.parametrize("value", ["", None])
def test_build_command_refuses_empty_values(value):
    with pytest.raises(ValueError):
        protocol.build_command({"fan_time": value})
    with pytest.raises(ValueError):
        protocol.build_command({})


def test_framebuffer_parses_getstatus_reply_in_one_read():
    out = protocol.FrameBuffer().feed(GETSTATUS_REPLY)
    assert out == [
        {"light_power": "OFF"},
        {"percentage": 75},
        {"colorTemperature": 2700},
        {"fan_power": "OFF"},
        {"fan_time": "00:00:00"},
    ]


def test_framebuffer_reassembles_split_frames():
    buf = protocol.FrameBuffer()
    data = GETSTATUS_REPLY
    got = []
    for i in range(0, len(data), 7):  # deliver in awkward 7-byte chunks
        got += buf.feed(data[i : i + 7])
    assert len(got) == 5 and got[-1] == {"fan_time": "00:00:00"}


def test_framebuffer_skips_garbage_and_bad_lengths():
    bad = b"\x18\x96\x18\x20" + struct.pack("<I", 10_000_000) + b"\x00" * 12
    data = b"junk" + bad + frame(b'MCU+PAS+{"fan_power": "ON"}&') + frame(b"MCU+PAS+not-json&")
    assert protocol.FrameBuffer().feed(data) == [{"fan_power": "ON"}]


@pytest.mark.parametrize(
    ("raw", "seconds"),
    [("00:00:00", 0), ("00:20:00", 1200), ("1:02:03", 3723), ("C3:H0:H0", None), ("", None), (5, None)],
)
def test_parse_fan_time(raw, seconds):
    assert protocol.parse_fan_time(raw) == seconds


def test_format_fan_time_clamps():
    assert protocol.format_fan_time(1200) == "00:20:00"
    assert protocol.format_fan_time(-5) == "00:00:00"
    assert protocol.format_fan_time(10**9) == "23:59:59"


def _api():
    return api_mod.HomewerksSmartFanApi("192.0.2.1")


def test_state_from_getstatus():
    api = _api()
    for p in protocol.FrameBuffer().feed(GETSTATUS_REPLY):
        api._update_state_from_response(p)
    s = api.state
    assert s["light_power"] is False and s["fan_power"] is False
    assert s["brightness"] == 75
    assert s["color_temp"] == 6500  # device 2700 is "cool white" on the inverted scale
    assert s["fan_timer_ends"] is None


def test_state_ignores_bad_values():
    api = _api()
    api._update_state_from_response({"percentage": 255})
    api._update_state_from_response({"colorTemperature": 3000})
    api._update_state_from_response({"fan_time": "C3:H0:H0"})
    api._update_state_from_response({"fan_power": "MAYBE"})
    s = api.state
    assert s["brightness"] == 100 and s["color_temp"] == 4000
    assert s["fan_timer_seconds"] == 0 and s["fan_power"] is False


def test_fan_timer_sets_and_fan_off_clears():
    api = _api()
    api._update_state_from_response({"fan_power": "ON", "fan_time": "00:20:00"})
    assert api.state["fan_timer_seconds"] == 1200
    assert api.state["fan_timer_ends"] is not None
    api._update_state_from_response({"fan_power": "OFF"})
    assert api.state["fan_timer_ends"] is None and api.state["fan_timer_seconds"] == 0


def test_reads_use_getstatus_and_timer_command():
    api = _api()
    sent: list[bytes] = []

    async def fake_write(frame_bytes: bytes) -> bool:
        sent.append(frame_bytes[20:])
        return True

    api._write = fake_write  # type: ignore[method-assign]

    async def run():
        assert await api.request_state()
        assert await api.set_fan_timer(1200)
        assert api.state["fan_power"] is True and api.state["fan_timer_seconds"] == 1200
        assert await api.cancel_fan_timer()
        assert api.state["fan_power"] is False and api.state["fan_timer_ends"] is None

    asyncio.run(run())
    assert sent == [
        b"MCU+PAS+GetStatus&",
        b'MCU+PAS+{"fan_time":"00:20:00"}&',
        b'MCU+PAS+{"fan_time":"00:00:00"}&',
    ]
    # No empty-value "queries" ever go out.
    assert not any(b'""' in s for s in sent)


def test_timer_end_is_fixed_while_running_and_cleared_when_off(monkeypatch):
    """fan_time is the run length (reported even when off), not the time left."""
    import datetime as dt

    api = _api()
    t0 = dt.datetime(2026, 11, 24, 12, 0, tzinfo=dt.timezone.utc)
    clock = {"now": t0}

    class FakeDT(dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return clock["now"]

    monkeypatch.setattr(api_mod, "datetime", FakeDT)
    # Off, but the device still reports its preset length: no end time.
    api._update_state_from_response({"fan_power": "OFF", "fan_time": "00:20:00"})
    assert api.state["fan_timer_ends"] is None
    # Run started by us: end fixed at now + 15 min.
    api._update_state_from_response({"fan_power": "ON", "fan_time": "00:15:00"}, rearm=True)
    end = t0 + dt.timedelta(minutes=15)
    assert api.state["fan_timer_ends"] == end
    # Later polls report the same length: end must not move.
    clock["now"] = t0 + dt.timedelta(minutes=5)
    api._update_state_from_response({"fan_power": "ON", "fan_time": "00:15:00"})
    assert api.state["fan_timer_ends"] == end
    # Re-armed (e.g. motion extends the run): new end from now.
    api._update_state_from_response({"fan_power": "ON", "fan_time": "00:15:00"}, rearm=True)
    assert api.state["fan_timer_ends"] == clock["now"] + dt.timedelta(minutes=15)
    # Fan stops: end cleared even though fan_time is still reported.
    api._update_state_from_response({"fan_power": "OFF", "fan_time": "00:15:00"})
    assert api.state["fan_timer_ends"] is None
    # Started externally (wall/Alexa) with a preset: end fixed from that first poll.
    clock["now"] = t0 + dt.timedelta(minutes=30)
    api._update_state_from_response({"fan_power": "ON", "fan_time": "00:20:00"})
    assert api.state["fan_timer_ends"] == clock["now"] + dt.timedelta(minutes=20)
