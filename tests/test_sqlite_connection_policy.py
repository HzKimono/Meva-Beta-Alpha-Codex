from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path

ALLOWED_CONNECT_CALLER = Path("src/btcbot/persistence/sqlite/sqlite_connection.py")


@dataclass(frozen=True)
class Offender:
    path: str
    line: int
    kind: str


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
        if self._is_sqlite_connect_call(node.func):
            self.offenders.append((getattr(node, "lineno", 0), type(node.func).__name__))
        self.generic_visit(node)

    def _is_sqlite_connect_call(self, func: ast.AST) -> bool:
        # sqlite3.connect(...), alias.connect(...)
        if isinstance(func, ast.Attribute) and func.attr == "connect":
            if isinstance(func.value, ast.Name) and func.value.id in self.sqlite_module_aliases:
                return True
        # connect(...), alias imported via `from sqlite3 import connect as alias`
        if isinstance(func, ast.Name) and func.id in self.connect_function_aliases:
            return True
        # getattr(sqlite3, "connect")(...), getattr(alias, "connect")(...)
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
                    return True
        return False


def test_no_sqlite_connect_calls_outside_helper_module() -> None:
    offenders: list[Offender] = []

    for path in sorted(Path("src").rglob("*.py")):
        if path == ALLOWED_CONNECT_CALLER:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        visitor = _ConnectCallVisitor()
        visitor.visit(tree)
        for line, kind in visitor.offenders:
            offenders.append(Offender(path=str(path), line=line, kind=kind))

    assert offenders == [], (
        "sqlite connect policy violation(s): "
        + ", ".join(f"{item.path}:{item.line}:{item.kind}" for item in offenders)
    )
