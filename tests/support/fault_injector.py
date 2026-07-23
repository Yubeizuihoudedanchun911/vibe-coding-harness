from __future__ import annotations


class InjectedCrash(RuntimeError):
    pass


class FaultInjector:
    def __init__(self, crash_at: str | None = None) -> None:
        self.crash_at = crash_at
        self.seen: list[str] = []

    def __call__(self, point: str) -> None:
        self.seen.append(point)
        if point == self.crash_at:
            raise InjectedCrash(point)
