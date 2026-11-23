"""ESA WorldCover annual boundaries, S3 provenance, transport faults and local receipt trust."""
from __future__ import annotations

import copy
import hashlib
import json
import threading
from pathlib import Path
from urllib.parse import urlsplit
from xml.etree import ElementTree as ET

import numpy as np
import pytest
import rasterio
import requests
from rasterio.shutil import copy as raster_copy
from rasterio.transform import Affine
from requests.structures import CaseInsensitiveDict
from shapely.geometry import Polygon, box

from conftest import StubResponse, assert_download_prefix
from core import open_worldcover as wc
from core.errors import Cancelled, UserError
from core.http import HttpClient, check_cancel
from core.s3_source import WC_AWS_SUFFIX, is_public_url, worldcover_object, worldcover_prefix, worldcover_url

TILE = "N00E006"
AOI = "6.58,0.30,6.63,0.35"
RAPA = box(-109.42, -27.16, -109.39, -27.13)
SINGLE = '"' + 'a' * 32 + '"'


class Reply(StubResponse):
    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


def etag(blob, part_bytes):
    if part_bytes is None:
        return f'"{hashlib.md5(blob, usedforsecurity=False).hexdigest()}"'
    parts = [hashlib.md5(blob[i:i+part_bytes], usedforsecurity=False).digest() for i in range(0, len(blob), part_bytes)]
    return f'"{hashlib.md5(b"".join(parts), usedforsecurity=False).hexdigest()}-{len(parts)}"'


class Archive:
    def __init__(self):
        self.objects = {}
        self.calls = []
        self.fault = None
        self.stopped = threading.Event()

    def put(self, layer, blob=b"example", year=2020, tile=TILE, part_bytes=None):
        url = worldcover_url(year, tile, layer)
        self.objects[url] = (blob, etag(blob, part_bytes), part_bytes)
        return url

    def request(self, method, url, *, cancel=None, headers=None, params=None, **kwargs):
        headers = headers or {}
        self.calls.append((method, url, headers, params))
        assert not kwargs.get("allow_redirects", False) and not kwargs.get("auth") and not headers.get("Authorization")
        check_cancel(cancel)
        if url.endswith("/"):
            assert method == "GET" and params["list-type"] == "2"
            bucket, prefix = urlsplit(url).hostname.replace(WC_AWS_SUFFIX, ""), params["prefix"]
            ns = wc.NS
            doc = ET.Element(ns+"ListBucketResult")
            if self.fault == "list-403":
                return Reply(403)
            for key, val in (("Name", bucket), ("Prefix", prefix + ("WRONG" if self.fault == "wrong-prefix" else "")),
                             ("IsTruncated", "true" if self.fault == "truncated-list" else "false")):
                ET.SubElement(doc, ns+key).text = val
            entries = {k[len(url):]:(len(v[0]), v[1]) for k, v in self.objects.items() if k.startswith(url+prefix)}
            # A listing can contain other products; a non-exact key must never become a usable asset.
            entries[prefix + "zz_useful_lookalike.tif"] = (5, SINGLE)
            pairs = list(entries.items())
            if self.fault == "duplicate-list":
                pairs = pairs[:1] + pairs
            for key, (size, tag) in pairs:
                c = ET.SubElement(doc, ns+"Contents")
                ET.SubElement(c, ns+"Key").text = key
                ET.SubElement(c, ns+"ETag").text = tag if self.fault != "bad-tag" else 'W/"some-version"'
                ET.SubElement(c, ns+"Size").text = str(size)
            ET.SubElement(doc, ns+"KeyCount").text = str(len(pairs))
            return Reply(content=ET.tostring(doc))
        assert url in self.objects
        blob, tag, split = self.objects[url]
        assert headers["Accept-Encoding"] == "identity" and headers["If-Match"] == tag
        h = CaseInsensitiveDict({"Content-Type": "binary/octet-stream", "ETag": tag, "Content-Length": str(len(blob)),
                                 "x-amz-server-side-encryption": "AES256"})
        if self.fault == "kms":
            h["x-amz-server-side-encryption"] = "aws:kms"
        if self.fault == "missing":
            return Reply(404)
        if method == "HEAD" and params:
            assert params == {"partNumber": 1} and split
            h["x-amz-mp-parts-count"] = tag.strip('"').split("-")[1]
            h["Content-Length"] = str(split)
            h["Content-Range"] = f"bytes 0-{split-1}/{len(blob)}"
            if self.fault == "bad-part":
                h["Content-Range"] = h["Content-Range"].replace("bytes 0", "bytes 1")
            return Reply(206, headers=h)
        if method == "HEAD":
            if self.fault == "wrong-length":
                h["Content-Length"] = str(len(blob)+1)
            return Reply(headers=h)
        assert method == "GET"
        if self.fault == "stale":
            return Reply(412, headers=h)
        if self.fault == "redirect":
            return Reply(302, headers={"Location": "https://not-the-source.invalid/"})
        if self.fault == "tamper":
            blob = blob[:-1] + bytes([blob[-1] ^ 1])
        if self.fault in ("truncate", "bad-range", "always-truncate", "cancel"):
            if "Range" in headers:
                start = int(headers["Range"].split("=")[1][:-1])
                wrong = start+1 if self.fault == "bad-range" else start
                h["Content-Range"] = f"bytes {wrong}-{len(blob)-1}/{len(blob)}"
                h["Content-Length"] = str(len(blob)-start)
                return Reply(206, headers=h, content=blob[start:] if self.fault != "always-truncate" else b"")
            host = self
            class Incomplete(Reply):
                def iter_content(self, size):
                    yield blob[:len(blob)//2]
                    if host.fault == "cancel":
                        host.stopped.set()
                        assert cancel.wait(5)
                        check_cancel(cancel)
                    raise requests.exceptions.ChunkedEncodingError("test stream disconnected")
            return Incomplete(headers=h)
        return Reply(headers=h, content=blob)


@pytest.fixture
def archive(monkeypatch):
    s3 = Archive()
    monkeypatch.setattr(HttpClient, "request", lambda self, *args, **kwargs: s3.request(*args, **kwargs))
    return s3


def image(tmp_path, monkeypatch, wrong=None):
    # Real tiled COG and embedded metadata, shrunk only for offline fault-injection tests.
    expected = wc._expected_raster
    monkeypatch.setattr(wc, "_expected_raster", lambda y, t, l: {**expected(y, t, l), "shape": [32, 32]})
    r = wc._expected_raster(2020, TILE, "SWIR")
    src, dst = tmp_path/"source.tif", tmp_path/"reference.tif"
    t = r["transform"][:]
    if wrong == "origin":
        t[2] += 1
    with rasterio.open(src, "w", driver="GTiff", count=2, height=32, width=32, dtype=r["dtype"], crs=4326,
                       transform=Affine(*t), nodata=r["nodata"]) as ds:
        tags = r["tags"].copy()
        if wrong == "metadata":
            tags["time_start"] = "2021-01-01T00:00:00Z"
        if wrong == "band-order":
            tags["bands"] = "Band 1: B12-p50, Band 2: B11-p50"
        ds.update_tags(**tags)
        ds.scales = (r["scale"],) * 2 if wrong != "scale" else (1., 1.)
        ds.offsets = (r["offset"],) * 2
        ds.write(np.arange(2048, dtype=np.uint8).reshape(2,32,32))
    raster_copy(src, dst, driver="COG", compress="DEFLATE")
    return dst.read_bytes()


def selection(client, out, profile=wc.S2_ANNUAL, start="2020-06-30", end="2020-06-30", assets=None, aoi=AOI):
    geometry = client.post("aoi", {"coordinates": aoi})["geometry"]
    criteria = {"start": start, "end": end}
    feed = client.wait(client.post("search", {"profile": profile, "geometry": geometry, **criteria})["job_id"])["result"]
    return {"profile": profile, "geometry": geometry, "criteria": criteria, "query_id": feed["query"]["id"],
            "scene_keys": [row["key"] for row in feed["scenes"]], "output_dir": str(out),
            "options": {"mode": "assets", "assets": assets or ["SWIR"]} if profile == wc.S2_ANNUAL else {"mode": "original"}}, feed


def get_manifest(out):
    return json.loads(next(out.rglob("下载记录_*.json")).read_text(encoding="utf-8"))


def test_worldcover_urls_strict_from_release_bucket_grid_and_family():
    swir = worldcover_url(2020, TILE, "SWIR")
    land = worldcover_url(2021, "S30W111", "Map")
    assert swir == f"https://esa-worldcover-s2.s3.eu-central-1.amazonaws.com/swir/2020/N00/ESA_WorldCover_10m_2020_v100_{TILE}_SWIR.tif"
    assert worldcover_object(land) == (2021, "S30W111", "Map") and is_public_url(land)
    assert worldcover_object(swir) == (2020, TILE, "SWIR") and is_public_url(swir)
    for url in (swir+"?partNumber=1", swir+"#fragment", swir.replace("v100", "v200"), swir.replace("/N00/", "/N01/"),
                swir.replace("s2.s3", "s1.s3"), swir.replace("/swir/", "/rgbnir/"), swir.replace("https:", "http:"),
                swir.replace("/swir/", "/%73wir/"), swir.replace(".amazonaws.com/", ".amazonaws.com:443/")):
        assert worldcover_object(url) is None and not is_public_url(url)
    assert worldcover_url(2021, "N90E000", "Map") is None
    assert worldcover_url(2021, "N00E180", "SWIR") is None
    assert worldcover_url(2021, "S00E006", "SWIR") is None
    assert worldcover_url(2021, "N01E006", "Map") is None
    assert worldcover_url(2022, TILE, "SWIR") is None


def test_overlap_calendar_and_missing_official_layer(archive):
    archive.put("SWIR"); archive.put("SWIR", year=2021)
    region = box(6.59, 0.31, 6.6, 0.32)
    scenes = wc.search_worldcover(region, wc.S2_ANNUAL, "2020-12-31", "2021-01-01")
    assert [s["date"] for s in scenes] == ["2021", "2020"]
    assert [s["end_utc"] for s in scenes] == ["2021-12-31T23:59:59Z", "2020-12-31T23:59:59Z"]
    assert all(s["coverage_percent"] == 100 and [a["id"] for a in s["raw"]["assets"]] == ["SWIR"] and "NDVI" in s["raw"]["source_note"] for s in scenes)
    before = len(archive.calls)
    assert wc.search_worldcover(region, wc.S2_ANNUAL, "2022-01-01", "2022-01-01") == []
    radar, = wc.search_worldcover(RAPA, wc.S1_ANNUAL, "2021-12-31", "2021-12-31")
    assert not radar["raw"]["available"] and not radar["raw"]["assets"] and "官方网格未发布" in radar["raw"]["unavailable_reason"]
    assert len(archive.calls) == before  # Missing radar is not replaced and does not issue optical/S3 requests.


def test_true_intersection_not_envelope_or_touch(archive):
    archive.put("SWIR")
    # Exact tile completely inside a hole, with its boundary touching the AOI.
    region = Polygon([(5.9,-.1),(7.1,-.1),(7.1,1.1),(5.9,1.1),(5.9,-.1)], [list(box(6,0,7,1).exterior.coords)])
    assert not any(TILE in x["tiles"] for x in wc.search_worldcover(region, wc.S2_ANNUAL, "2020-04-01", "2020-04-01"))


@pytest.mark.parametrize("fault", ["list-403", "truncated-list", "wrong-prefix", "duplicate-list", "bad-tag"])
def test_broken_s3_list_not_misreported_as_no_matching_year(archive, fault):
    url = archive.put("SWIR")
    archive.fault = fault
    with pytest.raises(UserError, match="目录"):
        wc._list_group(worldcover_prefix(2020, TILE, "SWIR"), {url.split("/", 3)[3]}, None)


def test_grid_defines_original_fill_scale_and_pixel_step():
    assert wc._expected_raster(2021, "S30W111", "Map")["shape"] == [36000,36000]
    assert wc._expected_raster(2021, "S28W110", "NDVI")["offset"] == -1
    d = wc._expected_raster(2020, TILE, "SWIR")
    assert d["shape"] == [6000,6000] and d["transform"] == [1/6000,0,6,0,-1/6000,1]
    assert (d["dtype"], d["nodata"], d["scale"]) == ("uint8",255,.004)


def test_api_resume_atomic_publish_and_upstream_verified_reuse(client, tmp_path, monkeypatch, archive):
    src = image(tmp_path, monkeypatch)
    url = archive.put("SWIR", src, part_bytes=240)
    out = tmp_path/"deliveries"
    selection_, s = selection(client, out)
    assert s["scenes"][0]["raw"]["available"]
    plan = client.post("plan", selection_)
    assert plan["requires"] == [] and plan["estimated_bytes"] == len(src) and plan["targets"][0]["url"] == url
    client.post("plan", {**selection_, "criteria": {"start": "2020-06-30", "end": "2020-07-01"}}, status=400)
    client.post("plan", {**selection_, "options": {"mode":"assets", "assets":["NDVI"]}}, status=400)
    archive.fault = "truncate"
    result = client.wait(client.post("download", selection_)["job_id"])["result"]
    path = Path(result["files"][0]["path"])
    assert path.read_bytes() == src and not list(out.rglob("*.part"))
    receipt = path.with_suffix(".starmap.json")
    info = json.loads(receipt.read_text(encoding="utf-8"))
    assert info["transfer_attempts"] == 2 and "multipart" in info["provider_checksum_verified"]
    assert any(h.get("Range") == f"bytes={len(src)//2}-" for method, u, h, _ in archive.calls if u==url and method=="GET")
    before = len([1 for method, u, *_ in archive.calls if method == "GET" and u == url])
    assert client.wait(client.post("download", selection_)["job_id"])["result"]["files"][0]["status"] == "skipped"
    assert before == len([1 for method, u, *_ in archive.calls if method == "GET" and u == url])
    modified = src[:-1] + bytes([src[-1] ^ 1])
    path.write_bytes(modified)
    info["sha256"] = hashlib.sha256(modified).hexdigest()
    receipt.write_text(json.dumps(info), encoding="utf-8")  # A forged local SHA alone does not make this a verified file.
    failure = client.wait(client.post("download", selection_)["job_id"], status="failed")
    assert "ETag" in failure["error"] and path.read_bytes() == modified
    path.rename(tmp_path/"moved-corrupt.tif"); receipt.rename(tmp_path/"moved-corrupt-receipt.json")
    archive.fault = None
    recovered, = client.wait(client.post("download", selection_)["job_id"])["result"]["files"]
    assert recovered["status"] == "completed" and path.read_bytes() == src
    assert not list(out.rglob("*.zip"))


@pytest.mark.parametrize("fault", ["tamper", "stale", "missing", "redirect", "wrong-length", "kms", "bad-part", "bad-range", "always-truncate"])
def test_transfer_fault_never_publishes_partial_or_wrong_object(client, tmp_path, monkeypatch, archive, fault):
    original = image(tmp_path, monkeypatch)
    archive.put("SWIR", original, part_bytes=240)
    output = tmp_path/"out"
    request, _ = selection(client, output)
    archive.fault = fault
    error = client.wait(client.post("download", request)["job_id"], status="failed")["error"]
    assert "异常（" not in error and not list(output.rglob("*.tif")) and not list(output.rglob("*.zip"))
    for partial in output.rglob("*.part"):
        assert_download_prefix(partial, original)
    assert get_manifest(output)["items"][0]["status"] == "failed"


@pytest.mark.parametrize("bad", ["origin", "metadata", "band-order", "scale"])
def test_source_hash_do_not_replace_grid_and_content_semantics(client, tmp_path, monkeypatch, archive, bad):
    archive.put("SWIR", image(tmp_path, monkeypatch, bad))
    dest = tmp_path/"out"
    request, _ = selection(client, dest)
    error = client.wait(client.post("download", request)["job_id"], status="failed")["error"]
    assert "不一致" in error and not list(dest.rglob("*.tif")) and not list(dest.rglob("*.part"))


def test_cancel_midstream_never_publishes_and_fresh_retry_works(client, tmp_path, monkeypatch, archive):
    original = image(tmp_path, monkeypatch)
    archive.put("SWIR", original)
    dest = tmp_path/"out"
    request, _ = selection(client, dest)
    archive.fault = "cancel"
    key = client.post("download", request)["job_id"]
    assert archive.stopped.wait(5)
    client.post("cancel", {"job_id": key})
    client.wait(key, status="cancelled")
    assert get_manifest(dest)["status"] == "cancelled" and not list(dest.rglob("*.tif")) and not list(dest.rglob("*.zip"))
    partial, = dest.rglob("*.part")
    assert_download_prefix(partial, original)
    archive.fault = None
    assert client.wait(client.post("download", request)["job_id"])["result"]["files"][0]["status"] == "completed"
    assert not list(dest.rglob("*.part"))


def test_target_identity_binds_metadata_and_content(client, archive, tmp_path):
    archive.put("SWIR")
    request, _ = selection(client, tmp_path)
    real = client.post("plan", request)["targets"][0]
    for k, v in [("asset", "NDVI"), ("key", real["key"].replace("v100", "v200")), ("transport", "cdse-s3"), ("bytes", True)]:
        with pytest.raises(UserError, match="目标"):
            wc.check_worldcover_target({**real, k:v})
    changed = copy.deepcopy(real)
    changed["identity"]["year"] = 2021
    with pytest.raises(UserError, match="目标"):
        wc.check_worldcover_target(changed)
