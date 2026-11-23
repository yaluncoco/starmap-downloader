"""Native-object routes: signed requests, product identity, and byte-level fail-closed checks."""
from __future__ import annotations

import binascii
import datetime as dt
import hashlib
import json
from pathlib import Path
from urllib.parse import urlsplit

import numpy as np
from scripts.browser_flow import open_data_page

import pytest
import rasterio
import requests
from rasterio.transform import from_bounds
from shapely.geometry import box, mapping

from conftest import BBOX, StubResponse
from core.errors import UserError
from core.http import HttpClient
from core.native_catalogue import DOWNLOAD_ROOT, STAC_ROOT, download_asset, raw_item
from core.public_radar import public_radar_row
from core.raw_download import _check_raw_raster, _stream, _validate, _validated_digests, download_original, raw_plan
from core.s3_source import CDSES3Auth, CDSE_HOST, S1_BUCKET, _sign_get, cdse_object

AOI = box(116.350, 39.900, 116.355, 39.905)
MODIS = "modis-terra-mod09q1"
MODIS_KEY = "MOD09Q1.A2024169.h26v05.061.2024181135518"
UUID = "6981cace-8626-43b2-9aaf-175e72ac8c80"
TEST_ACCESS = "test-s3-access-id"
TEST_SECRET = "test-s3-fake-secret-only"
HDF = b"\x0e\x03\x13\x01" + b"fixture header check only -- not a real MODIS file" * 3
S3_LINK = f"s3://eodata/MODIS/Test/{MODIS_KEY}/{MODIS_KEY}.hdf"
S3_URL = cdse_object(S3_LINK)[1]
RADAR_PREFIX = "S1A_IW_GRDH_1SDV_20240612T101408_20240612T101433_054289_069A97_"
MANIFEST = b"""<XFDU><familyName>SENTINEL-1</familyName><number>A</number><mode>IW</mode>
<orbitNumber type="start">54289</orbitNumber><missionDataTakeID>432791</missionDataTakeID>
<startTime>2024-06-12T10:14:08.898</startTime><stopTime>2024-06-12T10:14:33.897</stopTime>
<productType>GRD</productType><transmitterReceiverPolarisation>VV</transmitterReceiverPolarisation>
<transmitterReceiverPolarisation>VH</transmitterReceiverPolarisation></XFDU>"""
RADAR = RADAR_PREFIX + f"{binascii.crc_hqx(MANIFEST, 0xFFFF):04X}"


def modis_feature(*, odata=False, asset_url=S3_LINK):
    assets = {"data": {"href": asset_url, "roles": ["data"], "file:size": len(HDF),
                       "file:checksum": "1220" + hashlib.sha256(HDF).hexdigest()}}
    if odata:
        assets["Product"] = {"href": f"{DOWNLOAD_ROOT}/Products({UUID})/$value"}
    return {"id": MODIS_KEY, "collection": MODIS, "geometry": mapping(AOI),
            "properties": {"datetime": "2024-06-17T00:00:00Z", "end_datetime": "2024-06-24T23:59:59Z"}, "assets": assets}


def radar_feature(product=RADAR, *, folder=RADAR):
    raw = f"s3://{S1_BUCKET}/GRD/2024/6/12/IW/DV/{folder}"
    return {"id": product, "collection": "sentinel-1-grd", "geometry": mapping(AOI),
            "properties": {"datetime": "2024-06-12T10:14:08Z", "s1:product_identifier": product,
                           "sar:product_type": "GRD", "sar:instrument_mode": "IW", "sar:polarizations": ["VV", "VH"]},
            "assets": {"calibration-vv": {"href": raw+"/annotation/calibration/calibration-iw-vv.xml", "roles": ["metadata"]},
                       "manifest": {"href": raw+"/manifest.safe"}, "vv": {"href": raw+"/measurement/iw-vv.tiff", "roles": ["data"]}}}


def test_s3_sigv4_aws_published_get_vector():
    # AWS S3 API, "Examples: Signature Calculations in AWS Signature Version 4", GET Object.
    # https://docs.aws.amazon.com/AmazonS3/latest/API/sig-v4-header-based-auth.html
    result = _sign_get("https://examplebucket.s3.amazonaws.com/test.txt", {"Range": "bytes=0-9"},
                       "AKIAIOSFODNN7EXAMPLE", "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY", "us-east-1",
                       dt.datetime(2013, 5, 24, tzinfo=dt.timezone.utc))
    assert result["X-Amz-Date"] == "20130524T000000Z"
    assert result["Authorization"] == (
        "AWS4-HMAC-SHA256 Credential=AKIAIOSFODNN7EXAMPLE/20130524/us-east-1/s3/aws4_request,"
        "SignedHeaders=host;range;x-amz-content-sha256;x-amz-date,"
        "Signature=f0e8bdb87c964420e857bd35b5d6ed310bd44f0170aba48dd91039c6036bdb41"
    )


@pytest.mark.parametrize("path", ["s3://other/collection/object.tif", "s3://eodata@elsewhere/a/b", "s3://eodata/a/%2fbb.tif",
                                   "s3://eodata/a/%252e%252e/b", "s3://eodata/../x", "s3://eodata/a/x?query=1", "s3://eodata/a/%ff"])
def test_s3_rejects_ambiguous_addresses(path):
    assert cdse_object(path) is None
    if "?" not in path:
        assert download_asset(f"{DOWNLOAD_ROOT}/Products({UUID})/Nodes(product)/Nodes(%252f)/$value") is None


def test_s3_auth_only_signs_fixed_get():
    auth = CDSES3Auth()
    auth.connect(TEST_ACCESS, TEST_SECRET)
    for method, url in [("GET", "https://other.example.test/eodata/a/b"), ("PUT", S3_URL), ("GET", S3_URL+"?extra")]:
        with pytest.raises(UserError):
            requests.Request(method, url, auth=auth).prepare()
    assert auth.connected
    prepared = requests.Request("GET", S3_URL, headers={"Range": "bytes=5-", "If-Match": '"x"'}, auth=auth).prepare()
    assert "SignedHeaders=host;if-match;range;x-amz-content-sha256;x-amz-date" in prepared.headers["Authorization"]
    assert TEST_SECRET not in str(prepared.headers)
    auth.clear()
    with pytest.raises(UserError, match="密钥"):
        requests.Request("GET", S3_URL, auth=auth).prepare()


def test_s3_product_bound_single_file_and_odata_alias():
    options = {"mode": "original", "collection": MODIS}
    item = raw_item(modis_feature(), AOI, MODIS)
    auto = raw_plan(AOI, [item], options)
    assert auto["requires"] == ["cdse-s3"] and auto["targets"][0]["url"] == S3_URL
    assert item["raw"]["primary_s3"] == "data" and item["raw"]["uuid"] is None
    mixed = raw_item(modis_feature(odata=True), AOI, MODIS)
    assert mixed["raw"]["uuid"] == UUID
    odata = raw_plan(AOI, [mixed], options)
    assert odata["requires"] == ["odata"] and odata["fingerprint"] != auto["fingerprint"]
    manual = raw_plan(AOI, [mixed], {**options, "transport": "cdse-s3"})
    assert manual["requires"] == ["cdse-s3"] and manual["fingerprint"] != odata["fingerprint"]
    unrelated = raw_item(modis_feature(asset_url="s3://eodata/MODIS/other-product/data.hdf"), AOI, MODIS)
    assert not unrelated["raw"]["available"] and not unrelated["raw"]["assets"]
    with pytest.raises(UserError, match="没有"):
        raw_plan(AOI, [unrelated], options)


def test_multi_object_no_inferred_archive_and_interval_is_half_open():
    value = modis_feature()
    value["assets"]["quality"] = {"href": f"s3://eodata/MODIS/{MODIS_KEY}/quality.tif", "roles": ["data"]}
    row = raw_item(value, AOI, MODIS, dt.date(2024, 6, 23), dt.date(2024, 6, 24))
    assert row["raw"]["available"] and row["raw"]["primary_s3"] is None
    with pytest.raises(UserError, match="没有"):
        raw_plan(AOI, [row], {"collection": MODIS, "mode": "original", "transport": "cdse-s3"})
    assert raw_item(value, AOI, MODIS, dt.date(2024, 6, 16), dt.date(2024, 6, 17)) is None
    assert raw_item(value, AOI, MODIS, dt.date(2024, 6, 25), dt.date(2024, 6, 26)) is None


@pytest.fixture
def s3_provider(monkeypatch):
    calls = []
    def upstream(_, method, url, **kwargs):
        calls.append((method, url))
        if url == STAC_ROOT+"/collections":
            return StubResponse(data={"collections": [{"id": MODIS, "title": "MODIS L3"}]})
        if url == STAC_ROOT+"/search":
            assert not kwargs.get("auth") and not kwargs.get("headers")
            return StubResponse(data={"features": [modis_feature()]})
        assert url == S3_URL and method == "GET"
        assert urlsplit(url).hostname == CDSE_HOST
        prepared = requests.Request(method, url, headers=kwargs["headers"], auth=kwargs["auth"]).prepare()
        assert f"Credential={TEST_ACCESS}/" in prepared.headers["Authorization"]
        assert "/default/s3/aws4_request" in prepared.headers["Authorization"]
        return StubResponse(content=HDF, headers={"Content-Length": str(len(HDF)), "Content-Type": "application/octet-stream"})
    monkeypatch.setattr(HttpClient, "request", upstream)
    return calls


def test_s3_service_credentials_and_receipts(client, s3_provider, tmp_path):
    geom = client.post("aoi", {"coordinates": BBOX})["geometry"]
    criteria = {"collection": MODIS, "start": "2024-06-17", "end": "2024-06-18"}
    q = {"profile": "cdse-original", "geometry": geom}
    ticket = client.wait(client.post("search", {**q, **criteria})["job_id"])["result"]["query"]
    req = {**q, "criteria": criteria, "query_id": ticket["id"], "scene_keys": [MODIS_KEY],
           "options": {"mode": "original", "transport": "cdse-s3"}, "output_dir": str(tmp_path / "测试S3")}
    assert client.post("plan", req)["requires"] == ["cdse-s3"]
    assert "CDSE S3" in client.post("download", req, status=400)["error"]
    client.post("raw-connect", {"method": "token", "token": "example-bearer-token"})
    assert "CDSE S3" in client.post("download", req, status=400)["error"]
    client.post("s3-connect", {"access_key": "bad", "secret_key": "short"}, status=400)
    assert client.post("s3-connect", {"access_key": TEST_ACCESS, "secret_key": TEST_SECRET}) == {"connected": True, "verified": False, "saved": True}
    client.post("raw-disconnect", {})
    assert client.get("bootstrap").json()["s3_connected"]
    result = client.wait(client.post("download", req)["job_id"])["result"]
    path = Path(result["files"][0]["path"])
    assert path.read_bytes() == HDF and path.suffix == ".hdf"
    record_text = Path(result["manifest"]).read_text(encoding="utf-8")
    row, = json.loads(record_text)["items"]
    assert row["provider_checksum_verified"] == "SHA-256" and row["transport"] == "cdse-s3"
    assert TEST_SECRET not in record_text and TEST_ACCESS not in record_text
    assert all(TEST_SECRET not in p.read_text(encoding="utf-8") for p in tmp_path.rglob("preferences.json"))
    assert s3_provider.count(("GET", S3_URL)) == 1
    repeat = client.wait(client.post("download", req)["job_id"])["result"]
    cached, = json.loads(Path(repeat["manifest"]).read_text(encoding="utf-8"))["items"]
    assert cached["status"] == "skipped" and cached["sha256"] == row["sha256"]
    for field in ("size", "expected_bytes", "etag", "content_type", "format_validation", "provider_checksum_verified"):
        assert cached[field] == row[field], f"Cache reuse lost {field} from the new download record"
    assert s3_provider.count(("GET", S3_URL)) == 1
    assert client.post("s3-disconnect", {}) == {"connected": False, "saved": False}
    client.post("download", req, status=400)


@pytest.mark.parametrize("algorithm,notice", [("sha256", "SHA-256"), ("sha3_256", "SHA3-256"), ("md5", "MD5")])
def test_cached_native_file_checks_source_digest_even_if_local_receipt_changes(s3_provider, tmp_path, algorithm, notice):
    feature = modis_feature()
    digest = hashlib.new(algorithm, HDF, usedforsecurity=False).hexdigest()
    feature["assets"]["data"]["file:checksum"] = {"sha256": "1220", "sha3_256": "1620", "md5": "d50110"}[algorithm] + digest
    scene = raw_item(feature, AOI, MODIS)
    plan = raw_plan(AOI, [scene], {"collection": MODIS, "mode": "original", "transport": "cdse-s3"})
    auth = CDSES3Auth()
    auth.connect(TEST_ACCESS, TEST_SECRET)
    first = download_original(AOI, plan, tmp_path, None, s3_auth=auth)
    path = Path(first["files"][0]["path"])
    receipt, = (p for p in path.parent.glob("*.starmap.json") if not p.name.endswith(".zip.starmap.json"))
    changed = HDF.replace(b"fixture", b"changed")
    assert len(changed) == len(HDF)
    path.write_bytes(changed)
    local = json.loads(receipt.read_text(encoding="utf-8"))
    local["sha256"] = hashlib.sha256(changed).hexdigest()
    receipt.write_text(json.dumps(local), encoding="utf-8")
    with pytest.raises(UserError, match=notice):
        download_original(AOI, plan, tmp_path, None, s3_auth=auth)
    assert path.read_bytes() == changed
    assert s3_provider.count(("GET", S3_URL)) == 1


@pytest.mark.parametrize("code,notice", [("AccessDenied", "数据访问权限"), ("SignatureDoesNotMatch", "签名校验失败"),
                                         ("InvalidAccessKeyId", "已失效"), ("RequestTimeTooSkewed", "同步系统"),
                                         ("ExpiredToken", "已过期"), ("UnknownCode", "未授权")])
def test_s3_denial_distinguishes_official_codes_without_disclosing_error_xml(tmp_path, code, notice):
    payload = f"<Error xmlns='urn:s3'><Code>{code}</Code><Message>{TEST_ACCESS} {TEST_SECRET}</Message><Signature>private-server-signature</Signature></Error>".encode()
    response = StubResponse(403, content=payload)
    class Http:
        def request(self, method, url, **kwargs):
            assert method == "GET" and url == S3_URL
            return response
    item = {"name": MODIS_KEY + ".hdf", "url": S3_URL, "transport": "cdse-s3", "asset": "data", "bytes": len(HDF)}
    partial = tmp_path / "receive.part"
    auth = CDSES3Auth()
    auth.connect(TEST_ACCESS, TEST_SECRET)
    with pytest.raises(UserError, match=notice) as caught:
        _stream(Http(), None, item, partial, None, lambda *args: None, s3_auth=auth)
    assert TEST_ACCESS not in str(caught.value) and TEST_SECRET not in str(caught.value)
    assert "private-server-signature" not in str(caught.value)
    assert not partial.exists() and response.closed


@pytest.mark.parametrize("body", [b"not xml", b"<Error>" + b" " * 32768 + b"<Code>AccessDenied</Code></Error>"])
def test_s3_denial_malformed_or_oversized_body_uses_safe_fallback(tmp_path, body):
    response = StubResponse(403, content=body)
    class Http:
        def request(self, method, url, **kwargs):
            return response
    item = {"name": MODIS_KEY + ".hdf", "url": S3_URL, "transport": "cdse-s3", "asset": "data", "bytes": len(HDF)}
    auth = CDSES3Auth()
    auth.connect(TEST_ACCESS, TEST_SECRET)
    with pytest.raises(UserError, match="未授权"):
        _stream(Http(), None, item, tmp_path / "receive.part", None, lambda *args: None, s3_auth=auth)
    assert response.closed


class Interrupted(StubResponse):
    def iter_content(self, size):
        yield HDF[:12]
        raise requests.exceptions.ChunkedEncodingError("simulated break")


@pytest.mark.parametrize("status,range_override,tag_override,error", [
    (206, None, None, None), (206, "bytes 11-150/151", None, "长度或版本"),
    (206, None, '"new"', "长度或版本"), (412, None, None, "已变化"), (302, None, None, "重定向"),
])
def test_s3_resume_requires_exact_range_and_if_match(tmp_path, status, range_override, tag_override, error):
    auth = CDSES3Auth()
    auth.connect(TEST_ACCESS, TEST_SECRET)
    first = Interrupted(headers={"Content-Length": str(len(HDF)), "ETag": '"v1"'})
    second = StubResponse(status, content=HDF[12:], headers={"ETag": tag_override or '"v1"',
        "Content-Range": range_override or f"bytes 12-{len(HDF)-1}/{len(HDF)}", "Content-Length": str(len(HDF)-12),
        "Location": "https://untrusted.example.test/nope"})
    class Http:
        calls = 0
        def request(self, method, url, **kwargs):
            self.calls += 1
            assert method == "GET" and url == S3_URL
            sig = requests.Request(method, url, headers=kwargs["headers"], auth=kwargs["auth"]).prepare()
            if self.calls == 1:
                assert "Range" not in sig.headers
                return first
            assert self.calls == 2 and sig.headers["If-Match"] == '"v1"' and sig.headers["Range"] == "bytes=12-"
            assert "if-match;range" in sig.headers["Authorization"]
            return second
    item = {"name": MODIS_KEY+".hdf", "url": S3_URL, "transport": "cdse-s3", "asset": "data", "bytes": len(HDF)}
    http = Http()
    partial = tmp_path/"part"
    if error:
        with pytest.raises(UserError, match=error):
            _stream(http, None, item, partial, None, lambda *args: None, s3_auth=auth)
        assert partial.read_bytes() == HDF[:12]
    else:
        transfer = _stream(http, None, item, partial, None, lambda *args: None, s3_auth=auth)
        assert transfer["transfer_attempts"] == 2 and partial.read_bytes() == HDF
    assert http.calls == 2 and first.closed and second.closed


def test_provider_hash_mismatch_rejected(tmp_path):
    f = tmp_path/"broken-object"
    f.write_bytes(HDF)
    for item, etag, error in [({"provider_sha256": "0"*64}, None, "SHA-256"), ({}, "0"*32, "MD5")]:
        with pytest.raises(UserError, match=error):
            _validated_digests(f, item, etag, None)


def test_public_radar_does_not_cross_products_and_validates_exact_manifest(tmp_path):
    start, stop = dt.date(2024, 6, 12), dt.date(2024, 6, 13)
    other = RADAR_PREFIX + ("FFFF" if RADAR[-4:] != "FFFF" else "ABCD")
    assert public_radar_row(radar_feature(folder=other), AOI, .3, start, stop, "IW") is None
    assert public_radar_row(radar_feature(RADAR+"_COG"), AOI, .3, start, stop, "IW") is None
    assert public_radar_row(radar_feature(), AOI, .3, start, stop, "EW") is None
    row = public_radar_row(radar_feature(), AOI, .3, start, stop, "IW")
    plan = raw_plan(AOI, [row], {"mode": "metadata", "collection": "sentinel-1-grd", "transport": "public"})
    assert plan["requires"] == [] and plan["transports"] == ["public"]
    item, = plan["targets"]
    assert item["name"] == "manifest.safe" and item["key"] == RADAR
    f = tmp_path/"part"
    f.write_bytes(MANIFEST)
    assert _validate(f, item, None) == ".safe"
    with pytest.raises(UserError, match="CRC-16"):
        _validate(f, {**item, "key": other}, None)
    f.write_bytes(MANIFEST.replace(b"54289", b"54399"))
    with pytest.raises(UserError, match="轨道"):
        _validate(f, item, None)


def test_dem_full_tile_rejects_wrong_bounds_crs_and_resolution(tmp_path):
    tile = tmp_path/"fake.tif"
    area = (115., 39., 116., 40.)
    with rasterio.open(tile, "w", driver="GTiff", height=10, width=10, count=1, dtype="float32",
                       crs="EPSG:4326", transform=from_bounds(*area, 10, 10)) as ds:
        ds.write(np.ones((10, 10), dtype="float32"), 1)
    grid = {"epsg": 4326, "bounds": area, "height": 10, "dtype": "float32"}
    _check_raw_raster(tile, None, grid)
    for key, value in [("bounds", (116., 39., 117., 40.)), ("epsg", 32650), ("height", 11)]:
        with pytest.raises(UserError, match="瓦片"):
            _check_raw_raster(tile, None, {**grid, key: value})


@pytest.mark.browser
def test_ui_s3_requires_its_own_keys_and_discards_them(server, s3_provider, tmp_path):
    import os
    import re
    pw = pytest.importorskip("playwright.sync_api")
    with pw.sync_playwright() as driver:
        browser = driver.chromium.launch(channel=os.environ.get("STARMAP_TEST_BROWSER", "msedge"), headless=True)
        page = browser.new_page(viewport={"width": 1360, "height": 900})
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.route(re.compile(r"^https://"), lambda route: route.abort())
        open_data_page(page, server.origin)
        pw.expect(page.locator("#outputDirectory")).not_to_have_value("")
        page.locator("#sourceProfile").select_option("cdse-original")
        pw.expect(page.locator("#rawCollection option")).to_have_count(2)
        page.locator("#rawCollection").select_option(MODIS)
        page.locator('[data-tab="coordinates"]').click()
        page.locator("#coordinates").fill(BBOX)
        page.locator("#applyCoordinates").click()
        pw.expect(page.locator("#aoiName")).to_have_text("坐标研究区")
        page.locator("#startDate").fill("2024-06-17")
        page.locator("#endDate").fill("2024-06-18")
        page.locator("#searchScenes").click()
        pw.expect(page.locator("#scenesBody")).to_contain_text(MODIS_KEY)
        pw.expect(page.locator("#downloadEstimate")).to_contain_text("CDSE S3")
        pw.expect(page.locator("#downloadSelected")).to_be_disabled()
        page.locator("#manageAccounts").click()
        page.locator('[data-account="cdse"]').click()
        pw.expect(page.locator("#getS3KeyButton")).to_be_visible()
        page.locator("#s3AccessKey").fill(TEST_ACCESS)
        page.locator("#s3SecretKey").fill(TEST_SECRET)
        page.locator("#s3Connect").click()
        pw.expect(page.locator("#s3ConnectedPanel")).to_contain_text("下载时验证")
        pw.expect(page.locator("#s3AccessKey")).to_have_value("")
        pw.expect(page.locator("#s3SecretKey")).to_have_value("")
        page.locator("#accountComplete").click()
        page.locator("#rawTransport").select_option("odata")
        pw.expect(page.locator("#downloadEstimate")).to_contain_text("没有此通道")
        pw.expect(page.locator("#downloadSelected")).to_be_disabled()
        page.locator("#rawTransport").select_option("cdse-s3")
        destination = tmp_path / "S3实测界面模拟"
        page.locator("#outputDirectory").fill(str(destination))
        page.locator("#downloadSelected").click()
        pw.expect(page.locator("#planDetails")).to_contain_text("CDSE S3")
        page.locator("#confirmDownload").click()
        pw.expect(page.locator("#jobStatus")).to_have_text("下载完成", timeout=12000)
        assert next(destination.rglob("*.hdf")).read_bytes() == HDF
        page.locator("#manageAccounts").click()
        page.locator('[data-account="cdse"]').click()
        page.locator("#s3Disconnect").click()
        pw.expect(page.locator("#downloadSelected")).to_be_disabled()
        pw.expect(page.locator("#connectionStatus")).to_contain_text("需载入 CDSE S3")
        assert not errors
        browser.close()


def test_public_radar_service_separates_whole_safe_and_individual_asset(client, monkeypatch, tmp_path):
    from core.public_catalogue import EARTH_SEARCH
    calls = []
    def upstream(_, method, url, **kwargs):
        calls.append((method, url))
        if url == EARTH_SEARCH:
            assert kwargs["json"]["collections"] == ["sentinel-1-grd"]
            return StubResponse(data={"features": [radar_feature()]})
        assert method == "GET" and not kwargs.get("auth") and "Authorization" not in kwargs["headers"]
        assert url.endswith(f"/{RADAR}/manifest.safe")
        tag = hashlib.md5(MANIFEST, usedforsecurity=False).hexdigest()
        return StubResponse(content=MANIFEST, headers={"ETag": f'"{tag}"', "Content-Length": str(len(MANIFEST))})
    monkeypatch.setattr(HttpClient, "request", upstream)
    area = client.post("aoi", {"coordinates": BBOX})["geometry"]
    criteria = {"start": "2024-06-12", "end": "2024-06-12", "radar_mode": "IW"}
    q = {"profile": "s1-aws-original", "geometry": area}
    ticket = client.wait(client.post("search", {**q, **criteria})["job_id"])["result"]["query"]
    req = {**q, "criteria": criteria, "query_id": ticket["id"], "scene_keys": [RADAR], "output_dir": str(tmp_path/"雷达")}
    assert "只提供" in client.post("plan", {**req, "options": {"mode": "original"}}, status=400)["error"]
    client.post("plan", {**req, "options": {"mode": "metadata", "transport": "odata"}}, status=400)
    client.post("plan", {**req, "criteria": {**criteria, "radar_mode": "EW"}}, status=400)
    plan = client.post("plan", req)
    assert plan["targets"][0]["name"] == "manifest.safe" and plan["requires"] == []
    complete = client.wait(client.post("download", req)["job_id"])["result"]
    assert Path(complete["files"][0]["path"]).read_bytes() == MANIFEST
    assert len(calls) == 2
