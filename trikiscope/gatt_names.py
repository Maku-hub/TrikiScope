"""Human-readable names for Bluetooth SIG assigned UUIDs and company IDs.

Only a practical subset is included (the things you actually meet on a small
nRF52 sensor), plus helpers to normalise UUIDs and decode the standard
Device-Information / PnP-ID payloads.
"""

from __future__ import annotations

import struct
from typing import Optional

_BASE_SUFFIX = "-0000-1000-8000-00805f9b34fb"

# 16-bit SIG service UUIDs.
SERVICE_NAMES = {
    0x1800: "Generic Access",
    0x1801: "Generic Attribute",
    0x1802: "Immediate Alert",
    0x1803: "Link Loss",
    0x1804: "Tx Power",
    0x180A: "Device Information",
    0x180F: "Battery Service",
    0x1812: "Human Interface Device",
    0x1819: "Location and Navigation",
    0x181A: "Environmental Sensing",
    0x1826: "Fitness Machine",
    0xFE59: "Nordic DFU (Secure)",
}

# 16-bit SIG characteristic UUIDs.
CHARACTERISTIC_NAMES = {
    0x2A00: "Device Name",
    0x2A01: "Appearance",
    0x2A04: "Peripheral Preferred Connection Parameters",
    0x2A05: "Service Changed",
    0x2A19: "Battery Level",
    0x2A23: "System ID",
    0x2A24: "Model Number String",
    0x2A25: "Serial Number String",
    0x2A26: "Firmware Revision String",
    0x2A27: "Hardware Revision String",
    0x2A28: "Software Revision String",
    0x2A29: "Manufacturer Name String",
    0x2A50: "PnP ID",
    0x2A07: "Tx Power Level",
    0x2AA6: "Central Address Resolution",
}

DESCRIPTOR_NAMES = {
    0x2900: "Characteristic Extended Properties",
    0x2901: "Characteristic User Description",
    0x2902: "Client Characteristic Configuration",
    0x2903: "Server Characteristic Configuration",
    0x2904: "Characteristic Presentation Format",
}

# Vendor-specific UUIDs we know about for the Triki.
VENDOR_UUID_NAMES = {
    "6e400001-b5a3-f393-e0a9-e50e24dcca9e": "Nordic UART Service",
    "6e400002-b5a3-f393-e0a9-e50e24dcca9e": "NUS RX (write)",
    "6e400003-b5a3-f393-e0a9-e50e24dcca9e": "NUS TX (notify)",
    "6e400004-b5a3-f393-e0a9-e50e24dcca9e": "LED control (bit 0)",
}

# A small subset of Bluetooth SIG company identifiers.
COMPANY_IDS = {
    0x0006: "Microsoft",
    0x004C: "Apple, Inc.",
    0x0059: "Nordic Semiconductor ASA",
    0x0075: "Samsung Electronics",
    0x00E0: "Google",
    0x0171: "Amazon",
    0x0822: "Adafruit Industries",
}


def normalize_uuid(uuid: str) -> str:
    return str(uuid).lower()


def short_uuid(uuid: str) -> Optional[int]:
    """Return the 16-bit value if ``uuid`` is a standard SIG base UUID, else None."""
    u = normalize_uuid(uuid)
    if len(u) == 36 and u.startswith("0000") and u.endswith(_BASE_SUFFIX):
        try:
            return int(u[4:8], 16)
        except ValueError:
            return None
    if len(u) <= 6 and u.startswith("0x"):
        return int(u, 16)
    return None


def describe_service(uuid: str) -> str:
    return _describe(uuid, SERVICE_NAMES)


def describe_characteristic(uuid: str) -> str:
    return _describe(uuid, CHARACTERISTIC_NAMES)


def describe_descriptor(uuid: str) -> str:
    return _describe(uuid, DESCRIPTOR_NAMES)


def _describe(uuid: str, table: dict) -> str:
    u = normalize_uuid(uuid)
    if u in VENDOR_UUID_NAMES:
        return f"{VENDOR_UUID_NAMES[u]} ({u})"
    sid = short_uuid(u)
    if sid is not None and sid in table:
        return f"{table[sid]} (0x{sid:04X})"
    if sid is not None:
        return f"0x{sid:04X}"
    return u


def company_name(company_id: int) -> str:
    return COMPANY_IDS.get(company_id, f"0x{company_id:04X} (unknown)")


# -- Standard payload decoders -------------------------------------------------

def decode_text(data: bytes) -> Optional[str]:
    """Decode a UTF-8 string, but only if the payload really looks like text."""
    if not data:
        return None
    # Reject payloads that are mostly non-printable (binary characteristics
    # like connection parameters or System ID would otherwise show as garbage).
    printable = sum(1 for b in data if 32 <= b < 127 or b in (0, 9, 10, 13))
    if printable / len(data) < 0.7:
        return None
    value = data.decode("utf-8", errors="replace").strip("\x00 \t\r\n")
    return value or None


def decode_ppcp(data: bytes) -> Optional[str]:
    """Peripheral Preferred Connection Parameters (0x2A04)."""
    if len(data) < 8:
        return None
    min_i, max_i, latency, timeout = struct.unpack_from("<HHHH", data, 0)
    return (
        f"conn interval {min_i * 1.25:.2f}-{max_i * 1.25:.2f} ms, "
        f"slave latency {latency}, supervision timeout {timeout * 10} ms"
    )


def decode_central_address_resolution(data: bytes) -> Optional[str]:
    if not data:
        return None
    return "supported" if data[0] else "not supported"


def interpret(uuid: str, data: Optional[bytes]) -> Optional[str]:
    """Best-effort human-readable interpretation of a characteristic value."""
    if data is None:
        return None
    sid = short_uuid(uuid)
    if sid == 0x2A04:
        return decode_ppcp(data)
    if sid == 0x2A19:
        level = decode_battery_level(data)
        return f"{level}%" if level is not None else None
    if sid == 0x2A23:
        return decode_system_id(data)
    if sid == 0x2A50:
        return decode_pnp_id(data)
    if sid == 0x2A01:
        return decode_appearance(data)
    if sid == 0x2AA6:
        return decode_central_address_resolution(data)
    return decode_text(data)


def device_id_from_name(name: Optional[str]) -> Optional[str]:
    """The Triki advertises e.g. 'Triki 1733538611' - pull out the numeric id."""
    if not name:
        return None
    for token in name.split():
        if token.isdigit():
            return token
    return None


def decode_battery_level(data: bytes) -> Optional[int]:
    return data[0] if data else None


def decode_system_id(data: bytes) -> Optional[str]:
    if not data:
        return None
    return " ".join(f"{b:02X}" for b in data)


def decode_pnp_id(data: bytes) -> Optional[str]:
    if len(data) < 7:
        return " ".join(f"{b:02X}" for b in data) if data else None
    source = {1: "Bluetooth SIG", 2: "USB"}.get(data[0], f"source={data[0]}")
    vendor_id, product_id, product_version = struct.unpack_from("<HHH", data, 1)
    vendor_label = company_name(vendor_id) if data[0] == 1 else f"0x{vendor_id:04X}"
    return (
        f"{source} vendor=0x{vendor_id:04X} ({vendor_label}) "
        f"product=0x{product_id:04X} version=0x{product_version:04X}"
    )


def decode_appearance(data: bytes) -> Optional[str]:
    if len(data) < 2:
        return None
    value = struct.unpack_from("<H", data, 0)[0]
    category = value >> 6
    categories = {0: "Unknown", 1: "Phone", 64: "HID", 5: "Sports Watch", 16: "Generic Remote Control"}
    return f"0x{value:04X} (category {category}: {categories.get(category, 'unspecified')})"


def hexdump(data: bytes) -> str:
    return " ".join(f"{b:02X}" for b in data)


def ascii_preview(data: bytes) -> str:
    return "".join(chr(b) if 32 <= b < 127 else "." for b in data)
