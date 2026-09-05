"""Shared fixtures: a fake fountain that speaks the real wire protocol."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from custom_components.petkit_ble import protocol as p  # noqa: E402


class FakeFountain:
    """Emulates the firmware's request/response behaviour.

    Mirrors what the app's parsers expect, so the client under test is
    exercised against realistic byte layouts rather than hand-fed dicts.
    """

    def __init__(
        self,
        device_id: int = 0x0000000012345678,
        serial: str = "CTW3TEST000001",
        stored_secret: bytes | None = None,
        require_claim: bool = True,
    ) -> None:
        self.device_id = device_id
        self.serial = serial
        self.stored_secret = stored_secret
        self.require_claim = require_claim
        self.authenticated = False

        self.power_status = 1
        self.run_status = 1
        self.mode = 1
        self.filter_percent = 80
        self.pump_runtime = 100_000
        self.pump_runtime_today = 3600
        self.settings = bytearray([15, 30, 0, 45, 0, 90, 1, 2, 0, 0, 1, 1])

        self.received: list[p.Frame] = []
        self.clock_set = False
        # Visits the fountain has buffered, as (raw_time, stay_seconds).
        self.history: list[tuple[int, int]] = []
        self.pending_stream: list[bytes] = []
        self.stream_setting: bytes | None = None

    def handle(self, frame: p.Frame) -> bytes | None:
        """Return the response frame for a request, or None for silence."""
        self.received.append(frame)
        cmd, payload = frame.cmd, frame.payload

        if cmd == p.CMD_IDENTITY:
            body = self.device_id.to_bytes(8, "big") + self.serial.encode()[:14].ljust(
                14, b"\x00"
            )
            return self._resp(cmd, body)

        if cmd == p.CMD_INIT_DEVICE:
            if len(payload) != 16:
                return self._resp(cmd, b"\x00")
            self.device_id = int.from_bytes(payload[:8], "big")
            self.stored_secret = payload[8:16]
            return self._resp(cmd, b"\x01")

        if cmd == p.CMD_SECURITY_CHECK:
            if self.stored_secret is None and self.require_claim:
                return self._resp(cmd, b"\x00")
            ok = payload[:8] == self.stored_secret
            self.authenticated = ok
            return self._resp(cmd, b"\x01" if ok else b"\x00")

        if not self.authenticated:
            return self._resp(cmd, b"\x00")

        if cmd == p.CMD_SET_TIME:
            self.clock_set = True
            return self._resp(cmd, b"\x01")

        if cmd == p.CMD_DEVICE_INFO:
            return self._resp(cmd, bytes((3, 41)))

        if cmd == p.CMD_STATUS:
            return self._resp(cmd, self._status_block())

        if cmd == p.CMD_SETTINGS:
            return self._resp(cmd, bytes(self.settings))

        if cmd == p.CMD_SET_MODE:
            power, value, selector = payload[0], payload[1], payload[2]
            self.power_status = power
            if selector == p.SELECTOR_POWER_MODE:
                self.mode = value
            else:
                self.run_status = value
            return self._resp(cmd, b"\x01")

        if cmd == p.CMD_SET_SETTINGS:
            self.settings = bytearray(payload)
            return self._resp(cmd, b"\x01")

        if cmd == p.CMD_HISTORY:
            pending = len(self.history) * p.WORK_RECORD_SIZE
            return self._resp(cmd, b"\x01" + pending.to_bytes(4, "big"))

        if cmd == p.CMD_STREAM_SETTING:
            # Only now does the real firmware start sending chunks.
            self.stream_setting = payload
            self.pending_stream = self._history_frames()
            return self._resp(cmd, b"\x01")

        if cmd == p.CMD_STREAM_ACK and frame.type == p.TYPE_RESPONSE:
            return None  # our bitmap; nothing more to send

        if cmd == p.CMD_STREAM_END and frame.type == p.TYPE_RESPONSE:
            return None

        if cmd == p.CMD_RESET_FILTER:
            self.filter_percent = 100
            return self._resp(cmd, b"\x01")

        return self._resp(cmd, b"\x01")

    def _history_frames(self) -> list[bytes]:
        """Chunk the buffered records into stream frames, two records each."""
        blob = b"".join(
            raw.to_bytes(4, "big") + stay.to_bytes(2, "big")
            for raw, stay in self.history
        )
        per_chunk = 12  # two records
        chunks = [blob[i : i + per_chunk] for i in range(0, len(blob), per_chunk)]
        total = len(chunks)
        frames = []
        for index, chunk in enumerate(chunks):
            frames.append(
                bytes(
                    (
                        *p.STREAM_HEADER,
                        68,
                        p.TYPE_NO_RESPONSE,
                        index,
                        total,
                        len(chunk) & 0xFF,
                        (len(chunk) >> 8) & 0xFF,
                        *chunk,
                    )
                )
            )
        # Then the checkpoint and the end marker, both device-initiated.
        frames.append(p.build_frame(p.CMD_STREAM_ACK, b"", seq=7))
        frames.append(p.build_frame(p.CMD_STREAM_END, b"", seq=8))
        return frames

    def push_status(self) -> bytes:
        """Build an unsolicited cmd 230 frame (settings live at offset 30)."""
        body = self._status_block() + bytes(4) + bytes(self.settings[:10])
        return p.build_frame(p.CMD_PUSH_STATUS, body, msg_type=p.TYPE_REQUEST)

    def _status_block(self) -> bytes:
        return bytes(
            [
                self.power_status,
                self.run_status,
                self.mode,
                1,  # on mains
                0,  # night dnd inactive
                0,
                0,
                0,
                0,  # breakdown, water, low battery, filter warnings
                *self.pump_runtime.to_bytes(4, "big"),
                self.filter_percent,
                1,  # running
                *self.pump_runtime_today.to_bytes(4, "big"),
                0,  # no pet detected
                *(5260).to_bytes(2, "big"),
                *(4200).to_bytes(2, "big"),
                88,
                1,
            ]
        )

    @staticmethod
    def _resp(cmd: int, body: bytes) -> bytes:
        return p.build_frame(cmd, body, msg_type=p.TYPE_RESPONSE)


class FakeBleakClient:
    """Minimal BleakClient stand-in wired to a FakeFountain."""

    def __init__(self, fountain: FakeFountain, mtu: int = 20) -> None:
        self.fountain = fountain
        self.mtu = mtu
        self.is_connected = True
        self._notify_cb: Any = None
        self._decoder = p.FrameDecoder()
        self.writes: list[bytes] = []

    async def start_notify(self, _uuid: str, callback: Any) -> None:
        self._notify_cb = callback

    async def stop_notify(self, _uuid: str) -> None:
        self._notify_cb = None

    async def disconnect(self) -> None:
        self.is_connected = False

    async def write_gatt_char(
        self, _uuid: str, data: bytes, response: bool = False
    ) -> None:
        self.writes.append(bytes(data))
        for frame in self._decoder.feed(bytes(data)):
            reply = self.fountain.handle(frame)
            if reply is not None:
                self._emit(reply)
            for queued in self.fountain.pending_stream:
                self._emit(queued)
            self.fountain.pending_stream = []

    def _emit(self, payload: bytes) -> None:
        """Deliver a frame in MTU-sized chunks, like a real notification."""
        if self._notify_cb is None:
            return
        for i in range(0, len(payload), self.mtu):
            self._notify_cb(None, bytearray(payload[i : i + self.mtu]))


@pytest.fixture
def fountain() -> FakeFountain:
    return FakeFountain()


@pytest.fixture
def patched_connection(monkeypatch, fountain):
    """Point the device module's connector at the fake client."""
    from custom_components.petkit_ble import device as device_module

    client = FakeBleakClient(fountain)

    async def _establish(_cls, _ble_device, _name, _disconnect_cb, **_kwargs):
        client.is_connected = True
        return client

    monkeypatch.setattr(device_module, "establish_connection", _establish)
    return client
