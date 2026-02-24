from __future__ import annotations

from pathlib import Path


def test_no_direct_sqlite_connect_outside_helper() -> None:
    root = Path("src")
    offenders: list[str] = []
    for path in root.rglob("*.py"):
        if path.as_posix() == "src/btcbot/persistence/sqlite/sqlite_connection.py":
            continue
        text = path.read_text(encoding="utf-8")
        if "sqlite3.connect(" in text:
            offenders.append(path.as_posix())

    assert offenders == [], f"direct sqlite3.connect usage found: {offenders}"
