from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from btcbot.services.process_lock import (
    clear_stale_pid_file,
    get_lock_diagnostics,
    single_instance_lock,
)


@pytest.fixture(autouse=True)
def lock_dir_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BTCBOT_LOCK_DIR", str(tmp_path))


def test_single_instance_lock_blocks_second_acquire(tmp_path: Path) -> None:
    db_path = str(tmp_path / "state.db")
    with single_instance_lock(db_path=db_path, account_key="acct"):
        with pytest.raises(RuntimeError, match="LOCKED:"):
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

        pid_raw = lock_path.with_suffix(".pid").read_text(encoding="utf-8").strip()
        assert pid_raw == str(lock.pid)
        if sys.platform.startswith("win"):
            with pytest.raises(PermissionError):
                lock_path.read_text(encoding="utf-8")
        else:
            assert lock_path.read_text(encoding="utf-8").strip() == str(lock.pid)

    assert lock_path is not None
    assert lock_path.exists()
    assert not lock_path.with_suffix(".pid").exists()


def test_single_instance_lock_removes_pid_on_exception(tmp_path: Path) -> None:
    db_path = str(tmp_path / "state.db")
    lock_path = None

    with pytest.raises(RuntimeError, match="boom"):
        with single_instance_lock(db_path=db_path, account_key="acct") as lock:
            lock_path = Path(lock.path)
            raise RuntimeError("boom")

    assert lock_path is not None
    assert not lock_path.with_suffix(".pid").exists()


def test_single_instance_lock_path_is_deterministic_for_scope(tmp_path: Path) -> None:
    db_path = str(tmp_path / "state.db")

    with single_instance_lock(db_path=db_path, account_key="acct-a") as first:
        first_path = first.path

    with single_instance_lock(db_path=db_path, account_key="acct-a") as second:
        assert second.path == first_path

    with single_instance_lock(db_path=db_path, account_key="acct-b") as different_account:
        assert different_account.path != first_path


def test_single_instance_lock_blocks_across_processes(tmp_path: Path) -> None:
    db_path = str(tmp_path / "state.db")
    account_key = "acct-subprocess"

    script = (
        "import sys,time\n"
        "from btcbot.services.process_lock import single_instance_lock\n"
        "db_path=sys.argv[1]\n"
        "account_key=sys.argv[2]\n"
        "hold=float(sys.argv[3])\n"
        "with single_instance_lock(db_path=db_path, account_key=account_key):\n"
        "    print('LOCK_ACQUIRED', flush=True)\n"
        "    time.sleep(hold)\n"
    )

    env = os.environ.copy()
    env["PYTHONPATH"] = f"src{os.pathsep}" + env.get("PYTHONPATH", "")
    env["BTCBOT_LOCK_DIR"] = str(tmp_path)

    proc_a = subprocess.Popen(
        [sys.executable, "-c", script, db_path, account_key, "2"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )
    try:
        assert proc_a.stdout is not None
        started = proc_a.stdout.readline().strip()
        assert started == "LOCK_ACQUIRED"

        proc_b = subprocess.run(
            [sys.executable, "-c", script, db_path, account_key, "0"],
            capture_output=True,
            text=True,
            timeout=5,
            env=env,
        )
        combined = f"{proc_b.stdout}\n{proc_b.stderr}"
        assert proc_b.returncode != 0
        assert "LOCKED:" in combined
    finally:
        proc_a.wait(timeout=5)


def test_single_instance_lock_error_includes_owner_pid(tmp_path: Path) -> None:
    db_path = str(tmp_path / "state.db")
    with single_instance_lock(db_path=db_path, account_key="acct") as lock:
        with pytest.raises(RuntimeError, match=f"owner_pid={lock.pid}"):
            with single_instance_lock(db_path=db_path, account_key="acct"):
                pass


def test_single_instance_lock_allows_different_role_db_paths(tmp_path: Path) -> None:
    trader_db = str(tmp_path / "state_live.db")
    monitor_db = str(tmp_path / "state_monitor.db")
    with single_instance_lock(db_path=trader_db, account_key="trader-writer"):
        with single_instance_lock(db_path=monitor_db, account_key="monitor"):
            pass


def test_single_instance_lock_normalizes_db_path_scope(tmp_path: Path) -> None:
    db_file = tmp_path / "state.db"
    db_file.touch()
    relative = os.path.relpath(str(db_file), start=os.getcwd())
    absolute = str(db_file.resolve())
    with single_instance_lock(db_path=absolute, account_key="acct"):
        with pytest.raises(RuntimeError, match="LOCKED:"):
            with single_instance_lock(db_path=relative, account_key="acct"):
                pass


def test_clear_stale_pid_file_removes_dead_owner_metadata(tmp_path: Path) -> None:
    db_path = str(tmp_path / "state.db")
    diagnostics = get_lock_diagnostics(db_path=db_path, account_key="acct")
    diagnostics.pid_path.write_text("999999\n", encoding="utf-8")

    assert clear_stale_pid_file(db_path=db_path, account_key="acct") is True
    assert not diagnostics.pid_path.exists()
