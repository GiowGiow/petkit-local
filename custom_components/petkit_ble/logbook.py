"""Logbook entries for PetKit BLE.

Firing an event is not enough to reach the logbook: it only renders events it
has been taught to describe. Without this, petkit_ble_visit is visible to
automations and to the event bus and to nothing a person looks at.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from homeassistant.core import Event, HomeAssistant, callback

from .const import DOMAIN, EVENT_VISIT


@callback
def async_describe_events(
    hass: HomeAssistant,
    async_describe_event: Callable[[str, str, Callable[[Event], dict[str, Any]]], None],
) -> None:
    """Register a description for each visit the fountain reports."""

    @callback
    def describe_visit(event: Event) -> dict[str, Any]:
        seconds = event.data.get("seconds")
        duration = "an unknown time" if seconds is None else f"{seconds}s"
        return {
            "name": event.data.get("name") or "Fountain",
            "message": f"was visited for {duration}",
            "icon": "mdi:cat",
        }

    async_describe_event(DOMAIN, EVENT_VISIT, describe_visit)
