"""Live Sentinel-1 RTC acceptance: installed app plus an independent TIFF tile decoder."""
from __future__ import annotations

import argparse
import base64
import datetime as dt
import hashlib
import io
import json
import math
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qsl

import numpy as np
import rasterio
import requests
import tifffile
from pyproj import Transformer

from live_public import local_app

PROFILE = "s1-rtc-pc"
COLLECTION = "sentinel-1-rtc"
STAC_ROOT = f"https://planetarycomputer.microsoft.com/api/stac/v1/collections/{COLLECTION}/items/"
OBJECT_ROOT = "https://sentinel1euwestrtc.blob.core.windows.net/sentinel1-grd-rtc/"
TOKEN_URL = "https://planetarycomputer.microsoft.com/api/sas/v1/token/sentinel1euwestrtc/sentinel1-grd-rtc"
SECRET_MARKER = re.compile(r"[?&](?:sig|se|sp|sr|sv)=|SharedAccessSignature", re.I)
FILL = np.float32(-32768)


@dataclass(frozen=True)
class Case:
    name: str
    date: str
    bbox: str
    base: str
    revision: str
    mode: str
    bands: tuple[str, str]
    epsg: int
    resolution: int

    @property
    def key(self):
        return self.base + "_rtc"

    @property
    def release(self):
        return f"{self.base}_{self.revision}"

    @property
    def criteria(self):
        return {"start": self.date, "end": self.date, "radar_mode": self.mode}


BEIJING = Case("beijing-iw", "2024-06-12", "116.350,39.900,116.355,39.905",
               "S1A_IW_GRDH_1SDV_20240612T101408_20240612T101433_054289_069A97", "9D5E", "IW", ("VV", "VH"), 32650, 10)
SVALBARD = Case("svalbard-iw", "2024-06-15", "15.60,78.22,15.67,78.25",
                "S1A_IW_GRDH_1SDH_20240615T154541_20240615T154606_054336_069C31", "E720", "IW", ("HH", "HV"), 32633, 20)
SVALBARD_CROSS = Case("svalbard-iw-cross-zone", SVALBARD.date, "18.414,78.365,18.430,78.375",
                      SVALBARD.base, SVALBARD.revision, "IW", SVALBARD.bands, 32634, 10)
BEIJING_EDGE = Case("beijing-iw-edge", BEIJING.date, "117.170,40.070,117.240,40.110",
                    BEIJING.base, BEIJING.revision, "IW", BEIJING.bands, 32650, 10)
EW = Case("southern-ocean-ew", "2022-03-08", "86.063,-59.508,86.073,-59.499",
          "S1A_EW_GRDM_1SDH_20220308T134445_20220308T134521_042230_050877", "6CDE", "EW", ("HH", "HV"), 32745, 10)


def get(session, url, status=200, **kwargs):
    try:
        response = session.get(url, timeout=(12, 45), allow_redirects=False, **kwargs)
    except requests.RequestException as error:
        raise RuntimeError(f"Independent RTC request failed ({type(error).__name__})") from None
    if response.status_code != status:
        actual = response.status_code
        response.close()
        raise RuntimeError(f"Independent RTC service returned HTTP {actual}; expected {status}")
    return response


def utc(value):
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(dt.timezone.utc)


def temporary_read_token(session):
    with get(session, TOKEN_URL) as response:
        try:
            value = response.json()
            token = value["token"]
            fields = dict(parse_qsl(token, strict_parsing=True, max_num_fields=32))
            if (not 1 <= len(token) <= 4096 or "?" in token or "#" in token or not fields.get("sig")
                    or fields.get("sp") not in ("r", "rl") or utc(value["msft:expiry"]) <= dt.datetime.now(dt.timezone.utc)):
                raise ValueError
            return token
        except (KeyError, TypeError, ValueError):
            raise RuntimeError("Independent RTC service did not supply a valid short-lived read token") from None


class ReferenceBlob:
    """Fetch only specific TIFF metadata/tiles via Azure, not the app's RangeFile or GDAL warp."""
    def __init__(self, session, url, tag, size, token):
        assert url.startswith(OBJECT_ROOT) and "?" not in url and "#" not in url
        assert re.fullmatch(r'"0x[0-9a-fA-F]{1,32}"', tag) and 0 < size <= 8 * 1024**3
        self.session, self.url, self.tag, self.size, self.token = session, url, tag, size, token
        self.bytes_verified = 0
        self.ranges = 0

    def fetch(self, begin, length):
        last = begin + length - 1
        assert 0 < length <= 4 * 1024**2 and 0 <= begin <= last < self.size
        headers = {"Range": f"bytes={begin}-{last}", "If-Match": self.tag, "Accept-Encoding": "identity",
                   "x-ms-range-get-content-md5": "true"}
        with get(self.session, self.url + "?" + self.token, status=206, headers=headers, stream=True) as response:
            assert response.headers.get("Content-Encoding", "identity").lower() == "identity"
            assert response.headers["Content-Range"] == f"bytes {begin}-{last}/{self.size}"
            assert int(response.headers["Content-Length"]) == length and response.headers["ETag"] == self.tag
            stored = response.headers.get("Content-MD5", "")
            try:
                data = response.raw.read(length + 1)
            except Exception as error:
                raise RuntimeError(f"Independent RTC range failed ({type(error).__name__})") from None
        assert len(data) == length
        assert base64.b64encode(hashlib.md5(data, usedforsecurity=False).digest()).decode("ascii") == stored
        self.bytes_verified += length
        self.ranges += 1
        return data

    def pixels(self, x, y, epsg, spec):
        # The fixed fixtures are COGs with their TIFF directories and block tables at the front.
        # Fail loudly on any unsupported layout rather than pretending an independent comparison succeeded.
        header = self.fetch(0, min(self.size, 512 * 1024))
        with tifffile.TiffFile(io.BytesIO(header)) as tiff:
            page = tiff.pages[0]
            assert page.is_tiled and page.dtype == np.dtype("float32") and page.axes == "YX"
            assert len(page.databytecounts) == len(page.dataoffsets) and page.nodata == FILL
            assert not page.pages and not any(p.is_mask for p in tiff.pages)
            tags = page.geotiff_tags
            assert tags["GTRasterTypeGeoKey"] == 1
            sx, sy, _ = tags["ModelPixelScale"]
            i, j, _, ulx, uly, _ = tags["ModelTiepoint"]
            assert sx > 0 and sy > 0 and (i, j) == (0, 0)
            assert tags["ProjectedCSTypeGeoKey"] == spec["epsg"]
            assert list(page.shape) == spec["shape"] and [sx, 0, ulx, 0, -sy, uly] == spec["transform"]
            if epsg != spec["epsg"]:
                x, y = Transformer.from_crs(epsg, spec["epsg"], always_xy=True).transform(x, y)
            col, row = np.floor((x-ulx)/sx).astype(int), np.floor((uly-y)/sy).astype(int)
            inside = (row >= 0) & (row < page.imagelength) & (col >= 0) & (col < page.imagewidth)
            across = math.ceil(page.imagewidth/page.tilewidth)
            indices = (row // page.tilelength)*across + col // page.tilewidth
            sampled = np.full(x.shape, FILL)
            for index in np.unique(indices[inside]):
                length, offset = page.databytecounts[index], page.dataoffsets[index]
                if length == 0 and offset == 0:
                    continue
                assert length > 0 and offset > 0
                tile, position, _ = page.decode(self.fetch(offset, length), index)
                assert tile is not None and tile.shape == (1, page.tilelength, page.tilewidth, 1)
                _, base_y, base_x = position[1:4]
                where = inside & (indices == index)
                sampled[where] = tile[0, row[where]-base_y, col[where]-base_x, 0]
        return sampled


def expected_grid(case):
    west, south, east, north = map(float, case.bbox.split(","))
    verts = [(west, south), (east, south), (east, north), (west, north)]
    # Project the rectangle edges, then independently use ray-casting on its projected boundary.
    border = [point for a, b in zip(verts, verts[1:] + verts[:1]) for point in
              np.linspace(a, b, math.ceil(math.dist(a, b)/0.05), endpoint=False)]
    xs, ys = Transformer.from_crs(4326, case.epsg, always_xy=True).transform(*np.asarray(border).T)
    step = case.resolution
    left, right = math.floor(xs.min()/step)*step, math.ceil(xs.max()/step)*step
    bottom, top = math.floor(ys.min()/step)*step, math.ceil(ys.max()/step)*step
    xx, yy = np.meshgrid(np.arange(left+step/2, right, step), np.arange(top-step/2, bottom, -step))
    inside = np.zeros(xx.shape, dtype=bool)
    for x0, y0, x1, y1 in zip(xs, ys, np.roll(xs, -1), np.roll(ys, -1)):
        if y1 != y0:
            inside ^= ((y0 > yy) != (y1 > yy)) & (xx < x0 + (yy-y0)*(x1-x0)/(y1-y0))
    assert 0 < inside.sum() < inside.size
    return (step, 0, left, 0, -step, top), (left, bottom, right, top), xx, yy, inside


def compare(case, upstream, result, plan, version):
    file, = result["files"]
    assert file["scene"] == case.key and file["status"] == "completed"
    path, manifest_path = Path(file["path"]), Path(result["manifest"])
    receipt_path = path.with_suffix(".starmap.json")
    texts = [p.read_text(encoding="utf-8") for p in (manifest_path, receipt_path)]
    assert not any(SECRET_MARKER.search(text) for text in texts), "RTC access URL was persisted"
    manifest, receipt = (json.loads(text) for text in texts)
    record, = manifest["items"]
    assert manifest["status"] == record["status"] == "completed" and manifest["software"] == version
    assert manifest["collection"] == COLLECTION and manifest["plan"] == plan == result["plan"]
    assert record["scene"] == case.key and record["catalogue_product_id"] == case.release
    assert set(record["assets"]) == set(record["transport"]) == set(case.bands)
    with path.open("rb") as stream:
        sha = hashlib.file_digest(stream, "sha256").hexdigest()
    assert record["sha256"] == receipt["sha256"] == sha and record["size"] == receipt["size"] == path.stat().st_size
    assert receipt["file"] == path.name and receipt["request_fingerprint"] == plan["fingerprint"]

    with get(upstream, STAC_ROOT + case.key) as response:
        item = response.json()
    assert item["id"] == case.key and item["collection"] == COLLECTION
    properties = item["properties"]
    assert properties["sar:instrument_mode"] == case.mode and set(properties["sar:polarizations"]) == set(case.bands)
    assert utc(record["acquisition_utc"]) == utc(properties["datetime"])
    transform, bounds, xx, yy, inside = expected_grid(case)
    assert list(bounds) == plan["bounds"] and xx.shape == (plan["height"], plan["width"])
    assert plan["epsg"] == case.epsg and plan["resolution"] == case.resolution
    token = temporary_read_token(upstream)
    valid, evidence = [], {}
    with rasterio.open(path) as output:
        tags = output.tags()
        assert output.driver == "GTiff" and output.crs.to_epsg() == case.epsg and output.shape == xx.shape
        assert output.transform[:6] == transform and output.descriptions == case.bands
        assert output.dtypes == ("float32",)*2 and output.nodatavals == (FILL,)*2
        assert output.scales == (1., 1.) and output.offsets == (0., 0.)
        assert tags["radar_backscatter"] == "linear_gamma0" and tags["resampling"] == "nearest"
        assert tags["per_band_nodata"] == "true" and tags["cloud_mask"] == "False"
        assert utc(tags["acquisition_utc"]) == utc(properties["datetime"])
        assert not any(SECRET_MARKER.search(text) for text in tags.values())
        for i, band in enumerate(case.bands, start=1):
            source = item["assets"][band.lower()]
            asset, transfer = record["assets"][band], record["transport"][band]
            url, total = source["href"], source["file:size"]
            assert url == asset["url"] and f"/{case.mode}/" in url and f"/{case.release}/measurement/" in url
            assert total == asset["object_bytes"] == transfer["object_bytes"]
            assert 0 < transfer["azure_md5_verified_ranges"] <= transfer["range_requests"]
            assert 0 < transfer["bytes_received"] < total // 20, "Small crop unexpectedly fetched >=5% of a source"
            reference = ReferenceBlob(upstream, url, transfer["etag"], total, token)
            original = reference.pixels(xx, yy, case.epsg, asset)
            masked = inside & np.isfinite(original) & (original != FILL)
            assert np.any(masked) and np.any(~masked) and np.any(original[masked] > 0)
            expected = np.where(masked, original, FILL)
            np.testing.assert_array_equal(output.read_masks(i) == 255, masked)
            np.testing.assert_array_equal(output.read(i), expected)
            valid.append(masked)
            evidence[band] = {**transfer, "source_epsg": asset["epsg"], "valid_pixels": int(masked.sum()), "nodata_pixels": int((~masked).sum()),
                              "source_nodata_inside_aoi": int((inside & ~masked).sum()),
                              "min_linear": float(original[masked].min()), "max_linear": float(original[masked].max()),
                              "independent_verified_ranges": reference.ranges, "independent_verified_bytes": reference.bytes_verified}
        union = int(np.any(valid, axis=0).sum())
        assert int(tags["valid_pixels"]) == record["valid_pixels"] == receipt["valid_pixels"] == union
        assert record["valid_pixels_per_band"] == receipt["valid_pixels_per_band"] == {b: v["valid_pixels"] for b, v in evidence.items()}
    if case == SVALBARD_CROSS:
        assert all(b["source_epsg"] == 32633 for b in evidence.values()) and case.epsg == 32634
    if case == BEIJING_EDGE:
        assert plan["width"] > 512, "The footprint-edge fixture should span two app output blocks"
        assert all(b["source_nodata_inside_aoi"] > 0 for b in evidence.values()), "Expected source NoData inside this AOI"
    return {"case": case.name, "catalogue_id": case.key, "blob_revision": case.release, "mode": case.mode,
            "date_utc": case.date, "bbox_wgs84": case.bbox, "epsg": case.epsg, "resolution_m": case.resolution,
            "width": plan["width"], "height": plan["height"], "band_order": case.bands, "bands": evidence,
            "valid_pixels_union": union, "sha256": sha, "size": path.stat().st_size, "geotiff": str(path),
            "manifest": str(manifest_path), "checked_every_output_pixel": True, "max_absolute_error": 0.0}


def verify_browser(origin, out, cases):
    from playwright.sync_api import expect, sync_playwright
    with sync_playwright() as driver:
        browser = driver.chromium.launch(channel=os.environ.get("STARMAP_TEST_BROWSER", "msedge"), headless=True)
        page = browser.new_page(viewport={"width": 1360, "height": 900})
        errors = []
        page.on("pageerror", lambda err: errors.append(str(err)))
        page.route("https://**/*", lambda route: route.abort())  # No map tiles; the app server makes real imagery requests.
        page.goto(origin, wait_until="domcontentloaded")
        expect(page.locator("#outputDirectory")).not_to_have_value("")
        results = []
        for case in cases:
            page.locator("#sourceProfile").select_option(PROFILE)
            expect(page.locator("#rtcOptions")).to_be_visible()
            expect(page.locator('#radarMode option[value="WV"]')).to_be_disabled()
            page.locator('[data-tab="coordinates"]').click()
            page.locator("#coordinates").fill(case.bbox)
            page.locator("#applyCoordinates").click()
            expect(page.locator("#aoiStats")).to_contain_text("WGS84")
            page.locator("#startDate").fill(case.date)
            page.locator("#endDate").fill(case.date)
            page.locator("#radarMode").select_option(case.mode)
            page.locator("#searchScenes").click()
            row = page.locator(f'#scenesBody tr[data-key="{case.key}"]')
            expect(row).to_be_visible(timeout=90000)
            expect(row).to_contain_text(case.mode + " · " + "/".join(case.bands))
            page.locator("#selectNone").click()
            row.locator("input").check()
            expect(page.locator("#rtcBandAvailability")).to_contain_text(" / ".join(case.bands))
            expect(page.locator("#scenesSubtitle")).not_to_contain_text("云量")
            for band in ("VV", "VH", "HH", "HV"):
                inp = page.locator(f'#rtcBands input[value="{band}"]')
                if band not in case.bands:
                    if inp.is_checked():
                        expect(page.locator("#downloadSelected")).to_be_disabled()
                        inp.uncheck()
                    expect(inp).to_be_disabled()
                else:
                    inp.check()
            page.locator("#rtcResolution").select_option(str(case.resolution))
            page.locator("#outputDirectory").fill(str(out))
            page.locator("#downloadSelected").click(timeout=20000)
            expect(page.locator("#planDetails .band-list")).to_have_text(" / ".join(case.bands))
            expect(page.locator("#planNote")).to_contain_text("不转 dB")
            page.locator("#confirmDownload").click()
            expect(page.locator("#jobStatus")).to_have_text("下载完成", timeout=30000)
            expect(page.locator("#jobFiles .job-file")).to_have_count(1)
            expect(page.locator("#jobFiles .job-file")).to_contain_text("已有")
            page.locator("#sourceProfile").select_option("s1-aws-original")
            expect(page.locator("#rtcOptions")).to_be_hidden()
            expect(page.locator('#radarMode option[value="WV"]')).to_be_enabled()
            expect(page.locator("#sceneCount")).to_have_text("0")
            expect(page.locator("#downloadSelected")).to_be_disabled()
            results.append(case.name)
        assert not errors
        browser.close()
    return {"checked_cases": results, "switch_to_grd_clears_selection": True, "verified_existing_after_real_transfer": True}


def check(out, executable=None, cases=(BEIJING, SVALBARD, BEIJING_EDGE), browser=True):
    out.mkdir(parents=True, exist_ok=True)
    report_path = out / "rtc-live-report.json"
    if report_path.exists() or any(out.glob("s1_rtc_pc_*")):
        raise FileExistsError("Use a fresh output directory for live RTC acceptance")
    report = {"exe": str(executable) if executable else "source", "verified_cases": [], "browser": None, "status": "running"}

    def save():
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    with local_app(executable, out, settings_dir=out / ".verify-settings") as (origin, local), requests.Session() as upstream:
        def read(r, status):
            with r:
                assert r.status_code == status, (r.status_code, r.text[:500])
                return r.json()

        def post(action, value, status=200):
            return read(local.post(f"{origin}/api/{action}", json=value, timeout=15), status)

        def run(action, value, expect="completed"):
            jid = post(action, value)["job_id"]
            until = time.monotonic() + 300
            while time.monotonic() < until:
                job = read(local.get(f"{origin}/api/job", params={"id": jid}, timeout=8), 200)
                if job["status"] in ("completed", "failed", "cancelled"):
                    assert expect == job["status"], (action, job.get("error"), job["status"])
                    return job["result"] if expect == "completed" else job
                time.sleep(.25)
            raise TimeoutError(f"RTC local {action} did not finish")

        report["version"] = read(local.get(f"{origin}/api/bootstrap", timeout=8), 200)["version"]
        save()
        for case in cases:
            print(f"Live RTC: {case.name} ({case.mode} {'/'.join(case.bands)} {case.resolution} m)", flush=True)
            aoi = post("aoi", {"coordinates": case.bbox})["geometry"]
            common = {"geometry": aoi, "profile": PROFILE}
            found = run("search", {**common, **case.criteria})
            matching, = (row for row in found["scenes"] if row["key"] == case.key)
            assert matching["public"]["source_id"] == case.release and matching["tiles"] == [case.mode + " · " + "/".join(case.bands)]
            args = {**common, "criteria": case.criteria, "query_id": found["query"]["id"], "scene_keys": [case.key], "output_dir": str(out),
                    "options": {"mode": "rtc", "resolution": case.resolution, "bands": list(case.bands)}}
            plan = post("plan", args)
            assert plan["processing"] == "linear_gamma0" and plan["source_objects"] == 2 and plan["estimated_pu"] is None
            assert plan["kind"] == "public" and plan["collection"] == COLLECTION and not plan["options"]["mask_clouds"]
            wrong = "HH" if "VV" in case.bands else "VV"
            assert wrong in post("plan", {**args, "options": {**args["options"], "bands": [wrong]}}, 400)["error"]
            result = run("download", args)
            proof = compare(case, upstream, result, plan, report["version"])
            repeat, = run("download", args)["files"]
            assert repeat["status"] == "skipped" and repeat["path"] == proof["geotiff"]
            proof["repeat_verified"] = proof["missing_polarization_blocked"] = True
            if case == BEIJING:
                path = Path(proof["geotiff"])
                original = path.read_bytes()
                bad = original[:-1] + bytes([original[-1] ^ 1])
                path.write_bytes(bad)
                try:
                    fail = run("download", args, "failed")
                    assert "内容已变化" in fail["error"] and path.read_bytes() == bad
                finally:
                    path.write_bytes(original)
                proof["tampered_output_rejected_without_overwrite"] = True
            report["verified_cases"].append(proof)
            save()
            print(f"  exact match, both bands: {proof['width']} x {proof['height']}", flush=True)
        if browser:
            report["browser"] = verify_browser(origin, out, cases)
    assert not list(out.rglob("*.partial.tif"))
    report["status"] = "completed"
    save()
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--exe", type=Path, help="Installed or packaged executable; otherwise run from source")
    parser.add_argument("--output", type=Path, required=True, help="Fresh output folder")
    fixtures = {case.name: case for case in (BEIJING, SVALBARD, BEIJING_EDGE, EW, SVALBARD_CROSS)}
    scenario = parser.add_mutually_exclusive_group()
    scenario.add_argument("--only", choices=fixtures, help="Verify a single pinned fixture")
    scenario.add_argument("--include-ew", action="store_true", help="Also require the pinned southern-ocean EW fixture")
    parser.add_argument("--no-browser", action="store_true", help="Skip the Edge browser workflow")
    args = parser.parse_args()
    defaults = (BEIJING, SVALBARD, BEIJING_EDGE)
    cases = (fixtures[args.only],) if args.only else (*defaults, EW) if args.include_ew else defaults
    result = check(args.output.resolve(), args.exe.resolve(strict=True) if args.exe else None, cases, not args.no_browser)
    print(json.dumps(result, ensure_ascii=True, indent=2))
