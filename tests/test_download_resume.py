"""Persistent product queues, exact retries and source ranges with scientific integrity."""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from uuid import uuid4

import numpy as np
import pytest
import rasterio

from conftest import D1, P1, P2, StubResponse
from core.download_cache import PartialDownload
from core.download_tasks import DownloadTasks
from core.errors import Cancelled, TransientDownloadError, DownloadAccessError, UserError
from core.http import HttpClient
from core.local_paths import display_path
from core.mosaic import build_mosaic_plan, download_mosaic
from core.range_file import RangeFile
from core.raw_download import _stream
from core.service import Service
from test_mosaic import AOI, FieldReader, options, row
from test_public_workflow import pub_provider, search_public
from test_raw_workflow import auth_token, raw_provider, search_raw


def definition(tmp_path):
    return {"title": "Sentinel-2 · 研究分析包", "profile": "s2-earth-search", "output_dir": str(tmp_path),
            "geometry": {"type": "Polygon", "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 0]]]},
            "options": {"mode": "research"}, "plan": {"fingerprint": "abc"}, "scenes": [{"key": "product-a"}]}


def test_frozen_task_survives_restart_and_replaced_task_cannot_reappear(tmp_path):
    first = uuid4().hex
    tasks = DownloadTasks(tmp_path)
    original = definition(tmp_path)
    tasks.create(first, "download", original)
    restored = DownloadTasks(tmp_path)
    assert restored.summaries()[0]["status"] == "interrupted"
    assert restored.load(first) == ("download", original)
    second = uuid4().hex
    restored.create(second, "download", original, previous=first)
    restored.update({"id": first, "status": "failed", "message": "late completion", "progress": .5}, force=True)
    with pytest.raises(UserError, match="已继续"):
        restored.load(first)
    restored.update({"id": second, "status": "failed", "message": "offline", "progress": .6}, force=True)
    assert [r["id"] for r in DownloadTasks(tmp_path).summaries()] == [second]


@pytest.mark.parametrize("private", [{"access_token": "private"}, {"url": "https://example.test/a?X-Amz-Signature=private"}, {"url": "https://user:password@example.test/a"}])
def test_task_store_rejects_credentials_before_writing(tmp_path, private):
    tasks = DownloadTasks(tmp_path)
    with pytest.raises(UserError, match="认证"):
        tasks.create(uuid4().hex, "download", {**definition(tmp_path), "nested": [private]})
    assert not tasks.root.exists()


def test_retry_uses_frozen_product_after_service_restart_and_ui_change(client, server, pub_provider, tmp_path):
    request = {**search_public(client), "output_dir": str(tmp_path / "first")}
    pub_provider.bad = "whole"
    first = client.post("download", request)["job_id"]
    failure = client.wait(first, "failed")
    assert failure["retry_available"] and client.get("resumable-downloads").json()["tasks"]
    root = server.service.config_dir
    server.service = Service(config_dir=root)
    server.service.preferences["output_dir"] = str(tmp_path / "unrelated")
    assert server.service.last_query is None and not server.service.scenes
    pub_provider.bad = None
    second = client.post("retry-download", {"job_id": first})["job_id"]
    result = client.wait(second)
    assert result["previous_id"] == first and not result["retry_available"]
    output = Path(result["result"]["files"][0]["path"])
    assert Path(display_path(output)).is_relative_to(tmp_path / "first") and output.is_file()
    assert not list((tmp_path / "first").rglob("*.zip"))
    assert client.get("resumable-downloads").json()["tasks"] == []
    client.post("retry-download", {"job_id": first}, status=400)
    client.post("retry-download", {"job_id": second}, status=400)


def test_original_prefix_survives_process_restart_and_corruption_refetches_from_zero(monkeypatch, tmp_path):
    payload = bytes(range(256)) * 20
    item = {"name": "original.h5", "url": "https://example.test/object", "asset": "original", "bytes": len(payload), "transport": "public"}
    calls, stop = [], threading.Event()
    partial = tmp_path / "payload.part"

    class First(StubResponse):
        def iter_content(self, _size):
            yield payload[:1024]
            stop.set()
            yield payload[1024:]

    def get(headers, _status):
        calls.append(headers.copy())
        start = int(re.fullmatch(r"bytes=(\d+)-", headers["Range"])[1]) if "Range" in headers else 0
        if stop.is_set():
            raise AssertionError("cancel must stop before requesting")
        if len(calls) == 1:
            return First(headers={"Content-Length": str(len(payload)), "ETag": '"v1"'})
        return StubResponse(206 if start else 200, content=payload[start:], headers={
            "Content-Length": str(len(payload) - start), "ETag": '"v1"',
            **({"Content-Range": f"bytes {start}-{len(payload) - 1}/{len(payload)}"} if start else {})})

    with pytest.raises(Cancelled):
        _stream(None, None, item, partial, stop, lambda *_: None, requester=get)
    state = PartialDownload(partial, item).load()
    assert state["received"] == 1024
    stop.clear()
    transfer = _stream(None, None, item, partial, stop, lambda *_: None, requester=get)
    assert transfer["resumed_bytes"] == 1024 and calls[-1]["Range"] == "bytes=1024-" and partial.read_bytes() == payload
    # A completed transfer interrupted during decoding is also reusable without any HTTP request.
    count = len(calls)
    assert _stream(None, None, item, partial, stop, lambda *_: None, requester=get)["transfer_attempts"] == 0
    assert len(calls) == count
    partial.write_bytes(b"x" + payload[1:])
    _stream(None, None, item, partial, stop, lambda *_: None, requester=get)
    assert "Range" not in calls[-1] and partial.read_bytes() == payload


def test_expired_access_keeps_verified_prefix_for_updated_key(tmp_path):
    payload = b"original product bytes" * 100
    item = {"name": "file.tif", "asset": "file", "bytes": len(payload), "transport": "public"}
    partial = tmp_path / "source.part"
    partial.write_bytes(payload[:100])
    checkpoint = PartialDownload(partial, item)
    checkpoint.sha.update(payload[:100])
    checkpoint.checkpoint(100, len(payload), '"same-object"', force=True)
    with pytest.raises(DownloadAccessError):
        _stream(None, None, item, partial, None, lambda *_: None,
                requester=lambda *_: StubResponse(403))
    assert PartialDownload(partial, item).load()["received"] == 100
    requests = []
    def updated_key(headers, _status):
        requests.append(headers)
        return StubResponse(206, content=payload[100:], headers={"ETag": '"same-object"', "Content-Length": str(len(payload) - 100),
                                                                 "Content-Range": f"bytes 100-{len(payload) - 1}/{len(payload)}"})
    transfer = _stream(None, None, item, partial, None, lambda *_: None, requester=updated_key)
    assert transfer["resumed_bytes"] == 100 and requests[0]["Range"] == "bytes=100-" and partial.read_bytes() == payload


def test_gedi_rejected_range_preserves_prefix_and_next_retry_verifies_original_hdf(tmp_path, monkeypatch):
    import h5py
    import requests
    from types import SimpleNamespace
    from core.gedi_download import _fetch_source, _fingerprint
    from core.local_paths import io_path

    original = tmp_path / "official-source.h5"
    with h5py.File(original, "w") as file:
        beam = file.create_group("BEAM0000")
        for name in ("shot_number", "delta_time", "lat_lowestmode", "lon_lowestmode"):
            beam.create_dataset(name, data=np.arange(4))
    payload, cut = original.read_bytes(), 1024
    source = {"url": "https://data.lpdaac.earthdatacloud.nasa.gov/selected.h5", "size": len(payload),
              "checksum": {"algorithm": "SHA-256", "value": hashlib.sha256(payload).hexdigest()}}
    target = {"source": source, "relative_path": "received.h5"}
    calls, reject = [], [True]

    class Interrupted(StubResponse):
        def iter_content(self, _size):
            yield payload[:cut]
            raise requests.exceptions.ChunkedEncodingError("fixture interruption")

    def request(_method, _url, **kwargs):
        headers = kwargs["headers"]
        calls.append({key: headers[key] for key in ("Range", "If-Match") if key in headers})
        response_headers = {"ETag": '"exact-source"', "Content-Type": "application/octet-stream"}
        if len(calls) == 1:
            return Interrupted(headers={**response_headers, "Content-Length": str(len(payload))})
        assert headers["Range"] == f"bytes={cut}-" and headers["If-Match"] == '"exact-source"'
        start = cut + 1 if reject[0] else cut
        return StubResponse(206, content=payload[cut:], headers={**response_headers,
                            "Content-Length": str(len(payload) - cut), "Content-Range": f"bytes {start}-{len(payload)-1}/{len(payload)}"})

    monkeypatch.setattr("core.raw_download.wait_retry", lambda *_: None)
    root = io_path(tmp_path / "output")
    auth, http = SimpleNamespace(token=lambda: "synthetic-resume-token"), SimpleNamespace(request=request)
    with pytest.raises(UserError, match="长度或版本"):
        _fetch_source(http, auth, target, root, {"kind": "footprint"}, None, lambda *_: None)
    partial = root / ".download-cache" / _fingerprint(source) / "payload.part"
    assert partial.read_bytes() == payload[:cut] and not (root / "received.h5").exists()
    reject[0] = False
    path, record = _fetch_source(http, auth, target, root, {"kind": "footprint"}, None, lambda *_: None)
    assert path.read_bytes() == payload and record["resumed_bytes"] == cut
    assert record["provider_checksum_verified"] == source["checksum"] and record["format"] == "HDF5"
    assert not partial.exists()


def test_range_retry_budget_is_shared_by_headers_and_body_and_tls_fails_immediately(monkeypatch):
    import requests
    responses, calls = [], []
    http = HttpClient()
    def unavailable(*args, **kwargs):
        calls.append(kwargs)
        response = StubResponse(503)
        responses.append(response)
        return response
    monkeypatch.setattr(http.session, "request", unavailable)
    monkeypatch.setattr("core.range_file.wait_retry", lambda *_: None)
    try:
        with pytest.raises(TransientDownloadError):
            RangeFile(http, "https://example.test/object")
        assert len(calls) == 3 and all(response.closed for response in responses)
        def bad_tls(*args, **kwargs):
            calls.append(kwargs)
            raise requests.exceptions.SSLError("private proxy diagnostic")
        monkeypatch.setattr(http.session, "request", bad_tls)
        before = len(calls)
        with pytest.raises(UserError, match="证书") as error:
            RangeFile(http, "https://example.test/object")
        assert len(calls) == before + 1 and "private" not in str(error.value)
    finally:
        http.close()


def test_real_tcp_ranges_resume_without_retransmitting_verified_pages(monkeypatch, tmp_path):
    payload = bytes(range(251)) * 27
    received = []
    broken = {"enabled": True}
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            start, end = map(int, re.fullmatch(r"bytes=(\d+)-(\d+)", self.headers["Range"]).groups())
            end = min(end, len(payload) - 1)
            received.append(start)
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{end}/{len(payload)}")
            self.send_header("Content-Length", str(end - start + 1))
            self.send_header("ETag", '"tcp-v1"')
            self.end_headers()
            self.wfile.write(payload[start:start + 17] if start == 2048 and broken["enabled"] else payload[start:end + 1])
            self.wfile.flush()
            self.close_connection = True
        def log_message(self, *_):
            pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setattr("core.range_file.wait_retry", lambda *_: None)
    http = HttpClient()
    http.session.trust_env = False
    url = f"http://127.0.0.1:{server.server_port}/object"
    try:
        with pytest.raises(TransientDownloadError), RangeFile(http, url, page_size=1024, cache_pages=1, cache_dir=tmp_path) as stream:
            stream.read()
        assert received.count(2048) == 3
        before = len(received)
        broken["enabled"] = False
        with RangeFile(http, url, page_size=1024, cache_pages=1, cache_dir=tmp_path) as stream:
            assert stream.read() == payload and stream.cached_pages >= 2
        assert 0 not in received[before:] and 1024 not in received[before:]
        page, = [p for p in tmp_path.rglob("0") if p.is_file()]
        page.write_bytes(b"x" + page.read_bytes()[1:])
        before = len(received)
        with RangeFile(http, url, page_size=1024, cache_pages=1, cache_dir=tmp_path) as stream:
            assert stream.read() == payload
        assert received[before:] == [0]
    finally:
        http.close()
        server.shutdown()
        thread.join(3)
        server.server_close()


@pytest.mark.parametrize("source_failure", [False, True])
def test_mosaic_retry_reuses_blocks_and_preserves_all_scientific_layers(tmp_path, source_failure):
    opts = options()
    scenes = [row(P1, D1), row(P2, "2024-06-08T03:16:50Z", marker=2)] if source_failure else [row(P1, D1)]
    prepared = build_mosaic_plan(AOI, scenes, opts, tile_size=20)
    class Fails(FieldReader):
        def read(self, scene, tile, inside):
            if (source_failure and scene["marker"] == 2) or (not source_failure and len(self.calls) == 1):
                raise TransientDownloadError("fixture offline")
            return super().read(scene, tile, inside)
    reader = Fails(opts["bands"], split_x=0 if source_failure else None)
    with pytest.raises(TransientDownloadError):
        download_mosaic(AOI, scenes, opts, tmp_path / "resume", reader=reader, planned=prepared)
    resumed = FieldReader(opts["bands"], split_x=0 if source_failure else None)
    result = download_mosaic(AOI, scenes, opts, tmp_path / "resume", reader=resumed, planned=prepared)
    main = result["files"][0]
    manifest = json.loads(Path(result["manifest"]).read_text(encoding="utf-8"))["items"][0]
    assert manifest["reused_source_blocks" if source_failure else "reused_blocks"] >= 1
    baseline = download_mosaic(AOI, scenes, opts, tmp_path / "baseline", reader=FieldReader(opts["bands"], split_x=0 if source_failure else None), planned=prepared)
    baseline_file = baseline["files"][0]
    for role, path in {"analysis": main["path"], **main["bundle"]}.items():
        if role == "sources":
            continue
        baseline_path = baseline_file["path"] if role == "analysis" else baseline_file["bundle"][role]
        with rasterio.open(path) as actual, rasterio.open(baseline_path) as expected:
            np.testing.assert_array_equal(actual.read(), expected.read())
            assert actual.transform == expected.transform and actual.descriptions == expected.descriptions
    assert len(list(Path(result["folder"]).glob("*.tif"))) == 4
    assert not list(Path(result["folder"]).glob("*.zip"))


def test_rejected_mosaic_source_is_not_saved_as_a_resumable_block(tmp_path):
    opts = options()
    scenes = [row(P1, D1)]
    prepared = build_mosaic_plan(AOI, scenes, opts, tile_size=20)

    class WrongSource(FieldReader):
        def read(self, scene, tile, inside):
            values, scl, metadata = super().read(scene, tile, inside)
            metadata["sources"][0]["catalogue_id"] = "another-product"
            return values, scl, metadata

    with pytest.raises(UserError, match="未选择"):
        download_mosaic(AOI, scenes, opts, tmp_path, reader=WrongSource(opts["bands"]), planned=prepared)
    assert not list(tmp_path.rglob("*.npz"))
    reader = FieldReader(opts["bands"])
    result = download_mosaic(AOI, scenes, opts, tmp_path, reader=reader, planned=prepared)
    assert reader.calls and Path(result["files"][0]["path"]).is_file()


@pytest.mark.browser
def test_retry_controls_survive_page_reload_and_changed_download_selection(server, pub_provider, tmp_path):
    pw = pytest.importorskip("playwright.sync_api")
    from conftest import TRIANGLE
    from scripts.browser_flow import open_data_page
    failures = []
    with pw.sync_playwright() as driver:
        browser = driver.chromium.launch(channel=os.environ.get("STARMAP_TEST_BROWSER", "msedge"), headless=True)
        try:
            page = browser.new_page(viewport={"width": 1440, "height": 960})
            page.on("pageerror", lambda error: failures.append(str(error)))
            page.route(re.compile(r"^https://"), lambda route: route.abort())
            open_data_page(page, server.origin)
            page.locator("#sourceProfile").select_option("s2-earth-search")
            page.locator("#imageMode").select_option("ndvi")
            page.locator('[data-tab="coordinates"]').click()
            page.locator("#coordinates").fill(TRIANGLE)
            page.locator("#applyCoordinates").click()
            page.locator("#startDate").fill("2024-06-10")
            page.locator("#endDate").fill("2024-06-10")
            page.locator("#searchScenes").click()
            pw.expect(page.locator("#scenesBody tr")).to_have_count(1)
            pw.expect(page.locator("#downloadSelected")).to_be_enabled()
            page.locator("#downloadSelected").click()
            pw.expect(page.locator("#planNote")).to_contain_text("按产品排队")
            page.locator("#changePlanFolder").click()
            page.locator("#pathDialogInput").fill(str(tmp_path / "original-selection"))
            page.locator("#confirmPath").click()
            pub_provider.bad = "whole"
            page.locator("#confirmDownload").click()
            pw.expect(page.locator("#jobStatus")).to_have_text("下载失败", timeout=15000)
            pw.expect(page.locator("#retryDownload")).to_be_visible()
            pw.expect(page.locator("#resumeDownloads")).to_be_visible()
            # The saved task stays independent of the current source, AOI and output preferences.
            page.locator("#sourceProfile").select_option("dem-90")
            page.reload()
            pw.expect(page.locator("#resumeDownloads")).to_be_visible()
            pub_provider.bad = None
            page.locator("[data-retry-download]").click()
            pw.expect(page.locator("#jobStatus")).to_have_text("下载完成", timeout=20000)
            pw.expect(page.locator("#jobFiles")).to_contain_text(".tif")
            pw.expect(page.locator("#jobFiles")).to_contain_text("ndvi")
            pw.expect(page.locator("#resumeDownloads")).to_be_hidden()
            pw.expect(page.locator("#retryDownload")).to_be_hidden()
            assert list((tmp_path / "original-selection").rglob("*.tif")) and not failures
            assert not list((tmp_path / "original-selection").rglob("*.zip"))
        finally:
            browser.close()
