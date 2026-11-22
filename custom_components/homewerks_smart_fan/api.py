"""API client for Homewerks Smart Fan."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
import logging
from typing import Any

import aiohttp

from .const import (
    CONNECTION_TIMEOUT,
    DEFAULT_PORT,
    KEY_COLOR_TEMPERATURE,
    KEY_FAN_POWER,
    KEY_FAN_TIME,
    KEY_LIGHT_POWER,
    KEY_PERCENTAGE,
    MAX_COLOR_TEMP_KELVIN,
    MIN_COLOR_TEMP_KELVIN,
    SUPPORTED_DEVICE_COLOR_TEMPS,
    UPNP_PORT,
    VALUE_OFF,
    VALUE_ON,
)
from .protocol import (
    FrameBuffer,
    build_command,
    build_get_status,
    format_fan_time,
    parse_fan_time,
)

_LOGGER = logging.getLogger(__name__)

# No data for this long -> send GetStatus as a keepalive (it is read-only).
IDLE_KEEPALIVE_SECONDS = 120
# A stalled socket must not hold the lock forever.
WRITE_TIMEOUT = 5


class HomewerksSmartFanApi:
    """Client for the fan's MCU channel (TCP 8899) plus UPnP volume."""

    def __init__(
        self,
        host: str,
        port: int = DEFAULT_PORT,
        session: aiohttp.ClientSession | None = None,
    ) -> None:
        """Initialize the API client."""
        self._host = host
        self._port = port
        self._session = session
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._lock = asyncio.Lock()
        self._frames = FrameBuffer()
        self._state: dict[str, Any] = {
            "fan_power": False,
            "light_power": False,
            "brightness": 100,
            "color_temp": 4000,
            "volume": 50,
            "fan_timer_seconds": 0,
            "fan_timer_ends": None,
        }
        self._listener_task: asyncio.Task | None = None
        self._reconnect_task: asyncio.Task | None = None
        self._connected = False
        self._upnp_port = UPNP_PORT
        self._state_callbacks: list[Callable[[], None]] = []
        self._reconnect_delay = 1
        self._max_reconnect_delay = 60
        self._should_reconnect = True
        self._gen = 0  # connection generation, see connect()

    # ---- callbacks / properties -------------------------------------------------

    def register_state_callback(self, callback: Callable[[], None]) -> None:
        """Register a callback to be called when state changes."""
        if callback not in self._state_callbacks:
            self._state_callbacks.append(callback)

    def unregister_state_callback(self, callback: Callable[[], None]) -> None:
        """Unregister a state callback."""
        if callback in self._state_callbacks:
            self._state_callbacks.remove(callback)

    def _notify_state_change(self) -> None:
        for callback in list(self._state_callbacks):
            try:
                callback()
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("Error in state callback: %s", err)

    @property
    def host(self) -> str:
        """Return the host."""
        return self._host

    @property
    def connected(self) -> bool:
        """Return connection status."""
        return self._connected

    @property
    def state(self) -> dict[str, Any]:
        """Return a copy of the current state."""
        return self._state.copy()

    # ---- state handling ---------------------------------------------------------

    @staticmethod
    def _invert_color_temp(temp: int) -> int:
        """Device scale is inverted (7000 = warm). Same formula both directions."""
        return MIN_COLOR_TEMP_KELVIN + MAX_COLOR_TEMP_KELVIN - temp

    @staticmethod
    def _snap_color_temp(temp: int) -> int:
        """Snap to one of the four device color temperatures."""
        return min(SUPPORTED_DEVICE_COLOR_TEMPS, key=lambda t: abs(t - temp))

    def _set(self, key: str, value: Any) -> bool:
        if self._state.get(key) != value:
            self._state[key] = value
            return True
        return False

    def _apply_fan_timer(self, seconds: int) -> bool:
        ends = (
            datetime.now(timezone.utc) + timedelta(seconds=seconds) if seconds > 0 else None
        )
        changed = self._set("fan_timer_seconds", seconds)
        old = self._state.get("fan_timer_ends")
        # Only move the end time if it shifts by more than a few seconds (avoids churn on polls).
        if (old is None) != (ends is None) or (
            old is not None and ends is not None and abs((ends - old).total_seconds()) > 5
        ):
            self._state["fan_timer_ends"] = ends
            changed = True
        return changed

    def _update_state_from_response(self, parsed: dict[str, Any], notify: bool = True) -> None:
        """Merge one state payload from the device (or an optimistic command)."""
        changed = False
        if KEY_FAN_POWER in parsed and parsed[KEY_FAN_POWER] in (VALUE_ON, VALUE_OFF):
            on = parsed[KEY_FAN_POWER] == VALUE_ON
            changed |= self._set("fan_power", on)
            if not on:
                changed |= self._apply_fan_timer(0)
        if KEY_LIGHT_POWER in parsed and parsed[KEY_LIGHT_POWER] in (VALUE_ON, VALUE_OFF):
            changed |= self._set("light_power", parsed[KEY_LIGHT_POWER] == VALUE_ON)
        if KEY_PERCENTAGE in parsed:
            raw = parsed[KEY_PERCENTAGE]
            # 255 is the device's "not tracked" sentinel; accept 0-100 only.
            if isinstance(raw, (int, float)) and not isinstance(raw, bool) and 0 <= raw <= 100:
                changed |= self._set("brightness", int(raw))
        if KEY_COLOR_TEMPERATURE in parsed:
            raw = parsed[KEY_COLOR_TEMPERATURE]
            if isinstance(raw, int) and not isinstance(raw, bool) and raw in SUPPORTED_DEVICE_COLOR_TEMPS:
                changed |= self._set("color_temp", self._invert_color_temp(raw))
        if KEY_FAN_TIME in parsed:
            seconds = parse_fan_time(parsed[KEY_FAN_TIME])
            if seconds is None:
                _LOGGER.debug("Ignoring malformed fan_time %r", parsed[KEY_FAN_TIME])
            else:
                changed |= self._apply_fan_timer(seconds)
        if changed and notify:
            self._notify_state_change()

    # ---- connection -------------------------------------------------------------
    #
    # Each successful connect bumps a generation number. A listener only tears down the
    # connection it was started for, so a stale listener can never close a newer socket.

    async def connect(self) -> bool:
        """Connect to the device."""
        async with self._lock:
            if self._connected:
                return True
            try:
                reader, writer = await asyncio.wait_for(
                    asyncio.open_connection(self._host, self._port),
                    timeout=CONNECTION_TIMEOUT,
                )
            except (OSError, asyncio.TimeoutError) as err:
                _LOGGER.debug("Failed to connect to %s:%s: %s", self._host, self._port, err)
                return False
            old_listener = self._listener_task
            self._gen += 1
            self._reader, self._writer = reader, writer
            self._connected = True
            self._frames = FrameBuffer()
            self._reconnect_delay = 1
            self._listener_task = asyncio.create_task(self._listen_for_updates(self._gen, reader))
        if old_listener is not None and not old_listener.done():
            old_listener.cancel()
        _LOGGER.debug("Connected to %s:%s", self._host, self._port)
        self._notify_state_change()  # entities become available again
        return True

    async def _reconnect_loop(self) -> None:
        """Retry until connected AND the status request went out (single loop, backoff)."""
        while self._should_reconnect:
            await asyncio.sleep(self._reconnect_delay)
            self._reconnect_delay = min(self._reconnect_delay * 2, self._max_reconnect_delay)
            if await self.connect() and await self.request_state():
                # The listener may have seen EOF while the status went out; only stop
                # once the link is still up after a moment.
                await asyncio.sleep(0.5)
                if self._connected:
                    _LOGGER.info("Reconnected to %s:%s", self._host, self._port)
                    return

    def _start_reconnect(self) -> None:
        if self._should_reconnect and (
            self._reconnect_task is None or self._reconnect_task.done()
        ):
            self._reconnect_task = asyncio.create_task(self._reconnect_loop())

    def _drop_connection(self, gen: int) -> None:
        """Tear down connection `gen` (no-op if a newer connection replaced it)."""
        if gen != self._gen:
            return
        was_connected = self._connected
        self._connected = False
        if self._writer is not None:
            self._writer.close()
        self._writer = None
        self._reader = None
        if was_connected:
            self._notify_state_change()  # entities go unavailable

    async def disconnect(self) -> None:
        """Disconnect from the device and stop reconnecting."""
        self._should_reconnect = False
        for task in (self._reconnect_task, self._listener_task):
            if task is not None and task is not asyncio.current_task():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        self._reconnect_task = None
        self._listener_task = None
        async with self._lock:
            self._gen += 1
            if self._writer is not None:
                self._writer.close()
                try:
                    await self._writer.wait_closed()
                except Exception:  # noqa: BLE001
                    pass
            self._writer = None
            self._reader = None
            self._connected = False

    async def _listen_for_updates(self, gen: int, reader: asyncio.StreamReader) -> None:
        """Read frames pushed by the device on connection `gen` (also command replies)."""
        idle = 0
        while gen == self._gen and self._connected:
            try:
                data = await asyncio.wait_for(reader.read(4096), timeout=30)
            except asyncio.TimeoutError:
                idle += 30
                if idle >= IDLE_KEEPALIVE_SECONDS:
                    idle = 0
                    if not await self._write(build_get_status()):
                        break
                continue
            except asyncio.CancelledError:
                return
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("Error reading from device: %s", err)
                break
            if not data:
                _LOGGER.debug("Connection closed by device")
                break
            idle = 0
            for parsed in self._frames.feed(data):
                _LOGGER.debug("Received: %s", parsed)
                self._update_state_from_response(parsed)

        if gen == self._gen:
            self._drop_connection(gen)
            self._start_reconnect()

    async def _write(self, frame: bytes) -> bool:
        if not self._connected and not await self.connect():
            self._start_reconnect()
            return False
        async with self._lock:
            writer, gen = self._writer, self._gen
            if writer is None:
                return False
            try:
                writer.write(frame)
                await asyncio.wait_for(writer.drain(), timeout=WRITE_TIMEOUT)
                return True
            except (Exception, asyncio.TimeoutError) as err:  # noqa: BLE001
                _LOGGER.debug("Write failed: %s", err)
        self._drop_connection(gen)
        self._start_reconnect()
        return False

    # ---- commands ---------------------------------------------------------------

    async def _send_command(self, data: dict[str, Any]) -> bool:
        frame = build_command(data)  # raises on empty values
        if not await self._write(frame):
            return False
        _LOGGER.debug("Sent command: %s", data)
        self._update_state_from_response(data, notify=True)  # optimistic
        return True

    async def request_state(self) -> bool:
        """Ask for the full state (read-only GetStatus). Replies arrive on the listener."""
        return await self._write(build_get_status())

    async def set_fan_power(self, on: bool) -> bool:
        """Turn the fan on or off."""
        return await self._send_command({KEY_FAN_POWER: VALUE_ON if on else VALUE_OFF})

    async def set_fan_timer(self, seconds: int) -> bool:
        """Run the fan for `seconds` then stop (0 cancels the timer and stops the fan)."""
        value = format_fan_time(seconds)
        if not await self._send_command({KEY_FAN_TIME: value}):
            return False
        # The device switches the fan on/off itself; mirror that until it reports back.
        secs = parse_fan_time(value) or 0
        self._update_state_from_response(
            {KEY_FAN_POWER: VALUE_ON if secs else VALUE_OFF, KEY_FAN_TIME: value}
        )
        return True

    async def cancel_fan_timer(self) -> bool:
        """Cancel the run timer. The device also stops the fan."""
        return await self.set_fan_timer(0)

    async def set_light_power(self, on: bool) -> bool:
        """Turn the light on or off."""
        return await self._send_command({KEY_LIGHT_POWER: VALUE_ON if on else VALUE_OFF})

    async def set_brightness(self, brightness: int) -> bool:
        """Set the light brightness (0-100)."""
        return await self._send_command({KEY_PERCENTAGE: max(0, min(100, int(brightness)))})

    async def set_color_temperature(self, temp_kelvin: int) -> bool:
        """Set the color temperature in Kelvin (snapped to the device's four presets)."""
        temp_kelvin = max(MIN_COLOR_TEMP_KELVIN, min(MAX_COLOR_TEMP_KELVIN, temp_kelvin))
        device_temp = self._snap_color_temp(self._invert_color_temp(temp_kelvin))
        return await self._send_command({KEY_COLOR_TEMPERATURE: device_temp})

    async def send_command(self, data: dict[str, Any]) -> bool:
        """Send a combined command (public API)."""
        return await self._send_command(data)

    async def test_connection(self) -> bool:
        """Test if we can open the MCU port."""
        try:
            _reader, writer = await asyncio.wait_for(
                asyncio.open_connection(self._host, self._port), timeout=CONNECTION_TIMEOUT
            )
        except (OSError, asyncio.TimeoutError):
            return False
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:  # noqa: BLE001
            pass
        return True

    # ---- UPnP volume ------------------------------------------------------------

    async def _upnp_action(self, service: str, action: str, args: str = "") -> str | None:
        url = f"http://{self._host}:{self._upnp_port}/upnp/control/rendercontrol1"
        headers = {
            "Content-Type": 'text/xml; charset="utf-8"',
            "SOAPACTION": f'"urn:schemas-upnp-org:service:{service}:1#{action}"',
        }
        body = (
            '<?xml version="1.0" encoding="utf-8"?>'
            '<s:Envelope s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/" '
            'xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"><s:Body>'
            f'<u:{action} xmlns:u="urn:schemas-upnp-org:service:{service}:1">'
            f"<InstanceID>0</InstanceID>{args}</u:{action}></s:Body></s:Envelope>"
        )
        own = self._session is None
        session = self._session or aiohttp.ClientSession()
        try:
            async with session.post(
                url, headers=headers, data=body, timeout=aiohttp.ClientTimeout(total=5)
            ) as response:
                return await response.text()
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("UPnP %s failed: %s", action, err)
            return None
        finally:
            if own:
                await session.close()

    @staticmethod
    def _xml_value(response: str | None, tag: str) -> str | None:
        if not response:
            return None
        start = response.find(f"<{tag}>")
        end = response.find(f"</{tag}>")
        if start == -1 or end == -1:
            return None
        return response[start + len(tag) + 2 : end]

    async def get_volume(self) -> int | None:
        """Get the current volume (0-100)."""
        raw = self._xml_value(
            await self._upnp_action("RenderingControl", "GetVolume", "<Channel>Master</Channel>"),
            "CurrentVolume",
        )
        try:
            volume = int(raw) if raw is not None else None
        except ValueError:
            return None
        if volume is not None and self._set("volume", volume):
            self._notify_state_change()
        return volume

    async def set_volume(self, volume: int) -> bool:
        """Set the volume (0-100)."""
        volume = max(0, min(100, int(volume)))
        response = await self._upnp_action(
            "RenderingControl",
            "SetVolume",
            f"<Channel>Master</Channel><DesiredVolume>{volume}</DesiredVolume>",
        )
        if response is None:
            return False
        if self._set("volume", volume):
            self._notify_state_change()
        return True

    async def get_mute(self) -> bool | None:
        """Get the mute state."""
        raw = self._xml_value(
            await self._upnp_action("RenderingControl", "GetMute", "<Channel>Master</Channel>"),
            "CurrentMute",
        )
        return None if raw is None else raw == "1"

    async def set_mute(self, mute: bool) -> bool:
        """Set the mute state."""
        response = await self._upnp_action(
            "RenderingControl",
            "SetMute",
            f"<Channel>Master</Channel><DesiredMute>{1 if mute else 0}</DesiredMute>",
        )
        return response is not None
