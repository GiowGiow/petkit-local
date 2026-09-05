"""Tests for how the coordinator starts polling after a Home Assistant restart."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from unittest.mock import MagicMock

import pytest
from homeassistant.components import bluetooth

from custom_components.petkit_ble.coordinator import PetkitBleCoordinator
from custom_components.petkit_ble.device import PetkitFountain

pytestmark = pytest.mark.asyncio

ADDRESS = "AA:BB:CC:DD:EE:FF"
BLE_DEVICE = object()


@pytest.fixture
async def coordinator(monkeypatch):
    """A coordinator whose fountain answers polls without touching Bluetooth."""
    hass = MagicMock()
    hass.loop = asyncio.get_running_loop()
    # Stop DataUpdateCoordinator from arming its own timer during the test.
    hass.is_stopping = True
    entry = MagicMock(entry_id="test")

    fountain = PetkitFountain(address=ADDRESS, alias="CTW3")

    async def _poll(_device):
        return {"power_status": 1}

    monkeypatch.setattr(fountain, "async_poll", _poll)
    monkeypatch.setattr(bluetooth, "async_last_service_info", lambda *a, **k: None)

    made = PetkitBleCoordinator(hass, entry, fountain, 120)
    # The store would otherwise try to write through the mock hass.
    monkeypatch.setattr(made, "_save_visits", lambda: None)
    return made


def _sighting(monkeypatch, seen: bool) -> None:
    """Decide whether the Bluetooth registry can resolve the fountain."""
    monkeypatch.setattr(
        bluetooth,
        "async_ble_device_from_address",
        lambda *a, **k: BLE_DEVICE if seen else None,
    )


async def test_polls_at_once_when_already_in_range(coordinator, monkeypatch):
    """A reload mid-session must not sit through the startup grace period."""
    _sighting(monkeypatch, seen=True)

    async def _never(*args, **kwargs):
        raise AssertionError("should not have waited for an advertisement")

    monkeypatch.setattr(bluetooth, "async_process_advertisements", _never)

    await coordinator.async_start()

    assert coordinator.data == {"power_status": 1}
    assert coordinator.update_interval == timedelta(seconds=120)


async def test_holds_off_until_a_proxy_hears_the_fountain(coordinator, monkeypatch):
    """The first poll waits for the proxy instead of failing against an empty registry."""
    _sighting(monkeypatch, seen=False)
    heard = asyncio.Event()

    async def _wait(*args, **kwargs):
        await heard.wait()
        _sighting(monkeypatch, seen=True)
        return MagicMock()

    monkeypatch.setattr(bluetooth, "async_process_advertisements", _wait)

    task = asyncio.ensure_future(coordinator.async_start())
    await asyncio.sleep(0)

    # Nothing has heard the fountain yet, so nothing has been polled either.
    assert coordinator.data is None
    assert coordinator.update_interval is None

    heard.set()
    await task

    assert coordinator.data == {"power_status": 1}
    assert coordinator.last_update_success


async def test_polls_anyway_once_the_grace_period_expires(coordinator, monkeypatch):
    """A fountain that is genuinely unreachable still gets reported as such."""
    _sighting(monkeypatch, seen=False)

    async def _timeout(*args, **kwargs):
        raise TimeoutError

    monkeypatch.setattr(bluetooth, "async_process_advertisements", _timeout)

    await coordinator.async_start()

    assert coordinator.data is None
    assert not coordinator.last_update_success
    assert "not in range" in str(coordinator.last_exception)


async def test_last_connected_survives_a_restart(coordinator, monkeypatch):
    """A restart must not turn "reached an hour ago" into "never reached".

    The link state itself is per-process and starts empty, so the timestamp has
    to come back from the store or the sensor lies about a fountain it has
    talked to before.
    """
    from datetime import UTC, datetime

    when = datetime(2026, 9, 5, 16, 5, 6, tzinfo=UTC)
    coordinator.fountain.last_connected = when

    saved = coordinator._persisted_state()
    assert saved["last_connected"] == when.isoformat()

    # A fresh coordinator is what a restart produces: nothing in memory.
    coordinator.fountain.last_connected = None

    async def _load():
        return saved

    monkeypatch.setattr(coordinator._store, "async_load", _load)
    await coordinator.async_load_visits()

    assert coordinator.fountain.last_connected == when


async def test_an_unchanged_connection_queues_no_write(coordinator, monkeypatch):
    """Polling every two minutes must not write to disk every two minutes."""
    writes: list[int] = []
    monkeypatch.setattr(coordinator, "_save_visits", lambda: writes.append(1))
    _sighting(monkeypatch, True)

    from datetime import UTC, datetime

    coordinator.fountain.last_connected = datetime(2026, 9, 5, 16, 5, tzinfo=UTC)
    coordinator._saved_connected = coordinator.fountain.last_connected

    await coordinator._async_update_data()
    assert writes == []

    # A new session is worth a write.
    coordinator.fountain.last_connected = datetime(2026, 9, 5, 17, 0, tzinfo=UTC)
    await coordinator._async_update_data()
    assert len(writes) == 1

