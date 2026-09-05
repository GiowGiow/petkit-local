# PetKit BLE (local)

[![Validate](https://github.com/GiowGiow/petkit-local/actions/workflows/validate.yml/badge.svg)](https://github.com/GiowGiow/petkit-local/actions/workflows/validate.yml)
[![hacs](https://img.shields.io/badge/HACS-custom-41BDF5.svg)](https://hacs.xyz/docs/faq/custom_repositories)

> A fork of [b12e/petkit-local](https://github.com/b12e/petkit-local), tracking upstream
> and carrying fixes verified against a live fountain.
>
> - **0.4.4**: byte 1 of the status block is a run flag, not a suspend flag. The pump
>   switch inverted it, so a running fountain reported itself paused.

A Home Assistant integration that talks to PetKit Eversweet fountains **directly over
Bluetooth**. No PetKit account, no cloud round-trip, no second PetKit device acting as
a relay.

Built for the **Eversweet Max 2 (CTW3)**.

## Why this exists

The existing cloud integration reaches these fountains over Bluetooth *through PetKit's
servers*: it asks the cloud to have another PetKit WiFi device in your house relay
base64-encoded BLE frames to the fountain. A command travels to PetKit and back to reach
a device that is often in the same room.

This integration speaks the same protocol straight to the fountain's GATT service.

## Supported devices

| Model | Status |
| --- | --- |
| Eversweet Max 2 / CTW3 (incl. UV and 100 variants) | Supported |
| CTW2, W5, W4X | Discovered but not enabled - the CTW3 command set is assumed and untested |
| Yumshare feeders (D4SH), litter boxes, W7H | **Not possible.** Their Bluetooth radio only does WiFi provisioning and firmware updates; all control is cloud/MQTT |

## Installation

### HACS

Add this repository as a custom repository in HACS (HACS → Integrations → the three-dot
menu → Custom repositories), category **Integration**, then install **PetKit BLE (local)**
and restart Home Assistant.

### Manual

Copy `custom_components/petkit_ble` into your Home Assistant `config/custom_components/`
directory and restart.

Either way, the fountain should then be discovered automatically if it is in range of a
Bluetooth adapter or an ESPHome Bluetooth proxy. If it is not, add it from
**Settings → Devices & services → Add integration → PetKit BLE**.

## The secret, and what setup does to the PetKit app

Every fountain holds a secret that is written when the device is bound and checked on each
connection. You have two options at setup.

**Leave the secret blank (default).** Home Assistant derives its own key and claims the
fountain with it. Nothing ever contacts PetKit. The trade-off: the official app loses its
claim, and the fountain needs a power cycle before the app can reconnect. Pick this if Home
Assistant will be the only thing talking to the fountain.

**Paste PetKit's own secret.** Both the app and Home Assistant then keep working. Re-binding
the fountain in the app rotates the key, and the integration re-claims on the next failure.

### Getting the secret from your account

It is the `secret` field on the water fountain object in the PetKit API. One way to read it:

```bash
pip install pypetkitapi aiohttp
```

```python
import asyncio, aiohttp
from pypetkitapi import PetKitClient
from pypetkitapi.water_fountain_container import WaterFountain

async def main():
    async with aiohttp.ClientSession() as session:
        client = PetKitClient(
            username="you@example.com", password="...",
            region="BE", timezone="Europe/Brussels", session=session,
        )
        await client.get_devices_data()
        for device in client.petkit_entities.values():
            if isinstance(device, WaterFountain):
                print(device.name, device.mac, device.secret)

asyncio.run(main())
```

Note that `PetKitClient` needs an explicit `session=` - without one it fails with an
unhelpful `'NoneType' object has no attribute 'request'`.

The value comes back **shorter than the 8 bytes the firmware wants** - a CTW3 returns 6
bytes, 12 hex characters, like `1a2b3c4d5e6f`. That is expected. The integration left-pads
it with zeros exactly as the app's `ByteUtil.makeUpBtyesForward` does, so paste it as-is.

## Entities

**Controls** - power, pump run/pause, mode (normal / smart), light ring, light brightness,
do-not-disturb, child lock, proximity detection, smart and battery duty-cycle timings,
reset filter.

**Readings** - battery percent and voltage, supply voltage, filter remaining and estimated
days left, pump runtime today and total, water purified today and total, energy used,
pump state, signal strength, pet visits today, pet drinking time today, pet visits total,
pet drinking time total, last pet visit.

**Problems** - water low, filter due, fault, battery low, pet detected, mains power,
do-not-disturb active.

Water and energy figures are computed locally using the same coefficients the PetKit app
applies, so they should track what the app shows rather than being an independent guess.

The pump runtime counters those figures hang off occasionally come back a second lower than
the reading before them. A dip like that reads as a broken total, so the last high-water
mark is held until the fountain catches up; a real reset - the daily counter at midnight, or
the lifetime one after a factory reset - drops far enough to be told apart and is passed
straight through.

## Connection handling

The link is held open by default, which is what makes commands respond immediately rather
than paying for a connect plus handshake every time. It also keeps the fountain's own push
channel (command 230) live, so changes made at the device appear without waiting for a poll.

Holding a link costs nothing on mains but does cost runtime on battery, so the default
**Auto** setting follows the fountain's own reported power source: connected while it is
plugged in, disconnecting between polls once it falls back to battery. You can force this
either way in the integration's options.

## Pet visits

The fountain records every visit itself as a timestamp plus a stay duration, and buffers
them until something drains the buffer with command 212. That is how the PetKit app builds
accurate drink history without staying connected, and this integration does the same on
every poll. Those records are authoritative and do not depend on how often we sampled.

Before any records arrive - or if the history stream turns out not to work on a given
firmware - visits fall back to being reconstructed from the live `detect_status` flag,
which is only as good as the sample rate. The moment real records show up the fallback
stops counting, so the two can never double up.

The daily figures reset at local midnight; the totals do not, and both survive a restart -
they are persisted by the integration itself rather than recovered from entity state.
Only completed visits count toward them, so the numbers never go backwards. A visit that is
under way shows up as the `visit_in_progress` and `current_visit_seconds` attributes on the
daily drinking time instead.

## Protocol notes

Service `0000aaa0`, write to `0000aaa2`, notify on `0000aaa1`. Frames are:

```
FA FC FD | cmd | type | seq | len_lo | len_hi | payload | FB
```

Length is little-endian; integers inside payloads are big-endian. There is no checksum and
no encryption. [`protocol.py`](custom_components/petkit_ble/protocol.py) documents the
command set and every byte offset.

Two details cost me time, so they are worth stating plainly:

- The characteristic names are inverted from what you would guess. `CONTROL_UUID` (`aaa2`)
  is the **write** handle and `DATA_UUID` (`aaa1`) is the **notify** handle.
- In a command 230 push the settings block starts at byte **30**, not 26 - the app's parser
  skips bytes 26-29 - and the push omits the two proximity bytes entirely.
- Buffered history is a device-driven transfer: it sends chunks as stream frames, asks which
  arrived with command 67 (the reply is a bitmap where bit `31 - index` means received, and
  omitting a bit requests a resend), then ends with command 69. Each record is a 4-byte
  timestamp and a 2-byte stay duration.

## Testing

```bash
python -m pytest tests/ -q
```

The suite runs the client against a simulated fountain that speaks the real wire protocol,
including fragmented notifications and the settings read-modify-write cycle.

## Credit

The characteristic direction, the service-data model table, and the trick of claiming the
device with your own secret all come from
[slespersen/PetkitW5BLEMQTT](https://github.com/slespersen/PetkitW5BLEMQTT). Byte offsets
were verified against the decompiled PetKit app; where the two disagreed, the app won.

## Disclaimer

Not affiliated with, endorsed by, or supported by PetKit. Derived from static analysis of
the shipped Android app for interoperability. The PetKit name and logo belong to PetKit and
are used only to identify which devices this integration works with. Claiming a device with
your own secret changes state on the hardware - read the section above before you set it up.
