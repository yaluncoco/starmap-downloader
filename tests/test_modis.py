"""Fixture-based MODIS catalogue, transfer and end-to-end integrity tests."""
from __future__ import annotations

import base64
import copy
import datetime as dt
import hashlib
import json
import threading
import time
from pathlib import Path

import numpy as np
import pytest
import rasterio
import requests
from rasterio.crs import CRS
from rasterio.shutil import copy as raster_copy
from rasterio.transform import Affine
from requests.structures import CaseInsensitiveDict
from shapely.geometry import box, mapping

from conftest import assert_download_prefix
from core.errors import UserError
from core.http import HttpClient, check_cancel
from core.modis_source import COGS, HOST, SOURCE, granule, modis_object, object_url
from core.open_modis import PC_SEARCH, _overlap_tiles, check_modis_target, modis_row, verify_xml
from core.public_token import PC_TOKEN_ROOT, PlanetaryAccess
from core.raw_download import _asset_target, download_original, raw_plan
from core.service import Service

KEY = "MYD11A2.A2024177.h08v11.061.2024186040837"
REF = granule(KEY)
AOI = box(-109.42, -27.16, -109.39, -27.13)
COL = REF.collection
ON = "2024-06-25"
AFFINE = [926.6254331383342, 0, -11119505.197665, 0, -926.6254331391666, -2223901.039533]
SIN = CRS.from_string("+proj=sinu +R=6371007.181 +lon_0=0 +x_0=0 +y_0=0 +units=m +no_defs")
COG = "LST_Day_1km"
ETAG = '"azure-opaque-B1"'


def fixture(key=KEY, assets=("hdf", "metadata", COG), end="2024-07-02"):
    ref = granule(key)
    size, transform = [1200, 1200], AFFINE
    if ref.family == "43A4":
        size = [2400, 2400]
        transform = [463.3127165279165, 0, -16679257.795, 0, -463.3127165279167, 2223901.039333]
    return {"type": "Feature", "id": key, "collection": ref.collection, "geometry": mapping(AOI), "properties": {
        "datetime": None, "start_datetime": f"{ref.nominal}T00:00:00Z", "end_datetime": end+"T23:59:59Z",
        "modis:tile-id": ref.tile_id, "modis:horizontal-tile": ref.h, "modis:vertical-tile": ref.v,
        "proj:wkt2": SIN.to_wkt(), "proj:shape": size, "proj:transform": transform},
        "assets": {alias: {"href": object_url(ref, alias), **({"raster:bands": [{"data_type": "uint16", "scale": .02}]} if alias == COG else {})}
                   for alias in assets}}


def parse(item, date=ON):
    start = dt.date.fromisoformat(date)
    return modis_row(item, item["collection"], start, start+dt.timedelta(days=1), {(8, 11): 100.0, (3, 7): 85.0})


def xml_for(hdf: bytes, *, key=KEY):
    ref = granule(key)
    return f'''<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE GranuleMetaDataFile SYSTEM "https://never-load.invalid/granule.dtd">
<GranuleMetaDataFile><GranuleURMetaData>
<CollectionMetaData><ShortName>{ref.short}</ShortName><VersionID>61</VersionID></CollectionMetaData>
<ECSDataGranule><LocalGranuleID>{ref.filename}</LocalGranuleID></ECSDataGranule>
<DataFiles><DataFileContainer><DistributedFileName>{ref.filename}</DistributedFileName><FileSize>{len(hdf)}</FileSize><ChecksumType>MD5</ChecksumType><Checksum>{hashlib.md5(hdf).hexdigest()}</Checksum></DataFileContainer></DataFiles>
<RangeDateTime><RangeBeginningDate>{ref.nominal}</RangeBeginningDate><RangeEndingDate>2024-07-02</RangeEndingDate></RangeDateTime>
</GranuleURMetaData></GranuleMetaDataFile>'''.encode()


def make_cog(folder, *, wrong=None):
    raw = folder/"base.tif"
    cog = folder/"test-cog.tif"
    dtype = "uint16"
    nd = 999 if wrong == "nodata" else 0
    transform = AFFINE.copy()
    if wrong == "grid":
        transform[2] += 10
    with rasterio.open(raw, "w", driver="GTiff", crs="EPSG:4326" if wrong == "crs" else SIN, width=1200, height=1200, count=1,
                       dtype=dtype, nodata=nd, transform=Affine(*transform)) as ds:
        ds.scales = [.5 if wrong == "scale" else .02]
        label = ("8-day nighttime 1km grid Land-surface Temperature" if wrong == "layer" else "8-day daytime 1km grid Land-surface Temperature")
        ds.set_band_description(1, label)
        ds.update_tags(1, long_name=label)
        ds.update_tags(LOCALGRANULEID="elsewhere.hdf" if wrong == "granule" else REF.filename, VERSIONID="61", SHORTNAME=REF.short,
                       HORIZONTALTILENUMBER="08", VERTICALTILENUMBER="11", RANGEBEGINNINGDATE=ON,
                       RANGEENDINGDATE="2024-07-01" if wrong == "period" else "2024-07-02")
        a = np.full((1200, 1200), 15000, dtype=dtype)
        a[:2, :3] = 0
        ds.write(a, 1)
    raster_copy(raw, cog, driver="COG", compress="DEFLATE")
    return cog.read_bytes()


class Reply:
    def __init__(self, status=200, headers=None, content=b"", doc=None, generator=None):
        self.status_code = status
        self.headers = CaseInsensitiveDict(headers)
        self.content, self.doc, self.generator = content, doc, generator
        self.closed = False

    def close(self):
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def json(self):
        return self.doc

    def iter_content(self, size):
        if self.generator is not None:
            yield from self.generator()
            return
        for start in range(0, len(self.content), size):
            yield self.content[start:start+size]


class Archive:
    def __init__(self, cog):
        hdf = b"\x0e\x03\x13\x01" + b"fixture-only" * 80
        self.objects = {"hdf": hdf, "metadata": xml_for(hdf), COG: cog}
        self.features = [fixture()]
        self.calls, self.sas = [], []
        self.fault = None
        self.gate = threading.Event()
        self.pages = 0

    def request(self, method, url, *, headers=None, auth=None, cancel=None, json=None, **kwargs):
        self.calls.append((method, url, headers, json))
        check_cancel(cancel)
        if url == PC_SEARCH:
            assert method == "POST" and json["collections"] == [COL] and "intersects" not in json
            assert json["query"] == {"modis:tile-id": {"in": ["51008011"]}}
            if self.pages:
                self.pages += 1
                if "token" not in json:
                    return Reply(doc={"features": [], "links": [{"rel": "next", "href": url+"?collections="+COL, "method": "POST",
                                                                 "body": {"token": "page-2", "query": {"modis:tile-id": {"in": ["dummy"]}}}}]})
                assert json["token"] == "page-2"
            return Reply(doc={"features": self.features, "links": []})
        if url.startswith(PC_TOKEN_ROOT):
            assert method == "GET" and auth is None and kwargs["allow_redirects"] is False
            scope = url.rsplit("/", 1)[1]
            self.sas.append(scope)
            return Reply(doc={"token": f"sp=r&sr=c&sig=mock-{scope}", "msft:expiry": (dt.datetime.now(dt.timezone.utc)+dt.timedelta(hours=1)).isoformat()})
        assert url.startswith(f"https://{HOST}/") and kwargs["allow_redirects"] is False
        ref, layer = modis_object(url)
        assert ref == REF and headers["Accept-Encoding"] == "identity" and auth is not None
        expected_scope = COGS if layer == COG else SOURCE
        prepared = requests.Request(method, url).prepare()
        auth(prepared)
        assert prepared.url == url+f"?sp=r&sr=c&sig=mock-{expected_scope}"
        blob = self.objects[layer]
        digest = base64.b64encode(hashlib.md5(blob).digest()).decode()
        h = {"ETag": ETAG, "Content-MD5": digest, "Content-Type": "image/tiff" if layer == COG else "application/octet-stream", "Content-Length": str(len(blob))}
        focus = layer == COG
        if method == "HEAD":
            if focus and self.fault == "missing":
                return Reply(404)
            if focus and self.fault == "redirect-head":
                return Reply(301, {"Location": "https://never-follow.invalid/blob"})
            if focus and self.fault == "head-digest":
                h.pop("Content-MD5")
            if "If-Match" in headers:
                assert headers["If-Match"] == ETAG
            return Reply(headers=h)
        assert method == "GET" and headers["If-Match"] == ETAG
        if focus and self.fault == "wrong-get-size":
            h["Content-Length"] = str(len(blob)+1)
        if focus and self.fault == "changed":
            return Reply(412)
        if focus and self.fault == "redirect-get":
            return Reply(307, {"Location": "https://never-follow.invalid/blob"})
        if focus and self.fault == "tamper":
            return Reply(headers=h, content=blob[:-1]+bytes([blob[-1]^1]))
        if focus and self.fault in ("resume", "wrong-range", "range-digest", "cancel", "always-truncated"):
            if "Range" in headers:
                start = int(headers["Range"].removeprefix("bytes=").removesuffix("-"))
                h["x-ms-blob-content-md5"] = digest if self.fault != "range-digest" else base64.b64encode(b"x"*16).decode()
                h["Content-MD5"] = base64.b64encode(hashlib.md5(blob[start:]).digest()).decode()  # Not the full digest.
                h["Content-Range"] = f"bytes {start+(self.fault=='wrong-range')}-{len(blob)-1}/{len(blob)}"
                h["Content-Length"] = str(len(blob)-start)
                return Reply(206, h, blob[start:] if self.fault != "always-truncated" else b"")
            def interrupted():
                yield blob[:len(blob)//2]
                if self.fault == "cancel":
                    self.gate.set()
                    assert cancel.wait(5)
                    check_cancel(cancel)
                raise requests.exceptions.ChunkedEncodingError("synthetic connection loss")
            return Reply(headers=h, generator=interrupted)
        return Reply(headers=h, content=blob)


@pytest.fixture
def archive(tmp_path, monkeypatch):
    pc = Archive(make_cog(tmp_path))
    monkeypatch.setattr(HttpClient, "request", lambda self,*a,**k: pc.request(*a,**k))
    return pc


def selection(assets=(COG,), *, stac=None, mode="assets"):
    opts = {"collection": COL, "mode": mode, "transport": "pc-public", "assets": list(assets)}
    row = parse(fixture() if stac is None else stac)
    return raw_plan(AOI, [row], opts)


def download(tmp_path, plan=None, cancel=None):
    return download_original(AOI, selection() if plan is None else plan, str(tmp_path/"中文数据"), None, cancel=cancel)


def test_spatial_tile_filter_follows_the_sinusoidal_edge():
    assert _overlap_tiles(AOI) == {(8, 11): 100.0}
    assert _overlap_tiles(box(14.20,44.99,14.24,45.01)) == {(19, 4): 100.0}
    frac = _overlap_tiles(box(14.0,44.99,14.3,45.01))
    assert {(18,4),(19,4)} == set(frac) and sum(frac.values()) == 100


def test_nominal_not_interval_midpoint_or_overlap():
    row = parse(fixture())
    assert row["date"] == ON and row["time"] == "标称日"
    assert parse(fixture(), "2024-07-01") is None
    r = parse(fixture("MCD43A4.A2024182.h03v07.061.2024191035321", ("hdf",), "2024-07-07"), "2024-06-30")
    assert r["raw"]["assets"][0]["identity"]["catalogue_period"] == {"from": "2024-06-30", "through": "2024-07-07"}
    assert "2024-06-22" in r["raw"]["source_note"]
    for last in ("2024-12-31", "2025-01-02"):
        r = parse(fixture("MYD11A2.A2024361.h08v11.061.2025004063138", ("hdf",), last), "2024-12-26")
        assert r is not None
    assert parse(fixture("MYD11A2.A2024361.h08v11.061.2025004063138", ("hdf",), "2025-01-03"), "2024-12-26") is None


@pytest.mark.parametrize("key", [KEY.replace("061", "006"), KEY.replace("A2024177", "A2023366"), KEY.replace("A2024177", "A2024000"),
                                 KEY.replace("h08", "h36"), KEY.replace("MYD", "MCD"), KEY.replace("2024186040837", "2023186040837")])
def test_invalid_granule(key):
    with pytest.raises(ValueError):
        granule(key)


@pytest.mark.parametrize("change", ["?sig=injected", "#fragment", "/", ";test"])
def test_unsigned_exact_paths_and_read_only_scope(change):
    url = object_url(REF, COG)
    assert modis_object(url+change) is None
    auth = PlanetaryAccess(object(), scope="modis-source")
    with pytest.raises(UserError):
        auth(requests.Request("GET", url).prepare())
    with pytest.raises(UserError):
        auth(requests.Request("DELETE", object_url(REF, "hdf")).prepare())


def test_unsafe_asset_and_bound_metadata():
    bad = fixture()
    bad["assets"]["metadata"]["href"] += "?signature=wrong"
    row = parse(bad)
    assert [a["id"] for a in row["raw"]["assets"]] == [COG]
    target = _asset_target(row, row["raw"]["assets"][0], "pc-public")
    for prop in ("tile", "nominal_date", "version", "layer", "product", "catalogue_item"):
        t = copy.deepcopy(target)
        t["identity"][prop] = "wrong"
        with pytest.raises(UserError):
            check_modis_target(t)
    for prop, value in (("modis:horizontal-tile", 26), ("proj:shape", [1201, 1200]), ("proj:transform", [1,0,0,0,-1,0]), ("proj:wkt2", CRS.from_epsg(3857).to_wkt())):
        t = fixture(); t["properties"][prop] = value
        assert parse(t) is None


def test_valid_xml_and_reject_entities_or_wrong_id():
    obj = xml_for(b"x"*99)
    assert verify_xml(obj, REF)["hdf_bytes"] == 99
    for bad in (obj.replace(b"MYD11A2", b"MOD11A2"), obj.replace(b">61<", b">60<"), obj.replace(b">99<", b">x<"),
                obj.replace(b"SYSTEM", b"[<!ENTITY foo SYSTEM 'file:///fake'>]>\n<!DOCTYPE X SYSTEM")):
        with pytest.raises(UserError):
            verify_xml(bad, REF)


def test_https_blob_checksums_name_and_reuse(tmp_path, archive):
    plan = selection(("hdf", "metadata", COG))
    first = download(tmp_path, plan)
    assert {Path(f["path"]).name for f in first["files"]} == {REF.filename, REF.filename+".xml", KEY+"_"+COG+".tif"}
    raw = Path(first["manifest"]).read_text(encoding="utf-8")
    assert "sig=" not in raw and "mock-" not in raw
    rows = {r["asset"]:r for r in json.loads(raw)["items"]}
    assert rows[COG]["modis_validation"]["decoded_cells"] == 1200**2
    assert rows["hdf"]["modis_validation"]["nasa_xml_verified"]["hdf_md5"] == rows["hdf"]["azure_blob_md5"]
    assert sorted(archive.sas) == sorted([SOURCE, COGS])
    second = download(tmp_path, plan)
    assert all(f["status"] == "skipped" for f in second["files"])
    assert not list((tmp_path/"中文数据").rglob("*.part"))
    # A new bogus local SHA does not defeat the original provider digest.
    target = Path(first["files"][-1]["path"])
    head = target.with_suffix(".starmap.json")
    receipt = json.loads(head.read_bytes())
    content = target.read_bytes(); target.write_bytes(content[:-1]+bytes([content[-1]^1]))
    receipt["sha256"] = hashlib.sha256(target.read_bytes()).hexdigest()
    head.write_text(json.dumps(receipt), encoding="utf-8")
    with pytest.raises(UserError, match="MD5"):
        download(tmp_path, plan)
    target.unlink(); head.unlink()
    assert download(tmp_path, plan)["files"][-1]["status"] == "completed"


def test_hdf_without_optional_xml_visible_and_validated(tmp_path, archive):
    stac = fixture(assets=("hdf", COG))
    row = parse(stac)
    assert "无配套 XML" in row["raw"]["source_note"]
    assert selection(stac=stac, mode="original")["targets"][0]["asset"] == "hdf"
    with pytest.raises(UserError):
        selection(stac=stac, mode="metadata")
    result = download(tmp_path, selection(stac=stac, mode="original"))
    row, = json.loads(Path(result["manifest"]).read_bytes())["items"]
    assert row["modis_validation"]["nasa_xml_verified"] is None
    assert not any(u == object_url(REF,"metadata") for _, u, _, _ in archive.calls)


def test_hdf_xml_conflict_does_not_fall_back(tmp_path, archive):
    archive.objects["metadata"] = xml_for(b"0" * len(archive.objects["hdf"]))
    with pytest.raises(UserError, match="NASA XML"):
        download(tmp_path, selection(mode="original"))
    assert not list(tmp_path.rglob("*.hdf"))
    assert not any(m=="GET" and u==object_url(REF,"hdf") for m,u,_,_ in archive.calls)


@pytest.mark.parametrize("fault", ["missing", "redirect-head", "head-digest", "tamper", "changed", "redirect-get", "wrong-get-size", "wrong-range", "range-digest", "always-truncated"])
def test_transfer_fails_without_publishing(tmp_path, archive, fault):
    archive.fault = fault
    with pytest.raises(UserError):
        download(tmp_path)
    out = tmp_path/"中文数据"
    assert not any(p.suffix in (".tif", ".zip") or p.name.endswith(".starmap.json") for p in out.rglob("*"))
    for partial in out.rglob("*.part"):
        assert_download_prefix(partial, archive.objects[COG])
    manifest, = out.rglob("下载记录_*.json")
    assert json.loads(manifest.read_bytes())["status"] == "failed"


def test_safe_resume_uses_version_and_complete_digest(tmp_path, archive):
    archive.fault = "resume"
    result = download(tmp_path)
    row, = json.loads(Path(result["manifest"]).read_bytes())["items"]
    assert row["transfer_attempts"] == 2
    assert row["azure_blob_md5"] == hashlib.md5(archive.objects[COG]).hexdigest()
    assert Path(row["path"]).read_bytes() == archive.objects[COG]


@pytest.mark.parametrize("fault", ["layer", "granule", "scale", "grid", "crs", "period", "nodata"])
def test_wrong_cog_even_with_valid_transport_digest(tmp_path, archive, fault):
    archive.objects[COG] = make_cog(tmp_path, wrong=fault)
    with pytest.raises(UserError, match="COG"):
        download(tmp_path)
    assert not list((tmp_path/"中文数据").rglob("*.tif"))


def wait(service, job):
    for _ in range(400):
        result = service.job(job["job_id"])
        if result["status"] not in ("running", "cancelling"):
            return result
        time.sleep(.02)
    raise AssertionError("job did not stop")


def test_service_pagination_selection_and_cancel(tmp_path, archive):
    archive.pages = 1
    service = Service(tmp_path/"prefs.json")
    body = {"profile": COL, "start": ON, "end": ON, "geometry": mapping(AOI)}
    data = wait(service, service.search(body))["result"]
    assert archive.pages == 3 and [row["key"] for row in data["scenes"]] == [KEY]
    req = {**body, "query_id": data["query"]["id"], "scene_keys": [KEY], "criteria": body,
           "options": {"mode": "assets", "assets": [COG]}, "output_dir": str(tmp_path/"中文数据")}
    plan = service.plan(req)
    assert plan["requires"] == [] and plan["transports"] == ["pc-public"]
    with pytest.raises(UserError):
        service.download({**req, "plan_fingerprint": "stale"})
    with pytest.raises(UserError):
        service.plan({**req, "criteria": {**body, "end": "2024-06-26"}})
    with pytest.raises(UserError):
        service.plan({**req, "options": {"mode": "original", "transport": "cdse-s3"}})
    archive.fault = "cancel"
    job = service.download({**req, "plan_fingerprint": plan["fingerprint"]})
    assert archive.gate.wait(5)
    service.cancel(job["job_id"])
    assert wait(service, job)["status"] == "cancelled"
    output = tmp_path / "中文数据"
    assert not list(output.rglob("*.tif")) and not list(output.rglob("*.zip"))
    partial, = output.rglob("*.part")
    assert_download_prefix(partial, archive.objects[COG])
    archive.fault = None
    assert wait(service, service.download(req))["status"] == "completed"
    assert not list(output.rglob("*.part"))
