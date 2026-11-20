"""Read-only database-key acquisition for WeChat 4.x on Windows.

WeChat keeps every SQLite database encrypted with SQLCipher/WCDB. Windows
WeChat 4.1 keeps the per-database key material inside a live ``Config.Cipher``
object in the ``Weixin.exe`` process, so this module attaches to that process
read-only, locates the object, decodes its XOR-masked blob and proves every
candidate key against SQLCipher's page-1 HMAC before it is ever stored.

The object layout and mask below follow the MIT-licensed
``TANGandXUE/wcdb-key-tool`` (``wcdb_key_tool_windows.py``), which credits
kkocdko, wxchat-export and ylytdeng/wechat-decrypt. This module is a
re-implementation for this port; it never writes to WeChat, never patches the
client and never touches the network.
"""

from __future__ import annotations

import ctypes
import json
import os
import re
import struct
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .crypto import (
    PAGE_SIZE,
    SALT_SIZE,
    derive_enc_key_from_passphrase,
    read_salt,
    verify_key,
)

from ctypes import wintypes as wt

CONFIG_CIPHER_NAME = b"com.Tencent.WCDB.Config.Cipher"
CONFIG_XOR_MASK = bytes.fromhex(
    "d2c7442458020000004889442450488b"
    "450048844c2448488944254048584c24"
)
MAX_USER_ADDRESS = 0x0000_8000_0000_0000
CONFIG_BLOB_MAX = 1024
CONFIG_LITERAL_RE = re.compile(rb"[xX]'([0-9a-fA-F]{64,192})'")
LEGACY_KEYSPEC_RE = re.compile(rb"x'([0-9a-fA-F]{64,192})'")

MEM_COMMIT = 0x1000
READABLE_PROTECTIONS = {0x02, 0x04, 0x08, 0x10, 0x20, 0x40, 0x80}


class KeyError_(Exception):
    pass


@dataclass
class ProcessReading:
    pid: int
    needle_hits: int = 0
    string_refs: int = 0
    node_candidates: int = 0
    blob_count: int = 0
    candidate_count: int = 0
    verified: int = 0


@dataclass
class KeySet:
    """Per-database keys, shaped like greenbubbles' exported key file."""

    db_dir: str
    entries: dict[str, dict] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path | str) -> "KeySet":
        document = json.loads(Path(path).read_text(encoding="utf-8"))
        db_dir = document.get("_db_dir", "")
        entries = {k: v for k, v in document.items() if not k.startswith("_")}
        return cls(db_dir=db_dir, entries=entries)

    def save(self, path: Path | str) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload: dict[str, object] = {
            name: {
                "enc_key": entry["enc_key"],
                "salt": entry["salt"],
                "size_mb": entry.get("size_mb", 0),
            }
            for name, entry in sorted(self.entries.items())
        }
        payload["_db_dir"] = self.db_dir
        payload["_captured_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        payload["_source"] = "windows-config-cipher"
        path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding='utf-8')

    def key_for(self, rel_path: str) -> bytes | None:
        entry = self.entries.get(rel_path.replace("\\", "/")) or self.entries.get(rel_path)
        if not entry:
            return None
        return bytes.fromhex(entry["enc_key"])

    def __len__(self) -> int:
        return len(self.entries)


def _restrict_acl(path: Path) -> None:
    """Best-effort: keep the key file readable by the current user only."""

    try:
        subprocess.run(
            ["icacls", str(path), "/inheritance:r", "/grant:r", f"{os.environ.get('USERNAME')}:(R,W)"],
            capture_output=True,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Process memory access (read-only)
# ---------------------------------------------------------------------------


class _MBI(ctypes.Structure):
    _fields_ = [
        ("BaseAddress", ctypes.c_uint64),
        ("AllocationBase", ctypes.c_uint64),
        ("AllocationProtect", wt.DWORD),
        ("_pad1", wt.DWORD),
        ("RegionSize", ctypes.c_uint64),
        ("State", wt.DWORD),
        ("Protect", wt.DWORD),
        ("Type", wt.DWORD),
        ("_pad2", wt.DWORD),
    ]


class ProcessMemory:
    def __init__(self, pid: int) -> None:
        self.pid = pid
        self._kernel32 = ctypes.windll.kernel32
        self._handle = self._kernel32.OpenProcess(0x0010 | 0x0400, False, pid)
        if not self._handle:
            raise KeyError_(f"cannot open Weixin.exe (pid={pid}); try running as administrator")

    def close(self) -> None:
        if self._handle:
            self._kernel32.CloseHandle(self._handle)
            self._handle = None

    def __enter__(self) -> "ProcessMemory":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    def read(self, address: int, size: int) -> bytes:
        buf = ctypes.create_string_buffer(size)
        read = ctypes.c_size_t(0)
        if not self._kernel32.ReadProcessMemory(
            self._handle, ctypes.c_uint64(address), buf, size, ctypes.byref(read)
        ):
            return b""
        return buf.raw[: read.value]

    def regions(self) -> list[tuple[int, int]]:
        found: list[tuple[int, int]] = []
        address = 0
        mbi = _MBI()
        while address < 0x7FFF_FFFF_FFFF:
            if self._kernel32.VirtualQueryEx(
                self._handle, ctypes.c_uint64(address), ctypes.byref(mbi), ctypes.sizeof(mbi)
            ) == 0:
                break
            if (
                mbi.State == MEM_COMMIT
                and mbi.Protect in READABLE_PROTECTIONS
                and 0 < mbi.RegionSize < 500 * 1024 * 1024
            ):
                found.append((mbi.BaseAddress, mbi.RegionSize))
            next_address = mbi.BaseAddress + mbi.RegionSize
            if next_address <= address:
                break
            address = next_address
        return found

    def chunks(self, regions: list[tuple[int, int]], chunk_size: int = 2 * 1024 * 1024, overlap: int = 0):
        for base, size in regions:
            offset = 0
            tail = b""
            tail_base = base
            while offset < size:
                current = min(chunk_size, size - offset)
                chunk = self.read(base + offset, current)
                data_base = tail_base if tail else base + offset
                data = tail + chunk
                if data:
                    yield data_base, data
                    if overlap:
                        tail = data[-overlap:]
                        tail_base = data_base + max(0, len(data) - len(tail))
                    else:
                        tail = b""
                        tail_base = base + offset + current
                else:
                    tail = b""
                    tail_base = base + offset + current
                offset += current


def find_weixin_processes() -> list[int]:
    """Return Weixin.exe pids, largest working set first (main window process)."""

    result = subprocess.run(
        ["tasklist", "/FI", "IMAGENAME eq Weixin.exe", "/FO", "CSV", "/NH"],
        capture_output=True,
        text=True,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    candidates: list[tuple[int, int]] = []
    for line in result.stdout.strip().splitlines():
        if not line.strip() or "INFO" in line:
            continue
        parts = line.strip().strip('"').split('","')
        if len(parts) >= 5:
            try:
                candidates.append((int(parts[1]), int(parts[4].replace(",", "").replace(" K", "").strip() or 0)))
            except ValueError:
                continue
    if not candidates:
        raise KeyError_("Weixin.exe is not running; start WeChat and sign in first")
    candidates.sort(key=lambda item: item[1], reverse=True)
    return [pid for pid, _mem in candidates]


# ---------------------------------------------------------------------------
# Candidate decoding
# ---------------------------------------------------------------------------


def _xor(data: bytes, mask: bytes) -> bytes:
    return bytes(value ^ mask[index % len(mask)] for index, value in enumerate(data))


def _u64(data: bytes, offset: int) -> int:
    if offset < 0 or offset + 8 > len(data):
        return 0
    return struct.unpack_from("<Q", data, offset)[0]


def _plausible_key(key: bytes) -> bool:
    return len(key) == 32 and len(set(key)) >= 15 and key not in (b"\x00" * 32, b"\xff" * 32)


def _candidates_from_blob(blob: bytes):
    if not blob or len(blob) > CONFIG_BLOB_MAX:
        return []
    decoded = _xor(blob, CONFIG_XOR_MASK)
    out = []
    seen = set()
    for match in CONFIG_LITERAL_RE.finditer(decoded):
        run = match.group(1).decode("ascii").lower()
        starts = [0]
        if len(run) > 96:
            starts.extend(range(0, len(run) - 63, 32))
            starts.append(len(run) - 64)
        for start in dict.fromkeys(starts):
            if start < 0 or start + 64 > len(run):
                continue
            key_hex = run[start : start + 64]
            try:
                key = bytes.fromhex(key_hex)
            except ValueError:
                continue
            if not _plausible_key(key):
                continue
            embedded_salt = run[start + 64 : start + 96] if start + 96 <= len(run) else None
            item = (key_hex, embedded_salt)
            if item not in seen:
                seen.add(item)
                out.append(item)
    return out


def _page1(db_path: Path) -> bytes:
    with open(db_path, "rb") as handle:
        return handle.read(PAGE_SIZE)


def _try_candidate(key_hex: str, embedded_salt: str | None, by_salt: dict[str, list[Path]], key_map: dict[str, str]) -> int:
    try:
        key = bytes.fromhex(key_hex)
    except ValueError:
        return 0
    if not _plausible_key(key):
        return 0
    matched = 0
    targets = [embedded_salt] if embedded_salt in by_salt else list(by_salt)
    for salt_hex in targets:
        for db_path in by_salt.get(salt_hex, []):
            if verify_key(key, _page1(db_path)):
                key_map[salt_hex] = key_hex
                matched += 1
                break
    return matched


def _scan_config_cipher(memory: ProcessMemory, reading: ProcessReading, by_salt, key_map, needs_key) -> None:
    regions = memory.regions()
    needles: set[int] = set()
    overlap = len(CONFIG_CIPHER_NAME) - 1
    for data_base, data in memory.chunks(regions, overlap=overlap):
        position = data.find(CONFIG_CIPHER_NAME)
        while position >= 0:
            needles.add(data_base + position)
            position = data.find(CONFIG_CIPHER_NAME, position + 1)
    reading.needle_hits = len(needles)
    if not needles:
        return

    pairs = [
        struct.pack("<Q", address) + struct.pack("<Q", len(CONFIG_CIPHER_NAME)) for address in needles
    ]
    seen_candidates: set[tuple[str, str | None]] = set()

    for data_base, data in memory.chunks(regions, overlap=0x80):
        if not needs_key():
            return
        for pattern in pairs:
            position = data.find(pattern)
            while position >= 0:
                reading.string_refs += 1
                node = memory.read(data_base + position - 0x10, 0x50)
                if len(node) < 0x40 or _u64(node, 0x10) not in needles or _u64(node, 0x18) != len(
                    CONFIG_CIPHER_NAME
                ):
                    position = data.find(pattern, position + 1)
                    continue
                config_ptr = _u64(node, 0x28)
                if not (0x10000 <= config_ptr < MAX_USER_ADDRESS):
                    position = data.find(pattern, position + 1)
                    continue

                reading.node_candidates += 1
                obj = memory.read(config_ptr + 0x88, 0x28)
                if len(obj) < 0x18:
                    position = data.find(pattern, position + 1)
                    continue
                data_ptr, data_len = _u64(obj, 0x8), _u64(obj, 0x10)
                if not (0 < data_len <= CONFIG_BLOB_MAX and 0x10000 <= data_ptr < MAX_USER_ADDRESS):
                    position = data.find(pattern, position + 1)
                    continue
                blob = memory.read(data_ptr, int(data_len))
                if not blob or len(blob) != data_len:
                    position = data.find(pattern, position + 1)
                    continue
                reading.blob_count += 1
                for key_hex, embedded_salt in _candidates_from_blob(blob):
                    if (key_hex, embedded_salt) in seen_candidates:
                        continue
                    seen_candidates.add((key_hex, embedded_salt))
                    reading.candidate_count += 1
                    matched = _try_candidate(key_hex, embedded_salt, by_salt, key_map)
                    if matched:
                        reading.verified += 1
                position = data.find(pattern, position + 1)


def _scan_legacy_keyspec(memory: ProcessMemory, by_salt, key_map, needs_key) -> None:
    """Fallback for WeChat 4.0.x, where plaintext keyspecs linger in memory."""

    for _base, data in memory.chunks(memory.regions()):
        if not needs_key():
            return
        for match in LEGACY_KEYSPEC_RE.finditer(data):
            run = match.group(1).decode().lower()
            if len(run) < 96:
                continue
            key_hex, salt_hex = run[:64], run[64:96]
            if salt_hex not in by_salt:
                continue
            if _try_candidate(key_hex, salt_hex, by_salt, key_map):
                continue


def collect_databases(db_dir: Path | str) -> tuple[dict[str, list[Path]], dict[str, bytes]]:
    """Map salt -> database paths for every encrypted database under db_dir."""

    db_dir = Path(db_dir)
    by_salt: dict[str, list[Path]] = {}
    sizes: dict[str, bytes] = {}
    for path in sorted(db_dir.rglob("*.db")):
        name = path.name
        if name.endswith(("-wal", "-shm")):
            continue
        if path.stat().st_size < PAGE_SIZE:
            continue
        try:
            salt = read_salt(path).hex()
        except Exception:
            continue
        by_salt.setdefault(salt, []).append(path)
        sizes[salt] = salt.encode()
    return by_salt, sizes


def capture(db_dir: Path | str, passphrase: bytes | None = None) -> tuple[KeySet, list[ProcessReading]]:
    """Acquire keys for every database in db_dir, verifying each before use."""

    db_dir = Path(db_dir)
    by_salt, _sizes = collect_databases(db_dir)
    if not by_salt:
        raise KeyError_(f"no encrypted databases found under {db_dir}")

    key_map: dict[str, str] = {}
    readings: list[ProcessReading] = []
    remaining = set(by_salt)

    def needs_key() -> bool:
        return bool(remaining)

    started = time.time()
    for pid in find_weixin_processes():
        try:
            memory = ProcessMemory(pid)
        except KeyError_:
            continue
        with memory:
            reading = ProcessReading(pid=pid)
            _scan_config_cipher(memory, reading, by_salt, key_map, needs_key)
            remaining.difference_update(key_map)
            readings.append(reading)
            if not remaining:
                break
            _scan_legacy_keyspec(memory, by_salt, key_map, needs_key)
            remaining.difference_update(key_map)
            if not remaining:
                break

    if remaining and passphrase:
        for salt_hex in list(remaining):
            enc_key = derive_enc_key_from_passphrase(passphrase, bytes.fromhex(salt_hex))
            for db_path in by_salt[salt_hex]:
                if verify_key(enc_key, _page1(db_path)):
                    key_map[salt_hex] = enc_key.hex()
                    remaining.discard(salt_hex)
                    break

    if not key_map:
        raise KeyError_("no verified key was found; confirm WeChat is running and signed in")

    entries: dict[str, dict] = {}
    for salt_hex, paths in by_salt.items():
        enc_key_hex = key_map.get(salt_hex)
        if not enc_key_hex:
            continue
        for db_path in paths:
            rel = db_path.relative_to(db_dir).as_posix()
            entries[rel] = {
                "enc_key": enc_key_hex,
                "salt": salt_hex,
                "size_mb": round(db_path.stat().st_size / 1024 / 1024, 1),
            }

    elapsed = time.time() - started
    keys = KeySet(db_dir=str(db_dir), entries=entries)
    keys._elapsed = elapsed  # type: ignore[attr-defined]
    keys._matched = len(key_map)  # type: ignore[attr-defined]
    keys._total_salts = len(by_salt)  # type: ignore[attr-defined]
    return keys, readings


def verify_existing(keys: KeySet, db_dir: Path | str | None = None) -> tuple[int, int]:
    """Re-check stored keys against the databases currently on disk."""

    root = Path(db_dir or keys.db_dir)
    ok = missing = 0
    for rel, entry in keys.entries.items():
        path = root / rel
        if not path.exists():
            missing += 1
            continue
        if verify_key(bytes.fromhex(entry["enc_key"]), _page1(path)):
            ok += 1
        else:
            missing += 1
    return ok, missing
