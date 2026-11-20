"""Live, credential-free acceptance for Sentinel-3 via the installed Windows executable."""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import shutil
import time
import xml.etree.ElementTree as ET
from pathlib import Path

import h5py
import numpy as np
import requests

from live_public import BBOX, local_app
from live_s5p import independent_catalogue

PROFILE = "sen3-aws-original"
ARCHIVE = "https://meeo-s3.s3.eu-central-1.amazonaws.com/"
XFDU = "xfdumanifest.xml"
LST_NTC = "NTC/S3B/SL_2_LST___/2024/03/05/S3B_SL_2_LST____20240305T132901_20240305T133201_20240306T120123_0179_090_224_0540_PS2_O_NT_004.SEN3"
CASES = [
    ("sl-2-lst-ntc", BBOX, LST_NTC, True, 11,
     {"LST_in.nc": {"LST": [1200, 1500]}, "geodetic_in.nc": {"latitude_in": [1200, 1500], "longitude_in": [1200, 1500]},
      "flags_in.nc": {"confidence_in": [1200, 1500]}, "time_in.nc": {"time_stamp_i": [1200]}}),
    ("sl-2-lst-nrt", BBOX, "NRT/S3A/SL_2_LST___/2026/11/15/S3A_SL_2_LST____20261115T021353_20261115T021653_20261115T043525_0179_146_160_2340_PS1_O_NR_005.SEN3",
     False, 11, {"LST_in.nc": {"LST": [1200, 1500]}}),
    ("sl-1-rbt-ntc", BBOX, "NTC/S3B/SL_1_RBT___/2024/03/05/S3B_SL_1_RBT____20240305T132901_20240305T133201_20240306T111630_0179_090_224_0540_PS2_O_NT_004.SEN3",
     False, 97, {"S8_BT_in.nc": {"S8_BT_in": [1200, 1500]}}),
    ("sl-2-aod-nrt", "55.47315,-3.61253,55.49315,-3.59253", "NRT/S3A/SL_2_AOD___/2024/08/07/S3A_SL_2_AOD____20240807T060427_20240807T060926_20240807T075751_0299_115_262______MAR_O_NR_003.SEN3",
     True, 1, {"NRT_AOD.nc": {"AOD_550_Merged_OceanLand": [211, 157], "latitude": [211, 157], "longitude": [211, 157]}}),
    ("sl-2-frp-ntc", "58.44438,82.4247,58.46438,82.4447", "NTC/S3A/SL_2_FRP___/2024/09/29/S3A_SL_2_FRP____20240929T110829_20240929T111129_20240930T211153_0179_117_251_1440_PS1_O_NT_004.SEN3",
     False, 18, {"FRP_in.nc": {"flags": [1200, 1500]}}),
    ("olci-1-efr-ntc", "158.9033,75.29373,158.9233,75.31373", "NTC/S3A/OL_1_EFR___/2024/09/30/S3A_OL_1_EFR____20240930T004020_20240930T004223_20241001T011241_0122_117_259_1620_PS1_O_NT_004.SEN3",
     False, 50, {"Oa08_radiance.nc": {"Oa08_radiance": [2794, 4865]}}),
    ("olci-2-lfr-ntc", "155.59518,53.90355,155.61518,53.92355", "NTC/S3A/OL_2_LFR___/2025/01/06/S3A_OL_2_LFR____20250106T000535_20250106T000712_20250107T005246_0096_121_116_1980_PS1_O_NT_003.SEN3",
     True, 11, {"gifapar.nc": {"GIFAPAR": [2203, 4865]}}),
    ("olci-2-wfr-nrt", "152.27475,57.0351,152.29475,57.0551", "NRT/S3A/OL_2_WFR___/2026/11/19/S3A_OL_2_WFR____20261119T002320_20261119T002610_20261119T021615_0169_146_216_1980_MAR_O_NR_004.SEN3",
     False, 33, {"chl_nn.nc": {"CHL_NN": [3863, 4865]}, "geo_coordinates.nc": {"latitude": [3863, 4865], "longitude": [3863, 4865]}}),
]
LEGACY_CASES = CASES[:]
WST_MEMBER = "20240228010152-MAR-L2P_GHRSST-SSTskin-SLSTRA-20240229082217-v02.0-fv01.0.nc"
SRA_MEMBER = "measurement_l1a.nc"
CASES += [
    ("syn-2-aod-ntc", "136.42569,-11.25161,136.44569,-11.23161", "NTC/S3A/SY_2_AOD___/2026/01/07/S3A_SY_2_AOD____20260107T001655_20260107T010052_20260108T163638_2637_134_330______PS1_O_NT_002.SEN3",
     True, 1, {"NTC_AOD.nc": {field: [3996, 324] for field in ("AOD_550", "latitude", "longitude", "aod_quality_flags")}}),
    ("sl-2-wst-ntc", "114.54271,5.43159,114.56271,5.45159", "NTC/S3A/SL_2_WST___/2024/02/28/S3A_SL_2_WST____20240228T010152_20240228T024251_20240229T082217_6059_109_273______MAR_O_NT_003.SEN3",
     True, 1, {WST_MEMBER: {"lat": [40394, 1500], "lon": [40394, 1500], "time": [1],
                            **{field: [1, 40394, 1500] for field in ("sea_surface_temperature", "quality_level", "l2p_flags")}}}),
    ("sr-1-sra-a-ntc", "-37.10585,0.60653,-37.08585,0.62653", "NTC/S3B/SR_1_SRA_A_/2024/03/05/S3B_SR_1_SRA_A__20240305T120417_20240305T125447_20240329T141707_3029_090_223______PS2_O_NT_005.SEN3",
     True, 1, {SRA_MEMBER: {field + "_l1a_echo_sar_ku": [234544] for field in ("time", "lat", "lon", "alt")}}),
]
GAP = "S3A_OL_1_EFR____20240610T023612_20240610T023912_20240920T182055_0179_113_203_2340_MAR_R_NT_004"
LARGE_CASES = {"sl-2-wst-ntc", "sr-1-sra-a-ntc"}


from browser_flow import open_data_page

def manifest_from_source(prefix, feature):
    for trial in range(4):
        try:
            with requests.get(ARCHIVE + prefix + "/" + XFDU, timeout=(10, 35), allow_redirects=False) as response:
                if response.status_code == 200:
                    data = response.content
                    break
                if response.status_code not in (429, 500, 502, 503, 504):
                    raise RuntimeError(f"Independent MEEO manifest returned HTTP {response.status_code}")
        except (requests.ConnectionError, requests.Timeout, requests.exceptions.ChunkedEncodingError):
            pass
        time.sleep(2 ** (trial + 1))
    else:
        raise RuntimeError("Independent MEEO manifest remained unavailable")
    root = ET.fromstring(data)
    names = [n.text for n in root.iter() if n.tag.endswith("}productName")]
    orbits = [int(n.text) for n in root.iter() if n.tag.endswith("}orbitNumber") and n.get("type") == "start"]
    assert names == [prefix.rsplit("/", 1)[-1]] and orbits == [feature["properties"]["sat:absolute_orbit"]]
    members = {node.find("fileLocation").get("href").removeprefix("./"): (int(node.get("size")), node.findtext("checksum").lower())
               for node in root.findall("dataObjectSection/dataObject/byteStream")}
    assets = {a["href"].rsplit("/", 1)[-1]: a for a in feature["assets"].values() if "/" + names[0] + "/" in a["href"]}
    return data, members, assets


def file_hashes(path, original):
    sha = hashlib.sha256()
    md5 = hashlib.md5(usedforsecurity=False) if original else None
    with path.open("rb") as handle:
        while block := handle.read(2 * 1024 * 1024):
            sha.update(block)
            if md5 is not None:
                md5.update(block)
    return sha.hexdigest(), md5.hexdigest() if md5 is not None else None


def verify_array(ds, shape, axis):
    assert list(ds.shape) == shape and ds.size
    # Read all stripes of selected arrays without loading the orbit grids into memory.
    step = max(1, (4 * 1024**2) // (ds.size // ds.shape[axis] * ds.dtype.itemsize))
    any_finite = False
    for i in range(0, ds.shape[axis], step):
        sel = [slice(None)] * ds.ndim
        sel[axis] = slice(i, i + step)
        any_finite |= bool(np.isfinite(ds[tuple(sel)]).any())
    assert any_finite
    return {"shape": shape, "dtype": str(ds.dtype), "scan_axis": axis}


def text_attr(nc, key):
    value = nc.attrs[key]
    return value.decode("ascii") if isinstance(value, bytes) else value


def verify_identity(nc, name, product):
    if name == WST_MEMBER:
        assert "product_name" not in nc.attrs
        observed = {key: text_attr(nc, key) for key in (
            "platform", "sensor", "id", "processing_level", "processing_baseline", "product_version",
            "start_time", "stop_time", "time_coverage_start", "time_coverage_end", "date_created",
        )}
        assert observed["platform"].replace("-", "") == "Sentinel3A" and observed["sensor"] == "SLSTR"
        assert observed["id"] == "SLSTRA-MAR-L2P-v1.0" and observed["processing_level"] == "L2P"
        assert observed["product_version"] == "1.0" and observed["processing_baseline"].startswith("SL__SST.003.")
        start, stop = dt.datetime.fromisoformat(observed["start_time"]), dt.datetime.fromisoformat(observed["stop_time"])
        assert start == dt.datetime.fromisoformat("2024-02-28T01:01:51+00:00") and stop == dt.datetime.fromisoformat("2024-02-28T02:42:50+00:00")
        assert start == dt.datetime.fromisoformat(observed["time_coverage_start"]) and stop == dt.datetime.fromisoformat(observed["time_coverage_end"])
        assert dt.datetime.fromisoformat(observed["date_created"]) == dt.datetime.fromisoformat("2024-02-29T08:22:17+00:00")
        assert int(nc["time"][0]) == int((start - dt.datetime(1981, 1, 1, tzinfo=dt.timezone.utc)).total_seconds())
        return observed
    assert text_attr(nc, "product_name") == product
    if name == SRA_MEMBER:
        assert text_attr(nc, "altimeter_sensor_name") == "SRAL"
        first, last = (text_attr(nc, key) for key in ("first_meas_time", "last_meas_time"))
        assert abs((dt.datetime.fromisoformat(first) - dt.datetime.fromisoformat("2024-03-05T12:04:17+00:00")).total_seconds()) < 1
        assert abs((dt.datetime.fromisoformat(last) - dt.datetime.fromisoformat("2024-03-05T12:54:47+00:00")).total_seconds()) < 1
        assert np.allclose(nc["time_l1a_echo_sar_ku"][:3], [762955456.9067305, 762955456.9194655, 762955456.9322007], rtol=0, atol=1e-5)
        assert np.array_equal(nc["lat_l1a_echo_sar_ku"][:3], [81419034, 81419022, 81419010])
        assert np.array_equal(nc["lon_l1a_echo_sar_ku"][:3], [58153095, 58147990, 58142886])
        return {"first_meas_time": first, "last_meas_time": last}
    return {}


def verify_files(result, reference, prefix, arrays_by_name, complete):
    xml, declared, catalogue = reference
    product = prefix.rsplit("/", 1)[-1]
    record = json.loads(Path(result["manifest"]).read_text(encoding="utf-8"))
    assert record["status"] == "completed"
    wanted = set(declared) | {XFDU} if complete else set(arrays_by_name)
    paths = {p.name: p for f in result["files"] if f["status"] == "completed" for p in [Path(f["path"])]}
    assert set(paths) == wanted
    parent, = {p.parent.name for p in paths.values()}
    assert parent == (product if complete else product.removesuffix(".SEN3") + "_selected_members")
    assert {p.name for p in next(iter(paths.values())).parent.iterdir()} == wanted
    saved = {item["file"]: item for item in record["items"]}
    assert saved.keys() == paths.keys()
    files = []
    for name, path in sorted(paths.items()):
        row = saved[name]
        assert row["url"] == ARCHIVE + prefix + "/" + name and row["product"] == product
        sha, md5 = file_hashes(path, name != XFDU)
        assert sha == row["sha256"]
        evidence = {"name": name, "bytes": path.stat().st_size, "sha256": sha}
        if name == XFDU:
            assert path.read_bytes() == xml
            evidence["original_manifest_bytes_equal"] = True
        else:
            size, expected = declared[name]
            assert path.stat().st_size == size == row["size"] and md5 == expected and "SEN3 XFDU MD5" in row["provider_checksum_verified"]
            evidence["xfdu_md5"] = md5
            orig = catalogue.get(name, {})
            if orig.get("file:checksum", "").startswith("d50110"):
                assert orig["file:checksum"][6:].lower() == md5 and int(orig["file:size"]) == size
                evidence["cdse_md5"] = md5
            if name in (WST_MEMBER, SRA_MEMBER, "NTC_AOD.nc"):
                assert evidence.get("cdse_md5") == md5
            evidence["decoded_arrays"] = {}
            with h5py.File(path, "r") as nc:
                identity = verify_identity(nc, name, product)
                if identity:
                    evidence["format_identity"] = identity
                for field, shape in arrays_by_name.get(name, {}).items():
                    evidence["decoded_arrays"][field] = verify_array(nc[field], shape, int(name == WST_MEMBER and len(shape) == 3))
        files.append(evidence)
    return {"scope": "complete" if complete else "selected", "files": files, "receipt": result["manifest"],
            "bytes": sum(f["bytes"] for f in files), "declared_members": len(declared)}


def browser_check(origin, output, reference, category_count):
    from playwright.sync_api import expect, sync_playwright

    key = LST_NTC.rsplit("/", 1)[-1].removesuffix(".SEN3")
    names = {k: CASES[0][-1][k] for k in ("LST_in.nc", "geodetic_in.nc", "flags_in.nc")}
    # Use a different selection from the full download so the browser exercises a fresh transfer.
    with sync_playwright() as driver:
        browser = driver.chromium.launch(channel=os.environ.get("STARMAP_TEST_BROWSER", "msedge"), headless=True)
        page = browser.new_page(viewport={"width": 1360, "height": 900})
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.route("https://**/*", lambda route: route.abort())
        open_data_page(page, origin)
        expect(page.locator("#sen3Collection option")).to_have_count(category_count)
        page.locator("#sourceProfile").select_option(PROFILE)
        expect(page.locator("#publicAuth")).to_be_visible()
        expect(page.locator("#cloudOptions")).to_be_hidden()
        expect(page.locator("#rawWholeProduct")).to_contain_text("完整 SEN3")
        page.locator('[data-tab="coordinates"]').click()
        page.locator("#coordinates").fill(BBOX)
        page.locator("#applyCoordinates").click()
        for field in ("startDate", "endDate"):
            page.locator("#" + field).fill("2024-03-05")
        page.locator("#searchScenes").click()
        scene = page.locator(f'#scenesBody tr[data-key="{key}"]')
        expect(scene).to_be_visible(timeout=210000)
        expect(scene).to_contain_text("完整产品可用")
        scene.locator('input[type="checkbox"]').check()
        page.locator("#rawMode").select_option("assets")
        for name in names:
            page.locator(f'#rawMultiAssets input[value="{name}"]').check()
        page.locator("#outputDirectory").fill(str(output))
        expect(page.locator("#downloadSelected")).to_be_enabled(timeout=15000)
        page.locator("#downloadSelected").click()
        expect(page.locator("#planNote")).to_contain_text("不是完整 .SEN3")
        with page.expect_response(lambda response: response.url.endswith("/api/download") and response.ok) as accepted:
            page.locator("#confirmDownload").click()
        key = accepted.value.json()["job_id"]
        expect(page.locator("#jobStatus")).to_have_text("下载完成", timeout=280000)
        expect(page.locator("#jobFiles .job-file")).to_have_count(3)
        token = page.locator('meta[name="app-token"]').get_attribute("content")
        result = page.request.get(f"{origin}/api/job", params={"id": key}, headers={"X-App-Token":token}).json()["result"]
        page.locator("#closeJobPanel").click()
        page.locator("#sen3Collection").select_option("sentinel-3-sl-2-lst-nrt")
        expect(page.locator("#scenesBody tr")).to_have_count(0)
        expect(page.locator("#downloadSelected")).to_be_disabled()
        assert not errors, errors
        browser.close()
    return {"categories": category_count, "fresh_multi_selection": True, "cleared_after_category_change": True,
            **verify_files(result, reference, LST_NTC, names, False)}


def verify_reuse(job, request, result, output):
    rows = result["files"]
    clean = {Path(row["path"]): file_hashes(Path(row["path"]), False)[0] for row in rows}
    second = job("download", request)
    assert {Path(row["path"]) for row in second["files"]} == clean.keys()
    assert all(row["status"] == "skipped" for row in second["files"])

    # The intentionally corrupt sample lives in its own named sandbox; leave the verified copy untouched.
    sandbox = output / "tamper-fixture"
    sandbox.mkdir()
    parent = Path(rows[0]["path"]).parent
    target = sandbox / parent.parent.name / parent.name
    target.mkdir(parents=True)
    for file in clean:
        shutil.copyfile(file, target / file.name)
    receipt = parent.with_name(parent.name + ".starmap.json")
    shutil.copyfile(receipt, target.parent / receipt.name)
    nc = next(target.glob("*.nc"))
    size = nc.stat().st_size
    with nc.open("r+b") as handle:
        pos = handle.seek(-1, os.SEEK_END)
        byte = handle.read(1)
        handle.seek(pos)
        handle.write(bytes([byte[0] ^ 1]))
    corrupt_sha, _ = file_hashes(nc, False)
    denied = job("download", {**request, "output_dir": str(sandbox)}, expected="failed")
    assert "已有 SEN3" in denied["error"] and nc.stat().st_size == size
    assert file_hashes(nc, False)[0] == corrupt_sha != clean[parent / nc.name]
    assert all(file_hashes(file, False)[0] == digest for file, digest in clean.items())
    return {"clean_reuse": "skipped after integrity check", "same_size_corrupt_copy": "refused, not overwritten",
            "clean_output_unchanged": True, "reason": denied["error"], "fixture": str(nc)}


def run(exe, output, only, full_rbt, download_timeout, skip_large, check_reuse):
    output.mkdir(parents=True, exist_ok=True)
    destination = output / "sen3-live-report.json"
    report = {"started_utc": dt.datetime.now(dt.timezone.utc).isoformat(), "status": "running", "cases": []}

    def save():
        destination.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    try:
        with local_app(exe, output) as (origin, session):
            def post(route, body, code=200):
                response = session.post(f"{origin}/api/{route}", json=body, timeout=15)
                if response.status_code != code:
                    raise RuntimeError(f"Local {route} returned {response.status_code}: {response.json().get('error')}")
                return response.json()

            def job(route, body, *, show_long_progress=False, expected="completed"):
                key = post(route, body)["job_id"]
                begun = time.monotonic()
                deadline = begun + (download_timeout if route == "download" else 360)
                next_notice = begun + 30
                previous = ""
                def snapshot():
                    response = session.get(f"{origin}/api/job", params={"id": key}, timeout=10)
                    response.raise_for_status()
                    return response.json()

                while time.monotonic() < deadline:
                    data = snapshot()
                    if data["status"] == expected:
                        return data["result"] if expected == "completed" else data
                    if data["status"] not in ("running", "cancelling"):
                        raise RuntimeError(f"Local {route}: {data.get('error') or data.get('message')}")
                    message = data.get("message", "")
                    if "核对 SEN3 原始字节" in message and message != previous:
                        print(message, flush=True)
                    if show_long_progress and time.monotonic() >= next_notice:
                        detail = data["detail"]
                        got, want = detail.get("received_bytes"), detail.get("total_bytes")
                        transfer = f" · {got:,}/{want:,} bytes" if got is not None and want else ""
                        print(f"Job {data['progress']:.1%} · {message}{transfer}", flush=True)
                        next_notice = time.monotonic() + 30
                    previous = message
                    time.sleep(.45)
                state = post("cancel", {"job_id": key})["status"]
                grace = time.monotonic() + 120  # give in-flight reads time to close before stopping the app
                while state in ("running", "cancelling") and time.monotonic() < grace:
                    time.sleep(.5)
                    state = snapshot()["status"]
                raise RuntimeError(f"Local {route} missed its test deadline (final job status: {state})")

            boot = session.get(f"{origin}/api/bootstrap", timeout=10).json()
            cases = LEGACY_CASES if boot["version"] == "1.6.0" else CASES
            assert {r["id"] for r in boot["sen3_collections"]} == {"sentinel-3-" + r[0] for r in cases}
            if only and set(only) - {r[0] for r in cases}:
                raise RuntimeError("This installed version does not have one of the requested test collections")
            assert not any(boot[k] for k in ("connected", "raw_connected", "s3_connected"))
            report["version"] = boot["version"]
            report["listed_collections"] = len(cases)
            report["skipped_large_collections"] = ["sentinel-3-"+row[0] for row in cases if skip_large and row[0] in LARGE_CASES]
            save()
            for suffix, bbox, prefix, complete, count, fields in cases:
                if (only and suffix not in only) or (skip_large and suffix in LARGE_CASES):
                    continue
                if suffix == "sl-1-rbt-ntc" and full_rbt:
                    complete = True
                ident = prefix.rsplit("/", 1)[-1].removesuffix(".SEN3")
                date = "-".join(prefix.split("/")[3:6])
                collection = "sentinel-3-"+suffix
                print(f"Live SEN3 {suffix} {date}, {'full' if complete else 'selected'}: {ident}", flush=True)
                criteria = {"collection": collection, "start": date, "end": date}
                geo = post("aoi", {"coordinates": bbox})["geometry"]
                common = {"geometry": geo, "profile": PROFILE}
                found = job("search", {**common, **criteria})
                scene, = [row for row in found["scenes"] if row["key"] == ident]
                assert scene["raw"]["sen3"]["complete"] and scene["raw"]["sen3"]["declared_members"] == count
                options = {"mode": "original"} if complete else {"mode": "assets", "assets": list(fields)}
                value = {**common, "query_id": found["query"]["id"], "criteria": criteria, "scene_keys": [ident], "options": options,
                         "output_dir": str(output)}
                planned = post("plan", value)
                assert planned["sen3_complete"] == complete and planned["requires"] == []
                assert planned["requests"] == (count + 1 if complete else len(fields))
                reference = manifest_from_source(prefix, independent_catalogue(collection, ident))
                transfer_started = time.monotonic()
                result = job("download", value, show_long_progress=suffix in LARGE_CASES)
                transfer_seconds = round(time.monotonic() - transfer_started, 3)
                check_started = time.monotonic()
                receipt = verify_files(result, reference, prefix, fields, complete)
                check_seconds = round(time.monotonic() - check_started, 3)
                assert planned["estimated_bytes"] == receipt["bytes"]
                print(f"Verified {len(receipt['files'])} files, {receipt['bytes']:,} bytes; app {transfer_seconds}s, independent {check_seconds}s", flush=True)
                report["cases"].append({"collection": collection, "utc_date": date, "bbox": bbox, "product": ident + ".SEN3",
                                        "returned": len(found["scenes"]), "transfer_and_app_checks_seconds": transfer_seconds,
                                        "independent_checks_seconds": check_seconds, **receipt})
                save()
                if check_reuse and suffix == "sl-2-aod-nrt":
                    report["reuse_check"] = verify_reuse(job, value, result, output)
                    save()
                if prefix == LST_NTC and not only:
                    report["browser"] = browser_check(origin, output, reference, len(cases))
                    save()
            if not only:
                criteria = {"collection": "sentinel-3-olci-1-efr-ntc", "start": "2024-06-10", "end": "2024-06-10"}
                common = {"geometry": post("aoi", {"coordinates": BBOX})["geometry"], "profile": PROFILE}
                found = job("search", {**common, **criteria})
                gap = next(scene for scene in found["scenes"] if scene["key"] == GAP)
                assert not gap["raw"]["available"] and not gap["raw"]["assets"]
                denied = post("plan", {**common, "criteria": criteria, "query_id": found["query"]["id"], "scene_keys": [GAP],
                                       "options": {"mode": "original"}}, code=400)
                report["live_archive_gap"] = {"product": GAP, "status": "not offered, no substitution", "plan_message": denied["error"]}
        report["status"] = "passed"
    except Exception as err:
        report.update(status="failed", error=f"{type(err).__name__}: {err}")
        raise
    finally:
        report["finished_utc"] = dt.datetime.now(dt.timezone.utc).isoformat()
        save()
    print(f"SEN3 acceptance passed: {len(report['cases'])} collections; {destination}", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--exe", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--only", action="append", choices=[r[0] for r in CASES], help="Debug a subset; skips UI and negative live verification")
    parser.add_argument("--full-rbt", action="store_true", help="Also download all 98 original files for RBT rather than one member")
    parser.add_argument("--skip-large", action="store_true", help="Skip the 647 MB WST and 2.52 GB SRAL transfers")
    parser.add_argument("--check-reuse", action="store_true", help="Check verified reuse and a same-size corrupt copy of the small SLSTR AOD sample")
    parser.add_argument("--download-timeout", type=int, default=5400, help="Per-product job deadline in seconds (default: 5400)")
    args = parser.parse_args()
    if args.download_timeout <= 0:
        parser.error("--download-timeout must be positive")
    if args.skip_large and args.only and LARGE_CASES.intersection(args.only):
        parser.error("--only cannot name a large case together with --skip-large")
    if args.check_reuse and args.only and "sl-2-aod-nrt" not in args.only:
        parser.error("--check-reuse uses the sl-2-aod-nrt test case")
    run(args.exe.resolve(strict=True) if args.exe else None, args.output.resolve(), args.only, args.full_rbt, args.download_timeout,
        args.skip_large, args.check_reuse)


if __name__ == "__main__":
    main()
