"""Tests for the PetKit BLE wire protocol.

Expected byte layouts are taken from the decompiled app
(``PetkitBleMsg.toRawDataBytes``, ``CTW3DataConvertor``) and cross-checked
against ``pypetkitapi``'s relay frames.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from custom_components.petkit_ble import protocol as p
from custom_components.petkit_ble.const import MODE_NORMAL, MODE_SMART

# --- framing ------------------------------------------------------------


def test_empty_command_frame():
    assert p.build_frame(p.CMD_STATUS, b"", seq=3) == bytes.fromhex(
        "fafcfdd2010300 00fb".replace(" ", "")
    )


def test_frame_carries_little_endian_length():
    frame = p.build_frame(0xDC, bytes(300))
    assert frame[6] == 300 & 0xFF
    assert frame[7] == 300 >> 8


def test_frame_matches_pypetkitapi_pause():
    """pypetkitapi encodes PAUSE as [220, 1, <seq>, 3, 0, 1, 0, 2]."""
    frame = p.build_frame(220, bytes((1, 0, 2)), seq=9)
    assert frame[3:] == bytes((220, 1, 9, 3, 0, 1, 0, 2, 0xFB))


def test_payload_too_long_rejected():
    with pytest.raises(p.ProtocolError):
        p.build_frame(1, bytes(0x10000))


# --- decoding -----------------------------------------------------------


def test_decodes_whole_frame():
    decoder = p.FrameDecoder()
    payload = bytes(range(26))
    frames = decoder.feed(
        p.build_frame(p.CMD_STATUS, payload, msg_type=p.TYPE_RESPONSE)
    )
    assert len(frames) == 1
    assert frames[0].cmd == p.CMD_STATUS
    assert frames[0].payload == payload


@pytest.mark.parametrize("chunk", [1, 3, 7, 20])
def test_reassembles_fragments(chunk):
    decoder = p.FrameDecoder()
    payload = bytes(range(40))
    raw = p.build_frame(p.CMD_PUSH_STATUS, payload, msg_type=p.TYPE_RESPONSE)

    frames = []
    for i in range(0, len(raw), chunk):
        frames += decoder.feed(raw[i : i + chunk])

    assert len(frames) == 1
    assert frames[0].payload == payload


def test_decodes_back_to_back_frames_with_leading_noise():
    decoder = p.FrameDecoder()
    first = p.build_frame(
        p.CMD_BATTERY, bytes((0x0F, 0xA0, 88)), msg_type=p.TYPE_RESPONSE
    )
    second = p.build_frame(p.CMD_STATUS, bytes(26), msg_type=p.TYPE_RESPONSE)

    frames = decoder.feed(b"\x00\x11" + first + second)

    assert [f.cmd for f in frames] == [p.CMD_BATTERY, p.CMD_STATUS]


def test_stream_frame_decoded():
    body = bytes(range(12))
    raw = bytes((*p.STREAM_HEADER, 212, 2, 1, 4, len(body), 0, *body))
    frames = p.FrameDecoder().feed(raw)
    assert len(frames) == 1
    frame = frames[0]
    assert frame.is_stream and frame.index == 1 and frame.total == 4
    assert frame.payload == body


def test_decoder_does_not_grow_unboundedly_on_noise():
    decoder = p.FrameDecoder()
    for _ in range(50):
        decoder.feed(b"\x01\x02\x03\x04")
    assert len(decoder._buf) <= 4


# --- payload parsers ----------------------------------------------------


STATUS_BLOCK = bytes(
    [
        1,
        1,
        2,
        1,
        0,  # power, run, mode, mains, night dnd
        0,
        0,
        0,
        0,  # warnings
        0,
        1,
        0x86,
        0xA0,  # pump runtime = 100000
        73,  # filter %
        1,  # running
        0,
        0,
        0x1C,
        0x20,  # today = 7200
        0,  # detect
        0x14,
        0x8C,  # supply 5260 mV
        0x10,
        0x68,  # battery 4200 mV
        88,
        1,  # battery %, module
    ]
)


def test_parse_status():
    state = p.parse_status(STATUS_BLOCK)
    assert state["mode"] == 2
    assert state["pump_runtime"] == 100_000
    assert state["pump_runtime_today"] == 7200
    assert state["filter_percent"] == 73
    assert state["battery_percent"] == 88
    assert state["supply_voltage"] == pytest.approx(5.26)
    assert state["battery_voltage"] == pytest.approx(4.2)


# Captured over BLE from a CTW3 (Eversweet Max, device id 223) while the pump
# was running and the unit sat on mains. Kept verbatim: the synthetic block
# above encodes what we believe the layout to be, this one encodes what a
# fountain actually sent.
CTW3_LIVE_STATUS = bytes.fromhex(
    "0101010200000000000026fd530101000135ac001368106f640039053f05"
)


def test_parse_status_matches_real_ctw3_capture():
    """Byte 1 is a run flag, reading 1 while the pump runs.

    The two fields that describe the pump have to agree. Reading byte 1 as a
    suspend flag and inverting it made a running fountain report itself
    paused, which is the regression this capture pins down.
    """
    state = p.parse_status(CTW3_LIVE_STATUS)

    assert state["run_status"] == 1
    assert state["running_status"] == 1

    assert state["power_status"] == 1
    assert state["mode"] == MODE_NORMAL
    assert state["electric_status"] == 2
    assert state["filter_percent"] == 1
    assert state["pump_runtime"] == 2_555_219
    assert state["pump_runtime_today"] == 79_276
    assert state["battery_percent"] == 100
    assert state["supply_voltage"] == pytest.approx(4.968)
    assert state["battery_voltage"] == pytest.approx(4.207)


def test_parse_status_rejects_short_payload():
    with pytest.raises(p.ProtocolError):
        p.parse_status(bytes(10))


def test_parse_settings_short_and_long_forms():
    short = bytes([15, 30, 0, 45, 0, 90, 1, 2, 1, 0])
    parsed = p.parse_settings(short)
    assert parsed["smart_working_time"] == 15
    assert parsed["battery_sleep_time"] == 90
    assert parsed["child_lock"] == 0
    assert "smart_proximity" not in parsed

    long = short + bytes([1, 1])
    parsed = p.parse_settings(long)
    assert parsed["smart_proximity"] == 1
    assert parsed["battery_proximity"] == 1


def test_settings_block_round_trip():
    settings = p.FountainSettings(
        smart_working_time=15,
        smart_sleep_time=30,
        battery_working_time=45,
        battery_sleep_time=600,
        light_switch=1,
        light_brightness=2,
        dnd_switch=1,
        child_lock=0,
        smart_proximity=1,
        battery_proximity=0,
    )
    payload = p.settings_payload(settings)
    assert len(payload) == 12
    assert p.parse_settings(payload) == settings.as_dict()


def test_push_status_reads_settings_at_offset_30():
    """The app skips bytes 26-29 in the cmd 230 block."""
    push = STATUS_BLOCK + bytes(4) + bytes([20, 40, 0, 60, 0, 90, 1, 3, 1, 0])
    parsed = p.parse_push_status(push)
    assert parsed["smart_working_time"] == 20
    assert parsed["smart_sleep_time"] == 40
    assert parsed["battery_working_time"] == 60
    assert parsed["battery_sleep_time"] == 90
    assert parsed["light_brightness"] == 3
    assert parsed["child_lock"] == 0


def test_push_status_without_settings_tail():
    parsed = p.parse_push_status(STATUS_BLOCK)
    assert parsed["battery_percent"] == 88
    assert "light_brightness" not in parsed


def test_parse_identity():
    payload = (0x12345678).to_bytes(8, "big") + b"CTW3TEST000001"
    parsed = p.parse_identity(payload)
    assert parsed["device_id"] == 0x12345678
    assert parsed["serial"] == "CTW3TEST000001"


def test_parse_battery():
    assert p.parse_battery(bytes((0x10, 0x68, 91))) == {
        "battery_voltage": 4.2,
        "battery_percent": 91,
    }


def test_parse_schedule():
    payload = (
        bytes([1, 2, 0, 0, 0, 0]) + bytes([0, 60, 0, 120, 0]) + bytes([2, 88, 3, 32, 0])
    )
    parsed = p.parse_schedule(payload)
    assert parsed["enabled"] is True
    assert parsed["windows"] == [(60, 120), (600, 800)]


# --- builders -----------------------------------------------------------


def test_time_payload_shape():
    payload = p.time_payload(
        datetime(2026, 8, 16, 12, 0, tzinfo=UTC), tz_offset_hours=2
    )
    assert len(payload) == 6
    assert payload[0] == 0
    assert payload[5] == 14  # round(2) + 12
    seconds = int.from_bytes(payload[1:5], "big")
    assert seconds == int(
        (datetime(2026, 8, 16, 12, 0, tzinfo=UTC) - p.EPOCH_2000).total_seconds()
    )


def test_negative_timezone_offset():
    payload = p.time_payload(datetime(2026, 1, 1, tzinfo=UTC), tz_offset_hours=-5)
    assert payload[5] == 7


def test_init_device_payload_is_16_bytes():
    secret = p.derive_secret(0x12345678)
    payload = p.init_device_payload(0x12345678, secret)
    assert len(payload) == 16
    assert payload[:8] == (0x12345678).to_bytes(8, "big")
    assert payload[8:] == secret


def test_derive_secret_is_deterministic_and_nonzero_tail():
    secret = p.derive_secret(0x12345678)
    assert secret == p.derive_secret(0x12345678)
    assert len(secret) == 8
    assert secret[-2:] == bytes((13, 37))  # zero tail is replaced


def test_secret_payload_left_pads():
    assert p.secret_payload(b"\x01\x02") == bytes(6) + b"\x01\x02"


def test_mode_payload():
    assert p.mode_payload(True, 2, p.SELECTOR_POWER_MODE) == bytes((1, 2, 1))
    assert p.mode_payload(True, 0, p.SELECTOR_RUN_PAUSE) == bytes((1, 0, 2))
    assert p.mode_payload(False, 1, p.SELECTOR_POWER_MODE) == bytes((0, 1, 1))


# --- derived values -----------------------------------------------------


def test_water_purified_uses_ctw3_divisor():
    # 3600 s at 1.5 L/min / 3.0 -> 30 L
    assert p.water_purified_litres("CTW3", 3600) == pytest.approx(30.0)


def test_energy_uses_default_coefficient():
    assert p.energy_kwh("CTW3", 3600) == pytest.approx(0.75 * 3600 / 3600 / 1000)


def test_filter_days_normal_mode_ignores_duty_cycle():
    assert p.filter_days_left(100, MODE_NORMAL, 15, 30) == 30


def test_filter_days_smart_mode_extends_life():
    """Duty-cycling the pump makes the filter last proportionally longer."""
    normal = p.filter_days_left(100, MODE_NORMAL, 15, 30)
    smart = p.filter_days_left(100, MODE_SMART, 15, 30)
    assert smart == 90 and smart > normal


# --- discovery ----------------------------------------------------------


def test_parse_service_data_reads_byte_five():
    assert p.parse_service_data({"uuid": bytes([0, 1, 2, 3, 4, 247])}) == 247


def test_parse_service_data_too_short():
    assert p.parse_service_data({"uuid": bytes(3)}) is None


# --- buffered history stream --------------------------------------------


def test_parse_work_records():
    """Each record is a 4-byte timestamp then a 2-byte stay duration."""
    raw = (1000).to_bytes(4, "big") + (25).to_bytes(2, "big")
    raw += (2000).to_bytes(4, "big") + (40).to_bytes(2, "big")

    records = p.parse_work_records(raw)

    assert [r.raw_time for r in records] == [1000, 2000]
    assert [r.stay_seconds for r in records] == [25, 40]
    assert records[0].timestamp == p.EPOCH_2000 + timedelta(seconds=1000)


def test_parse_work_records_skips_empty_slots():
    raw = bytes(6) + (2000).to_bytes(4, "big") + (40).to_bytes(2, "big")
    assert [r.raw_time for r in p.parse_work_records(raw)] == [2000]


def test_parse_work_records_ignores_trailing_partial():
    raw = (1000).to_bytes(4, "big") + (25).to_bytes(2, "big") + b"\x01\x02"
    assert len(p.parse_work_records(raw)) == 1


def test_stream_ack_bitmap_sets_bit_31_minus_index():
    """Matches BaseDataConvertor.checkStreamData."""
    assert p.stream_ack_payload({0}) == (1 << 31).to_bytes(4, "big")
    assert p.stream_ack_payload({31}) == (1).to_bytes(4, "big")
    assert p.stream_ack_payload({0, 1}) == ((1 << 31) | (1 << 30)).to_bytes(4, "big")
    assert p.stream_ack_payload(set()) == bytes(4)


def test_stream_ack_ignores_out_of_window_indices():
    assert p.stream_ack_payload({99}) == bytes(4)


def test_stream_completeness():
    assert p.stream_is_complete({0, 1, 2}, 3)
    assert not p.stream_is_complete({0, 2}, 3)
    assert p.stream_is_complete(set(range(32)), 0)  # unknown total -> full window
