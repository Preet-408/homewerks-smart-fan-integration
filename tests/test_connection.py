"""Connection lifecycle tests against a fake fan on localhost."""

from __future__ import annotations

import asyncio

from test_protocol import GETSTATUS_REPLY, api_mod, protocol


class FakeFan:
    """Answers GetStatus like the real device; can drop clients on demand."""

    def __init__(self) -> None:
        self.server: asyncio.base_events.Server | None = None
        self.writers: list[asyncio.StreamWriter] = []
        self.received: list[dict | str] = []

    async def start(self) -> int:
        self.server = await asyncio.start_server(self._client, "127.0.0.1", 0)
        return self.server.sockets[0].getsockname()[1]

    async def _client(self, reader, writer) -> None:
        self.writers.append(writer)
        buf = b""
        try:
            while data := await reader.read(4096):
                buf += data
                while b"&" in buf:
                    i = buf.index(b"&") + 1
                    payload, buf = buf[20:i], buf[i:]
                    if payload == b"MCU+PAS+GetStatus&":
                        self.received.append("GetStatus")
                        writer.write(GETSTATUS_REPLY)
                    else:
                        self.received.append(protocol.parse_payload(payload))
                    await writer.drain()
        except ConnectionError:
            pass

    def drop_clients(self) -> None:
        for w in self.writers:
            w.close()
        self.writers.clear()

    async def stop(self) -> None:
        self.drop_clients()
        self.server.close()
        await self.server.wait_closed()


async def _wait(pred, timeout=3.0):
    end = asyncio.get_running_loop().time() + timeout
    while not pred():
        if asyncio.get_running_loop().time() > end:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.02)


def test_connect_sync_drop_and_reconnect():
    async def run():
        fan = FakeFan()
        port = await fan.start()
        api = api_mod.HomewerksSmartFanApi("127.0.0.1", port=port)
        calls = []
        api.register_state_callback(lambda: calls.append(api.connected))

        assert await api.connect()
        assert calls and calls[-1] is True  # availability notified on connect
        assert await api.request_state()
        await _wait(lambda: api.state["brightness"] == 75)

        api._reconnect_delay = 0.05
        fan.drop_clients()
        await _wait(lambda: not api.connected)
        assert False in calls  # unavailability notified
        await _wait(lambda: api.connected)  # reconnect loop brought it back
        await _wait(lambda: fan.received.count("GetStatus") >= 2)  # and re-synced

        await api.disconnect()
        await fan.stop()
        # Only reads and real commands ever reached the device.
        assert all(r == "GetStatus" for r in fan.received)

    asyncio.run(run())


def test_stale_generation_cannot_close_newer_connection():
    async def run():
        fan = FakeFan()
        port = await fan.start()
        api = api_mod.HomewerksSmartFanApi("127.0.0.1", port=port)
        api._should_reconnect = False  # keep the test deterministic
        assert await api.connect()
        old_gen = api._gen
        api._drop_connection(old_gen)
        assert await api.connect()
        api._drop_connection(old_gen)  # stale: must be a no-op
        assert api.connected
        assert await api.set_fan_timer(600)
        await _wait(lambda: {"fan_time": "00:10:00"} in fan.received)
        await api.disconnect()
        await fan.stop()

    asyncio.run(run())


def test_write_timeout_drops_connection():
    async def run():
        api = api_mod.HomewerksSmartFanApi("127.0.0.1")
        api._should_reconnect = False

        class StuckWriter:
            def write(self, _):
                pass

            async def drain(self):
                await asyncio.sleep(60)

            def close(self):
                pass

        api._connected = True
        api._writer = StuckWriter()
        saved, api_mod.WRITE_TIMEOUT = api_mod.WRITE_TIMEOUT, 0.05
        try:
            assert await api.request_state() is False
            assert not api.connected
        finally:
            api_mod.WRITE_TIMEOUT = saved

    asyncio.run(run())
