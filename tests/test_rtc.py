"""Exact Sentinel-1 RTC binding, verified Azure bytes and linear-radar GeoTIFFs."""
from __future__ import annotations

import base64
import copy
import datetime as dt
import hashlib
import json
import os
import threading
from pathlib import Path

import numpy as np
from scripts.browser_flow import open_data_page

import pytest
import rasterio
import requests
from rasterio.io import MemoryFile
from rasterio.transform import from_origin
from shapely.geometry import box, mapping

from conftest import AppClient, BBOX, StubResponse, serve
from core.errors import UserError
from core.geometry import area_km2
from core.http import HttpClient
from core.open_native import PC_SEARCH
from core.public_rtc import RTC_COLLECTION, S1_RTC, rtc_options, rtc_row
from core.public_token import RTC_TOKEN, PlanetaryAccess
from core.range_file import RangeFile
from core.s3_source import PC_RTC_HOST, rtc_blob_object

AOI = box(116.350, 39.900, 116.355, 39.905)
DAY = dt.date(2024, 6, 12)
KEY = "S1A_IW_GRDH_1SDV_20240612T101408_20240612T101433_054289_069A97_rtc"
SOURCE = KEY.removesuffix("_rtc") + "_9D5E"
ROOT = f"https://{PC_RTC_HOST}/sentinel1-grd-rtc/GRD/2024/6/12/IW/DV/{SOURCE}/measurement/"
URLS = {pol: ROOT + f"iw-{pol.lower()}.rtc.tiff" for pol in ("VV", "VH")}
TAG, NEW_TAG = '"0x8DC8CA61223210E"', '"0x8DC8CA61223210F"'
MOCK_TOKEN = "sp=r&sr=c&sig=mock-rtc-only"
NODATA = -32768.0
TRANSFORM = from_origin(444410, 4417440, 10, 10)
DIMS = (64, 56)


def digest(block):
    return base64.b64encode(hashlib.md5(block, usedforsecurity=False).digest()).decode("ascii")


def feature(sizes=(1990, 2010)):
    return {"id": KEY, "collection": RTC_COLLECTION, "geometry": mapping(AOI.buffer(.01)),
            "properties": {"platform": "SENTINEL-1A", "datetime": "2024-06-12T10:14:21.398251Z",
                           "start_datetime": "2024-06-12 10:14:08.898984+00:00", "end_datetime": "2024-06-12 10:14:33.897518+00:00",
                           "sat:absolute_orbit": 54289, "sar:instrument_mode": "IW", "sar:product_type": "GRD", "sar:polarizations": ["VV", "VH"],
                           "proj:epsg": 32650, "proj:shape": list(DIMS), "proj:transform": list(TRANSFORM)},
            "assets": {band.lower(): {"href": URLS[band], "type": "image/tiff; application=geotiff", "roles": ["data"],
                                     "file:size": size, "raster:bands": [{"nodata": NODATA, "data_type": "float32", "spatial_resolution": 10}]}
                       for band, size in zip(URLS, sizes, strict=True)}}


def row(item):
    return rtc_row(item, AOI, area_km2(AOI), DAY, DAY + dt.timedelta(days=1), "IW")


def test_rtc_canonical_urls_and_exact_revision():
    assert rtc_blob_object(URLS["VV"]) == (SOURCE, "VV")
    ew = (f"https://{PC_RTC_HOST}/sentinel1-grd-rtc/GRD/2022/3/8/EW/DH/"
          "S1A_EW_GRDM_1SDH_20220308T134445_20220308T134521_042230_050877_6CDE/measurement/iw-hh.rtc.tiff")
    assert rtc_blob_object(ew) == ("S1A_EW_GRDM_1SDH_20220308T134445_20220308T134521_042230_050877_6CDE", "HH")
    for invalid in (URLS["VV"] + "?sig=x", URLS["VV"].replace(PC_RTC_HOST, PC_RTC_HOST + ".evil.invalid"),
                    URLS["VV"].replace("/6/12/", "/06/12/"), URLS["VV"].replace("iw-vv", "iw-hh"),
                    URLS["VV"].replace("/measurement/", "/foo/"), URLS["VV"].replace("/IW/DV/", "/EW/DV/"),
                    URLS["VV"].replace("/measurement/", "/%6deasurement/")):
        assert rtc_blob_object(invalid) is None


@pytest.mark.parametrize("mutation", ["orbit", "time", "wrong-platform", "wrong-mode", "revision-conflict", "grid", "outside"])
def test_catalogue_drops_bad_scene_instead_of_using_another_scene(mutation):
    item = feature()
    good = row(item)
    assert good["public"]["source_id"] == SOURCE and list(good["public"]["assets"]) == ["VV", "VH"]
    assert good["tiles"] == ["IW · VV/VH"] and good["cloud_min"] is None
    if mutation == "orbit":
        item["properties"]["sat:absolute_orbit"] += 1
    elif mutation == "time":
        item["properties"]["start_datetime"] = "2024-06-12T10:14:03Z"
    elif mutation == "wrong-platform":
        item["properties"]["platform"] = "SENTINEL-1B"
    elif mutation == "wrong-mode":
        item["properties"]["sar:instrument_mode"] = "EW"
    elif mutation == "revision-conflict":
        item["assets"]["vh"]["href"] = URLS["VH"].replace("_9D5E", "_1111")
    elif mutation == "grid":
        item["properties"]["proj:transform"][3] = .1
    elif mutation == "outside":
        item["geometry"] = mapping(box(18, 40, 20, 42))
    assert row(item) is None


@pytest.mark.parametrize("options", [
    {"bands": []}, {"bands": ["VV", "VV"]}, {"bands": ["B04"]}, {"bands": [{}]},
    {"mode": "rgb"}, {"mask_clouds": True}, {"resolution": True}, {"resolution": 5},
])
def test_rtc_never_accepts_inapplicable_optical_or_invalid_controls(options):
    with pytest.raises(UserError):
        rtc_options(options)


def test_sas_scoped_to_only_rtc_data_paths():
    class TokenServer:
        requests = 0

        def request(self, method, url, **params):
            assert (method, url) == ("GET", RTC_TOKEN) and params["allow_redirects"] is False
            self.requests += 1
            return StubResponse(data={"token": MOCK_TOKEN, "msft:expiry": (dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=2)).isoformat()})

    server = TokenServer()
    auth = PlanetaryAccess(server, scope="rtc")
    for bad in (requests.Request("POST", URLS["VV"]), requests.Request("GET", URLS["VV"]+"?already=true"),
                requests.Request("GET", URLS["VV"].replace(PC_RTC_HOST, "elsewhere.invalid")),
                requests.Request("GET", f"https://{PC_RTC_HOST}/sentinel1-grd-rtc")):
        with pytest.raises(UserError):
            auth(bad.prepare())
    assert server.requests == 0
    assert auth(requests.Request("GET", URLS["VV"]).prepare()).url == URLS["VV"] + "?" + MOCK_TOKEN
    assert auth(requests.Request("GET", URLS["VH"]).prepare()).url == URLS["VH"] + "?" + MOCK_TOKEN and server.requests == 1


@pytest.mark.parametrize("fault", ["none", "short", "missing-md5", "bad-md5", "wrong-tag", "wrong-size", "redirect", "refresh"])
def test_rtc_range_pins_version_and_checks_every_reply(fault):
    contents = bytes(range(180))
    probes = []

    class Access:
        refreshed = 0

        def clear(self):
            self.refreshed += 1

    access = Access()

    class Reader:
        def request(self, method, url, **kwargs):
            assert (method, url) == ("GET", URLS["VV"]) and kwargs["auth"] is access and not kwargs["allow_redirects"]
            headers = kwargs["headers"].copy()
            probes.append(headers)
            lo, hi = map(int, headers["Range"].removeprefix("bytes=").split("-"))
            assert headers["x-ms-range-get-content-md5"] == "true" and headers["Accept-Encoding"] == "identity"
            if lo:
                assert headers["If-Match"] == TAG
            body = contents[lo:hi+1]
            reply = {"Content-Range": f"bytes {lo}-{hi}/{len(contents)}", "ETag": TAG,
                     "Content-MD5": digest(body), "Content-Length": str(len(body))}
            if lo and len(probes) == 2:
                if fault == "short":
                    body = body[:-10]
                elif fault == "missing-md5":
                    reply.pop("Content-MD5")
                elif fault == "bad-md5":
                    body = bytes([body[0] ^ 1]) + body[1:]
                elif fault == "wrong-tag":
                    reply["ETag"] = NEW_TAG
                elif fault == "wrong-size":
                    reply["Content-Range"] = f"bytes {lo}-{hi}/181"
                elif fault == "redirect":
                    return StubResponse(status=302, headers={"Location": "https://elsewhere.invalid"})
                elif fault == "refresh":
                    return StubResponse(status=403)
            return StubResponse(status=206, content=body, headers=reply)

    with RangeFile(Reader(), URLS["VV"], page_size=64, access=access, range_md5=True, expected_size=len(contents)) as stream:
        with pytest.raises(UserError) if fault in ("bad-md5", "missing-md5", "wrong-tag", "wrong-size", "redirect") else _no_error():
            assert stream.read(100) == contents[:100]
        if fault in ("short", "refresh"):
            assert len(probes) == 3 and probes[-1] == probes[-2] and stream.verified_range_md5 == 2
        assert access.refreshed == (fault == "refresh")
        if fault == "none":
            assert stream.verified_range_md5 == 2 and stream.etag == TAG


def _no_error():
    from contextlib import nullcontext
    return nullcontext()


def _tiff(value, valid_left):
    with MemoryFile() as mem:
        with mem.open(driver="GTiff", width=DIMS[1], height=DIMS[0], count=1, dtype="float32", nodata=NODATA,
                      crs="EPSG:32650", transform=TRANSFORM) as raster:
            array = np.full(DIMS, NODATA, dtype=np.float32)
            keep = slice(None, DIMS[1]//2) if valid_left else slice(DIMS[1]//2, None)
            array[:, keep] = value
            raster.write(array, 1)
        return mem.read()


@pytest.mark.parametrize("fault", ["none", "same-length-corruption", "wrong-grid", "auth-denied", "all-nodata", "cancel-range"])
def test_rtc_app_downloads_linear_bands_with_individual_nodata_or_refuses_to_publish(tmp_path, monkeypatch, fault):
    values = (NODATA, NODATA) if fault == "all-nodata" else (.25, .03125)
    bodies = {URLS["VV"]: _tiff(values[0], True), URLS["VH"]: _tiff(values[1], False)}
    item = feature([len(bodies[url]) for url in URLS.values()])
    if fault == "wrong-grid":
        item["properties"]["proj:transform"][2] += 10
    requests_for_blobs = []
    pending, resume = threading.Event(), threading.Event()

    class PendingRange(StubResponse):
        def iter_content(self, size):
            pending.set()
            assert resume.wait(8), "The test must release the in-flight range"
            yield from super().iter_content(size)

    def request(_client, method, url, **kwargs):
        if (method, url) == ("POST", PC_SEARCH):
            assert kwargs["json"]["query"] == {"sar:instrument_mode": {"eq": "IW"}}
            return StubResponse(data={"features": [copy.deepcopy(item)], "links": []})
        if (method, url) == ("GET", RTC_TOKEN):
            if fault == "auth-denied":
                return StubResponse(status=403)
            return StubResponse(data={"token": MOCK_TOKEN, "msft:expiry": (dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=2)).isoformat()})
        assert method == "GET" and url in bodies
        prepared = kwargs["auth"](requests.Request(method, url).prepare())
        assert prepared.url == url + "?" + MOCK_TOKEN
        headers = kwargs["headers"].copy()
        assert headers["x-ms-range-get-content-md5"] == "true"
        requests_for_blobs.append(url)
        lo, hi = map(int, headers["Range"].removeprefix("bytes=").split("-"))
        size = len(bodies[url])
        hi = min(size-1, hi)
        part = bodies[url][lo:hi+1]
        checks = {"Content-Range": f"bytes {lo}-{hi}/{size}", "ETag": TAG, "Content-MD5": digest(part), "Content-Length": str(len(part))}
        if fault == "same-length-corruption" and url == URLS["VH"]:
            part = part[:-1] + bytes([part[-1] ^ 1])
        response = PendingRange if fault == "cancel-range" and url == URLS["VH"] else StubResponse
        return response(status=206, content=part, headers=checks)

    monkeypatch.setattr(HttpClient, "request", request)
    with serve(tmp_path / "settings") as server:
        api = AppClient(server.origin)
        try:
            area = api.post("aoi", {"coordinates": BBOX})["geometry"]
            common = {"profile": S1_RTC, "geometry": area}
            criteria = {"start": DAY.isoformat(), "end": DAY.isoformat(), "radar_mode": "IW"}
            result = api.wait(api.post("search", {**common, **criteria})["job_id"])["result"]
            out = tmp_path / "raster"
            args = {**common, "query_id": result["query"]["id"], "criteria": criteria, "scene_keys": [KEY],
                    "options": {"mode": "rtc", "bands": ["VV", "VH"], "resolution": 10}, "output_dir": str(out)}
            plan = api.post("plan", args)
            assert plan["collection"] == RTC_COLLECTION and plan["processing"] == "linear_gamma0"
            assert plan["source_objects"] == 2 and plan["estimated_pu"] is None and not requests_for_blobs
            assert "HH" in api.post("plan", {**args, "options": {**args["options"], "bands": ["VV", "HH"]}}, status=400)["error"]
            stale = {**criteria, "radar_mode": "EW"}
            assert "重新检索" in api.post("plan", {**args, "criteria": stale}, status=400)["error"]
            wanted_status = ("completed" if fault in ("none", "all-nodata") else "cancelled" if fault == "cancel-range" else "failed")
            job_id = api.post("download", args)["job_id"]
            if fault == "cancel-range":
                try:
                    assert pending.wait(5)
                    api.post("cancel", {"job_id": job_id})
                finally:
                    resume.set()
            job = api.wait(job_id, wanted_status)
            if fault != "none":
                assert not tuple(out.rglob("*.tif")) and not tuple(out.rglob("*.starmap.json"))
                proof_text = next(out.rglob("下载记录_*.json")).read_text(encoding="utf-8")
                assert MOCK_TOKEN not in proof_text
                manifest = json.loads(proof_text)
                status = "empty" if fault == "all-nodata" else wanted_status
                assert manifest["status"] == manifest["items"][0]["status"] == status
                if wanted_status == "failed":
                    assert {"same-length-corruption": "MD5", "wrong-grid": "像元网格", "auth-denied": "临时读取授权"}[fault] in job["error"]
                if fault == "all-nodata":
                    assert job["result"]["files"] == [] and job["result"]["empty_scenes"] == [KEY]
                return
            file, = job["result"]["files"]
            source = Path(file["path"])
            proof = Path(job["result"]["manifest"]).read_text(encoding="utf-8")
            assert MOCK_TOKEN not in proof and "?sp=" not in proof
            saved, = json.loads(proof)["items"]
            assert saved["catalogue_product_id"] == SOURCE
            assert all(v["azure_md5_verified_ranges"] > 0 for v in saved["transport"].values())
            with rasterio.open(source) as raster:
                assert raster.descriptions == ("VV", "VH") and raster.nodata == NODATA and raster.tags()["radar_backscatter"] == "linear_gamma0"
                vv, vh = raster.read(masked=True)
                assert vv.count() and vh.count() and np.all(vv.compressed() == .25) and np.all(vh.compressed() == .03125)
                assert not np.any(~vv.mask & ~vh.mask)
                assert saved["valid_pixels"] == vv.count() + vh.count()
                assert saved["valid_pixels_per_band"] == {"VV": vv.count(), "VH": vh.count()}
            previous = requests_for_blobs.copy()
            reuse, = api.wait(api.post("download", args)["job_id"])["result"]["files"]
            assert reuse["status"] == "skipped" and requests_for_blobs == previous
        finally:
            api.session.close()


@pytest.mark.browser
def test_rtc_ui_reports_no_common_polarization_until_selection_is_fixed(tmp_path, monkeypatch):
    pw = pytest.importorskip("playwright.sync_api")
    first, second = feature(), feature()
    second["id"] = KEY.replace("069A97", "069A98")
    first["properties"]["sar:polarizations"] = ["VV"]
    second["properties"]["sar:polarizations"] = ["VH"]
    del first["assets"]["vh"], second["assets"]["vv"]
    second["assets"]["vh"]["href"] = URLS["VH"].replace("069A97", "069A98")

    def request(_http, method, url, **kwargs):
        assert (method, url) == ("POST", PC_SEARCH)
        return StubResponse(data={"features": [first, second], "links": []})

    monkeypatch.setattr(HttpClient, "request", request)
    with serve(tmp_path / "settings") as server, pw.sync_playwright() as driver:
        browser = driver.chromium.launch(channel=os.environ.get("STARMAP_TEST_BROWSER", "msedge"), headless=True)
        page = browser.new_page(viewport={"width": 1360, "height": 900})
        page.route("https://**/*", lambda route: route.abort())
        open_data_page(page, server.origin)
        pw.expect(page.locator("#outputDirectory")).not_to_have_value("")
        page.locator("#sourceProfile").select_option(S1_RTC)
        page.locator('[data-tab="coordinates"]').click()
        page.locator("#coordinates").fill(BBOX)
        page.locator("#applyCoordinates").click()
        page.locator("#startDate").fill(DAY.isoformat())
        page.locator("#endDate").fill(DAY.isoformat())
        page.locator("#searchScenes").click()
        pw.expect(page.locator("#scenesBody tr")).to_have_count(2)
        page.locator("#selectAll").click()
        pw.expect(page.locator("#rtcBandAvailability")).to_contain_text("没有共有极化")
        pw.expect(page.locator("#downloadSelected")).to_be_disabled()
        page.locator(f'#scenesBody tr[data-key="{second["id"]}"] input').uncheck()
        pw.expect(page.locator("#rtcBandAvailability")).to_have_text("所选景共有极化：VV")
        pw.expect(page.locator('#rtcBands input[value="VH"]')).to_be_disabled()
        pw.expect(page.locator("#downloadSelected")).to_be_enabled()
        browser.close()
