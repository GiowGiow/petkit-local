"""Update coordinator for PetKit BLE fountains."""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from bleak.backends.device import BLEDevice
from homeassistant.components import bluetooth
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .const import DOMAIN, EVENT_VISIT
from .device import PetkitAuthError, PetkitConnectionError, PetkitFountain
from .visits import VisitTracker

_LOGGER = logging.getLogger(__name__)

# How many polls in a row must fail before entities are marked unavailable.
MAX_TRANSIENT_FAILURES = 3

# How long to wait for the fountain to be heard before polling regardless.
# Home Assistant brings its integrations up in parallel, and an ESPHome
# Bluetooth proxy usually finishes connecting a minute or two after we are set
# up, so the registry is still empty when we would otherwise poll.
STARTUP_GRACE_PERIOD = 180

STORAGE_VERSION = 1
# Visit statistics are cheap to lose but noisy to write; batch them up.
STORAGE_SAVE_DELAY = 30


class PetkitBleCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Polls one fountain over BLE and relays its pushes."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        fountain: PetkitFountain,
        scan_interval: int,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN} {fountain.address}",
            # Polling stays off until async_start has heard the fountain, so a
            # restart never fires a doomed poll at a Bluetooth registry that
            # has simply not filled in yet.
            update_interval=None,
            config_entry=entry,
        )
        self._scan_interval = timedelta(seconds=scan_interval)
        self.fountain = fountain
        self.rssi: int | None = None
        self.visits = VisitTracker()
        self._failures = 0
        self._store: Store[dict] = Store(
            hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}.visits"
        )
        # Let the client re-resolve the device itself, so a retry can go out
        # through a different Bluetooth proxy rather than the one that just
        # failed.
        fountain.set_device_provider(self._lookup_device)
        fountain.register_push_callback(self._on_push)
        fountain.register_history_callback(self._on_history)

    async def async_start(self) -> None:
        """Wait for the fountain to turn up, then start polling it.

        The proxy a fountain lives behind is itself a Home Assistant device, and
        it reconnects well after this entry is set up. Polling immediately meant
        every restart logged a failure about the fountain being out of range,
        when all that had happened was that nothing had listened for it yet.
        """
        if self._lookup_device() is None:
            await self._async_wait_for_device()
        self.update_interval = self._scan_interval
        await self.async_refresh()

    async def _async_wait_for_device(self) -> None:
        """Hold until some adapter or proxy hears the fountain advertise."""
        address = self.fountain.address
        _LOGGER.debug("%s: waiting for an adapter or proxy to hear it", address)
        try:
            await bluetooth.async_process_advertisements(
                self.hass,
                lambda _service_info: True,
                bluetooth.BluetoothCallbackMatcher(address=address, connectable=True),
                # Passive is enough: the fountain advertises on its own, and
                # this is the same traffic the registry is already built from.
                bluetooth.BluetoothScanningMode.PASSIVE,
                STARTUP_GRACE_PERIOD,
            )
        except TimeoutError:
            # Give up waiting and poll anyway, so a fountain that really is
            # unreachable is reported as such instead of silently never
            # starting.
            _LOGGER.debug(
                "%s: not heard within %ss, polling anyway",
                address,
                STARTUP_GRACE_PERIOD,
            )

    async def async_load_visits(self) -> None:
        """Restore visit statistics before any entity reports a value."""
        stored = await self._store.async_load()
        if stored:
            self.visits.from_dict(stored, dt_util.now().date())
            _LOGGER.debug(
                "%s: restored visits today=%s total=%s",
                self.fountain.address,
                self.visits.count,
                self.visits.total_count,
            )

    @callback
    def _save_visits(self) -> None:
        """Queue a write of the visit statistics."""
        self._store.async_delay_save(self.visits.to_dict, STORAGE_SAVE_DELAY)

    @callback
    def _lookup_device(self) -> BLEDevice | None:
        """Resolve the fountain through whichever adapter or proxy can see it."""
        return bluetooth.async_ble_device_from_address(
            self.hass, self.fountain.address, connectable=True
        )

    @callback
    def _log_sources(self) -> None:
        """Record which proxies can currently reach the fountain."""
        try:
            devices = bluetooth.async_scanner_devices_by_address(
                self.hass, self.fountain.address, connectable=True
            )
        except Exception:  # noqa: BLE001 - diagnostics must not break a poll
            return
        if not devices:
            _LOGGER.debug("%s: no proxy currently sees it", self.fountain.address)
            return
        _LOGGER.debug(
            "%s: visible via %s",
            self.fountain.address,
            ", ".join(
                f"{d.scanner.name or d.scanner.source} (rssi {d.advertisement.rssi})"
                for d in devices
            ),
        )

    @callback
    def _on_push(self, state: dict[str, Any]) -> None:
        """Publish a device-initiated update without waiting for the poll."""
        self._track_visit(state)
        self.async_set_updated_data(dict(state))

    @callback
    def _on_history(self, records: list[Any]) -> None:
        """Bank visit records the fountain recorded while we were away."""
        if not self.visits.ingest(records, dt_util.now()):
            return
        self._announce_visits()
        self._save_visits()
        if self.data is not None:
            self.async_set_updated_data(dict(self.data))

    @callback
    def _announce_visits(self) -> None:
        """Fire one event per newly banked visit.

        The counters say how much the pet drank; these say when. Firing them
        puts each visit in the logbook and lets automations react to a single
        drink, which a running total cannot express.
        """
        pending, self.visits.new_visits = self.visits.new_visits, []
        for visit in pending:
            self.hass.bus.async_fire(
                EVENT_VISIT,
                {
                    "address": self.fountain.address,
                    "name": self.config_entry.title if self.config_entry else None,
                    "at": visit["at"],
                    "seconds": visit["seconds"],
                },
            )

    @callback
    def _track_visit(self, state: dict[str, Any]) -> None:
        """Fold the detection flag into the visit statistics."""
        detected = state.get("detect_status")
        if detected is None:
            return
        if self.visits.update(bool(detected), dt_util.now()):
            self._announce_visits()
            self._save_visits()

    def _ble_device(self) -> BLEDevice | None:
        """Find the fountain, or None if we can carry on without it.

        A connected peripheral usually stops advertising, so once the link is
        held open Home Assistant's registry can no longer produce a BLEDevice
        for it. That is expected, not a failure: the existing connection is
        reused and no lookup is needed. Only when there is nothing to reuse
        does being absent from the registry actually block us.
        """
        device = self._lookup_device()
        if device is not None:
            return device
        if self.fountain.is_connected:
            return None
        self._log_sources()
        raise PetkitConnectionError(
            f"{self.fountain.address} is not in range of any Bluetooth adapter or proxy"
        )

    def _tolerate(self, reason: str) -> dict[str, Any]:
        """Ride out a blip instead of blanking every entity.

        A fountain drops off the Bluetooth registry for a moment whenever the
        proxy it lives behind hiccups. Failing the update on the first miss made
        every sensor flick to unavailable and back, so brief trouble now keeps
        the last known state and only sustained trouble is surfaced.
        """
        self._failures += 1
        if self.data and self._failures < MAX_TRANSIENT_FAILURES:
            _LOGGER.debug(
                "%s: transient failure %s/%s, keeping last state: %s",
                self.fountain.address,
                self._failures,
                MAX_TRANSIENT_FAILURES,
                reason,
            )
            return self.data
        raise UpdateFailed(reason)

    async def _async_update_data(self) -> dict[str, Any]:
        service_info = bluetooth.async_last_service_info(
            self.hass, self.fountain.address, connectable=True
        )
        if service_info is not None:
            self.rssi = service_info.rssi

        try:
            device = self._ble_device()
            state = await self.fountain.async_poll(device)
        except PetkitAuthError as err:
            # A rejected secret will not fix itself; surface it immediately.
            raise UpdateFailed(f"authentication failed: {err}") from err
        except (PetkitConnectionError, TimeoutError) as err:
            return self._tolerate(str(err))

        self._failures = 0
        self._track_visit(state)
        return state

    @callback
    def async_assume(self, **values: Any) -> None:
        """Publish an expected state change straight away.

        A command is a BLE round trip, so waiting for confirmation makes every
        toggle feel broken. Show the intended value now; the refresh that
        follows the command corrects it if the fountain disagreed.
        """
        if self.data is None:
            return
        self.async_set_updated_data({**self.data, **values})

    async def async_run(self, action: str, **kwargs: Any) -> None:
        """Run a command on the fountain, then push fresh state to entities."""
        fountain = self.fountain

        try:
            device = self._ble_device()
            match action:
                case "power":
                    await fountain.async_set_power(device, kwargs["on"])
                case "mode":
                    await fountain.async_set_mode(device, kwargs["mode"])
                case "paused":
                    await fountain.async_set_paused(device, kwargs["paused"])
                case "reset_filter":
                    await fountain.async_reset_filter(device)
                case "settings":
                    await fountain.async_update_settings(device, **kwargs["changes"])
                case _:
                    raise ValueError(f"unknown action {action!r}")
        except (PetkitConnectionError, PetkitAuthError, TimeoutError) as err:
            raise UpdateFailed(f"command {action} failed: {err}") from err

        self.async_set_updated_data(dict(fountain.state))
