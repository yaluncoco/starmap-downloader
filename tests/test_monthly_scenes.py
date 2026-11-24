"""Stage barriers, complete scene downloads and exact local monthly clipping."""
from __future__ import annotations

import copy
import json
from pathlib import Path

import numpy as np
import pytest
import rasterio
from pyproj import Transformer
from rasterio.windows import Window
from shapely import contains_xy
from shapely.geometry import box, shape

from conftest import BBOX, feature
from core import monthly_scenes
from core.errors import Cancelled, UserError
from core.http import HttpClient
from core.planning import normalize_options
from core.research_bundle import NODATA
from test_monthly import OPTIONS, prepare, selection
from test_native_mosaic import AOI, native_fixture
from test_public_workflow import pub_provider
from test_s2_native import l2a_archive



@pytest.fixture
def complete_public_scene(l2a_archive, monkeypatch, tmp_path):
    """Full native COG dimensions and real checksums, using controlled HTTP bytes."""
    import hashlib
    import xml.etree.ElementTree as ET
    from rasterio.shutil import copy as rio_copy
    from conftest import StubResponse
    from core.public_catalogue import EARTH_SEARCH

    state = l2a_archive
    feature = state["feature"]
    props = feature["properties"]
    bodies = {"tileInfo.json": state["index"], "product_metadata.xml": state["xml"]}
    for alias, name, value, dtype in (("red", "B04.tif", 2000, "uint16"), ("scl", "SCL.tif", 4, "uint8")):
        asset = feature["assets"][alias]
        height, width = asset["proj:shape"]
        original = tmp_path / ("input-" + name)
        target = tmp_path / name
        with rasterio.open(original, "w", driver="GTiff", width=width, height=height, count=1,
                dtype=dtype, crs=f"EPSG:{props['proj:epsg']}", transform=rasterio.Affine(*asset["proj:transform"]),
                nodata=0, tiled=True, blockxsize=512, blockysize=512, compress="DEFLATE") as ds:
            ds.scales = (asset["raster:bands"][0].get("scale", 1),)
            ds.offsets = (asset["raster:bands"][0].get("offset", 0),)
            for _, window in ds.block_windows(1):
                ds.write(np.full((int(window.height), int(window.width)), value, dtype=dtype), 1, window=window)
        rio_copy(original, target, driver="COG", BLOCKSIZE=512, COMPRESS="DEFLATE", OVERVIEWS="NONE")
        bodies[name] = target.read_bytes()
        asset.update({"file:size": len(bodies[name]),
                      "file:checksum": "1220" + hashlib.sha256(bodies[name]).hexdigest()})
    root = ET.Element("Level-2A_Tile_ID")
    general = ET.SubElement(root, "General_Info")
    for key, value in {"TILE_ID": props["s2:tile_id"], "DATASTRIP_ID": props["s2:datastrip_id"],
                        "SENSING_TIME": props["datetime"]}.items():
        ET.SubElement(general, key).text = value
    geo = ET.SubElement(ET.SubElement(root, "Geometric_Info"), "Tile_Geocoding")
    ET.SubElement(geo, "HORIZONTAL_CS_CODE").text = f"EPSG:{props['proj:epsg']}"
    origin = feature["assets"]["red"]["proj:transform"]
    for resolution in (10, 20, 60):
        position = ET.SubElement(geo, "Geoposition", resolution=str(resolution))
        for key, value in {"ULX": origin[2], "ULY": origin[5], "XDIM": resolution, "YDIM": -resolution}.items():
            ET.SubElement(position, key).text = str(value)
        size = ET.SubElement(geo, "Size", resolution=str(resolution))
        for key in ("NROWS", "NCOLS"):
            ET.SubElement(size, key).text = str(109800 // resolution)
    bodies["metadata.xml"] = ET.tostring(root, encoding="utf-8")
    feature["assets"]["granule_metadata"].update({
        "file:size": len(bodies["metadata.xml"]),
        "file:checksum": "1220" + hashlib.sha256(bodies["metadata.xml"]).hexdigest()})
    calls = []

    def request(_self, method, url, *, cancel=None, **kwargs):
        from core.http import check_cancel
        check_cancel(cancel)
        assert "auth" not in kwargs and "Authorization" not in kwargs.get("headers", {})
        calls.append((method, url))
        if url == EARTH_SEARCH:
            return StubResponse(data={"features": [feature], "links": []})
        name = url.rsplit("/", 1)[-1]
        body = bodies[name]
        headers = {"ETag": '"' + hashlib.md5(body).hexdigest() + '"', "Content-Length": str(len(body)),
                   "Content-Type": "image/tiff" if name.endswith(".tif") else
                                   "application/json" if name.endswith(".json") else "application/xml"}
        return StubResponse(content=b"" if method == "HEAD" else body, headers=headers)

    monkeypatch.setattr(HttpClient, "request", request)
    return feature, bodies, calls


def test_complete_public_cogs_download_first_with_safe_identity_and_large_resolution_windows(
        client, complete_public_scene, monkeypatch, tmp_path):
    import hashlib
    feature, bodies, calls = complete_public_scene
    region = shape(feature["geometry"]).representative_point().buffer(.004)
    aoi = client.post("aoi", {"geometry": json.loads(json.dumps(region.__geo_interface__))})
    result = client.wait(client.post("monthly-search", {
        "geometry": aoi["geometry"], "profile": "s2-earth-search", "start_month": "2024-06",
        "end_month": "2024-06", "max_cloud": 100})["job_id"])["result"]["monthly"]
    request = selection(aoi, result, tmp_path)
    request["profile"] = "s2-earth-search"
    request["months"][0]["options"] = {**OPTIONS, "mode": "custom", "bands": ["B04"], "resolution": 60}
    request, _ = prepare(client, request)
    original = monthly_scenes.download_mosaic
    phase_calls = []
    def offline(*args, **kwargs):
        phase_calls.append(len(calls))
        def forbidden(*args, **kwargs):
            raise AssertionError("Mosaic or crop attempted an HTTP request")
        with monkeypatch.context() as scope:
            scope.setattr(HttpClient, "request", forbidden)
            return original(*args, **kwargs)
    monkeypatch.setattr(monthly_scenes, "download_mosaic", offline)
    job = client.wait(client.post("monthly-download", request)["job_id"])
    month = job["result"]["months"][0]["result"]
    assert len(phase_calls) == 2 and phase_calls[0] == phase_calls[1] == len(calls)
    assert len(month["files"]) == 1
    document = json.loads(Path(month["manifest"]).read_text(encoding="utf-8"))
    assert document["selected_products"] == [feature["properties"]["s2:product_uri"]]
    assert len(document["sources"][0]["files"]) == 5
    for file in document["sources"][0]["files"]:
        path = Path(file["path"])
        receipt = json.loads(path.with_suffix(".starmap.json").read_text(encoding="utf-8"))
        assert hashlib.sha256(path.read_bytes()).hexdigest() == receipt["source_checksums"]["SHA-256"]
    with rasterio.open(month["files"][0]["path"]) as ds:
        values = ds.read(1)
        assert ds.res == (60, 60)
        np.testing.assert_allclose(values[values != NODATA], np.float32(2000) * np.float32(.0001) + np.float32(-.1),
                                   rtol=0, atol=1e-8)
    compare_complete_month_and_clip(month, shape(aoi["geometry"]))


def january_request(client, provider, tmp_path, count=10):
    extent = box(116.349, 39.899, 116.356, 39.906)
    provider.catalogue = [
        feature(f"S2A_MSIL2A_202501{day:02d}T030531_N0510_R075_T50TMK_202501{day:02d}T081949.SAFE",
                f"2025-01-{day:02d}T03:16:50Z", cloud=day, footprint=extent)
        for day in range(1, count + 1)]
    client.post("connect", {"client_id": "mock-client", "client_secret": "mock-secret"})
    aoi = client.post("aoi", {"coordinates": BBOX})
    result = client.wait(client.post("monthly-search", {
        "geometry": aoi["geometry"], "profile": "s2-process", "start_month": "2025-01",
        "end_month": "2025-01", "max_cloud": 100})["job_id"])["result"]["monthly"]
    request = selection(aoi, result, tmp_path)
    request["months"][0]["scene_keys"] = [scene["key"] for scene in result["groups"][0]["scenes"]]
    assert len(request["months"][0]["scene_keys"]) == count
    return aoi, prepare(client, request)[0]


def compare_complete_month_and_clip(result, geometry):
    whole = result["whole_mosaic"]["files"][0]
    clipped = result["files"][0]
    whole_paths = {"analysis": whole["path"], **whole["bundle"]}
    clip_paths = {"analysis": clipped["path"], **clipped["bundle"]}
    with rasterio.open(whole_paths["unmasked"]) as raw, rasterio.open(clip_paths.get("unmasked", clip_paths["analysis"])) as city:
        assert raw.crs == city.crs and raw.res == city.res
        assert raw.width > city.width and raw.height > city.height
        y, x = np.mgrid[0:raw.height, 0:raw.width]
        converter = Transformer.from_crs(raw.crs, 4326, always_xy=True)
        lon, lat = converter.transform(raw.transform.c + (x + .5) * raw.res[0],
                                       raw.transform.f - (y + .5) * raw.res[1])
        whole_inside = contains_xy(geometry, lon, lat)
        assert np.any((raw.read(1) != NODATA) & ~whole_inside)
        col = (city.transform.c - raw.transform.c) / raw.res[0]
        row = (raw.transform.f - city.transform.f) / raw.res[1]
        assert col == round(col) and row == round(row)
        window = Window(round(col), round(row), city.width, city.height)
        y, x = np.mgrid[0:city.height, 0:city.width]
        lon, lat = converter.transform(city.transform.c + (x + .5) * city.res[0],
                                       city.transform.f - (y + .5) * city.res[1])
        inside = contains_xy(geometry, lon, lat)
    for role in ("analysis", "unmasked", "scl", "source"):
        if role not in clip_paths:
            continue
        with rasterio.open(whole_paths[role]) as full, rasterio.open(clip_paths[role]) as city:
            expected = full.read(window=window)
            expected[:, ~inside] = 255 if role == "scl" else 0 if role == "source" else NODATA
            np.testing.assert_array_equal(city.read(), expected)


def test_ten_january_scenes_all_download_before_any_mosaic_and_clip(client, provider, server, monkeypatch, tmp_path):
    aoi, request = january_request(client, provider, tmp_path)
    assert len(provider.process_calls) == 0  # Planning does not read or combine pixels.
    original = monthly_scenes.download_mosaic
    calls = []

    def offline_only(*args, **kwargs):
        assert len(provider.process_calls) == 10
        calls.append((shape(aoi["geometry"]).equals(args[0]), type(kwargs["reader"]).__name__))

        def forbidden(*args, **kwargs):
            raise AssertionError("Local mosaic/crop attempted a network request")

        with monkeypatch.context() as scope:
            scope.setattr(HttpClient, "request", forbidden)
            return original(*args, **kwargs)

    monkeypatch.setattr(monthly_scenes, "download_mosaic", offline_only)
    result = client.wait(client.post("monthly-download", request)["job_id"])["result"]
    month = result["months"][0]["result"]
    assert calls == [(False, "_LocalSceneReader"), (True, "_MonthMosaicReader")]
    assert len(provider.process_calls) == 10 and len(result["files"]) == 1
    document = json.loads(Path(month["manifest"]).read_text(encoding="utf-8"))
    assert len(document["sources"]) == 10
    assert all(source["status"] == "verified" and len(source["files"]) == 1 for source in document["sources"])
    assert document["stages"][0]["result"]["verified_scene_products"] == 10
    stages = document["stages"]
    assert [stage["id"] for stage in stages] == ["download_scenes", "local_mosaic", "clip_region"]
    assert all(stage["status"] == "completed" for stage in stages)
    assert stages[0]["completed_at"] <= stages[1]["started_at"]
    assert stages[1]["completed_at"] <= stages[2]["started_at"]
    table = json.loads(Path(month["files"][0]["bundle"]["sources"]).read_text(encoding="utf-8"))
    assert len(table["sources"]) == 10 and all(row["acquisition_utc"].startswith("2025-01") for row in table["sources"])
    assert table["processing_stage"] == "clip_region" and "no resampling" in table["clip_method"]
    compare_complete_month_and_clip(month, shape(aoi["geometry"]))


@pytest.mark.parametrize("mode", ("rgb", "rgbnir", "all", "ndvi", "custom"))
def test_monthly_converts_saved_reflectance_once_and_keeps_quality_and_sources(
        client, provider, tmp_path, mode):
    _, request = january_request(client, provider, tmp_path, count=2)
    request["months"][0]["options"] = {**OPTIONS, "mode": mode, "bands": ["B12", "B02", "B08"]}
    request, plan = prepare(client, request)
    opts = plan["months"][0]["options"]
    month = client.wait(client.post("monthly-download", request)["job_id"])["result"]["months"][0]["result"]
    assert len(provider.process_calls) == 2
    full, final = month["whole_mosaic"]["files"][0], month["files"][0]
    with rasterio.open(full["bundle"]["unmasked"]) as raw:
        assert raw.count == len(opts["bands"]) and raw.descriptions == tuple(opts["bands"])
        available = raw.read() != NODATA
        np.testing.assert_array_equal(raw.read()[available], np.float32(.25))
    with rasterio.open(final["path"]) as ds, rasterio.open(final["bundle"]["source"]) as source, \
            rasterio.open(final["bundle"]["scl"]) as quality:
        present = source.read(1) > 0
        assert present.any() and ds.count == opts["count"]
        assert ds.dtypes == (opts["dtype"],) * opts["count"]
        assert ds.descriptions == tuple(opts["output_bands"])
        np.testing.assert_array_equal(quality.read(1)[present], 4)
        values = ds.read()
        if mode == "rgb":
            np.testing.assert_array_equal(values[:3, present], 159)
            np.testing.assert_array_equal(values[3, present], 255)
            np.testing.assert_array_equal(values[:, ~present], 0)
        else:
            np.testing.assert_array_equal(values[:, present], 0 if mode == "ndvi" else np.float32(.25))
            np.testing.assert_array_equal(values[:, ~present], NODATA)
    assert set(final["bundle"]) == {"scl", "source", "sources"}


def test_ndvi_monthly_uses_valid_fallback_before_freezing_pixel_ownership(
        client, provider, monkeypatch, tmp_path):
    _, request = january_request(client, provider, tmp_path, count=2)
    request["months"][0]["options"] = {**OPTIONS, "mode": "ndvi"}
    request, _ = prepare(client, request)
    read = monthly_scenes._LocalSceneReader.read

    def denominator_case(self, scene, tile, inside):
        values, quality, evidence = read(self, scene, tile, inside)
        red = values[self.options["bands"].index("B04")]
        nir = values[self.options["bands"].index("B08")]
        available = (red != NODATA) & (nir != NODATA)
        red[available] = -.25 if scene["date"] == "2025-01-01" else .25
        nir[available] = .25 if scene["date"] == "2025-01-01" else .75
        return values, quality, evidence

    monkeypatch.setattr(monthly_scenes._LocalSceneReader, "read", denominator_case)
    month = client.wait(client.post("monthly-download", request)["job_id"])["result"]["months"][0]["result"]
    file = month["files"][0]
    with rasterio.open(file["path"]) as ds, rasterio.open(file["bundle"]["source"]) as source:
        present = source.read(1) > 0
        assert present.any()
        np.testing.assert_array_equal(source.read(1)[present], 2)
        np.testing.assert_array_equal(ds.read(1)[present], .5)


@pytest.mark.parametrize("phase", ("local_mosaic", "clip_region"))
def test_cancel_local_stage_keeps_complete_scenes_and_verified_blocks_then_resumes(
        client, provider, monkeypatch, tmp_path, phase):
    _, request = january_request(client, provider, tmp_path, count=2)
    original = monthly_scenes.download_mosaic
    cancelled = False

    def stop_before_publish(*args, **kwargs):
        nonlocal cancelled
        current = "clip_region" if isinstance(kwargs["reader"], monthly_scenes._MonthMosaicReader) else "local_mosaic"
        progress = kwargs["progress"]

        def observe(message, fraction=0, detail=None):
            nonlocal cancelled
            progress(message, fraction, detail)
            if current == phase and not cancelled and fraction == .94:
                cancelled = True
                kwargs["cancel"].set()
        return original(*args, **{**kwargs, "progress": observe})

    monkeypatch.setattr(monthly_scenes, "download_mosaic", stop_before_publish)
    first = client.post("monthly-download", request)["job_id"]
    job = client.wait(first, status="cancelled")
    month = job["result"]["months"][0]["result"]
    assert len(provider.process_calls) == 2 and month["files"] == []
    document = json.loads(Path(month["manifest"]).read_text(encoding="utf-8"))
    assert document["stages"][0]["status"] == "completed"
    assert document["stages"][1 if phase == "local_mosaic" else 2]["status"] == "cancelled"
    assert list(Path(month["folder"]).rglob("*.npz"))
    result = client.wait(client.post("retry-download", {"job_id": first})["job_id"])["result"]
    resumed = result["months"][0]["result"]
    assert len(provider.process_calls) == 2 and len(resumed["files"]) == 1
    stage_result = resumed["whole_mosaic"] if phase == "local_mosaic" else json.loads(
        Path(resumed["manifest"]).read_text(encoding="utf-8"))["clipped_result"]
    record = json.loads(Path(stage_result["manifest"]).read_text(encoding="utf-8"))
    assert record["items"][0]["reused_blocks"] > 0


def test_official_native_whole_month_keeps_partial_bands_and_clips_exactly(native_fixture, monkeypatch, tmp_path):
    from core import native_mosaic
    scene, options, _, _, auth, calls, _ = native_fixture
    options = {**options, "monthly_pipeline": monthly_scenes.PIPELINE}
    monkeypatch.setattr(monthly_scenes, "download_original", native_mosaic.download_original)
    result = monthly_scenes.download_monthly_scenes(AOI, [scene], options, str(tmp_path / "staged"), s3_auth=auth)
    assert len(calls) == 1 and len(calls[0]["targets"]) == 14
    assert len(result["files"]) == 1
    compare_complete_month_and_clip(result, AOI)
    with rasterio.open(result["files"][0]["bundle"]["unmasked"]) as raw:
        values = raw.read()
        assert np.any((values[0] < 0) & (values[0] != NODATA))
        assert np.any((values[4] == NODATA) & (values[0] != NODATA))


def test_native_checksum_failure_stops_before_local_mosaic(native_fixture, monkeypatch, tmp_path):
    from core import native_mosaic
    scene, options, paths, _, auth, _, _ = native_fixture
    options = {**options, "monthly_pipeline": monthly_scenes.PIPELINE}
    monkeypatch.setattr(monthly_scenes, "download_original", native_mosaic.download_original)
    paths["B02_10m"].write_bytes(paths["B02_10m"].read_bytes() + b"changed")
    out = tmp_path / "bad-stage"
    with pytest.raises(UserError, match="清单校验"):
        monthly_scenes.download_monthly_scenes(AOI, [scene], options, str(out), s3_auth=auth)
    document = json.loads(next(out.rglob("月度处理记录.json")).read_text(encoding="utf-8"))
    assert document["stages"][0]["status"] == "failed"
    assert all(stage["status"] == "pending" for stage in document["stages"][1:])
    assert not list(out.rglob("*.tif"))


def test_public_monthly_does_not_silently_use_interleaved_ranges_without_full_source_proof(
        client, pub_provider, tmp_path):
    aoi = client.post("aoi", {"coordinates": BBOX})
    result = client.wait(client.post("monthly-search", {
        "geometry": aoi["geometry"], "profile": "s2-earth-search", "start_month": "2024-06",
        "end_month": "2024-06", "max_cloud": 100})["job_id"])["result"]["monthly"]
    request = selection(aoi, result, tmp_path)
    request["profile"] = "s2-earth-search"
    request["months"][0]["options"] = {**OPTIONS, "mode": "custom", "bands": ["B04"]}
    response = client.post("monthly-plan", request, status=400)
    assert "完整 COG" in response["error"] and len(pub_provider.requests) == 1


def test_schema_one_frozen_month_resumes_legacy_ranges_without_complete_cog_descriptors(
        client, server, pub_provider, tmp_path):
    aoi = client.post("aoi", {"coordinates": BBOX})
    found = client.wait(client.post("monthly-search", {
        "geometry": aoi["geometry"], "profile": "s2-earth-search", "start_month": "2024-06",
        "end_month": "2024-06", "max_cloud": 100})["job_id"])["result"]["monthly"]
    group, = found["groups"]
    scenes = copy.deepcopy(group["scenes"])
    for scene in scenes:
        scene["public"].pop("native_raw", None)
    options = {**normalize_options({**OPTIONS, "mode": "custom", "bands": ["B04"]}),
               "profile": "s2-earth-search"}
    source_plan = server.service._plan_source("s2-earth-search", shape(aoi["geometry"]),
                                              options, scenes, group["criteria"])
    definition = {"title": "Frozen 1.7.1 monthly task", "profile": "s2-earth-search",
                  "geometry": aoi["geometry"], "boundary_name": "Legacy study area",
                  "output_dir": str(tmp_path / "legacy-months"),
                  "plan": {"kind": "monthly", "schema": 1, "fingerprint": "legacy-schema-one",
                           "folder_name": "legacy-schema-one", "requires": []},
                  "groups": [{"month": group["month"], "criteria": group["criteria"],
                              "scene_keys": [scene["key"] for scene in scenes], "scenes": scenes,
                              "options": options, "plan": source_plan, "status": "ready", "error": None}]}

    def resume(identifier):
        store = server.service.download_tasks
        store.create(identifier, "monthly-download", copy.deepcopy(definition))
        store.update({"id": identifier, "status": "cancelled", "message": "Controlled legacy interruption",
                      "progress": 0, "result": {}, "detail": {}}, force=True)
        return client.wait(client.post("retry-download", {"job_id": identifier})["job_id"])["result"]

    result = resume("a" * 32)
    assert result["status"] == "completed" and len(result["files"]) == 1
    document = json.loads(Path(result["manifest"]).read_text(encoding="utf-8"))
    assert document["schema"] == 1
    assert document["months"][0]["result"].get("pipeline") is None
    assert not list(Path(result["folder"]).rglob("月度处理记录.json"))
    with rasterio.open(result["files"][0]["path"]) as ds:
        values = ds.read(1)
        assert np.any(values != NODATA)
        np.testing.assert_allclose(values[values != NODATA], .1, rtol=0, atol=1e-6)
    requests = len(pub_provider.requests)
    repeated = resume("b" * 32)
    assert repeated["files"][0]["status"] == "skipped" and len(pub_provider.requests) == requests
