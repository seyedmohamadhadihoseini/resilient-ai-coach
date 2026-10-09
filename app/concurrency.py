"""Concurrency utilities and SQLite busy-retry handlers for multi-worker environments.

When multiple Uvicorn worker processes concurrently execute write transactions,
SQLite handles serialization at the WAL lock level. This module provides exponential
backoff with randomized jitter to prevent lock starvation and thundering herds.
"""

from __future__ import annotations

import logging
import random
import sqlite3
import time
from typing import Callable, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")


def execute_with_busy_retry(
    operation: Callable[[], T],
    max_retries: int = 5,
    base_delay_s: float = 0.02,
    max_delay_s: float = 0.3,
) -> T:
    """Executes a database operation with exponential backoff and jitter on lock contention."""
    attempt = 0
    while True:
        try:
            return operation()
        except sqlite3.OperationalError as exc:
            if "locked" in str(exc).lower() or "busy" in str(exc).lower():
                attempt += 1
                if attempt > max_retries:
                    logger.error(
                        "Database lock contention exhausted %d retries: %s",
                        max_retries,
                        exc,
                    )
                    raise

                # Full Jitter backoff formula
                delay = min(max_delay_s, base_delay_s * (2 ** (attempt - 1)))
                jittered_delay = random.uniform(0, delay)
                logger.warning(
                    "Database busy (attempt %d/%d). Retrying in %.3fs...",
                    attempt,
                    max_retries,
                    jittered_delay,
                )
                time.sleep(jittered_delay)
            else:
                raise