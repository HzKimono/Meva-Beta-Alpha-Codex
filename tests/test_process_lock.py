from __future__ import annotations

from pathlib import Path

import pytest

from btcbot.services.process_lock import single_instance_lock


def test_single_instance_lock_blocks_second_acquire(tmp_path: Path) -> None:
    db_path = str(tmp_path / "state.db")
    with single_instance_lock(db_path=db_path, account_key="acct"):
        with pytest.raises(RuntimeError, match="already running"):
            with single_instance_lock(db_path=db_path, account_key="acct"):
                pass


def test_single_instance_lock_reacquire_after_release(tmp_path: Path) -> None:
    db_path = str(tmp_path / "state.db")
    with single_instance_lock(db_path=db_path, account_key="acct"):
        pass

    with single_instance_lock(db_path=db_path, account_key="acct"):
        pass


def test_single_instance_lock_writes_pid(tmp_path: Path) -> None:
    db_path = str(tmp_path / "state.db")
    lock_path = None
    with single_instance_lock(db_path=db_path, account_key="acct") as lock:
        lock_path = Path(lock.path)
        assert isinstance(lock.pid, int)
        assert lock.pid > 0

    assert lock_path is not None
    pid_raw = lock_path.with_suffix(".pid").read_text(encoding="utf-8").strip()
    assert pid_raw == str(lock.pid)

    assert lock_path.exists()


def test_single_instance_lock_path_is_deterministic_for_scope(tmp_path: Path) -> None:
    db_path = str(tmp_path / "state.db")

    with single_instance_lock(db_path=db_path, account_key="acct-a") as first:
        first_path = first.path

    with single_instance_lock(db_path=db_path, account_key="acct-a") as second:
        assert second.path == first_path

    with single_instance_lock(db_path=db_path, account_key="acct-b") as different_account:
        assert different_account.path != first_path
