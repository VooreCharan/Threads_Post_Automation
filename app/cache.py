"""A tiny thread-safe cache with expiry, for results that are expensive to compute (free-tier AI calls)."""
import threading
import time

MISSING = object()


class TTLCache:
    def __init__(self, ttl_seconds: float, maxsize: int = 500):
        self.ttl = ttl_seconds
        self.maxsize = maxsize
        self._data: dict = {}
        self._lock = threading.Lock()

    def get(self, key):
        """The cached value, or MISSING if absent or expired."""
        with self._lock:
            hit = self._data.get(key)
            if hit and time.time() - hit[0] < self.ttl:
                return hit[1]
            self._data.pop(key, None)
            return MISSING

    def set(self, key, value) -> None:
        with self._lock:
            if len(self._data) >= self.maxsize:  # drop the oldest fifth
                for k in sorted(self._data, key=lambda k: self._data[k][0])[: self.maxsize // 5]:
                    del self._data[k]
            self._data[key] = (time.time(), value)

    def clear(self) -> None:
        with self._lock:
            self._data.clear()
