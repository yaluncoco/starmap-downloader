"""Verify shared catalogue criteria and small public downloads through the packaged API."""
from __future__ import annotations

import argparse
import copy
import datetime as dt
import hashlib
import json
import os
import tempfile
import time
from pathlib import Path

import numpy as np
import rasterio
import shapefile
from shapely.geometry import shape

from live_public import B04_SAFE, BBOX, local_app
from live_s2_tiles import Local

DATES = {"start": "2024-06-10", "end": "2024-06-10"}
OPTICAL = {"mode": "custom", "bands": ["B04"], "resolution": 10, "max_cloud": 100, "mask_clouds": True}
GEDI_KEY = "gedi03-003"


from browser_flow import open_data_page

def wait_job(api, route, value, timeout=600):
    key = api.post(route, value)["job_id"]
    end = time.monotonic() + timeout
    previous = None
    while time.monotonic() < end:
        response = api.session.get(api.origin + "/api/job", params={"id": key}, timeout=10)
        response.raise_for_status()
        job = response.json()
        if job["message"] != previous:
            print(job["message"], flush=True)
            previous = job["message"]
        if job["status"] not in ("running", "cancelling"):
            if job["status"] != "completed":
                raise RuntimeError(f"{route} finished {job['status']}: {job.get('error') or job['message']}")
            return job["result"]
        time.sleep(.45)
    api.post("cancel", {"job_id": key})
    raise RuntimeError(f"{route} verification timed out")


def audit_source(record):
    result = record["results"][0]
    path = Path(result["files"][0]["path"])
    receipt = json.loads(path.with_suffix(".starmap.json").read_text(encoding="utf-8"))
    with path.open("rb") as file:
        digest = hashlib.file_digest(file, "sha256").hexdigest()
    assert digest == receipt["sha256"]
    with rasterio.open(path) as image:
        data = image.read(1, masked=True)
        assert image.crs.to_epsg() == 32650 and data.count() > 0
        assert np.isfinite(data.compressed()).all()
        expected_band, expected_shape, expected_valid, median_range = (
            ("B04", (57, 44), 2337, (.11, .13)) if record["profile"] == "s2-earth-search"
            else ("DSM", (20, 15), 252, (48, 51)))
        assert image.descriptions == (expected_band,) and image.shape == expected_shape and data.count() == expected_valid
        median = float(np.median(data.compressed()))
        assert median_range[0] <= median <= median_range[1]
        return {"profile": record["profile"], "path": str(path), "sha256": digest, "grid": [image.width, image.height],
                "epsg": 32650, "resolution": list(image.res), "valid_pixels": int(data.count()), "median": median}


def browser_check(origin, output):
    from playwright.sync_api import expect, sync_playwright
    errors = []
    with sync_playwright() as driver:
        browser = driver.chromium.launch(channel=os.environ.get("STARMAP_TEST_BROWSER", "msedge"), headless=True)
        page = browser.new_page(viewport={"width": 1366, "height": 768})
        page.on("pageerror", lambda error: errors.append(str(error)))
        # Catalogue/download traffic uses the packaged backend. This page needs no remote map tiles.
        page.route("https://**", lambda route: route.abort())
        open_data_page(page, origin)
        expect(page.locator("#batchResultTabs [data-result-source]")).to_have_count(3)
        expect(page.locator("#manageAccounts")).to_be_visible()
        assert page.locator(".brand-mark img").evaluate("img => img.complete && img.naturalWidth > 0")
        page.locator('[data-result-source="gedi-earthdata"]').click()
        expect(page.locator("#companionResults")).to_contain_text("汇总期")
        expect(page.locator("#companionResults")).to_contain_text("2024-06-10")
        page.locator(".sidebar-scroll").evaluate("element => element.scrollTop = 0")
        page.screenshot(path=output / "packaged-batch-catalogue.png", full_page=True)
        page.locator("#manageAccounts").click()
        expect(page.locator("#accountsDialog")).to_be_visible()
        expect(page.locator("#earthdataConnect")).to_be_in_viewport(ratio=1)
        page.screenshot(path=output / "packaged-batch-accounts.png", full_page=True)
        page.locator('#accountsDialog .modal-footer [data-close]').click()
        assert not errors
        browser.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--exe", type=Path)
    parser.add_argument("--output", type=Path, default=Path(__file__).resolve().parents[1] / "verification" / "batch" / "live")
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    report_path = output / "batch-live-report.json"
    report = {"verified_utc": dt.datetime.now(dt.timezone.utc).isoformat(), "shared_dates": DATES,
              "bounds_wgs84": BBOX, "gedi_collection": GEDI_KEY, "gedi_authenticated_download": False,
              "note": "Real CMR/Sentinel-2/DEM catalogue, actual small Sentinel-2+DEM transfer; NASA protected transfer requires user authentication."}
    def save():
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    with tempfile.TemporaryDirectory(prefix="starmap-batch-settings-") as settings:
        with local_app(args.exe.resolve(strict=True) if args.exe else None, output, Path(settings)) as (origin, session):
            api = Local(origin, session)
            report["packaged_version"] = session.get(origin + "/api/bootstrap", timeout=10).json()["version"]
            report["executable"] = str(args.exe) if args.exe else None
            area = api.post("aoi", {"coordinates": BBOX})
            boundary = output / "联合研究区.shp"
            if not boundary.exists():
                api.post("export-aoi", {"geometry": area["geometry"], "path": str(boundary)})
            imported = api.post("aoi", {"path": str(boundary)})
            assert shape(imported["geometry"]).equals(shape(area["geometry"]))
            search = wait_job(api, "batch-search", {"profile": "s2-earth-search", "geometry": imported["geometry"], **DATES,
                          "max_cloud": 100, "shared_dates": DATES, "options": OPTICAL, "companions": [
                              {"id": "gedi-earthdata", "profile": "gedi-earthdata", "criteria": {"collection": GEDI_KEY}, "options": {"mode": "original"}},
                              {"id": "dem-30", "profile": "dem-30", "criteria": {"start": None, "end": None}, "options": {"mode": "dem"}}]})
            groups = search["batch"]["groups"]
            assert all(not group["error"] and group["scenes"] for group in groups), [(g["profile"], g["error"]) for g in groups]
            assert all(group["criteria"][key] == value for group in groups[:2] for key, value in DATES.items())
            assert groups[2]["criteria"] == {"start": None, "end": None}
            main_scene = next(scene for scene in groups[0]["scenes"] if scene["title"] == B04_SAFE)
            gedi_scene = min((s for s in groups[1]["scenes"] if s["gedi"]["available"]), key=lambda s: s["gedi"]["estimated_bytes"] or float("inf"))
            req = {"profile": "s2-earth-search", "geometry": imported["geometry"], "query_id": search["query"]["id"],
                   "batch_id": search["batch"]["id"], "criteria": {**DATES, "max_cloud": 100}, "shared_dates": DATES,
                   "scene_keys": [main_scene["key"]], "options": OPTICAL, "output_dir": str(output / "preflight-only"), "companions": [
                       {"id": "gedi-earthdata", "profile": "gedi-earthdata", "criteria": groups[1]["criteria"],
                        "scene_keys": [gedi_scene["key"]], "options": {"mode": "original"}},
                       {"id": "dem-30", "profile": "dem-30", "criteria": groups[2]["criteria"],
                        "scene_keys": [groups[2]["scenes"][0]["key"]], "options": {"mode": "dem"}}]}
            plan = api.post("batch-plan", req)
            assert plan["requires"] == ["earthdata"]
            rejected = api.post("batch-download", {**req, "plan_fingerprint": plan["fingerprint"]}, 400)
            assert "Earthdata" in rejected["error"] and not Path(req["output_dir"]).exists()
            report.update(shared_catalogue_verified=True, atomic_auth_preflight_verified=True,
                          matches={g["profile"]: len(g["scenes"]) for g in groups}, selected_gedi=gedi_scene["gedi"])
            save()
            public = copy.deepcopy(req)
            public["output_dir"] = str(output / "data")
            public["companions"][0]["scene_keys"] = []
            plan = api.post("batch-plan", public)
            assert plan["requires"] == []
            result = wait_job(api, "batch-download", {**public, "plan_fingerprint": plan["fingerprint"]})
            assert [s["status"] for s in result["sources"]] == ["completed", "not_selected", "completed"]
            report["public_downloads"] = [audit_source(record) for record in (result["sources"][0], result["sources"][2])]
            report["combined_manifest"] = result["manifest"]
            with shapefile.Reader(str(Path(result["folder"]) / "研究区.shp")) as reader:
                assert shape(reader.shape(0).__geo_interface__).equals(shape(imported["geometry"]))
            browser_check(origin, output)
            report["packaged_browser"] = "ok"
            save()
            print(json.dumps({"version": report["packaged_version"], "sources": report["matches"],
                              "public_transfers": len(report["public_downloads"]), "report": str(report_path)}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
