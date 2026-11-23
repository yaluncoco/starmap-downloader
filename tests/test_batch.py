"""Shared AOI/date batches, source isolation, dependencies and atomic auth preflight."""
from __future__ import annotations

import copy
import csv
import hashlib
import json
import threading
from pathlib import Path

import pytest
import rasterio
from shapely.geometry import mapping, shape

from conftest import StubProvider, StubResponse
from core.batch_sources import SOURCE_INFO
from core.gedi_catalogue import CMR_URL, GEDI
from core.geometry import load_aoi
from core.http import HttpClient
from core.http import PROCESS_URL, SH_CATALOGUE_URL, TOKEN_URL
from core.native_catalogue import STAC_ROOT
from core.service import PROFILE_IDS, S2_PUBLIC
from test_gedi import GediProvider, KEY, TOKEN, h5_bytes, reference_and_aoi, synthetic_item
from test_public_workflow import EARTH_SEARCH, PublicProvider, asset, raster
from test_raw_workflow import RawProvider, TOKEN as RAW_TOKEN

DATES = {"start": "2024-06-10", "end": "2024-06-10"}
OPTICAL = {"mode": "ndvi", "resolution": 20, "max_cloud": 20, "mask_clouds": True}


class BatchProvider:
    def __init__(self, tmp_path):
        self.reference, affine, self.geometry = reference_and_aoi(tmp_path)
        content = h5_bytes(KEY, affine)
        self.gedi = GediProvider(synthetic_item(KEY, content, self.geometry), content)
        self.public = PublicProvider()
        self.process = StubProvider()
        self.raw = RawProvider()
        self.raw_features = None
        self.raw_assets = {}
        self.public.features[0]["assets"].update(green=asset("B03"), blue=asset("B02"))
        self.public.content.update({"B03.tif": raster(2100), "B02.tif": raster(2200)})
        from core.planning import RESEARCH_BANDS
        from core.public_catalogue import BAND_ASSET
        for band in RESEARCH_BANDS:
            if BAND_ASSET[band] not in self.public.features[0]["assets"]:
                self.public.features[0]["assets"][BAND_ASSET[band]] = asset(band)
                self.public.content[band + ".tif"] = raster(2400)
        self.cmr_empty = False
        self.cmr_failure = False
        self.catalogue_criteria = []

    def request(self, method, url, **kwargs):
        if url in (PROCESS_URL, SH_CATALOGUE_URL, TOKEN_URL):
            return self.process.request(method, url, **kwargs)
        if url == STAC_ROOT + "/search" and self.raw_features is not None:
            self.catalogue_criteria.append((url, copy.deepcopy(kwargs["json"])))
            return StubResponse(data={"features": self.raw_features})
        if url in self.raw_assets:
            assert kwargs["headers"]["Authorization"] == "Bearer " + RAW_TOKEN
            data = self.raw_assets[url]
            return StubResponse(content=data, headers={"Content-Length": str(len(data)), "ETag": '"raw-raster-v1"'})
        if "dataspace.copernicus.eu" in url:
            return self.raw.request(method, url, **kwargs)
        if url == CMR_URL:
            self.catalogue_criteria.append((url, copy.deepcopy(kwargs["params"])))
            if self.cmr_failure:
                from core.errors import UserError
                raise UserError("NASA CMR 暂时不可用。")
            if self.cmr_empty:
                return StubResponse(data={"items": []})
        if url == EARTH_SEARCH:
            self.catalogue_criteria.append((url, copy.deepcopy(kwargs["json"])))
        if url == CMR_URL or "nasa.gov" in url or "amazonaws.com/test-file" in url:
            return self.gedi.request(method, url, **kwargs)
        return self.public.request(method, url, **kwargs)


@pytest.fixture
def batch_setup(monkeypatch, tmp_path):
    upstream = BatchProvider(tmp_path)
    monkeypatch.setattr(HttpClient, "request", lambda _, *args, **kwargs: upstream.request(*args, **kwargs))
    return upstream


def search_batch(client, setup, *, align=True, sample=True, extra=None, primary=S2_PUBLIC, primary_options=None, primary_criteria=None):
    area = client.post("aoi", {"geometry": mapping(setup.geometry)})
    companions = [{"id": GEDI, "profile": GEDI, "criteria": {"collection": KEY},
                   "options": {"mode": "aligned", "fields": ["rh95", "rh98"], "resolution": 20}}]
    if extra:
        companions.extend(extra if isinstance(extra, list) else [extra])
    criteria = primary_criteria if primary_criteria is not None else {**DATES, "max_cloud": 20}
    response = client.post("batch-search", {"geometry": area["geometry"], "profile": primary, **criteria,
                                            "shared_dates": DATES, "companions": companions})
    result = client.wait(response["job_id"])["result"]
    batch = result["batch"]
    selections = []
    for group in batch["groups"][1:]:
        original = next(c for c in companions if c["id"] == group["id"])
        selections.append({"id": group["id"], "profile": group["profile"], "criteria": group["criteria"],
                           "scene_keys": [s["key"] for s in group["scenes"]], "options": original["options"],
                           "align_to_primary": align if group["id"] == GEDI else False,
                           "sample_primary": sample and align if group["id"] == GEDI else False})
    return {"profile": primary, "geometry": area["geometry"], "criteria": criteria,
            "shared_dates": DATES, "batch_id": batch["id"], "query_id": result["query"]["id"],
            "scene_keys": [s["key"] for s in result["scenes"]], "options": copy.deepcopy(primary_options or OPTICAL), "companions": selections}


def prepare(client, request, output):
    request = {**request, "output_dir": str(output)}
    plan = client.post("batch-plan", request)
    return {**request, "plan_fingerprint": plan["fingerprint"]}, plan


def test_all_existing_sources_can_be_attached():
    assert set(SOURCE_INFO) == set(PROFILE_IDS)


def test_same_dates_and_geometry_auth_before_any_download(client, batch_setup, tmp_path):
    request = search_batch(client, batch_setup)
    request, plan = prepare(client, request, tmp_path / "auth-first")
    assert plan["requires"] == ["earthdata"]
    assert "Earthdata" in client.post("batch-download", request, 400)["error"]
    assert not Path(request["output_dir"]).exists()
    assert not batch_setup.gedi.downloads
    public_search, cmr_search = batch_setup.catalogue_criteria
    assert public_search[1]["datetime"] == "2024-06-10T00:00:00Z/2024-06-11T00:00:00Z"
    assert cmr_search[1]["temporal"] == "2024-06-10T00:00:00Z,2024-06-11T00:00:00Z"
    assert shape(public_search[1]["intersects"]).equals(shape(request["geometry"]))
    assert list(map(float, cmr_search[1]["bounding_box"].split(","))) == pytest.approx(batch_setup.geometry.bounds)


def test_real_download_pipeline_follows_main_grid_and_samples_it(client, batch_setup, tmp_path):
    client.post("earthdata-connect", {"token": TOKEN})
    request, plan = prepare(client, search_batch(client, batch_setup), tmp_path / "联合结果")
    assert plan["groups"][1]["plans"][0]["grid"]["width"] == plan["groups"][0]["plans"][0]["width"]
    result = client.wait(client.post("batch-download", request)["job_id"])["result"]
    assert result["status"] == "completed" and all(g["status"] == "completed" for g in result["sources"])
    primary = result["sources"][0]["results"][0]
    gedi = result["sources"][1]["results"][0]
    primary_image = Path(primary["files"][0]["path"])
    with rasterio.open(primary_image) as image, rasterio.open(Path(gedi["folder"]) / "gedi_metrics.tif") as metrics:
        assert metrics.crs == image.crs and metrics.transform == image.transform and metrics.shape == image.shape
        assert metrics.res == (20, 20)
    assert gedi["plan"]["options"]["reference_path"] == str(primary_image)
    with (Path(gedi["folder"]) / "footprints.csv").open(encoding="utf-8-sig", newline="") as file:
        rows = list(csv.DictReader(file))
    assert len(rows) == 4
    assert float(rows[0]["reference_band_1"]) == pytest.approx(1 / 3, abs=1e-6)
    boundary = Path(result["folder"]) / "研究区.shp"
    assert load_aoi(boundary).equals(batch_setup.geometry)
    manifest = json.loads(Path(result["manifest"]).read_text(encoding="utf-8"))
    assert manifest["shared_dates"] == DATES and manifest["primary_reference"] == str(primary_image)
    assert TOKEN not in json.dumps(manifest) and TOKEN not in client.get("bootstrap").text
    again = client.wait(client.post("batch-download", request)["job_id"])["result"]
    assert len(batch_setup.gedi.downloads) == 1
    assert all(item["status"] == "skipped" for item in again["files"] if item["source"] not in ("boundary", "manifest"))


def test_repeated_search_keeps_batch_fingerprint_and_originals(client, batch_setup, tmp_path):
    first = client.post("batch-plan", search_batch(client, batch_setup))
    second = client.post("batch-plan", search_batch(client, batch_setup))
    assert first["fingerprint"] == second["fingerprint"]


@pytest.mark.parametrize("change", ["date", "source_date", "area", "sources", "file", "reference", "fingerprint"])
def test_stale_or_changed_batch_is_not_downloaded(client, batch_setup, tmp_path, change):
    client.post("earthdata-connect", {"token": TOKEN})
    request, _ = prepare(client, search_batch(client, batch_setup), tmp_path / change)
    modified = copy.deepcopy(request)
    if change == "date":
        modified["shared_dates"]["end"] = "2024-06-11"
    elif change == "source_date":
        modified["companions"][0]["criteria"]["end"] = "2024-06-11"
    elif change == "area":
        modified["geometry"]["coordinates"][0][1][0] += 0.00001
    elif change == "sources":
        modified["companions"] = []
    elif change == "file":
        modified["companions"][0]["scene_keys"] = ["G-not-in-this-search"]
    elif change == "reference":
        modified["primary_reference"] = "not-a-selected-image"
    else:
        modified["plan_fingerprint"] = "old-plan"
    client.post("batch-download", modified, 400)
    assert not Path(modified["output_dir"]).exists() and not batch_setup.gedi.downloads


@pytest.mark.parametrize("failure", ["empty", "search_failed", "download_failed"])
def test_missing_or_failed_gedi_is_reported_and_primary_preserved(client, batch_setup, tmp_path, failure):
    batch_setup.cmr_empty = failure == "empty"
    batch_setup.cmr_failure = failure == "search_failed"
    if failure == "download_failed":
        batch_setup.gedi.status = 403
        client.post("earthdata-connect", {"token": TOKEN})
    request, plan = prepare(client, search_batch(client, batch_setup), tmp_path / failure)
    expected = "completed" if failure == "empty" else "partial"
    result = client.wait(client.post("batch-download", request)["job_id"], expected)["result"]
    assert result["sources"][0]["status"] == "completed"
    assert Path(result["sources"][0]["results"][0]["files"][0]["path"]).is_file()
    assert result["sources"][1]["status"] == {"empty": "empty", "search_failed": "search_failed", "download_failed": "failed"}[failure]
    assert json.loads(Path(result["manifest"]).read_text(encoding="utf-8"))["status"] == expected
    if failure != "download_failed":
        assert plan["requires"] == []


def test_main_failure_does_not_substitute_another_reference(client, batch_setup, tmp_path):
    client.post("earthdata-connect", {"token": TOKEN})
    batch_setup.public.content["B04.tif"] = raster(2000, crs=4326)
    request, _ = prepare(client, search_batch(client, batch_setup), tmp_path / "bad-main")
    result = client.wait(client.post("batch-download", request)["job_id"], "failed")["result"]
    assert result["sources"][0]["status"] == "failed" and result["sources"][1]["status"] == "dependency_failed"
    assert not batch_setup.gedi.downloads
    assert not list(Path(result["folder"]).rglob("*.h5"))


def test_independent_sources_complete_in_product_queue_order(client, server, batch_setup, tmp_path, monkeypatch):
    client.post("earthdata-connect", {"token": TOKEN})
    request, _ = prepare(client, search_batch(client, batch_setup, align=False, sample=False), tmp_path / "parallel")
    original = server.service._run_source
    events = []
    active = []
    def run(profile, *args, **kwargs):
        assert not active
        active.append(profile)
        events.append(("start", profile))
        try:
            result = original(profile, *args, **kwargs)
            assert result["files"] and all(Path(item["path"]).is_file() for item in result["files"])
            return result
        finally:
            events.append(("finish", profile))
            active.pop()
    monkeypatch.setattr(server.service, "_run_source", run)
    result = client.wait(client.post("batch-download", request)["job_id"])["result"]
    assert all(group["status"] == "completed" for group in result["sources"])
    assert events == [("start", S2_PUBLIC), ("finish", S2_PUBLIC), ("start", GEDI), ("finish", GEDI)]


def test_cancel_cleans_up_gedi_and_keeps_completed_image(client, server, batch_setup, tmp_path):
    client.post("earthdata-connect", {"token": TOKEN})
    request, _ = prepare(client, search_batch(client, batch_setup, align=True), tmp_path / "cancel")
    batch_setup.gedi.block = True
    identifier = client.post("batch-download", request)["job_id"]
    assert batch_setup.gedi.started.wait(5)
    client.post("cancel", {"job_id": identifier})
    client.wait(identifier, "cancelled")
    assert not list(Path(request["output_dir"]).rglob("*.part"))
    assert list(Path(request["output_dir"]).rglob("*.tif"))
    manifest = next(Path(request["output_dir"]).rglob("联合下载记录_*.json"))
    assert json.loads(manifest.read_text(encoding="utf-8"))["status"] == "cancelled"
    batch_setup.gedi.block = False
    recovered = client.wait(client.post("batch-download", request)["job_id"])["result"]
    assert all(source["status"] == "completed" for source in recovered["sources"])
    assert recovered["sources"][0]["results"][0]["files"][0]["status"] == "skipped"


def test_prelogin_does_not_change_primary_or_expose_credentials(client, batch_setup):
    request = search_batch(client, batch_setup)
    before = client.get("bootstrap").json()
    client.post("earthdata-connect", {"token": TOKEN})
    after = client.get("bootstrap").json()
    assert after["query"] == before["query"] and after["batch"]["id"] == before["batch"]["id"]
    assert after["earthdata_connected"] and not after["connected"]
    assert TOKEN not in json.dumps(after)
    assert client.post("batch-plan", request)["scene_count"] == 2


def test_multi_scene_raw_assets_are_explicit_and_not_shared_between_products(client, server, batch_setup, monkeypatch):
    original = server.service._search_profile
    scenes = [{"key": key, "title": key, "date": DATES["start"], "time": "00:00", "tiles": ["50TMK"],
               "raw": {"uuid": None, "names": [key], "available": True,
                       "assets": [{"id": band, "name": f"{key}_{band}.tif", "public_url": f"https://example.test/{key}_{band}.tif"}
                                  for band in ("B04", "B08")]}} for key in ("native-one", "native-two")]
    monkeypatch.setattr(server.service, "_search_profile", lambda profile, *args, **kwargs: copy.deepcopy(scenes) if profile == "s2-l2a-cog" else original(profile, *args, **kwargs))
    extra = {"id": "s2-l2a-cog", "profile": "s2-l2a-cog", "criteria": {"max_cloud": 20}, "options": {"mode": "assets"}}
    request = search_batch(client, batch_setup, extra=extra)
    request["companions"][1]["scene_assets"] = {"native-one": ["B04"], "native-two": ["B08"]}
    plan = client.post("batch-plan", request)
    units = plan["groups"][2]["plans"]
    assert len(units) == 2 and units[0]["targets"][0]["asset"] == "B04" and units[1]["targets"][0]["asset"] == "B08"
    request["companions"][1]["scene_assets"].pop("native-two")
    assert "每个" in client.post("batch-plan", request, 400)["error"]


def test_static_companion_uses_region_without_claiming_acquisition_date(client, server, batch_setup, monkeypatch):
    original = server.service._search_profile
    captured = []
    def search(profile, geometry, criteria, **kwargs):
        if profile == "dem-30":
            captured.append((geometry, criteria))
            return []
        return original(profile, geometry, criteria, **kwargs)
    monkeypatch.setattr(server.service, "_search_profile", search)
    extra = {"id": "dem-30", "profile": "dem-30", "criteria": {"start": None, "end": None}, "options": {"mode": "dem"}}
    request = search_batch(client, batch_setup, extra=extra)
    plan = client.post("batch-plan", request)
    assert captured[0][0].equals(batch_setup.geometry) and captured[0][1] == {"start": None, "end": None}
    assert "静态" in plan["groups"][2]["time_note"]


def test_four_sources_download_only_after_all_accounts_are_connected(client, batch_setup, tmp_path):
    client.post("connect", {"client_id": "mock-client", "client_secret": "mock-secret"})
    extra = [{"id": "s2-process", "profile": "s2-process", "criteria": {"max_cloud": 20}, "options": OPTICAL},
             {"id": "cdse-original", "profile": "cdse-original", "criteria": {"collection": "sentinel-2-l2a"},
              "options": {"mode": "original"}}]
    request, plan = prepare(client, search_batch(client, batch_setup, extra=extra), tmp_path / "四来源")
    assert set(plan["requires"]) == {"earthdata", "odata", "sentinel-hub"}
    error = client.post("batch-download", request, 400)["error"]
    assert "Earthdata" in error and "OData" in error
    client.post("earthdata-connect", {"token": TOKEN})
    assert "OData" in client.post("batch-download", request, 400)["error"]
    assert not Path(request["output_dir"]).exists() and not batch_setup.gedi.downloads and not batch_setup.process.process_calls
    client.post("raw-connect", {"method": "token", "token": RAW_TOKEN})
    result = client.wait(client.post("batch-download", request)["job_id"])["result"]
    assert len(result["sources"]) == 4 and all(group["status"] == "completed" for group in result["sources"])
    assert len(batch_setup.gedi.downloads) == 1 and len(batch_setup.process.process_calls) == 1
    assert any(Path(item["path"]).suffix == ".zip" for item in result["files"])


def add_second_public_scene(setup):
    first = setup.public.features[0]
    second = copy.deepcopy(first)
    old = first["id"]
    second["id"] = old.replace("S2A", "S2B")
    second["properties"]["s2:product_uri"] = first["properties"]["s2:product_uri"].replace("S2A", "S2B")
    for value in second["assets"].values():
        value["href"] = value["href"].replace(old, second["id"])
    setup.public.features.append(second)
    return second["id"]


@pytest.mark.parametrize("selected_reference", [False, True])
def test_mid_source_failure_retains_files_and_never_substitutes_reference(client, batch_setup, tmp_path, monkeypatch, selected_reference):
    from core.errors import UserError
    second = add_second_public_scene(batch_setup)
    original = batch_setup.request
    def request(method, url, **kwargs):
        if second in url and url.endswith("B04.tif"):
            raise UserError("第二景源文件暂时不可用。")
        return original(method, url, **kwargs)
    monkeypatch.setattr(batch_setup, "request", request)
    client.post("earthdata-connect", {"token": TOKEN})
    req = search_batch(client, batch_setup, align=selected_reference, sample=selected_reference)
    req["scene_keys"] = [batch_setup.public.features[0]["id"], second]
    if selected_reference:
        plan = client.post("batch-plan", req)
        assert len(plan["reference_candidates"]) == 2
        req["primary_reference"] = next(c["id"] for c in plan["reference_candidates"] if c["scene_key"] == second)
    req, _ = prepare(client, req, tmp_path / "部分来源")
    result = client.wait(client.post("batch-download", req)["job_id"], "partial")["result"]
    main = result["sources"][0]
    assert main["status"] == "partial" and len(main["results"][0]["files"]) == 1
    assert main["results"][0]["interrupted"]
    assert Path(main["results"][0]["files"][0]["path"]).is_file()
    assert any(item["source"] == "primary" for item in result["files"])
    if selected_reference:
        assert result["sources"][1]["status"] == "dependency_failed" and not batch_setup.gedi.downloads
    else:
        assert result["sources"][1]["status"] == "completed"
    manifest = json.loads(Path(result["manifest"]).read_text(encoding="utf-8"))
    assert manifest["status"] == "partial" and manifest["sources"][0]["results"][0]["files"]
    monkeypatch.setattr(batch_setup, "request", original)
    recovered = client.wait(client.post("batch-download", req)["job_id"])["result"]
    assert all(source["status"] == "completed" for source in recovered["sources"])
    assert recovered["sources"][0]["results"][0]["files"][0]["status"] == "skipped"
    if selected_reference:
        reference_path = recovered["sources"][1]["results"][0]["plan"]["options"]["reference_path"]
        assert Path(reference_path).name.startswith("S2B_")
        recovered_manifest = json.loads(Path(recovered["manifest"]).read_text(encoding="utf-8"))
        assert recovered_manifest["primary_reference"] == reference_path


def test_raw_primary_uses_explicit_asset_actual_filename_and_grid(client, batch_setup, tmp_path):
    from rasterio.io import MemoryFile
    from test_raw_workflow import feature as raw_feature, PRODUCT_URL

    feature = raw_feature()
    with rasterio.open(batch_setup.reference) as image, MemoryFile() as memory:
        values = image.read(1)
        values[values != image.nodata] += 100
        with memory.open(**image.profile) as writer:
            writer.write(values, 1)
            writer.scales, writer.offsets = image.scales, image.offsets
        nir_bytes = memory.read()
    assets = {}
    for band, data in (("B04", batch_setup.reference.read_bytes()), ("B08", nir_bytes)):
        url = PRODUCT_URL.replace("/$value", f"/Nodes({feature['id']}.SAFE)/Nodes({band}.tif)/$value")
        assets[band] = {"href": url, "type": "image/tiff", "roles": ["data"], "file:size": len(data),
                        "file:checksum": "1220" + hashlib.sha256(data).hexdigest()}
        batch_setup.raw_assets[url] = data
    feature["assets"] = assets
    batch_setup.raw_features = [feature]
    client.post("earthdata-connect", {"token": TOKEN})
    client.post("raw-connect", {"method": "token", "token": RAW_TOKEN})
    req = search_batch(client, batch_setup, primary="cdse-original", primary_options={"mode": "assets", "assets": ["B04", "B08"]},
                       primary_criteria={**DATES, "collection": "sentinel-2-l2a", "radar_mode": ""})
    plan = client.post("batch-plan", req)
    assert len(plan["reference_candidates"]) == 2 and plan["groups"][1]["reference_grid_pending"]
    req["primary_reference"] = next(c["id"] for c in plan["reference_candidates"] if c["title"].startswith("B08.tif"))
    req, _ = prepare(client, req, tmp_path / "原文件参考")
    result = client.wait(client.post("batch-download", req)["job_id"])["result"]
    gedi = result["sources"][1]["results"][0]
    actual_reference = Path(gedi["plan"]["options"]["reference_path"])
    assert actual_reference.name.lower().startswith("b08_") and actual_reference.name != "B08.tif"
    with rasterio.open(actual_reference) as reference, rasterio.open(Path(gedi["folder"]) / "gedi_metrics.tif") as metrics:
        assert reference.shape == metrics.shape and reference.transform == metrics.transform and reference.crs == metrics.crs
    with (Path(gedi["folder"]) / "footprints.csv").open(encoding="utf-8-sig", newline="") as file:
        rows = list(csv.DictReader(file))
    assert float(rows[0]["reference_band_1"]) == pytest.approx(.07, abs=1e-6)


def test_unselected_static_primary_can_download_companion(client, server, batch_setup, tmp_path, monkeypatch):
    original = server.service._search_profile
    monkeypatch.setattr(server.service, "_search_profile", lambda profile, *args, **kwargs: [] if profile == "dem-30" else original(profile, *args, **kwargs))
    client.post("earthdata-connect", {"token": TOKEN})
    req = search_batch(client, batch_setup, primary="dem-30", primary_options={"mode": "dem"},
                       primary_criteria={"start": None, "end": None}, align=False, sample=False)
    req["scene_keys"] = []
    req, plan = prepare(client, req, tmp_path / "仅附带来源")
    assert plan["groups"][0]["criteria"] == {"start": None, "end": None} and "静态" in plan["groups"][0]["time_note"]
    result = client.wait(client.post("batch-download", req)["job_id"])["result"]
    assert result["sources"][0]["status"] == "empty" and result["sources"][1]["status"] == "completed"


def test_observational_primary_cannot_bypass_shared_dates(client, batch_setup):
    area = client.post("aoi", {"geometry": mapping(batch_setup.geometry)})
    error = client.post("batch-search", {"profile": "cdse-original", "collection": "sentinel-2-l2a", "start": None, "end": None,
                                        "geometry": area["geometry"], "shared_dates": DATES,
                                        "companions": [{"id": GEDI, "profile": GEDI, "criteria": {"collection": KEY}}]}, 400)["error"]
    assert "同一个" in error


def test_deep_windows_output_path_downloads_and_reuses_verified_files(client, batch_setup, tmp_path):
    client.post("earthdata-connect", {"token": TOKEN})
    output = tmp_path.joinpath(*(["外业数据与多来源卫星观测结果归档"] * 12))
    request, _ = prepare(client, search_batch(client, batch_setup), output)
    result = client.wait(client.post("batch-download", request)["job_id"])["result"]
    assert len(str(output)) > 260
    assert all(Path(item["path"]).is_file() for item in result["files"])
    assert load_aoi(Path(result["folder"]) / "研究区.shp").equals(batch_setup.geometry)
    again = client.wait(client.post("batch-download", request)["job_id"])["result"]
    assert len(batch_setup.gedi.downloads) == 1
    assert all(item["status"] == "skipped" for item in again["files"] if item["source"] not in ("boundary", "manifest"))
