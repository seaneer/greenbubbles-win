"""Regression tests for GB-001/GB-002: adaptive XOR key and structural validation."""

import struct
import threading
import unittest
import zlib
from pathlib import Path
from unittest import mock

from Crypto.Cipher import AES

from greenbubbles_win import media

V2_KEY = b"0123456789abcdef"
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def _chunk(kind: bytes, payload: bytes) -> bytes:
    crc = zlib.crc32(kind + payload) & 0xFFFFFFFF
    return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", crc)


def make_png() -> bytes:
    ihdr = struct.pack(">IIBBBBB", 2, 2, 8, 2, 0, 0, 0)
    raw = b"\x00" + b"\xff\x00\x00" * 2 + b"\x00" + b"\x00\xff\x00" * 2
    idat = zlib.compress(raw)
    return PNG_SIGNATURE + _chunk(b"IHDR", ihdr) + _chunk(b"IDAT", idat) + _chunk(b"IEND", b"")


def make_wxgf(tail_start: int = 64, tail_size: int = 4096) -> bytes:
    header = b"wxgf" + b"\x00" * (tail_start - 4)
    nal = b"\x00\x00\x00\x01\x40\x01" + b"payload" * 8
    tail = (nal * ((tail_size // len(nal)) + 1))[:tail_size]
    return header + tail


def wrap_v2(plain: bytes, xor_key: int) -> bytes:
    """Build a V2 container whose AES section covers the first 64 bytes."""

    prefix = plain[:64]
    if len(prefix) < 64:
        raise ValueError("plaintext too short for the 64-byte AES section")
    tail = plain[64:]
    encrypted = AES.new(V2_KEY, AES.MODE_ECB).encrypt(prefix + b"\x00" * 16)
    masked = bytes(byte ^ xor_key for byte in tail)
    header = (
        media.V2_MAGIC
        + struct.pack("<I", 64)
        + struct.pack("<I", len(tail))
        + b"\x00"
    )
    return header + encrypted + masked


class AdaptiveXorKeyTests(unittest.TestCase):
    def _assert_decrypts(self, plain: bytes, xor_key: int, expected_extension: str):
        data = wrap_v2(plain, xor_key)
        recovered, extension, fmt = media.decrypt_dat(data, V2_KEY)
        self.assertEqual(extension, expected_extension)
        self.assertEqual(recovered, plain)
        self.assertIn(f"xor=0x{xor_key:02x}", fmt)
        valid, _ = media.validate_image(recovered, extension)
        self.assertTrue(valid)

    def test_png_with_legacy_key(self):
        self._assert_decrypts(make_png(), 0x37, "png")

    def test_png_with_current_batch_key(self):
        self._assert_decrypts(make_png(), 0x1C, "png")

    def test_wxgf_with_current_batch_key(self):
        with mock.patch("greenbubbles_win.media.shutil.which", return_value=None):
            self._assert_decrypts(make_wxgf(), 0x1C, "wxgf")

    def test_unknown_key_is_reported_as_unverified(self):
        plain = make_png()
        data = bytearray(wrap_v2(plain, 0x1C))
        data[-1] ^= 0xFF  # no single XOR key can repair this tail
        recovered, extension, fmt = media.decrypt_dat(bytes(data), V2_KEY)
        self.assertEqual(extension, "png")
        self.assertIn("unverified", fmt)
        valid, reason = media.validate_image(recovered, extension)
        self.assertFalse(valid)
        self.assertTrue(reason)


class ValidationTests(unittest.TestCase):
    def test_truncated_png_fails(self):
        data = make_png()[:-8]
        valid, reason = media.validate_image(data, "png")
        self.assertFalse(valid)
        self.assertIn("IEND", reason)

    def test_jpeg_without_eoi_fails(self):
        valid, _ = media.validate_image(b"\xff\xd8\xff\xe0" + b"0" * 40, "jpg")
        self.assertFalse(valid)

    def test_wxgf_without_nal_units_fails(self):
        valid, _ = media.validate_image(b"wxgf" + b"\x00" * 5000, "wxgf", tail_start=64)
        self.assertFalse(valid)


    def test_connector_rejects_invalid_image(self):
        from greenbubbles_win import query, service

        connector = object.__new__(service.Connector)
        connector.lock = threading.RLock()
        connector.store = object()
        bad = media.MediaFile(
            kind="image",
            name="broken.dat",
            source=Path("broken.dat"),
            size=0,
            extension="jpg",
            data=None,
            valid=False,
            note="decrypt invalid",
        )
        with mock.patch.object(query, "resolve_media_for", return_value=bad):
            with self.assertRaises(media.MediaValidationError):
                connector.media_bytes({"conversation": "c", "id": "m"})


if __name__ == "__main__":
    unittest.main()
