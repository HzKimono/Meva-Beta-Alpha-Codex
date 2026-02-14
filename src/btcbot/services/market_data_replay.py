from __future__ import annotations

import csv
import random
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from btcbot.domain.market_data_models import Candle, OrderBookTop, TickerStat
from btcbot.domain.symbols import canonical_symbol


class MarketDataSchemaError(ValueError):
    """Raised when replay input files do not match expected schema."""


class MarketDataReplay:
    def __init__(
        self,
        *,
        candles_by_symbol: dict[str, list[Candle]],
        orderbook_by_symbol: dict[str, list[OrderBookTop]],
        ticker_by_symbol: dict[str, list[TickerStat]],
        start_ts: datetime,
        end_ts: datetime,
        step_seconds: int,
        seed: int,
    ) -> None:
        if step_seconds <= 0:
            raise ValueError("step_seconds must be > 0")
        self._seed = int(seed)
        self._rng = random.Random(self._seed)
        self._start_ts = _ensure_utc(start_ts)
        self._end_ts = _ensure_utc(end_ts)
        if self._end_ts < self._start_ts:
            raise ValueError("end_ts must be >= start_ts")
        self._step = timedelta(seconds=int(step_seconds))
        self._current = self._start_ts
        self._idx = 0
        self._candles = _normalize_series(candles_by_symbol)
        self._orderbooks = _normalize_series(orderbook_by_symbol)
        self._tickers = _normalize_series(ticker_by_symbol)

    @property
    def seed(self) -> int:
        return self._seed

    def now(self) -> datetime:
        return self._current

    def advance(self) -> bool:
        nxt = self._current + self._step
        if nxt > self._end_ts:
            return False
        self._current = nxt
        self._idx += 1
        return True

    def get_candles(self, symbol: str, limit: int) -> list[Candle]:
        symbol_n = canonical_symbol(symbol)
        series = self._candles.get(symbol_n, [])
        if limit <= 0:
            return []
        upto = [item for item in series if item.ts <= self._current]
        return upto[-limit:]

    def get_orderbook(self, symbol: str) -> tuple[Decimal, Decimal]:
        symbol_n = canonical_symbol(symbol)
        point = _nearest_prior(self._orderbooks.get(symbol_n, []), self._current)
        if point is None:
            raise KeyError(f"orderbook not found for {symbol_n} at {self._current.isoformat()}")
        return point.best_bid, point.best_ask

    def get_ticker_stats(self) -> list[dict[str, object]]:
        rows: list[dict[str, object]] = []
        symbols = sorted(set(self._candles) | set(self._orderbooks) | set(self._tickers))
        for symbol in symbols:
            ticker = _nearest_prior(self._tickers.get(symbol, []), self._current)
            if ticker is None:
                candles = self.get_candles(symbol, 1)
                if not candles:
                    continue
                last = candles[-1].close
                ticker = TickerStat(
                    ts=candles[-1].ts,
                    last=last,
                    high=last,
                    low=last,
                    volume=Decimal("0"),
                    quote_volume=None,
                )
            rows.append(
                {
                    "pairSymbol": symbol,
                    "last": str(ticker.last),
                    "high": str(ticker.high),
                    "low": str(ticker.low),
                    "volume": str(ticker.volume),
                    "quoteVolume": (
                        str(ticker.quote_volume)
                        if ticker.quote_volume is not None
                        else str(ticker.volume)
                    ),
                    "ts": int(ticker.ts.timestamp()),
                }
            )
        return rows

    @classmethod
    def from_folder(
        cls,
        *,
        data_path: Path,
        start_ts: datetime,
        end_ts: datetime,
        step_seconds: int,
        seed: int,
    ) -> MarketDataReplay:
        data_root = Path(data_path)
        candles = _load_candles_dir(data_root / "candles")
        orderbooks = _load_orderbook_dir(data_root / "orderbook")
        tickers = _load_ticker_dir(data_root / "ticker")
        return cls(
            candles_by_symbol=candles,
            orderbook_by_symbol=orderbooks,
            ticker_by_symbol=tickers,
            start_ts=start_ts,
            end_ts=end_ts,
            step_seconds=step_seconds,
            seed=seed,
        )


def _ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _normalize_series(series_by_symbol: dict[str, list[object]]) -> dict[str, list[object]]:
    out: dict[str, list[object]] = {}
    for symbol, rows in series_by_symbol.items():
        normalized = canonical_symbol(symbol)
        out[normalized] = sorted(rows, key=lambda item: item.ts)
    return out


def _nearest_prior(series: list[object], now_ts: datetime) -> object | None:
    last = None
    for item in series:
        if item.ts <= now_ts:
            last = item
        else:
            break
    return last


def _iter_csv(path: Path, required: set[str]) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            raise MarketDataSchemaError(f"missing header in {path}")
        fieldnames = {name.strip() for name in reader.fieldnames}
        if not required.issubset(fieldnames):
            raise MarketDataSchemaError(
                f"invalid schema in {path}; required={sorted(required)} got={sorted(fieldnames)}"
            )
        rows: list[dict[str, str]] = []
        for row in reader:
            normalized = {str(key).strip(): str(value).strip() for key, value in row.items()}
            rows.append(normalized)
        return rows


def _parse_ts(raw: str, *, path: Path) -> datetime:
    token = str(raw).strip()
    if token.endswith("Z"):
        token = token[:-1] + "+00:00"
    try:
        if token.isdigit():
            if len(token) == 13:
                return datetime.fromtimestamp(int(token) / 1000, tz=UTC)
            return datetime.fromtimestamp(int(token), tz=UTC)
        return _ensure_utc(datetime.fromisoformat(token))
    except Exception as exc:  # noqa: BLE001
        raise MarketDataSchemaError(f"invalid ts {raw!r} in {path}") from exc


def _parse_decimal(raw: str, *, path: Path, col: str) -> Decimal:
    try:
        return Decimal(str(raw))
    except Exception as exc:  # noqa: BLE001
        raise MarketDataSchemaError(f"invalid decimal in {path} col={col} value={raw!r}") from exc


def _load_candles_dir(path: Path) -> dict[str, list[Candle]]:
    payload: dict[str, list[Candle]] = defaultdict(list)
    if not path.exists():
        return {}
    for file in sorted(path.glob("*.csv")):
        symbol = canonical_symbol(file.stem)
        for row in _iter_csv(file, {"ts", "open", "high", "low", "close", "volume"}):
            payload[symbol].append(
                Candle(
                    ts=_parse_ts(row["ts"], path=file),
                    open=_parse_decimal(row["open"], path=file, col="open"),
                    high=_parse_decimal(row["high"], path=file, col="high"),
                    low=_parse_decimal(row["low"], path=file, col="low"),
                    close=_parse_decimal(row["close"], path=file, col="close"),
                    volume=_parse_decimal(row["volume"], path=file, col="volume"),
                )
            )
    return dict(payload)


def _load_orderbook_dir(path: Path) -> dict[str, list[OrderBookTop]]:
    payload: dict[str, list[OrderBookTop]] = defaultdict(list)
    if not path.exists():
        return {}
    for file in sorted(path.glob("*.csv")):
        symbol = canonical_symbol(file.stem)
        for row in _iter_csv(file, {"ts", "best_bid", "best_ask"}):
            payload[symbol].append(
                OrderBookTop(
                    ts=_parse_ts(row["ts"], path=file),
                    best_bid=_parse_decimal(row["best_bid"], path=file, col="best_bid"),
                    best_ask=_parse_decimal(row["best_ask"], path=file, col="best_ask"),
                )
            )
    return dict(payload)


def _load_ticker_dir(path: Path) -> dict[str, list[TickerStat]]:
    payload: dict[str, list[TickerStat]] = defaultdict(list)
    if not path.exists():
        return {}
    for file in sorted(path.glob("*.csv")):
        symbol = canonical_symbol(file.stem)
        for row in _iter_csv(file, {"ts", "last", "high", "low", "volume"}):
            quote_volume_raw = row.get("quote_volume") or row.get("quoteVolume")
            payload[symbol].append(
                TickerStat(
                    ts=_parse_ts(row["ts"], path=file),
                    last=_parse_decimal(row["last"], path=file, col="last"),
                    high=_parse_decimal(row["high"], path=file, col="high"),
                    low=_parse_decimal(row["low"], path=file, col="low"),
                    volume=_parse_decimal(row["volume"], path=file, col="volume"),
                    quote_volume=(
                        _parse_decimal(quote_volume_raw, path=file, col="quote_volume")
                        if quote_volume_raw
                        else None
                    ),
                )
            )
    return dict(payload)
