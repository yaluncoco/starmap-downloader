"""Cancellation remains responsive while headers, bodies or planning are blocked."""
from __future__ import annotations

import hashlib
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from uuid import uuid4

import pytest

from core.download_tasks import DownloadTasks
from core.errors import Cancelled, UserError
from core.http import HttpClient
from core.service import Job, Service
from test_download_resume import definition


@contextmanager
def stalled_server(stage):
    waiting, release = threading.Event(), threading.Event()
    payload = b"verified original bytes" * 4096

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):
            try:
                if stage == "headers":
                    waiting.set()
                    release.wait(5)
                self.send_response(200)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                if stage == "body":
                    self.wfile.write(payload[:1024])
                    self.wfile.flush()
                    waiting.set()
                    release.wait(5)
                    self.wfile.write(payload[1024:])
                else:
                    self.wfile.write(payload)
            except (BrokenPipeError, ConnectionResetError):
                pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", waiting, payload
    finally:
        release.set()
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)


@pytest.mark.parametrize("stage", ["headers", "body"])
def test_cancel_interrupts_a_real_wait_without_reducing_read_timeout(stage, tmp_path):
    cancel, finished = threading.Event(), threading.Event()
    errors, received = [], bytearray()
    with stalled_server(stage) as (origin, waiting, _):
        http = HttpClient()

        def download():
            try:
                with http.request("GET", origin, cancel=cancel, stream=True, timeout=(5, 30), attempts=1) as response:
                    for chunk in response.iter_content(512):
                        received.extend(chunk)
            except Exception as error:
                errors.append(error)
            finally:
                http.close()
                finished.set()

        thread = threading.Thread(target=download, daemon=True)
        thread.start()
        assert waiting.wait(2), "The server must really enter the intended network wait"
        started = time.monotonic()
        cancel.set()
        assert finished.wait(1.5), "Cancellation must not wait for the 30-second socket deadline"
        assert len(errors) == 1 and isinstance(errors[0], Cancelled)
        assert time.monotonic() - started < 1.5
        if stage == "body":
            assert len(received) <= 1024
        thread.join(timeout=1)


def test_cancellable_response_retains_all_bytes_on_a_successful_stream():
    cancel = threading.Event()
    with stalled_server("normal") as (origin, _, payload):
        http = HttpClient()
        try:
            with http.request("GET", origin, cancel=cancel, stream=True, attempts=1) as response:
                actual = b"".join(response.iter_content(3072))
                assert int(response.headers["Content-Length"]) == len(actual) == len(payload)
                assert hashlib.sha256(actual).digest() == hashlib.sha256(payload).digest()
        finally:
            http.close()


def test_cancel_and_job_status_do_not_wait_for_the_planning_lock(tmp_path):
    service = Service(tmp_path)
    job = Job("download")
    with service._jobs_lock:
        service.jobs[job.id] = job
    blocked, release = threading.Event(), threading.Event()

    def planning():
        with service.lock:
            blocked.set()
            release.wait(3)

    thread = threading.Thread(target=planning, daemon=True)
    thread.start()
    try:
        assert blocked.wait(1)
        started = time.monotonic()
        assert service.job(job.id)["status"] == "running"
        assert service.cancel(job.id)["status"] == "cancelling"
        assert job.cancel_event.is_set() and time.monotonic() - started < .5
    finally:
        release.set()
        thread.join(timeout=1)
        service.shutdown()


def test_abandon_preserves_data_and_caches_and_cannot_return_after_restart(tmp_path):
    tasks = DownloadTasks(tmp_path / "settings")
    file = tmp_path / "already-completed.tif"
    cache = tmp_path / "verified-cache.part"
    file.write_bytes(b"valid completed product")
    cache.write_bytes(b"verified partial source bytes")
    identifier = uuid4().hex
    tasks.create(identifier, "download", definition(tmp_path))
    tasks.update({"id": identifier, "status": "cancelled", "message": "paused", "progress": .3}, force=True)
    assert tasks.abandon(identifier)["files_preserved"]
    assert file.read_bytes() == b"valid completed product" and cache.read_bytes() == b"verified partial source bytes"
    assert DownloadTasks(tmp_path / "settings").summaries() == []
    with pytest.raises(UserError):
        tasks.load(identifier)
    tasks.update({"id": identifier, "status": "failed", "message": "late update", "progress": .4}, force=True)
    assert not tasks.summaries()


def test_running_task_cannot_be_removed_before_its_writers_stop(tmp_path):
    tasks = DownloadTasks(tmp_path)
    identifier = uuid4().hex
    tasks.create(identifier, "download", definition(tmp_path))
    with pytest.raises(UserError, match="运行"):
        tasks.abandon(identifier)
