"""Scientific retention, honest pixel provenance and complete-bundle publication."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import numpy as np
import pytest
import rasterio

from conftest import BBOX, D1, P1, TRIANGLE
from core.errors import UserError
from core.geometry import parse_coordinates
from core.planning import RESEARCH_BANDS, build_plan, normalize_options
from core.process_request import process_payload
from core.public_catalogue import BAND_ASSET
from core.public_download import keys_for
from core.research_bundle import (bundle_paths, checked_bundle,
                                  make_block, process_block, transfer_options)
from core.scene_identity import expected_products
from test_public_workflow import asset, pub_provider, raster, search_public
from test_batch import batch_setup


def node_runtime():
    node = shutil.which("node")
    if not node:
        try:
            import playwright
            bundled = Path(playwright.__file__).parent / "driver" / ("node.exe" if os.name == "nt" else "node")
            node = str(bundled) if bundled.is_file() else None
        except ImportError:
            pass
    return node


def test_default_research_plan_accounts_for_all_outputs_and_transfer():
    options = normalize_options({})
    assert options["mode"] == "research" and options["bands"] == RESEARCH_BANDS and options["count"] == 10
    plan, _, _ = build_plan(parse_coordinates(BBOX), options)
    assert plan["bytes_per_scene"] == 44 * 57 * (10 * 4 * 2 + 1 + 2)
    assert plan["transfer_bands"] == transfer_options(options)["count"] == 12
    unmasked_options = normalize_options({"mask_clouds": False})
    assert "SCL" in keys_for(unmasked_options)
    assert build_plan(parse_coordinates(BBOX), unmasked_options)[0]["estimated_pu"] == plan["estimated_pu"]
    scene = {"key": "S2A_20240610T030531", "products": [{"id": P1, "acquisition_utc": D1}]}
    tile = build_plan(parse_coordinates(BBOX), options)[1][0]
    payload = process_payload(tile, plan, scene, unmasked_options)
    assert payload["input"]["data"][0]["processing"] == {"upsampling": "NEAREST", "downsampling": "NEAREST", "harmonizeValues": False}


def test_research_requests_bound_memory_without_dropping_grid_coverage():
    plan, tiles, _ = build_plan(parse_coordinates("116.20,39.80,116.70,40.20"), normalize_options({}))
    assert len(tiles) > 1 and max(max(tile.width, tile.height) for tile in tiles) <= 1024
    assert sum(tile.width * tile.height for tile in tiles) == plan["width"] * plan["height"]


def test_cloud_values_negative_reflectance_partial_bands_and_snow_are_retained():
    options = normalize_options({})
    raw = np.full((10, 1, 5), .23, dtype=np.float32)
    raw[0, 0, 0] = -.04  # Cloud-masked negative reflectance must survive in the backup.
    raw[9, 0, 2] = -9999  # Preserve the other nine bands at an incomplete footprint.
    scl = np.array([[9, 11, 4, 0, 4]], dtype=np.uint8)
    source = np.ones((1, 5), dtype=np.uint16)
    inside = np.array([[True, True, True, True, False]])
    block, counts = make_block(raw, scl, source, inside, options)
    assert counts == {"analysis": 1, "unmasked": 4, "scl": 4, "source": 4}
    assert block["unmasked"][0, 0, 0] == np.float32(-.04)
    assert np.all(block["analysis"][:, 0, 0] == -9999)
    assert np.all(block["analysis"][:, 0, 1] == np.float32(.23))  # SCL 11 is retained.
    assert np.all(block["unmasked"][:9, 0, 2] == np.float32(.23))
    assert block["scl"].tolist() == [[[9, 11, 4, 0, 255]]]
    assert block["source"].tolist() == [[[1, 1, 1, 1, 0]]]
    assert raw[0, 0, 4] == np.float32(.23) and scl[0, 4] == 4  # Inputs were not overwritten.


def test_actual_evalscript_uses_one_stable_source_before_cloud_masking():
    node = node_runtime()
    if not node:
        pytest.skip("A Node runtime is required for the actual Process evalscript")
    options = normalize_options({})
    area = parse_coordinates(BBOX)
    plan, tiles, _ = build_plan(area, options)
    other = P1.replace("T50TMK", "T50TNK")
    scene = {"key": "S2A_20240610T030531", "products": [{"id": other, "acquisition_utc": D1}, {"id": P1, "acquisition_utc": D1}]}
    products = expected_products(scene)
    payload = process_payload(tiles[0], plan, scene, options, products=products)
    js = r'''
const vm=require('node:vm'), assert=require('node:assert/strict'), fs=require('node:fs');
const {script,products,bands}=JSON.parse(fs.readFileSync(0,'utf8'));
const realm={}; vm.createContext(realm); vm.runInContext(script,realm);
const tile=p=>({dataPath:'s3://archive/'+p.id+'.SAFE',date:p.utc});
const collection={scenes:{tiles:[tile(products[1]),tile(products[0])]}};
const scenes=realm.preProcessScenes(collection).scenes;
assert.equal(scenes.tiles[0].dataPath,tile(products[0]).dataPath);
const sample=(v,scl,mask=1)=>Object.assign({SCL:scl,dataMask:mask},Object.fromEntries(bands.map(b=>[b,v])));
const cloudy=sample(-.02,9), clear=sample(.31,4);
let actual=Array.from(realm.evaluatePixel([cloudy,clear],scenes));
assert.equal(realm.setup().output.bands,12);
assert.deepEqual(actual,[...Array(10).fill(-.02),9,1]);
actual=Array.from(realm.evaluatePixel([sample(.7,4,0),clear],scenes));
assert.deepEqual(actual,[...Array(10).fill(.31),4,2]);
actual=Array.from(realm.evaluatePixel([sample(.7,0,0),sample(.7,0,0)],scenes));
assert.deepEqual(actual,[...Array(10).fill(-9999),0,0]);
assert.deepEqual(Array.from(realm.evaluatePixel([],scenes)),[...Array(10).fill(-9999),255,0]);
const output={}; realm.updateOutputMetadata(scenes,{},output);
assert.deepEqual(Array.from(output.userData.sources,s=>s.source_index),[1,2]);
'''
    subprocess.run([node, "-e", js], input=json.dumps({"script": payload["evalscript"], "products": products, "bands": options["bands"]}),
                   text=True, capture_output=True, timeout=15, check=True)


@pytest.mark.parametrize("problem", ["fractional_scl", "unrecorded_index", "missing_index", "nonfinite", "wrong_layout"])
def test_invalid_process_quality_or_source_is_rejected(problem):
    options = normalize_options({})
    products = [{"id": P1.removesuffix(".SAFE")}]
    metadata = {"sources": [{"catalogue_id": products[0]["id"], "source_index": 1}]}
    values = np.full((12, 2, 2), .25, dtype=np.float32)
    values[-2] = 4
    values[-1] = 1
    if problem == "fractional_scl":
        values[-2, 0, 0] = 4.5
    elif problem == "unrecorded_index":
        values[-1, 0, 0] = 2
    elif problem == "missing_index":
        metadata["sources"][0].pop("source_index")
    elif problem == "nonfinite":
        values[0, 0, 0] = np.nan
    elif problem == "wrong_layout":
        values = values[:-1]
    with pytest.raises(UserError):
        process_block(values, np.ones((2, 2), dtype=bool), options, products, metadata)


def research_request(client, tmp_path, *, clouds=True):
    req = client.search(TRIANGLE, cloud=20)
    return {**req, "options": {"mode": "research", "resolution": 10, "max_cloud": 20, "mask_clouds": clouds},
            "output_dir": str(tmp_path / "research")}


def assert_complete(file, manifest):
    target = Path(file["path"])
    paths = bundle_paths(target)
    assert file["bundle"] == {role: str(path) for role, path in paths.items() if role != "analysis"}
    receipt = json.loads(target.with_suffix(".starmap.json").read_text(encoding="utf-8"))
    plan = manifest["plan"]
    with rasterio.open(target) as main:
        assert main.descriptions == tuple(RESEARCH_BANDS) and main.count == 10
        assert json.loads(main.tags()["bands"]) == list(main.descriptions)
        grid = main.transform
        identity = main.tags()["source_fingerprint"]
        for role in ("unmasked", "scl", "source"):
            with rasterio.open(paths[role]) as companion:
                assert companion.crs == main.crs and companion.shape == main.shape and companion.transform == grid
                assert json.loads(companion.tags()["bands"]) == list(companion.descriptions)
        checked_bundle(target, receipt, plan, grid, identity)
    return paths, receipt


@pytest.mark.parametrize("cloud_class", [4, 9])
def test_authenticated_research_transfer_preserves_all_cloud_scenes_and_resume(client, provider, tmp_path, cloud_class):
    provider.research_scl = cloud_class
    req = research_request(client, tmp_path)
    result = client.wait(client.post("download", req)["job_id"])["result"]
    file, = result["files"]
    manifest = json.loads(Path(result["manifest"]).read_text(encoding="utf-8"))
    paths, receipt = assert_complete(file, manifest)
    assert receipt["unmasked_valid_pixels"] > 0
    assert (receipt["valid_pixels"] > 0) == (cloud_class == 4)
    with rasterio.open(paths["unmasked"]) as ds:
        np.testing.assert_allclose(ds.read(masked=True).compressed(), .25)
    with rasterio.open(paths["scl"]) as ds:
        assert set(ds.read(1).ravel()) == {cloud_class, 255}
    with rasterio.open(paths["source"]) as ds:
        assert set(ds.read(1).ravel()) == {0, 1}
    count = len(provider.process_calls)
    repeat = client.wait(client.post("download", req)["job_id"])["result"]
    assert repeat["files"][0]["status"] == "skipped" and repeat["files"][0]["bundle"] == file["bundle"]
    assert len(provider.process_calls) == count


@pytest.mark.parametrize("role,change", [("unmasked", "missing"), ("scl", "changed"), ("source", "missing"), ("sources", "changed")])
def test_incomplete_cached_bundle_is_never_reported_as_complete(client, provider, tmp_path, role, change):
    req = research_request(client, tmp_path)
    result = client.wait(client.post("download", req)["job_id"])["result"]
    path = Path(result["files"][0]["bundle"][role])
    if change == "missing":
        path.unlink()
    else:
        path.write_bytes(path.read_bytes() + b"changed")
    requests = len(provider.process_calls)
    failed = client.wait(client.post("download", req)["job_id"], "failed")
    assert "研究分析包" in failed["error"] and len(provider.process_calls) == requests
    assert "已完成" not in failed["message"]


def populate_research_public(provider):
    for index, band in enumerate(RESEARCH_BANDS):
        provider.features[0]["assets"][BAND_ASSET[band]] = asset(band)
        provider.content[band + ".tif"] = raster(500 + index * 100)


@pytest.mark.parametrize("scl", [0, 9, 11])
def test_public_research_retains_scl_zero_clouds_negative_values_and_pixel_sources(client, pub_provider, tmp_path, scl):
    populate_research_public(pub_provider)
    pub_provider.content["SCL.tif"] = raster(scl, "uint8")
    request = search_public(client)
    req = {**request, "options": {"mode": "research", "resolution": 10, "max_cloud": 20, "mask_clouds": True},
           "output_dir": str(tmp_path / "public-research")}
    plan = client.post("plan", req)
    assert plan["source_objects"] == 11
    result = client.wait(client.post("download", req)["job_id"])["result"]
    file, = result["files"]
    manifest = json.loads(Path(result["manifest"]).read_text(encoding="utf-8"))
    paths, receipt = assert_complete(file, manifest)
    assert receipt["unmasked_valid_pixels"] > 0
    assert (receipt["valid_pixels"] > 0) == (scl == 11)
    with rasterio.open(paths["scl"]) as ds:
        assert ds.nodata == 255 and set(ds.read(1).ravel()) == {scl, 255}
    with rasterio.open(paths["unmasked"]) as ds:
        assert float(ds.read(1, masked=True).min()) == pytest.approx(-.05, abs=1e-6)
        assert len(receipt["bundle"]["unmasked"]["valid_pixels_per_band"]) == 10
    source_data = json.loads(paths["sources"].read_text(encoding="utf-8"))
    assert source_data["sources"][0]["catalogue_id"] == P1 and source_data["sources"][0]["index"] == 1


def test_failed_companion_publication_rolls_back_only_own_outputs(client, provider, tmp_path, monkeypatch):
    import core.research_bundle as bundle_module
    original = bundle_module.publish_file
    calls = []

    def fail(partial, target):
        calls.append(target)
        if target.name.endswith(".source.tif"):
            raise OSError("Synthetic disk write failure")
        original(partial, target)

    monkeypatch.setattr(bundle_module, "publish_file", fail)
    req = research_request(client, tmp_path)
    job = client.wait(client.post("download", req)["job_id"], "failed")
    folder = Path(req["output_dir"])
    assert len(calls) == 3
    assert not list(folder.rglob("*.tif")) and not list(folder.rglob("*.starmap.json"))
    assert not list(folder.rglob("*.partial*")) and not list(folder.rglob("*.sources.json"))
    assert job["status"] == "failed"


def test_aoi_holes_do_not_publish_spectral_or_quality_values():
    options = normalize_options({"mask_clouds": False})
    inside = np.ones((3, 3), dtype=bool)
    inside[1, 1] = False
    blocks, _ = make_block(np.full((10, 3, 3), .1), np.full((3, 3), 4), np.ones((3, 3)), inside, options)
    assert np.all(blocks["analysis"][:, 1, 1] == -9999)
    assert np.all(blocks["unmasked"][:, 1, 1] == -9999)
    assert blocks["scl"][0, 1, 1] == 255 and blocks["source"][0, 1, 1] == 0


def test_gedi_reference_uses_analysis_image_from_research_bundle(client, batch_setup, tmp_path):
    from test_batch import prepare, search_batch, TOKEN
    from core.gedi_catalogue import GEDI
    req = search_batch(client, batch_setup, primary_options={"mode": "research", "resolution": 20, "max_cloud": 20, "mask_clouds": True})
    req, plan = prepare(client, req, tmp_path / "research-with-gedi")
    assert len(plan["reference_candidates"]) == 1
    client.post("earthdata-connect", {"method": "token", "token": TOKEN})
    result = client.wait(client.post("batch-download", req)["job_id"])["result"]
    manifest = json.loads(Path(result["manifest"]).read_text(encoding="utf-8"))
    reference = Path(manifest["primary_reference"])
    assert ".unmasked." not in reference.name and ".scl." not in reference.name and ".source." not in reference.name
    gedi = next(source for source in manifest["sources"] if source["profile"] == GEDI)
    metrics = Path(gedi["results"][0]["folder"]) / "gedi_metrics.tif"
    with rasterio.open(reference) as optical, rasterio.open(metrics) as observations:
        assert optical.count == 10 and optical.descriptions == tuple(RESEARCH_BANDS)
        assert observations.shape == optical.shape and observations.transform == optical.transform and observations.crs == optical.crs
