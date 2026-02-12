from __future__ import annotations

from typing import Protocol

from btcbot.domain.intent import Intent
from btcbot.strategies.context import StrategyContext


class Strategy(Protocol):
    def generate_intents(self, context: StrategyContext) -> list[Intent]: ...
