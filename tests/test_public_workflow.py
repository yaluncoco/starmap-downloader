"""Public source selection, reprojection, completeness checks and real HTTP byte ranges."""
from __future__ import annotations

import datetime as dt
import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import numpy as np
import pytest
import rasterio
from rasterio.io import MemoryFile
from rasterio.transform import from_origin
from shapely.geometry import box, mapping

from conftest import P1, TRIANGLE, StubResponse
from core.errors import UserError
from core.http import HttpClient, check_cancel
from core.public_catalogue import EARTH_SEARCH, ES_COLLECTION, ES_HOST, _asset_for, _earth_item, ES_ITEM, search_earth
from core.range_file import RangeFile

ITEM = "S2A_T50TMK_20240610T031035_L2A"
ACQUIRED = "2024-06-10T03:16:50.553000Z"
TRANSFORM = from_origin(443000, 4418000, 10, 10)
SIDE = 512
START, STOP = dt.date(2024, 6, 10), dt.date(2024, 6, 11)
CRITERIA = {"start": "2024-06-10", "end": "2024-06-10", "max_cloud": 20}


def asset(band, dtype="uint16"):
    return {"href": f"https://{ES_HOST}/{ES_COLLECTION}/50/T/MK/2024/6/{ITEM}/{band}.tif", "proj:shape": [SIDE, SIDE],
            "proj:transform": list(TRANSFORM[:6]), "raster:bands": [{"nodata": 0, "data_type": dtype,
                                                                     "scale": 1 if dtype == "uint8" else .0001,
                                                                     "offset": 0 if dtype == "uint8" else -.1}]}


def feature():
    return {"collection": ES_COLLECTION, "properties": {"s2:product_uri": P1, "datetime": ACQUIRED, "eo:cloud_cover": 12, "proj:epsg": 32650},
            "id": ITEM, "geometry": mapping(box(116, 39.5, 116.9, 40.5)),
            "assets": {"red": asset("B04"), "scl": asset("SCL", "uint8"), "nir": asset("B08")}}


def raster(value, dtype="uint16", crs=32650, count=1):
    with MemoryFile() as temp:
        with temp.open(driver="GTiff", width=SIDE, height=SIDE, count=count, dtype=dtype, nodata=0,
                       crs=f"EPSG:{crs}", transform=TRANSFORM, tiled=True, blockxsize=128, blockysize=128) as ds:
            ds.write(np.full((count, SIDE, SIDE), value, dtype=dtype))
        return temp.read()


class PublicProvider:
    def __init__(self):
        self.requests = []
        self.content = {"B04.tif": raster(2000), "B08.tif": raster(3000), "SCL.tif": raster(4, "uint8")}
        self.features = [feature()]
        self.bad = None

    def request(self, method, url, *, cancel=None, **kwargs):
        check_cancel(cancel)
        self.requests.append((method, url, kwargs))
        headers = kwargs.get("headers", {})
        assert "Authorization" not in headers
        if url == EARTH_SEARCH:
            assert method == "POST" and kwargs["json"]["collections"] == [ES_COLLECTION]
            return StubResponse(data={"features": self.features, "links": []})
        name = url.rsplit("/", 1)[1]
        data = self.content[name]
        start, end = map(int, re.fullmatch(r"bytes=(\d+)-(\d+)", headers["Range"]).groups())
        if start and self.bad == "etag":
            tag = '"other-object"'
        else:
            tag = '"e84-v1"'
        if self.bad == "whole":
            return StubResponse(200, content=data)
        end = min(end, len(data)-1)
        chunk = data[start:end+1]
        return StubResponse(206, content=chunk, headers={"Content-Range": f"bytes {start}-{end}/{len(data)}", "ETag": tag,
                                                         "Content-Length": str(len(chunk))})


@pytest.fixture
def pub_provider(monkeypatch):
    p = PublicProvider()
    monkeypatch.setattr(HttpClient, "request", lambda _self, *args, **kwargs: p.request(*args, **kwargs))
    return p


def search_public(client):
    area = client.post("aoi", {"coordinates": TRIANGLE})
    search = {"profile": "s2-earth-search", "geometry": area["geometry"]}
    job = client.wait(client.post("search", {**search, **CRITERIA})["job_id"])
    assert [s["key"] for s in job["result"]["scenes"]] == [ITEM]
    return {**search, "query_id": job["result"]["query"]["id"], "scene_keys": [ITEM], "criteria": CRITERIA,
            "options": {"mode": "custom", "bands": ["B04"], "resolution": 10, "max_cloud": 20, "mask_clouds": True}}


def test_public_download_exact_reflectance_and_no_auth(client, pub_provider, tmp_path):
    req = {**search_public(client), "output_dir": str(tmp_path / "public")}
    p = client.post("plan", req)
    assert p["kind"] == "public" and p["source_objects"] == 2 and p["estimated_pu"] is None
    assert p["epsg"] == 32650
    client.post("plan", {**req, "criteria": {**CRITERIA, "max_cloud": 100}}, status=400)
    result = client.wait(client.post("download", req)["job_id"])["result"]
    output = Path(result["files"][0]["path"])
    with rasterio.open(output) as ds:
        assert ds.tags()["catalogue_product_id"] == P1
        data = ds.read(1, masked=True)
        assert ds.res == (10, 10) and ds.descriptions == ("B04",) and ds.crs.to_epsg() == 32650
        assert 0 < data.count() < data.size  # triangle clipped
        np.testing.assert_allclose(data.compressed(), .1, rtol=0, atol=2e-7)
    manifest = json.loads(Path(result["manifest"]).read_text(encoding="utf-8"))
    assert manifest["items"][0]["assets"]["B04"]["offset"] == -.1
    assert manifest["items"][0]["transport"]["B04"]["range_requests"] > 0
    previous_requests = len(pub_provider.requests)
    repeat = client.wait(client.post("download", req)["job_id"])["result"]
    assert repeat["files"][0]["status"] == "skipped"
    assert len(pub_provider.requests) == previous_requests
    output.write_bytes(output.read_bytes() + b"changed")
    assert "完整性" in client.wait(client.post("download", req)["job_id"], "failed")["error"]


@pytest.mark.parametrize("case", ["wrong_crs", "wrong_band_count", "html", "whole", "etag", "empty", "cloud"])
def test_public_bad_data_does_not_publish(client, pub_provider, tmp_path, case, monkeypatch):
    if case == "wrong_crs":
        pub_provider.content["B04.tif"] = raster(2000, crs=4326)
    elif case == "wrong_band_count":
        pub_provider.content["B04.tif"] = raster(2000, count=2)
    elif case == "html":
        pub_provider.content["B04.tif"] = b"<html>Not a TIFF</html>"
    elif case == "empty":
        pub_provider.content["B04.tif"] = raster(0)
    elif case == "cloud":
        pub_provider.content["SCL.tif"] = raster(9, "uint8")
    else:
        pub_provider.bad = case
    if case == "etag":
        monkeypatch.setattr("core.public_download.RangeFile", lambda *args: RangeFile(*args, page_size=4096))
    req = {**search_public(client), "output_dir": str(tmp_path / case)}
    status = "completed" if case in ("empty", "cloud") else "failed"
    job = client.wait(client.post("download", req)["job_id"], status)
    if status == "completed":
        assert job["result"]["files"] == [] and job["result"]["empty_scenes"] == [ITEM]
    assert not list((tmp_path / case).rglob("*.tif"))


def test_wrong_asset_identity_unplanned(client, pub_provider):
    pub_provider.features[0]["assets"]["red"]["href"] = asset("B08")["href"]
    req = search_public(client)
    assert "B04" in client.post("plan", req, status=400)["error"]
    assert len(pub_provider.requests) == 1


def test_earth_catalogue_rejects_wrong_day_and_accepts_unpadded_zone():
    f = feature()
    assert _earth_item(f, box(116, 39, 116.5, 40), 1000, dt.date(2024,6,9), START, 20) is None
    local = asset("B04")
    other = ITEM.replace("T50TMK", "T01UDT")
    local["href"] = local["href"].replace("/50/T/MK/", "/1/U/DT/").replace(ITEM, other)
    assert _asset_for(other, local, "B04", ES_ITEM.fullmatch(other), 32601)


def test_real_http_range_socket_resume_and_bounded_cache():
    content = bytes(range(256)) * 6
    requests = []
    class RangeServer(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            start, end = map(int, re.fullmatch(r"bytes=(\d+)-(\d+)", self.headers.get("Range", "")).groups())
            requests.append((start, self.headers.get("If-Match")))
            end = min(end, len(content)-1)
            self.send_response(206)
            self.send_header("ETag", '"socket-v1"')
            self.send_header("Content-Range", f"bytes {start}-{end}/{len(content)}")
            self.send_header("Content-Length", str(end-start+1))
            self.end_headers()
            # First TCP response closes mid-range; second must ask for the same exact version.
            payload = content[start:end+1]
            self.wfile.write(payload[:20] if len(requests) == 1 else payload)

    server = ThreadingHTTPServer(("127.0.0.1", 0), RangeServer)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    http = HttpClient()
    try:
        with RangeFile(http, f"http://127.0.0.1:{server.server_port}/range", page_size=128, cache_pages=2) as f:
            assert f.read(273) == content[:273]
            assert len(f.cache) == 2
            assert requests[:2] == [(0, None), (0, '"socket-v1"')]
            f.seek(-64, 2)
            assert f.read(128) == content[-64:]
    finally:
        http.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
