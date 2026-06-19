"""Shuffled content pools with anti-repetition tracking."""
from __future__ import annotations

import random
from typing import Generic, TypeVar

T = TypeVar("T")


class RotatingPool(Generic[T]):
    """Draw items in shuffled order; recently played items are held back."""

    def __init__(self, items: list[T], *, recent_size: int = 15) -> None:
        unique: list[T] = []
        seen: set = set()
        for item in items:
            key = self._key(item)
            if key in seen:
                continue
            seen.add(key)
            unique.append(item)
        if not unique:
            raise ValueError("RotatingPool requires at least one item")
        self._all = unique
        cap = max(1, min(recent_size, len(unique) - 1)) if len(unique) > 1 else 0
        self._recent_size = cap
        self._queue: list[T] = []
        self._recent_keys: list = []

    @staticmethod
    def _key(item: T):
        if isinstance(item, tuple) and item:
            return item[0]
        if isinstance(item, dict):
            return item.get("prompt") or item.get("statement") or id(item)
        return item

    def _refill(self) -> None:
        blocked = set(self._recent_keys)
        candidates = [i for i in self._all if self._key(i) not in blocked]
        if not candidates:
            self._recent_keys.clear()
            candidates = list(self._all)
        random.shuffle(candidates)
        self._queue = candidates

    def draw(self) -> T:
        if not self._queue:
            self._refill()
        item = self._queue.pop(0)
        if self._recent_size:
            self._recent_keys.append(self._key(item))
            if len(self._recent_keys) > self._recent_size:
                self._recent_keys.pop(0)
        return item

    def __len__(self) -> int:
        return len(self._all)


class TriviaPool:
    """Rotating pool for trivia (question, answers) pairs."""

    def __init__(self, bank: dict[str, list[str]], *, recent_size: int = 20) -> None:
        items = list(bank.items())
        self._pool = RotatingPool(items, recent_size=recent_size)

    def draw(self) -> tuple[str, list[str]]:
        question, answers = self._pool.draw()
        return question, list(answers)

    def __len__(self) -> int:
        return len(self._pool)
