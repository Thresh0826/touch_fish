"""Bounded retry for transient PermissionError in local file operations only."""
import time
import math


def retry_permission(operation, *, seconds=1.0, clock=None, sleep=None):
    if (isinstance(seconds, bool) or not isinstance(seconds, (int, float))
            or not math.isfinite(seconds) or seconds < 0):
        raise ValueError("File retry duration must be finite and nonnegative")
    clock = clock or time.monotonic
    sleep = sleep or time.sleep
    deadline = clock() + seconds
    delay = 0.02
    while True:
        try:
            return operation()
        except PermissionError:
            remaining = deadline - clock()
            if remaining <= 0:
                raise
            sleep(min(delay, remaining))
            delay = min(delay * 2, 0.2)
