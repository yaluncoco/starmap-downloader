"""Browser transports retain the SAFE contract, queue bounds and retry identity."""
from __future__ import annotations

import copy
import hashlib
import json
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from conftest import P1, P2, serve, AppClient
from core.browser_download import BrowserUnavailable, EdgeDownload, matches_product
from core.download import _save_json
from core.download_queue import run_product_queue
from core.errors import Cancelled, UserError
from core.http import CATALOGUE_URL, HttpClient, check_cancel
from core.local_paths import io_path
from core.safe_zip import download_options
from test_safe_zip import UUIDS, TOKEN, zip_provider, zip_search
from test_native_mosaic import native_fixture
from test_monthly import prepare


class NativeTransport:
    def __init__(self, upstream, *, barrier=False, failure=None):
        self.upstream, self.failure = upstream, failure
        self.barrier = threading.Barrier(2) if barrier else None
        self.active, self.maximum = 0, 0
        self.lock = threading.Lock()
        self.calls = []

    def download(self, item, path, auth, cancel, report):
        assert auth.token(cancel) == TOKEN
        with self.lock:
            self.calls.append(item["uuid"])
            self.active += 1
            self.maximum = max(self.maximum, self.active)
        try:
            if self.failure:
                raise self.failure
            if self.barrier:
                self.barrier.wait(3)
            check_cancel(cancel)
            body = self.upstream.payloads[item["uuid"]]
            path.write_bytes(body)
            report("接收完整 ZIP", len(body), len(body))
            transfer = {"size": len(body), "expected_bytes": len(body), "etag": None,
                        "content_type": "application/zip", "aws_single_part_md5": None, "azure_blob_md5": None,
                        "transport_engine": "edge-webview2", "resumed_bytes": 0}
            _save_json(path.with_name(path.name + ".browser.json"), {"schema": 1, "url": item["url"], "uuid": item["uuid"], "transfer": transfer})
            return transfer
        finally:
            with self.lock:
                self.active -= 1


@pytest.mark.parametrize("monthly,mode", [(False, "original"), (True, "original"), (False, "research"), (True, "research")])
def test_browser_queue_reaches_two_workers_and_every_output_remains_verified(
        client, server, zip_provider, tmp_path, monkeypatch, monthly, mode):
    _, (request, baseline) = zip_search(client, tmp_path, monthly=monthly, mode=mode)
    request = copy.deepcopy(request)
    settings = request["months"][0]["options"] if monthly else request["options"]
    settings.update(download_engine="edge", download_workers=2)
    request, plan = prepare(client, request)
    transport = NativeTransport(zip_provider, barrier=True)
    server.service.browser_download = transport
    clients = []
    original_request = HttpClient.request
    def audit(http, method, url, **kwargs):
        if url.startswith(CATALOGUE_URL + "("):
            clients.append(http.session)
        return original_request(http, method, url, **kwargs)
    monkeypatch.setattr(HttpClient, "request", audit)
    job = client.wait(client.post("monthly-download" if monthly else "download", request)["job_id"])
    assert transport.maximum == 2 and transport.active == 0
    assert len(clients) == 2 and clients[0] is not clients[1]
    assert sorted(transport.calls) == sorted(UUIDS) and not zip_provider.archive_gets
    if mode == "original":
        assert plan["folder_name"] == baseline["folder_name"]
        assert [Path(file["path"]).name for file in job["result"]["files"]] == [P1 + ".zip", P2 + ".zip"]
    paths = [io_path(path) for path in Path(request["output_dir"]).rglob("*.zip")]
    assert len(paths) == 2
    for path in paths:
        receipt = json.loads(path.with_suffix(".starmap.json").read_text(encoding="utf-8"))
        assert receipt["transport_engine"] == "edge-webview2"
        assert receipt["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
        assert receipt["safe_zip_validation"]["declared_members_checksum_verified"] > 10
    assert not list(Path(request["output_dir"]).rglob("*.part"))
    assert not list(Path(request["output_dir"]).rglob("*.browser.json"))


@pytest.mark.parametrize("error,success", [(BrowserUnavailable("not ready"), True),
                                         (UserError("下载连接的证书校验失败"), False)])
def test_auto_falls_back_only_when_browser_is_unavailable(client, server, zip_provider, tmp_path, error, success):
    _, (request, _) = zip_search(client, tmp_path)
    request["scene_keys"] = request["scene_keys"][:1]
    request["options"].update(download_engine="auto", download_workers=1)
    request, _ = prepare(client, request)
    server.service.browser_download = NativeTransport(zip_provider, failure=error)
    job = client.wait(client.post("download", request)["job_id"], "completed" if success else "failed")
    assert bool(zip_provider.archive_gets) == success
    if not success:
        assert "证书校验失败" in job["error"]
        assert not list(Path(request["output_dir"]).rglob("*.zip"))


@pytest.mark.parametrize("monthly,mode", [(False, "original"), (True, "original"), (False, "research"), (True, "research")])
def test_engine_and_worker_choices_change_frozen_plan_but_reuse_same_folder(client, zip_provider, tmp_path, monthly, mode):
    _, (request, initial) = zip_search(client, tmp_path, monthly=monthly, mode=mode)
    request = copy.deepcopy(request)
    settings = request["months"][0]["options"] if monthly else request["options"]
    settings.update(download_engine="http-direct", download_workers=4)
    request, changed = prepare(client, request)
    assert changed["fingerprint"] != initial["fingerprint"]
    assert changed["folder_name"] == initial["folder_name"]
    selected = changed["months"][0]["options"] if monthly else changed["options"]
    assert selected["download_engine"] == "http-direct" and selected["download_workers"] == 4


def test_two_cancelled_versions_resume_after_restart_and_keep_selection(client, server, zip_provider, tmp_path):
    _, (request, _) = zip_search(client, tmp_path)
    zip_provider.pause = True
    identifier = client.post("download", request)["job_id"]
    deadline = time.monotonic() + 4
    while time.monotonic() < deadline:
        job = client.get("job", params={"id": identifier}).json()
        rows = job["detail"].get("product_queue", [])
        if len(rows) == 2 and all(row["received_bytes"] == 65536 for row in rows):
            break
        time.sleep(.02)
    else:
        pytest.fail("Both products did not receive a cancellable prefix")
    client.post("cancel", {"job_id": identifier})
    client.wait(identifier, "cancelled")
    partials = list(Path(request["output_dir"]).rglob("*.part"))
    assert len(partials) == 2 and all(path.stat().st_size == 65536 for path in partials)
    assert not list(Path(request["output_dir"]).rglob("*.zip"))
    sizes = [path.stat().st_size for path in partials]
    time.sleep(.1)
    assert [path.stat().st_size for path in partials] == sizes
    zip_provider.pause = False
    with serve(server.service.config_dir) as resumed:
        cold = AppClient(resumed.origin)
        try:
            cold.post("raw-connect", {"method": "token", "token": TOKEN})
            cold.post("aoi", {"coordinates": "120,30,120.01,30.01"})
            job = cold.wait(cold.post("retry-download", {"job_id": identifier})["job_id"])
            assert zip_provider.ranges == [65536, 65536]
            assert [file["scene"] for file in job["result"]["files"]] == [P1, P2]
            assert all(path.stat().st_size > 65536 for path in Path(request["output_dir"]).rglob("*.zip"))
        finally:
            cold.session.close()


def test_untrusted_browser_transfer_cache_is_not_a_verified_safe(client, server, zip_provider, tmp_path, monkeypatch):
    from core import safe_zip
    _, (request, _) = zip_search(client, tmp_path)
    request["scene_keys"] = request["scene_keys"][:1]
    request["options"].update(download_engine="edge", download_workers=1)
    request, _ = prepare(client, request)
    server.service.browser_download = NativeTransport(zip_provider)
    original_verify = safe_zip.verify_safe_archive
    validation_started = threading.Event()
    def pause(path, item, cancel):
        validation_started.set()
        assert cancel.wait(4)
        check_cancel(cancel)
    monkeypatch.setattr(safe_zip, "verify_safe_archive", pause)
    identifier = client.post("download", request)["job_id"]
    assert validation_started.wait(3)
    client.post("cancel", {"job_id": identifier})
    client.wait(identifier, "cancelled")
    partial, = Path(request["output_dir"]).rglob("*.part")
    data = partial.read_bytes()
    assert b"complete product auxiliary payload" in data
    partial.write_bytes(data.replace(b"complete product auxiliary payload", b"Complete product auxiliary payload"))
    monkeypatch.setattr(safe_zip, "verify_safe_archive", original_verify)
    before = len(server.service.browser_download.calls)
    job = client.wait(client.post("retry-download", {"job_id": identifier})["job_id"], "failed")
    assert "清单校验" in job["error"]
    assert len(server.service.browser_download.calls) == before and not zip_provider.archive_gets
    assert not list(Path(request["output_dir"]).rglob("*.zip"))


@pytest.mark.parametrize("workers", [0, 5, True, 2.0, "2"])
def test_download_workers_must_respect_official_integer_limit(workers):
    with pytest.raises(UserError):
        download_options({"download_workers": workers})


def test_queue_stops_new_products_and_waits_for_active_writers_on_failure():
    barrier = threading.Barrier(2)
    calls, stopped = [], threading.Event()
    def worker(index, item, cancel):
        calls.append(index)
        barrier.wait(2)
        if index == 0:
            raise UserError("source failed")
        assert cancel.wait(2)
        stopped.set()
        check_cancel(cancel)
    with pytest.raises(UserError, match="source failed"):
        run_product_queue([1, 2, 3], worker, 2)
    assert stopped.is_set() and sorted(calls) == [0, 1]


def test_browser_product_boundaries_reject_credentials_to_other_urls():
    identifier = UUIDS[0]
    canonical = f"https://download.dataspace.copernicus.eu/odata/v1/Products({identifier})/$value"
    item = {"uuid": identifier, "url": canonical}
    assert matches_product(canonical, item)
    assert matches_product(canonical.replace("://download.", "://zipper."), item)
    for url in (canonical + "?token=private", canonical.replace(identifier, UUIDS[1]),
                canonical.replace("/$value", "/Nodes(B04)/$value"),
                canonical.replace("download.dataspace.copernicus.eu", "foreign.invalid"), canonical.replace("https:", "http:")):
        assert not matches_product(url, item)


class NativeEvent:
    def __init__(self):
        self.handlers = []

    def __iadd__(self, handler):
        self.handlers.append(handler)
        return self

    def __isub__(self, handler):
        self.handlers.remove(handler)
        return self

    def fire(self):
        for handler in list(self.handlers):
            handler(None, None)


@pytest.mark.parametrize("cancel_during_recovery", [False, True])
def test_native_network_interruption_recovers_same_file_or_stops_writer_before_return(
        tmp_path, monkeypatch, cancel_during_recovery):
    """Exercise the actual event adapter, including Chromium's aborted navigation."""
    from core import browser_download
    window = SimpleNamespace(events=SimpleNamespace(loaded=NativeEvent(), closed=NativeEvent()))
    engine = EdgeDownload(window)
    monkeypatch.setattr(engine, "_invoke", lambda callback: callback())
    monkeypatch.setattr(browser_download, "wait_retry", lambda attempt, cancel, *args: check_cancel(cancel))
    identifier = UUIDS[0]
    item = {"uuid": identifier, "url": f"https://download.dataspace.copernicus.eu/odata/v1/Products({identifier})/$value"}
    path = tmp_path / "native.part"
    cancel = threading.Event()
    headers, resumes, cancellations = {}, [], []
    body = b"native product bytes" * 32

    class Operation:
        Uri = item["url"]
        BytesReceived = len(body) // 2
        TotalBytesToReceive = len(body)
        State = "InProgress"
        CanResume = True
        InterruptReason = "NetworkDisconnected"
        BytesReceivedChanged = NativeEvent()
        StateChanged = NativeEvent()

        def Resume(self):
            resumes.append(self.BytesReceived)
            request_headers = SimpleNamespace(SetHeader=lambda key, value: headers.update({key: value}))
            engine._requesting(None, SimpleNamespace(Request=SimpleNamespace(Uri=self.Uri, Headers=request_headers)))
            self.State = "InProgress"
            self.StateChanged.fire()
            with path.open("ab") as output:
                output.write(body[self.BytesReceived:])
            self.BytesReceived = len(body)
            self.BytesReceivedChanged.fire()
            self.State = "Completed"
            self.StateChanged.fire()

        def Cancel(self):
            cancellations.append(True)
            self.State, self.InterruptReason = "Interrupted", "UserCanceled"
            self.StateChanged.fire()

    operation = Operation()

    def navigate(request):
        engine._navigation_starting(None, SimpleNamespace(Uri=item["url"], NavigationId=1, IsRedirected=False))
        engine._navigation_completed(None, SimpleNamespace(NavigationId=1, HttpStatusCode=0,
                                                         IsSuccess=False, WebErrorStatus="ConnectionAborted"))
        path.write_bytes(body[:operation.BytesReceived])
        engine._download_starting(None, SimpleNamespace(DownloadOperation=operation))
        operation.State = "Interrupted"
        operation.StateChanged.fire()

    engine.core = SimpleNamespace(Environment=SimpleNamespace(CreateWebResourceRequest=lambda *args: args),
                                  NavigateWithWebResourceRequest=navigate)
    tokens = iter(("initial-token", "refreshed-token"))
    auth = SimpleNamespace(token=lambda cancel: next(tokens))

    def progress(message, received, total):
        if cancel_during_recovery:
            cancel.set()

    if cancel_during_recovery:
        with pytest.raises(Cancelled):
            engine._once(item, path, auth, cancel, progress)
        assert cancellations and not resumes
        size = path.stat().st_size
        time.sleep(.05)
        assert path.stat().st_size == size == len(body) // 2
    else:
        transfer = engine._once(item, path, auth, cancel, progress)
        assert transfer["native_resume_attempts"] == 1
        assert resumes == [len(body) // 2] and not cancellations
        assert headers == {"Authorization": "Bearer refreshed-token"}
        assert path.read_bytes() == body
    assert not engine.jobs and not operation.StateChanged.handlers and not operation.BytesReceivedChanged.handlers


def test_native_request_filter_removes_account_header_from_unrelated_resource():
    window = SimpleNamespace(events=SimpleNamespace(loaded=NativeEvent(), closed=NativeEvent()))
    engine = EdgeDownload(window)
    engine.core = SimpleNamespace(Environment=SimpleNamespace(CreateWebResourceResponse=lambda *args: args))
    sent = {"Authorization": "Bearer private-account-token"}
    headers = SimpleNamespace(RemoveHeader=lambda key: sent.pop(key, None))
    args = SimpleNamespace(Request=SimpleNamespace(Uri="https://unrelated.invalid/content", Headers=headers))
    engine._requesting(None, args)
    assert sent == {} and args.Response[1] == 403
