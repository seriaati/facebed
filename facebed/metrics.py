import threading
import time


class ServiceMetrics:
    def __init__(self, started_at: float | None = None) -> None:
        self._started_at = time.monotonic() if started_at is None else started_at
        self._requests = 0
        self._errors = 0
        self._lock = threading.Lock()

    def record_request(self) -> None:
        with self._lock:
            self._requests += 1

    def record_error(self) -> None:
        with self._lock:
            self._errors += 1

    def snapshot(self) -> tuple[int, int, int]:
        with self._lock:
            uptime_secs = max(0, int(time.monotonic() - self._started_at))
            return uptime_secs, self._requests, self._errors


service_metrics = ServiceMetrics()
