"""Switches for PetKit BLE fountains."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity, SwitchEntityDescription
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import PetkitBleConfigEntry
from .entity import PetkitBleEntity

# Settings-block switches: each maps to one byte of the cmd 221 payload.
SETTING_SWITCHES: tuple[SwitchEntityDescription, ...] = (
    SwitchEntityDescription(
        key="light_switch",
        translation_key="light",
        icon="mdi:lightbulb-outline",
    ),
    SwitchEntityDescription(
        key="dnd_switch",
        translation_key="do_not_disturb",
        icon="mdi:sleep",
    ),
    SwitchEntityDescription(
        key="child_lock",
        translation_key="child_lock",
        entity_category=EntityCategory.CONFIG,
        icon="mdi:lock",
    ),
    SwitchEntityDescription(
        key="smart_proximity",
        translation_key="smart_proximity",
        entity_category=EntityCategory.CONFIG,
        icon="mdi:motion-sensor",
    ),
    SwitchEntityDescription(
        key="battery_proximity",
        translation_key="battery_proximity",
        entity_category=EntityCategory.CONFIG,
        icon="mdi:motion-sensor",
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: PetkitBleConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up switches."""
    coordinator = entry.runtime_data
    entities: list[SwitchEntity] = [
        PetkitBleSettingSwitch(coordinator, description)
        for description in SETTING_SWITCHES
    ]
    entities.append(PetkitBlePowerSwitch(coordinator))
    entities.append(PetkitBlePauseSwitch(coordinator))
    async_add_entities(entities)


class PetkitBlePowerSwitch(PetkitBleEntity, SwitchEntity):
    """Pump power."""

    _attr_translation_key = "power"
    _attr_icon = "mdi:power"

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator, "power")

    @property
    def is_on(self) -> bool | None:
        value = self._value("power_status")
        return None if value is None else bool(value)

    async def async_turn_on(self, **kwargs: Any) -> None:
        self.coordinator.async_assume(power_status=1)
        await self.coordinator.async_run("power", on=True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        self.coordinator.async_assume(power_status=0)
        await self.coordinator.async_run("power", on=False)


class PetkitBlePauseSwitch(PetkitBleEntity, SwitchEntity):
    """Pump running state, separate from power.

    On means running; the fountain stays powered when this is off.
    """

    _attr_translation_key = "running"
    _attr_icon = "mdi:play-pause"

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator, "running")

    @property
    def is_on(self) -> bool | None:
        value = self._value("run_status")
        return None if value is None else bool(value)

    async def async_turn_on(self, **kwargs: Any) -> None:
        self.coordinator.async_assume(run_status=1)
        await self.coordinator.async_run("paused", paused=False)

    async def async_turn_off(self, **kwargs: Any) -> None:
        self.coordinator.async_assume(run_status=0)
        await self.coordinator.async_run("paused", paused=True)


class PetkitBleSettingSwitch(PetkitBleEntity, SwitchEntity):
    """A single byte in the settings block."""

    def __init__(self, coordinator, description: SwitchEntityDescription) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description

    @property
    def is_on(self) -> bool | None:
        value = self._value(self.entity_description.key)
        return None if value is None else bool(value)

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._async_set(1)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._async_set(0)

    async def _async_set(self, value: int) -> None:
        self.coordinator.async_assume(**{self.entity_description.key: value})
        await self.coordinator.async_run(
            "settings", changes={self.entity_description.key: value}
        )
