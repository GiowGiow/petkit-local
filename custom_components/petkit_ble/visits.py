"""Pet visit statistics.

There are two sources, and they are not equal.

The fountain logs every visit itself as a `(timestamp, stayTime)` record and
buffers them until something drains the buffer with command 212. That is what
the PetKit app does on each connection, and it is why the app has accurate
drink history without staying connected. These records are authoritative: they
do not depend on how often we sampled.

The live `detect_status` flag in each status frame is the fallback. Before any
device records arrive - or if the history stream turns out not to work on a
given firmware - visits are reconstructed by watching that flag change, which
is only as good as the sample rate. Once real records show up the tracker stops
counting from the flag, so the two can never double-count; the flag then only
drives "a pet is drinking right now".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

# A drink is many small approaches; treat a brief gap as the same visit rather
# than counting each dip of the head.
VISIT_GAP_GRACE = timedelta(seconds=30)

# Ignore single-sample blips, which are usually the sensor twitching.
MIN_VISIT_DURATION = timedelta(seconds=2)

# How many individual visits to keep for the timeline. The fountain logs each
# one with its own duration, which is what the PetKit app draws its drink
# history from; keeping them lets a dashboard do the same instead of only
# showing a running count.
MAX_RECENT_VISITS = 50

# Records are deduplicated by device timestamp across days, so the set has to
# outlive midnight. Cap it, keeping the newest.
MAX_SEEN_RECORDS = 500


@dataclass
class VisitTracker:
    """Counts visits and drinking time for the current local day."""

    count: int = 0
    duration: timedelta = timedelta()
    last_visit: datetime | None = None
    day: date | None = None

    # Lifetime figures. These never reset at midnight, so they survive as a
    # running total for as long as the integration is installed.
    total_count: int = 0
    total_duration: timedelta = timedelta()

    # Set once the fountain hands us its own records; the flag-watching
    # fallback stops counting from that point so totals cannot be doubled.
    device_backed: bool = False

    # The individual visits behind the counters, newest last.
    recent: list[dict] = field(default_factory=list)

    # Visits banked by the last ingest, for the caller to announce. Transient:
    # never persisted, cleared on every call.
    new_visits: list[dict] = field(default_factory=list, repr=False)

    _seen: set[int] = field(default_factory=set, repr=False)
    _started: datetime | None = field(default=None, repr=False)
    _last_seen: datetime | None = field(default=None, repr=False)
    _counted: bool = field(default=False, repr=False)

    @property
    def in_progress(self) -> bool:
        return self._started is not None

    def current_duration(self, now: datetime) -> timedelta:
        """How long the visit under way has lasted, or zero."""
        if self._started is None:
            return timedelta()
        return max(now - self._started, timedelta())

    def to_dict(self) -> dict:
        """Serialise for persistence.

        Everything lives here rather than in an entity's restore payload: five
        sensors read this tracker, so it has one owner and survives entities
        being renamed or disabled.
        """
        return {
            "count": self.count,
            "duration": self.duration.total_seconds(),
            "last_visit": self.last_visit.isoformat() if self.last_visit else None,
            "day": self.day.isoformat() if self.day else None,
            "total_count": self.total_count,
            "total_duration": self.total_duration.total_seconds(),
            "device_backed": self.device_backed,
            # Keeps records from being counted twice if the fountain resends a
            # window across a restart.
            "seen": sorted(self._seen),
            "recent": self.recent,
        }

    def from_dict(self, data: dict, today: date) -> None:
        """Seed from persisted state after a restart.

        Lifetime figures always come back. Today's figures only do if the
        stored day is still today, otherwise they are left at zero so a restart
        after midnight does not resurrect yesterday.
        """
        self.total_count = int(data.get("total_count") or 0)
        self.total_duration = timedelta(seconds=float(data.get("total_duration") or 0.0))
        self.device_backed = bool(data.get("device_backed"))

        stored_day = data.get("day")
        day = date.fromisoformat(stored_day) if stored_day else None
        if day != today:
            self.day = today
            return

        self.day = day
        self.count = int(data.get("count") or 0)
        self.duration = timedelta(seconds=float(data.get("duration") or 0.0))
        last_visit = data.get("last_visit")
        if last_visit:
            self.last_visit = datetime.fromisoformat(last_visit)
        self._seen = {int(v) for v in data.get("seen") or ()}
        self.recent = list(data.get("recent") or ())[-MAX_RECENT_VISITS:]

    def ingest(self, records, now: datetime) -> bool:
        """Fold in visit records the fountain recorded itself.

        These are authoritative: the firmware logs every visit with its own
        duration, so they do not depend on how often we sampled. Records are
        deduplicated by their device timestamp, because the fountain may resend
        a window we have already banked.
        """
        self._roll_day(now)
        today = now.date()
        changed = False

        if records and not self.device_backed:
            # Switching sources: drop anything the fallback guessed so the
            # device's own numbers are not added on top of estimates. The
            # lifetime figures have to give the estimates back too, or they
            # would keep counting today twice.
            self.device_backed = True
            self.total_count = max(0, self.total_count - self.count)
            self.total_duration = max(
                timedelta(), self.total_duration - self.duration
            )
            self.count = 0
            self.duration = timedelta()
            changed = True

        self.new_visits = []

        for record in records:
            local = record.timestamp.astimezone(now.tzinfo)
            if record.raw_time in self._seen:
                continue
            self._seen.add(record.raw_time)

            stay = timedelta(seconds=record.stay_seconds)
            visit = {"at": local.isoformat(), "seconds": record.stay_seconds}
            self.recent.append(visit)
            self.new_visits.append(visit)

            # Lifetime counts every visit the fountain reports. Only the ones
            # that happened today may touch the daily figures: the buffer can
            # hold yesterday's visits, and those must not inflate today.
            self.total_count += 1
            self.total_duration += stay
            if local.date() == today:
                self.count += 1
                self.duration += stay
            if self.last_visit is None or local > self.last_visit:
                self.last_visit = local
            changed = True

        if changed:
            self.recent.sort(key=lambda visit: visit["at"])
            del self.recent[:-MAX_RECENT_VISITS]
            self._trim_seen()

        return changed

    def _trim_seen(self) -> None:
        """Keep the dedup set bounded, dropping the oldest device timestamps."""
        if len(self._seen) <= MAX_SEEN_RECORDS:
            return
        self._seen = set(sorted(self._seen)[-MAX_SEEN_RECORDS:])

    def update(self, detected: bool, now: datetime) -> bool:
        """Fold in one live observation of the detection flag.

        Always tracks whether a visit is under way. Only contributes to the
        daily totals while no device records have arrived.
        """
        changed = self._roll_day(now)

        if detected:
            if self._started is None:
                self._started = now
                self._counted = False
            self._last_seen = now
            # Count once the visit is long enough to be real, so a visit shows
            # up while it is happening rather than only after it ends.
            if (
                not self._counted
                and not self.device_backed
                and now - self._started >= MIN_VISIT_DURATION
            ):
                self.count += 1
                self.total_count += 1
                self._counted = True
                self.last_visit = self._started
                changed = True
            return changed

        if self._started is None:
            return changed

        # Not detected: hold the visit open briefly so a gap between sips does
        # not split one drink into several.
        last_seen = self._last_seen or self._started
        if now - last_seen < VISIT_GAP_GRACE:
            return changed

        return self._close(last_seen) or changed

    def _close(self, ended: datetime) -> bool:
        """End the visit under way, banking its duration."""
        if self._started is None:
            return False
        length = max(ended - self._started, timedelta())
        self._started = None
        self._last_seen = None

        if not self._counted:
            # Too short to have been counted, or the device is supplying the
            # real numbers; either way nothing to bank here.
            self._counted = False
            return False

        self.duration += length
        self.total_duration += length
        self.last_visit = ended
        self._counted = False
        return True

    def _roll_day(self, now: datetime) -> bool:
        """Reset totals at local midnight."""
        today = now.date()
        if self.day == today:
            return False

        if self._started is not None:
            # Bank whatever happened before midnight, then treat the rest of
            # the visit as belonging to the new day.
            self._close(self._last_seen or self._started)
            self._started = now

        self.day = today
        self.count = 0
        self.duration = timedelta()
        # `_seen` deliberately survives the roll: records are deduplicated by
        # device timestamp across days, so clearing it here would let the
        # fountain's next resend be banked a second time.
        return True
