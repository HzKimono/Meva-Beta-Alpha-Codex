from __future__ import annotations

import hashlib
import os
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO


@dataclass(frozen=True)
class ProcessLock:
    path: str
    handle: object
    pid: int


def _lock_file_path(key: str) -> Path:
    digest = hashlib.sha256(key.encode()).hexdigest()[:16]
    return Path(tempfile.gettempdir()) / f"btcbot-{digest}.lock"


@contextmanager
def single_instance_lock(*, db_path: str, account_key: str = "default"):
    lock_key = f"{os.path.abspath(db_path)}::{account_key}"
    path = _lock_file_path(lock_key)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR)
    fh: BinaryIO = os.fdopen(fd, "r+b")
    pid = os.getpid()
    lock_acquired = False
    try:
        fh.seek(0)
        fh.truncate(0)
        fh.write(f"{pid}\n".encode())
        fh.flush()
        try:
            if os.name == "nt":
                import msvcrt

                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            lock_acquired = True
        except OSError as exc:
            raise RuntimeError(
                f"Another btcbot instance is already running for db/account lock: {path}"
            ) from exc
        yield ProcessLock(path=str(path), handle=fh, pid=pid)
    finally:
        try:
            if lock_acquired:
                if os.name == "nt":
                    import msvcrt

                    fh.seek(0)
                    msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        try:
            fh.close()
        except OSError:
            pass
