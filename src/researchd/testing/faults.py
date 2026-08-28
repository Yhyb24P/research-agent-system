"""Deterministic one-shot fault injection for integration/recovery tests."""

from collections import Counter
from collections.abc import Mapping


class InjectedFault(RuntimeError):
    pass


class FaultInjector:
    def __init__(self, points: Mapping[str, BaseException] | None = None) -> None:
        self._points = dict(points or {})
        self.hits: Counter[str] = Counter()

    def hit(self, point: str) -> None:
        self.hits[point] += 1
        error = self._points.pop(point, None)
        if error is not None:
            raise error

    def arm(self, point: str, error: BaseException) -> None:
        self._points[point] = error
