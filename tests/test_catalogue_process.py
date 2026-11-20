from __future__ import annotations

import copy
import json
import shutil
import subprocess
from types import SimpleNamespace

import pytest
import requests
from shapely.geometry import box

import core.process_catalogue as catalogue
from core.errors import Cancelled, UserError
from core.geometry import parse_coordinates
from core.http import HttpClient, SH_CATALOGUE_URL, retry_seconds
from core.planning import build_plan, normalize_options
from core.process_request import _read_archive, fetch_tile, process_payload
from core.scene_identity import expected_products
from conftest import BBOX, D1, P1, P2, StubResponse, archive_bytes, feature


class StubAuth:
    connected = True

    def __init__(self):
        self.refreshed = []

    def token(self, cancel=None, force=False):
        self.refreshed.append(force)
        return "token"


def test_catalogue_paginates_inclusive_utc_and_groups(monkeypatch):
    area = parse_coordinates(BBOX)
    old = feature(P1.replace("N0510", "N0509"))
    current = feature(acquired="2024-06-10T11:16:50.553+08:00")
    another_tile = feature(P1.replace("T50TMK", "T50TNK"), acquired="2024-06-10T03:17:01Z", cloud=18)
    same_date_cloudy = feature(P2, acquired="2024-06-10T02:00:00Z", cloud=99)
    no_area = feature(P1.replace("T50TMK", "T50TLK"), footprint=box(116.355, 39.900, 116.360, 39.905))
    missing = feature(P1.replace("T50TMK", "T50TNF"), cloud=None)
    next_day = feature(P2, acquired="2024-06-11T00:00:00Z", cloud=2)
    calls = []
    responses = [StubResponse(401), StubResponse(data={"features": [old, same_date_cloudy, missing, None], "context": {"next": 4}}),
                 StubResponse(data={"features": [current, another_tile, no_area, next_day], "context": {}})]

    def request(_, method, url, *, json, **kwargs):
        calls.append(copy.deepcopy(json))
        return responses[len(calls)-1]

    monkeypatch.setattr(HttpClient, "request", request)
    auth = StubAuth()
    scenes = catalogue.find_scenes(area, "2024-06-10", "2024-06-10", 20, auth)
    assert len(scenes) == 1
    result = scenes[0]
    assert result["key"] == "S2A_20240610T030531"
    assert result["acquisition_start"].startswith("2024-06-10T03:16:50.553")
    assert result["acquisition_end"] == "2024-06-10T03:17:01Z"
    assert result["tiles"] == ["T50TMK", "T50TNK"] and result["cloud_max"] == 18 and result["coverage_percent"] == 100
    assert [p["id"] for p in result["products"]] == [P1, another_tile["id"]]
    assert auth.refreshed == [False, True, False]
    assert calls[0]["datetime"] == "2024-06-10T00:00:00Z/2024-06-11T00:00:00Z"
    assert "next" not in calls[0] and calls[2]["next"] == 4
    assert all(r.closed for r in responses)


def test_catalogue_rejects_repeated_cursor(monkeypatch):
    def repeat(*_, **__):
        return StubResponse(data={"features": [], "context": {"next": 12}})
    monkeypatch.setattr(HttpClient, "request", repeat)
    with pytest.raises(UserError, match="分页未推进"):
        catalogue.find_scenes(parse_coordinates(BBOX), "2024-06-10", "2024-06-10", 100, StubAuth())


@pytest.mark.parametrize("headers, status, sentinel, expected", [
    ({"Retry-After": "3398"}, 429, True, 3.398), ({"Retry-After": "7"}, 429, False, 7),
    ({"Retry-After": "2"}, 503, True, 2), ({"X-RateLimit-Retry-After": "2500"}, 429, True, 2.5),
    ({"Retry-After": "NaN"}, 429, True, 4),
])
def test_retry_units(headers, status, sentinel, expected):
    assert retry_seconds(StubResponse(status, headers=headers), 1, sentinel_hub=sentinel) == expected


def test_http_retry_wait_is_cancellable(monkeypatch):
    waits = []
    event = SimpleNamespace(is_set=lambda: False, wait=lambda delay: waits.append(delay) or True)
    http = HttpClient()
    failed = StubResponse(429, headers={"Retry-After": "3600"})
    monkeypatch.setattr(http.session, "request", lambda *_, **__: failed)
    with pytest.raises(Cancelled):
        http.request("POST", SH_CATALOGUE_URL, cancel=event)
    assert waits == [3.6] and failed.closed
    http.close()


def _payload(options=None):
    area = parse_coordinates(BBOX)
    options = normalize_options(options or {})
    plan, tiles, _ = build_plan(area, options)
    scene = {"key": "S2A_20240610T030531", "datetime": D1, "products": [{"id": P1, "acquisition_utc": D1}]}
    return process_payload(tiles[0], plan, scene, options), expected_products(scene), options


def test_process_uses_granule_instead_of_datatake_and_preserves_categorical_mask():
    payload, _, _ = _payload()
    data = payload["input"]["data"][0]
    assert data["dataFilter"]["timeRange"] == {"from": "2024-06-10T03:16:20.553000Z", "to": "2024-06-10T03:17:20.553000Z"}
    assert data["processing"] == {"upsampling": "NEAREST", "downsampling": "NEAREST"}
    payload, _, _ = _payload({"mask_clouds": False})
    assert payload["input"]["data"][0]["processing"]["upsampling"] == "BILINEAR"
    assert [entry["identifier"] for entry in payload["output"]["responses"]] == ["default", "userdata"]


def test_evalscript_rejects_other_granules_and_ambiguous_source_paths():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is required to execute the actual Process evalscript")
    payload, _, _ = _payload({"mode": "ndvi", "mask_clouds": True})
    script = r'''
const fs = require('node:fs'), vm = require('node:vm'), assert = require('node:assert/strict');
const {script, product, utc} = JSON.parse(fs.readFileSync(0,'utf8'));
const realm = {};
vm.createContext(realm);
vm.runInContext(script, realm);
const tile = (path,date=utc)=>({dataPath:path,date});
const exact=tile('s3://archive/2024/'+product+'.SAFE');
const other=tile('s3://archive/2024/'+product.replace('N0510','N0509')+'.SAFE');
const candidate=tile('s3://sentinel-s2-l2a/tiles/50/T/MK/2024/6/10/0','2024-06-10T03:16:50Z');
const duplicate=tile('s3://sentinel-s2-l2a/tiles/50/T/MK/2024/6/10/1');
const wrongTile=tile('s3://sentinel-s2-l2a/tiles/50/T/ML/2024/6/10/0');
const wrongTime=tile(candidate.dataPath,'2024-06-10T03:16:51Z');
function test(tiles, paths, matches) {
    const scenes = realm.preProcessScenes({scenes:{tiles}}).scenes;
    const output={};
    realm.updateOutputMetadata(scenes, {}, output);
    assert.deepEqual(Array.from(scenes.tiles,t=>t.dataPath),paths);
    assert.deepEqual(Array.from(output.userData.sources,p=>p.match),matches);
    assert.equal(output.userData.candidates,tiles.length);
}
test([other,candidate,wrongTile,wrongTime,exact],[exact.dataPath],['safe_id']);
test([other,candidate,wrongTile,wrongTime],[candidate.dataPath],['mgrs_and_utc']);
test([candidate,duplicate,other,wrongTile],[],[]);
assert.equal(realm.setup().mosaicking,'TILE');
assert.equal(realm.setup().output.nodataValue,-9999);
assert.equal(realm.evaluatePixel([{dataMask:1,SCL:9,B08:.9,B04:.1}])[0],-9999);
const ndvi=realm.evaluatePixel([{dataMask:0,SCL:4,B04:.1,B08:.4},{dataMask:1,SCL:4,B04:.1,B08:.4}])[0];
assert.ok(Math.abs(ndvi-.6)<1e-12);
'''
    subprocess.run([node, "-e", script], input=json.dumps({"script": payload["evalscript"], "product": P1.removesuffix(".SAFE"), "utc": D1}),
                   check=True, text=True, capture_output=True, timeout=15)


@pytest.mark.parametrize("member", ["../default.tif", "default.tif"])
def test_archive_with_unexpected_member_or_mismatched_source_is_rejected(tmp_path, member):
    _, products, _ = _payload()
    tiff = tmp_path / "block.tif"
    bundle = tmp_path / "remote.part"
    item = {"catalogue_id": products[0]["id"], "match": "mgrs_and_utc", "date": D1, "data_path": "s3://example/tiles/50/T/ML/2024/6/10/0"}
    info = json.dumps({"candidates": 1, "sources": [item]}).encode()
    bundle.write_bytes(archive_bytes((member, b"stub file"), ("userdata.json", info)))
    with pytest.raises(UserError, match="未返回可核验"):
        _read_archive(bundle, tiff, 5_000, products)


def test_process_transfer_retries_clears_archive_and_only_refreshes_on_401(tmp_path):
    class Broken(StubResponse):
        def iter_content(self, size):
            yield b"incomplete"
            raise requests.exceptions.ChunkedEncodingError

    payload, products, options = _payload()
    target = tmp_path / "block.tif"
    responses = [StubResponse(401), Broken(), StubResponse(content=archive_bytes(
        ("default.tif", b"test tiff data"), ("userdata.json", b'{"candidates":0,"sources":[]}')))]
    pending = iter(responses)
    auth = StubAuth()
    received = fetch_tile(SimpleNamespace(request=lambda *_, **__: next(pending)), auth, payload, target, options=options, products=products)
    assert received["sources"] == [] and auth.refreshed == [False, True, False, False]
    assert target.read_bytes() == b"test tiff data"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["block.tif"] and all(r.closed for r in responses)
