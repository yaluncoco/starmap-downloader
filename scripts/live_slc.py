"""Live installer acceptance: exact ASF SLC ZIP, SAFE inventory, and signed I/Q sample comparisons."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import tempfile
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import rasterio
from rasterio.windows import Window
import tifffile

from live_public import local_app
from live_s2_tiles import Local, digest

PROFILE = "s1-slc-seasia"


@dataclass(frozen=True)
class Case:
    product: str
    box: str
    date: str
    size: int
    asf_md5: str
    pols: tuple[str, ...]
    cpp: int  # expected files in ZIP (not directory entries)
    declared: int


VV = Case("S1A_IW_SLC__1SSV_20141101T100821_20141101T100834_003086_00388F_357C", "123.70,-8.145,123.74,-8.10",
          "2014-11-01", 966228993, "715e858d788f4e0e2df4d0b38068f08a", ("VV",), 26, 15)
DUAL = Case("S1A_IW_SLC__1SDV_20230910T100156_20230910T100208_050263_060CF3_3977", "125.03,-4.89,125.055,-4.86",
            "2023-09-10", 1536693925, "a2e46d516d8803d3b1c1c0be021157c7", ("VV", "VH"), 45, 33)


from browser_flow import open_data_page

def wait_job(api, key, timeout):
    deadline, last_update, msg = time.monotonic() + timeout, 0, None
    while time.monotonic() < deadline:
        resp = api.session.get(f"{api.origin}/api/job", params={"id": key}, timeout=12)
        resp.raise_for_status()
        snapshot = resp.json()
        now = time.monotonic()
        if snapshot["message"] != msg or now - last_update >= 30:
            msg, last_update = snapshot["message"], now
            detail = snapshot["detail"]
            print(f"{msg} · {snapshot['progress']:.1%} · {detail.get('received_bytes', '-')}/{detail.get('total_bytes', '-')}", flush=True)
        if snapshot["status"] not in ("running", "cancelling"):
            if snapshot["status"] != "completed":
                raise RuntimeError(snapshot.get("error") or msg)
            return snapshot["result"]
        time.sleep(.75)
    api.post("cancel", {"job_id": key})
    raise RuntimeError("SLC live transfer exceeded acceptance timeout")


def search(api, coords, date):
    geometry = api.post("aoi", {"coordinates": coords})["geometry"]
    criteria = {"start": date, "end": date}
    result = api.job("search", {"geometry": geometry, "profile": PROFILE, **criteria}, timeout=150)["result"]
    return {"geometry": geometry, "criteria": criteria, "query_id": result["query"]["id"], "profile": PROFILE}, result["scenes"]


def prepared(api, out, case, base, rows):
    assert [row["key"] for row in rows] == [case.product], rows
    row = rows[0]
    assert row["date"] == case.date and row["coverage_percent"] == 100
    assert row["tiles"] == ["IW · " + "+".join(case.pols)]
    asset, = row["raw"]["assets"]
    assert asset["provider_md5"] == case.asf_md5 and asset["bytes"] == case.size
    request = {**base, "scene_keys": [case.product], "output_dir": str(out), "options": {"mode": "original", "transport": "public"}}
    plan = api.post("plan", request)
    assert not plan["requires"] and plan["estimated_bytes"] == case.size and plan["transports"] == ["public"]
    assert "不支持" in api.post("plan", {**request, "options": {"mode": "metadata"}}, status=400)["error"]
    return {**request, "plan_fingerprint": plan["fingerprint"]}


def browser_download(api, out, case, timeout):
    from playwright.sync_api import expect, sync_playwright

    with sync_playwright() as engine:
        browser = engine.chromium.launch(channel=os.environ.get("STARMAP_TEST_BROWSER", "msedge"), headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 960})
        problems = []
        page.on("pageerror", lambda err: problems.append(str(err)))
        page.route("https://**/*", lambda route: route.abort())
        open_data_page(page, api.origin)
        page.locator("#sourceProfile").select_option(PROFILE)
        expect(page.locator("#slcInfo")).to_contain_text("并非连续或全球覆盖")
        for selector in ("#cloudOptions", "#radarModeField", "#processAuth", "#rawOptions"):
            expect(page.locator(selector)).to_be_hidden()
        page.locator('[data-tab="coordinates"]').click()
        page.locator("#coordinates").fill(case.box)
        page.locator("#applyCoordinates").click()
        expect(page.locator("#aoiName")).to_have_text("坐标研究区")
        for selector in ("#startDate", "#endDate"):
            page.locator(selector).fill(case.date)
        page.locator("#searchScenes").click()
        expect(page.locator(f'#scenesBody tr[data-key="{case.product}"] input')).to_be_checked(timeout=150000)
        bootstrap = api.session.get(f"{api.origin}/api/bootstrap", timeout=12).json()
        assert bootstrap["query"]["profile"] == PROFILE and not bootstrap["connected"] and not bootstrap["s3_connected"]
        base = {"geometry": bootstrap["aoi"]["geometry"], "criteria": {"start": case.date, "end": case.date},
                "query_id": bootstrap["query"]["id"], "profile": PROFILE}
        request = prepared(api, out, case, base, bootstrap["scenes"])
        page.locator("#outputDirectory").fill(str(out))
        page.locator("#downloadSelected").click(timeout=18000)
        expect(page.locator("#planDetails")).to_contain_text("完整原始 SAFE ZIP")
        expect(page.locator("#planNote")).to_contain_text("ASF 全文件 MD5")
        with page.expect_response(lambda r: r.url == f"{api.origin}/api/download" and r.request.method == "POST") as started:
            page.locator("#confirmDownload").click()
        result = wait_job(api, started.value.json()["job_id"], timeout)
        expect(page.locator("#jobStatus")).to_have_text("下载完成", timeout=15000)
        expect(page.locator("#jobFiles .job-file")).to_have_count(1)
        assert not problems, problems
        browser.close()
    return request, result


def decode_iq(archive, measurement, path, out):
    # Rasterio/GDAL decode vs independently read signed words from the original uncompressed TIFF strips.
    with tempfile.NamedTemporaryFile(prefix=".slc-iq-", suffix=".tiff", dir=out, delete=False) as tmp:
        tif = Path(tmp.name)
        try:
            with archive.open(path) as member:
                shutil.copyfileobj(member, tmp, length=1024**2)
        except BaseException:
            tmp.close()
            tif.unlink(missing_ok=True)
            raise
    try:
        with rasterio.open(tif) as ds, tifffile.TiffFile(tif) as source, tif.open("rb") as raw:
            assert ds.count == 1 and ds.dtypes == ("complex_int16",) and [ds.width, ds.height] == measurement["shape"]
            page = source.pages[0]
            assert page.compression == 1 and not page.is_tiled and page.sampleformat == 5
            assert int(page.tags['BitsPerSample'].value) == 32 and page.samplesperpixel == 1
            n, count, signed = 0, 0, hashlib.sha256()
            for yc in (ds.height // 4, ds.height // 2, ds.height * 3 // 4):
                for xc in (ds.width // 4, ds.width // 2, ds.width * 3 // 4):
                    values = ds.read(1, window=Window(xc, yc, 8, 8))
                    expected = []
                    for y in range(yc, yc + 8):
                        strip, row = divmod(y, int(page.rowsperstrip))
                        raw.seek(int(page.dataoffsets[strip]) + (row * ds.width + xc) * 4)
                        expected.append(np.frombuffer(raw.read(8 * 4), dtype=source.byteorder + "i2").reshape(8, 2))
                    array = np.array(expected)
                    assert np.array_equal(values.real, array[:, :, 0]) and np.array_equal(values.imag, array[:, :, 1])
                    count += int(np.count_nonzero((array[:, :, 0] != 0) & (array[:, :, 1] != 0)))
                    signed.update(array.astype("<i2").tobytes())
                    n += values.size
            assert count > 0
            return {"member": measurement["name"], "shape": measurement["shape"], "pixels_compared_iq": n,
                    "nonzero_both": count, "samples_sha256": signed.hexdigest()}
    finally:
        tif.unlink(missing_ok=True)


def audit(result, out, case, reference):
    row, = json.loads(Path(result["manifest"]).read_bytes())["items"]
    path = Path(row["path"])
    assert row["status"] == "completed" and path.name == case.product + ".zip" and path.stat().st_size == case.size
    with path.open("rb") as file:
        md5 = hashlib.file_digest(file, "md5").hexdigest()
    sha = digest(path)
    assert md5 == case.asf_md5 == row["asf_zip_md5"] and sha == row["sha256"] and row["provider_checksum_verified"] == "ASF DAAC ZIP MD5"
    independent = None
    if reference is not None and (reference / path.name).is_file():
        source = reference / path.name
        assert source.stat().st_size == case.size and digest(source) == sha
        independent = str(source)
    elif reference is not None and case == VV:
        raise AssertionError(f"Missing independent reference ZIP: {reference / path.name}")
    checked = row["slc_validation"]
    assert checked["product"] == case.product and checked["polarizations"] == list(case.pols)
    assert checked["zip_files_crc_verified"] == case.cpp and checked["declared_members_md5_verified"] == case.declared
    assert len(checked["measurements"]) == len(case.pols)*3
    with zipfile.ZipFile(path) as z:
        assert z.testzip() is None and len([m for m in z.infolist() if not m.is_dir()]) == case.cpp
        assert hashlib.sha256(z.read(case.product+".SAFE/manifest.safe")).hexdigest() == checked["manifest_sha256"]
        decoded = [decode_iq(z, m, case.product+".SAFE/"+m["name"], out) for m in checked["measurements"]]
    assert not list(path.parent.glob("*.part"))
    return {"path": str(path), "bytes": case.size, "source": row["url"], "etag": row["etag"], "asf_md5": md5,
            "sha256": sha, "independent_zip": independent, "manifest": result["manifest"], "format": checked, "iq": decoded,
            "ranges": row["range_windows"], "range_workers": row["range_workers"], "range_attempts": row["transfer_attempts"]}


def recheck(api, request, report):
    path = Path(report["path"])
    receipt = path.with_suffix(".starmap.json")
    assert api.job("download", request, timeout=240)["result"]["files"][0]["status"] == "skipped"
    original = receipt.read_bytes()
    with path.open("r+b") as f:
        f.seek(1024)
        saved = f.read(1)
        f.seek(1024)
        f.write(bytes([saved[0] ^ 0x1]))
    try:
        edited = json.loads(original)
        edited["sha256"] = digest(path)
        receipt.write_text(json.dumps(edited), encoding="utf-8")
        denied = api.job("download", request, timeout=240, expected="failed")
        assert "MD5" in denied["error"] and not list(path.parent.glob("*.part"))
    finally:
        with path.open("r+b") as f:
            f.seek(1024)
            f.write(saved)
        receipt.write_bytes(original)
    assert digest(path) == report["sha256"]
    assert api.job("download", request, timeout=240)["result"]["files"][0]["status"] == "skipped"
    return {"same_length_tamper_with_forged_local_sha256": denied["error"], "skip_after_restoring_exact_bytes": True}


def run(exe, out, case, reference, timeout):
    out.mkdir(exist_ok=True, parents=True)
    report = {}
    with local_app(exe, out, out / "acceptance-settings") as (origin, session):
        api = Local(origin, session)
        if case == DUAL:
            base, rows = search(api, case.box, case.date)
            request = prepared(api, out, case, base, rows)
            result = wait_job(api, api.post("download", request)["job_id"], timeout)
        else:
            assert not search(api, case.box, "2024-04-05")[1]
            assert not search(api, case.box, "2014-11-02")[1]
            request, result = browser_download(api, out, case, timeout)
            report.update(browser=True, outside_archive_excluded=True, wrong_utc_day_excluded=True)
        report.update(audit(result, out, case, reference))
        if case == VV:
            report.update(recheck(api, request, report))
    f = out / "slc-live-report.json"
    f.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Verified {case.product}, {case.size} exact bytes, {len(report['iq'])} I/Q TIFFs: {f}", flush=True)
    return str(f)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--exe", type=lambda s: Path(s).resolve())
    p.add_argument("--output", required=True, type=lambda s: Path(s).resolve())
    p.add_argument("--reference-dir", type=lambda s: Path(s).resolve())
    cases = p.add_mutually_exclusive_group()
    cases.add_argument("--dual", action="store_true", help="Test the 2023 VV+VH sample instead of VV")
    cases.add_argument("--both", action="store_true", help="Test both samples in separate local app processes")
    p.add_argument("--timeout", type=int, default=5400)
    a = p.parse_args()
    if a.both:
        with ThreadPoolExecutor(max_workers=2) as pool:
            one = pool.submit(run, a.exe, a.output / "vv", VV, a.reference_dir, a.timeout)
            two = pool.submit(run, a.exe, a.output / "dual", DUAL, a.reference_dir, a.timeout)
            reports = {"vv": one.result(), "dual": two.result()}
        (a.output / "slc-live-report.json").write_text(json.dumps(reports, ensure_ascii=False, indent=2), encoding="utf-8")
    else:
        run(a.exe, a.output, DUAL if a.dual else VV, a.reference_dir, a.timeout)


if __name__ == "__main__":
    main()
