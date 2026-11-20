"""Resolve and decrypt WeChat 4.x media: images, files and videos.

Verified layout on WeChat 4.1.11 (Windows):

* ``msg/attach/<md5(username)>/<YYYY-MM>/Img/<md5>.dat`` -- images (``_t`` thumb, ``_h`` original)
* ``msg/file/<YYYY-MM>/<title>`` -- received files
* ``msg/video/<YYYY-MM>/<md5>.mp4`` -- videos

``.dat`` images use a 15-byte container::

    6B  magic ``07 08 56 32 08 07`` (V2) or ``07 08 56 31 08 07`` (V1)
    4B  aes_size (LE uint32)
    4B  xor_size (LE uint32)
    1B  padding
    payload = AES-128-ECB(aligned aes_size) | raw middle | XOR(xor_size, key)

The XOR key is per-batch, not fixed: observed ``0x1c`` and ``0x37`` on 4.1.11.24.
It is recovered by validating the decrypted bytes (PNG chunk CRC, JPEG decode,
GIF trailer, BMP/WEBP declared size, or an HEVC decode for ``wxgf``).

V2 key = ``md5(uin + wxid).hex()[:16].encode("ascii")``: ``uin`` is the numeric
account id from ``ilink/kvcomm/config.ini`` (``last_uin=``, base64), ``wxid`` is the
account directory name without its 4-character suffix.
"""

from __future__ import annotations

import base64
import hashlib
import os
import re
import shutil
import subprocess
import zlib
from dataclasses import dataclass
from pathlib import Path

from Crypto.Cipher import AES

V1_MAGIC = b"\x07\x08V1\x08\x07"
V2_MAGIC = b"\x07\x08V2\x08\x07"
V1_FIXED_KEY = b"cfcd208495d565ef"
HEADER_SIZE = 15
DEFAULT_XOR_KEY = 0x37
# Observed variants: 0x37 on older exports, 0x1c on WeChat 4.1.11 batches.
KNOWN_XOR_KEYS = (0x1C, 0x37)
MAX_BRUTE_FORCE_BYTES = 8 * 1024 * 1024

IMAGE_MAGICS = (
    ("wxgf", b"wxgf"),
    ("jpg", b"\xff\xd8\xff"),
    ("png", b"\x89PNG"),
    ("gif", b"GIF8"),
    ("webp", b"RIFF"),
    ("bmp", b"BM"),
)

_MD5_RE = re.compile(rb"[0-9a-fA-F]{32}")


class MediaValidationError(RuntimeError):
    """Raised when decrypted bytes fail structural validation."""


@dataclass
class MediaFile:
    kind: str
    name: str
    source: Path
    size: int
    extension: str
    data: bytes | None = None
    md5: str | None = None
    note: str | None = None
    valid: bool | None = None


def detect_kind(data: bytes) -> tuple[str, str]:
    """Return (extension, format-label) for decrypted image bytes."""

    for label, magic in IMAGE_MAGICS:
        if data.startswith(magic):
            if label == "webp" and b"WEBP" not in data[:16]:
                continue
            return label, label
    return "bin", "unknown"


def _validate_png(data: bytes) -> tuple[bool, str]:
    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        return False, "bad PNG signature"
    pos = 8
    seen_ihdr = False
    seen_idat = False
    while pos + 8 <= len(data):
        length = int.from_bytes(data[pos : pos + 4], "big")
        chunk_type = data[pos + 4 : pos + 8]
        end = pos + 12 + length
        if end > len(data):
            return False, f"PNG chunk {chunk_type!r} truncated"
        payload = data[pos + 8 : pos + 8 + length]
        crc = int.from_bytes(data[pos + 8 + length : end], "big")
        if (zlib.crc32(chunk_type + payload) & 0xFFFFFFFF) != crc:
            return False, f"PNG CRC mismatch in {chunk_type!r}"
        if chunk_type == b"IHDR":
            seen_ihdr = True
        elif chunk_type == b"IDAT":
            seen_idat = True
        elif chunk_type == b"IEND":
            if not seen_ihdr or not seen_idat:
                return False, "PNG missing IHDR/IDAT"
            return True, f"png chunks ok, {length}B iend"
        pos = end
    return False, "PNG missing IEND"


def _ffmpeg_decodes(data: bytes, fmt: str) -> tuple[bool, str]:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return False, "ffmpeg not found"
    try:
        proc = subprocess.run(
            [ffmpeg, "-v", "error", "-f", fmt, "-i", "pipe:0", "-frames:v", "1", "-f", "null", "-"],
            input=data,
            capture_output=True,
            timeout=20,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"ffmpeg failed: {exc}"
    if proc.returncode == 0 and not proc.stderr.strip():
        return True, "decode ok"
    detail = proc.stderr.decode("utf-8", "ignore").strip().splitlines()
    return False, (detail[0] if detail else f"ffmpeg exit {proc.returncode}")


def _validate_jpeg(data: bytes, decode_if_needed: bool = True) -> tuple[bool, str]:
    if not data.startswith(b"\xff\xd8"):
        return False, "bad JPEG SOI"
    if b"\xff\xd9" in data[-32:]:
        return True, "jpeg SOI/EOI ok"
    # WeChat's copy is sometimes written without the trailing EOI but still
    # decodes. Only accept that when a decoder confirms the scan data is sound;
    # a wrong XOR key also keeps the AES-decrypted SOI but never decodes.
    if decode_if_needed:
        ok, reason = _ffmpeg_decodes(data, "mjpeg")
        if ok:
            return True, "jpeg decodes, EOI missing"
        return False, f"JPEG missing EOI ({reason})"
    return False, "JPEG missing EOI"


def _validate_gif(data: bytes) -> tuple[bool, str]:
    if not data.startswith(b"GIF8"):
        return False, "bad GIF signature"
    if b";" not in data[-16:]:
        return False, "GIF missing trailer"
    return True, "gif trailer ok"


def _validate_bmp(data: bytes) -> tuple[bool, str]:
    if not data.startswith(b"BM") or len(data) < 14:
        return False, "bad BMP signature"
    declared = int.from_bytes(data[2:6], "little")
    if declared and abs(declared - len(data)) > 16:
        return False, f"BMP size mismatch {declared} vs {len(data)}"
    return True, "bmp header ok"


def _validate_webp(data: bytes) -> tuple[bool, str]:
    if not data.startswith(b"RIFF") or b"WEBP" not in data[:16]:
        return False, "bad WEBP signature"
    declared = int.from_bytes(data[4:8], "little") + 8
    if abs(declared - len(data)) > 16:
        return False, f"WEBP size mismatch {declared} vs {len(data)}"
    return True, "webp header ok"


def _hevc_start_codes(data: bytes, start: int) -> int:
    """Count plausible HEVC NAL units, used to pick the XOR key for ``wxgf``."""

    count = 0
    pos = data.find(b"\x00\x00\x00\x01", max(0, start))
    while pos >= 0 and pos + 5 < len(data):
        header = data[pos + 4]
        nal_type = (header >> 1) & 0x3F
        temporal_id = data[pos + 5] & 0x07
        if (header >> 7) == 0 and nal_type <= 40 and temporal_id != 0:
            count += 1
        pos = data.find(b"\x00\x00\x00\x01", pos + 4)
    return count


def _validate_wxgf(data: bytes, tail_start: int) -> tuple[bool, str]:
    if not data.startswith(b"wxgf") or len(data) < 64:
        return False, "bad wxgf header"
    # Decoding is the only reliable oracle for this container: the wrong XOR
    # key yields a stream that ffmpeg rejects (or reports QP/NAL errors).
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg:
        try:
            proc = subprocess.run(
                [ffmpeg, "-v", "error", "-f", "hevc", "-i", "pipe:0", "-frames:v", "1", "-f", "null", "-"],
                input=data,
                capture_output=True,
                timeout=20,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return False, f"ffmpeg failed: {exc}"
        if proc.returncode == 0 and not proc.stderr.strip():
            return True, "hevc decode ok"
        detail = proc.stderr.decode("utf-8", "ignore").strip().splitlines()
        return False, (detail[0] if detail else f"ffmpeg exit {proc.returncode}")
    # Without ffmpeg, fall back to counting NAL units in the XOR-covered tail.
    if len(data) - tail_start >= 2048:
        count = _hevc_start_codes(data, tail_start)
        if count == 0:
            return False, "no HEVC NAL units in xor section"
        return True, f"hevc nal units={count}"
    count = _hevc_start_codes(data, 0)
    return (count > 0), f"hevc nal units={count}"


def validate_image(
    data: bytes,
    extension: str | None = None,
    tail_start: int = 0,
    decode_if_needed: bool = True,
) -> tuple[bool, str]:
    """Structural validation; a wrong XOR key almost always fails here."""

    ext = extension or detect_kind(data)[0]
    if ext == "png":
        return _validate_png(data)
    if ext in {"jpg", "jpeg"}:
        return _validate_jpeg(data, decode_if_needed=decode_if_needed)
    if ext == "gif":
        return _validate_gif(data)
    if ext == "bmp":
        return _validate_bmp(data)
    if ext == "webp":
        return _validate_webp(data)
    if ext == "wxgf":
        return _validate_wxgf(data, tail_start)
    return False, "unknown image format"


def _xor_bytes(data: bytes, key: int) -> bytes:
    if not key:
        return data
    return data.translate(bytes(i ^ key for i in range(256)))


def _aes_ecb(key: bytes, ciphertext: bytes) -> bytes:
    cipher = AES.new(key, AES.MODE_ECB)
    return cipher.decrypt(ciphertext)


def _candidate_keys(tail: bytes) -> list[int]:
    keys = list(KNOWN_XOR_KEYS)
    if len(tail) <= MAX_BRUTE_FORCE_BYTES:
        keys.extend(k for k in range(256) if k not in KNOWN_XOR_KEYS)
    return keys


def decrypt_dat(data: bytes, v2_key: bytes | None) -> tuple[bytes, str, str]:
    """Decrypt a WeChat ``.dat`` image. Returns (plaintext, extension, format).

    The tail XOR key is not stable across WeChat builds. For V2 containers we
    try known keys plus the full 0..255 range and keep the first candidate that
    passes structural validation (PNG CRC/IEND, JPEG EOI, HEVC NAL units...).
    """

    if data[:6] == V2_MAGIC:
        if not v2_key:
            raise ValueError("V2 image needs the derived key but none was available")
        key, fmt = v2_key, "v2"
    elif data[:6] == V1_MAGIC:
        key, fmt = V1_FIXED_KEY, "v1"
    else:
        # Legacy single-byte XOR container: derive the key from the image magic.
        for _label, magic in IMAGE_MAGICS:
            if len(data) < len(magic):
                continue
            k = data[0] ^ magic[0]
            if all(data[i] ^ k == magic[i] for i in range(len(magic))):
                plain = bytes(b ^ k for b in data)
                ext, _ = detect_kind(plain)
                return plain, ext, "xor"
        raise ValueError("unrecognised .dat container")

    if len(data) < HEADER_SIZE:
        raise ValueError("truncated .dat header")
    aes_size = int.from_bytes(data[6:10], "little")
    xor_size = int.from_bytes(data[10:14], "little")
    aligned = (aes_size // 16 + 1) * 16
    payload = data[HEADER_SIZE:]
    if aligned > len(payload):
        raise ValueError("AES section exceeds payload")

    prefix = _aes_ecb(key, payload[:aligned])[:aes_size]
    raw_end = len(payload) - xor_size
    middle = payload[aligned:raw_end] if aligned < raw_end else b""
    tail = payload[raw_end:]

    if not xor_size:
        plain = prefix + middle
        ext, _ = detect_kind(plain)
        return plain, ext, fmt

    if fmt == "v1":
        plain = prefix + middle + _xor_bytes(tail, DEFAULT_XOR_KEY)
        ext, _ = detect_kind(plain)
        return plain, ext, f"{fmt}/xor=0x{DEFAULT_XOR_KEY:02x}"

    fallback: tuple[bytes, str, int] | None = None
    # wxgf needs a decode per candidate, so never brute-force 256 keys for it.
    probe = prefix + middle + _xor_bytes(tail, KNOWN_XOR_KEYS[0])
    keys = list(KNOWN_XOR_KEYS) if detect_kind(probe)[0] == "wxgf" else _candidate_keys(tail)
    for candidate in keys:
        plain = prefix + middle + _xor_bytes(tail, candidate)
        ext, _ = detect_kind(plain)
        ok, _reason = validate_image(
            plain,
            ext,
            tail_start=len(prefix) + len(middle),
            decode_if_needed=candidate in KNOWN_XOR_KEYS,
        )
        if ok:
            return plain, ext, f"{fmt}/xor=0x{candidate:02x}"
        if fallback is None and ext != "bin":
            fallback = (plain, ext, candidate)

    if fallback is not None:
        plain, ext, candidate = fallback
        return plain, ext, f"{fmt}/xor=0x{candidate:02x}/unverified"

    plain = prefix + middle + _xor_bytes(tail, DEFAULT_XOR_KEY)
    ext, _ = detect_kind(plain)
    return plain, ext, f"{fmt}/xor=0x{DEFAULT_XOR_KEY:02x}/unverified"


def read_uin(account_root: Path) -> str | None:
    """Find ``last_uin`` in the ilink config, which sits in %APPDATA% on Windows."""

    candidates: list[Path] = []
    appdata = os.environ.get("APPDATA")
    if appdata:
        candidates.append(Path(appdata) / "Tencent" / "xwechat" / "ilink" / "kvcomm" / "config.ini")
        ilink = Path(appdata) / "Tencent" / "xwechat" / "ilink"
        if ilink.is_dir():
            candidates.extend(sorted(ilink.glob("*/kvcomm/config.ini")))
    candidates.append(account_root / "app_data" / "radium" / "ilink" / "kvcomm" / "config.ini")

    for path in candidates:
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for line in text.splitlines():
            line = line.strip()
            if not line.startswith("last_uin="):
                continue
            value = line.split("=", 1)[1].strip()
            if not value:
                continue
            try:
                decoded = base64.b64decode(value).decode("ascii")
            except Exception:
                continue
            if decoded.isdigit():
                return decoded
    return None


def base_wxid(account_dir_name: str) -> str:
    """``wxid_abc_12ab`` -> ``wxid_abc``; ``wxid_example_1a2b`` -> ``wxid_example``."""

    parts = account_dir_name.rsplit("_", 1)
    if len(parts) == 2 and len(parts[1]) == 4:
        return parts[0]
    return account_dir_name


def v2_key(account) -> tuple[bytes | None, str | None]:
    uin = read_uin(account.account_dir)
    if not uin:
        return None, "no last_uin in config.ini"
    wxid = base_wxid(account.account_dir.name)
    digest = hashlib.md5((uin + wxid).encode("utf-8")).hexdigest()[:16]
    return digest.encode("ascii"), None


def image_md5(packed_info: bytes | None, raw_text: str | None = None) -> str | None:
    """The local image file md5 is embedded in ``packed_info_data`` as ASCII."""

    if packed_info:
        match = _MD5_RE.search(bytes(packed_info))
        if match:
            return match.group(0).decode("ascii")
    if raw_text:
        match = re.search(r'md5="([0-9a-fA-F]{32})"', raw_text)
        if match:
            return match.group(1)
    return None


class MediaResolver:
    """Best-effort resolver for media referenced by a message row."""

    def __init__(self, account) -> None:
        self.account = account
        self.attach_dir = account.account_dir / "msg" / "attach"
        self.file_dir = account.account_dir / "msg" / "file"
        self.video_dir = account.account_dir / "msg" / "video"
        self._key, self.key_error = v2_key(account)
        self._image_cache: dict[tuple[str, int, int], MediaFile] = {}

    # -- images ---------------------------------------------------------
    def bucket(self, username: str) -> Path:
        return self.attach_dir / hashlib.md5(username.encode("utf-8")).hexdigest()

    def image(self, username: str, md5: str) -> MediaFile | None:
        base = self.bucket(username)
        if not base.is_dir():
            return None
        candidates = []
        for month in base.iterdir():
            if not month.is_dir():
                continue
            img_dir = month / "Img"
            if not img_dir.is_dir():
                continue
            for entry in img_dir.iterdir():
                if entry.is_file() and entry.name.startswith(md5) and entry.name.endswith(".dat"):
                    candidates.append(entry)
        if not candidates:
            return None

        preferred = [
            img_dir_file
            for img_dir_file in candidates
            if img_dir_file.name == f"{md5}_h.dat"
        ] or [c for c in candidates if c.name == f"{md5}.dat"] or candidates
        best = preferred[0]
        prefer_hd = best.name == f"{md5}_h.dat"
        for candidate in preferred:
            if candidate.name == f"{md5}_h.dat":
                best = candidate
                prefer_hd = True
                break
            if candidate.name == f"{md5}.dat" and not prefer_hd:
                best = candidate

        # Message lists ask for a media summary per image; caching by file
        # identity keeps a 100-row page from decrypting the same file repeatedly.
        try:
            stamp = (str(best), best.stat().st_mtime_ns, best.stat().st_size)
        except OSError:
            stamp = None
        if stamp and stamp in self._image_cache:
            return self._image_cache[stamp]

        try:
            plain, ext, fmt = decrypt_dat(best.read_bytes(), self._key)
        except Exception as exc:
            result = MediaFile(
                kind="image",
                name=best.name,
                source=best,
                size=best.stat().st_size,
                extension=best.suffix.lstrip(".") or "dat",
                md5=md5,
                note=f"decrypt failed: {exc}",
                valid=False,
            )
            if stamp:
                self._remember(stamp, result)
            return result
        ok, reason = validate_image(plain, ext)
        thumb = best.name.endswith("_t.dat")
        if not ok:
            result = MediaFile(
                kind="image",
                name=best.name,
                source=best,
                size=best.stat().st_size,
                extension=ext,
                md5=md5,
                note=f"{fmt}; decrypt invalid: {reason}" + (" thumbnail" if thumb else ""),
                valid=False,
            )
            if stamp:
                self._remember(stamp, result)
            return result
        result = MediaFile(
            kind="image",
            name=best.name,
            source=best,
            size=len(plain),
            extension=ext,
            data=plain,
            md5=md5,
            note=f"{fmt}; {reason}" + (" thumbnail" if thumb else ""),
            valid=True,
        )
        if stamp:
            self._remember(stamp, result)
        return result

    def _remember(self, stamp: tuple[str, int, int], value: MediaFile) -> None:
        if len(self._image_cache) >= 32:
            self._image_cache.pop(next(iter(self._image_cache)))
        self._image_cache[stamp] = value

    # -- files ----------------------------------------------------------
    def file(self, title: str, size_hint: int | None = None, month: str | None = None) -> MediaFile | None:
        if not title:
            return None
        safe = title.replace("/", "_").replace("\\", "_")
        roots = [self.file_dir / month] if month else []
        if self.file_dir.is_dir():
            roots.extend(sorted((p for p in self.file_dir.iterdir() if p.is_dir()), reverse=True))
        roots.append(self.file_dir)
        for root in roots:
            candidate = root / safe
            if candidate.is_file():
                return MediaFile(
                    kind="file",
                    name=candidate.name,
                    source=candidate,
                    size=candidate.stat().st_size,
                    extension=candidate.suffix.lstrip(".") or "bin",
                )
        # Fallback for rewritten names (dedupe suffix, sanitised chars). Only inside the
        # message's own month, and only when the declared size roughly matches, so we never
        # silently substitute an unrelated file that happens to share a prefix.
        stem = Path(safe).stem[:24]
        suffix = Path(safe).suffix.lower()
        for root in roots[:1] if month else roots[:2]:
            if not root.is_dir():
                continue
            for candidate in root.glob(stem + "*"):
                if not candidate.is_file() or candidate.suffix.lower() != suffix:
                    continue
                if size_hint and abs(candidate.stat().st_size - size_hint) > max(1024, size_hint * 0.3):
                    continue
                return MediaFile(
                    kind="file",
                    name=candidate.name,
                    source=candidate,
                    size=candidate.stat().st_size,
                    extension=candidate.suffix.lstrip(".") or "bin",
                )
        return None

    # -- videos ---------------------------------------------------------
    def video(self, md5: str) -> MediaFile | None:
        if not md5 or not self.video_dir.is_dir():
            return None
        for month in sorted((p for p in self.video_dir.iterdir() if p.is_dir()), reverse=True):
            for candidate in month.iterdir():
                if candidate.is_file() and candidate.name.startswith(md5):
                    return MediaFile(
                        kind="video",
                        name=candidate.name,
                        source=candidate,
                        size=candidate.stat().st_size,
                        extension=candidate.suffix.lstrip(".") or "mp4",
                    )
        return None

    def summary(self) -> dict[str, object]:
        return {
            "attachDir": str(self.attach_dir),
            "v2Key": "derived" if self._key else None,
            "v2KeyError": self.key_error,
        }


def save(media: MediaFile, dest_dir: Path) -> Path:
    """Write resolved media into ``dest_dir``; returns the written path."""

    dest_dir.mkdir(parents=True, exist_ok=True)
    raw = Path(media.name)
    name = raw.name
    if not raw.suffix and media.extension:
        name = f"{raw.stem}.{media.extension}"
    target = dest_dir / name
    if media.data is not None:
        target.write_bytes(media.data)
    else:
        target.write_bytes(media.source.read_bytes())
    return target
