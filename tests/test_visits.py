"""Tests for pet visit reconstruction."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from custom_components.petkit_ble.visits import (
    MIN_VISIT_DURATION,
    VISIT_GAP_GRACE,
    VisitTracker,
)

START = datetime(2026, 8, 16, 9, 0, tzinfo=UTC)


def at(seconds: float) -> datetime:
    return START + timedelta(seconds=seconds)


def test_no_visits_when_never_detected():
    tracker = VisitTracker()
    for i in range(10):
        tracker.update(False, at(i * 10))
    assert tracker.count == 0
    assert tracker.duration == timedelta()
    assert tracker.last_visit is None


def test_single_visit_counted_and_timed():
    tracker = VisitTracker()
    tracker.update(True, at(0))
    tracker.update(True, at(20))
    # Gap must exceed the grace period before the visit closes.
    tracker.update(False, at(20 + VISIT_GAP_GRACE.total_seconds() + 1))

    assert tracker.count == 1
    assert tracker.duration == timedelta(seconds=20)
    assert tracker.last_visit is not None


def test_visit_counted_while_still_in_progress():
    """A drink should appear as it happens, not only once it ends."""
    tracker = VisitTracker()
    tracker.update(True, at(0))
    assert tracker.count == 0  # too short to trust yet

    tracker.update(True, at(MIN_VISIT_DURATION.total_seconds() + 1))
    assert tracker.count == 1
    assert tracker.in_progress


def test_momentary_blip_is_ignored():
    """A single stray sample must not become a visit."""
    tracker = VisitTracker()
    tracker.update(True, at(0))
    tracker.update(False, at(VISIT_GAP_GRACE.total_seconds() + 1))

    assert tracker.count == 0
    assert tracker.duration == timedelta()


def test_brief_gap_does_not_split_one_drink():
    """Cats sip in bursts; a short pause is the same visit."""
    tracker = VisitTracker()
    tracker.update(True, at(0))
    tracker.update(True, at(10))
    tracker.update(False, at(15))  # inside the grace period
    tracker.update(True, at(20))
    tracker.update(True, at(30))
    tracker.update(False, at(30 + VISIT_GAP_GRACE.total_seconds() + 1))

    assert tracker.count == 1
    assert tracker.duration == timedelta(seconds=30)


def test_long_gap_splits_into_two_visits():
    tracker = VisitTracker()
    gap = VISIT_GAP_GRACE.total_seconds() + 5

    tracker.update(True, at(0))
    tracker.update(True, at(10))
    tracker.update(False, at(10 + gap))

    tracker.update(True, at(200))
    tracker.update(True, at(215))
    tracker.update(False, at(215 + gap))

    assert tracker.count == 2
    assert tracker.duration == timedelta(seconds=25)


def test_current_duration_climbs_during_a_visit():
    tracker = VisitTracker()
    tracker.update(True, at(0))
    assert tracker.current_duration(at(12)) == timedelta(seconds=12)
    assert tracker.current_duration(at(0)) == timedelta()


def test_current_duration_zero_when_idle():
    tracker = VisitTracker()
    assert tracker.current_duration(at(5)) == timedelta()


def test_totals_reset_at_local_midnight():
    tracker = VisitTracker()
    tracker.update(True, at(0))
    tracker.update(True, at(10))
    tracker.update(False, at(10 + VISIT_GAP_GRACE.total_seconds() + 1))
    assert tracker.count == 1

    tomorrow = START + timedelta(days=1)
    tracker.update(False, tomorrow)

    assert tracker.count == 0
    assert tracker.duration == timedelta()


def test_restore_seeds_todays_totals():
    tracker = VisitTracker()
    tracker.from_dict(
        {
            "count": 4,
            "duration": 125.5,
            "last_visit": at(-60).isoformat(),
            "day": START.date().isoformat(),
        },
        START.date(),
    )
    assert tracker.count == 4
    assert tracker.duration == timedelta(seconds=125.5)

    # A further visit continues from the restored totals.
    tracker.update(True, at(0))
    tracker.update(True, at(10))
    tracker.update(False, at(10 + VISIT_GAP_GRACE.total_seconds() + 1))

    assert tracker.count == 5
    assert tracker.duration == timedelta(seconds=135.5)


# --- device-recorded history (authoritative) ----------------------------


class FakeRecord:
    """Stands in for protocol.WorkRecord."""

    def __init__(self, offset_seconds: float, stay: int, raw: int) -> None:
        self.timestamp = START + timedelta(seconds=offset_seconds)
        self.stay_seconds = stay
        self.raw_time = raw


def test_device_records_are_counted():
    tracker = VisitTracker()
    tracker.update(False, at(0))  # establish today

    changed = tracker.ingest(
        [FakeRecord(60, 25, raw=1001), FakeRecord(300, 40, raw=1002)], at(600)
    )

    assert changed
    assert tracker.count == 2
    assert tracker.duration == timedelta(seconds=65)
    assert tracker.device_backed


def test_device_records_are_deduplicated():
    """The fountain may resend a window it already delivered."""
    tracker = VisitTracker()
    records = [FakeRecord(60, 25, raw=1001)]

    tracker.ingest(records, at(600))
    tracker.ingest(records, at(700))

    assert tracker.count == 1
    assert tracker.duration == timedelta(seconds=25)


def test_device_records_supersede_the_fallback():
    """Estimates are discarded rather than added to real numbers."""
    tracker = VisitTracker()
    tracker.update(True, at(0))
    tracker.update(True, at(10))
    tracker.update(False, at(10 + VISIT_GAP_GRACE.total_seconds() + 1))
    assert tracker.count == 1 and not tracker.device_backed

    tracker.ingest([FakeRecord(60, 25, raw=1001)], at(600))

    assert tracker.device_backed
    assert tracker.count == 1  # the device's record, not estimate + record
    assert tracker.duration == timedelta(seconds=25)


def test_flag_stops_counting_once_device_backed():
    tracker = VisitTracker()
    tracker.ingest([FakeRecord(60, 25, raw=1001)], at(100))
    assert tracker.count == 1

    tracker.update(True, at(200))
    tracker.update(True, at(230))
    tracker.update(False, at(230 + VISIT_GAP_GRACE.total_seconds() + 1))

    # Still one: the flag only reports the live visit now.
    assert tracker.count == 1
    assert tracker.duration == timedelta(seconds=25)


def test_flag_still_reports_visit_in_progress_when_device_backed():
    tracker = VisitTracker()
    tracker.ingest([FakeRecord(60, 25, raw=1001)], at(100))

    tracker.update(True, at(200))
    assert tracker.in_progress
    assert tracker.current_duration(at(210)) == timedelta(seconds=10)


def test_records_from_another_day_stay_out_of_today():
    """The buffer can hold yesterday's visits; they must not inflate today.

    They are still real visits, so they count towards the lifetime figures and
    are kept in the log. Dropping them outright, as this used to, threw away
    the only record of them anyone had.
    """
    tracker = VisitTracker()
    yesterday = FakeRecord(-86400 + 60, 30, raw=900)
    today = FakeRecord(60, 20, raw=1001)

    tracker.ingest([yesterday, today], at(600))

    assert tracker.count == 1
    assert tracker.duration == timedelta(seconds=20)

    assert tracker.total_count == 2
    assert tracker.total_duration == timedelta(seconds=50)
    assert len(tracker.recent) == 2
    assert [visit["seconds"] for visit in tracker.recent] == [30, 20]


def test_every_banked_visit_is_announced_once():
    """new_visits carries what the caller has not seen, and only that."""
    tracker = VisitTracker()

    tracker.ingest([FakeRecord(60, 25, raw=1001)], at(600))
    assert [visit["seconds"] for visit in tracker.new_visits] == [25]

    # The queue is drained by the caller, not reset per ingest, so a visit the
    # fallback banked is not thrown away by the next history sync.
    tracker.new_visits = []

    # A resend of the same window announces nothing.
    tracker.ingest([FakeRecord(60, 25, raw=1001)], at(700))
    assert tracker.new_visits == []

    tracker.ingest([FakeRecord(300, 40, raw=1002)], at(800))
    assert [visit["seconds"] for visit in tracker.new_visits] == [40]


def test_fallback_visits_are_logged_too():
    """A visit the detection flag caught still belongs in the timeline.

    Before this, the flag path incremented the counters and left no record of
    when the visit happened, so a dashboard could say "1 visit today" and show
    an empty history.
    """
    tracker = VisitTracker()

    tracker.update(True, at(0))
    tracker.update(True, at(10))
    tracker.update(False, at(60))
    tracker.update(False, at(200))  # past the grace period, visit closes

    assert tracker.count == 1
    assert len(tracker.recent) == 1
    assert tracker.recent[0]["seconds"] > 0
    assert [v["seconds"] for v in tracker.new_visits] == [tracker.recent[0]["seconds"]]


def test_dedup_survives_midnight():
    """Regression: the dedup set used to be cleared with the daily counters.

    The fountain resends a window it has already delivered, so a record that
    crossed midnight would be banked a second time.
    """
    tracker = VisitTracker()
    record = FakeRecord(60, 25, raw=1001)

    tracker.ingest([record], at(600))
    tracker.ingest([], at(86400 + 600))  # roll into the next day
    tracker.ingest([record], at(86400 + 700))

    assert tracker.total_count == 1
    assert len(tracker.recent) == 1


def test_visit_log_is_bounded():
    """The log is for a timeline, not an archive."""
    from custom_components.petkit_ble.visits import MAX_RECENT_VISITS

    tracker = VisitTracker()
    tracker.ingest(
        [FakeRecord(60 + n, 10, raw=2000 + n) for n in range(MAX_RECENT_VISITS + 20)],
        at(600),
    )

    assert len(tracker.recent) == MAX_RECENT_VISITS


# --- lifetime totals ------------------------------------------------------


def test_daily_duration_never_goes_backwards():
    """Regression: the daily total used to dip every time a visit closed.

    The live in-progress figure kept climbing through the grace period, then
    only the shorter measured length was banked, so the sensor fell back. A
    total_increasing sensor that decreases reads as a counter reset.
    """
    tracker = VisitTracker()
    seen: list[float] = []

    def sample():
        seen.append(tracker.duration.total_seconds())

    grace = VISIT_GAP_GRACE.total_seconds()
    observations = [
        (True, 0),
        (True, 10),
        (True, 20),
        (False, 25),
        (False, 20 + grace + 1),
        (True, 200),
        (True, 215),
        (False, 215 + grace + 1),
    ]

    sample()
    for detected, offset in observations:
        tracker.update(detected, at(offset))
        sample()

    assert seen == sorted(seen), f"daily duration dipped: {seen}"


def test_lifetime_totals_survive_midnight():
    tracker = VisitTracker()
    tracker.update(True, at(0))
    tracker.update(True, at(30))
    tracker.update(False, at(30 + VISIT_GAP_GRACE.total_seconds() + 1))

    assert tracker.count == 1
    assert tracker.total_count == 1
    assert tracker.total_duration == timedelta(seconds=30)

    tomorrow = START + timedelta(days=1)
    tracker.update(False, tomorrow)

    assert tracker.count == 0, "daily figures reset"
    assert tracker.duration == timedelta()
    assert tracker.total_count == 1, "lifetime figures do not"
    assert tracker.total_duration == timedelta(seconds=30)


def test_lifetime_totals_accumulate_device_records():
    tracker = VisitTracker()
    tracker.ingest([FakeRecord(60, 25, raw=1)], at(100))
    tracker.ingest([FakeRecord(300, 40, raw=2)], at(400))

    assert tracker.total_count == 2
    assert tracker.total_duration == timedelta(seconds=65)


def test_switching_to_device_records_unwinds_estimates_from_totals():
    """Estimates must be given back, or today gets counted twice lifetime."""
    tracker = VisitTracker()
    tracker.update(True, at(0))
    tracker.update(True, at(20))
    tracker.update(False, at(20 + VISIT_GAP_GRACE.total_seconds() + 1))
    assert tracker.total_count == 1
    assert tracker.total_duration == timedelta(seconds=20)

    tracker.ingest([FakeRecord(60, 25, raw=1)], at(300))

    assert tracker.total_count == 1, "estimate replaced, not added to"
    assert tracker.total_duration == timedelta(seconds=25)


def test_totals_never_go_negative_when_unwinding():
    tracker = VisitTracker()
    tracker.count = 5
    tracker.duration = timedelta(seconds=99)

    tracker.ingest([FakeRecord(60, 10, raw=1)], at(300))

    assert tracker.total_count >= 0
    assert tracker.total_duration >= timedelta()


def test_restore_totals():
    tracker = VisitTracker()
    tracker.from_dict(
        {"total_count": 42, "total_duration": 1234.5}, START.date()
    )
    assert tracker.total_count == 42
    assert tracker.total_duration == timedelta(seconds=1234.5)

    tracker.ingest([FakeRecord(60, 10, raw=1)], at(300))
    assert tracker.total_count == 43
    assert tracker.total_duration == timedelta(seconds=1244.5)


# --- persistence across restarts -----------------------------------------


def _restart(tracker: VisitTracker, today) -> VisitTracker:
    """Simulate a Home Assistant restart via the persisted payload."""
    fresh = VisitTracker()
    fresh.from_dict(tracker.to_dict(), today)
    return fresh


def test_daily_count_survives_a_restart():
    """Regression: both counters came back as zero after every restart.

    State used to ride on a RestoreSensor's extra data, but overriding that
    broke async_get_last_sensor_data, so nothing was ever read back.
    """
    tracker = VisitTracker()
    tracker.update(True, at(0))
    tracker.update(True, at(20))
    tracker.update(False, at(20 + VISIT_GAP_GRACE.total_seconds() + 1))
    assert (tracker.count, tracker.total_count) == (1, 1)

    revived = _restart(tracker, START.date())

    assert revived.count == 1, "daily count lost on restart"
    assert revived.total_count == 1, "lifetime count lost on restart"
    assert revived.duration == timedelta(seconds=20)
    assert revived.total_duration == timedelta(seconds=20)
    assert revived.last_visit is not None


def test_restart_after_midnight_keeps_only_lifetime():
    tracker = VisitTracker()
    tracker.update(True, at(0))
    tracker.update(True, at(30))
    tracker.update(False, at(30 + VISIT_GAP_GRACE.total_seconds() + 1))

    tomorrow = (START + timedelta(days=1)).date()
    revived = _restart(tracker, tomorrow)

    assert revived.count == 0, "yesterday must not carry over"
    assert revived.duration == timedelta()
    assert revived.total_count == 1
    assert revived.total_duration == timedelta(seconds=30)
    assert revived.day == tomorrow


def test_restart_preserves_dedup_and_source():
    """Records already banked must not be counted again after a restart."""
    tracker = VisitTracker()
    records = [FakeRecord(60, 25, raw=1001)]
    tracker.ingest(records, at(100))

    revived = _restart(tracker, START.date())
    assert revived.device_backed, "device-backed flag lost"

    revived.ingest(records, at(200))
    assert revived.count == 1, "record double-counted after restart"
    assert revived.total_count == 1


def test_counts_continue_after_a_restart():
    tracker = VisitTracker()
    tracker.ingest([FakeRecord(60, 25, raw=1)], at(100))
    revived = _restart(tracker, START.date())

    revived.ingest([FakeRecord(300, 40, raw=2)], at(400))

    assert revived.count == 2
    assert revived.total_count == 2
    assert revived.total_duration == timedelta(seconds=65)


def test_persisted_payload_is_json_safe():
    import json

    tracker = VisitTracker()
    tracker.update(True, at(0))
    tracker.update(True, at(20))
    tracker.update(False, at(20 + VISIT_GAP_GRACE.total_seconds() + 1))
    tracker.ingest([FakeRecord(60, 25, raw=1001)], at(300))

    payload = tracker.to_dict()
    round_tripped = json.loads(json.dumps(payload))

    revived = VisitTracker()
    revived.from_dict(round_tripped, START.date())
    assert revived.total_count == tracker.total_count
    assert revived.count == tracker.count


def test_from_dict_tolerates_missing_keys():
    tracker = VisitTracker()
    tracker.from_dict({}, START.date())
    assert tracker.count == 0
    assert tracker.total_count == 0
    assert tracker.day == START.date()
