from __future__ import annotations

import tomllib
from pathlib import Path

MAX_LINE_LENGTH = 240
MIN_LINES = 5

ROOT_CRITICAL_FILES = [
    Path(".github/workflows/ci.yml"),
    Path("pyproject.toml"),
    Path(".env.example"),
    Path("scripts/guard_multiline.py"),
]


def _line_length_issues(path: Path, text: str) -> list[str]:
    issues: list[str] = []
    for index, line in enumerate(text.splitlines(), start=1):
        if len(line) > MAX_LINE_LENGTH:
            issues.append(
                f"{path}: line {index} exceeds max length {MAX_LINE_LENGTH} (got {len(line)})"
            )
    return issues


def require_readable_multiline(path: Path, min_lines: int = MIN_LINES) -> list[str]:
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    issues: list[str] = []

    if not text.strip():
        issues.append(f"{path}: empty file")
        return issues

    if "\n" not in text:
        issues.append(f"{path}: missing newline characters")

    if len(lines) < min_lines:
        issues.append(f"{path}: expected at least {min_lines} lines, got {len(lines)}")

    issues.extend(_line_length_issues(path, text))
    return issues


def _validate_toml(path: Path) -> list[str]:
    try:
        tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        return [f"{path}: invalid TOML ({exc})"]
    return []


def _validate_workflow_yaml(path: Path) -> list[str]:
    text = path.read_text(encoding="utf-8")
    issues: list[str] = []

    if "\t" in text:
        issues.append(f"{path}: YAML must not contain tab indentation")

    required_snippets = [
        "on:",
        "push:",
        "pull_request:",
        "workflow_dispatch:",
        "jobs:",
        "python -m compileall src tests",
    ]
    for snippet in required_snippets:
        if snippet not in text:
            issues.append(f"{path}: missing required CI snippet '{snippet}'")

    return issues


def _iter_python_files() -> list[Path]:
    files: list[Path] = []
    for root in (Path("src"), Path("tests")):
        files.extend(sorted(root.rglob("*.py")))
    return files


def main() -> None:
    issues: list[str] = []

    for file_path in ROOT_CRITICAL_FILES:
        if not file_path.exists():
            issues.append(f"{file_path}: missing")
            continue
        issues.extend(require_readable_multiline(file_path))

    for py_file in _iter_python_files():
        issues.extend(require_readable_multiline(py_file, min_lines=2))

    pyproject = Path("pyproject.toml")
    if pyproject.exists():
        issues.extend(_validate_toml(pyproject))

    workflow = Path(".github/workflows/ci.yml")
    if workflow.exists():
        issues.extend(_validate_workflow_yaml(workflow))

    if issues:
        print("guard_multiline failed. Offending files/reasons:")
        for issue in issues:
            print(f" - {issue}")
        raise SystemExit(1)

    print("guard_multiline passed.")


if __name__ == "__main__":
    main()
