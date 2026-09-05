"""Tests for the logbook platform."""

from __future__ import annotations

from custom_components.petkit_ble.const import DOMAIN, EVENT_VISIT
from custom_components.petkit_ble.logbook import async_describe_events


class FakeEvent:
    def __init__(self, data):
        self.data = data


def test_visit_events_are_described():
    """A fired event only reaches the logbook if it is described."""
    registered: dict = {}

    def async_describe_event(domain, event_name, describe):
        registered[(domain, event_name)] = describe

    async_describe_events(None, async_describe_event)

    assert (DOMAIN, EVENT_VISIT) in registered
    describe = registered[(DOMAIN, EVENT_VISIT)]

    entry = describe(FakeEvent({"name": "Eversweet Max", "seconds": 51}))
    assert entry["name"] == "Eversweet Max"
    assert "51s" in entry["message"]


def test_a_visit_without_a_duration_still_reads():
    describe_holder: dict = {}
    async_describe_events(None, lambda d, e, f: describe_holder.setdefault("f", f))

    entry = describe_holder["f"](FakeEvent({}))
    assert entry["name"] == "Fountain"
    assert "unknown" in entry["message"]
