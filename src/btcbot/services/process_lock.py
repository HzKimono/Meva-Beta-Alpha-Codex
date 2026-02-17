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


def _lock_file_path(key: str) -> Path:
    digest = hashlib.sha256(key.encode()).hexdigest()[:16]
    return Path(tempfile.gettempdir()) / f"btcbot-{digest}.lock"


def _pid_file_path(lock_path: Path) -> Path:
    return lock_path.with_suffix(".pid")


def _read_pid_text(pid_path: Path) -> str | None:
    try:
        text = pid_path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return text or None


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


@contextmanager
def single_instance_lock(*, db_path: str, account_key: str = "default"):
    """Acquire a singleton runtime lock.

    This lock uses OS file locking (flock/msvcrt) and is intended for local filesystems.
    Lock behavior over network filesystems (for example some NFS setups) may vary.
    """

    abs_db_path = os.path.abspath(db_path)
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
            owner_pid = _read_pid_text(pid_path)
            owner_text = f" owner_pid={owner_pid}" if owner_pid else ""
            raise RuntimeError(
                "LOCKED: Another btcbot instance is already running "
                f"for db/account scope db_path={abs_db_path} account_key={account_key} "
                f"lock_path={path}.{owner_text}"
            ) from exc

        try:
            fh.seek(0)
            fh.truncate(0)
            fh.write(f"{pid}\n".encode("utf-8"))
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
