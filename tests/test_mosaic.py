"""Spatial and raster evidence for selected multi-date mosaics and exact product scope."""
from __future__ import annotations

import json
import re
import threading
from pathlib import Path

import numpy as np
import pytest
import rasterio
from pyproj import Transformer
from rasterio.features import geometry_mask
from rasterio.transform import from_origin
from shapely import contains_xy
from shapely.geometry import box, mapping, shape
from shapely.ops import unary_union

from core.errors import Cancelled, UserError
from core.mosaic import build_mosaic_plan, download_mosaic
from core.planning import normalize_options
from core.scene_identity import SAFE_ID
from core.spatial_selection import coverage_summary, footprint, polygon_area
from test_batch import DATES, KEY as GEDI_COLLECTION, TOKEN, batch_setup, prepare, search_batch
from conftest import D1, P1, P2, feature

AOI = box(116.350, 39.900, 116.355, 39.905)


def row(identifier, acquired, region=AOI, cloud=1, marker=1):
    parsed = SAFE_ID.fullmatch(identifier)
    return {"key": identifier, "overpass_key": parsed[1] + "_" + parsed[2], "title": identifier,
            "platform": parsed[1], "datetime": acquired, "date": acquired[:10], "time": acquired[11:19],
            "tiles": [parsed[3]], "tile_count": 1, "cloud_min": cloud, "cloud_max": cloud,
            "marker": marker, "products": [{"id": identifier, "acquisition_utc": acquired,
                                             "cloud": cloud, "footprint": mapping(region)}]}


def options(mode="research", **extra):
    return normalize_options({"mode": mode, "layout": "mosaic", "priority": "selection", "max_cloud": 100, **extra})


class FieldReader:
    """An affine geospatial field detects shifts and cross-band/source mixing."""
    def __init__(self, bands, split_x=None, gap_y=None, all_cloud=False, cancel=None):
        self.bands, self.split_x, self.gap_y, self.all_cloud, self.cancel = bands, split_x, gap_y, all_cloud, cancel
        self.calls = []

    def field(self, marker, x, y):
        return np.stack([np.full(np.broadcast_shapes(x.shape, y.shape), np.float32(marker * .1 + (i + 1) * .01))
                         + x.astype(np.float32) * np.float32(1e-7) + y.astype(np.float32) * np.float32(1e-8)
                         for i in range(len(self.bands))])

    def read(self, scene, tile, inside):
        self.calls.append((scene["key"], tile, int(inside.sum())))
        x = (tile.bounds[0] + (np.arange(tile.width) + .5) * 10)[None, :]
        y = (tile.bounds[3] - (np.arange(tile.height) + .5) * 10)[:, None]
        values = self.field(scene["marker"], x, y)
        scl = np.full((tile.height, tile.width), 9 if self.all_cloud else 4, dtype=np.uint8)
        if scene["marker"] == 1:
            if self.split_x is not None:
                scl[:, x[0] >= self.split_x] = 9
            if self.gap_y is not None:
                values[:, y[:, 0] < self.gap_y, :] = -9999
                scl[y[:, 0] < self.gap_y, :] = 0
        if self.cancel:
            self.cancel.set()
        return values, scl, {"sources": [{"catalogue_id": scene["key"].removesuffix(".SAFE"), "match": "safe_id"}]}

    def close(self):
        pass


def two_rows(region=AOI):
    return [row(P1, D1, region, marker=1), row(P2, "2024-06-05T03:16:50.631Z", region, cloud=5, marker=2)]


def test_real_ten_safe_footprints_keep_a_repeat_dates_unique_fringe_without_repeated_allocation():
    fixture = json.loads((Path(__file__).parent / "fixtures/optical/s2-2604-reference.json").read_text(encoding="utf-8"))
    scenes = [row(p["product_id"], p["acquisition_utc"], shape(p["footprint"]), p["cloud"]) for p in fixture["products"]]
    geometry = unary_union([footprint(s) for s in scenes])
    summary = coverage_summary(geometry, scenes, [s["key"] for s in scenes], "cloud")
    assert summary["selected_count"] == 10 and summary["tile_count"] == 9
    assert summary["dates"] == ["2026-04-08", "2026-04-11", "2026-04-18", "2026-04-20"]
    assert summary["coverage_percent"] == 100 and summary["uncovered_percent"] == 0
    assert summary["overlap_percent"] > 0
    assert len(summary["recommended_keys"]) == 10
    fringe = next(s["key"] for s in scenes if s["date"] == "2026-04-18")
    assert fringe in summary["recommended_keys"]
    contribution = next(b for b in summary["blocks"] if b["key"] == fringe)
    assert 0 < contribution["contribution_percent"] < 1
    without_fringe = coverage_summary(geometry, scenes, [s["key"] for s in scenes if s["key"] != fringe], "cloud")
    assert 0 < without_fringe["uncovered_percent"] < 1
    assigned = [shape(b["assigned"]) for b in summary["blocks"] if b["assigned"]]
    for index, region in enumerate(assigned):
        assert all(region.intersection(other).area < 1e-12 for other in assigned[index + 1:])
    assert polygon_area(geometry.symmetric_difference(unary_union(assigned))) < 1e-5
    for product in fixture["products"]:
        assert product["grids"]["10"]["epsg"] == 32650
        assert product["grids"]["10"]["width"] == product["grids"]["10"]["height"] == 10980
        assert product["quantification"] == 10000 and product["boa_offsets"]["B02"] == -1000
        assert 114 < min(x for x, _ in product["footprint"]["coordinates"][0]) < 119
        assert 38 < min(y for _, y in product["footprint"]["coordinates"][0]) < 42


def test_coverage_uses_union_gaps_holes_and_adjustable_priority():
    geometry = box(116, 39, 117, 40).difference(box(116.4, 39.4, 116.6, 39.6))
    scenes = two_rows(box(116, 39, 116.8, 40))
    scenes[0]["cloud_max"] = 20
    scenes[1]["cloud_max"] = 2
    summary = coverage_summary(geometry, scenes, [s["key"] for s in scenes], "cloud")
    assert summary["ordered_keys"][0] == P2
    assert 15 < summary["uncovered_percent"] < 25
    assert not shape(summary["covered"]).intersects(box(116.45, 39.45, 116.55, 39.55))
    assert shape(summary["uncovered"]).within(geometry)
    manual = coverage_summary(geometry, scenes, [P1, P2], "selection")
    assert manual["ordered_keys"] == [P1, P2]
    assert manual["blocks"][0]["contribution_percent"] > manual["blocks"][1]["contribution_percent"]
    assert len(manual["recommended_keys"]) == 1
    assert coverage_summary(geometry, scenes, [], "cloud")["uncovered_percent"] == 100


def test_mosaic_fills_cloud_and_nodata_with_selected_products_on_one_grid(tmp_path):
    geometry = AOI.difference(box(116.351, 39.901, 116.352, 39.902))
    scenes, opts = two_rows(), options()
    planned = build_mosaic_plan(geometry, scenes, opts, tile_size=16)
    plan = planned[0]
    split_x = plan["bounds"][0] + plan["width"] * 5
    gap_y = plan["bounds"][1] + plan["height"] * 3
    reader = FieldReader(opts["bands"], split_x, gap_y)
    result = download_mosaic(geometry, scenes, opts, str(tmp_path), reader=reader, planned=planned)
    assert len(result["files"]) == 1 and plan["scene_count"] == 1 and plan["source_count"] == 2
    file = result["files"][0]
    with rasterio.open(file["path"]) as analysis, rasterio.open(file["bundle"]["unmasked"]) as raw_ds, \
         rasterio.open(file["bundle"]["source"]) as source_ds, rasterio.open(file["bundle"]["scl"]) as scl_ds:
        assert analysis.count == raw_ds.count == 10
        assert analysis.descriptions == tuple(opts["bands"])
        assert all(ds.transform == analysis.transform and ds.crs == analysis.crs for ds in (raw_ds, source_ds, scl_ds))
        x = (plan["bounds"][0] + (np.arange(plan["width"]) + .5) * 10)[None, :]
        y = (plan["bounds"][3] - (np.arange(plan["height"]) + .5) * 10)[:, None]
        converter = __import__("pyproj").Transformer.from_crs(4326, plan["epsg"], always_xy=True)
        projected = __import__("shapely").transform(geometry, converter.transform, interleaved=False)
        inside = geometry_mask([mapping(projected)], (plan["height"], plan["width"]), analysis.transform, invert=True)
        expected_source = np.where((x >= split_x) | (y < gap_y), 2, 1).astype(np.uint16)
        expected_source[~inside] = 0
        np.testing.assert_array_equal(source_ds.read(1), expected_source)
        expected = np.where(expected_source[None] == 2, reader.field(2, x, y), reader.field(1, x, y))
        expected[:, ~inside] = -9999
        np.testing.assert_array_equal(analysis.read(), expected)
        np.testing.assert_array_equal(raw_ds.read(), expected)
        assert np.all(scl_ds.read(1)[inside] == 4)
        table = json.loads(Path(file["bundle"]["sources"]).read_text(encoding="utf-8"))
        assert table["sources"][0]["catalogue_id"] == P1.removesuffix(".SAFE")
        assert table["sources"][1]["acquisition_utc"].startswith("2024-06-05")
        assert sum(s["used_pixels"] for s in table["sources"]) == int(inside.sum())
    repeated = download_mosaic(geometry, scenes, opts, str(tmp_path), reader=reader, planned=planned)
    assert repeated["files"][0]["status"] == "skipped"


def test_mosaic_without_fallback_does_not_request_a_redundant_date(tmp_path):
    scenes, opts = two_rows(), options(fill_gaps=False)
    plan = build_mosaic_plan(AOI, scenes, opts)
    reader = FieldReader(opts["bands"], split_x=plan[0]["bounds"][0])
    result = download_mosaic(AOI, scenes, opts, str(tmp_path), reader=reader, planned=plan)
    assert {key for key, _, _ in reader.calls} == {P1}
    with rasterio.open(result["files"][0]["path"]) as ds:
        assert np.all(ds.read() == -9999)
    with rasterio.open(result["files"][0]["bundle"]["unmasked"]) as ds:
        assert np.any(ds.read() != -9999)


@pytest.mark.parametrize("mode", ["rgb", "rgbnir", "all", "ndvi", "custom"])
def test_other_mosaic_modes_keep_quality_and_pixel_provenance(tmp_path, mode):
    opts = options(mode, bands=["B02", "B08"])
    reader = FieldReader(opts["bands"])
    result = download_mosaic(AOI, two_rows(), opts, str(tmp_path), reader=reader)
    file = result["files"][0]
    assert set(file["bundle"]) == {"scl", "source", "sources"}
    with rasterio.open(file["path"]) as ds:
        assert ds.count == opts["count"] and ds.dtypes == (opts["dtype"],) * opts["count"]
        assert int(ds.tags()["valid_pixels"]) > 0
    damaged = Path(file["bundle"]["scl"])
    original = damaged.read_bytes()
    changed = bytearray(original); changed[len(changed)//2] ^= 1
    damaged.write_bytes(changed)
    with pytest.raises(UserError, match="完整性"):
        download_mosaic(AOI, two_rows(), opts, str(tmp_path), reader=reader)
    assert damaged.read_bytes() == changed


def test_mosaic_cancel_publishes_no_incomplete_bundle(tmp_path):
    cancelled = threading.Event()
    opts = options()
    reader = FieldReader(opts["bands"], cancel=cancelled)
    with pytest.raises(Cancelled):
        download_mosaic(AOI, two_rows(), opts, str(tmp_path), reader=reader, cancel=cancelled)
    assert not list(tmp_path.rglob("*.tif"))
    assert not list(tmp_path.rglob("*.partial*"))
    manifest = json.loads(next(tmp_path.rglob("下载记录_*.json")).read_text(encoding="utf-8"))
    assert manifest["status"] == "cancelled"


def test_pixel_centres_follow_original_geographic_aoi_and_official_footprints(tmp_path):
    fixture = json.loads((Path(__file__).parent / "fixtures/optical/s2-2604-reference.json").read_text(encoding="utf-8"))
    products = [p for p in fixture["products"] if p["tile"] in ("T50SMJ", "T50SNJ")
                and not p["acquisition_utc"].startswith("2026-04-18")]
    scenes = [row(p["product_id"], p["acquisition_utc"], shape(p["footprint"]), marker=index)
              for index, p in enumerate(products, 1)]
    geometry = box(117.060, 39.30, 117.075, 39.32)
    opts = options(fill_gaps=False)
    result = download_mosaic(geometry, scenes, opts, str(tmp_path), reader=FieldReader(opts["bands"]))
    with rasterio.open(result["files"][0]["path"]) as ds, rasterio.open(result["files"][0]["bundle"]["source"]) as provenance:
        x = np.broadcast_to((ds.transform.c + (np.arange(ds.width) + .5) * 10)[None], ds.shape)
        y = np.broadcast_to((ds.transform.f - (np.arange(ds.height) + .5) * 10)[:, None], ds.shape)
        lon, lat = Transformer.from_crs(ds.crs, 4326, always_xy=True).transform(x, y)
        inside = contains_xy(geometry, lon, lat)
        expected = np.zeros(ds.shape, dtype=np.uint16)
        for index, scene in enumerate(scenes, 1):
            expected[inside & contains_xy(footprint(scene), lon, lat) & (expected == 0)] = index
        np.testing.assert_array_equal(provenance.read(1), expected)
        assert np.all(ds.read()[:, ~inside] == -9999)


def test_mosaic_rejects_an_empty_process_response_without_matching_products(client, provider, tmp_path):
    request = client.search(cloud=100)
    provider.empty = provider.unmatched = True
    request.update(options={**request["options"], "layout": "mosaic"}, output_dir=str(tmp_path))
    failed = client.wait(client.post("download", request)["job_id"], status="failed")
    assert "所选产品" in failed["error"]
    assert not list(tmp_path.rglob("*.tif"))


def test_batch_mosaic_has_one_gedi_reference_and_companion_coverage_is_query_bound(client, batch_setup, tmp_path):
    request = search_batch(client, batch_setup)
    request["options"]["layout"] = "mosaic"
    client.post("earthdata-connect", {"token": TOKEN})
    request, plan = prepare(client, request, tmp_path / "联合拼接")
    assert plan["groups"][0]["plans"][0]["kind"] == "mosaic"
    assert len(plan["reference_candidates"]) == 1 and plan["reference_candidates"][0]["scene_key"] == "mosaic"
    result = client.wait(client.post("batch-download", request)["job_id"])["result"]
    assert result["status"] == "completed"
    image = Path(result["sources"][0]["results"][0]["files"][0]["path"])
    metrics = Path(result["sources"][1]["results"][0]["folder"]) / "gedi_metrics.tif"
    with rasterio.open(image) as main, rasterio.open(metrics) as gedi:
        assert main.transform == gedi.transform and main.shape == gedi.shape and main.crs == gedi.crs

    reverse = {"geometry": request["geometry"], "profile": "gedi-earthdata", "collection": GEDI_COLLECTION, **DATES,
               "shared_dates": DATES, "companions": [{"id": "s2-earth-search", "profile": "s2-earth-search",
                   "criteria": {"max_cloud": 20}, "options": {"mode": "research", "layout": "mosaic"}}]}
    batch = client.wait(client.post("batch-search", reverse)["job_id"])["result"]["batch"]
    group = batch["groups"][1]
    coverage = {"profile": group["profile"], "geometry": reverse["geometry"], "batch_id": batch["id"],
                "group_id": group["id"], "query_id": group["query"]["id"], "criteria": group["criteria"],
                "scene_keys": [s["key"] for s in group["scenes"]], "options": {"priority": "cloud"}}
    assert client.post("coverage", coverage)["selected_count"] == 1
    assert client.post("coverage", {**coverage, "scene_keys": []})["uncovered_percent"] == 100
    client.post("coverage", {**coverage, "criteria": None}, status=400)
    client.post("coverage", {**coverage, "criteria": {**DATES, "max_cloud": 99}}, status=400)
    client.post("coverage", {**coverage, "query_id": "stale"}, status=400)


def test_api_selects_one_tile_from_an_overpass_and_deletion_invalidates_everything(client, provider, server, tmp_path):
    other = P1.replace("T50TMK", "T50TML")
    provider.catalogue = [feature(P1, D1), feature(other, "2024-06-10T03:16:35Z")]
    request = client.search(cloud=20)
    assert set(request["scene_keys"]) == {P1, other}
    empty = client.post("coverage", {**request, "scene_keys": []})
    assert empty["selected_count"] == 0 and empty["uncovered_percent"] == 100
    selected = {**request, "scene_keys": [other], "options": {**request["options"], "layout": "mosaic"},
                "output_dir": str(tmp_path)}
    plan = client.post("plan", selected)
    assert plan["source_count"] == 1 and plan["product_keys"] == [other]
    client.post("download", {**selected, "plan_fingerprint": "stale"}, status=400)
    result = client.wait(client.post("download", selected)["job_id"])["result"]
    assert len(result["files"]) == 1
    for call in provider.process_calls:
        allowed = json.loads(re.search(r"^var allowed = (.*);$", call["json"]["evalscript"], re.M)[1])
        assert [p["id"] for p in allowed] == [other.removesuffix(".SAFE")]
    client.post("aoi", {"clear": True})
    boot = client.get("bootstrap").json()
    assert boot["aoi"] is None and boot["query"] is None and boot["scenes"] == [] and boot["batch"] is None
    client.post("coverage", selected, status=400)
    client.post("download", selected, status=400)
    assert server.service._query_geometry is None
