"""API transfer barriers, exact fallback scope and offline cache failure handling."""
from __future__ import annotations

import json
import re
import threading
from pathlib import Path

import numpy as np
import pytest
import rasterio
from shapely.geometry import box

from core.download import download_scenes
from core.errors import Cancelled, UserError
from core.http import HttpClient, PROCESS_URL
from core.local_paths import io_path
from core.mosaic import build_mosaic_plan
from core.planning import build_plan, normalize_options
from core.scene_identity import expected_products, source_fingerprint
from test_mosaic import AOI, two_rows


def setup(client, server, provider, layout, mode="research", **extra):
    client.search(cloud=100)
    geometry = AOI.difference(box(116.351, 39.901, 116.352, 39.902))
    scenes = two_rows()
    options = normalize_options({"layout": layout, "mode": mode, "priority": "selection", **extra})
    planned = (build_mosaic_plan(geometry, scenes, options, tile_size=16) if layout == "mosaic" else
               build_plan(geometry, options, len(scenes), tile_size=16))
    return geometry, scenes, options, server.service.auth, planned


def cloudy_first(provider, monkeypatch):
    actual = provider.request

    def request(_http, method, url, **kwargs):
        if url == PROCESS_URL:
            products = json.loads(re.search(r"^var allowed = (.*);$", kwargs["json"]["evalscript"], re.M)[1])
            provider.research_scl = 9 if products[0]["id"].startswith("S2A_") else 4
        return actual(method, url, **kwargs)

    monkeypatch.setattr(HttpClient, "request", request)


@pytest.mark.parametrize("layout", ["series", "mosaic"])
@pytest.mark.parametrize("mode", ["research", "rgb"])
def test_every_api_transfer_finishes_before_any_output_opens(client, server, provider, monkeypatch, tmp_path, layout, mode):
    args = setup(client, server, provider, layout, mode)
    cloudy_first(provider, monkeypatch)
    actual_request, actual_open = HttpClient.request, rasterio.open
    phase, network_at_barrier, output_writes = ["download"], [], []

    def request(*args, **kwargs):
        assert phase[0] == "download", "An output stage attempted HTTP"
        assert not list(tmp_path.rglob("*.partial.tif")), "Output encoding started before API transfer finished"
        return actual_request(*args, **kwargs)

    def open_raster(path, mode="r", *args, **kwargs):
        if mode.startswith("w"):
            assert phase[0] == "local", "A composite raster opened during transfer"
            output_writes.append(str(path))
        return actual_open(path, mode, *args, **kwargs)

    def progress(message, fraction, detail):
        if detail.get("workflow_stage") == "process_local" and phase[0] == "download":
            phase[0] = "local"
            network_at_barrier.append(len(provider.process_calls))

    monkeypatch.setattr(HttpClient, "request", request)
    monkeypatch.setattr(rasterio, "open", open_raster)
    result = download_scenes(*args[:3], str(tmp_path), args[3], planned=args[4], progress=progress)
    assert output_writes and network_at_barrier == [len(provider.process_calls)]
    assert len(result["files"]) == (2 if layout == "series" else 1)
    phases = result["phases"]
    assert [phase["status"] for phase in phases] == ["completed", "completed"]
    assert phases[0]["completed_at"] <= phases[1]["started_at"]
    assert phases[0]["downloaded_requests"] == len(provider.process_calls) and phases[0]["reused_requests"] == 0
    with rasterio.open(result["files"][0]["path"]) as dataset:
        assert dataset.crs.to_epsg() == args[4][0]["epsg"]
        assert dataset.transform.a == 10 and dataset.transform.e == -10
        if mode == "research":
            values = dataset.read()
            assert np.all((values == .25) | (values == -9999))
            if layout == "series":
                assert np.all(values == -9999), "The first cloudy date must remain masked"
            else:
                assert np.any(values == .25) and np.any(values == -9999)
            with rasterio.open(result["files"][0]["bundle"]["unmasked"]) as unmasked:
                raw = unmasked.read()
                assert np.any(raw == .25) and np.any(raw == -9999)
    manifest = json.loads(Path(result["manifest"]).read_text(encoding="utf-8"))
    assert manifest["phases"] == phases


@pytest.mark.parametrize("fill_gaps", [False, True])
def test_api_download_probe_avoids_redundant_clear_overlap(client, server, provider, tmp_path, fill_gaps):
    args = setup(client, server, provider, "mosaic", fill_gaps=fill_gaps)
    result = download_scenes(*args[:3], str(tmp_path), args[3], planned=args[4])
    selected = [json.loads(re.search(r"^var allowed = (.*);$", call["json"]["evalscript"], re.M)[1])[0]["id"]
                for call in provider.process_calls]
    assert set(selected) == {args[1][0]["key"].removesuffix(".SAFE")}
    assert result["phases"][0]["downloaded_requests"] < args[4][0]["max_requests"]


@pytest.mark.parametrize("layout", ["series", "mosaic"])
def test_cancelling_api_transfer_leaves_only_reusable_source_blocks(client, server, provider, tmp_path, layout):
    args = setup(client, server, provider, layout)
    cancelled, errors = threading.Event(), []
    provider.block_number = 2

    def run():
        try:
            download_scenes(*args[:3], str(tmp_path), args[3], planned=args[4], cancel=cancelled)
        except Exception as error:
            errors.append(error)

    worker = threading.Thread(target=run)
    worker.start()
    assert provider.trap_started.wait(4)
    cancelled.set()
    worker.join(4)
    assert not worker.is_alive() and len(errors) == 1 and isinstance(errors[0], Cancelled)
    assert list(tmp_path.rglob("*.npz")) and not list(tmp_path.rglob("*.tif"))
    manifest = json.loads(next(tmp_path.rglob("下载记录_*.json")).read_text(encoding="utf-8"))
    assert [phase["status"] for phase in manifest["phases"]] == ["cancelled", "not_started"]
    provider.block_number = None
    result = download_scenes(*args[:3], str(tmp_path), args[3], planned=args[4])
    assert result["phases"][0]["reused_requests"] == 1
    assert result["files"] and not list(tmp_path.rglob("*.partial.tif"))


@pytest.mark.parametrize("layout", ["series", "mosaic"])
@pytest.mark.parametrize("changed", ["array", "receipt"])
def test_changed_downloaded_blocks_stop_offline_processing_without_http(
        client, server, provider, monkeypatch, tmp_path, layout, changed):
    args = setup(client, server, provider, layout)
    network_at_barrier = []

    def progress(message, fraction, detail):
        if detail.get("workflow_stage") != "process_local" or network_at_barrier:
            return
        network_at_barrier.append(len(provider.process_calls))
        cache_root = io_path(detail["folder"]) / ".download-cache"
        if layout == "series":
            cache_root /= source_fingerprint(expected_products(args[1][0]))
        block = next(cache_root.rglob("*.npz"))
        if changed == "array":
            data = bytearray(block.read_bytes())
            data[-1] ^= 1
            block.write_bytes(data)
        else:
            receipt = block.with_suffix(".json")
            document = json.loads(receipt.read_text(encoding="utf-8"))
            document["detail_sha256"] = "0" * 64
            receipt.write_text(json.dumps(document), encoding="utf-8")

        def forbidden(*args, **kwargs):
            raise AssertionError("Local processing attempted to replace a changed source via HTTP")

        monkeypatch.setattr(HttpClient, "request", forbidden)

    with pytest.raises(UserError, match="源区块缺失或校验失败"):
        download_scenes(*args[:3], str(tmp_path), args[3], planned=args[4], progress=progress)
    assert network_at_barrier == [len(provider.process_calls)]
    assert not list(tmp_path.rglob("*.tif"))
