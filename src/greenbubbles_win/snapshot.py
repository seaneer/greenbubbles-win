"""Encrypted archive snapshots: BIP39 mnemonic + Argon2id + ChaCha20-Poly1305.

The snapshot is a single ``.gbvault`` file:

    magic  b"GBVAULT1"
    u32    header length (little endian)
    header JSON: {"formatVersion":1,"kdf":{...},"nonce":<b64>,"words":24,"createdAt":...}
    ciphertext  (ChaCha20-Poly1305 over a tar.gz of the archive directory)

The header is used as AEAD associated data, so tampering with the KDF parameters or
the nonce makes decryption fail rather than silently producing garbage.
"""

from __future__ import annotations

import base64
import io
import json
import os
import tarfile
import time
from pathlib import Path
from typing import Any

from argon2.low_level import hash_secret_raw, Type
from Crypto.Cipher import ChaCha20_Poly1305
from mnemonic import Mnemonic

MAGIC = b"GBVAULT1"
FORMAT_VERSION = 1
KDF_TIME_COST = 3
KDF_MEMORY_KIB = 65536
KDF_PARALLELISM = 4
KDF_HASH_LEN = 32


class SnapshotError(Exception):
    pass


def new_mnemonic(words: int = 24) -> str:
    return Mnemonic("english").generate(strength=256 if words == 24 else 128)


def _derive_key(phrase: str, salt: bytes) -> bytes:
    normalized = " ".join(phrase.split()).lower()
    if not Mnemonic("english").check(normalized):
        raise SnapshotError("mnemonic failed the BIP39 checksum")
    return hash_secret_raw(
        secret=normalized.encode("utf-8"),
        salt=salt,
        time_cost=KDF_TIME_COST,
        memory_cost=KDF_MEMORY_KIB,
        parallelism=KDF_PARALLELISM,
        hash_len=KDF_HASH_LEN,
        type=Type.ID,
    )


def _tar_bytes(root: Path) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        tar.add(str(root), arcname="archive")
    return buffer.getvalue()


def create(root: Path | str, out_file: Path | str, phrase: str | None = None) -> dict[str, Any]:
    root = Path(root)
    if not root.exists():
        raise SnapshotError(f"archive directory not found: {root}")
    phrase = phrase or new_mnemonic(24)

    salt = os.urandom(16)
    nonce = os.urandom(12)
    key = _derive_key(phrase, salt)

    header = {
        "formatVersion": FORMAT_VERSION,
        "createdAt": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "words": len(phrase.split()),
        "kdf": {
            "name": "argon2id",
            "timeCost": KDF_TIME_COST,
            "memoryKiB": KDF_MEMORY_KIB,
            "parallelism": KDF_PARALLELISM,
            "hashLen": KDF_HASH_LEN,
            "salt": base64.b64encode(salt).decode(),
        },
        "cipher": "chacha20poly1305",
        "nonce": base64.b64encode(nonce).decode(),
        "payload": "tar.gz",
    }
    header_bytes = json.dumps(header, ensure_ascii=False).encode("utf-8")

    payload = _tar_bytes(root)
    cipher = ChaCha20_Poly1305.new(key=key, nonce=nonce)
    cipher.update(header_bytes)
    ciphertext, tag = cipher.encrypt_and_digest(payload)

    out = Path(out_file)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("wb") as handle:
        handle.write(MAGIC)
        handle.write(len(header_bytes).to_bytes(4, "little"))
        handle.write(header_bytes)
        handle.write(ciphertext)
        handle.write(tag)

    return {
        "ok": True,
        "file": str(out),
        "bytes": out.stat().st_size,
        "plainBytes": len(payload),
        "words": len(phrase.split()),
        "mnemonic": phrase,
        "kdf": "argon2id(m=64MiB,t=3,p=4)",
        "cipher": "chacha20poly1305",
    }


def read_header(path: Path | str) -> dict[str, Any]:
    with Path(path).open("rb") as handle:
        magic = handle.read(len(MAGIC))
        if magic != MAGIC:
            raise SnapshotError("not a greenbubbles vault file")
        length = int.from_bytes(handle.read(4), "little")
        return json.loads(handle.read(length).decode("utf-8"))


def restore(path: Path | str, phrase: str, out_dir: Path | str, overwrite: bool = False) -> dict[str, Any]:
    path = Path(path)
    with path.open("rb") as handle:
        if handle.read(len(MAGIC)) != MAGIC:
            raise SnapshotError("not a greenbubbles vault file")
        length = int.from_bytes(handle.read(4), "little")
        header_bytes = handle.read(length)
        header = json.loads(header_bytes.decode("utf-8"))
        body = handle.read()

    ciphertext, tag = body[:-16], body[-16:]
    normalized = " ".join(phrase.split()).lower()
    if not Mnemonic("english").check(normalized):
        raise SnapshotError("mnemonic checksum is invalid; check the words and their order")
    kdf = header["kdf"]
    salt = base64.b64decode(kdf["salt"])
    nonce = base64.b64decode(header["nonce"])
    key = hash_secret_raw(
        secret=" ".join(phrase.split()).lower().encode("utf-8"),
        salt=salt,
        time_cost=kdf["timeCost"],
        memory_cost=kdf["memoryKiB"],
        parallelism=kdf["parallelism"],
        hash_len=kdf["hashLen"],
        type=Type.ID,
    )
    cipher = ChaCha20_Poly1305.new(key=key, nonce=nonce)
    cipher.update(header_bytes)
    try:
        payload = cipher.decrypt_and_verify(ciphertext, tag)
    except ValueError as exc:
        raise SnapshotError("decryption failed: wrong mnemonic or corrupted file") from exc

    out = Path(out_dir)
    if out.exists() and not overwrite:
        raise SnapshotError(f"destination exists: {out} (pass overwrite=True)")
    out.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as tar:
        _extract_safely(tar, out)

    return {
        "ok": True,
        "outDir": str(out),
        "bytes": len(payload),
        "createdAt": header.get("createdAt"),
        "extracted": sorted(p.name for p in out.iterdir()),
    }


def _extract_safely(tar: tarfile.TarFile, out: Path) -> None:
    """Extract members only when they stay under ``out``.

    ``tar.extractall`` follows ``..`` components and absolute paths, so a
    crafted vault could write outside the chosen directory.  Link members are
    rejected for the same reason.
    """

    root = out.resolve()
    for member in tar.getmembers():
        if member.issym() or member.islnk():
            raise SnapshotError(f"refusing to extract link member: {member.name}")
        target = (root / member.name).resolve()
        if target != root and root not in target.parents:
            raise SnapshotError(f"refusing to extract outside the destination: {member.name}")
    tar.extractall(str(root))
