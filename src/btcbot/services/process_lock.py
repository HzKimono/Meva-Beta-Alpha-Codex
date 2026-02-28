from __future__ import annotations

import hashlib
import os
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO


@dataclass(frozen=True)
class ProcessLock:
    path: str
    handle: object
    pid: int


@dataclass(frozen=True)
class LockDiagnostics:
    lock_path: Path
    pid_path: Path
    lock_dir: Path
    owner_pid: int | None
    owner_pid_alive: bool


def _default_lock_dir() -> Path:
    if os.name == "nt":
        root = os.getenv("LOCALAPPDATA") or tempfile.gettempdir()
        return Path(root) / "btcbot" / "locks"
    return Path(tempfile.gettempdir()) / "btcbot-locks"


def get_lock_dir() -> Path:
    configured = os.getenv("BTCBOT_LOCK_DIR")
    lock_dir = Path(configured).expanduser() if configured else _default_lock_dir()
    lock_dir.mkdir(parents=True, exist_ok=True)
    if not lock_dir.is_dir():
        raise RuntimeError(f"lock directory is not a directory: {lock_dir}")
    return lock_dir.resolve()


def _normalize_db_for_lock(db_path: str) -> str:
    return str(Path(db_path).expanduser().resolve())


def _lock_file_path(key: str) -> Path:
    digest = hashlib.sha256(key.encode()).hexdigest()[:16]
    lock_dir = get_lock_dir()
    return lock_dir / f"btcbot-{digest}.lock"


def _pid_file_path(lock_path: Path) -> Path:
    return lock_path.with_suffix(".pid")


def _read_pid_text(pid_path: Path) -> str | None:
    try:
        text = pid_path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return text or None


def _pid_appears_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        # Portable process probing is unreliable without pywin32; assume unknown as alive.
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def get_lock_diagnostics(*, db_path: str, account_key: str = "default") -> LockDiagnostics:
    lock_key = f"{_normalize_db_for_lock(db_path)}::{account_key}"
    lock_path = _lock_file_path(lock_key)
    pid_path = _pid_file_path(lock_path)
    owner_pid_raw = _read_pid_text(pid_path)
    owner_pid = int(owner_pid_raw) if owner_pid_raw and owner_pid_raw.isdigit() else None
    owner_alive = _pid_appears_alive(owner_pid) if owner_pid is not None else False
    return LockDiagnostics(
        lock_path=lock_path,
        pid_path=pid_path,
        lock_dir=lock_path.parent,
        owner_pid=owner_pid,
        owner_pid_alive=owner_alive,
    )


def _write_pid_file(pid_path: Path, pid: int) -> None:
    tmp_path = pid_path.with_suffix(f".pid.{pid}.tmp")
    with open(tmp_path, "w", encoding="utf-8") as pid_file:
        pid_file.write(f"{pid}\n")
        pid_file.flush()
        os.fsync(pid_file.fileno())
    os.replace(tmp_path, pid_path)


def _remove_pid_file_if_owned(pid_path: Path, pid: int) -> None:
    try:
        pid_raw = pid_path.read_text(encoding="utf-8").strip()
    except OSError:
        return
    if pid_raw != str(pid):
        return
    try:
        pid_path.unlink()
    except OSError:
        return


def clear_stale_pid_file(*, db_path: str, account_key: str = "default") -> bool:
    diagnostics = get_lock_diagnostics(db_path=db_path, account_key=account_key)
    if diagnostics.owner_pid is None or diagnostics.owner_pid_alive:
        return False
    try:
        diagnostics.pid_path.unlink()
    except FileNotFoundError:
        return False
    return True


@contextmanager
def single_instance_lock(*, db_path: str, account_key: str = "default"):
    """Acquire a singleton runtime lock.

    This lock uses OS file locking (flock/msvcrt) and is intended for local filesystems.
    Lock behavior over network filesystems (for example some NFS setups) may vary.
    """

    abs_db_path = _normalize_db_for_lock(db_path)
    lock_key = f"{abs_db_path}::{account_key}"
    path = _lock_file_path(lock_key)
    pid_path = _pid_file_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR)
    fh: BinaryIO = os.fdopen(fd, "r+b")
    pid = os.getpid()
    lock_acquired = False
    try:
        try:
            if os.name == "nt":
                import msvcrt

                fh.seek(0)
                msvcrt_mod: Any = msvcrt
                msvcrt_mod.locking(fh.fileno(), msvcrt_mod.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            lock_acquired = True
        except OSError as exc:
            diagnostics = get_lock_diagnostics(db_path=abs_db_path, account_key=account_key)
            owner_text = (
                f" owner_pid={diagnostics.owner_pid} owner_alive={diagnostics.owner_pid_alive}"
                if diagnostics.owner_pid is not None
                else ""
            )
            stale_hint = (
                " If this is stale, run: btcbot state-db-unlock --db "
                f"\"{abs_db_path}\" --lock-account-key {account_key} --i-understand"
                if diagnostics.owner_pid is not None and not diagnostics.owner_pid_alive
                else ""
            )
            raise RuntimeError(
                "LOCKED: Another btcbot instance is already running "
                f"for db/account scope db_path={abs_db_path} account_key={account_key} "
                f"lock_path={path} lock_dir={diagnostics.lock_dir}.{owner_text}{stale_hint}"
            ) from exc

        try:
            fh.seek(0)
            fh.truncate(0)
            fh.write(f"{pid}\n".encode())
            fh.flush()
            os.fsync(fh.fileno())
        except OSError:
            pass

        _write_pid_file(pid_path, pid)
        yield ProcessLock(path=str(path), handle=fh, pid=pid)
    finally:
        try:
            if lock_acquired:
                if os.name == "nt":
                    import msvcrt

                    fh.seek(0)
                    msvcrt_mod_unlock: Any = msvcrt
                    msvcrt_mod_unlock.locking(fh.fileno(), msvcrt_mod_unlock.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        try:
            fh.close()
        except OSError:
            pass
        if lock_acquired:
            _remove_pid_file_if_owned(pid_path, pid)
