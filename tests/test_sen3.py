"""Exact SEN3 archive selection, format integrity, and all-or-nothing publication."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import h5py
import numpy as np
import pytest
import requests
from shapely.geometry import box, mapping

from conftest import AppClient, StubResponse, serve
from core import sen3_download
from core.errors import Cancelled, UserError
from core.http import HttpClient, check_cancel
from core.native_catalogue import STAC_ROOT
from core.open_sen3 import ARCHIVE, SAFE, SEN3, SEN3_PUBLIC, _resolve, parse_xfdu, sen3_row
from core.raw_download import raw_plan
from core.s3_source import SEN3_MANIFEST, SEN3_NAME, is_public_url, sen3_archive_parts
from core.sen3_download import download_sen3
from core.sen3_netcdf import verify_netcdf

COLLECTION = "sentinel-3-sl-2-lst-ntc"
AOI = box(116.350, 39.900, 116.355, 39.905)
DAY = dt.date(2024, 3, 5)
PRODUCT = "S3B_SL_2_LST____20240305T132901_20240305T133201_20240306T120123_0179_090_224_0540_PS2_O_NT_004.SEN3"
KEY = PRODUCT.removesuffix(".SEN3")
FIRST, LAST, ORBIT = "2024-03-05T13:29:01.065434Z", "2024-03-05T13:32:01.065434Z", 30526
PREFIX = "/".join(sen3_archive_parts(PRODUCT)) + "/"
TAG = '"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa-2"'
PROOF = {"product": PRODUCT, "orbit": ORBIT, "start_utc": FIRST, "end_utc": LAST}


def md5(data):
    return hashlib.md5(data, usedforsecurity=False).hexdigest()


def xfdu(files, proof=PROOF):
    def block(ident, body):
        return f'<metadataObject ID="{ident}"><metadataWrap><xmlData>{body}</xmlData></metadataWrap></metadataObject>'

    ident = proof["product"]
    _, platform_code, kind, *_ = sen3_archive_parts(ident)
    fields = SEN3_NAME.fullmatch(ident)
    relative, baseline = int(fields["relative"]), fields["baseline"]
    period = block("acquisitionPeriod", f"<s:acquisitionPeriod><s:startTime>{proof['start_utc']}</s:startTime><s:stopTime>{proof['end_utc']}</s:stopTime></s:acquisitionPeriod>")
    platform = block("platform", f"<s:platform><s:familyName>Sentinel-3</s:familyName><s:number>{platform_code[-1]}</s:number></s:platform>")
    orbit = block("measurementOrbitReference", f"<s:orbitReference><s:orbitNumber type='start'>{proof['orbit']}</s:orbitNumber>"
                  f"<s:relativeOrbitNumber type='start'>{relative}</s:relativeOrbitNumber></s:orbitReference>")
    product = block("generalProductInformation", f"<p:generalProductInformation><p:productName>{ident}</p:productName>"
                    f"<p:productType>{kind}</p:productType><p:timeliness>NT</p:timeliness><p:baselineCollection>{baseline}</p:baselineCollection></p:generalProductInformation>")
    objects = [f'<dataObject ID="D{i}"><byteStream mimeType="application/x-netcdf" size="{len(data)}">'
               f'<fileLocation locatorType="URL" href="./{name}"/><checksum checksumName="MD5">{md5(data)}</checksum>'
               f'</byteStream></dataObject>' for i, (name, data) in enumerate(files.items())]
    pointers = [f'<dataObjectPointer dataObjectID="D{i}"/>' for i in range(len(objects))]
    return (f'<xfdu:XFDU xmlns:xfdu="urn:ccsds:schema:xfdu:1" xmlns:s="{SAFE[1:-1]}" xmlns:p="{SEN3[1:-1]}">'
            f'<informationPackageMap>{"".join(pointers)}</informationPackageMap><metadataSection>{period}{platform}{orbit}{product}</metadataSection>'
            f'<dataObjectSection>{"".join(objects)}</dataObjectSection></xfdu:XFDU>').encode()


def listing(files, prefix=PREFIX, ending=""):
    nodes = [f'<Contents><Key>{prefix}{name}</Key><Size>{len(data)}</Size><ETag>{TAG}</ETag></Contents>' for name, data in files.items()]
    return (f'<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">'
            f'<Name>meeo-s3</Name><Prefix>{prefix}</Prefix><IsTruncated>false</IsTruncated>{"".join(nodes)}{ending}</ListBucketResult>').encode()


def feature(files, proof=PROOF, collection=COLLECTION):
    ident = proof["product"]
    _, sat, kind, year, month, day, _ = sen3_archive_parts(ident)
    fields = SEN3_NAME.fullmatch(ident)
    return {"id": ident.removesuffix(".SEN3"), "collection": collection, "geometry": mapping(AOI), "properties": {
        "platform": "sentinel-3" + sat[-1].lower(), "sat:absolute_orbit": proof["orbit"],
        "sat:relative_orbit": int(fields["relative"]), "product:type": kind,
        "product:timeliness_category": "NT", "datetime": proof["start_utc"],
        "start_datetime": proof["start_utc"], "end_datetime": proof["end_utc"]},
        "assets": {f"asset{i}": {"href": f"s3://eodata/Sentinel-3/SLSTR/{kind}/{year}/{month}/{day}/{ident}/{name}",
                                   "file:local_path": f"{ident}/{name}", "roles": ["data"], "file:size": float(len(data)),
                                   "file:checksum": "d50110" + md5(data), "type": "application/netcdf"}
                   for i, (name, data) in enumerate(files.items())}}


@pytest.fixture
def nc_files(tmp_path):
    def make(name, changes=None):
        path = tmp_path / name
        with h5py.File(path,"w") as nc:
            nc.attrs.update(product_name=PRODUCT, start_time=FIRST, stop_time=LAST, absolute_orbit_number=[ORBIT])
            if name == "LST_in.nc":
                nc["LST"] = np.arange(6, dtype="i2").reshape((2, 3))
                nc["exception"] = np.zeros((2, 3), dtype="u1")
            else:
                nc["time_stamp_i"] = np.arange(2, dtype="i8")
            if changes:
                changes(nc)
        return path.read_bytes()
    return make


def setup_archive(files, *, listed=None, xml=None):
    f = feature(files)
    xml = xfdu(files) if xml is None else xml
    parts = {SEN3_MANIFEST: xml, **files}
    archive_listing = listing(parts if listed is None else listed)
    return f, parts, archive_listing


def install_stub(monkeypatch, f, parts, archive_listing, response=None, *, prefix=PREFIX):
    calls = []

    def request(_self, method, url, **kwargs):
        calls.append((method, url, kwargs))
        if url == STAC_ROOT + "/search":
            assert kwargs["json"]["collections"] == [f["collection"]]
            return StubResponse(data={"features": [f], "links": []})
        if url == ARCHIVE:
            assert kwargs["params"]["prefix"] == prefix
            return StubResponse(content=archive_listing)
        assert url.startswith(ARCHIVE + prefix)
        name = url.removeprefix(ARCHIVE + prefix)
        assert kwargs.get("auth") is None and kwargs["headers"]["If-Match"] == TAG
        if response is not None:
            selected = response(name, kwargs, calls)
            if selected is not None:
                return selected
        body = parts[name]
        return StubResponse(content=body, headers={"ETag": TAG, "Content-Length": str(len(body))})

    monkeypatch.setattr(HttpClient, "request", request)
    return calls


def resolved(f):
    day = dt.date.fromisoformat(f["properties"]["start_datetime"][:10])
    row = sen3_row(f, AOI, f["collection"], day, day + dt.timedelta(days=1))
    http = HttpClient()
    try:
        return _resolve(row, http, None)
    finally:
        http.close()


def plan(row, **updates):
    return raw_plan(AOI, [row], {"mode": "original", "collection": row["raw"]["collection"], "transport": "public", **updates})


def test_allowlists_canonical_prefix_and_exact_members():
    good = ARCHIVE + PREFIX + "LST_in.nc"
    assert is_public_url(good)
    for variant in (good.replace("NTC/", "NRT/"), good.replace("/03/05/", "/05/03/"), good.replace("/S3B/", "/S3A/"),
                    good.replace("LST_in.nc", "%4cST_in.nc"), good.replace("LST_in.nc", "CON.nc"),
                    good.replace("LST_in.nc", "../LST_in.nc"), good+"?versionId=w", good.replace(".amazonaws.com", ".amazonaws.com.evil.test")):
        assert not is_public_url(variant)


@pytest.mark.parametrize("old,new", [
    (b'./LST_in.nc', b'../LST_in.nc'), (b'./LST_in.nc', b'/LST_in.nc'), (b'./LST_in.nc', b'./CON.nc'),
    (b'./time_in.nc', b'./lst_IN.nc'), (b'dataObject ID="D1"', b'dataObject ID="D0"'),
    (b'dataObjectID="D1"', b'dataObjectID="WRONG"'), (b'<p:baselineCollection>004', b'<p:baselineCollection>005'),
    (b'20240306T120123', b'20240306T120124'), (b'>30526<', b'>30527<'), (b'13:29:01.065434', b'13:28:01.065434'),
    (b'checksumName="MD5"', b'checksumName="SHA256"'), (b'<xfdu:XFDU', b'<!DOCTYPE x><xfdu:XFDU'),
])
def test_xfdu_rejects_ambiguous_identity_members_and_size(old, new):
    data = xfdu({"LST_in.nc": b"lst", "time_in.nc": b"time"})
    assert parse_xfdu(data, PROOF)["LST_in.nc"] == (3, md5(b"lst"))
    assert old in data
    with pytest.raises(UserError, match="清单"):
        parse_xfdu(data.replace(old, new, 1), PROOF)


@pytest.mark.parametrize("state", ["missing-version", "missing-member", "size-conflict", "digest-conflict", "undeclared", "wrong-list-prefix"])
def test_search_labels_archive_gaps_without_other_version(tmp_path, monkeypatch, nc_files, state):
    data = {name: nc_files(name) for name in ("LST_in.nc", "time_in.nc")}
    f, parts, _ = setup_archive(data)
    listed = parts.copy()
    if state == "missing-version":
        listed = {}
    elif state == "missing-member":
        listed.pop("time_in.nc")
    elif state == "size-conflict":
        listed["time_in.nc"] += b"more"
    elif state == "digest-conflict":
        f["assets"]["asset1"]["file:checksum"] = "d50110"+"a"*32
    elif state == "undeclared":
        f["assets"]["asset2"] = {**f["assets"]["asset1"], "href": f["assets"]["asset1"]["href"].replace("time_in.nc", "other.nc"),
                                "file:local_path": PRODUCT + "/other.nc"}
    calls = install_stub(monkeypatch, f, parts, listing(listed, prefix=PREFIX if state != "wrong-list-prefix" else "other/"))
    row = resolved(f)
    assert row["raw"]["sen3"]["complete"] is False
    with pytest.raises(UserError):
        plan(row)
    if state in {"missing-version", "wrong-list-prefix"}:
        assert row["raw"]["available"] is False and row["raw"]["unavailable_reason"]
        assert len(calls) == 1
    else:
        assert row["raw"]["available"] and plan(row, mode="asset", asset="LST_in.nc")["requests"] == 1
        aliases = [a["id"] for a in row["raw"]["assets"]]
        assert ("time_in.nc" in aliases) == (state == "undeclared")


def test_local_api_complete_partial_reuse_and_wrong_selection(tmp_path, monkeypatch, nc_files):
    data = {name: nc_files(name) for name in ("LST_in.nc", "time_in.nc")}
    f, parts, archive_listing = setup_archive(data)
    calls = install_stub(monkeypatch, f, parts, archive_listing)
    with serve(tmp_path / "settings") as server:
        api = AppClient(server.origin)
        boot = api.get("bootstrap").json()
        assert COLLECTION == boot["sen3_default"] and len(boot["sen3_collections"]) >= 8 and not boot["connected"]
        common = {"geometry": mapping(AOI), "profile": SEN3_PUBLIC}
        criteria = {"collection": COLLECTION, "start": DAY.isoformat(), "end": DAY.isoformat()}
        search = api.post("search", {**common, **criteria})
        result = api.wait(search["job_id"])["result"]
        choose = {**common, "criteria": criteria, "query_id": result["query"]["id"], "scene_keys": [KEY], "output_dir": str(tmp_path / "output")}
        api.post("plan", {**choose, "options": {"mode": "original", "transport": "odata"}}, status=400)
        api.post("plan", {**choose, "criteria": {**criteria, "collection": "sentinel-3-sl-2-lst-nrt"}}, status=400)
        api.post("plan", {**choose, "options": {"mode": "asset", "asset": "other.nc"}}, status=400)
        for complete in [True, False]:
            value = {**choose, "options": {"mode": "original"} if complete else {"mode":"assets", "assets":["LST_in.nc", "time_in.nc"]}}
            planned = api.post("plan", value)
            assert planned["requires"] == [] and planned["sen3_complete"] == complete and planned["requests"] == (3 if complete else 2)
            job = api.post("download", value)
            output = api.wait(job["job_id"])["result"]
            paths = [Path(row["path"]) for row in output["files"]]
            assert paths[0].parent.name == (PRODUCT if complete else KEY + "_selected_members")
            assert {p.name for p in paths} == set(parts if complete else data)
            assert all(p.read_bytes() == parts[p.name] for p in paths)
            record = json.loads(Path(output["manifest"]).read_text(encoding="utf-8"))
            assert record["status"] == "completed" and record["items"][-1]["sen3_validation"]["decoded_arrays"]
            n = len(calls)
            reused = api.wait(api.post("download", value)["job_id"])["result"]
            assert len(calls) == n and all(row["status"] == "skipped" for row in reused["files"])
            paths[-1].write_bytes(b"damaged")
            failed = api.wait(api.post("download", value)["job_id"], "failed")
            assert "已有 SEN3" in failed["error"] and paths[-1].read_bytes() == b"damaged"


@pytest.mark.parametrize("state", ["version", "manifest-drift", "hash", "product", "orbit", "invalid-array", "interrupt", "cancel", "list-incomplete"])
def test_group_failure_never_publishes_partial(tmp_path, monkeypatch, nc_files, state):
    changes = {
        "product": lambda d: d.attrs.update(product_name=PRODUCT.replace("120123", "120122")),
        "orbit": lambda d: d.attrs.update(absolute_orbit_number=[999]),
        "invalid-array": lambda d: d.__delitem__("time_stamp_i"),
    }
    data = {"LST_in.nc": nc_files("LST_in.nc"), "time_in.nc": nc_files("time_in.nc", changes.get(state))}
    f, parts, archive_listing = setup_archive(data)
    stop = threading.Event()
    attempts = []

    class Dropped(StubResponse):
        def iter_content(self, size):
            if state == "cancel":
                stop.set()
                yield b"partial"
            else:
                # Without partial bytes, retries must not append or fall back to another product.
                raise requests.exceptions.ChunkedEncodingError("mock interrupted transfer")

    def defect(name, kwargs, calls):
        if name == SEN3_MANIFEST and state == "manifest-drift" and len([c for c in calls if c[1].endswith(SEN3_MANIFEST)]) == 2:
            changed = parts[name].replace(b"LST", b"FOO", 1)
            return StubResponse(content=changed, headers={"ETag": TAG, "Content-Length": str(len(changed))})
        if name != "time_in.nc":
            return None
        attempts.append(kwargs["headers"].copy())
        if state in ("cancel", "interrupt"):
            return Dropped(headers={"ETag": TAG, "Content-Length": str(len(data[name]))})
        if state == "version":
            return StubResponse(status=412)
        if state == "hash":
            body = data[name][:-1] + bytes([data[name][-1] ^ 0x01])
            return StubResponse(content=body, headers={"ETag": TAG, "Content-Length": str(len(body))})
        return None

    install_stub(monkeypatch, f, parts, archive_listing, defect)
    prepared = plan(resolved(f))
    if state == "list-incomplete":
        prepared["targets"].pop()
    with pytest.raises(Cancelled if state == "cancel" else UserError):
        download_sen3(AOI, prepared, tmp_path / "failed-output", cancel=stop)
    published = (tmp_path / "failed-output") / prepared["folder_name"]
    assert not any(p.is_dir() or p.name.endswith(("starmap.json", ".part")) for p in published.iterdir())
    report = json.loads(next(published.glob("下载记录_*.json")).read_text(encoding="utf-8"))
    assert report["status"] == ("cancelled" if state == "cancel" else "failed") and report["items"] == []
    if state == "interrupt":
        assert len(attempts) == 4 and all(h["If-Match"] == TAG and "Range" not in h for h in attempts)


def test_dropped_member_resumes_with_same_pinned_version(tmp_path, monkeypatch, nc_files):
    name = "LST_in.nc"
    data = {name: nc_files(name)}
    f, parts, archive_listing = setup_archive(data)
    resumed = []
    offset = 100

    class FirstPart(StubResponse):
        def iter_content(self, size):
            yield data[name][:offset]
            raise requests.exceptions.ChunkedEncodingError("dropped test response")

    def respond(member, kwargs, _calls):
        if member != name:
            return None
        if "Range" not in kwargs["headers"]:
            return FirstPart(headers={"ETag": TAG, "Content-Length": str(len(data[name]))})
        resumed.append(kwargs["headers"].copy())
        return StubResponse(status=206, content=data[name][offset:], headers={"ETag": TAG, "Content-Length": str(len(data[name])-offset),
                             "Content-Range": f"bytes {offset}-{len(data[name])-1}/{len(data[name])}"})

    install_stub(monkeypatch, f, parts, archive_listing, respond)
    result = download_sen3(AOI, plan(resolved(f)), tmp_path / "resumed")
    assert resumed == [{"Accept-Encoding": "identity", "If-Match": TAG, "Range": f"bytes={offset}-"}]
    assert Path(result["files"][-1]["path"]).read_bytes() == data[name]
    assert json.loads(Path(result["manifest"]).read_text(encoding="utf-8"))["items"][-1]["transfer_attempts"] == 2


WST_COLLECTION = "sentinel-3-sl-2-wst-ntc"
WST = {
    "product": "S3A_SL_2_WST____20240228T010152_20240228T024251_20240229T082217_6059_109_273______MAR_O_NT_003.SEN3",
    "start_utc": "2024-02-28T01:01:51.887229Z", "end_utc": "2024-02-28T02:42:51.043294Z", "orbit": 41826,
}
WST_FILE = "20240228010152-MAR-L2P_GHRSST-SSTskin-SLSTRA-20240229082217-v02.0-fv01.0.nc"
SRA = {
    "product": "S3B_SR_1_SRA_A__20240305T120417_20240305T125447_20240329T141707_3029_090_223______PS2_O_NT_005.SEN3",
    "start_utc": "2024-03-05T12:04:16.906731Z", "end_utc": "2024-03-05T12:54:47Z", "orbit": 30525,
}


@pytest.fixture
def wst_file(tmp_path):
    path = tmp_path / WST_FILE
    with h5py.File(path, "w") as nc:
        nc.attrs.update(id="SLSTRA-MAR-L2P-v1.0", platform="Sentinel-3A", sensor="SLSTR", processing_level="L2P",
                        naming_authority="org.ghrsst", processing_baseline="SL__SST.003.00.00", product_version="1.0",
                        gds_version_id="2.0r5", date_created="20240229T082217Z", start_time="20240228T010151Z",
                        stop_time="20240228T024250Z", time_coverage_start="20240228T010151Z", time_coverage_end="20240228T024250Z")
        start = dt.datetime.fromisoformat("2024-02-28T01:01:51Z")
        seconds = int((start - dt.datetime(1981, 1, 1, tzinfo=dt.timezone.utc)).total_seconds())
        nc["time"] = np.asarray([seconds], dtype="i4")
        nc["time"].attrs["units"] = "seconds since 1981-01-01T00:00:00Z"
        for name, direction in (("lat", "north"), ("lon", "east")):
            nc[name] = np.zeros((4, 3), dtype="f4")
            nc[name].attrs["units"] = "degrees_" + direction
        for name in ("sea_surface_temperature", "quality_level", "l2p_flags"):
            nc.create_dataset(name, data=np.ones((1, 4, 3), dtype="i2"), chunks=(1, 2, 3), compression="gzip")
        nc["sea_surface_temperature"].attrs["standard_name"] = "sea_surface_skin_temperature"
    return path


@pytest.mark.parametrize("case", ["good", "filename", "platform", "version", "creation", "coverage", "time-array", "array-shape"])
def test_wst_uses_native_ghrsst_identity_and_scanline_shapes(wst_file, case):
    name = WST_FILE.replace("082217", "082218") if case == "filename" else WST_FILE
    changes = {"platform": {"platform": "Sentinel-3B"}, "version": {"product_version": "2.0"},
               "creation": {"date_created": "20240229T082218Z"},
               "coverage": {"start_time": "20240228T011151Z", "time_coverage_start": "20240228T011151Z"}}
    with h5py.File(wst_file, "r+") as nc:
        assert "product_name" not in nc.attrs
        nc.attrs.update(changes.get(case, {}))
        if case == "time-array":
            nc["time"][0] += 600
        if case == "array-shape":
            del nc["quality_level"]
            nc["quality_level"] = np.ones((1, 3, 4), dtype="i1")

    if case != "good":
        with pytest.raises(UserError, match="GHRSST|SEN3 NetCDF"):
            verify_netcdf(wst_file, name, WST)
    else:
        checks = verify_netcdf(wst_file, name, WST)
        assert checks["time_attributes_verified"] and checks["identity_schema"].startswith("GHRSST")
        assert {r["variable"]: r["shape"] for r in checks["decoded_arrays"]} == {
            "lat": [4, 3], "lon": [4, 3], "time": [1], **{f: [1, 4, 3] for f in ("sea_surface_temperature", "quality_level", "l2p_flags")},
        }


@pytest.mark.parametrize("case", ["missing", "changed", "same"])
def test_wst_requires_cdse_digest_both_when_resolving_and_executing(tmp_path, monkeypatch, wst_file, case):
    data = {WST_FILE: wst_file.read_bytes()}
    f = feature(data, WST, WST_COLLECTION)
    parts = {SEN3_MANIFEST: xfdu(data, WST), **data}
    prefix = "/".join(sen3_archive_parts(WST["product"])) + "/"
    if case == "missing":
        del f["assets"]["asset0"]["file:checksum"]
    elif case == "changed":
        f["assets"]["asset0"]["file:checksum"] = "d50110" + "0" * 32
    install_stub(monkeypatch, f, parts, listing(parts, prefix), prefix=prefix)
    row = resolved(f)
    assert row["raw"]["sen3"]["complete"] is (case == "same")
    if case != "same":
        assert WST_FILE not in {a["id"] for a in row["raw"]["assets"]}
        with pytest.raises(UserError):
            plan(row)
        return

    prepared = plan(row)
    out = download_sen3(AOI, prepared, tmp_path / "verified-wst")
    assert Path(out["files"][-1]["path"]).read_bytes() == data[WST_FILE]
    for item in prepared["targets"]:
        if item["name"] == WST_FILE:
            item.pop("provider_md5")
    with pytest.raises(UserError, match="校验值"):
        download_sen3(AOI, prepared, tmp_path / "no-digest")


@pytest.mark.parametrize("case", ["good", "identity", "time", "missing-measurement-times", "shape"])
def test_sral_verifies_native_measurement_identity_and_dimensions(tmp_path, case):
    path = tmp_path / "measurement_l1a.nc"
    product = SRA["product"] if case != "identity" else SRA["product"].replace("141707", "141706")
    when = SRA["start_utc"] if case != "time" else "2024-03-05T12:07:16.906731Z"
    with h5py.File(path, "w") as nc:
        nc.attrs.update(product_name=product, altimeter_sensor_name="SRAL", absolute_orbit_number=SRA["orbit"])
        if case == "missing-measurement-times":
            nc.attrs.update(start_time=when, stop_time=SRA["end_utc"])
        else:
            nc.attrs.update(first_meas_time=when, last_meas_time=SRA["end_utc"])
        for field in ("time", "lat", "lon", "alt"):
            shape = (2, 1) if case == "shape" and field == "alt" else (2,)
            nc[field + "_l1a_echo_sar_ku"] = np.zeros(shape, dtype="f8" if field == "time" else "i4")
    if case != "good":
        with pytest.raises(UserError, match="SEN3 NetCDF"):
            verify_netcdf(path, path.name, SRA)
    else:
        result = verify_netcdf(path, path.name, SRA)
        assert result["product"] == SRA["product"] and result["time_attributes_verified"] and result["orbit_attribute_verified"]
        assert len(result["decoded_arrays"]) == 4 and all(r["shape"] == [2] for r in result["decoded_arrays"])


def parallel_case(nc_files, count):
    science = nc_files("time_in.nc")
    names = [f"scan_{i:02}.nc" for i in range(count)]
    return names, setup_archive({member: science for member in reversed(names)})


def test_parallel_group_checks_manifest_first_limits_workers_and_publishes_in_order(tmp_path, monkeypatch, nc_files):
    names, (f, parts, archive_listing) = parallel_case(nc_files, 8)
    install_stub(monkeypatch, f, parts, archive_listing)
    prepared = plan(resolved(f))
    base = tmp_path / "parallel"
    folder = base / prepared["folder_name"]
    barrier = threading.Barrier(4)
    first_manifest = threading.Event()
    first_sibling = threading.Event()
    last_held = threading.Event()
    release = threading.Event()
    lock = threading.Lock()
    active = peak = 0
    clients = {}
    finished = []
    percentages = []
    original = sen3_download._receive

    def instrument(path, item, items, complete, http, cancel, report):
        nonlocal active, peak
        name = item["name"]
        if name == SEN3_MANIFEST:
            row = original(path, item, items, complete, http, cancel, report)
            first_manifest.set()
            return row
        assert first_manifest.is_set()
        with lock:
            active += 1
            peak = max(peak, active)
            clients.setdefault(threading.get_ident(), set()).add(http)
        try:
            if name in names[:4]:
                barrier.wait(timeout=10)
            if name == names[0]:
                assert first_sibling.wait(10)
            row = original(path, item, items, complete, http, cancel, report)
            if name == names[-1]:
                last_held.set()
                assert release.wait(10)
            with lock:
                finished.append(name)
            if name == names[3]:
                first_sibling.set()
            return row
        finally:
            with lock:
                active -= 1

    monkeypatch.setattr(sen3_download, "_receive", instrument)
    with ThreadPoolExecutor(max_workers=1) as runner:
        task = runner.submit(download_sen3, AOI, prepared, base,
                             progress=lambda _m, ratio, _p: percentages.append(ratio))
        try:
            assert last_held.wait(10)
            assert not (folder / PRODUCT).exists() and not tuple(folder.glob("*.starmap.json"))
        finally:
            release.set()
        result = task.result(timeout=15)
    assert peak == 4 and len(clients) == 4 and len(set.union(*clients.values())) == 4
    assert all(len(used) == 1 for used in clients.values()) and finished.index(names[3]) < finished.index(names[0])
    assert percentages and sorted(percentages) == percentages and percentages[-1] == .995
    receipt = json.loads((folder / (PRODUCT + ".starmap.json")).read_text(encoding="utf-8"))
    assert [r["file"] for r in receipt["members"]] == [SEN3_MANIFEST, *names]
    assert [Path(row["path"]).name for row in result["files"]] == [SEN3_MANIFEST, *names]


@pytest.mark.parametrize("mode", ["bad-hash", "user-cancel"])
def test_parallel_group_drains_sibling_writers_before_cleanup(tmp_path, monkeypatch, nc_files, mode):
    names, (f, parts, archive_listing) = parallel_case(nc_files, 4)
    stop = threading.Event()
    barrier = threading.Barrier(4)
    sibling_stops = []
    responses = []
    base = tmp_path / "drained"

    def change(name, kwargs, calls):
        if name == SEN3_MANIFEST:
            return None
        payload = parts[name]
        cancel = kwargs["cancel"]

        class Paused(StubResponse):
            def iter_content(self, _size):
                yield payload[:64]
                barrier.wait(timeout=10)
                if name == names[0]:
                    if mode == "user-cancel":
                        stop.set()
                    yield payload[64:-1] + bytes([payload[-1] ^ 1])
                else:
                    assert cancel.wait(10)
                    # A sibling is still in the response iterator. It must see its staging directory intact.
                    assert len(tuple(base.glob("*/.sen3-*"))) == 1
                    sibling_stops.append(name)
                    check_cancel(cancel)

        response = Paused(headers={"Content-Length": str(len(payload)), "ETag": TAG})
        responses.append(response)
        return response

    install_stub(monkeypatch, f, parts, archive_listing, change)
    prepared = plan(resolved(f))
    if mode == "bad-hash":
        with pytest.raises(UserError, match="MD5"):
            download_sen3(AOI, prepared, base, cancel=stop)
    else:
        with pytest.raises(Cancelled):
            download_sen3(AOI, prepared, base, cancel=stop)
    assert sorted(sibling_stops) == names[1:] and len(responses) == 4 and all(r.closed for r in responses)
    folder = base / prepared["folder_name"]
    assert not any(p.is_dir() or p.name.endswith(("starmap.json", ".part")) for p in folder.iterdir())
    record = json.loads(next(folder.glob("下载记录_*.json")).read_text(encoding="utf-8"))
    assert record["status"] == ("failed" if mode == "bad-hash" else "cancelled") and not record["items"]
