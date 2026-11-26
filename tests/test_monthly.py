"""Independent monthly research bundles and frozen cancellation/resume contracts."""
from __future__ import annotations

import copy
import datetime as dt
import json
import threading
import time
from pathlib import Path

import numpy as np
import pytest
import rasterio
from shapely.geometry import box, shape

from conftest import BBOX, feature
from core.download_tasks import DownloadTasks
from core.errors import Cancelled, UserError
from core.geometry import load_aoi
from core.http import check_cancel
from core.monthly_workflow import in_month, month_range
from core.planning import RESEARCH_BANDS
from core.service import Service
from test_public_workflow import pub_provider

DEC = "S2A_MSIL2A_20241220T030531_N0510_R075_T50TMK_20241220T081949.SAFE"
JAN = "S2B_MSIL2A_20250110T030529_N0510_R075_T50TMK_20250110T082015.SAFE"
PERIOD = {"start_month": "2024-12", "end_month": "2025-02"}
OPTIONS = {"mode": "research", "layout": "mosaic", "resolution": 10, "max_cloud": 100,
           "mask_clouds": True, "priority": "cloud", "fill_gaps": True}


def search(client, provider, *, end="2025-02"):
    extent = box(116.349, 39.899, 116.356, 39.906)
    provider.catalogue = [feature(DEC, "2024-12-20T03:16:50Z", cloud=20, footprint=extent),
                          feature(JAN, "2025-01-10T03:16:50Z", cloud=1, footprint=extent)]
    client.post("connect", {"client_id": "mock-client", "client_secret": "mock-secret"})
    aoi = client.post("aoi", {"coordinates": BBOX})
    request = {"geometry": aoi["geometry"], "profile": "s2-process", **PERIOD, "end_month": end, "max_cloud": 100}
    found = client.wait(client.post("monthly-search", request)["job_id"])["result"]["monthly"]
    return aoi, found


def selection(aoi, found, tmp_path):
    return {"geometry": aoi["geometry"], "profile": "s2-process", "monthly_id": found["id"],
            "start_month": found["start_month"], "end_month": found["end_month"], "output_dir": str(tmp_path / "months"),
            "months": [{"month": group["month"], "include": True, "query_id": group["query"]["id"],
                        "criteria": group["criteria"], "scene_keys": group["recommended_keys"], "options": OPTIONS}
                       for group in found["groups"]]}


def prepare(client, request):
    job = client.wait(client.post("prepare-plan", request)["job_id"])
    return {**request, "plan_fingerprint": job["result"]["fingerprint"]}, job["result"]


def test_cross_year_months_leap_days_and_input_bounds():
    result = month_range("2023-12", "2024-03")
    assert [row["month"] for row in result] == ["2023-12", "2024-01", "2024-02", "2024-03"]
    assert result[2]["end"] == "2024-02-29"
    assert month_range("2015-06", "2015-06")[0]["start"] == "2015-06-23"
    now = dt.datetime.now(dt.timezone.utc)
    assert month_range(now.strftime("%Y-%m"), now.strftime("%Y-%m"))[0]["end"] == now.date().isoformat()
    for first, last in [("2024-02", "2024-01"), ("2024-13", "2024-13"), ("2024-2", "2024-03"),
                        ("2015-05", "2015-06"), ("2099-01", "2099-02")]:
        with pytest.raises(UserError):
            month_range(first, last)


def test_monthly_search_never_imports_adjacent_month_products_and_empty_month_is_explicit(client, provider, tmp_path):
    aoi, found = search(client, provider)
    december, january, february = found["groups"]
    assert december["recommended_keys"] == [DEC] and january["recommended_keys"] == [JAN]
    assert all(in_month(scene, group["month"]) for group in found["groups"] for scene in group["scenes"])
    assert february["status"] == "empty" and february["scenes"] == []
    request = selection(aoi, found, tmp_path)
    coverage = client.post("coverage", {"geometry": aoi["geometry"], "profile": "s2-process", "monthly_id": found["id"],
                                        "month": january["month"], **request["months"][1]})
    assert coverage["dates"] == ["2025-01-10"] and coverage["coverage_percent"] == 100
    bad = copy.deepcopy(request)
    bad["months"][1]["scene_keys"] = [DEC]
    client.post("monthly-plan", bad, status=400)
    bad = copy.deepcopy(request)
    bad["months"][0]["criteria"]["max_cloud"] = 20
    client.post("monthly-plan", bad, status=400)
    bad = {**request, "start_month": "2024-11"}
    client.post("monthly-plan", bad, status=400)


def test_public_monthly_catalogue_includes_product_acquisition_time(client, pub_provider):
    aoi = client.post("aoi", {"coordinates": BBOX})
    result = client.wait(client.post("monthly-search", {"geometry": aoi["geometry"], "profile": "s2-earth-search",
                                                      "start_month": "2024-06", "end_month": "2024-06", "max_cloud": 100})["job_id"])["result"]
    group, = result["monthly"]["groups"]
    assert group["status"] == "ready" and group["recommended_keys"]
    assert all(in_month(scene, "2024-06") for scene in group["scenes"])
    assert all(product["acquisition_utc"] == scene["datetime"] for scene in group["scenes"] for product in scene["products"])


def test_each_month_has_one_complete_research_bundle_and_full_root_shp(client, provider, tmp_path):
    aoi, found = search(client, provider)
    request, plan = prepare(client, selection(aoi, found, tmp_path))
    assert plan["kind"] == "monthly" and plan["output_count"] == 2
    result = client.wait(client.post("monthly-download", request)["job_id"])["result"]
    assert len(result["files"]) == 2
    assert [row["status"] for row in result["months"]] == ["completed", "completed", "empty"]
    assert not (Path(result["folder"]) / "2025-02").exists()
    document = json.loads(Path(result["manifest"]).read_text(encoding="utf-8"))
    assert load_aoi(document["boundary"]).equals(shape(aoi["geometry"]))
    assert len(document["boundary_files"]) == 5 and document["status"] == "completed"
    for row in result["months"][:2]:
        assert all(date[:7] == row["month"] for date in row["result"]["used_dates"])
        file, = row["result"]["files"]
        assert row["month"] in Path(file["path"]).parts
        with rasterio.open(file["path"]) as analysis, rasterio.open(file["bundle"]["unmasked"]) as unmasked, \
                rasterio.open(file["bundle"]["scl"]) as scl, rasterio.open(file["bundle"]["source"]) as source:
            assert analysis.count == unmasked.count == 10 and analysis.descriptions == tuple(RESEARCH_BANDS)
            assert all(ds.transform == analysis.transform and ds.crs == analysis.crs for ds in (unmasked, scl, source))
            valid = source.read(1) > 0
            assert valid.any() and set(np.unique(source.read(1))) <= {0, 1}
            np.testing.assert_array_equal(scl.read(1)[valid], 4)
            np.testing.assert_array_equal(analysis.read()[:, valid], unmasked.read()[:, valid])
            np.testing.assert_allclose(analysis.read()[:, valid], .25)
    assert len(provider.process_calls) == 2
    changed = copy.deepcopy(request)
    changed["months"][0]["include"] = False
    client.post("monthly-download", changed, status=400)


def test_month_checkbox_prevents_unselected_month_transfer(client, provider, tmp_path):
    aoi, found = search(client, provider)
    request = selection(aoi, found, tmp_path)
    request["months"][1]["include"] = False
    request, plan = prepare(client, request)
    assert plan["output_count"] == 1
    result = client.wait(client.post("monthly-download", request)["job_id"])["result"]
    assert result["months"][1]["status"] == "not_selected" and len(provider.process_calls) == 1
    assert not (Path(result["folder"]) / "2025-01").exists()


def test_cancel_and_cold_task_store_resume_original_months_after_ui_boundary_changes(client, provider, server, tmp_path):
    aoi, found = search(client, provider)
    request, _ = prepare(client, selection(aoi, found, tmp_path))
    provider.block_number = 2
    first = client.post("monthly-download", request)["job_id"]
    assert provider.trap_started.wait(5)
    client.post("cancel", {"job_id": first})
    cancelled = client.wait(first, status="cancelled")["result"]
    assert cancelled["files"] == []  # No city mosaic is published before all downloads finish.
    downloaded = cancelled["months"][0]["result"]
    assert cancelled["months"][0]["status"] == "downloaded"
    assert downloaded["stages"][0]["status"] == "completed"
    assert downloaded["stages"][1]["status"] == "pending"
    original = Path(downloaded["downloaded_source_files"][0]["path"])
    before = original.stat().st_mtime_ns
    cold = DownloadTasks(server.service.config_dir)
    kind, definition = cold.load(first)
    assert kind == "monthly-download"
    assert definition["geometry"] == aoi["geometry"]
    assert [row["month"] for row in definition["groups"]] == ["2024-12", "2025-01", "2025-02"]
    previous = server.service
    previous.shutdown()
    server.service = Service(server.service.config_dir)
    until = time.monotonic() + 10
    while server.service.accounts.restoring and time.monotonic() < until:
        time.sleep(.02)
    assert not server.service.accounts.restoring
    if not server.service.auth.connected:
        client.post("connect", {"client_id": "mock-client", "client_secret": "mock-secret"})
    client.post("aoi", {"coordinates": "117,40,117.01,40.01"})
    provider.block_number = None
    second = client.post("retry-download", {"job_id": first})["job_id"]
    result = client.wait(second)["result"]
    assert result["months"][0]["result"]["downloaded_source_files"][0]["status"] == "skipped"
    assert result["months"][0]["result"]["files"][0]["status"] == "completed"
    assert original.stat().st_mtime_ns == before
    assert Path(result["folder"]) == Path(cancelled["folder"])
    assert load_aoi(Path(result["folder"]) / "研究区.shp").equals(shape(aoi["geometry"]))
    assert client.get("resumable-downloads").json()["tasks"] == []


def test_async_monthly_planning_registers_before_work_and_cancels_promptly(client, provider, server, monkeypatch, tmp_path):
    aoi, found = search(client, provider)
    request = selection(aoi, found, tmp_path)
    started = threading.Event()
    def blocked(*args, cancel=None, **kwargs):
        started.set()
        assert cancel is not None
        cancel.wait(5)
        check_cancel(cancel)
        raise AssertionError("Planning should have been cancelled")
    monkeypatch.setattr(server.service, "_plan_source", blocked)
    begin = time.monotonic()
    job_id = client.post("prepare-plan", request)["job_id"]
    assert time.monotonic() - begin < 2 and started.wait(2)
    assert client.get("job", params={"id": job_id}).json()["status"] == "running"
    begin = time.monotonic()
    client.post("cancel", {"job_id": job_id})
    client.wait(job_id, status="cancelled")
    assert time.monotonic() - begin < 2


def test_one_failed_month_is_partial_and_other_months_are_reviewable(client, provider, server, monkeypatch, tmp_path):
    aoi, found = search(client, provider)
    request, _ = prepare(client, selection(aoi, found, tmp_path))
    actual = server.service._run_source
    def run(profile, geometry, options, scenes, *args, **kwargs):
        if scenes[0]["key"] == JAN:
            raise UserError("该月测试来源不可用，已保留进度")
        return actual(profile, geometry, options, scenes, *args, **kwargs)
    monkeypatch.setattr(server.service, "_run_source", run)
    job = client.wait(client.post("monthly-download", request)["job_id"], status="partial")
    assert job["retry_available"] and len(job["result"]["files"]) == 1
    assert [row["status"] for row in job["result"]["months"]] == ["completed", "failed", "empty"]
    assert "来源不可用" in job["result"]["months"][1]["error"]
    assert json.loads(Path(job["result"]["manifest"]).read_text(encoding="utf-8"))["status"] == "partial"
