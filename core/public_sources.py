"""Read distinct public raster bands with a bounded number of independent clients."""
from __future__ import annotations

from concurrent.futures import Future, as_completed
from contextlib import ExitStack
import queue
import threading
import time

from .http import HttpClient, check_cancel


class _ReadCancellation:
    def __init__(self, parent):
        self.parent, self.stopped = parent, threading.Event()

    def is_set(self):
        return self.stopped.is_set() or self.parent is not None and self.parent.is_set()

    def wait(self, seconds):
        deadline = time.monotonic() + seconds
        while not self.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            self.stopped.wait(min(.1, remaining))
        return True


class PublicSources:
    """Each worker opens, reads and closes its own GDAL datasets and HTTP session."""
    def __init__(self, assets, cancel=None, *, cache_dir=None, status=None, rtc_access=None, workers=4):
        if not assets or type(workers) is not int or not 1 <= workers <= 4:
            raise ValueError("Public raster reads require assets and one to four workers")
        self.assets, self.cancel = assets, _ReadCancellation(cancel)
        self.cache_dir, self.status, self.rtc_access = cache_dir, status, rtc_access
        self.closed, self.read_lock = False, threading.Lock()
        count = min(1 if rtc_access is not None else workers, len(assets))
        keys = list(assets)
        self.queues = [queue.Queue() for _ in range(count)]
        self.startup = [Future() for _ in range(count)]
        self.threads = [threading.Thread(target=self._worker, args=(i, keys[i::count]),
                                         name=f"public-band-{i}", daemon=True) for i in range(count)]
        self.open_seconds, self.read_seconds = 0.0, 0.0
        self.sources = {}
        begin = time.monotonic()
        try:
            for thread in self.threads:
                thread.start()
            opened = self._results(self.startup)
            self.sources = {key: source for group in opened for key, source in group.items()}
            self.sources = {key: self.sources[key] for key in assets}
        except BaseException:
            self.close()
            raise
        self.open_seconds = time.monotonic() - begin

    def _results(self, pending):
        values, first_error = {}, None
        for future in as_completed(pending):
            try:
                values[future] = future.result()
            except BaseException as error:
                if first_error is None:
                    first_error = error
                    self.cancel.stopped.set()
        if first_error is not None:
            raise first_error
        check_cancel(self.cancel)
        return [values[future] for future in pending]

    def _worker(self, index, keys):
        from .public_download import _open_source

        # Rasterio's custom opener registers callbacks in thread-local state.
        # A dataset must therefore remain on its opening thread through close.
        with ExitStack() as stack:
            try:
                http = HttpClient()
                stack.callback(http.close)
                sources = {}
                for band in keys:
                    check_cancel(self.cancel)
                    sources[band] = _open_source(stack, http, self.assets[band], self.cancel, self.rtc_access,
                                                cache_dir=self.cache_dir, status=self.status)
                self.startup[index].set_result(sources)
            except BaseException as error:
                self.startup[index].set_exception(error)
                return
            while True:
                request = self.queues[index].get()
                if request is None:
                    return
                operation, future = request
                try:
                    check_cancel(self.cancel)
                    future.set_result(operation(sources))
                except BaseException as error:
                    future.set_exception(error)

    def read(self, bounds, transform, crs, height, width):
        from .public_download import read_on_grid

        if self.closed:
            raise ValueError("Public raster readers are closed")
        begin = time.monotonic()
        try:
            with self.read_lock:
                check_cancel(self.cancel)
                pending = [Future() for _ in self.queues]
                def read(sources):
                    return {band: read_on_grid(source, bounds, transform, crs, height, width, self.cancel,
                                                preserve_nodata=band == "SCL") for band, source in sources.items()}
                for channel, future in zip(self.queues, pending):
                    channel.put((read, future))
                groups = self._results(pending)
                values = {key: value for group in groups for key, value in group.items()}
                return {key: values[key] for key in self.assets}
        except BaseException:
            self.close()
            raise
        finally:
            self.read_seconds += time.monotonic() - begin

    def close(self):
        if self.closed:
            return
        self.closed = True
        self.cancel.stopped.set()
        for channel in self.queues:
            channel.put(None)
        for thread in self.threads:
            if thread.ident is not None:
                thread.join()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
