"""Constants for the PetKit BLE integration."""

from __future__ import annotations

from typing import Final

DOMAIN: Final = "petkit_ble"

# Fired once per visit the fountain reports, so the logbook and automations can
# see individual drinks rather than only a running count.
EVENT_VISIT: Final = "petkit_ble_visit"

# --- GATT ---------------------------------------------------------------
# Service 0xAAA0 carries the PetKit application protocol.
# Confirmed against PetkitBleClientImpl: mWriteChar is CONTROL_UUID (aaa2),
# mNotifyChar is DATA_UUID (aaa1). Note this is the opposite of what the
# constant names suggest.
SERVICE_UUID: Final = "0000aaa0-0000-1000-8000-00805f9b34fb"
WRITE_UUID: Final = "0000aaa2-0000-1000-8000-00805f9b34fb"
NOTIFY_UUID: Final = "0000aaa1-0000-1000-8000-00805f9b34fb"

# The firmware always wants 8 bytes. The cloud hands out shorter secrets
# (a CTW3 returns 6), which the app left-pads with zeros before sending.
SECRET_LENGTH: Final = 8

# --- Config entry keys --------------------------------------------------
CONF_ADDRESS: Final = "address"
CONF_SECRET: Final = "secret"
CONF_DEVICE_ID: Final = "device_id"
CONF_SERIAL: Final = "serial"
CONF_MODEL: Final = "model"
CONF_ALIAS: Final = "alias"

# --- Tuning -------------------------------------------------------------
DEFAULT_SCAN_INTERVAL: Final = 120
CONF_SCAN_INTERVAL: Final = "scan_interval"

CONNECT_TIMEOUT: Final = 30.0
COMMAND_TIMEOUT: Final = 8.0
RECONNECT_DELAY: Final = 1.0

# Only used when keep-alive is turned off, for genuinely battery-only setups.
IDLE_DISCONNECT_DELAY: Final = 30.0

CONF_KEEP_ALIVE: Final = "keep_alive"

# Whether to hold the BLE link open between operations. Holding it makes
# commands respond immediately, but costs runtime on battery. "auto"
# follows the fountain's own reported power source (electric_status), so a
# mains-powered unit stays connected and a unit running on battery does not.
KEEP_ALIVE_AUTO: Final = "auto"
KEEP_ALIVE_ALWAYS: Final = "always"
KEEP_ALIVE_NEVER: Final = "never"
KEEP_ALIVE_OPTIONS: Final = (KEEP_ALIVE_AUTO, KEEP_ALIVE_ALWAYS, KEEP_ALIVE_NEVER)
DEFAULT_KEEP_ALIVE: Final = KEEP_ALIVE_AUTO

# --- Device identification ---------------------------------------------
# Byte 5 of the advertised service data identifies the model. Table sourced
# from slespersen/PetkitW5BLEMQTT and cross-checked against the app.
DEVICE_MODELS: Final[dict[int, dict[str, object]]] = {
    205: {"name": "Petkit_W5C", "alias": "W5C", "model": "Eversweet Mini", "type": 14},
    206: {"name": "Petkit_W5", "alias": "W5", "model": "Eversweet Mini", "type": 14},
    213: {"name": "Petkit_W5N", "alias": "W5N", "model": "Eversweet Mini", "type": 14},
    214: {"name": "Petkit_W4X", "alias": "W4X", "model": "Eversweet 3 Pro", "type": 14},
    217: {
        "name": "Petkit_CTW2",
        "alias": "CTW2",
        "model": "Eversweet Solo 2",
        "type": 14,
    },
    223: {"name": "Petkit_CTW3", "alias": "CTW3", "model": "Eversweet Max", "type": 24},
    228: {
        "name": "Petkit_W4XUVC",
        "alias": "W4X",
        "model": "Eversweet 3 Pro (UVC)",
        "type": 14,
    },
    246: {
        "name": "Petkit_CTW3_2",
        "alias": "CTW3",
        "model": "Eversweet Max",
        "type": 24,
    },
    247: {
        "name": "Petkit_CTW3_100",
        "alias": "CTW3",
        "model": "Eversweet Max 2",
        "type": 24,
    },
    248: {
        "name": "Petkit_CTW3UV",
        "alias": "CTW3",
        "model": "Eversweet Max (UV)",
        "type": 24,
    },
    249: {
        "name": "Petkit_CTW3UV_100",
        "alias": "CTW3",
        "model": "Eversweet Max 2 (UV)",
        "type": 24,
    },
}

# Local-name prefixes we accept during discovery.
NAME_PREFIXES: Final = ("Petkit_CTW3", "Petkit_CTW2", "Petkit_W5", "Petkit_W4X")

# Only CTW3 has a fully mapped command set in this integration.
SUPPORTED_ALIASES: Final = ("CTW3",)

# --- Enumerations -------------------------------------------------------
MODE_NORMAL: Final = 1
MODE_SMART: Final = 2

MODE_OPTIONS: Final = {MODE_NORMAL: "normal", MODE_SMART: "smart"}

BRIGHTNESS_LOW: Final = 1
BRIGHTNESS_MEDIUM: Final = 2
BRIGHTNESS_HIGH: Final = 3

BRIGHTNESS_OPTIONS: Final = {
    BRIGHTNESS_LOW: "low",
    BRIGHTNESS_MEDIUM: "medium",
    BRIGHTNESS_HIGH: "high",
}

# Water throughput / power coefficients, from the app's
# calculateWxEnergyForType. Used to reproduce the cloud's derived figures.
WATER_FLOW_L_PER_MIN: Final = 1.5
WATER_DIVISOR: Final[dict[str, float]] = {
    "W5C": 1.0,
    "W4X": 1.8,
    "CTW3": 3.0,
}
WATER_FLOW_OVERRIDE: Final[dict[str, float]] = {"W5C": 1.3}
POWER_COEFFICIENT: Final[dict[str, float]] = {"W5C": 0.182}
POWER_COEFFICIENT_DEFAULT: Final = 0.75
