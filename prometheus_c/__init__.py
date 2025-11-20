"""Minimal Prometheus client shim for offline environments."""

from __future__ import annotations

import math
from typing import Iterable, List

CONTENT_TYPE_LATEST = "text/plain; version=0.0.4; charset=utf-8"


class _BaseMetric:
    def __init__(self, name: str, documentation: str) -> None:
        self._name = name
        self._documentation = documentation
        _REGISTRY.append(self)

    def render(self) -> str:
        raise NotImplementedError


class Gauge(_BaseMetric):
    def __init__(self, name: str, documentation: str) -> None:
        super().__init__(name, documentation)
        self._value = 0.0

    def set(self, value: float) -> None:
        try:
            self._value = float(value)
        except (TypeError, ValueError):  # pragma: no cover - defensive
            return

    def render(self) -> str:
        return "\n".join(
            [
                f"# HELP {self._name} {self._documentation}",
                f"# TYPE {self._name} gauge",
                f"{self._name} {self._value}",
            ]
        )


class Histogram(_BaseMetric):
    DEFAULT_BUCKETS: List[float] = [
        0.1,
        0.5,
        1.0,
        2.5,
        5.0,
        10.0,
    ]

    def __init__(self, name: str, documentation: str, buckets: Iterable[float] | None = None) -> None:
        super().__init__(name, documentation)
        configured = sorted(set(buckets or self.DEFAULT_BUCKETS))
        self._buckets = [value for value in configured if math.isfinite(value)]
        self._buckets.append(math.inf)
        self._counts = [0 for _ in self._buckets]
        self._count = 0
        self._sum = 0.0

    def observe(self, value: float) -> None:
        try:
            numeric = float(value)
        except (TypeError, ValueError):  # pragma: no cover - defensive
            return
        self._count += 1
        self._sum += numeric
        for index, bound in enumerate(self._buckets):
            if numeric <= bound:
                self._counts[index] += 1

    def render(self) -> str:
        lines = [
            f"# HELP {self._name} {self._documentation}",
            f"# TYPE {self._name} histogram",
        ]
        cumulative = 0
        for bound, bucket_count in zip(self._buckets, self._counts):
            cumulative = bucket_count
            label = "+Inf" if math.isinf(bound) else repr(bound)
            lines.append(f'{self._name}_bucket{{le="{label}"}} {cumulative}')
        lines.append(f"{self._name}_sum {self._sum}")
        lines.append(f"{self._name}_count {self._count}")
        return "\n".join(lines)


_REGISTRY: List[_BaseMetric] = []


def generate_latest() -> bytes:
    payload = "\n".join(metric.render() for metric in _REGISTRY) + "\n"
    return payload.encode("utf-8")


__all__ = [
    "CONTENT_TYPE_LATEST",
    "Gauge",
    "Histogram",
    "generate_latest",
]
