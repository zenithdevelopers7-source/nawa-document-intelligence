"""In-process request gate for synthetic recovery experiments only."""
from contextlib import contextmanager
import math
import threading
import time


class GateClosed(RuntimeError):
    pass


class SyntheticRequestGate:
    """Track explicitly enrolled work; this is not a distributed barrier."""
    def __init__(self):
        self._condition = threading.Condition()
        self._closed = False
        self._active = 0

    @contextmanager
    def operation(self):
        with self._condition:
            if self._closed:
                raise GateClosed('Synthetic recovery in progress')
            self._active += 1
        try:
            yield
        finally:
            with self._condition:
                self._active -= 1
                self._condition.notify_all()

    def close(self):
        with self._condition:
            if self._closed:
                return False
            self._closed = True
            return True

    def drain(self, timeout):
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout < 0:
            raise ValueError('Invalid drain timeout')
        deadline = time.monotonic() + timeout
        with self._condition:
            if not self._closed:
                return False
            while self._active:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._condition.wait(remaining)
            return True

    def resume(self):
        with self._condition:
            if not self._closed or self._active:
                raise GateClosed('Gate cannot resume')
            self._closed = False
