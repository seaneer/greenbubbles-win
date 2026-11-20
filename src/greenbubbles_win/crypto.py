"""SQLCipher 4 / WCDB page crypto used by WeChat 4.x on Windows.

All parameters below are the ones WeChat 4.1 uses on both macOS and Windows:
4096-byte pages, AES-256-CBC, HMAC-SHA512 authentication, 80 reserved bytes
(16-byte IV + 64-byte HMAC), PBKDF2-HMAC-SHA512 with 256,000 rounds when a
passphrase has to be derived into a per-database key.
"""

from __future__ import annotations

import hashlib
import hmac as hmac_mod
import struct
from pathlib import Path

from Crypto.Cipher import AES

PAGE_SIZE = 4096
KDF_ITER = 256_000
HMAC_SIZE = 64
RESERVE = 80
KEY_SIZE = 32
SALT_SIZE = 16
IV_SIZE = 16

SQLITE_HDR = b"SQLite format 3\x00"


class CryptoError(Exception):
    pass


def derive_mac_key(enc_key: bytes, salt: bytes) -> bytes:
    """SQLCipher's HMAC key: PBKDF2-HMAC-SHA512(enc_key, salt XOR 0x3a, 2 rounds)."""

    mac_salt = bytes(byte ^ 0x3A for byte in salt)
    return hashlib.pbkdf2_hmac("sha512", enc_key, mac_salt, 2, dklen=KEY_SIZE)


def derive_enc_key_from_passphrase(passphrase: bytes, salt: bytes) -> bytes:
    """Account passphrase + per-database salt -> per-database AES-256 key."""

    return hashlib.pbkdf2_hmac("sha512", passphrase, salt, KDF_ITER, dklen=KEY_SIZE)


def read_salt(db_path: Path | str) -> bytes:
    with open(db_path, "rb") as handle:
        first = handle.read(PAGE_SIZE)
    if len(first) < PAGE_SIZE:
        raise CryptoError(f"database is smaller than one page: {db_path}")
    return first[:SALT_SIZE]


def verify_key(enc_key: bytes, page1: bytes) -> bool:
    """Check a candidate key against the HMAC SQLCipher stored on page 1."""

    if len(page1) < PAGE_SIZE:
        return False
    salt = page1[:SALT_SIZE]
    mac_key = derive_mac_key(enc_key, salt)
    hmac_data = page1[SALT_SIZE : PAGE_SIZE - RESERVE + IV_SIZE]
    stored = page1[PAGE_SIZE - HMAC_SIZE : PAGE_SIZE]
    digest = hmac_mod.new(mac_key, hmac_data, hashlib.sha512)
    digest.update(struct.pack("<I", 1))
    return hmac_mod.compare_digest(digest.digest(), stored)


def is_plain_sqlite(path: Path | str) -> bool:
    with open(path, "rb") as handle:
        head = handle.read(16)
    return head == SQLITE_HDR


def decrypt_page(page: bytes, enc_key: bytes, mac_key: bytes, page_num: int) -> bytes:
    """Decrypt one 4096-byte page into a full plaintext page (page_num is 0-based)."""

    offset = SALT_SIZE if page_num == 0 else 0
    hmac_data_end = PAGE_SIZE - RESERVE + IV_SIZE

    mac = hmac_mod.new(mac_key, page[offset:hmac_data_end], hashlib.sha512)
    mac.update(struct.pack("<I", page_num + 1))
    if not hmac_mod.compare_digest(mac.digest(), page[hmac_data_end : hmac_data_end + HMAC_SIZE]):
        raise CryptoError(f"page {page_num + 1}: HMAC verification failed")

    iv = page[PAGE_SIZE - RESERVE : PAGE_SIZE - RESERVE + IV_SIZE]
    cipher = AES.new(enc_key, AES.MODE_CBC, iv)
    data = cipher.decrypt(page[offset : PAGE_SIZE - RESERVE])

    head = SQLITE_HDR if page_num == 0 else b""
    return head + data + b"\x00" * RESERVE


def decrypt_database(db_path: Path | str, out_path: Path | str, enc_key: bytes) -> int:
    """Decrypt a whole database file page by page. Returns the page count."""

    db_path, out_path = Path(db_path), Path(out_path)
    salt = read_salt(db_path)
    mac_key = derive_mac_key(enc_key, salt)

    with open(db_path, "rb") as handle:
        page1 = handle.read(PAGE_SIZE)
    if not verify_key(enc_key, page1):
        raise CryptoError(f"key does not authenticate {db_path}")

    total = max(1, (db_path.stat().st_size + PAGE_SIZE - 1) // PAGE_SIZE)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    with open(db_path, "rb") as fin, open(out_path, "wb") as fout:
        for page_num in range(total):
            page = fin.read(PAGE_SIZE)
            if not page:
                break
            if len(page) < PAGE_SIZE:
                page += b"\x00" * (PAGE_SIZE - len(page))
            fout.write(decrypt_page(page, enc_key, mac_key, page_num))
    return total


def apply_wal(db_path: Path | str, wal_path: Path | str, plain_path: Path | str, enc_key: bytes) -> int:
    """Replay committed WCDB WAL frames into an already decrypted database copy."""

    wal_path, plain_path = Path(wal_path), Path(plain_path)
    if not wal_path.exists():
        return 0
    if wal_path.stat().st_size < 32:
        return 0

    salt = read_salt(db_path)
    mac_key = derive_mac_key(enc_key, salt)

    frame_size = 24 + PAGE_SIZE
    with open(wal_path, "rb") as wal:
        header = wal.read(32)
        magic = struct.unpack(">I", header[0:4])[0]
        if magic not in (0x377F0682, 0x377F0683):
            raise CryptoError(f"unexpected WAL magic in {wal_path}")
        salt1 = struct.unpack(">I", header[16:20])[0]
        salt2 = struct.unpack(">I", header[20:24])[0]

        body = wal.read()
        total_frames = len(body) // frame_size

        last_commit = -1
        for index in range(total_frames):
            frame = body[index * frame_size : index * frame_size + 24]
            f_salt1, f_salt2 = struct.unpack(">I", frame[8:12])[0], struct.unpack(">I", frame[12:16])[0]
            commit_size = struct.unpack(">I", frame[4:8])[0]
            page_number = struct.unpack(">I", frame[0:4])[0]
            if (
                page_number > 0
                and page_number <= 1_000_000
                and f_salt1 == salt1
                and f_salt2 == salt2
                and commit_size > 0
            ):
                last_commit = index
        if last_commit < 0:
            return 0

        patched = 0
        with open(plain_path, "r+b") as db:
            for index in range(last_commit + 1):
                frame = body[index * frame_size : index * frame_size + 24]
                page = body[index * frame_size + 24 : (index + 1) * frame_size]
                page_number, f_salt1, f_salt2 = (
                    struct.unpack(">I", frame[0:4])[0],
                    struct.unpack(">I", frame[8:12])[0],
                    struct.unpack(">I", frame[12:16])[0],
                )
                if page_number == 0 or f_salt1 != salt1 or f_salt2 != salt2:
                    continue
                if not page.strip(b"\x00"):
                    continue
                plain = decrypt_page(page, enc_key, mac_key, page_number - 1)
                db.seek((page_number - 1) * PAGE_SIZE)
                db.write(plain)
                patched += 1
        return patched
