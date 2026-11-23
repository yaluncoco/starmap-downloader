"""Interrupt requests without letting detached network readers write download files."""
from __future__ import annotations

import queue
import socket
import threading

from .errors import Cancelled


def _cancelled(event):
    return event is not None and event.is_set()


def _shutdown(response):
    # Shutting down the socket wakes a blocked urllib3 read. Closing its buffered
    # reader from another thread could wait for that reader's lock instead.
    try:
        raw = response.raw
        connection = getattr(raw, "_connection", None)
        sock = getattr(connection, "sock", None)
        if sock is None:
            sock = raw._fp.fp.raw._sock
        sock.shutdown(socket.SHUT_RDWR)
    except (AttributeError, OSError, ValueError):
        pass


class CancellableResponse:
    """Bounded stream queue; only the consuming download thread sees the bytes."""

    def __init__(self, response, cancel):
        self._response, self._cancel = response, cancel
        self._stop = threading.Event()
        self._finished = threading.Event()
        self._started = False
        self._closed = False

    def __getattr__(self, name):
        return getattr(self._response, name)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def iter_content(self, chunk_size=1, decode_unicode=False):
        if self._started:
            raise RuntimeError("A download response can only be streamed once")
        self._started = True
        chunks = queue.Queue(maxsize=2)

        def deliver(kind, value=None):
            while not self._stop.is_set() and not _cancelled(self._cancel):
                try:
                    chunks.put((kind, value), timeout=.1)
                    return True
                except queue.Full:
                    continue
            return False

        def receive():
            try:
                iterator = (self._response.iter_content(chunk_size, decode_unicode=True) if decode_unicode
                            else self._response.iter_content(chunk_size))
                for chunk in iterator:
                    if not deliver("data", chunk):
                        return
                deliver("end")
            except Exception as error:
                deliver("error", error)
            finally:
                self._response.close()
                self._finished.set()

        worker = threading.Thread(target=receive, name="download-body", daemon=True)
        worker.start()
        try:
            while True:
                if _cancelled(self._cancel):
                    self._stop.set()
                    _shutdown(self._response)
                    raise Cancelled()
                if self._stop.is_set():
                    return
                try:
                    kind, value = chunks.get(timeout=.1)
                except queue.Empty:
                    continue
                if _cancelled(self._cancel):
                    raise Cancelled()
                if kind == "error":
                    raise value
                if kind == "end":
                    return
                yield value
        finally:
            self._stop.set()
            if not self._finished.is_set():
                _shutdown(self._response)
            worker.join(timeout=.25)

    def close(self):
        if self._closed:
            return
        self._closed = True
        self._stop.set()
        if self._started and not self._finished.is_set():
            _shutdown(self._response)
            # The receiver owns and closes the buffered response; do not block
            # cancellation on its I/O lock or close it while it is reading.
            return
        self._response.close()


def request_with_cancel(session, method, url, *, cancel=None, **kwargs):
    if cancel is None:
        return session.request(method, url, **kwargs)
    if _cancelled(cancel):
        raise Cancelled()
    ready, abandoned = threading.Event(), threading.Event()
    guard = threading.Lock()
    result = {}

    def request():
        try:
            response = session.request(method, url, **kwargs)
            with guard:
                if abandoned.is_set() or _cancelled(cancel):
                    response.close()
                else:
                    result["response"] = response
        except Exception as error:
            with guard:
                result["error"] = error
        finally:
            ready.set()

    threading.Thread(target=request, name="download-request", daemon=True).start()
    while not ready.wait(.1):
        if _cancelled(cancel):
            with guard:
                abandoned.set()
                response = result.pop("response", None)
            if response is not None:
                response.close()
            raise Cancelled()
    with guard:
        if _cancelled(cancel):
            abandoned.set()
            response = result.get("response")
            if response is not None:
                response.close()
            raise Cancelled()
        if "error" in result:
            raise result["error"]
        response = result["response"]
    return CancellableResponse(response, cancel) if kwargs.get("stream") else response
