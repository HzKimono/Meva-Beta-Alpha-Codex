from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path

ALLOWED_CONNECT_CALLER = Path("src/btcbot/persistence/sqlite/sqlite_connection.py")
SCAN_ROOTS = (Path("src"), Path("scripts"), Path("tools"))


@dataclass(frozen=True)
class Offender:
    path: str
    line: int
    form: str


class _ConnectCallVisitor(ast.NodeVisitor):
    def __init__(self) -> None:
        self.sqlite_module_aliases: set[str] = set()
        self.connect_function_aliases: set[str] = set()
        self.offenders: list[tuple[int, str]] = []

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            if alias.name == "sqlite3":
                self.sqlite_module_aliases.add(alias.asname or alias.name)
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.module == "sqlite3":
            for alias in node.names:
                if alias.name == "connect":
                    self.connect_function_aliases.add(alias.asname or alias.name)
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        form = self._detect_sqlite_connect_call_form(node.func)
        if form is not None:
            self.offenders.append((getattr(node, "lineno", 0), form))
        self.generic_visit(node)

    def _detect_sqlite_connect_call_form(self, func: ast.AST) -> str | None:
        # sqlite3.connect(...), s.connect(...)
        if isinstance(func, ast.Attribute) and func.attr == "connect":
            if isinstance(func.value, ast.Name) and func.value.id in self.sqlite_module_aliases:
                return f"{func.value.id}.connect(...) (module alias)"

        # connect(...), c(...) where c imported via `from sqlite3 import connect as c`
        if isinstance(func, ast.Name) and func.id in self.connect_function_aliases:
            return f"{func.id}(...) (from sqlite3 import connect alias)"

        # getattr(sqlite3_alias, "connect")(...)
        if isinstance(func, ast.Call):
            if isinstance(func.func, ast.Name) and func.func.id == "getattr" and len(func.args) >= 2:
                target = func.args[0]
                attr_name = func.args[1]
                if (
                    isinstance(target, ast.Name)
                    and target.id in self.sqlite_module_aliases
                    and isinstance(attr_name, ast.Constant)
                    and attr_name.value == "connect"
                ):
                    return f'getattr({target.id}, "connect")(... ) (module alias)'
        return None


def _collect_offenders_from_source(source: str) -> list[tuple[int, str]]:
    tree = ast.parse(source)
    visitor = _ConnectCallVisitor()
    visitor.visit(tree)
    return visitor.offenders


def test_sqlite_connect_visitor_detects_all_call_forms() -> None:
    offenders = _collect_offenders_from_source(
        """
import sqlite3
import sqlite3 as s
from sqlite3 import connect
from sqlite3 import connect as c

sqlite3.connect('a.db')
s.connect('b.db')
connect('c.db')
c('d.db')
getattr(sqlite3, 'connect')('e.db')
getattr(s, 'connect')('f.db')
"""
    )

    forms = [form for _, form in offenders]
    assert "sqlite3.connect(...) (module alias)" in forms
    assert "s.connect(...) (module alias)" in forms
    assert "connect(...) (from sqlite3 import connect alias)" in forms
    assert "c(...) (from sqlite3 import connect alias)" in forms
    assert 'getattr(sqlite3, "connect")(... ) (module alias)' in forms
    assert 'getattr(s, "connect")(... ) (module alias)' in forms


def test_no_sqlite_connect_calls_outside_helper_module() -> None:
    offenders: list[Offender] = []

    for root in SCAN_ROOTS:
        if not root.exists() or not root.is_dir():
            continue
        for path in sorted(root.rglob("*.py")):
            if path == ALLOWED_CONNECT_CALLER:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            visitor = _ConnectCallVisitor()
            visitor.visit(tree)
            for line, form in visitor.offenders:
                offenders.append(Offender(path=str(path), line=line, form=form))

    assert offenders == [], (
        "sqlite connect policy violation(s): "
        + ", ".join(f"{item.path}:{item.line}:{item.form}" for item in offenders)
    )
