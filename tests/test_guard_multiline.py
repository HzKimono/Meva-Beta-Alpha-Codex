from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType


def _load_guard_module() -> ModuleType:
    module_path = Path("scripts/guard_multiline.py")
    spec = importlib.util.spec_from_file_location("guard_multiline", module_path)
    assert spec and spec.loader, "Could not load scripts/guard_multiline.py as a module"

    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_guard_detects_single_line_file(tmp_path: Path) -> None:
    module = _load_guard_module()

    bad = tmp_path / "bad.py"
    bad.write_text("print('x')", encoding="utf-8")

    issues = module.require_readable_multiline(bad)

    assert issues, "Expected guard to report issues for a single-line file"


def test_guard_accepts_multiline_file(tmp_path: Path) -> None:
    module = _load_guard_module()

    good = tmp_path / "good.py"
    good.write_text(
        "from __future__ import annotations\n\n"
        "def hello() -> str:\n"
        "    return 'x'\n"
        "\n"
        "print(hello())\n",
        encoding="utf-8",
    )

    issues = module.require_readable_multiline(good)

    assert issues == [], f"Expected no issues for multiline file, got: {issues}"


def test_stage3_package_init_files_are_multiline() -> None:
    module = _load_guard_module()

    files = [
        Path("src/btcbot/accounting/__init__.py"),
        Path("src/btcbot/risk/__init__.py"),
        Path("src/btcbot/strategies/__init__.py"),
    ]

    for file_path in files:
        issues = module.require_readable_multiline(file_path, min_lines=2)
        assert issues == [], f"Expected no issues for {file_path}, got: {issues}"
