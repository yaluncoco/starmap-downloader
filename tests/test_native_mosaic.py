"""Native XML corrections, manifest integrity and per-band NoData/BOA conversion."""
from __future__ import annotations

import copy
import hashlib
import math
import xml.etree.ElementTree as ET
from pathlib import Path

import numpy as np
import pytest
import rasterio
from rasterio.transform import from_origin
from shapely.geometry import mapping, shape

from conftest import D1, P1
from core.errors import UserError
from core.mosaic import build_mosaic_plan
from core.native_mosaic import (PROFILE, NativeReader, native_assets, native_metadata,
                                native_source_plan, search_native)
from core.planning import RESEARCH_BANDS, normalize_options
from core.s3_source import CDSES3Auth
from core.scene_identity import utc_time
from test_mosaic import AOI, row


@pytest.fixture
def native_fixture(tmp_path, monkeypatch):
    from core import native_mosaic

    opts = normalize_options({"mode": "research", "layout": "mosaic", "max_cloud": 100})
    opts["profile"] = PROFILE
    scene = row(P1, D1)
    original = {**copy.deepcopy(scene), "key": P1.removesuffix(".SAFE"), "title": P1.removesuffix(".SAFE")}
    original["raw"] = {"names": [P1], "assets": [], "collection": "sentinel-2-l2a", "available": True}
    scene["native"] = {"scene": original, "available": True}
    grid = build_mosaic_plan(AOI, [row(P1, D1)], {**opts, "profile": "s2-process"})[0]
    origin = (math.floor(grid["bounds"][0] / 20) * 20, math.ceil(grid["bounds"][3] / 20) * 20)
    paths, inputs = {}, {}
    for index, band in enumerate(RESEARCH_BANDS + ["SCL"]):
        from core.native_mosaic import NATIVE_RESOLUTION
        resolution = NATIVE_RESOLUTION[band]
        side = 1280 // resolution
        y, x = np.indices((side, side))
        dn = (np.full((side, side), 4, dtype=np.uint8) if band == "SCL" else
              (2000 + index * 10 + x + y).astype(np.uint16))
        if band == "B02":
            dn[8:12, 8:12] = 800  # Negative BOA reflectance survives the unmasked output.
        if band == "B05":
            dn[5:7, 5:7] = 0  # Only this spectral band is missing.
        if band == "SCL":
            dn[12:14, 12:14] = 9
            dn[15, 15] = 0  # True SCL class zero is distinct from outside-data 255.
        alias = f"{band}_{resolution}m"
        path = tmp_path / f"T50TMK_20240610T030531_{alias}.jp2"
        with rasterio.open(path, "w", driver="GTiff", width=side, height=side, count=1, dtype=dn.dtype,
                           crs="EPSG:32650", transform=from_origin(*origin, resolution, resolution)) as ds:
            ds.write(dn, 1)
        paths[alias], inputs[band] = path, (dn, resolution)
    product = ET.Element("Level-2A_User_Product")
    for name, value in {"PRODUCT_URI": P1, "PROCESSING_BASELINE": "05.10", "BOA_QUANTIFICATION_VALUE": "10000",
                        "Cloud_Coverage_Assessment": "0.040455",
                        "EXT_POS_LIST": " ".join(str(n) for x, y in mapping(AOI)["coordinates"][0] for n in (y, x))}.items():
        ET.SubElement(product, name).text = value
    for index, band in enumerate(RESEARCH_BANDS):
        ET.SubElement(product, "Spectral_Information", bandId=str(index), physicalBand=band)
        ET.SubElement(product, "BOA_ADD_OFFSET", band_id=str(index)).text = "-1000"
    tile = ET.Element("Level-2A_Tile_ID")
    ET.SubElement(tile, "TILE_ID").text = "L2A_T50TMK_A001_20240610T030531"
    ET.SubElement(tile, "SENSING_TIME").text = D1
    ET.SubElement(tile, "HORIZONTAL_CS_CODE").text = "EPSG:32650"
    geocoding = ET.SubElement(tile, "Tile_Geocoding")
    for resolution in (10, 20, 60):
        size = ET.SubElement(geocoding, "Size", resolution=str(resolution))
        for label in ("NCOLS", "NROWS"):
            ET.SubElement(size, label).text = str(1280 // resolution)
        position = ET.SubElement(geocoding, "Geoposition", resolution=str(resolution))
        for label, value in {"ULX": origin[0], "ULY": origin[1], "XDIM": resolution, "YDIM": -resolution}.items():
            ET.SubElement(position, label).text = str(value)
    for role, root, name in (("product_metadata", product, "MTD_MSIL2A.xml"), ("granule_metadata", tile, "MTD_TL.xml")):
        paths[role] = tmp_path / name
        ET.ElementTree(root).write(paths[role], encoding="utf-8")
    manifest = ET.Element("XFDU")
    for index, path in enumerate(paths.values()):
        obj = ET.SubElement(manifest, "dataObject", ID=str(index))
        stream = ET.SubElement(obj, "byteStream", size=str(path.stat().st_size))
        ET.SubElement(stream, "fileLocation", href="./" + path.name)
        ET.SubElement(stream, "checksum", checksumName="SHA3-256").text = hashlib.sha3_256(path.read_bytes()).hexdigest()
    paths["safe_manifest"] = tmp_path / "manifest.safe"
    ET.ElementTree(manifest).write(paths["safe_manifest"], encoding="utf-8")
    for alias in native_assets(opts):
        path = paths[alias]
        original["raw"]["assets"].append({"id": alias, "name": path.name, "bytes": path.stat().st_size,
            "roles": ["metadata" if alias.endswith("metadata") or alias == "safe_manifest" else "data"],
            "s3_url": f"https://eodata.dataspace.copernicus.eu/eodata/Sentinel-2/{P1}/{path.name}"})
    calls = []

    def download(geometry, plan, output, auth, **kwargs):
        calls.append(plan)
        return {"files": [{"path": str(paths[target["asset"]]), "status": "completed"} for target in plan["targets"]],
                "manifest": str(tmp_path / "native-download.json")}

    monkeypatch.setattr(native_mosaic, "download_original", download)
    auth = CDSES3Auth()
    auth.connect("test-key-123", "test-secret-value-123456")
    return scene, opts, paths, inputs, auth, calls, origin


def test_native_reader_preserves_individual_band_gaps_clouds_and_negative_reflectance(native_fixture, tmp_path):
    from core.native_mosaic import download_native_mosaic

    scene, opts, paths, inputs, auth, calls, origin = native_fixture
    result = download_native_mosaic(AOI, [scene], opts, str(tmp_path / "native-mosaic"), auth)
    file, = result["files"]
    assert len(calls) == 1 and len(calls[0]["targets"]) == 14
    with rasterio.open(file["path"]) as main, rasterio.open(file["bundle"]["unmasked"]) as raw, \
         rasterio.open(file["bundle"]["scl"]) as scl, rasterio.open(file["bundle"]["source"]) as source:
        x = np.broadcast_to((main.transform.c + (np.arange(main.width) + .5) * 10)[None], main.shape)
        y = np.broadcast_to((main.transform.f - (np.arange(main.height) + .5) * 10)[:, None], main.shape)
        observed_source = source.read(1)
        expected = []
        for band in RESEARCH_BANDS:
            dn, resolution = inputs[band]
            col = np.floor((x - origin[0]) / resolution).astype(int)
            row = np.floor((origin[1] - y) / resolution).astype(int)
            values = dn[row, col]
            reflectance = (values.astype(np.float32) - np.float32(1000)) / np.float32(10000)
            reflectance[(values == 0) | (observed_source == 0)] = -9999
            expected.append(reflectance)
        expected = np.stack(expected)
        np.testing.assert_array_equal(raw.read(), expected)
        assert np.any((expected[0] < 0) & (expected[0] != -9999))
        assert np.any((expected[4] == -9999) & (expected[0] != -9999))
        masked = expected.copy()
        valid = (expected != -9999).all(axis=0) & np.isin(scl.read(1), (2, 4, 5, 6, 7, 11))
        masked[:, ~valid] = -9999
        np.testing.assert_array_equal(main.read(), masked)
        assert np.any(scl.read(1) == 0) and np.any(scl.read(1) == 9)


def test_native_manifest_detects_a_changed_source_member(native_fixture):
    scene, opts, paths, _, _, _, _ = native_fixture
    plan = native_source_plan(scene, opts)
    metadata = native_metadata(paths, plan["targets"], scene)
    assert len(metadata["manifest_checked_members"]) == 13
    path = paths["B02_10m"]
    data = bytearray(path.read_bytes()); data[-1] ^= 1
    path.write_bytes(data)
    with pytest.raises(UserError, match="SAFE 清单"):
        native_metadata(paths, plan["targets"], scene)


@pytest.mark.parametrize("field", ["time", "footprint"])
def test_native_metadata_rejects_changed_catalogue_identity(native_fixture, field):
    from shapely.affinity import translate

    scene, opts, paths, _, _, _, _ = native_fixture
    plan = native_source_plan(scene, opts)
    scene = copy.deepcopy(scene)
    if field == "time":
        scene["products"][0]["acquisition_utc"] = "2024-06-10T03:10:00Z"
    else:
        scene["products"][0]["footprint"] = mapping(translate(AOI, xoff=.001))
    with pytest.raises(UserError, match="产品标识"):
        native_metadata(paths, plan["targets"], scene)


def test_native_search_corrects_trimmed_catalogue_footprints_times_and_rounded_cloud(native_fixture, monkeypatch, tmp_path):
    from core import native_mosaic
    from shapely.geometry import box

    scene, opts, _, _, auth, calls, _ = native_fixture
    original = copy.deepcopy(scene["native"]["scene"])
    wrong = box(116.40, 39.90, 116.405, 39.905)
    original["products"][0]["footprint"] = mapping(wrong)
    original.update(datetime="2024-06-10T03:05:31Z", time="03:05:31", cloud_min=.04, cloud_max=.04)

    def search(geometry, *args, **kwargs):
        assert geometry.intersects(wrong) and not AOI.intersects(wrong)
        return [original]

    monkeypatch.setattr(native_mosaic, "search_original", search)
    found, = search_native(AOI, "2024-06-10", "2024-06-10", 100, auth, tmp_path)
    assert shape(found["products"][0]["footprint"]).equals(AOI)
    assert utc_time(found["datetime"]) == utc_time(D1) and found["cloud_max"] == .040455
    assert found["footprint_basis"] == "official product XML"
    assert len(calls[0]["targets"]) == 2
