"""The PetKit BLE integration - local control for Eversweet fountains."""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .const import (
    CONF_ADDRESS,
    CONF_ALIAS,
    CONF_DEVICE_ID,
    CONF_KEEP_ALIVE,
    CONF_SCAN_INTERVAL,
    CONF_SECRET,
    DEFAULT_KEEP_ALIVE,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
)
from .coordinator import PetkitBleCoordinator
from .device import PetkitFountain

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.NUMBER,
    Platform.SELECT,
    Platform.SENSOR,
    Platform.SWITCH,
]

type PetkitBleConfigEntry = ConfigEntry[PetkitBleCoordinator]


async def async_setup_entry(hass: HomeAssistant, entry: PetkitBleConfigEntry) -> bool:
    """Set up a fountain from a config entry."""
    address: str = entry.unique_id or entry.data[CONF_ADDRESS]
    secret_hex: str | None = entry.data.get(CONF_SECRET)
    fountain = PetkitFountain(
        address=address,
        alias=entry.data.get(CONF_ALIAS, "CTW3"),
        secret=bytes.fromhex(secret_hex) if secret_hex else None,
        device_id=entry.data.get(CONF_DEVICE_ID),
        keep_alive=entry.options.get(CONF_KEEP_ALIVE, DEFAULT_KEEP_ALIVE),
    )

    coordinator = PetkitBleCoordinator(
        hass,
        entry,
        fountain,
        entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL),
    )

    # Restore visit statistics before any entity can report a value, so a
    # restart does not briefly publish zero and poison the statistics.
    await coordinator.async_load_visits()

    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_reload_entry))

    # Reaching the fountain can take a while - it may be asleep, or the
    # Bluetooth proxy it lives behind may still be booting. Doing that inline
    # held up Home Assistant startup for minutes, so polling is started in the
    # background and entities stay unavailable until the first poll lands.
    entry.async_create_background_task(
        hass,
        _async_first_refresh(hass, entry, coordinator, bool(secret_hex)),
        f"{DOMAIN} first refresh {address}",
    )
    return True


async def _async_first_refresh(
    hass: HomeAssistant,
    entry: PetkitBleConfigEntry,
    coordinator: PetkitBleCoordinator,
    had_secret: bool,
) -> None:
    """Populate state, then persist anything the handshake taught us."""
    await coordinator.async_start()

    fountain = coordinator.fountain
    # The secret is only known after a successful handshake; persist it so a
    # later restart reuses the same claim instead of re-writing cmd 73.
    if fountain.secret is None or had_secret:
        return

    hass.config_entries.async_update_entry(
        entry,
        data={
            **entry.data,
            CONF_SECRET: fountain.secret.hex(),
            CONF_DEVICE_ID: fountain.device_id,
        },
    )


async def async_unload_entry(hass: HomeAssistant, entry: PetkitBleConfigEntry) -> bool:
    """Unload a config entry."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        # Release the BLE slot; a held-open link would block the next setup.
        await entry.runtime_data.fountain.async_disconnect()
    return unloaded


async def _async_reload_entry(hass: HomeAssistant, entry: PetkitBleConfigEntry) -> None:
    """Reload when options change."""
    await hass.config_entries.async_reload(entry.entry_id)
