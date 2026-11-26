"""Cross-month transfer limits and a barrier before any offline pixel processing."""
from __future__ import annotations

import copy
import dataclasses
import json
import threading
from pathlib import Path

import pytest
import rasterio
from shapely.geometry import mapping, shape

from conftest import D1, P1
from core import monthly_scenes
from core.http import HttpClient, check_cancel
from core.native_catalogue import DOWNLOAD_ROOT
from core.safe_zip import PROFILE
from test_browser_downloads import NativeTransport
from test_monthly import OPTIONS, prepare, search, selection
from test_native_mosaic import AOI, native_fixture
from test_safe_zip import TOKEN, UUIDS, archive, safe_members, zip_provider


def two_month_zip(client, upstream, paths, tmp_path, *, mode="original", workers=2, per_month=1):
    """Two UTC months, using small real SAFE rasters and independent identities."""
    template = copy.deepcopy(upstream.features[0])
    identifiers = [UUIDS[0], UUIDS[1], "8049c2ac-8878-4cf5-97ba-16d9fa21a06f", "47ee0cf5-989d-4185-a2b2-cd862aac084b"]
    rows, upstream.payloads, upstream.members = [], {}, {}
    for month in (6, 7):
        for day in ([10] if per_month == 1 else [5, 10]):
            stamp = f"2024{month:02d}{day:02d}"
            name = P1.replace("20240610", stamp)
            acquired = D1.replace("2024-06-10", f"2024-{month:02d}-{day:02d}")
            identifier = identifiers[len(rows)]
            row = copy.deepcopy(template)
            members, aliases = safe_members(paths, name, acquired)
            body = archive(members)
            row["id"] = name
            row["properties"]["datetime"] = acquired
            row["assets"] = {
                "product": {"href": f"{DOWNLOAD_ROOT}/Products({identifier})/$value", "roles": ["archive"]},
                **{alias: {"href": f"{DOWNLOAD_ROOT}/Products({identifier})/Nodes({name})/Nodes({Path(member).name})/$value",
                           "roles": ["metadata" if alias.endswith("metadata") or alias == "safe_manifest" else "data"],
                           "file:size": len(members[name + "/" + member])}
                   for alias, member in aliases.items()}}
            rows.append(row)
            upstream.payloads[identifier] = body
            upstream.members[identifier] = members
    upstream.features = rows
    client.post("raw-connect", {"method": "token", "token": TOKEN})
    aoi = client.post("aoi", {"geometry": json.loads(json.dumps(mapping(AOI.buffer(-.001))))})
    found = client.wait(client.post("monthly-search", {"geometry": aoi["geometry"], "profile": PROFILE,
        "start_month": "2024-06", "end_month": "2024-07", "max_cloud": 100})["job_id"])["result"]["monthly"]
    request = selection(aoi, found, tmp_path)
    request["profile"] = PROFILE
    for requested, group in zip(request["months"], found["groups"], strict=True):
        assert len(group["scenes"]) == per_month
        requested.update(scene_keys=[scene["key"] for scene in group["scenes"]],
                         options={**({"mode": "original"} if mode == "original" else OPTIONS),
                                  "download_engine": "edge", "download_workers": workers})
    request, plan = prepare(client, request)
    return request, plan


@pytest.mark.parametrize("mode", ["original", "research"])
def test_single_product_months_share_concurrency_and_all_downloads_precede_processing(
        client, server, zip_provider, native_fixture, monkeypatch, tmp_path, mode):
    request, plan = two_month_zip(client, zip_provider, native_fixture[2], tmp_path, mode=mode)
    transport = NativeTransport(zip_provider, barrier=True)
    server.service.browser_download = transport
    actual = server.service._run_source
    local_calls = []

    def run(*args, **kwargs):
        if kwargs.get("staged") is not None:
            assert sorted(transport.calls) == sorted(zip_provider.payloads)
            assert transport.active == 0
            local_calls.append(args[3][0]["date"][:7])

            def forbidden(*args, **kwargs):
                raise AssertionError("Offline SAFE extraction, mosaic or clip attempted a network request")

            with monkeypatch.context() as scope:
                scope.setattr(HttpClient, "request", forbidden)
                return actual(*args, **kwargs)
        return actual(*args, **kwargs)

    monkeypatch.setattr(server.service, "_run_source", run)
    job = client.wait(client.post("monthly-download", request)["job_id"])
    assert transport.maximum == 2 and transport.active == 0
    assert [row["status"] for row in job["result"]["months"]] == ["completed", "completed"]
    assert local_calls == ([] if mode == "original" else ["2024-06", "2024-07"])
    document = json.loads(Path(job["result"]["manifest"]).read_text(encoding="utf-8"))
    assert plan["download_scope"] == "all-selected-months"
    assert document["phases"][0]["status"] == "completed"
    if mode == "research":
        assert document["phases"][0]["completed_at"] <= document["phases"][1]["started_at"]
        assert document["phases"][1]["status"] == "completed"
        months = [json.loads(Path(row["result"]["manifest"]).read_text(encoding="utf-8"))
                  for row in job["result"]["months"]]
        last_download = max(month["stages"][0]["completed_at"] for month in months)
        assert all(last_download <= month["stages"][1]["started_at"] for month in months)
        assert all(date[:7] == row["month"] for row in job["result"]["months"] for date in row["result"]["used_dates"])
    else:
        assert len(document["phases"]) == 1
        assert [Path(file["path"]).name for file in job["result"]["files"]] == [row["id"] + ".zip" for row in zip_provider.features]
    assert {row["month"] for row in job["detail"]["product_queue"]} == {"2024-06", "2024-07"}
    assert len(job["detail"]["product_queue"]) == 2


@pytest.mark.parametrize("workers", [1, 2, 4])
def test_nested_month_queues_never_multiply_the_selected_product_limit(
        client, server, zip_provider, native_fixture, tmp_path, workers):
    request, _ = two_month_zip(client, zip_provider, native_fixture[2], tmp_path, workers=workers, per_month=2)
    transport = NativeTransport(zip_provider)
    transport.barrier = threading.Barrier(workers)
    server.service.browser_download = transport
    result = client.wait(client.post("monthly-download", request)["job_id"])["result"]
    assert transport.maximum == workers and transport.active == 0
    assert len(transport.calls) == len(set(transport.calls)) == 4
    assert len(result["files"]) == 4


def test_process_api_downloads_both_months_before_any_local_mosaic(
        client, provider, server, monkeypatch, tmp_path):
    aoi, found = search(client, provider)
    request, _ = prepare(client, selection(aoi, found, tmp_path))
    actual = server.service._run_source
    local_calls = []

    def run(*args, **kwargs):
        if kwargs.get("staged") is not None:
            assert len(provider.process_calls) == 2
            local_calls.append(args[3][0]["date"][:7])

            def forbidden(*args, **kwargs):
                raise AssertionError("Process API was called after the global download phase")

            with monkeypatch.context() as scope:
                scope.setattr(HttpClient, "request", forbidden)
                return actual(*args, **kwargs)
        return actual(*args, **kwargs)

    monkeypatch.setattr(server.service, "_run_source", run)
    result = client.wait(client.post("monthly-download", request)["job_id"])["result"]
    assert local_calls == ["2024-12", "2025-01"]
    assert len(result["files"]) == 2
    assert len(provider.process_calls) == 2


@pytest.mark.parametrize("changed", ["folder", "fingerprint", "missing_record"])
def test_changed_staged_inputs_cannot_start_local_processing(
        client, provider, server, monkeypatch, tmp_path, changed):
    aoi, found = search(client, provider)
    request = selection(aoi, found, tmp_path)
    request["months"][1]["include"] = False
    request, _ = prepare(client, request)
    actual = server.service._run_source

    def run(*args, **kwargs):
        result = actual(*args, **kwargs)
        if isinstance(result, monthly_scenes.DownloadedScenes):
            if changed == "folder":
                return dataclasses.replace(result, folder=tmp_path / "other-folder")
            if changed == "fingerprint":
                return dataclasses.replace(result, plan={**result.plan, "fingerprint": "other-plan"})
            return dataclasses.replace(result, records={})
        return result

    monkeypatch.setattr(server.service, "_run_source", run)
    job = client.wait(client.post("monthly-download", request)["job_id"], status="partial")
    assert "冻结计划不一致" in job["result"]["months"][0]["error"]
    assert job["result"]["files"] == []
    assert len(provider.process_calls) == 1
    assert not list(Path(job["result"]["folder"]).rglob("整月拼接"))


def test_cancel_releases_the_cross_month_slot_and_waits_for_the_writer(
        client, server, zip_provider, native_fixture, tmp_path):
    request, _ = two_month_zip(client, zip_provider, native_fixture[2], tmp_path, workers=1)
    transport = NativeTransport(zip_provider)
    started, stopped = threading.Event(), threading.Event()

    def blocked(item, path, auth, cancel, report):
        with transport.lock:
            transport.calls.append(item["uuid"])
            transport.active += 1
        try:
            started.set()
            assert cancel.wait(5)
            check_cancel(cancel)
        finally:
            with transport.lock:
                transport.active -= 1
            stopped.set()

    transport.download = blocked
    server.service.browser_download = transport
    identifier = client.post("monthly-download", request)["job_id"]
    assert started.wait(3)
    client.post("cancel", {"job_id": identifier})
    job = client.wait(identifier, status="cancelled")
    assert stopped.is_set() and transport.active == 0 and len(transport.calls) == 1
    assert not list(Path(request["output_dir"]).rglob("*.part"))
    assert not list(Path(request["output_dir"]).rglob("*.zip"))
    server.service.browser_download = NativeTransport(zip_provider)
    resumed = client.wait(client.post("retry-download", {"job_id": identifier})["job_id"])
    assert len(resumed["result"]["files"]) == 2


@pytest.mark.parametrize("component", ["unmasked", "scl", "receipt"])
def test_local_sources_changed_while_waiting_are_rejected_before_mosaic(
        client, provider, server, monkeypatch, tmp_path, component):
    aoi, found = search(client, provider)
    request = selection(aoi, found, tmp_path)
    request["months"][1]["include"] = False
    request, _ = prepare(client, request)
    actual = server.service._run_source

    def run(*args, **kwargs):
        result = actual(*args, **kwargs)
        if isinstance(result, monthly_scenes.DownloadedScenes):
            file = result.preview()["downloaded_source_files"][0]
            if component == "receipt":
                receipt = Path(file["path"]).with_suffix(".starmap.json")
                receipt.write_text(receipt.read_text(encoding="utf-8") + "\n", encoding="utf-8")
            else:
                path = file["bundle"][component]
                with rasterio.open(path, "r+") as ds:
                    values = ds.read(1)
                    values[0, 0] = (int(values[0, 0]) + 1) % 256 if component == "scl" else float(values[0, 0]) + 1
                    ds.write(values, 1)
        return result

    monkeypatch.setattr(server.service, "_run_source", run)
    job = client.wait(client.post("monthly-download", request)["job_id"], status="partial")
    assert "等待处理期间发生变化" in job["result"]["months"][0]["error"]
    assert job["result"]["files"] == []
    assert len(provider.process_calls) == 1
    assert not list(Path(job["result"]["folder"]).rglob("整月拼接"))
