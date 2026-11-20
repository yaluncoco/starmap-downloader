"""Sentinel-2 whole-tile routes: strict cross-provider identity and atomic validation."""
import datetime as dt
import hashlib
import json
import threading
from pathlib import Path

import numpy as np
import pytest
import rasterio
from rasterio.shutil import copy as rio_copy
from rasterio.transform import from_origin
from shapely.geometry import box

from conftest import StubResponse
from core import open_l2a, open_l3
from core.errors import Cancelled, UserError
from core.geometry import area_km2
from core.http import HttpClient, check_cancel
from core.native_catalogue import STAC_ROOT
from core.public_catalogue import EARTH_SEARCH
from core.raw_download import _check_raw_raster, _stream
from core.s3_source import is_public_url, l3_mirror_url

FIXTURE = Path(__file__).parent / "fixtures" / "optical"
SVALBARD = "15.60,78.22,15.67,78.25"
BEIJING = "116.350,39.900,116.355,39.905"
SVAL_AREA, BJ_AREA = [box(*map(float, b.split(","))) for b in (SVALBARD, BEIJING)]
DAY = dt.date(2024, 6, 5)
L3_DAY = dt.date(2024, 6, 10)
DAY_STEP = dt.timedelta(days=1)
L3_KEY = "Sentinel-2_mosaic_2024_Q2_50TMK_0_0"
L3_URL = l3_mirror_url(L3_KEY, "B04")
TAG = '"' + "1" * 32 + '-3"'
OTHER_TAG = '"' + "a" * 32 + '-3"'
STAC_TAG = '"' + "2" * 32 + '"'


def read(name):
    return json.loads((FIXTURE / name).read_bytes())


def l2a(feature):
    return open_l2a.l2a_row(feature, SVAL_AREA, area_km2(SVAL_AREA), DAY, DAY+DAY_STEP, 100)


def l3(feature, start=L3_DAY, stop=L3_DAY+DAY_STEP):
    return open_l3.official_row(feature, BJ_AREA, open_l3.COLLECTION, start, stop)


def response(content=b"", tag=TAG, status=200, *, size=None, mime="image/tiff"):
    return StubResponse(status, content=content, headers={"Content-Length": str(len(content) if size is None else size),
                                                          "ETag": tag, "Content-Type": mime})


@pytest.mark.parametrize("bad", ("host", "another-tile", "injected-query", "year", "sha-missing", "sha-other-algorithm",
                                  "size", "origin", "scale", "rgb-declared-as-single"))
def test_l2a_rejects_unbound_member_without_replacing_it(bad):
    f = read("s2-l2a-item.json")
    red = f["assets"]["red"]
    result_alias = "B04"
    if bad == "host":
        red["href"] = red["href"].replace("amazonaws.com", "example.test")
    elif bad == "another-tile":
        red["href"] = red["href"].replace("T33XWG", "T33XXG")
    elif bad == "injected-query":
        red["href"] += "?versionId=other"
    elif bad == "year":
        red["href"] = red["href"].replace("/2024/", "/2025/")
    elif bad == "sha-missing":
        del red["file:checksum"]
    elif bad == "sha-other-algorithm":
        red["file:checksum"] = "1620" + "0" * 64
    elif bad == "size":
        red["file:size"] = True
    elif bad == "origin":
        red["proj:transform"][2] += 10
    elif bad == "scale":
        red["raster:bands"][0]["scale"] = .01
    elif bad == "rgb-declared-as-single":
        f["assets"]["visual"]["raster:bands"] = f["assets"]["visual"]["raster:bands"][:1]
        result_alias = "TCI"
    if bad == "origin":
        assert l2a(f) is None
    else:
        row = l2a(f)
        assert len(row["raw"]["assets"]) == 20 and result_alias not in {a["id"] for a in row["raw"]["assets"]}


@pytest.mark.parametrize("field", ("via", "index", "datastrip", "datatake", "granule", "safe"))
def test_l2a_requires_catalogue_cross_fields_and_exact_index(field):
    f = read("s2-l2a-item.json")
    if field == "via":
        f["links"][0]["href"] = f["links"][0]["href"].replace("/33/", "/34/")
    elif field == "index":
        del f["assets"]["tileinfo_metadata"]["file:checksum"]
    else:
        p = {"datastrip":"s2:datastrip_id", "datatake":"s2:datatake_id", "granule":"s2:tile_id", "safe":"s2:product_uri"}[field]
        f["properties"][p] = f["properties"][p].replace("S2B", "S2A")
    assert l2a(f) is None


def l2a_request(client, output, assets):
    criteria = {"start": str(DAY), "end": str(DAY), "max_cloud": 100}
    base = {"profile": open_l2a.S2_L2A_COG, "geometry": client.post("aoi", {"coordinates": SVALBARD})["geometry"]}
    d = client.wait(client.post("search", {**base, **criteria})["job_id"])["result"]
    return {**base, "criteria": criteria, "query_id": d["query"]["id"], "scene_keys": [d["scenes"][0]["key"]],
            "output_dir": str(output), "options": {"mode": "assets", "transport": "public", "assets": assets}}


@pytest.fixture
def l2a_archive(monkeypatch):
    f = read("s2-l2a-item.json")
    index = (FIXTURE / "s2-l2a-tileInfo.json").read_bytes()
    assert hashlib.sha256(index).hexdigest() == f["assets"]["tileinfo_metadata"]["file:checksum"][4:]
    props = f["properties"]
    xml = (f'<Level-2A_User_Product><General_Info><Product_Info><PRODUCT_URI>{props["s2:product_uri"]}</PRODUCT_URI>'
           f'<PROCESSING_BASELINE>05.10</PROCESSING_BASELINE><Datatake datatakeIdentifier="{props["s2:datatake_id"]}">'
           '<SPACECRAFT_NAME>Sentinel-2B</SPACECRAFT_NAME><DATATAKE_SENSING_START>2024-06-05T13:07:09.024Z</DATATAKE_SENSING_START>'
           '</Datatake></Product_Info></General_Info></Level-2A_User_Product>').encode()
    obj = f["assets"]["product_metadata"]
    obj.update({"file:size": len(xml), "file:checksum": "1220"+hashlib.sha256(xml).hexdigest()})
    index_obj = f["assets"]["tileinfo_metadata"]
    state = {"xml": xml, "index": index, "feature": f, "stale": False, "calls": [], "cancel_on_xml": False}
    def request(self, method, url, *, cancel=None, **kwargs):
        check_cancel(cancel)
        assert "auth" not in kwargs and "Authorization" not in kwargs.get("headers", {})
        state["calls"].append((method, url, kwargs))
        if url == EARTH_SEARCH:
            assert kwargs["json"]["collections"] == [open_l2a.ES_COLLECTION]
            return StubResponse(data={"features": [f]})
        if url == index_obj["href"]:
            if method == "HEAD":
                assert kwargs["headers"]["If-Match"] == STAC_TAG
                return response(tag=STAC_TAG, status=412 if state["stale"] else 200, size=len(state["index"]))
            return response(state["index"], STAC_TAG, mime="application/json")
        assert url == obj["href"] and method == "GET"
        r = response(state["xml"], mime="application/xml")
        if state["cancel_on_xml"]:
            cancel.set()
        return r
    monkeypatch.setattr(HttpClient, "request", request)
    return state


def assert_not_published(folder, status):
    assert not list(folder.rglob("*.part")) and not list(folder.rglob("*.starmap.json"))
    assert not list(folder.rglob("*.tif")) and not list(folder.rglob("*.xml"))
    manifest, = folder.rglob("下载记录_*.json")
    data = json.loads(manifest.read_bytes())
    assert data["status"] == status and data["items"][0]["status"] == status


@pytest.mark.parametrize("failure,hint", [("stale", "改版"), ("wrong-index-bytes", "索引 SHA-256"), ("wrong-index-path", "索引的 SAFE"),
                                         ("wrong-sha", "SHA-256"), ("wrong-xml", "L2A XML"), ("cancelled", None)])
def test_l2a_never_publishes_bad_or_cancelled_file(client, l2a_archive, tmp_path, failure, hint):
    f = l2a_archive["feature"]
    if failure == "stale":
        l2a_archive["stale"] = True
    if failure in ("wrong-index-bytes", "wrong-index-path"):
        payload = l2a_archive["index"].replace(b'/6/5/0', b'/6/5/1')
        assert payload != l2a_archive["index"] and len(payload) == len(l2a_archive["index"])
        l2a_archive["index"] = payload
        if failure == "wrong-index-path":
            f["assets"]["tileinfo_metadata"]["file:checksum"] = "1220"+hashlib.sha256(payload).hexdigest()
    if failure in ("wrong-sha", "wrong-xml"):
        payload = l2a_archive["xml"].replace(b"05.10", b"05.11")
        assert len(payload) == len(l2a_archive["xml"]) and payload != l2a_archive["xml"]
        l2a_archive["xml"] = payload
        if failure == "wrong-xml":
            f["assets"]["product_metadata"]["file:checksum"] = "1220"+hashlib.sha256(payload).hexdigest()
    if failure == "cancelled":
        l2a_archive["cancel_on_xml"] = True
    out = tmp_path / "输出"
    req = l2a_request(client, out, ["product_metadata.xml"])
    status = "cancelled" if failure == "cancelled" else "failed"
    snap = client.wait(client.post("download", req)["job_id"], status=status)
    if hint:
        assert hint in snap["error"]
    assert_not_published(out, status)


def test_l2a_use_source_sha_even_if_local_receipt_is_also_modified(client, l2a_archive, tmp_path):
    req = l2a_request(client, tmp_path / "输出", ["product_metadata.xml"])
    plan = client.post("plan", req)
    assert plan["requires"] == [] and plan["requests"] == 1
    target, = plan["targets"]
    assert open_l2a.check_l2a_target(target)["tile"] == "33XWG"
    for bad in ({**target,"name":"metadata.xml"}, {**target,"provider_sha256":None}, {**target,"bytes":True}):
        with pytest.raises(UserError, match="L2A"):
            open_l2a.check_l2a_target(bad)
    result = client.wait(client.post("download", req)["job_id"])["result"]
    file, = result["files"]
    assert file["status"] == "completed"
    path = Path(file["path"])
    assert path.read_bytes() == l2a_archive["xml"]
    calls = len(l2a_archive["calls"])
    repeat = client.wait(client.post("download", req)["job_id"])["result"]
    assert repeat["files"][0]["status"] == "skipped" and len(l2a_archive["calls"]) == calls
    receipt = path.with_suffix(".starmap.json")
    sidecar = json.loads(receipt.read_bytes())
    path.write_bytes(b"0" + path.read_bytes()[1:])
    sidecar["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    receipt.write_text(json.dumps(sidecar), encoding="utf-8")
    bad = client.wait(client.post("download", req)["job_id"], status="failed")
    assert "完整性校验失败" in bad["error"] and path.read_bytes().startswith(b"0")


def test_full_rgb_blocks_and_mismatched_raster_contract(tmp_path):
    base = tmp_path / "base.tif"
    cog = tmp_path / "visual.tif"
    t = from_origin(499980, 8700000, 10, 10)
    with rasterio.open(base, 'w', driver='GTiff', width=64, height=32, count=3, dtype='uint8', crs='EPSG:32633',
                       transform=t, nodata=0, photometric="RGB") as ds:
        ds.write(np.broadcast_to(np.array([51, 101, 201], dtype=np.uint8)[:, None, None], (3, 32, 64)))
    rio_copy(base, cog, driver="COG", BLOCKSIZE=128, COMPRESS="DEFLATE")
    raster = dict(epsg=32633, shape=[32,64], transform=list(t)[:6], dtype="uint8", nodata=0, scale=1, offset=0, layout="COG", count=3)
    _check_raw_raster(cog, None, raster)
    for key, val in (("count",1), ("epsg",32634), ("nodata",255), ("scale",.001), ("offset",-.1), ("transform",[10,0,500000,0,-10,8700000]), ("layout","Broken")):
        with pytest.raises(UserError, match="网格"):
            _check_raw_raster(cog, None, {**raster,key:val})
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(Cancelled):
        _check_raw_raster(cog, cancel, raster)
    # Corrupt the compressed interleaved block without changing the IFD or file size.
    import tifffile
    with tifffile.TiffFile(cog) as tf:
        page = tf.pages[0]
        offset, count = page.dataoffsets[0], page.databytecounts[0]
    with cog.open("r+b") as fd:
        fd.seek(offset)
        fd.write(b"\xff" * count)
    with pytest.raises(UserError, match="解码"):
        _check_raw_raster(cog, None, raster)


@pytest.mark.parametrize("start,end,expected", [(dt.date(2024,4,1),dt.date(2024,4,2),True),
    (dt.date(2024,6,30),dt.date(2024,7,1),True), (dt.date(2024,3,31),dt.date(2024,4,1),False),
    (dt.date(2024,7,1),dt.date(2024,7,2),False), (L3_DAY,L3_DAY+DAY_STEP,True)])
def test_l3_quarter_intersection_uses_inclusive_utc_days(start, end, expected):
    row = l3(read("s2-l3-cdse-item.json"), start, end)
    assert bool(row) == expected
    if row:
        assert row["date"] == "2024 Q2" and row["cloud_max"] is None
        assert row["period"] == {"from":"2024-04-01T00:00:00Z", "until_exclusive":"2024-07-01T00:00:00Z"}


@pytest.mark.parametrize("malformed", ("tile", "period", "metadata-host", "official-band", "mirror-band", "mirror-hash", "mirror-bbox", "file-size", "geo-grid", "missing"))
def test_l3_refuses_wrong_archive_and_inconsistent_members(malformed):
    official, mirror = [read(f"s2-l3-{src}-item.json") for src in ("cdse", "mirror")]
    if malformed == "tile":
        mirror["id"] += "0"
    if malformed == "period":
        mirror["properties"]["start_datetime"] = "2024-04-02T00:00:00Z"
    if malformed == "metadata-host":
        next(link for link in mirror["links"] if link["rel"] == "derived_from")["href"] += "?v=1"
    if malformed == "official-band":
        official["assets"]["B04"]["href"] = official["assets"]["B04"]["href"].replace("50TMK", "50TML")
    if malformed == "mirror-band":
        mirror["assets"]["B04"]["href"] = mirror["assets"]["B04"]["href"].replace("50TMK", "50TML")
    if malformed == "mirror-hash":
        mirror["assets"]["B04"]["file:checksum"] = "1220" + "3" * 64
        official["assets"]["B04"]["file:checksum"] = "1220" + "4" * 64
    if malformed == "mirror-bbox":
        mirror["bbox"][0] += .1
    if malformed == "file-size":
        mirror["assets"]["B04"]["file:size"] += 1
    if malformed == "geo-grid":
        mirror["assets"]["B04"]["proj:transform"][2] += 1
    def request(method, url, **kwargs):
        assert method == "GET" and url == l3_mirror_url(L3_KEY)
        return response(json.dumps(mirror).encode(), STAC_TAG, status=404 if malformed=="missing" else 200, mime="application/json")
    from types import SimpleNamespace
    row = open_l3.resolve_mirror(l3(official), SimpleNamespace(request=request), None)
    raw = row["raw"]
    assert "_official" not in row and "B04" not in {b["id"] for b in raw["assets"]}
    if malformed in ("missing", "tile", "period", "metadata-host", "mirror-bbox"):
        assert not raw["available"] and raw["unavailable_reason"]
    else:
        assert raw["available"] and len(raw["assets"]) == 3


def test_l3_url_allowlist_is_product_and_band_specific():
    assert is_public_url(L3_URL)
    for bad in (L3_URL+'#a', L3_URL+'?q=2', L3_URL.replace('/Q2/','/Q9/'), L3_URL.replace('B04.tif','observations.tif'),
                L3_URL.replace('/tge-labs/', '/unrelated/'), L3_URL.replace('B04', '%424'), l3_mirror_url(L3_KEY)):
        assert not is_public_url(bad)


@pytest.fixture
def l3_archive(monkeypatch, tmp_path):
    official, mirror = [read(f"s2-l3-{src}-item.json") for src in ("cdse", "mirror")]
    # Small byte count but the real full-size COG contract. Sparse, constant fixture with a known DN in a valid tile.
    src, dest = tmp_path / "sparse.tif", tmp_path / "sparse-cog.tif"
    matrix = np.full((512, 512), 1000, dtype="int16")
    with rasterio.open(src, 'w', driver='GTiff', width=10008, height=10008, dtype='int16', count=1,
                       nodata=-32768, crs='EPSG:32650', transform=from_origin(399960, 4500000, 10, 10),
                       tiled=True, blockxsize=512, blockysize=512, SPARSE_OK=True) as ds:
        ds.write(matrix, 1, window=rasterio.windows.Window(0, 0, 512, 512))
    rio_copy(src, dest, driver="COG", BLOCKSIZE=512, COMPRESS="DEFLATE", SPARSE_OK=True, OVERVIEWS="NONE")
    tiff = dest.read_bytes()
    for catalogue in (official, mirror):
        catalogue["assets"]["B04"]["file:size"] = len(tiff)
    doc = json.dumps(mirror).encode()
    state = {"calls": [], "fault": None, "content": tiff, "official": official}
    def request(self, method, url, *, cancel=None, **kwargs):
        check_cancel(cancel)
        headers = kwargs.get("headers", {})
        assert "auth" not in kwargs and "Authorization" not in headers
        state["calls"].append((method,url,headers))
        fault = state["fault"]
        if url == STAC_ROOT + "/search":
            assert kwargs["json"]["collections"] == [open_l3.COLLECTION] and method == "POST"
            return StubResponse(data={"features": [official]})
        if url == l3_mirror_url(L3_KEY):
            if method == "GET":
                return response(doc, STAC_TAG, status=404 if fault=="missing-index" else 200, mime="application/json")
            assert headers["If-Match"] == STAC_TAG
            return response(tag=STAC_TAG,size=len(doc),status=412 if fault=="stale-index" else 200,mime="application/json")
        assert url == L3_URL
        if method == "HEAD":
            if fault == "missing-band":
                return response(status=404)
            if "If-Match" in headers:
                assert headers["If-Match"] == TAG
            return response(size=len(tiff),tag=OTHER_TAG if fault=="changed-on-reuse" else TAG)
        assert method == "GET" and headers["If-Match"] == TAG
        return response(state["content"],tag=OTHER_TAG if fault=="changed-get" else TAG,
                        size=len(tiff)+1 if fault=="bad-size" else len(tiff))
    monkeypatch.setattr(HttpClient, "request", request)
    return state


def l3_request(client, out):
    criteria = {"start": str(L3_DAY), "end": str(L3_DAY)}
    base = {"profile": open_l3.S2_L3_PUBLIC, "geometry": client.post("aoi", {"coordinates": BEIJING})["geometry"]}
    d = client.wait(client.post("search", {**base, **criteria})["job_id"])["result"]
    return d, {**base, "criteria": criteria, "query_id": d["query"]["id"], "scene_keys": [L3_KEY],
               "output_dir": str(out), "options": {"mode": "assets", "transport": "public", "assets": ["B04"]}}


@pytest.mark.parametrize("fault,hint", (("stale-index", "版本已变化"), ("missing-band", "镜像缺少"),
                                         ("changed-get", "对象版本"), ("bad-size", "字节长度")))
def test_l3_transfer_does_not_fallback_or_publish_bad_object(client, l3_archive, tmp_path, fault, hint):
    out = tmp_path / "输出"
    _, req = l3_request(client, out)
    l3_archive["fault"] = fault
    job = client.wait(client.post("download", req)["job_id"], status="failed")
    assert hint in job["error"]
    assert_not_published(out, "failed")


def test_l3_success_uses_etag_as_version_not_hash_and_reuse_checks_source(client, l3_archive, tmp_path):
    search, req = l3_request(client, tmp_path / "输出")
    scene, = search["scenes"]
    assert scene["raw"]["available"] and {b["id"] for b in scene["raw"]["assets"]} == {"B02","B03","B04","B08"}
    for mode in ("original", "metadata"):
        client.post("plan", {**req,"options":{"mode":mode}}, status=400)
    plan = client.post("plan", req)
    assert not plan["requires"] and plan["targets"][0]["identity"]["reflectance_scale"] == .0001
    result = client.wait(client.post("download", req)["job_id"])["result"]
    file, = result["files"]
    item, = json.loads(Path(result["manifest"]).read_bytes())["items"]
    assert file["status"] == item["status"] == "completed" and item["provider_checksum_verified"] is None and item["etag"] == TAG
    assert item["sha256"] == hashlib.sha256(l3_archive["content"]).hexdigest()
    with rasterio.open(file["path"]) as ds:
        assert ds.scales == (1.,) and int(ds.read(1, window=rasterio.windows.Window(2,2,1,1))[0,0]) * item["reflectance_scale"] == .1
    result = client.wait(client.post("download", req)["job_id"])["result"]
    assert result["files"][0]["status"] == "skipped"
    l3_archive["fault"] = "changed-on-reuse"
    rejected = client.wait(client.post("download", req)["job_id"], status="failed")
    assert "版本已变化" in rejected["error"] and Path(file["path"]).read_bytes() == l3_archive["content"]


def test_l3_catalogue_only_scene_has_no_download_plan(client, l3_archive, tmp_path):
    l3_archive["fault"] = "missing-index"
    result, req = l3_request(client, tmp_path)
    row, = result["scenes"]
    assert not row["raw"]["available"] and "缺少该季度瓦片" in row["raw"]["unavailable_reason"]
    client.post("plan", req, status=400)
    client.post("download", req, status=400)
    assert not any(url == L3_URL for _,url,_ in l3_archive["calls"])


@pytest.mark.parametrize("fault", (None, "range", "version"))
def test_l3_pinned_retry_only_appends_the_exact_range(client, l3_archive, tmp_path, fault):
    import requests
    from types import SimpleNamespace
    blob = l3_archive["content"]
    cut = 800
    _, req = l3_request(client, tmp_path / "output")
    target, = client.post("plan", req)["targets"]
    first = response(blob, size=len(blob))
    def interrupted(_chunk):
        yield blob[:cut]
        raise requests.exceptions.ChunkedEncodingError("synthetic interruption")
    first.iter_content = interrupted
    later = response(blob[cut:], tag=OTHER_TAG if fault=="version" else TAG, status=206)
    later.headers["Content-Range"] = f"bytes {cut+(fault=='range')}-{len(blob)-1}/{len(blob)}"
    calls = []
    def request(method, url, **kwargs):
        assert method == "GET" and url == L3_URL and kwargs["headers"]["If-Match"] == TAG
        calls.append(kwargs["headers"].copy())
        return first if len(calls)==1 else later
    partial = tmp_path / "partial.part"
    args = (SimpleNamespace(request=request), None, target, partial, None, lambda *_: None)
    if fault:
        with pytest.raises(UserError, match="区间|长度|版本"):
            _stream(*args, pinned_etag=TAG)
        assert partial.read_bytes() == blob[:cut]
    else:
        result = _stream(*args, pinned_etag=TAG)
        assert result["transfer_attempts"] == 2 and result["size"] == len(blob) and result["etag"] == TAG
        assert result["aws_single_part_md5"] is None and partial.read_bytes() == blob
    assert len(calls) == 2 and calls[0].get("Range") is None and calls[1]["Range"] == f"bytes={cut}-"
    assert first.closed and later.closed
