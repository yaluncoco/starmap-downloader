"""Whole-product Browser downloads, archive integrity and offline SAFE analysis."""
from __future__ import annotations

import copy
import hashlib
import io
import json
import os
import re
import threading
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

import numpy as np
import pytest
import rasterio
from shapely.geometry import mapping

from conftest import D1, D2, P1, P2, StubResponse, assert_download_prefix, serve, AppClient
from core import monthly_scenes
from core.errors import UserError
from core.http import CATALOGUE_URL, HttpClient, check_cancel
from core.native_catalogue import DOWNLOAD_ROOT, STAC_ROOT
from core.planning import RESEARCH_BANDS
from core.research_bundle import NODATA
from core.safe_zip import PROFILE, check_zip_target, verify_safe_archive
from scripts.browser_flow import open_data_page
from test_monthly import OPTIONS, prepare, selection
from test_monthly_scenes import compare_complete_month_and_clip
from test_native_mosaic import AOI, native_fixture

UUIDS = ("59fdc3e6-a362-4bbd-99d1-e7dd038f5052", "e23ac462-d702-47c3-b276-280fcf435fdc")
TOKEN = "temporary-zip-fixture-token"


def archive(members):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_STORED) as bundle:
        for name, body in members.items():
            bundle.writestr(name, body)
    return output.getvalue()


def safe_members(paths, product, acquired):
    """Real small rasters and XML; rebuild the SAFE inventory in standard directories."""
    granule = f"GRANULE/L2A_T50TMK_A001_{product.split('_')[2]}/"
    members, aliases = {}, {}
    for alias, path in paths.items():
        if alias == "safe_manifest":
            continue
        if alias == "product_metadata":
            name = "MTD_MSIL2A.xml"
            root = ET.fromstring(path.read_bytes())
            root.find("PRODUCT_URI").text = product
            body = ET.tostring(root, encoding="utf-8")
        elif alias == "granule_metadata":
            name = granule + "MTD_TL.xml"
            root = ET.fromstring(path.read_bytes())
            root.find("SENSING_TIME").text = acquired
            root.find("TILE_ID").text = f"L2A_T50TMK_A001_{product.split('_')[2]}"
            body = ET.tostring(root, encoding="utf-8")
        else:
            resolution = alias.split("_")[1]
            name = granule + f"IMG_DATA/R{resolution}/T50TMK_{product.split('_')[2]}_{alias}.jp2"
            body = path.read_bytes()
        aliases[alias] = name
        members[product + "/" + name] = body
    # Include an unselected auxiliary file: ZIP-only must preserve it too.
    members[product + "/AUX_DATA/auxiliary.bin"] = b"complete product auxiliary payload"
    root = ET.Element("XFDU")
    for index, (name, body) in enumerate(members.items()):
        stream = ET.SubElement(ET.SubElement(root, "dataObject", ID=str(index)), "byteStream", size=str(len(body)))
        ET.SubElement(stream, "fileLocation", href="./" + name[len(product) + 1:])
        ET.SubElement(stream, "checksum", checksumName="SHA3-256").text = hashlib.sha3_256(body).hexdigest()
    members[product + "/manifest.safe"] = ET.tostring(root, encoding="utf-8")
    aliases["safe_manifest"] = "manifest.safe"
    return members, aliases


class ZipProvider:
    """Accept catalogue and exact full-product GET only; reject all pixel APIs and Nodes."""
    def __init__(self, paths):
        self.features, self.members, self.payloads = [], {}, {}
        self.calls, self.archive_gets, self.ranges = [], [], []
        self.pause, self.redirect, self.changed_tag = False, False, False
        self.started = threading.Event()
        for product, acquired, identifier in zip((P1, P2), (D1, D2), UUIDS):
            members, aliases = safe_members(paths, product, acquired)
            self.members[identifier] = members
            self.payloads[identifier] = archive(members)
            self.features.append({
                "id": product, "collection": "sentinel-2-l2a", "geometry": mapping(AOI),
                "properties": {"datetime": acquired, "eo:cloud_cover": 0.04},
                "assets": {
                    "product": {"href": f"{DOWNLOAD_ROOT}/Products({identifier})/$value", "roles": ["archive"]},
                    **{alias: {
                        "href": f"{DOWNLOAD_ROOT}/Products({identifier})/Nodes({product})/Nodes({Path(name).name})/$value",
                        "roles": ["metadata" if alias.endswith("metadata") or alias == "safe_manifest" else "data"],
                        "file:size": len(members[product + "/" + name])} for alias, name in aliases.items()}}})

    def request(self, method, url, *, cancel=None, **kwargs):
        check_cancel(cancel)
        headers = kwargs.get("headers", {})
        self.calls.append((method, url))
        if url == STAC_ROOT + "/search":
            assert method == "POST" and "Authorization" not in headers and "auth" not in kwargs
            return StubResponse(data={"features": copy.deepcopy(self.features), "links": []})
        match = re.fullmatch(re.escape(CATALOGUE_URL) + r"\(([^)]+)\)", url)
        if match:
            assert method == "GET" and "Authorization" not in headers
            identifier = match[1]
            product = next(row["id"] for row in self.features if identifier in row["assets"]["product"]["href"])
            # The catalogue checksum/length describe the directory product, not a dynamically packed ZIP.
            return StubResponse(data={"Id": identifier, "Name": product, "Online": True,
                "ContentLength": 999999999, "Checksum": [{"Algorithm": "MD5", "Value": "0" * 32}]})
        match = re.fullmatch(r"https://(download|zipper)\.dataspace\.copernicus\.eu/odata/v1/Products\(([^)]+)\)/\$value", url)
        assert match, f"ZIP flow attempted a member, processing or foreign request: {method} {url}"
        assert method == "GET" and headers.get("Authorization") == "Bearer " + TOKEN
        assert headers.get("Accept") == "application/octet-stream" and headers.get("Accept-Encoding") == "identity"
        identifier = match[2]
        if self.redirect and match[1] == "download":
            return StubResponse(302, headers={"Location": url.replace("://download.", "://zipper.")})
        self.archive_gets.append(identifier)
        body = self.payloads[identifier]
        tag = '"zip-object-v2"' if self.changed_tag else '"zip-object-v1"'
        start = int(headers["Range"].split("=")[1].rstrip("-")) if "Range" in headers else 0
        if start:
            assert headers.get("If-Range") == '"zip-object-v1"'
            self.ranges.append(start)
            return StubResponse(206, content=body[start:], headers={"Content-Length": str(len(body) - start),
                "Content-Range": f"bytes {start}-{len(body) - 1}/{len(body)}", "ETag": tag,
                "Content-Type": "application/zip"})
        if self.pause:
            started = self.started
            class Paused(StubResponse):
                def iter_content(self, size):
                    yield body[:65536]
                    started.set()
                    assert cancel.wait(6)
                    check_cancel(cancel)
            cls = Paused
        else:
            cls = StubResponse
        return cls(content=body, headers={"Content-Length": str(len(body)), "ETag": tag,
                                         "Content-Type": "application/zip"})


@pytest.fixture
def zip_provider(native_fixture, provider, monkeypatch):
    upstream = ZipProvider(native_fixture[2])
    monkeypatch.setattr(HttpClient, "request", lambda _, *args, **kwargs: upstream.request(*args, **kwargs))
    return upstream


def zip_search(client, tmp_path, *, monthly=False, mode="original"):
    client.post("raw-connect", {"method": "token", "token": TOKEN})
    area = client.post("aoi", {"geometry": json.loads(json.dumps(mapping(AOI.buffer(-.001))))})
    base = {"geometry": area["geometry"], "profile": PROFILE, "max_cloud": 100}
    if monthly:
        found = client.wait(client.post("monthly-search", {**base, "start_month": "2024-06",
            "end_month": "2024-06"})["job_id"])["result"]["monthly"]
        request = selection(area, found, tmp_path)
        request["profile"] = PROFILE
        request["months"][0].update(scene_keys=[row["key"] for row in found["groups"][0]["scenes"]],
                                   options={"mode": mode} if mode == "original" else OPTIONS)
    else:
        result = client.wait(client.post("search", {**base, "start": "2024-06-01",
            "end": "2024-06-10"})["job_id"])["result"]
        request = {**base, "query_id": result["query"]["id"], "criteria": result["query"],
            "scene_keys": [row["key"] for row in result["scenes"]],
            "output_dir": str(tmp_path / "zip-output"), "options": {"mode": mode} if mode == "original" else OPTIONS}
    return area, prepare(client, request)


@pytest.mark.parametrize("monthly", [False, True])
def test_default_zip_saves_exact_one_product_file_and_never_starts_local_processing(
        client, zip_provider, monkeypatch, tmp_path, monthly):
    _, (request, plan) = zip_search(client, tmp_path, monthly=monthly)
    assert plan["requires"] == ["odata"] and plan["pipeline"] == "download-zip"
    assert (plan["archive_only"] if monthly else plan["archive_count"] == 2)
    def forbidden(*args, **kwargs):
        raise AssertionError("ZIP-only download started mosaic, crop or extraction")
    monkeypatch.setattr(monthly_scenes, "download_mosaic", forbidden)
    monkeypatch.setattr(monthly_scenes, "prepare_zip_source", forbidden)
    route = "monthly-download" if monthly else "download"
    saved = client.wait(client.post(route, request)["job_id"])["result"]
    assert len(saved["files"]) == 2 and zip_provider.archive_gets == list(UUIDS)
    for file, identifier, product in zip(saved["files"], UUIDS, (P1, P2), strict=True):
        path = Path(file["path"])
        assert path.name == product + ".zip" and path.read_bytes() == zip_provider.payloads[identifier]
        receipt = json.loads(path.with_suffix(".starmap.json").read_bytes())
        evidence = receipt["safe_zip_validation"]
        assert receipt["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
        assert not receipt["provider_checksum_verified"] and receipt["source_checksums"] == {}
        assert evidence["zip_files_crc_verified"] == len(zip_provider.members[identifier])
        assert evidence["declared_members_checksum_verified"] == len(zip_provider.members[identifier]) - 1
        with zipfile.ZipFile(path) as bundle:
            assert bundle.read(product + "/AUX_DATA/auxiliary.bin") == b"complete product auxiliary payload"
    assert not list(Path(request["output_dir"]).rglob("*.tif"))
    before = len(zip_provider.calls)
    again = client.wait(client.post(route, request)["job_id"])["result"]
    assert all(file["status"] == "skipped" for file in again["files"]) and len(zip_provider.calls) == before


@pytest.mark.parametrize("monthly", [False, True])
def test_all_selected_zip_downloads_finish_before_offline_native_calibration_and_saved_image_crop(
        client, zip_provider, native_fixture, monkeypatch, tmp_path, monthly):
    area, (request, plan) = zip_search(client, tmp_path, monthly=monthly, mode="research")
    assert plan["pipeline"] == monthly_scenes.PIPELINE
    assert not zip_provider.archive_gets
    stages = []
    for name in ("prepare_zip_source", "download_mosaic"):
        original = getattr(monthly_scenes, name)
        def offline(*args, _original=original, _name=name, **kwargs):
            assert zip_provider.archive_gets == list(UUIDS)
            stages.append(_name)
            with monkeypatch.context() as scope:
                scope.setattr(HttpClient, "request", lambda *a, **k: pytest.fail("Local SAFE processing made an HTTP request"))
                return _original(*args, **kwargs)
        monkeypatch.setattr(monthly_scenes, name, offline)
    result = client.wait(client.post("monthly-download" if monthly else "download", request)["job_id"])["result"]
    month = result["months"][0]["result"] if monthly else result
    assert stages == ["prepare_zip_source"] * 2 + ["download_mosaic"] * 2
    assert len(month["files"]) == 1
    document = json.loads(Path(month["manifest"]).read_bytes())
    assert all(stage["status"] == "completed" for stage in document["stages"])
    assert len(document["sources"]) == 2 and all(len(source["files"]) == 1 for source in document["sources"])
    full = month["whole_mosaic"]["files"][0]
    _, _, _, inputs, _, _, origin = native_fixture
    with rasterio.open(full["bundle"]["unmasked"]) as raw, rasterio.open(full["bundle"]["source"]) as owner, \
            rasterio.open(full["bundle"]["scl"]) as quality, rasterio.open(full["path"]) as main:
        x = np.broadcast_to((raw.transform.c + (np.arange(raw.width) + .5) * 10)[None], raw.shape)
        y = np.broadcast_to((raw.transform.f - (np.arange(raw.height) + .5) * 10)[:, None], raw.shape)
        observed = owner.read(1)
        expected = []
        for band in RESEARCH_BANDS:
            dn, resolution = inputs[band]
            col = np.floor((x - origin[0]) / resolution).astype(int)
            row = np.floor((origin[1] - y) / resolution).astype(int)
            valid = (col >= 0) & (row >= 0) & (col < dn.shape[1]) & (row < dn.shape[0]) & (observed != 0)
            values = dn[np.clip(row, 0, dn.shape[0] - 1), np.clip(col, 0, dn.shape[1] - 1)]
            reflectance = (values.astype(np.float32) - np.float32(1000)) / np.float32(10000)
            reflectance[~valid | (values == 0)] = NODATA
            expected.append(reflectance)
        expected = np.stack(expected)
        np.testing.assert_array_equal(raw.read(), expected)
        assert np.any((expected[0] < 0) & (expected[0] != NODATA))
        assert np.any((expected[4] == NODATA) & (expected[0] != NODATA))
        masked = expected.copy()
        valid = (expected != NODATA).all(axis=0) & np.isin(quality.read(1), (2, 4, 5, 6, 7, 11))
        masked[:, ~valid] = NODATA
        np.testing.assert_array_equal(main.read(), masked)
    from shapely.geometry import shape
    compare_complete_month_and_clip(month, shape(area["geometry"]))
    final = month["files"][0]
    with rasterio.open(final["path"]) as main, rasterio.open(final["bundle"]["unmasked"]) as raw, \
            rasterio.open(final["bundle"]["scl"]) as quality:
        expected = raw.read()
        valid = (expected != NODATA).all(axis=0) & np.isin(quality.read(1), (2, 4, 5, 6, 7, 11))
        expected[:, ~valid] = NODATA
        np.testing.assert_array_equal(main.read(), expected)
    products = list(Path(month["folder"]).rglob("B02_10m.jp2"))
    assert len(products) == 2
    cached = products[0]
    data = bytearray(cached.read_bytes()); data[-1] ^= 1
    cached.write_bytes(data)
    before = len(zip_provider.calls)
    again = client.wait(client.post("monthly-download" if monthly else "download", request)["job_id"])["result"]
    assert len(zip_provider.calls) == before
    assert cached.read_bytes() != bytes(data)  # Damaged local member was rebuilt from the saved ZIP.
    again = again["months"][0]["result"] if monthly else again
    assert again["files"][0]["status"] == "skipped"
    compare_complete_month_and_clip(again, shape(area["geometry"]))


def test_crc_correct_but_false_safe_manifest_hash_is_rejected_before_publication(client, zip_provider, tmp_path):
    _, (request, _) = zip_search(client, tmp_path)
    members = copy.deepcopy(zip_provider.members[UUIDS[0]])
    name = next(name for name in members if name.endswith("_B02_10m.jp2"))
    changed = bytearray(members[name]); changed[-1] ^= 1
    members[name] = bytes(changed)
    zip_provider.payloads[UUIDS[0]] = archive(members)  # Repacking updates ZIP CRC; original SAFE checksum stays.
    result = client.wait(client.post("download", request)["job_id"], "failed")
    assert "清单校验" in result["error"] and not list(Path(request["output_dir"]).rglob("*.zip"))


def test_cancel_local_extraction_keeps_all_zips_and_retry_needs_no_remote_data(
        client, zip_provider, monkeypatch, tmp_path):
    from core import safe_zip
    _, (request, _) = zip_search(client, tmp_path, mode="research")
    original = safe_zip._copy_member
    calls = []
    def cancel_extract(bundle, member, path, cancel):
        calls.append(member)
        if len(calls) == 2:
            cancel.set()
        return original(bundle, member, path, cancel)
    monkeypatch.setattr(safe_zip, "_copy_member", cancel_extract)
    identifier = client.post("download", request)["job_id"]
    client.wait(identifier, "cancelled")
    assert zip_provider.archive_gets == list(UUIDS)
    assert len(list(Path(request["output_dir"]).rglob("*.zip"))) == 2
    assert not list(Path(request["output_dir"]).rglob("*.tif"))
    assert not list(Path(request["output_dir"]).rglob("*.part"))
    monkeypatch.setattr(safe_zip, "_copy_member", original)
    before = len(zip_provider.calls)
    result = client.wait(client.post("retry-download", {"job_id": identifier})["job_id"])["result"]
    assert len(result["files"]) == 1 and len(zip_provider.calls) == before


@pytest.mark.parametrize("problem", ["product", "missing", "crc", "duplicate", "traversal"])
def test_archive_inventory_and_product_identity_fail_closed(client, zip_provider, tmp_path, problem):
    _, (_, plan) = zip_search(client, tmp_path)
    target = plan["targets"][0]
    members = copy.deepcopy(zip_provider.members[UUIDS[0]])
    if problem == "product":
        name = P1 + "/MTD_MSIL2A.xml"
        members[name] = members[name].replace(P1.encode(), P2.encode())
    elif problem == "missing":
        del members[next(name for name in members if name.endswith("_B02_10m.jp2"))]
    elif problem == "traversal":
        members[P1 + "/../foreign.txt"] = b"foreign"
    data = archive(members)
    if problem == "crc":
        data = data.replace(b"complete product auxiliary payload", b"Complete product auxiliary payload")
    elif problem == "duplicate":
        output = io.BytesIO(data)
        with pytest.warns(UserWarning, match="Duplicate"):
            with zipfile.ZipFile(output, "a") as bundle:
                bundle.writestr(P1 + "/manifest.safe", members[P1 + "/manifest.safe"])
        data = output.getvalue()
    path = tmp_path / "invalid.zip"
    path.write_bytes(data)
    with pytest.raises(UserError, match="CRC 或清单校验"):
        verify_safe_archive(path, target)


@pytest.mark.parametrize("field,value", [("uuid", UUIDS[1]), ("name", P2), ("transport", "public"),
    ("url", "https://other.invalid/archive.zip"), ("url", f"{DOWNLOAD_ROOT}/Products({UUIDS[0]})/$zip"),
    ("asset", "B02_10m")])
def test_zip_download_contract_cannot_be_changed_to_another_product_or_channel(client, zip_provider, tmp_path, field, value):
    _, (_, plan) = zip_search(client, tmp_path)
    target = {**plan["targets"][0], field: value}
    with pytest.raises(UserError, match="整景 ZIP"):
        check_zip_target(target)


def test_official_zipper_redirect_keeps_exact_product_and_filename(client, zip_provider, tmp_path):
    _, (request, _) = zip_search(client, tmp_path)
    zip_provider.redirect = True
    result = client.wait(client.post("download", request)["job_id"])["result"]
    assert len(result["files"]) == 2 and zip_provider.archive_gets == list(UUIDS)


@pytest.mark.parametrize("changed_version", [False, True])
def test_cancelled_zip_resumes_frozen_selection_after_server_restart_and_rejects_changed_version(
        client, server, zip_provider, tmp_path, changed_version):
    _, (request, _) = zip_search(client, tmp_path)
    zip_provider.pause = True
    identifier = client.post("download", request)["job_id"]
    assert zip_provider.started.wait(4)
    client.post("cancel", {"job_id": identifier})
    client.wait(identifier, "cancelled")
    partial, = Path(request["output_dir"]).rglob("*.part")
    assert_download_prefix(partial, zip_provider.payloads[UUIDS[0]])
    assert partial.stat().st_size == 65536
    assert not list(Path(request["output_dir"]).rglob("*.zip"))
    zip_provider.pause = False
    zip_provider.changed_tag = changed_version
    with serve(server.service.config_dir) as restarted:
        cold = AppClient(restarted.origin)
        try:
            cold.post("raw-connect", {"method": "token", "token": TOKEN})
            cold.post("aoi", {"coordinates": "120,30,120.01,30.01"})
            job = cold.wait(cold.post("retry-download", {"job_id": identifier})["job_id"],
                            "failed" if changed_version else "completed")
            assert zip_provider.ranges == [65536]
            if changed_version:
                assert "版本已变化" in job["error"] and not list(Path(request["output_dir"]).rglob("*.zip"))
            else:
                assert len(job["result"]["files"]) == 2
                assert Path(job["result"]["files"][0]["path"]).read_bytes() == zip_provider.payloads[UUIDS[0]]
                assert not list(Path(request["output_dir"]).rglob("*.part"))
        finally:
            cold.session.close()


@pytest.mark.browser
def test_fresh_edge_defaults_to_whole_zip_and_can_switch_local_analysis_and_monthly_plan(
        server, client, zip_provider, tmp_path):
    pw = pytest.importorskip("playwright.sync_api")
    client.post("raw-connect", {"method": "token", "token": TOKEN})
    errors = []
    with pw.sync_playwright() as driver:
        browser = driver.chromium.launch(channel=os.environ.get("STARMAP_TEST_BROWSER", "msedge"), headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 960})
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.route(re.compile(r"^https://"), lambda route: route.abort())
        open_data_page(page, server.origin)
        pw.expect(page.locator("#sourceProfile")).to_have_value(PROFILE)
        pw.expect(page.locator("#zipProcessing")).to_have_value("original")
        page.locator('[data-tab="coordinates"]').click()
        page.locator("#coordinates").fill("116.350,39.900,116.355,39.905")
        page.locator("#applyCoordinates").click()
        page.locator("#timeMode").select_option("dates")
        page.locator("#startDate").fill("2024-06-01")
        page.locator("#endDate").fill("2024-06-10")
        page.locator("#outputDirectory").fill(str(tmp_path / "browser-zips"))
        page.locator("#searchScenes").click()
        page.wait_for_function("()=>!state.busy&&state.plan?.pipeline==='download-zip'", timeout=15000)
        page.locator("#downloadSelected").click()
        pw.expect(page.locator("#planDetails")).to_contain_text("完整原始 SAFE ZIP")
        pw.expect(page.locator("#planNote")).to_contain_text("SAFE 清单成员校验值")
        assert "预计请求数量" not in page.locator("#planDetails").inner_text()
        page.locator("#confirmDownload").click()
        pw.expect(page.locator("#jobStatus")).to_have_text("下载完成", timeout=15000)
        page.locator("#zipProcessing").select_option("analysis")
        page.wait_for_function("()=>!state.busy&&state.plan?.kind==='monthly-scenes'", timeout=15000)
        pw.expect(page.locator("#downloadEstimate")).to_contain_text("在本地处理")
        assert "次请求" not in page.locator("#downloadEstimate").inner_text()
        page.locator("#downloadSelected").click()
        pw.expect(page.locator("#planDetails")).to_contain_text("本地完整拼接")
        assert "预计请求数量" not in page.locator("#planDetails").inner_text()
        page.locator("#downloadDialog [data-close]").first.click()
        page.locator("#zipProcessing").select_option("original")
        page.locator("#timeMode").select_option("monthly")
        page.locator("#startMonth").fill("2024-06")
        page.locator("#endMonth").fill("2024-06")
        page.locator("#searchScenes").click()
        page.wait_for_function("()=>!state.busy&&state.plan?.archive_only===true", timeout=15000)
        pw.expect(page.locator("#downloadEstimate")).to_contain_text("个完整 ZIP")
        assert "每月一份拼接影像" not in page.locator("#downloadEstimate").inner_text()
        page.locator("#downloadSelected").click()
        pw.expect(page.locator("#planDetails")).to_contain_text("个完整 ZIP")
        assert not errors, errors
        browser.close()
