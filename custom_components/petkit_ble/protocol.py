"""Wire protocol for PetKit BLE fountains.

Frame layout, taken from ``PetkitBleMsg.toRawDataBytes()`` in the official app:

    command:  FA FC FD | cmd | type | seq | len_lo | len_hi | payload | FB
    stream:   FA FC FE | cmd | type | idx  | total  | len_lo | len_hi | payload

The length field is little-endian. Every multi-byte integer *inside* a payload
is big-endian (``ByteUtil.bytes2Short`` / ``bytes2Int`` / ``bytes2Long`` all
shift left over ascending indices). There is no checksum.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from .const import (
    MODE_NORMAL,
    POWER_COEFFICIENT,
    POWER_COEFFICIENT_DEFAULT,
    WATER_DIVISOR,
    WATER_FLOW_L_PER_MIN,
    WATER_FLOW_OVERRIDE,
)

CMD_HEADER = bytes((0xFA, 0xFC, 0xFD))
STREAM_HEADER = bytes((0xFA, 0xFC, 0xFE))
END_BYTE = 0xFB

CMD_HEADER_LEN = 8
STREAM_HEADER_LEN = 9

# Epoch the firmware counts seconds from.
EPOCH_2000 = datetime(2000, 1, 1, tzinfo=UTC)

# --- Message types ------------------------------------------------------
TYPE_OTA = 0
TYPE_REQUEST = 1
TYPE_RESPONSE = 2
TYPE_NO_RESPONSE = 3

# --- Commands -----------------------------------------------------------
CMD_BATTERY = 66
CMD_STREAM_ACK = 67  # device asks which chunks arrived; we reply with a bitmap
CMD_STREAM_END = 69  # device signals the transfer is finished
CMD_INIT_DEVICE = 73  # writes device id + secret ("claims" the device)
CMD_STREAM_SETTING = 80
CMD_SET_TIME = 84
CMD_SECURITY_CHECK = 86
CMD_DEVICE_INFO = 200  # hardware + firmware
CMD_DEVICE_DESC = 201
CMD_STATUS = 210
CMD_SETTINGS = 211
CMD_HISTORY = 212
CMD_IDENTITY = 213  # device id + serial number
CMD_LIGHT_SCHEDULE = 215
CMD_DND_SCHEDULE = 216
CMD_SET_MODE = 220
CMD_SET_SETTINGS = 221
CMD_RESET_FILTER = 222
CMD_WRITE_LIGHT_SCHEDULE = 225
CMD_WRITE_DND_SCHEDULE = 226
CMD_PUSH_STATUS = 230  # device-initiated, expects an ack

# cmd 220 selectors
SELECTOR_POWER_MODE = 1
SELECTOR_RUN_PAUSE = 2

# Bulk history arrives as stream frames carrying one of these commands.
STREAM_DATA_CMDS = (68, 82)

# The device acknowledges in windows of 32 chunks.
STREAM_WINDOW = 32

# One buffered visit: 4-byte timestamp then 2-byte stay duration.
WORK_RECORD_SIZE = 6


class ProtocolError(Exception):
    """Raised when a frame cannot be understood."""


@dataclass(slots=True)
class Frame:
    """A decoded frame from the fountain."""

    cmd: int
    type: int
    payload: bytes
    seq: int = 0
    index: int = 0
    total: int = 0
    is_stream: bool = False


def build_frame(
    cmd: int, payload: bytes = b"", seq: int = 0, msg_type: int = TYPE_REQUEST
) -> bytes:
    """Encode a command frame."""
    length = len(payload)
    if length > 0xFFFF:
        raise ProtocolError(f"payload too long: {length}")
    return bytes(
        (
            *CMD_HEADER,
            cmd & 0xFF,
            msg_type & 0xFF,
            seq & 0xFF,
            length & 0xFF,
            (length >> 8) & 0xFF,
            *payload,
            END_BYTE,
        )
    )


class FrameDecoder:
    """Reassembles frames from a stream of BLE notifications.

    A single notification is capped by the negotiated MTU, so a 40-byte status
    push arrives in pieces. Bytes are buffered until the length field says a
    whole frame is present.
    """

    def __init__(self) -> None:
        self._buf = bytearray()

    def reset(self) -> None:
        self._buf.clear()

    def feed(self, data: bytes) -> list[Frame]:
        """Add received bytes and return any frames that completed."""
        self._buf.extend(data)
        frames: list[Frame] = []

        while True:
            start = self._find_header()
            if start is None:
                # Keep at most two bytes; a header may straddle the boundary.
                if len(self._buf) > 2:
                    del self._buf[: len(self._buf) - 2]
                break
            if start:
                del self._buf[:start]

            frame, consumed = self._try_decode()
            if frame is None:
                break
            del self._buf[:consumed]
            frames.append(frame)

        return frames

    def _find_header(self) -> int | None:
        for i in range(len(self._buf) - 2):
            three = bytes(self._buf[i : i + 3])
            if three in (CMD_HEADER, STREAM_HEADER):
                return i
        return None

    def _try_decode(self) -> tuple[Frame | None, int]:
        buf = self._buf
        header = bytes(buf[:3])

        if header == CMD_HEADER:
            if len(buf) < CMD_HEADER_LEN:
                return None, 0
            length = buf[6] | (buf[7] << 8)
            total = CMD_HEADER_LEN + length + 1  # trailing END_BYTE
            if len(buf) < total:
                return None, 0
            return (
                Frame(
                    cmd=buf[3],
                    type=buf[4],
                    seq=buf[5],
                    payload=bytes(buf[CMD_HEADER_LEN : CMD_HEADER_LEN + length]),
                ),
                total,
            )

        if len(buf) < STREAM_HEADER_LEN:
            return None, 0
        length = buf[7] | (buf[8] << 8)
        total = STREAM_HEADER_LEN + length
        if len(buf) < total:
            return None, 0
        # Stream frames carry no terminator; swallow one if the device sent it.
        consumed = total + 1 if len(buf) > total and buf[total] == END_BYTE else total
        return (
            Frame(
                cmd=buf[3],
                type=buf[4],
                index=buf[5],
                total=buf[6],
                payload=bytes(buf[STREAM_HEADER_LEN : STREAM_HEADER_LEN + length]),
                is_stream=True,
            ),
            consumed,
        )


# --- Payload builders ---------------------------------------------------


def time_payload(
    now: datetime | None = None, tz_offset_hours: float | None = None
) -> bytes:
    """Build the cmd 84 clock payload.

    One leading zero byte, then seconds since 2000-01-01 as a big-endian
    uint32, then the timezone as ``round(offset_hours) + 12``.
    """
    now = now or datetime.now(UTC)
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    if tz_offset_hours is None:
        local_offset = now.astimezone().utcoffset()
        tz_offset_hours = local_offset.total_seconds() / 3600.0 if local_offset else 0.0

    seconds = int((now - EPOCH_2000).total_seconds())
    tz_byte = int(round(tz_offset_hours)) + 12
    return bytes(
        (
            0,
            (seconds >> 24) & 0xFF,
            (seconds >> 16) & 0xFF,
            (seconds >> 8) & 0xFF,
            seconds & 0xFF,
            tz_byte & 0xFF,
        )
    )


def secret_payload(secret: bytes) -> bytes:
    """cmd 86 - present the 8-byte secret, left-padded."""
    return _pad_left(secret, 8)


def init_device_payload(device_id: int, secret: bytes) -> bytes:
    """cmd 73 - write device id and secret into the fountain."""
    return _pad_left(device_id.to_bytes(8, "big"), 8) + _pad_left(secret, 8)


def derive_secret(device_id: int) -> bytes:
    """Derive a deterministic secret from the device id.

    Used when claiming the device instead of importing PetKit's own secret.
    The value is arbitrary - it only has to match what cmd 73 wrote.
    """
    raw = bytearray(device_id.to_bytes(8, "big"))
    raw.reverse()
    if raw[-1] == 0 and raw[-2] == 0:
        raw[-2], raw[-1] = 13, 37
    return bytes(raw)


def mode_payload(power_on: bool, value: int, selector: int) -> bytes:
    """cmd 220 - [power, value, selector].

    With ``selector`` 1 the value is the operating mode; with 2 it is the
    run/pause flag.
    """
    return bytes((1 if power_on else 0, value & 0xFF, selector & 0xFF))


def settings_payload(settings: FountainSettings) -> bytes:
    """cmd 221 - the whole 12-byte settings block.

    The firmware has no partial write, so every field goes out together.
    """
    return bytes(
        (
            settings.smart_working_time & 0xFF,
            settings.smart_sleep_time & 0xFF,
            *_u16_be(settings.battery_working_time),
            *_u16_be(settings.battery_sleep_time),
            settings.light_switch & 0xFF,
            settings.light_brightness & 0xFF,
            settings.dnd_switch & 0xFF,
            settings.child_lock & 0xFF,
            settings.smart_proximity & 0xFF,
            settings.battery_proximity & 0xFF,
        )
    )


# --- Payload parsers ----------------------------------------------------


@dataclass(slots=True)
class FountainSettings:
    """Mutable settings block, mirrored so cmd 221 can be rebuilt."""

    smart_working_time: int = 0
    smart_sleep_time: int = 0
    battery_working_time: int = 0
    battery_sleep_time: int = 0
    light_switch: int = 0
    light_brightness: int = 1
    dnd_switch: int = 0
    child_lock: int = 0
    smart_proximity: int = 0
    battery_proximity: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "smart_working_time": self.smart_working_time,
            "smart_sleep_time": self.smart_sleep_time,
            "battery_working_time": self.battery_working_time,
            "battery_sleep_time": self.battery_sleep_time,
            "light_switch": self.light_switch,
            "light_brightness": self.light_brightness,
            "dnd_switch": self.dnd_switch,
            "child_lock": self.child_lock,
            "smart_proximity": self.smart_proximity,
            "battery_proximity": self.battery_proximity,
        }


def parse_identity(payload: bytes) -> dict[str, Any]:
    """cmd 213 - 8-byte device id, then a 14-character serial."""
    if len(payload) < 8:
        raise ProtocolError(f"identity payload too short: {len(payload)}")
    result: dict[str, Any] = {"device_id": int.from_bytes(payload[:8], "big")}
    if len(payload) >= 22:
        result["serial"] = (
            payload[8:22].decode("ascii", errors="replace").strip("\x00 ")
        )
    return result


def parse_device_info(payload: bytes) -> dict[str, Any]:
    """cmd 200 - hardware and firmware revision."""
    if len(payload) < 2:
        raise ProtocolError("device info payload too short")
    return {
        "hardware": payload[0],
        "firmware": payload[1],
        "firmware_version": f"{payload[0]}.{payload[1]}",
    }


def parse_battery(payload: bytes) -> dict[str, Any]:
    """cmd 66 - battery voltage (big-endian uint16, mV) and percent."""
    if len(payload) < 3:
        raise ProtocolError("battery payload too short")
    return {
        "battery_voltage": int.from_bytes(payload[0:2], "big") / 1000.0,
        "battery_percent": payload[2],
    }


def parse_status(payload: bytes) -> dict[str, Any]:
    """cmd 210 - the 26-byte live status block."""
    if len(payload) < 26:
        raise ProtocolError(f"status payload too short: {len(payload)}")
    return {
        "power_status": payload[0],
        # A run flag, not a suspend flag: the firmware reports 1 while the
        # pump is running and 0 while it is paused, mirroring the value that
        # cmd 220 sends with SELECTOR_RUN_PAUSE. Captured from a CTW3 with the
        # pump running: byte 1 is 0x01 while byte 14 (run_state) reads 1.
        "run_status": payload[1],
        "mode": payload[2],
        "electric_status": payload[3],
        "night_dnd_active": payload[4],
        "warning_breakdown": payload[5],
        "warning_water_missing": payload[6],
        "warning_low_battery": payload[7],
        "warning_filter": payload[8],
        "pump_runtime": int.from_bytes(payload[9:13], "big"),
        "filter_percent": payload[13],
        "running_status": payload[14],
        "pump_runtime_today": int.from_bytes(payload[15:19], "big"),
        "detect_status": payload[19],
        "supply_voltage": int.from_bytes(payload[20:22], "big") / 1000.0,
        "battery_voltage": int.from_bytes(payload[22:24], "big") / 1000.0,
        "battery_percent": payload[24],
        "module_status": payload[25],
    }


def parse_settings(payload: bytes) -> dict[str, Any]:
    """cmd 211 - settings block.

    Ships in a 10-byte form (through child lock) and a 12-byte form that adds
    the two proximity switches.
    """
    if len(payload) < 9:
        raise ProtocolError(f"settings payload too short: {len(payload)}")
    result: dict[str, Any] = {
        "smart_working_time": payload[0],
        "smart_sleep_time": payload[1],
        "battery_working_time": int.from_bytes(payload[2:4], "big"),
        "battery_sleep_time": int.from_bytes(payload[4:6], "big"),
        "light_switch": payload[6],
        "light_brightness": payload[7],
        "dnd_switch": payload[8],
    }
    if len(payload) > 9:
        result["child_lock"] = payload[9]
    if len(payload) > 11:
        result["smart_proximity"] = payload[10]
        result["battery_proximity"] = payload[11]
    return result


def parse_push_status(payload: bytes) -> dict[str, Any]:
    """cmd 230 - status block plus settings appended at offset 30.

    Bytes 26-29 are unused by the app's parser and are skipped here too.
    """
    if len(payload) < 26:
        raise ProtocolError(f"push payload too short: {len(payload)}")
    result = parse_status(payload)
    if len(payload) >= 39:
        result.update(
            {
                "smart_working_time": payload[30],
                "smart_sleep_time": payload[31],
                "battery_working_time": int.from_bytes(payload[32:34], "big"),
                "battery_sleep_time": int.from_bytes(payload[34:36], "big"),
                "light_switch": payload[36],
                "light_brightness": payload[37],
                "dnd_switch": payload[38],
            }
        )
    if len(payload) >= 40:
        result["child_lock"] = payload[39]
    return result


@dataclass(slots=True, frozen=True)
class WorkRecord:
    """One visit the fountain recorded and buffered for us.

    ``CTW3WorkData`` in the app: a 4-byte ``workTime`` and a 2-byte
    ``stayTime``. The fountain logs these itself, so they do not depend on how
    often we happen to be connected.
    """

    timestamp: datetime
    stay_seconds: int
    raw_time: int


def parse_history_header(payload: bytes) -> dict[str, Any]:
    """cmd 212 reply: a status byte then the pending byte count.

    ``CTW3BleClient`` reads bytes 1-4 into ``maxDataLength``, so a zero here
    means the fountain has nothing buffered and no stream will follow.
    """
    if len(payload) < 5:
        raise ProtocolError(f"history header too short: {len(payload)}")
    return {"ok": payload[0] == 1, "pending_bytes": int.from_bytes(payload[1:5], "big")}


def stream_setting_payload(
    package_count: int = STREAM_WINDOW, package_length: int = 1
) -> bytes:
    """cmd 80 - the command that actually starts the transfer.

    The app sends this immediately after cmd 212 reports pending data, with the
    window size and its ``maxPackageLength`` (still 1 at that point). Without
    it the fountain answers cmd 212 and then stays silent.
    """
    return package_count.to_bytes(4, "big") + package_length.to_bytes(4, "big")


def parse_work_records(payload: bytes) -> list[WorkRecord]:
    """Split a reassembled history stream into visit records."""
    records: list[WorkRecord] = []
    for offset in range(0, len(payload) - WORK_RECORD_SIZE + 1, WORK_RECORD_SIZE):
        chunk = payload[offset : offset + WORK_RECORD_SIZE]
        raw_time = int.from_bytes(chunk[0:4], "big")
        stay = int.from_bytes(chunk[4:6], "big")
        if raw_time == 0:
            continue
        records.append(
            WorkRecord(
                timestamp=device_time_to_datetime(raw_time),
                stay_seconds=stay,
                raw_time=raw_time,
            )
        )
    return records


def device_time_to_datetime(raw: int) -> datetime:
    """Convert the firmware's seconds-since-2000 counter to a datetime."""
    return EPOCH_2000 + timedelta(seconds=raw)


def stream_ack_payload(received: set[int], window: int = STREAM_WINDOW) -> bytes:
    """Build the cmd 67 bitmap telling the device which chunks arrived.

    Bit ``31 - index`` is set for each chunk we hold, matching
    ``BaseDataConvertor.checkStreamData``.
    """
    mask = 0
    for index in received:
        if 0 <= index < window:
            mask |= 1 << (31 - index)
    return mask.to_bytes(4, "big")


def stream_is_complete(received: set[int], total: int) -> bool:
    """Whether every chunk the device announced has arrived."""
    expected = min(total or STREAM_WINDOW, STREAM_WINDOW)
    return all(index in received for index in range(expected))


def parse_schedule(payload: bytes) -> dict[str, Any]:
    """cmd 215 / 216 - a list of ``(start, end)`` windows in minutes."""
    if len(payload) < 2:
        raise ProtocolError("schedule payload too short")
    enabled = payload[0]
    count = payload[1]
    windows: list[tuple[int, int]] = []
    for i in range(count):
        base = 6 + i * 5
        if base + 4 > len(payload):
            break
        windows.append(
            (
                int.from_bytes(payload[base : base + 2], "big"),
                int.from_bytes(payload[base + 2 : base + 4], "big"),
            )
        )
    return {"enabled": bool(enabled), "windows": windows}


# --- Derived values -----------------------------------------------------


def water_purified_litres(alias: str, pump_seconds: int) -> float:
    """Reproduce the cloud's purified-water estimate from pump runtime."""
    flow = WATER_FLOW_OVERRIDE.get(alias, WATER_FLOW_L_PER_MIN)
    divisor = WATER_DIVISOR.get(alias, 2.0)
    return (flow * pump_seconds) / 60.0 / divisor


def energy_kwh(alias: str, pump_seconds: int) -> float:
    """Reproduce the cloud's energy estimate from pump runtime."""
    coefficient = POWER_COEFFICIENT.get(alias, POWER_COEFFICIENT_DEFAULT)
    return (coefficient * pump_seconds) / 3600.0 / 1000.0


def filter_days_left(
    filter_percent: int, mode: int, time_on: int, time_off: int
) -> int:
    """Estimate remaining filter days from its percentage and duty cycle."""
    fraction = filter_percent / 100.0
    if mode == MODE_NORMAL:
        time_on, time_off = 1, 0
    if not time_on:
        return math.ceil(fraction * 60)
    return math.ceil(((fraction * 30.0) * (time_on + time_off)) / time_on)


# --- Helpers ------------------------------------------------------------


def _pad_left(data: bytes, size: int) -> bytes:
    if len(data) >= size:
        return data[-size:]
    return bytes(size - len(data)) + data


def _u16_be(value: int) -> tuple[int, int]:
    value &= 0xFFFF
    return (value >> 8) & 0xFF, value & 0xFF


def parse_service_data(service_data: dict[str, bytes]) -> int | None:
    """Pull the model identifier out of advertised service data (byte 5)."""
    combined = bytearray()
    for value in service_data.values():
        combined.extend(value)
    if len(combined) < 6:
        return None
    return combined[5]
