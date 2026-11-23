"""Full municipality extents, GIS round trips and fail-closed offline boundaries."""
from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest
from shapely.geometry import Point, shape

from core.cities import DATA, CityCatalogue
from core.errors import UserError
from core.geometry import describe_aoi, load_aoi, save_aoi


def test_municipalities_include_remote_counties_and_preserve_all_vertices(tmp_path):
    catalogue = CityCatalogue()
    beijing, chongqing = catalogue.get("110000"), catalogue.get("500000")
    assert 16000 < beijing["area_km2"] < 17000 and beijing["vertices"] > 14000
    assert 80000 < chongqing["area_km2"] < 84000 and chongqing["vertices"] > 24000
    geometry = shape(beijing["geometry"])
    assert geometry.contains(Point(116.40, 39.91))
    assert geometry.contains(Point(115.98, 40.47))  # Yanqing, outside the built-up city.
    assert geometry.contains(Point(117.05, 40.53))  # Miyun's outer administrative area.
    assert shape(chongqing["geometry"]).contains(Point(109.63, 31.40))  # Wuxi, far from central Chongqing.
    assert beijing["region"]["municipality"] and "直辖市" in beijing["region"]["scope"]
    assert beijing["region"]["source"]["source_crs"] == "GCJ-02"
    target = Path(save_aoi(geometry, tmp_path / "北京市完整区域.shp", "北京市"))
    assert all(target.with_suffix(ext).is_file() for ext in (".shp", ".shx", ".dbf", ".prj", ".cpg"))
    restored = load_aoi(target)
    assert restored.equals(geometry)
    assert describe_aoi(restored)["vertices"] == beijing["vertices"]
    assert abs(describe_aoi(restored)["area_km2"] - beijing["area_km2"]) < .0001


def test_city_catalogue_covers_all_entries_and_labels_actual_administrative_scope():
    catalogue = CityCatalogue()
    public = catalogue.public()
    assert len(public["cities"]) == len(catalogue.rows) == 371
    assert all("member" not in row and "sha256" not in row for row in public["cities"])
    for row in catalogue.rows.values():
        geometry = shape(catalogue.get(row["id"])["geometry"])
        assert geometry.is_valid and not geometry.is_empty
        assert list(geometry.bounds) == row["bounds"]
    assert "省级" in catalogue.get("710000")["region"]["scope"]
    assert "特别行政区" in catalogue.get("810000")["region"]["scope"]
    assert catalogue.search("北京")[0]["id"] == "110000"


def test_city_member_digest_and_missing_catalogue_do_not_silently_replace_geometry(tmp_path):
    root = tmp_path / "cities"
    root.mkdir()
    (root / "cities-index.json").write_bytes((DATA / "cities-index.json").read_bytes())
    with zipfile.ZipFile(root / "cities.zip", "w") as archive:
        archive.writestr("110000.geojson", json.dumps({"type": "Polygon", "coordinates": [[[116, 39], [117, 39], [117, 40], [116, 39]]]}))
    with pytest.raises(UserError, match="校验失败"):
        CityCatalogue(root).get("110000")
    missing = CityCatalogue(tmp_path / "missing")
    assert missing.search("北京") == []
    with pytest.raises(UserError, match="目录缺失"):
        missing.public()
    with pytest.raises(UserError, match="目录缺失"):
        missing.get("110000")


def test_city_service_and_search_use_same_full_boundary(client, server):
    region = client.post("aoi", {"city_id": "110000"})
    assert region["vertices"] > 14000
    assert client.get("cities").json()["cities"]
    found = client.post("geocode", {"query": "北京市"})["places"]
    assert found[0]["city_id"] == "110000"
    query = client.post("search", {"city_id": "110000", "profile": "s2-earth-search", "start": "2024-06-01", "end": "2024-06-30"})
    # This fixture deliberately has no public upstream; the region must still persist in full.
    client.wait(query["job_id"], status="failed")
    stored = server.service.last_aoi
    assert stored["region"] == region["region"]
    assert shape(stored["geometry"]).equals(shape(region["geometry"]))
