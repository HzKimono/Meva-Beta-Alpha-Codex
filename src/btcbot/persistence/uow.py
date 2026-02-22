from __future__ import annotations

from dataclasses import dataclass
import logging
import sqlite3
from typing import Callable

from btcbot.persistence.sqlite.metrics_repo import SqliteMetricsRepo
from btcbot.persistence.sqlite.orders_repo import SqliteOrdersRepo
from btcbot.persistence.sqlite.risk_repo import SqliteRiskRepo
from btcbot.persistence.sqlite.sqlite_connection import create_sqlite_connection, ensure_min_schema
from btcbot.persistence.sqlite.trace_repo import SqliteTraceRepo

logger = logging.getLogger(__name__)


class UnitOfWork:
    def __init__(self, db_path: str, *, read_only: bool = False) -> None:
        self._db_path = db_path
        self.read_only = read_only
        self._conn: sqlite3.Connection | None = None
        self.risk: SqliteRiskRepo
        self.metrics: SqliteMetricsRepo
        self.trace: SqliteTraceRepo
        self.orders: SqliteOrdersRepo

    def __enter__(self) -> UnitOfWork:
        conn = create_sqlite_connection(self._db_path)
        ensure_min_schema(conn)
        if self.read_only:
            conn.execute("BEGIN")
        else:
            conn.execute("BEGIN IMMEDIATE")
        self._conn = conn
        self.risk = SqliteRiskRepo(conn, read_only=self.read_only)
        self.metrics = SqliteMetricsRepo(conn, read_only=self.read_only)
        self.trace = SqliteTraceRepo(conn, read_only=self.read_only)
        self.orders = SqliteOrdersRepo(conn, read_only=self.read_only)
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._conn is None:
            return
        try:
            if exc_type is None:
                self._conn.commit()
            else:
                self._conn.rollback()
        finally:
            self._conn.close()
            self._conn = None


@dataclass(frozen=True)
class UnitOfWorkFactory:
    db_path: str
    read_only: bool = False

    def __call__(self) -> UnitOfWork:
        return UnitOfWork(self.db_path, read_only=self.read_only)
