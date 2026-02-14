from __future__ import annotations

import csv
import json
import random
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from btcbot.adapters.btcturk_http import BtcturkHttpClient
from btcbot.replay.validate import validate_replay_dataset


@dataclass(frozen=True)
class ReplayCaptureConfig:
    dataset: Path
    symbols: list[str]
    seconds: int
    interval_seconds: int


def _atomic_write_text(path: Path, content: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    tmp.replace(path)


def _atomic_write_csv(path: Path, fieldnames: list[str], rows: list[dict[str, str]]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    tmp.replace(path)


def init_replay_dataset(
    *, dataset_path: Path, seed: int = 123, write_synthetic: bool = True
) -> None:
    dataset_path.mkdir(parents=True, exist_ok=True)
    for folder in ("candles", "orderbook", "ticker"):
        (dataset_path / folder).mkdir(parents=True, exist_ok=True)

    readme = (
        "# Replay Dataset\n\n"
        "Required folders: candles/, orderbook/. Optional folder: ticker/.\n"
        "One CSV per symbol (e.g., BTCTRY.csv).\n"
        "Run `python -m btcbot.cli replay-init --dataset .\\data\\replay "
        "--seed 123` for synthetic data.\n"
    )
    _atomic_write_text(dataset_path / "README.md", readme)

    schema = {
        "candles": ["ts", "open", "high", "low", "close", "volume"],
        "orderbook": ["ts", "best_bid", "best_ask"],
        "ticker": ["ts", "last", "high", "low", "volume", "quote_volume"],
    }
    _atomic_write_text(
        dataset_path / "schema.json", json.dumps(schema, indent=2, sort_keys=True) + "\n"
    )

    if write_synthetic:
        rng = random.Random(seed)
        start = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
        points = [start + timedelta(minutes=i) for i in range(6)]
        candles: list[dict[str, str]] = []
        books: list[dict[str, str]] = []
        tickers: list[dict[str, str]] = []
        for idx, ts in enumerate(points):
            base = Decimal("100000") + Decimal(idx * 100) + Decimal(str(rng.randint(0, 5)))
            candles.append(
                {
                    "ts": ts.isoformat(),
                    "open": str(base),
                    "high": str(base + Decimal("80")),
                    "low": str(base - Decimal("80")),
                    "close": str(base + Decimal("20")),
                    "volume": str(Decimal("1.5") + Decimal(idx) / Decimal("10")),
                }
            )
            books.append(
                {
                    "ts": ts.isoformat(),
                    "best_bid": str(base + Decimal("10")),
                    "best_ask": str(base + Decimal("30")),
                }
            )
            tickers.append(
                {
                    "ts": ts.isoformat(),
                    "last": str(base + Decimal("20")),
                    "high": str(base + Decimal("80")),
                    "low": str(base - Decimal("80")),
                    "volume": str(Decimal("1.5") + Decimal(idx) / Decimal("10")),
                    "quote_volume": str(
                        (base + Decimal("20")) * (Decimal("1.5") + Decimal(idx) / Decimal("10"))
                    ),
                }
            )

        _atomic_write_csv(dataset_path / "candles" / "BTCTRY.csv", list(candles[0].keys()), candles)
        _atomic_write_csv(dataset_path / "orderbook" / "BTCTRY.csv", list(books[0].keys()), books)
        _atomic_write_csv(dataset_path / "ticker" / "BTCTRY.csv", list(tickers[0].keys()), tickers)


def capture_replay_dataset(config: ReplayCaptureConfig) -> None:
    if config.seconds <= 0:
        raise ValueError("--seconds must be > 0")
    if config.interval_seconds <= 0:
        raise ValueError("--interval-seconds must be > 0")

    init_replay_dataset(dataset_path=config.dataset, write_synthetic=False)
    iterations = max(1, config.seconds // config.interval_seconds)
    symbols = [symbol.strip().upper() for symbol in config.symbols if symbol.strip()]

    candles: dict[str, list[dict[str, str]]] = {symbol: [] for symbol in symbols}
    books: dict[str, list[dict[str, str]]] = {symbol: [] for symbol in symbols}
    tickers: dict[str, list[dict[str, str]]] = {symbol: [] for symbol in symbols}

    with BtcturkHttpClient() as client:
        for index in range(iterations):
            now_ts = datetime.now(UTC).isoformat()
            ticker_rows = {
                str(row.get("pairSymbol", "")).upper(): row for row in client.get_ticker_stats()
            }
            for symbol in symbols:
                bid, ask = client.get_orderbook(symbol)
                ticker_row = ticker_rows.get(symbol, {})
                last = str(
                    ticker_row.get("last") or (Decimal(str(bid)) + Decimal(str(ask))) / Decimal("2")
                )
                high = str(ticker_row.get("high") or last)
                low = str(ticker_row.get("low") or last)
                volume = str(ticker_row.get("volume") or "0")
                quote_volume = str(
                    ticker_row.get("quoteVolume") or ticker_row.get("quote_volume") or "0"
                )

                candles[symbol].append(
                    {
                        "ts": now_ts,
                        "open": last,
                        "high": high,
                        "low": low,
                        "close": last,
                        "volume": volume,
                    }
                )
                books[symbol].append({"ts": now_ts, "best_bid": str(bid), "best_ask": str(ask)})
                tickers[symbol].append(
                    {
                        "ts": now_ts,
                        "last": last,
                        "high": high,
                        "low": low,
                        "volume": volume,
                        "quote_volume": quote_volume,
                    }
                )

            if index < iterations - 1:
                time.sleep(config.interval_seconds)

    for symbol in symbols:
        _atomic_write_csv(
            config.dataset / "candles" / f"{symbol}.csv",
            ["ts", "open", "high", "low", "close", "volume"],
            candles[symbol],
        )
        _atomic_write_csv(
            config.dataset / "orderbook" / f"{symbol}.csv",
            ["ts", "best_bid", "best_ask"],
            books[symbol],
        )
        _atomic_write_csv(
            config.dataset / "ticker" / f"{symbol}.csv",
            ["ts", "last", "high", "low", "volume", "quote_volume"],
            tickers[symbol],
        )

    report = validate_replay_dataset(config.dataset)
    if not report.ok:
        raise ValueError("capture completed but dataset failed validation")
