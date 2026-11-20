"""Locate WeChat 4.x data on Windows and manage the decrypted read cache."""

from __future__ import annotations

import glob
import hashlib
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path


class DiscoveryError(Exception):
    pass


def _drive_roots() -> list[str]:
    roots = []
    for letter in "CDEFGH":
        root = f"{letter}:\\"
        if os.path.exists(root):
            roots.append(root)
    return roots


def candidate_data_roots() -> list[Path]:
    """Every plausible ``xwechat_files`` parent, cheapest checks first."""

    roots: list[Path] = []

    appdata = os.environ.get("APPDATA")
    if appdata:
        config_dir = Path(appdata) / "Tencent" / "xwechat" / "config"
        for ini in sorted(glob.glob(str(config_dir / "*.ini"))):
            try:
                text = Path(ini).read_text(encoding="utf-8", errors="ignore").strip()
            except OSError:
                continue
            if text and Path(text).is_dir():
                roots.append(Path(text) / "xwechat_files")

    documents = Path(os.environ.get("USERPROFILE", ".")) / "Documents"
    roots.append(documents / "xwechat_files")
    roots.append(documents / "WeChat Files")

    for drive in _drive_roots():
        roots.append(Path(drive) / "xwechat_files")

    seen: list[Path] = []
    for root in roots:
        if root not in seen:
            seen.append(root)
    return seen


@dataclass
class Account:
    data_root: Path
    account_dir: Path
    db_dir: Path
    account_id: str
    newest_write: float

    @property
    def label(self) -> str:
        return self.account_dir.name


def _database_recency(db_dir: Path) -> float:
    newest = 0.0
    for path in db_dir.rglob("*.db"):
        try:
            newest = max(newest, path.stat().st_mtime)
        except OSError:
            continue
    return newest


def discover_accounts() -> list[Account]:
    found: list[Account] = []
    for data_root in candidate_data_roots():
        if not data_root.is_dir():
            continue
        for account_dir in sorted(data_root.iterdir()):
            if account_dir.name in {"all_users", "Backup", "old_backup"}:
                continue
            db_dir = account_dir / "db_storage"
            if not db_dir.is_dir():
                continue
            if not (db_dir / "contact").is_dir() or not (db_dir / "message").is_dir():
                continue
            newest = _database_recency(db_dir)
            found.append(
                Account(
                    data_root=data_root,
                    account_dir=account_dir,
                    db_dir=db_dir,
                    account_id=account_dir.name,
                    newest_write=newest,
                )
            )
    found.sort(key=lambda account: account.newest_write, reverse=True)
    return found


def discover_default_account(override: str | None = None) -> Account:
    if override:
        db_dir = Path(override)
        if not db_dir.is_dir():
            raise DiscoveryError(f"not a directory: {db_dir}")
        account_dir = db_dir.parent
        return Account(
            data_root=account_dir.parent,
            account_dir=account_dir,
            db_dir=db_dir,
            account_id=account_dir.name,
            newest_write=_database_recency(db_dir),
        )

    accounts = discover_accounts()
    if not accounts:
        raise DiscoveryError(
            "no WeChat 4.x database was found; sign in to WeChat for Windows once, "
            "or pass --db-root <path to db_storage>"
        )
    return accounts[0]


def wechat_version() -> str | None:
    try:
        import winreg
    except ImportError:
        return None
    keys = [
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
    ]
    for hive, path in keys:
        try:
            with winreg.OpenKey(hive, path) as root:
                for index in range(winreg.QueryInfoKey(root)[0]):
                    try:
                        name = winreg.EnumKey(root, index)
                        with winreg.OpenKey(root, name) as entry:
                            display = winreg.QueryValueEx(entry, "DisplayName")[0]
                            if isinstance(display, str) and "微信" in display:
                                return str(winreg.QueryValueEx(entry, "DisplayVersion")[0])
                    except OSError:
                        continue
        except OSError:
            continue
    return None


def account_source_id(account: Account) -> str:
    """Opaque, path-free account identity used in CLI output."""

    digest = hashlib.sha256(account.account_dir.name.encode("utf-8")).hexdigest()[:16]
    return f"acct_{digest}"


def project_root() -> Path:
    """Locate the checkout root for a ``src/`` layout or an installed package.

    ``state/`` must stay beside the project (``<root>/state``), never inside
    ``src/`` and never in ``%LOCALAPPDATA%``.
    """

    here = Path(__file__).resolve()
    for candidate in (here.parents[2], here.parents[1], here.parent):
        if (candidate / "pyproject.toml").is_file() or (candidate / "greenbubbles.py").is_file():
            return candidate
    return Path.cwd()


def state_dir() -> Path:
    """Everything this tool writes lives next to the project, not in %LOCALAPPDATA%."""

    override = os.environ.get("GREENBUBBLES_HOME")
    if override:
        return Path(override)
    return project_root() / "state"


def cache_root() -> Path:
    return state_dir() / "cache"


def key_file() -> Path:
    return state_dir() / "keys.json"


class ReadCache:
    """Decrypted, queryable copies of the live databases.

    WeChat keeps its files locked and encrypted while running, so every command
    works on a decrypted copy. Copies are refreshed when the source database or
    its WAL is newer than the copy.
    """

    def __init__(self, account: Account, keys) -> None:
        self.account = account
        self.keys = keys
        digest = hashlib.sha256(str(account.db_dir).encode("utf-8")).hexdigest()[:16]
        self.root = cache_root() / digest
        self.root.mkdir(parents=True, exist_ok=True)
        self.manifest_path = self.root / "manifest.json"
        self.manifest: dict[str, dict] = {}
        if self.manifest_path.exists():
            try:
                self.manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                self.manifest = {}

    def _save_manifest(self) -> None:
        tmp = self.manifest_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.manifest, indent=2), encoding="utf-8")
        os.replace(tmp, self.manifest_path)

    def plain_path(self, rel: str) -> Path:
        return self.root / "db" / rel

    def is_fresh(self, rel: str) -> bool:
        entry = self.manifest.get(rel)
        if not entry:
            return False
        plain = self.plain_path(rel)
        if not plain.exists():
            return False
        source = self.account.db_dir / rel
        if not source.exists():
            return False
        if abs(source.stat().st_mtime - entry.get("source_mtime", 0)) > 0.001:
            return False
        wal = source.with_name(source.name + "-wal")
        wal_mtime = wal.stat().st_mtime if wal.exists() else 0
        return abs(wal_mtime - entry.get("wal_mtime", 0)) <= 0.001

    def refresh(self, rel: str, progress=None) -> Path:
        from .crypto import apply_wal, decrypt_database, is_plain_sqlite

        source = self.account.db_dir / rel
        plain = self.plain_path(rel)
        if is_plain_sqlite(source):
            plain.parent.mkdir(parents=True, exist_ok=True)
            plain.write_bytes(source.read_bytes())
            self.manifest[rel] = {"source_mtime": source.stat().st_mtime, "wal_mtime": 0}
            self._save_manifest()
            return plain

        key = self.keys.key_for(rel)
        if key is None:
            raise DiscoveryError(f"no key stored for {rel}; run 'greenbubbles key capture'")

        if progress:
            progress(f"decrypting {rel}")
        decrypt_database(source, plain, key)
        wal = source.with_name(source.name + "-wal")
        if wal.exists():
            apply_wal(source, wal, plain, key)

        self.manifest[rel] = {
            "source_mtime": source.stat().st_mtime,
            "wal_mtime": wal.stat().st_mtime if wal.exists() else 0,
            "decrypted_at": time.time(),
        }
        self._save_manifest()
        return plain

    def ensure(self, rel: str, progress=None) -> Path:
        if not self.is_fresh(rel):
            return self.refresh(rel, progress)
        return self.plain_path(rel)

    def clear(self) -> None:
        import shutil

        if self.root.exists():
            shutil.rmtree(self.root, ignore_errors=True)
        self.root.mkdir(parents=True, exist_ok=True)
        self.manifest = {}
        self._save_manifest()

    def size_bytes(self) -> int:
        total = 0
        db_root = self.root / "db"
        if db_root.exists():
            for path in db_root.rglob("*.db"):
                total += path.stat().st_size
        return total
