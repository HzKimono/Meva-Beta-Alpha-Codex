from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path


@dataclass(frozen=True)
class ValidationIssue:
    level: str
    code: str
    message: str
    action: str | None = None


@dataclass(frozen=True)
class DatasetValidationReport:
    ok: bool
    dataset_path: str
    issues: list[ValidationIssue]
    symbol_counts: dict[str, dict[str, int]]

    @property
    def errors(self) -> list[ValidationIssue]:
        return [issue for issue in self.issues if issue.level == "error"]

    @property
    def warnings(self) -> list[ValidationIssue]:
        return [issue for issue in self.issues if issue.level == "warning"]


_REQUIRED_LAYOUT: dict[str, set[str]] = {
    "candles": {"ts", "open", "high", "low", "close", "volume"},
    "orderbook": {"ts", "best_bid", "best_ask"},
    "ticker": {"ts", "last", "high", "low", "volume"},
}


def _parse_ts(raw: str) -> datetime:
    token = str(raw).strip()
    if token.endswith("Z"):
        token = token[:-1] + "+00:00"
    if token.isdigit() and len(token) in {10, 13}:
        return datetime.fromtimestamp(int(token) / (1000 if len(token) == 13 else 1), tz=UTC)
    parsed = datetime.fromisoformat(token)
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def validate_replay_dataset(
    dataset_path: Path, *, min_rows_per_file: int = 1
) -> DatasetValidationReport:
    root = Path(dataset_path)
    issues: list[ValidationIssue] = []
    symbol_counts: dict[str, dict[str, int]] = {"candles": {}, "orderbook": {}, "ticker": {}}

    if not root.exists():
        issues.append(
            ValidationIssue(
                level="error",
                code="dataset_missing",
                message=f"dataset path does not exist: {root}",
                action="Run: python -m btcbot.cli replay-init --dataset .\\data\\replay",
            )
        )
        return DatasetValidationReport(
            ok=False, dataset_path=str(root), issues=issues, symbol_counts=symbol_counts
        )
    if not root.is_dir():
        issues.append(
            ValidationIssue(
                level="error",
                code="dataset_not_directory",
                message=f"dataset path is not a directory: {root}",
            )
        )
        return DatasetValidationReport(
            ok=False, dataset_path=str(root), issues=issues, symbol_counts=symbol_counts
        )

    for folder, required_cols in _REQUIRED_LAYOUT.items():
        folder_path = root / folder
        if not folder_path.exists() and folder in {"candles", "orderbook"}:
            issues.append(
                ValidationIssue(
                    level="error",
                    code=f"missing_{folder}_folder",
                    message=f"dataset folder missing: {folder_path}",
                    action=f"Create folder: .\\data\\replay\\{folder}",
                )
            )
            continue
        csv_files = sorted(folder_path.glob("*.csv")) if folder_path.exists() else []
        if folder in {"candles", "orderbook"} and not csv_files:
            issues.append(
                ValidationIssue(
                    level="error",
                    code=f"missing_{folder}_files",
                    message=f"dataset folder has no CSV files: {folder_path}",
                )
            )
            continue

        for csv_path in csv_files:
            with csv_path.open("r", encoding="utf-8", newline="") as handle:
                reader = csv.DictReader(handle)
                headers = set(reader.fieldnames or [])
                if not required_cols.issubset(headers):
                    issues.append(
                        ValidationIssue(
                            level="error",
                            code="invalid_schema",
                            message=(
                                f"invalid schema in {csv_path}; "
                                f"required={sorted(required_cols)} got={sorted(headers)}"
                            ),
                        )
                    )
                    continue

                last_ts: datetime | None = None
                row_count = 0
                for row in reader:
                    row_count += 1
                    try:
                        current_ts = _parse_ts(str(row.get("ts", "")))
                    except Exception:
                        issues.append(
                            ValidationIssue(
                                level="error",
                                code="invalid_timestamp",
                                message=f"invalid ts in {csv_path} row={row_count}",
                            )
                        )
                        break
                    if last_ts is not None and current_ts < last_ts:
                        issues.append(
                            ValidationIssue(
                                level="error",
                                code="non_monotonic_timestamp",
                                message=f"non-monotonic ts in {csv_path} row={row_count}",
                            )
                        )
                        break
                    last_ts = current_ts

                if row_count < min_rows_per_file:
                    issues.append(
                        ValidationIssue(
                            level="error",
                            code="insufficient_rows",
                            message=(
                                f"{csv_path} has {row_count} rows; "
                                f"requires at least {min_rows_per_file}"
                            ),
                        )
                    )
                symbol_counts[folder][csv_path.stem.upper()] = row_count

    candle_symbols = set(symbol_counts["candles"])
    book_symbols = set(symbol_counts["orderbook"])
    if candle_symbols and book_symbols and candle_symbols != book_symbols:
        issues.append(
            ValidationIssue(
                level="warning",
                code="symbol_mismatch",
                message="candles/orderbook symbols differ; replay may be partial",
            )
        )

    ok = not any(issue.level == "error" for issue in issues)
    return DatasetValidationReport(
        ok=ok, dataset_path=str(root), issues=issues, symbol_counts=symbol_counts
    )
