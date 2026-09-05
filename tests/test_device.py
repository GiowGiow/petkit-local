"""End-to-end tests of the BLE client against a simulated fountain."""

from __future__ import annotations

import asyncio

import pytest
from bleak.exc import BleakError

from custom_components.petkit_ble import protocol as p
from custom_components.petkit_ble.const import MODE_SMART
from custom_components.petkit_ble.device import (
    PetkitAuthError,
    PetkitConnectionError,
    PetkitFountain,
)

pytestmark = pytest.mark.asyncio

BLE_DEVICE = object()  # opaque; the fake connector ignores it


def _cmds(fountain) -> list[int]:
    return [f.cmd for f in fountain.received]


async def test_first_run_claims_device(fountain, patched_connection):
    """With no stored secret the client claims the fountain via cmd 73."""
    client = PetkitFountain("AA:BB:CC:DD:EE:FF", "CTW3")
    state = await client.async_poll(BLE_DEVICE)

    assert _cmds(fountain)[:4] == [
        p.CMD_IDENTITY,
        p.CMD_INIT_DEVICE,
        p.CMD_SECURITY_CHECK,
        p.CMD_SET_TIME,
    ]
    assert fountain.authenticated
    assert fountain.clock_set
    assert client.secret is not None and len(client.secret) == 8
    assert client.device_id == 0x12345678
    assert client.serial == "CTW3TEST000001"
    assert client.firmware == "3.41"
    assert state["battery_percent"] == 88
    assert state["filter_percent"] == 80


async def test_stored_secret_skips_claim(fountain, patched_connection):
    """A known-good secret authenticates without rewriting cmd 73."""
    secret = bytes(range(8))
    fountain.stored_secret = secret

    client = PetkitFountain("AA:BB:CC:DD:EE:FF", "CTW3", secret=secret)
    await client.async_poll(BLE_DEVICE)

    assert p.CMD_INIT_DEVICE not in _cmds(fountain)
    assert fountain.authenticated


async def test_stale_secret_triggers_reclaim(fountain, patched_connection):
    """If the device was re-bound elsewhere, the client re-claims it once."""
    fountain.stored_secret = bytes(range(8))

    client = PetkitFountain("AA:BB:CC:DD:EE:FF", "CTW3", secret=b"\xde\xad\xbe\xef" * 2)
    await client.async_poll(BLE_DEVICE)

    cmds = _cmds(fountain)
    assert cmds.count(p.CMD_SECURITY_CHECK) == 2  # rejected, then accepted
    assert p.CMD_INIT_DEVICE in cmds
    assert fountain.authenticated


async def test_claim_failure_surfaces(fountain, patched_connection, monkeypatch):
    """A device that refuses the claim raises rather than silently degrading."""

    def _refuse(frame):
        fountain.received.append(frame)
        if frame.cmd == p.CMD_IDENTITY:
            body = fountain.device_id.to_bytes(8, "big") + b"X" * 14
            return p.build_frame(frame.cmd, body, msg_type=p.TYPE_RESPONSE)
        return p.build_frame(frame.cmd, b"\x00", msg_type=p.TYPE_RESPONSE)

    monkeypatch.setattr(fountain, "handle", _refuse)
    client = PetkitFountain("AA:BB:CC:DD:EE:FF", "CTW3")

    with pytest.raises(PetkitAuthError):
        await client.async_poll(BLE_DEVICE)


async def test_derived_values(fountain, patched_connection):
    """Cloud-side estimates are reproduced locally from pump runtime."""
    client = PetkitFountain("AA:BB:CC:DD:EE:FF", "CTW3")
    state = await client.async_poll(BLE_DEVICE)

    # 3600s at 1.5 L/min over the CTW3 divisor of 3.0 -> 30 L
    assert state["water_purified_today"] == pytest.approx(30.0)
    assert state["energy_consumed"] > 0
    assert state["filter_days_left"] > 0


async def test_runtime_jitter_does_not_walk_a_total_backwards(
    fountain, patched_connection
):
    """A second lost off a runtime counter holds, rather than dipping."""
    client = PetkitFountain("AA:BB:CC:DD:EE:FF", "CTW3")
    first = await client.async_poll(BLE_DEVICE)

    fountain.pump_runtime -= 1
    fountain.pump_runtime_today -= 1
    second = await client.async_poll(BLE_DEVICE)

    assert second["pump_runtime"] == first["pump_runtime"]
    assert second["pump_runtime_today"] == first["pump_runtime_today"]
    assert second["water_purified"] == first["water_purified"]
    assert second["water_purified_today"] == first["water_purified_today"]
    assert second["energy_consumed"] == first["energy_consumed"]

    # The held figure is a floor, not a freeze: growth still comes through.
    fountain.pump_runtime += 60
    fountain.pump_runtime_today += 60
    third = await client.async_poll(BLE_DEVICE)

    assert third["pump_runtime"] == first["pump_runtime"] + 59
    assert third["water_purified"] > first["water_purified"]


async def test_daily_counter_reset_is_let_through(fountain, patched_connection):
    """Midnight zeroes the daily runtime; that is a reset, not jitter."""
    client = PetkitFountain("AA:BB:CC:DD:EE:FF", "CTW3")
    await client.async_poll(BLE_DEVICE)

    fountain.pump_runtime_today = 0
    state = await client.async_poll(BLE_DEVICE)

    assert state["pump_runtime_today"] == 0
    assert state["water_purified_today"] == 0
    # The lifetime counter kept climbing across the same boundary.
    assert state["pump_runtime"] == 100_000


async def test_pushed_runtime_is_held_too(fountain, patched_connection):
    """The clamp covers device-initiated updates, not just polls."""
    client = PetkitFountain("AA:BB:CC:DD:EE:FF", "CTW3")
    await client.async_poll(BLE_DEVICE)

    await client._connect(BLE_DEVICE)
    fountain.pump_runtime -= 1
    patched_connection._emit(fountain.push_status())

    assert client.state["pump_runtime"] == 100_000


async def test_set_mode_emits_correct_frame(fountain, patched_connection):
    """Switching to smart mode sends cmd 220 with selector 1."""
    client = PetkitFountain("AA:BB:CC:DD:EE:FF", "CTW3")
    await client.async_poll(BLE_DEVICE)
    fountain.received.clear()

    await client.async_set_mode(BLE_DEVICE, MODE_SMART)

    sent = next(f for f in fountain.received if f.cmd == p.CMD_SET_MODE)
    assert sent.payload == bytes((1, 2, p.SELECTOR_POWER_MODE))
    assert fountain.mode == 2


async def test_pause_uses_run_selector(fountain, patched_connection):
    """Pausing keeps power on and flips the run flag via selector 2."""
    client = PetkitFountain("AA:BB:CC:DD:EE:FF", "CTW3")
    await client.async_poll(BLE_DEVICE)
    fountain.received.clear()

    await client.async_set_paused(BLE_DEVICE, True)

    sent = next(f for f in fountain.received if f.cmd == p.CMD_SET_MODE)
    assert sent.payload == bytes((1, 0, p.SELECTOR_RUN_PAUSE))
    assert fountain.run_status == 0
    assert fountain.power_status == 1


async def test_power_off_preserves_mode(fountain, patched_connection):
    """Turning off carries the current mode rather than resetting it."""
    client = PetkitFountain("AA:BB:CC:DD:EE:FF", "CTW3")
    fountain.mode = 2
    await client.async_poll(BLE_DEVICE)
    fountain.received.clear()

    await client.async_set_power(BLE_DEVICE, False)

    sent = next(f for f in fountain.received if f.cmd == p.CMD_SET_MODE)
    assert sent.payload == bytes((0, 2, p.SELECTOR_POWER_MODE))


async def test_settings_write_preserves_untouched_fields(fountain, patched_connection):
    """Changing one setting must not zero the rest of the 12-byte block."""
    client = PetkitFountain("AA:BB:CC:DD:EE:FF", "CTW3")
    await client.async_poll(BLE_DEVICE)
    original = bytes(fountain.settings)

    await client.async_update_settings(BLE_DEVICE, light_brightness=3)

    written = bytes(fountain.settings)
    assert len(written) == 12
    assert written[7] == 3  # brightness updated
    assert written[0] == original[0] == 15  # smart run time preserved
    assert written[1] == original[1] == 30
    assert written[2:6] == original[2:6]  # battery timings preserved
    assert written[10:12] == original[10:12]  # proximity switches preserved


async def test_reset_filter(fountain, patched_connection):
    client = PetkitFountain("AA:BB:CC:DD:EE:FF", "CTW3")
    await client.async_poll(BLE_DEVICE)

    await client.async_reset_filter(BLE_DEVICE)

    assert fountain.filter_percent == 100
    assert client.state["filter_percent"] == 100


async def test_push_status_updates_state_and_acks(fountain, patched_connection):
    """An unsolicited cmd 230 updates state and is acknowledged."""
    received: list[dict] = []
    client = PetkitFountain("AA:BB:CC:DD:EE:FF", "CTW3")
    client.register_push_callback(received.append)

    await client.async_poll(BLE_DEVICE)

    # Reconnect so the ack has a live link, then inject a push.
    await client._connect(BLE_DEVICE)
    fountain.power_status = 0
    patched_connection._emit(fountain.push_status())

    assert received, "push callback was not invoked"
    assert received[-1]["power_status"] == 0
    assert client.state["power_status"] == 0


async def test_push_does_not_clobber_proximity_settings(fountain, patched_connection):
    """cmd 230 omits the proximity bytes; they must survive a later write."""
    client = PetkitFountain("AA:BB:CC:DD:EE:FF", "CTW3")
    await client.async_poll(BLE_DEVICE)
    assert client.settings.smart_proximity == 1
    assert client.settings.battery_proximity == 1

    await client._connect(BLE_DEVICE)
    patched_connection._emit(fountain.push_status())

    assert client.settings.smart_proximity == 1
    assert client.settings.battery_proximity == 1


async def test_fragmented_responses(monkeypatch, fountain):
    """Responses split across small notifications still reassemble."""
    from custom_components.petkit_ble import device as device_module
    from tests.conftest import FakeBleakClient

    client_obj = FakeBleakClient(fountain, mtu=7)

    async def _establish(_cls, _dev, _name, _cb, **_kw):
        client_obj.is_connected = True
        return client_obj

    monkeypatch.setattr(device_module, "establish_connection", _establish)

    client = PetkitFountain("AA:BB:CC:DD:EE:FF", "CTW3")
    state = await client.async_poll(BLE_DEVICE)

    assert state["battery_percent"] == 88
    assert client.serial == "CTW3TEST000001"


async def test_connect_does_not_reuse_cached_services(monkeypatch, fountain):
    """Service discovery is repeated on every connection.

    Restoring cached GATT services leaves this fountain unable to answer: the
    link comes up and the first command of the session times out instead. The
    flag is the whole fix, so a refactor must not drop it silently.
    """
    from custom_components.petkit_ble import device as device_module
    from tests.conftest import FakeBleakClient

    client_obj = FakeBleakClient(fountain)
    seen: dict = {}

    async def _establish(_cls, _dev, _name, _cb, **kwargs):
        seen.update(kwargs)
        client_obj.is_connected = True
        return client_obj

    monkeypatch.setattr(device_module, "establish_connection", _establish)

    client = PetkitFountain("AA:BB:CC:DD:EE:FF", "CTW3")
    await client.async_poll(BLE_DEVICE)

    assert seen["use_services_cache"] is False


# --- buffered history sync ----------------------------------------------


async def test_history_records_reach_the_callback(fountain, patched_connection):
    """A poll drains the visits the fountain buffered while we were away."""
    fountain.history = [(1000, 25), (2000, 40), (3000, 15)]
    received: list = []

    client = PetkitFountain("AA:BB:CC:DD:EE:FF", "CTW3")
    client.register_history_callback(received.extend)
    await client.async_poll(BLE_DEVICE)

    assert p.CMD_HISTORY in _cmds(fountain)
    assert [r.raw_time for r in received] == [1000, 2000, 3000]
    assert [r.stay_seconds for r in received] == [25, 40, 15]


async def test_history_is_acknowledged(fountain, patched_connection):
    """The device's checkpoint and end markers both get a response."""
    fountain.history = [(1000, 25), (2000, 40)]

    client = PetkitFountain("AA:BB:CC:DD:EE:FF", "CTW3")
    await client.async_poll(BLE_DEVICE)
    await asyncio.sleep(0)  # let the queued ack writes run

    replies = [
        f
        for f in fountain.received
        if f.cmd in (p.CMD_STREAM_ACK, p.CMD_STREAM_END) and f.type == p.TYPE_RESPONSE
    ]
    assert {f.cmd for f in replies} == {p.CMD_STREAM_ACK, p.CMD_STREAM_END}

    ack = next(f for f in replies if f.cmd == p.CMD_STREAM_ACK)
    # One chunk of two records -> only index 0, so the top bit is set.
    assert ack.payload == (1 << 31).to_bytes(4, "big")


async def test_history_spanning_multiple_chunks(fountain, patched_connection):
    """Records split across stream frames reassemble in order."""
    fountain.history = [(1000 + i * 100, 10 + i) for i in range(9)]
    received: list = []

    client = PetkitFountain("AA:BB:CC:DD:EE:FF", "CTW3")
    client.register_history_callback(received.extend)
    await client.async_poll(BLE_DEVICE)

    assert len(received) == 9
    assert [r.raw_time for r in received] == [1000 + i * 100 for i in range(9)]


async def test_empty_history_is_harmless(fountain, patched_connection):
    fountain.history = []
    received: list = []

    client = PetkitFountain("AA:BB:CC:DD:EE:FF", "CTW3")
    client.register_history_callback(received.extend)
    state = await client.async_poll(BLE_DEVICE)

    assert received == []
    assert state["battery_percent"] == 88  # poll still succeeded


# --- connection lifecycle ------------------------------------------------


async def test_link_is_held_open_between_polls(fountain, patched_connection):
    """Mains-powered fountains keep the link, so commands stay instant."""
    client = PetkitFountain("AA:BB:CC:DD:EE:FF", "CTW3")
    await client.async_poll(BLE_DEVICE)
    assert client.hold_link  # fake reports electric_status 1
    assert patched_connection.is_connected

    fountain.received.clear()
    await client.async_poll(BLE_DEVICE)

    # No second handshake: the session survived.
    assert p.CMD_IDENTITY not in _cmds(fountain)
    assert p.CMD_SECURITY_CHECK not in _cmds(fountain)


async def test_battery_power_releases_the_link(fountain, patched_connection):
    """On battery the fountain should not be kept awake by an open link."""
    client = PetkitFountain("AA:BB:CC:DD:EE:FF", "CTW3")
    await client.async_poll(BLE_DEVICE)

    client.state["electric_status"] = 0
    assert not client.hold_link


async def test_keep_alive_override(fountain, patched_connection):
    client = PetkitFountain("AA:BB:CC:DD:EE:FF", "CTW3", keep_alive="never")
    await client.async_poll(BLE_DEVICE)
    assert not client.hold_link

    client = PetkitFountain("AA:BB:CC:DD:EE:FF", "CTW3", keep_alive="always")
    await client.async_poll(BLE_DEVICE)
    client.state["electric_status"] = 0
    assert client.hold_link


async def test_dropped_link_is_rebuilt(monkeypatch, fountain):
    """A mid-command disconnect reconnects instead of surfacing an error."""
    from custom_components.petkit_ble import device as device_module
    from tests.conftest import FakeBleakClient

    clients: list[FakeBleakClient] = []
    fail_once = {"done": False}

    async def _establish(_cls, _dev, _name, _cb, **_kw):
        client = FakeBleakClient(fountain)
        original = client.write_gatt_char

        async def _write(uuid, data, response=False):
            if not fail_once["done"]:
                fail_once["done"] = True
                client.is_connected = False
                raise BleakError("peripheral went away")
            await original(uuid, data, response=response)

        client.write_gatt_char = _write
        clients.append(client)
        return client

    monkeypatch.setattr(device_module, "establish_connection", _establish)
    monkeypatch.setattr(device_module, "RECONNECT_DELAY", 0)

    client = PetkitFountain("AA:BB:CC:DD:EE:FF", "CTW3")
    state = await client.async_poll(BLE_DEVICE)

    assert len(clients) == 2, "should have reconnected once"
    assert state["battery_percent"] == 88


async def test_persistent_failure_raises(monkeypatch, fountain):
    from custom_components.petkit_ble import device as device_module

    async def _establish(*_args, **_kwargs):
        raise BleakError("no route to device")

    monkeypatch.setattr(device_module, "establish_connection", _establish)
    monkeypatch.setattr(device_module, "RECONNECT_DELAY", 0)

    client = PetkitFountain("AA:BB:CC:DD:EE:FF", "CTW3")
    with pytest.raises(PetkitConnectionError):
        await client.async_poll(BLE_DEVICE)


async def test_stream_is_started_with_cmd_80(fountain, patched_connection):
    """cmd 212 only reports what is waiting; cmd 80 makes it flow.

    Regression test: without this the fountain answers cmd 212 and then stays
    silent, so no history ever arrives.
    """
    fountain.history = [(1000, 25), (2000, 40)]

    client = PetkitFountain("AA:BB:CC:DD:EE:FF", "CTW3")
    await client.async_poll(BLE_DEVICE)

    cmds = _cmds(fountain)
    assert p.CMD_HISTORY in cmds
    assert p.CMD_STREAM_SETTING in cmds
    assert cmds.index(p.CMD_HISTORY) < cmds.index(p.CMD_STREAM_SETTING)
    # Window size then package length, both big-endian uint32.
    assert fountain.stream_setting == (32).to_bytes(4, "big") + (1).to_bytes(4, "big")


async def test_empty_buffer_skips_the_stream(fountain, patched_connection):
    """A fountain with nothing buffered should not be asked to send."""
    fountain.history = []
    received: list = []

    client = PetkitFountain("AA:BB:CC:DD:EE:FF", "CTW3")
    client.register_history_callback(received.extend)
    state = await client.async_poll(BLE_DEVICE)

    assert p.CMD_HISTORY in _cmds(fountain)
    assert p.CMD_STREAM_SETTING not in _cmds(fountain)
    assert received == []
    assert state["battery_percent"] == 88


async def test_history_header_parsing_matches_the_device(fountain, patched_connection):
    """The observed 0100000000 reply means ok with nothing pending."""
    header = p.parse_history_header(bytes.fromhex("0100000000"))
    assert header == {"ok": True, "pending_bytes": 0}

    header = p.parse_history_header(bytes.fromhex("010000004E"))
    assert header["pending_bytes"] == 78  # 13 records


async def test_poll_works_without_a_ble_device_when_connected(fountain, patched_connection):
    """A connected fountain stops advertising, so no BLEDevice is available.

    Regression test: requiring one made every entity flick to unavailable once
    per poll while the link was held open.
    """
    client = PetkitFountain("AA:BB:CC:DD:EE:FF", "CTW3")
    await client.async_poll(BLE_DEVICE)
    assert client.is_connected

    # The registry can no longer find it, so the coordinator passes None.
    state = await client.async_poll(None)
    assert state["battery_percent"] == 88


async def test_poll_without_device_or_connection_fails(monkeypatch, fountain):
    """With nothing to reuse, being absent from the registry is a real error."""
    from custom_components.petkit_ble import device as device_module

    async def _establish(*_args, **_kwargs):
        raise AssertionError("should not try to connect without a device")

    monkeypatch.setattr(device_module, "establish_connection", _establish)
    monkeypatch.setattr(device_module, "RECONNECT_DELAY", 0)

    client = PetkitFountain("AA:BB:CC:DD:EE:FF", "CTW3")
    assert not client.is_connected
    with pytest.raises(PetkitConnectionError):
        await client.async_poll(None)


async def test_is_connected_tracks_teardown(fountain, patched_connection):
    client = PetkitFountain("AA:BB:CC:DD:EE:FF", "CTW3")
    await client.async_poll(BLE_DEVICE)
    assert client.is_connected

    await client.async_disconnect()
    assert not client.is_connected


# --- multiple Bluetooth proxies ------------------------------------------


async def test_retry_resolves_a_fresh_device(monkeypatch, fountain):
    """A retry must be able to go out through a different proxy.

    A BLEDevice is bound to the proxy that last saw the fountain, so reusing
    one pins every attempt to a proxy that may be down.
    """
    from custom_components.petkit_ble import device as device_module
    from tests.conftest import FakeBleakClient

    dead_proxy = object()
    live_proxy = object()
    provider_calls = {"n": 0}
    used: list = []

    def _provider():
        provider_calls["n"] += 1
        # First lookup hands out the dead proxy, later ones a healthy proxy.
        return dead_proxy if provider_calls["n"] == 1 else live_proxy

    async def _establish(_cls, dev, _name, _cb, **_kw):
        used.append(dev)
        if dev is dead_proxy:
            raise BleakError("proxy is down")
        return FakeBleakClient(fountain)

    monkeypatch.setattr(device_module, "establish_connection", _establish)
    monkeypatch.setattr(device_module, "RECONNECT_DELAY", 0)

    client = PetkitFountain(
        "AA:BB:CC:DD:EE:FF", "CTW3", device_provider=_provider
    )
    state = await client.async_poll(None)

    assert used == [dead_proxy, live_proxy], "retry should use a fresh device"
    assert state["battery_percent"] == 88


async def test_provider_result_beats_the_passed_device(monkeypatch, fountain):
    """A freshly resolved device wins over a possibly stale one."""
    from custom_components.petkit_ble import device as device_module
    from tests.conftest import FakeBleakClient

    stale = object()
    fresh = object()
    used: list = []

    async def _establish(_cls, dev, _name, _cb, **_kw):
        used.append(dev)
        return FakeBleakClient(fountain)

    monkeypatch.setattr(device_module, "establish_connection", _establish)

    client = PetkitFountain(
        "AA:BB:CC:DD:EE:FF", "CTW3", device_provider=lambda: fresh
    )
    await client.async_poll(stale)

    assert used == [fresh]


async def test_falls_back_when_provider_returns_nothing(monkeypatch, fountain):
    """If no proxy sees it, the device we were handed is still worth a try."""
    from custom_components.petkit_ble import device as device_module
    from tests.conftest import FakeBleakClient

    handed = object()
    used: list = []

    async def _establish(_cls, dev, _name, _cb, **_kw):
        used.append(dev)
        return FakeBleakClient(fountain)

    monkeypatch.setattr(device_module, "establish_connection", _establish)

    client = PetkitFountain("AA:BB:CC:DD:EE:FF", "CTW3", device_provider=lambda: None)
    await client.async_poll(handed)

    assert used == [handed]


async def test_provider_errors_do_not_break_a_poll(monkeypatch, fountain):
    from custom_components.petkit_ble import device as device_module
    from tests.conftest import FakeBleakClient

    handed = object()

    async def _establish(_cls, dev, _name, _cb, **_kw):
        return FakeBleakClient(fountain)

    def _boom():
        raise RuntimeError("registry exploded")

    monkeypatch.setattr(device_module, "establish_connection", _establish)

    client = PetkitFountain("AA:BB:CC:DD:EE:FF", "CTW3", device_provider=_boom)
    state = await client.async_poll(handed)
    assert state["battery_percent"] == 88
