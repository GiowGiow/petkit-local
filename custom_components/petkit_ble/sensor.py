"""Sensors for PetKit BLE fountains."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import (
    PERCENTAGE,
    EntityCategory,
    UnitOfElectricPotential,
    UnitOfEnergy,
    UnitOfTime,
    UnitOfVolume,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util

from . import PetkitBleConfigEntry
from .const import BRIGHTNESS_OPTIONS, MODE_OPTIONS
from .entity import PetkitBleEntity

RUN_STATES = {0: "idle", 1: "running", 2: "paused"}


@dataclass(frozen=True, kw_only=True)
class PetkitSensorDescription(SensorEntityDescription):
    """Describes a PetKit sensor."""

    value_fn: Callable[[dict[str, Any]], Any] | None = None


SENSORS: tuple[PetkitSensorDescription, ...] = (
    PetkitSensorDescription(
        key="battery_percent",
        translation_key="battery_percent",
        device_class=SensorDeviceClass.BATTERY,
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    PetkitSensorDescription(
        key="battery_voltage",
        translation_key="battery_voltage",
        device_class=SensorDeviceClass.VOLTAGE,
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
    ),
    PetkitSensorDescription(
        key="supply_voltage",
        translation_key="supply_voltage",
        device_class=SensorDeviceClass.VOLTAGE,
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
    ),
    PetkitSensorDescription(
        key="filter_percent",
        translation_key="filter_percent",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:air-filter",
    ),
    PetkitSensorDescription(
        key="filter_days_left",
        translation_key="filter_days_left",
        native_unit_of_measurement=UnitOfTime.DAYS,
        icon="mdi:calendar-clock",
    ),
    PetkitSensorDescription(
        key="pump_runtime_today",
        translation_key="pump_runtime_today",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.SECONDS,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_unit_of_measurement=UnitOfTime.MINUTES,
        icon="mdi:pump",
    ),
    PetkitSensorDescription(
        key="pump_runtime",
        translation_key="pump_runtime",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.SECONDS,
        state_class=SensorStateClass.TOTAL_INCREASING,
        suggested_unit_of_measurement=UnitOfTime.HOURS,
        entity_category=EntityCategory.DIAGNOSTIC,
        icon="mdi:pump",
    ),
    PetkitSensorDescription(
        key="water_purified_today",
        translation_key="water_purified_today",
        device_class=SensorDeviceClass.WATER,
        native_unit_of_measurement=UnitOfVolume.LITERS,
        # WATER rejects MEASUREMENT; the daily figure resets, so it is
        # TOTAL_INCREASING rather than a plain TOTAL.
        state_class=SensorStateClass.TOTAL_INCREASING,
        suggested_display_precision=2,
        icon="mdi:water-check",
    ),
    PetkitSensorDescription(
        key="water_purified",
        translation_key="water_purified",
        device_class=SensorDeviceClass.WATER,
        native_unit_of_measurement=UnitOfVolume.LITERS,
        state_class=SensorStateClass.TOTAL_INCREASING,
        suggested_display_precision=1,
        entity_category=EntityCategory.DIAGNOSTIC,
        icon="mdi:water-check",
    ),
    PetkitSensorDescription(
        key="energy_consumed",
        translation_key="energy_consumed",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        state_class=SensorStateClass.TOTAL_INCREASING,
        suggested_display_precision=4,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    PetkitSensorDescription(
        key="energy_consumed_today",
        translation_key="energy_consumed_today",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        state_class=SensorStateClass.TOTAL_INCREASING,
        suggested_display_precision=4,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    PetkitSensorDescription(
        key="mode",
        translation_key="mode",
        device_class=SensorDeviceClass.ENUM,
        options=list(MODE_OPTIONS.values()),
        value_fn=lambda data: MODE_OPTIONS.get(data.get("mode")),
        icon="mdi:auto-mode",
    ),
    PetkitSensorDescription(
        key="running_status",
        translation_key="running_status",
        device_class=SensorDeviceClass.ENUM,
        options=list(RUN_STATES.values()),
        value_fn=lambda data: RUN_STATES.get(data.get("running_status")),
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    PetkitSensorDescription(
        key="light_brightness_level",
        translation_key="light_brightness_level",
        device_class=SensorDeviceClass.ENUM,
        options=list(BRIGHTNESS_OPTIONS.values()),
        value_fn=lambda data: BRIGHTNESS_OPTIONS.get(data.get("light_brightness")),
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        icon="mdi:brightness-6",
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: PetkitBleConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up sensors."""
    coordinator = entry.runtime_data
    entities: list[SensorEntity] = [
        PetkitBleSensor(coordinator, description) for description in SENSORS
    ]
    entities.append(PetkitBleRssiSensor(coordinator))
    entities.append(PetkitBleConnectionSensor(coordinator))
    entities.append(PetkitBleVisitCountSensor(coordinator))
    entities.append(PetkitBleVisitDurationSensor(coordinator))
    entities.append(PetkitBleVisitTotalSensor(coordinator))
    entities.append(PetkitBleVisitTotalDurationSensor(coordinator))
    entities.append(PetkitBleLastVisitSensor(coordinator))
    entities.append(PetkitBleAverageVisitSensor(coordinator))
    async_add_entities(entities)


class PetkitBleSensor(PetkitBleEntity, SensorEntity):
    """A value read off the fountain."""

    entity_description: PetkitSensorDescription

    def __init__(self, coordinator, description: PetkitSensorDescription) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description

    @property
    def native_value(self) -> Any:
        data = self.coordinator.data or {}
        if self.entity_description.value_fn is not None:
            return self.entity_description.value_fn(data)
        return data.get(self.entity_description.key)


class PetkitBleVisitCountSensor(PetkitBleEntity, SensorEntity):
    """How many times a pet has used the fountain today.

    State is persisted by the coordinator, not through this entity: overriding
    a RestoreSensor's extra data broke async_get_last_sensor_data, so the count
    silently came back as zero after every restart.
    """

    _attr_translation_key = "pet_visits_today"
    _attr_icon = "mdi:cat"
    _attr_state_class = SensorStateClass.TOTAL_INCREASING

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator, "pet_visits_today")

    @property
    def native_value(self) -> int:
        return self.coordinator.visits.count

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"visit_in_progress": self.coordinator.visits.in_progress}

    @property
    def available(self) -> bool:
        # Derived locally, so it stays meaningful even if a poll fails.
        return True


class PetkitBleVisitDurationSensor(PetkitBleEntity, SensorEntity):
    """Total time a pet has spent at the fountain today."""

    _attr_translation_key = "pet_drink_duration_today"
    _attr_icon = "mdi:timer-outline"
    _attr_device_class = SensorDeviceClass.DURATION
    _attr_native_unit_of_measurement = UnitOfTime.SECONDS
    _attr_suggested_unit_of_measurement = UnitOfTime.MINUTES
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_suggested_display_precision = 1

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator, "pet_drink_duration_today")

    @property
    def native_value(self) -> float:
        # Only completed visits. Adding the one under way made this fall back
        # every time a visit closed, because the live figure keeps climbing
        # through the grace period while only the shorter measured length is
        # banked. A total that goes down reads as a counter reset and wrecks
        # the long-term statistics.
        return round(self.coordinator.visits.duration.total_seconds(), 1)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        visits = self.coordinator.visits
        return {
            "visit_in_progress": visits.in_progress,
            "current_visit_seconds": round(
                visits.current_duration(dt_util.now()).total_seconds(), 1
            ),
        }

    @property
    def available(self) -> bool:
        return True


class PetkitBleVisitTotalSensor(PetkitBleEntity, SensorEntity):
    """Lifetime visit count, carried across midnight and restarts."""

    _attr_translation_key = "pet_visits_total"
    _attr_icon = "mdi:cat"
    _attr_state_class = SensorStateClass.TOTAL_INCREASING

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator, "pet_visits_total")

    @property
    def native_value(self) -> int:
        return self.coordinator.visits.total_count

    @property
    def available(self) -> bool:
        return True


class PetkitBleVisitTotalDurationSensor(PetkitBleEntity, SensorEntity):
    """Lifetime drinking time."""

    _attr_translation_key = "pet_drink_duration_total"
    _attr_icon = "mdi:timer-outline"
    _attr_device_class = SensorDeviceClass.DURATION
    _attr_native_unit_of_measurement = UnitOfTime.SECONDS
    _attr_suggested_unit_of_measurement = UnitOfTime.MINUTES
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_suggested_display_precision = 1
    _attr_entity_registry_enabled_default = False

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator, "pet_drink_duration_total")

    @property
    def native_value(self) -> float:
        return round(self.coordinator.visits.total_duration.total_seconds(), 1)

    @property
    def available(self) -> bool:
        return True


class PetkitBleLastVisitSensor(PetkitBleEntity, SensorEntity):
    """When a pet last used the fountain."""

    _attr_translation_key = "last_pet_visit"
    _attr_icon = "mdi:clock-outline"
    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator, "last_pet_visit")

    @property
    def native_value(self):
        return self.coordinator.visits.last_visit

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """The individual visits behind the counters, newest last.

        The fountain logs each visit with its own duration, which is what the
        PetKit app draws its drink history from. Carrying them here lets a card
        render the same timeline without a separate entity per visit.
        """
        return {"visits": self.coordinator.visits.recent}

    @property
    def available(self) -> bool:
        return True


class PetkitBleAverageVisitSensor(PetkitBleEntity, SensorEntity):
    """How long the pet drinks per visit today.

    The app shows this next to the visit count, and it says something the
    count alone does not: six quick sips is a different animal from one long
    drink.
    """

    _attr_translation_key = "average_visit_duration"
    _attr_icon = "mdi:timer-sand"
    _attr_device_class = SensorDeviceClass.DURATION
    _attr_native_unit_of_measurement = UnitOfTime.SECONDS
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 0

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator, "average_visit_duration")

    @property
    def native_value(self) -> float | None:
        visits = self.coordinator.visits
        if not visits.count:
            return 0.0
        return visits.duration.total_seconds() / visits.count

    @property
    def available(self) -> bool:
        return True


class PetkitBleConnectionSensor(PetkitBleEntity, SensorEntity):
    """Whether a Bluetooth link to the fountain is being held right now.

    Reports even when the data is stale, since a dropped link is exactly what
    it exists to show.
    """

    _attr_translation_key = "connection"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = ["connected", "disconnected"]
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator, "connection")

    @property
    def available(self) -> bool:
        return True

    @property
    def native_value(self) -> str:
        return "connected" if self.coordinator.fountain.is_connected else "disconnected"


class PetkitBleRssiSensor(PetkitBleEntity, SensorEntity):
    """Signal strength of the last advertisement we saw."""

    _attr_translation_key = "rssi"
    _attr_device_class = SensorDeviceClass.SIGNAL_STRENGTH
    _attr_native_unit_of_measurement = "dBm"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator, "rssi")

    @property
    def native_value(self) -> int | None:
        return self.coordinator.rssi
