"""Verify full-city monthly outputs, every pixel's AOI contract, real map tiles and reuse."""
from __future__ import annotations

import argparse
import collections
import hashlib
import json
import math
import os
import shutil
import sys
import time
from contextlib import ExitStack
from pathlib import Path

import numpy as np
import rasterio
from pyproj import Transformer
from shapely import contains_xy, prepare
from shapely.geometry import shape

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from core.geometry import load_aoi
from core.download_tasks import DownloadTasks
from core.planning import RESEARCH_BANDS
from core.research_bundle import CLEAR_CLASSES, NODATA
from scripts.browser_flow import open_data_page
from scripts.live_public import local_app


def write(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")


def verify_month(row, geometry):
    result = row["result"]
    assert result and len(result["files"]) == 1, row
    file = result["files"][0]
    receipt = json.loads(Path(file["path"]).with_suffix(".starmap.json").read_text(encoding="utf-8"))
    paths = {"analysis": file["path"], **file["bundle"]}
    assert all(Path(path).is_file() for path in paths.values())
    counts, analysis_counts = collections.Counter(), collections.Counter()
    valid_pixels, raw_pixels, outside_pixels, aoi_pixels, partial_pixels = 0, 0, 0, 0, 0
    band_pixels = np.zeros(len(RESEARCH_BANDS), dtype=np.int64)
    with ExitStack() as stack:
        datasets = {role: stack.enter_context(rasterio.open(path)) for role, path in paths.items() if role != "sources"}
        main = datasets["analysis"]
        assert main.count == datasets["unmasked"].count == 10
        assert main.descriptions == datasets["unmasked"].descriptions == tuple(RESEARCH_BANDS)
        assert main.dtypes == datasets["unmasked"].dtypes == ("float32",) * 10
        assert main.nodata == datasets["unmasked"].nodata == NODATA
        assert datasets["scl"].dtypes == ("uint8",) and datasets["source"].dtypes == ("uint16",)
        assert all(ds.transform == main.transform and ds.crs == main.crs and ds.shape == main.shape for ds in datasets.values())
        assert main.width == result["plan"]["width"] and main.height == result["plan"]["height"]
        converter = Transformer.from_crs(main.crs, 4326, always_xy=True)
        prepare(geometry)
        for _, window in main.block_windows(1):
            values = main.read(window=window)
            raw = datasets["unmasked"].read(window=window)
            scl = datasets["scl"].read(1, window=window)
            source = datasets["source"].read(1, window=window)
            y, x = np.mgrid[0:int(window.height), 0:int(window.width)]
            x, y = x + window.col_off + .5, y + window.row_off + .5
            east = main.transform.c + x * main.transform.a + y * main.transform.b
            north = main.transform.f + x * main.transform.d + y * main.transform.e
            longitude, latitude = converter.transform(east, north)
            inside = contains_xy(geometry, longitude, latitude)
            aoi_pixels += int(inside.sum())
            outside_pixels += int((~inside).sum())
            assert np.all(source[~inside] == 0)
            assert np.all(raw[:, ~inside] == NODATA) and np.all(values[:, ~inside] == NODATA)
            assert np.all(scl[~inside] == 255)
            assert np.all(np.isfinite(raw))
            available = raw != NODATA
            present = source > 0
            np.testing.assert_array_equal(present, available.any(axis=0))
            complete = available.all(axis=0)
            # Native bands can have different missing margins. The backup keeps
            # each available value; the analysis requires a complete spectrum.
            expected = complete & np.isin(scl, CLEAR_CLASSES)
            assert np.all(np.isin(scl, [*range(12), 255]))
            np.testing.assert_array_equal(values[:, expected], raw[:, expected])
            assert np.all(values[:, ~expected] == NODATA)
            valid_pixels += int(expected.sum())
            raw_pixels += int(present.sum())
            partial_pixels += int((present & ~complete).sum())
            band_pixels += available.sum(axis=(1, 2))
            unique, pixels = np.unique(source[present], return_counts=True)
            counts.update({int(key): int(value) for key, value in zip(unique, pixels)})
            unique, pixels = np.unique(source[expected], return_counts=True)
            analysis_counts.update({int(key): int(value) for key, value in zip(unique, pixels)})
        grid = {"width": main.width, "height": main.height, "epsg": main.crs.to_epsg(),
                "transform": list(main.transform)[:6], "bounds": list(main.bounds)}
    assert aoi_pixels > 0 and outside_pixels > 0 and raw_pixels > 0
    assert valid_pixels == receipt["valid_pixels"] and raw_pixels == receipt["unmasked_valid_pixels"]
    assert result["used_dates"] and all(date[:7] == row["month"] for date in result["used_dates"])
    table = json.loads(Path(file["bundle"]["sources"]).read_text(encoding="utf-8"))
    # Both the authoritative source list and the actual used dates must stay in this month.
    source_rows = table["sources"]
    assert all(source["acquisition_utc"][:7] == row["month"] for source in source_rows)
    assert all(key <= len(source_rows) for key in counts)
    assert {int(source["index"]): source["used_pixels"] for source in source_rows if source["used_pixels"]} == dict(counts)
    assert {int(source["index"]): source["analysis_pixels"] for source in source_rows if source["analysis_pixels"]} == dict(analysis_counts)
    hashes = {}
    for role, path in paths.items():
        with Path(path).open("rb") as handle:
            hashes[role] = hashlib.file_digest(handle, "sha256").hexdigest()
        recorded = receipt if role == "analysis" else receipt["bundle"][role]
        assert hashes[role] == recorded["sha256"] and Path(path).stat().st_size == recorded["size"]
    return {"month": row["month"], "grid": grid, "aoi_pixels": aoi_pixels, "outside_pixels": outside_pixels,
            "unmasked_pixels": raw_pixels, "analysis_pixels": valid_pixels, "source_pixels": dict(counts),
            "analysis_pixels_by_source": dict(analysis_counts), "partial_spectral_pixels": partial_pixels,
            "unmasked_band_pixels": {band: int(count) for band, count in zip(RESEARCH_BANDS, band_pixels)},
            "used_dates": result["used_dates"], "paths": paths, "sha256": hashes,
            "pixel_contract": "all raster blocks checked against full WGS84 city, SCL and backup"}


def browser_check(origin, out, resolution=60):
    from playwright.sync_api import sync_playwright, expect
    errors, responses = [], []
    with sync_playwright() as driver:
        browser = driver.chromium.launch(channel=os.environ.get("STARMAP_TEST_BROWSER", "msedge"), headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 960})
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.on("response", lambda response: responses.append({"status": response.status, "path": response.url.split(origin, 1)[-1]})
                if "/map/tiles/" in response.url else None)
        open_data_page(page, origin)
        page.wait_for_function("()=>state.aoi?.region?.city_id && Monthly.query && !state.busy && state.plan?.kind==='monthly'", timeout=120000)
        expect(page.locator("#monthlyFields")).to_be_visible()
        expect(page.locator("#dateFields")).to_be_hidden()
        expect(page.locator("#companionSection")).to_be_hidden()
        page.locator("#resolution").select_option(str(resolution))
        page.wait_for_function("()=>!state.busy&&state.plan?.kind==='monthly'&&state.plan.months.filter(row=>row.plan).every(row=>row.plan.resolution===Number(document.querySelector('#resolution').value))", timeout=120000)
        assert page.evaluate("state.aoi.vertices") > 14000
        expect(page.locator("#monthlyResults")).to_be_visible()
        tiles_ready = "()=>baseLayer&&!baseLayer.isLoading()&&Object.values(baseLayer._tiles).some(row=>row.current)&&Object.values(baseLayer._tiles).filter(row=>row.current).every(row=>row.el.complete&&row.el.naturalWidth>=256)"
        page.wait_for_function(tiles_ready, timeout=90000)
        standard_tiles = page.evaluate("()=>Object.values(baseLayer._tiles).filter(row=>row.current).length")
        page.screenshot(path=str(out / "city-monthly-standard.png"), full_page=True)
        months = page.locator("#monthResultSelect option").evaluate_all("rows=>rows.map(row=>row.value)")
        for month in months:
            page.locator("#monthResultSelect").select_option(month)
            expect(page.locator("#scenesSubtitle")).to_contain_text(month)
        page.locator('[data-basemap="satellite"]').click()
        page.wait_for_function(tiles_ready, timeout=90000)
        satellite_tiles = page.evaluate("()=>Object.values(baseLayer._tiles).filter(row=>row.current).length")
        page.screenshot(path=str(out / "city-monthly-satellite.png"), full_page=True)
        assert any(row["status"] == 200 and "/satellite/" in row["path"] for row in responses), responses
        assert any(row["status"] == 200 and "/standard/" in row["path"] for row in responses), responses
        page.locator("#downloadSelected").click()
        expect(page.locator("#downloadDialog")).to_be_visible()
        page.screenshot(path=str(out / "city-monthly-plan.png"), full_page=True)
        assert not errors, errors
        browser.close()
    return {"months": months, "actual_same_origin_tile_responses": responses, "page_errors": errors,
            "all_visible_tiles_loaded": {"standard": standard_tiles, "satellite": satellite_tiles},
            "restored_month_controls": True, "resolution": resolution}


def run(args):
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=True)
    settings = out / "settings"
    if args.copy_accounts:
        original = Path(os.environ["LOCALAPPDATA"]) / "CopernicusDownloader" / "accounts.v1.dpapi"
        target = settings / "CopernicusDownloader" / original.name
        target.parent.mkdir(parents=True, exist_ok=True)
        if original.is_file() and not target.exists():
            shutil.copyfile(original, target)
    report = {"status": "running", "city_id": args.city, "profile": args.profile, "start_month": args.start_month,
              "end_month": args.end_month, "resolution": args.resolution, "download_requested": args.download}
    if args.resume_job and (out / "report.json").is_file():
        shutil.copyfile(out / "report.json", out / "before-cold-resume-report.json")
    write(out / "report.json", report)
    with local_app(args.exe.resolve() if args.exe else None, out, settings_dir=settings) as (origin, session):
        def get(route):
            response = session.get(origin + "/api/" + route, timeout=30)
            response.raise_for_status()
            return response.json()
        def post(route, value):
            response = session.post(origin + "/api/" + route, json=value, timeout=30)
            if response.status_code != 200:
                raise RuntimeError(response.json().get("error", "Local operation failed"))
            return response.json()
        def job(route, value, *, deadline=7200):
            begin = time.monotonic()
            identifier = post(route, value)["job_id"]
            last = None
            while time.monotonic() - begin < deadline:
                snapshot = get("job?id=" + identifier)
                if snapshot["message"] != last:
                    last = snapshot["message"]
                    print(route, snapshot["status"], round(snapshot["progress"] * 100, 1), last, flush=True)
                if snapshot["status"] not in ("running", "cancelling"):
                    write(out / (route + "-job.json"), snapshot)
                    assert snapshot["status"] == "completed", snapshot["error"] or snapshot["message"]
                    return snapshot["result"], time.monotonic() - begin
                time.sleep(1)
            post("cancel", {"job_id": identifier})
            raise TimeoutError("Live verification exceeded the task deadline; verified download progress was retained.")
        for _ in range(180):
            bootstrap = get("bootstrap")
            if not bootstrap["accounts"]["restoring"]:
                break
            time.sleep(1)
        aoi = post("aoi", {"city_id": args.city})
        geometry = shape(aoi["geometry"])
        report["version"] = bootstrap["version"]
        report["city"] = {key: value for key, value in aoi.items() if key != "geometry"}
        print(aoi["name"], aoi["vertices"], aoi["area_km2"], flush=True)
        resumed, resumed_groups, resumed_plan = None, {}, None
        if args.resume_job:
            kind, definition = DownloadTasks(settings / "CopernicusDownloader").load(args.resume_job)
            assert kind == "monthly-download" and shape(definition["geometry"]).equals(geometry)
            resumed_plan = definition["plan"]
            assert resumed_plan["profile"] == args.profile
            assert resumed_plan["start_month"] == args.start_month and resumed_plan["end_month"] == args.end_month
            resumed_groups = {group["month"]: group for group in definition["groups"]}
            assert all(group["options"]["resolution"] == args.resolution for group in resumed_groups.values() if group["scenes"])
            resumed, elapsed = job("retry-download", {"job_id": args.resume_job})
            report["cold_resume"] = {"original_id": args.resume_job, "seconds": elapsed,
                                     "original_geometry_months_products_and_options_restored": True}
            write(out / "report.json", report)
        query = {"city_id": args.city, "profile": args.profile, "start_month": args.start_month,
                 "end_month": args.end_month, "max_cloud": 100}
        found, elapsed = job("monthly-search", query)
        monthly = found["monthly"]
        report["search_seconds"] = elapsed
        request = {"city_id": args.city, "profile": args.profile, "monthly_id": monthly["id"],
                   "start_month": args.start_month, "end_month": args.end_month, "output_dir": str(out / "data"),
                   "months": [{"month": group["month"], "include": True, "query_id": group["query"]["id"],
                               "criteria": group["criteria"],
                               "scene_keys": resumed_groups.get(group["month"], {}).get("scene_keys", group["recommended_keys"]),
                               "options": resumed_groups.get(group["month"], {}).get("options", {
                                           "mode": "research", "resolution": args.resolution, "max_cloud": 100,
                                           "mask_clouds": True, "layout": "mosaic", "priority": "cloud", "fill_gaps": True})}
                              for group in monthly["groups"]]}
        assert not any(group["error"] for group in monthly["groups"])
        plan, elapsed = job("prepare-plan", request)
        report.update(plan=plan, planning_seconds=elapsed)
        assert all(row["plan"] and row["plan"]["coverage_percent"] >= 99.9999 for row in plan["months"]), plan["months"]
        write(out / "report.json", report)
        if args.download:
            request["plan_fingerprint"] = plan["fingerprint"]
            if resumed is not None:
                assert plan["fingerprint"] == resumed_plan["fingerprint"], "Cold resume must retain the exact original monthly plan"
                result, elapsed = resumed, report["cold_resume"]["seconds"]
            else:
                result, elapsed = job("monthly-download", request)
            report["download_seconds"] = elapsed
            assert all(row["status"] == "completed" for row in result["months"])
            assert load_aoi(Path(result["folder"]) / "研究区.shp").equals(geometry)
            report["months"] = [verify_month(row, geometry) for row in result["months"]]
            if args.reuse:
                product_paths = {path for file in result["files"]
                                 for path in (file["path"], *file["bundle"].values(),
                                              str(Path(file["path"]).with_suffix(".starmap.json")))}
                product_paths.update(str(Path(result["folder"]) / ("研究区" + suffix))
                                     for suffix in (".shp", ".shx", ".dbf", ".prj", ".cpg"))
                previous = {path: Path(path).stat().st_mtime_ns for path in product_paths}
                repeated, elapsed = job("monthly-download", request)
                assert all(file["status"] == "skipped" for file in repeated["files"])
                assert all(Path(path).stat().st_mtime_ns == timestamp for path, timestamp in previous.items())
                report["reuse"] = {"seconds": elapsed, "skipped_months": len(repeated["files"]),
                                   "unchanged_bundle_and_boundary_files": len(previous), "file_times_unchanged": True}
        if args.browser:
            report["browser"] = browser_check(origin, out, args.resolution)
        report["map_cache"] = get("map-status")
        report["status"] = "passed"
        write(out / "report.json", report)
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--exe", type=Path)
    parser.add_argument("--city", default="110000")
    parser.add_argument("--profile", choices=("s2-earth-search", "s2-cdse-s3", "s2-process"), default="s2-earth-search")
    parser.add_argument("--start-month", default="2026-04")
    parser.add_argument("--end-month", default="2026-05")
    parser.add_argument("--resolution", choices=(10, 20, 60), type=int, default=60)
    parser.add_argument("--download", action="store_true")
    parser.add_argument("--reuse", action="store_true")
    parser.add_argument("--browser", action="store_true")
    parser.add_argument("--copy-accounts", action="store_true", help="Copy the Windows encrypted vault into isolated verification settings")
    parser.add_argument("--resume-job", help="Cold-resume a frozen monthly task in this output's isolated configuration")
    args = parser.parse_args()
    if args.resume_job and not args.download:
        parser.error("--resume-job requires --download")
    try:
        return run(args)
    except Exception as error:
        path = args.out.resolve() / "failure.json"
        write(path, {"status": "failed", "type": type(error).__name__, "message": str(error)})
        print(type(error).__name__, str(error), flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
