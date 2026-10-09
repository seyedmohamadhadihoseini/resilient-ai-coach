"""Chaos injection engine for testing resilience under sudden crashes.

Allows deterministic simulation of catastrophic failure modes, such as a process
crashing immediately after a database commit but before returning an HTTP response.
"""

from __future__ import annotations

from enum import Enum
from typing import Optional, Set


class ChaosPoint(str, Enum):
    BEFORE_PROVIDER = "before_provider"
    BEFORE_COMMIT = "before_commit"
    AFTER_COMMIT_BEFORE_RESPONSE = "after_commit_before_response"


class SimulatedCrashError(Exception):
    """Simulates SIGKILL or unhandled process termination."""


class ChaosHook:
    def __init__(self, active_points: Optional[Set[ChaosPoint]] = None) -> None:
        self.active_points: Set[ChaosPoint] = active_points or set()
        self.triggered_points: Set[ChaosPoint] = set()

    def arm(self, point: ChaosPoint) -> None:
        self.active_points.add(point)

    def disarm(self, point: ChaosPoint) -> None:
        self.active_points.discard(point)

    def trigger(self, point: ChaosPoint) -> None:
        if point in self.active_points:
            self.triggered_points.add(point)
            raise SimulatedCrashError(f"CRASH INJECTION triggered at: {point.value}")