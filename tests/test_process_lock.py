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
