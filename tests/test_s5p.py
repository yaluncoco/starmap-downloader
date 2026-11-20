"""Sentinel-5P original: swath, catalogue checksum and payload proof are separate."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import threading
from pathlib import Path

import h5py
import numpy as np
import pytest
from shapely.geometry import box, mapping

from conftest import AppClient, BBOX, StubResponse, serve
from core.errors import Cancelled, UserError
from core.http import HttpClient
from core.native_catalogue import STAC_ROOT
from core.open_s5p import ARCHIVE, COLLECTIONS, S5P_PUBLIC, archive_objects, s5p_row, verify_swath
from core.s3_source import S5P_ARCHIVE_HOST, is_public_url

AOI = box(116.350, 39.900, 116.355, 39.905)
DAY = dt.date(2024, 6, 10)
STOP = DAY + dt.timedelta(days=1)
PRODUCT = "S5P_OFFL_L2__CH4____20240610T041611_20240610T055741_34501_03_020600_20240614T023809"
NAME = PRODUCT + ".nc"
COLLECTION = "sentinel-5p-l2-ch4-offl"
VARIABLE = "methane_mixing_ratio"
FIRST = "2024-06-10T04:37:45Z"
LAST = "2024-06-10T05:36:09Z"
CDSE_PATH = "s3://eodata/Sentinel-5P/TROPOMI/L2__CH4___/2024/06/10/" + NAME
PREFIX = "OFFL/L2__CH4___/2024/06/10/"
SOURCE = ARCHIVE + PREFIX + NAME
ETAG = '"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa-2"'


def archive_xml(body="", *, prefix=PREFIX, next_token=""):
    token = f"<NextContinuationToken>{next_token}</NextContinuationToken>" if next_token else ""
    truncated = "true" if next_token else "false"
    return (f'<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">'
            f'<Name>meeo-s5p</Name><Prefix>{prefix}</Prefix><IsTruncated>{truncated}</IsTruncated>{token}{body}'
            f'</ListBucketResult>').encode()


def archive_node(length, tag=ETAG):
    return f"<Contents><Key>{PREFIX}{NAME}</Key><Size>{length}</Size><ETag>{tag}</ETag></Contents>"


def feature(data=b"fixture"):
    return {"id": PRODUCT, "collection": COLLECTION, "geometry": mapping(AOI),
            "properties": {"platform": "sentinel-5p", "sat:absolute_orbit": 34501, "product:type": "L2__CH4___",
                           "datetime": FIRST, "start_datetime": FIRST, "end_datetime": LAST, "proj:shape": [2, 3]},
            "assets": {"Product": {"href": CDSE_PATH, "type": "application/netcdf", "roles": ["data"], "file:local_path": NAME,
                                    "file:size": len(data), "file:checksum": "d50110" + hashlib.md5(data, usedforsecurity=False).hexdigest()}}}


def row(f):
    return s5p_row(f, AOI, COLLECTION, DAY, STOP)


def local_nc(path, change=""):
    dims = (1, 2, 3)
    with h5py.File(path, "w") as f:
        f.attrs.update({"id": (PRODUCT if change != "identity" else PRODUCT[:-1] + "0").encode(),
                        "orbit": [34501 if change != "orbit" else 34502],
                        "time_coverage_start": (FIRST if change != "time" else "2024-06-09T04:37:45Z").encode(),
                        "time_coverage_end": LAST.encode()})
        g = f.create_group("PRODUCT")
        g["latitude"] = np.full(dims, 39.9 if change != "geocodes" else 999, dtype="f4")
        if change == "linked":
            g["longitude"] = h5py.ExternalLink(str(path) + ".elsewhere", "/data")
        else:
            g["longitude"] = np.full(dims, 116.35, dtype="f4")
        g["qa_value"] = np.full(dims, 100, dtype="u1")
        if change != "variable":
            dset = g.create_dataset(VARIABLE, data=np.full(dims if change != "shape" else (1, 3, 2), 1800., dtype="f4"), compression="gzip")
            if change == "fill":
                dset.attrs["_FillValue"] = 1800.
    return path


def test_exact_product_and_canonical_archive_url():
    a, = row(feature())["raw"]["assets"]
    assert a["public_url"] == SOURCE and a["identity"]["orbit"] == 34501 and a["identity"]["variable"] == VARIABLE
    assert is_public_url(SOURCE)
    for bad in (SOURCE.replace("/OFFL/", "/RPRO/"), SOURCE.replace("/06/10/", "/06/11/"),
                SOURCE + "?acl", SOURCE.replace(".nc", "/preview.tif"), SOURCE.replace(".nc", "%2enc"),
                SOURCE.replace(S5P_ARCHIVE_HOST, "external.test")):
        assert not is_public_url(bad)


@pytest.mark.parametrize("problem", ["id-stream", "aoi", "date", "bad-orbit", "asset-path", "missing-digest", "ambiguous", "different-version"])
def test_unmatched_assets_never_fall_back(problem):
    f = feature()
    asset = f["assets"]["Product"]
    if problem == "id-stream":
        f["id"] = PRODUCT.replace("_OFFL_", "_NRTI_")
    elif problem == "aoi":
        f["geometry"] = mapping(box(114, 39, 115, 40))
    elif problem == "date":
        f["properties"].update(start_datetime="2024-06-09T23:59:00Z", end_datetime="2024-06-10T00:00:00Z")
    elif problem == "bad-orbit":
        f["properties"]["sat:absolute_orbit"] = 99999
    elif problem == "asset-path":
        asset["href"] = "https://elsewhere.test/" + NAME
    elif problem == "missing-digest":
        asset["file:checksum"] = "d50120" + "a" * 64
    elif problem == "ambiguous":
        f["assets"]["netcdf"] = {**asset, "file:checksum": "d50110" + "a" * 32}
    else:
        asset["href"] = CDSE_PATH.replace("020600", "020500")
    result = row(f)
    assert result is None or not result["raw"]["available"] and not result["raw"]["assets"]


@pytest.mark.parametrize("defect,match", [("orbit", "轨道"), ("time", "时段"), ("identity", "编号"), ("shape", "维度"),
                                          ("geocodes", "范围"), ("variable", "解码"), ("linked", "解码")])
def test_swath_decodes_arrays_and_refuses_corrupt_or_external_content(tmp_path, defect, match):
    proof = row(feature())["raw"]["assets"][0]["identity"]
    path = local_nc(tmp_path / "actual.nc", defect)
    with pytest.raises(UserError, match=match):
        verify_swath(path, proof)


def test_fill_values_are_not_a_failed_download_and_cancel_is_respected(tmp_path):
    proof = row(feature())["raw"]["assets"][0]["identity"]
    path = local_nc(tmp_path / "fill.nc", "fill")
    verified = verify_swath(path, proof)
    assert verified["shape"] == [1, 2, 3]
    assert verified["decoded_variables"] == ["latitude", "longitude", "qa_value", VARIABLE]
    assert verified["geolocated_pixels"] == 6 and verified["science_non_fill_geolocated"] == 0
    signal = threading.Event()
    signal.set()
    with pytest.raises(Cancelled):
        verify_swath(path, proof, signal)


@pytest.mark.parametrize("state", ["ok", "digest", "length", "payload", "missing", "redirect", "changed", "precondition", "not-indexed", "archive-length"])
def test_exact_s5p_api_pipeline_with_atomic_publication(tmp_path, monkeypatch, state):
    payload = local_nc(tmp_path / "sample.nc", "identity" if state == "payload" else "").read_bytes()
    doc = feature(payload)
    if state == "digest":
        payload = payload[:-1] + bytes([payload[-1] ^ 0x80])
    elif state == "length":
        payload = payload[:-1]
    downloads = []

    def reply(_, method, url, **kwargs):
        if url == STAC_ROOT + "/search":
            assert method == "POST" and kwargs["json"]["collections"] == [COLLECTION]
            assert kwargs["json"]["datetime"] == "2024-06-10T00:00:00Z/2024-06-11T00:00:00Z"
            return StubResponse(data={"features": [doc]})
        if url == ARCHIVE:
            assert method == "GET" and kwargs["params"] == {"list-type": "2", "prefix": PREFIX, "max-keys": "1000"}
            n = doc["assets"]["Product"]["file:size"]
            return StubResponse(content=archive_xml("" if state == "not-indexed" else archive_node(n + (state == "archive-length"))))
        assert method == "GET" and url == SOURCE and "auth" not in kwargs and kwargs["headers"]["If-Match"] == ETAG
        downloads.append(url)
        if state in ("missing", "redirect"):
            return StubResponse(404 if state == "missing" else 302, headers={"Location": "https://other.test/example"})
        if state == "precondition":
            return StubResponse(412)
        return StubResponse(content=payload, headers={"Content-Length": str(len(payload)), "ETag": '"another-version"' if state == "changed" else ETAG})

    monkeypatch.setattr(HttpClient, "request", reply)
    output = tmp_path / "研究区结果"
    with serve(tmp_path / "settings") as server:
        client = AppClient(f"http://127.0.0.1:{server.server_port}")
        config = client.get("bootstrap").json()
        assert not config["connected"] and len(config["s5p_collections"]) == len(COLLECTIONS) == 27
        aoi = client.post("aoi", {"coordinates": BBOX})["geometry"]
        criteria = {"collection": COLLECTION, "start": DAY.isoformat(), "end": DAY.isoformat()}
        result = client.wait(client.post("search", {"geometry": aoi, "profile": S5P_PUBLIC, **criteria})["job_id"])["result"]
        scene, = result["scenes"]
        assert scene["key"] == PRODUCT
        opts = {"mode": "original", "transport": "public"}
        req = {"geometry": aoi, "profile": S5P_PUBLIC, "criteria": criteria, "query_id": result["query"]["id"],
               "scene_keys": [PRODUCT], "options": opts, "output_dir": str(output)}
        if state in ("not-indexed", "archive-length"):
            assert not scene["raw"]["available"] and not scene["raw"]["assets"]
            assert ("未列出" if state == "not-indexed" else "长度") in scene["raw"]["unavailable_reason"]
            client.post("plan", req, 400)
            assert not downloads
            return
        assert scene["raw"]["available"]
        client.post("plan", {**req, "criteria": {**criteria, "collection": "sentinel-5p-l2-ch4-nrti"}}, 400)
        client.post("plan", {**req, "options": {**opts, "transport": "odata"}}, 400)
        plan = client.post("plan", req)
        assert not plan["requires"] and plan["estimated_bytes"] == doc["assets"]["Product"]["file:size"]
        job = client.wait(client.post("download", req)["job_id"], status="completed" if state == "ok" else "failed")
        assert downloads == [SOURCE] and not list(output.rglob("*.part"))
        if state == "ok":
            record = json.loads(Path(job["result"]["manifest"]).read_text(encoding="utf-8"))["items"][0]
            assert record["format_validation"] == "NetCDF swath identity and full core-array decode"
            assert record["provider_checksum_verified"] == "MD5" and record["s5p_validation"]["science_non_fill_geolocated"] == 6
            retry = client.wait(client.post("download", req)["job_id"])["result"]
            path = Path(retry["files"][0]["path"])
            assert retry["files"][0]["status"] == "skipped" and path.read_bytes() == payload and downloads == [SOURCE]
            path.write_bytes(payload[:-1] + bytes([payload[-1] ^ 1]))
            client.wait(client.post("download", req)["job_id"], status="failed")
            assert downloads == [SOURCE]
        else:
            assert not list(output.rglob("*.nc")) and not list(output.rglob("*.starmap.json"))
            assert {"digest": "MD5", "length": "长度", "payload": "编号", "missing": "没有改用", "redirect": "重定向", "changed": "版本", "precondition": "版本"}[state] in job["error"]


@pytest.mark.parametrize("kind", ["good", "loop", "wrong-prefix"])
def test_archive_pagination_only_advances_fixed_utc_prefix(kind):
    class Http:
        def __init__(self):
            self.calls = []

        def request(self, method, url, **kwargs):
            assert method == "GET" and url == ARCHIVE
            p = kwargs["params"]
            self.calls.append(p)
            if len(self.calls) == 1:
                return StubResponse(content=archive_xml(next_token="safe+/=", prefix=PREFIX if kind != "wrong-prefix" else "different/"))
            assert p["continuation-token"] == "safe+/="
            return StubResponse(content=archive_xml(next_token="safe+/=" if kind == "loop" else "",
                                                     body=archive_node(4567) if kind == "good" else ""))

    http = Http()
    if kind == "good":
        assert archive_objects(http, PREFIX, {PREFIX + NAME}, None) == {PREFIX + NAME: (4567, ETAG)}
    else:
        with pytest.raises(UserError, match="索引"):
            archive_objects(http, PREFIX, {PREFIX + NAME}, None)
    assert len(http.calls) == (1 if kind == "wrong-prefix" else 2)
