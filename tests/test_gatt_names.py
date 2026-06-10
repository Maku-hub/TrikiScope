"""Tests for UUID naming and value decoders."""

import struct

from trikiscope import gatt_names as g


def test_decode_text_accepts_strings():
    assert g.decode_text(b"Triki 1733538611") == "Triki 1733538611"
    assert g.decode_text(b"3.2.1-A") == "3.2.1-A"


def test_decode_text_rejects_binary():
    # Connection-parameter style binary should not be shown as text.
    assert g.decode_text(struct.pack("<HHHH", 6, 12, 0, 400)) is None
    assert g.decode_text(b"\x00\xff\xfe\x01") is None


def test_decode_ppcp():
    # min=6 (7.5ms), max=12 (15ms), latency=0, timeout=400 (4000ms)
    data = struct.pack("<HHHH", 6, 12, 0, 400)
    text = g.decode_ppcp(data)
    assert "7.50-15.00 ms" in text
    assert "slave latency 0" in text
    assert "4000 ms" in text


def test_interpret_dispatch():
    assert g.interpret("00002a19-0000-1000-8000-00805f9b34fb", b"\x64") == "100%"
    assert g.interpret("00002aa6-0000-1000-8000-00805f9b34fb", b"\x01") == "supported"
    assert g.interpret("00002aa6-0000-1000-8000-00805f9b34fb", b"\x00") == "not supported"
    assert g.interpret("00002a26-0000-1000-8000-00805f9b34fb", b"3.2.1-A") == "3.2.1-A"


def test_device_id_from_name():
    assert g.device_id_from_name("Triki 1733538611") == "1733538611"
    assert g.device_id_from_name("Triki") is None
    assert g.device_id_from_name(None) is None


def test_describe_known_and_vendor_uuids():
    assert "Central Address Resolution" in g.describe_characteristic("00002aa6-0000-1000-8000-00805f9b34fb")
    assert "Nordic UART Service" in g.describe_service("6e400001-b5a3-f393-e0a9-e50e24dcca9e")
    assert "LED control" in g.describe_characteristic("6e400004-b5a3-f393-e0a9-e50e24dcca9e")


def test_company_name_unknown():
    assert g.company_name(0x0059) == "Nordic Semiconductor ASA"
    assert "unknown" in g.company_name(0xFF00)
