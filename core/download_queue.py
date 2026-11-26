"""A bounded product queue with deterministic results and cooperative cancellation."""
from __future__ import annotations

import threading
import time
from collections import deque
from contextlib import contextmanager
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait

from .errors import Cancelled
from .http import check_cancel


class DownloadSlots:
    """Share a product limit across month queues without multiplying concurrency."""

    def __init__(self, workers):
        self.workers = workers
        self._available = threading.BoundedSemaphore(workers)

    @contextmanager
    def acquire(self, cancel=None):
        check_cancel(cancel)
        while not self._available.acquire(timeout=.1):
            check_cancel(cancel)
        try:
            check_cancel(cancel)
            yield
        finally:
            self._available.release()


class TransferSpeed:
    """Measure a short byte window so native download event bursts stay readable."""

    def __init__(self, seconds=4.):
        self.seconds = seconds
        self.samples = deque()
        self.previous = None

    def update(self, received, now=None):
        now = time.monotonic() if now is None else now
        if self.previous is not None and received < self.previous:
            self.samples.clear()
        self.previous = received
        if not self.samples:
            self.samples.append((now, received))
            return 0.
        if now - self.samples[-1][0] >= .2:
            self.samples.append((now, received))
        while len(self.samples) > 1 and self.samples[1][0] <= now - self.seconds:
            self.samples.popleft()
        started, prefix = self.samples[0]
        return max(0., (received - prefix) / (now - started)) if now > started else 0.


class QueueCancel:
    def __init__(self, external=None):
        self.external = external
        self.stopped = threading.Event()

    def is_set(self):
        return self.stopped.is_set() or self.external is not None and self.external.is_set()

    def wait(self, timeout=None):
        deadline = None if timeout is None else time.monotonic() + max(0., timeout)
        while not self.is_set():
            remaining = None if deadline is None else deadline - time.monotonic()
            if remaining is not None and remaining <= 0:
                return False
            self.stopped.wait(.1 if remaining is None else min(.1, remaining))
        return True


def run_product_queue(items, worker, workers, *, cancel=None, on_result=None):
    """Stop scheduling on error; wait for active writers before returning or raising.

    Each worker owns its HTTP session and temporary output. Successful results are
    ordered like the input even when a later product finishes first.
    """
    signal = QueueCancel(cancel)
    results, errors = {}, []
    pending, next_index = {}, 0
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="safe-product") as pool:
        while pending or next_index < len(items):
            if signal.is_set() and not errors:
                errors.append(Cancelled())
            while not signal.is_set() and next_index < len(items) and len(pending) < workers:
                index = next_index
                pending[pool.submit(worker, index, items[index], signal)] = index
                next_index += 1
            if not pending:
                break
            completed, _ = wait(pending, timeout=.1, return_when=FIRST_COMPLETED)
            for future in sorted(completed, key=lambda f: pending[f]):
                index = pending.pop(future)
                try:
                    value = future.result()
                    results[index] = value
                    if on_result:
                        on_result(index, value, None)
                except BaseException as error:
                    # Preserve the initiating error ahead of cancellations it causes.
                    errors.append(error)
                    signal.stopped.set()
                    if on_result:
                        on_result(index, None, error)
    if errors:
        error = next((e for e in errors if not isinstance(e, Cancelled)), errors[0])
        error.queue_results = results
        raise error
    check_cancel(cancel)
    return [results[index] for index in range(len(items))]


class ProductProgress:
    def __init__(self, names, callback=None, *, folder=None, workers=1):
        self.names, self.callback, self.folder, self.workers = names, callback, folder, workers
        self.lock = threading.RLock()
        self.rows = [{"name": name, "status": "queued", "progress": 0., "received_bytes": 0,
                      "total_bytes": None, "bytes_per_second": 0.} for name in names]

    def update(self, index, message, fraction=0, detail=None, *, status=None):
        with self.lock:
            row = self.rows[index]
            detail = detail or {}
            receiving = detail.get("transfer_phase") == "download"
            row.update(status=status or ("downloading" if receiving else "verifying"),
                       progress=max(0., min(1., fraction or 0)), message=message)
            for field in ("received_bytes", "total_bytes", "transport_engine"):
                if detail.get(field) is not None:
                    row[field] = detail[field]
            row["bytes_per_second"] = max(0., detail.get("bytes_per_second") or 0.) if receiving else 0.
            row["updated_at"] = time.monotonic()
            done = sum(r["status"] == "completed" for r in self.rows)
            active = [r for r in self.rows if r["status"] in ("downloading", "verifying")]
            speed = sum(r["bytes_per_second"] for r in active if time.monotonic() - r.get("updated_at", 0) < 5)
            known_total = all(r["total_bytes"] for r in self.rows)
            received = sum(r["received_bytes"] for r in self.rows)
            size = sum(r["total_bytes"] for r in self.rows) if known_total else None
            payload = {"folder": self.folder, "received_bytes": received, "total_bytes": size,
                       "bytes_per_second": speed, "eta_seconds": max(0., (size - received) / speed) if size and speed else None,
                       "completed_scenes": done, "total_scenes": len(self.rows), "active_downloads": len(active),
                       "download_workers": self.workers, "queued_products": sum(r["status"] == "queued" for r in self.rows),
                       "transfer_phase": "download" if any(r["status"] == "downloading" for r in active) else "verify",
                       "product_queue": [{k: v for k, v in r.items() if k != "updated_at"} for r in self.rows]}
            if self.callback:
                self.callback(f"已完成 {done}/{len(self.rows)} · {message}",
                              sum(r["progress"] for r in self.rows) / len(self.rows), payload)

    def complete(self, index, value=None, error=None):
        self.update(index, "已通过完整性校验" if error is None else "已取消" if isinstance(error, Cancelled) else "下载未完成",
                    1. if error is None else self.rows[index]["progress"],
                    status="completed" if error is None else "cancelled" if isinstance(error, Cancelled) else "failed")
