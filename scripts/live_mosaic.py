"""Verify saved-account mosaics and native S3 assets against the ten official SAFE ZIPs."""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import re
import shutil
import time
import zipfile
from pathlib import Path

from shapely.geometry import box, mapping, shape
from shapely.ops import unary_union

from check_mosaic_reference import NativeSources, compare_mosaic, reference_scene
from check_safe_reference import read_reference
from live_public import local_app
from live_s3 import CACHE_EVIDENCE, decode_original, sha256
from core.planning import normalize_options
from core.native_mosaic import native_source_plan
from core.scene_identity import utc_time
from core.spatial_selection import coverage_summary, polygon_area

SEAM = box(117.060, 39.30, 117.075, 39.32)


def map_acceptance(origin, profile, scenes, geometry, output):
    """Use actual Edge mouse clicks and the served UI, without injecting a selection."""
    from playwright.sync_api import expect, sync_playwright
    from browser_flow import open_data_page

    failures = []
    with sync_playwright() as driver:
        browser = driver.chromium.launch(channel="msedge", headless=True)
        try:
            page = browser.new_page(viewport={"width": 1440, "height": 940})
            page.route(re.compile(r"^https://"), lambda route: route.abort())
            page.on("pageerror", lambda error: failures.append(str(error)))
            open_data_page(page, origin)
            expect(page.locator("#sourceProfile")).to_have_value(profile)
            page.wait_for_function("()=>Spatial.layers.size === state.scenes.length && state.scenes.length > 0")
            assert page.evaluate("[...Spatial.layers.values()].every(g=>g.getLayers().every(l=>l.getLayers().every(p=>p.options.dashArray==='6,5')))")
            if page.locator("#selectNone").is_enabled():
                page.locator("#selectNone").click()
            page.locator("#mapFootprintScope").select_option("candidates")
            page.locator("#mapFitAoi").click()
            overlap = geometry.intersection(shape(scenes[0]["products"][0]["footprint"])).intersection(
                shape(scenes[1]["products"][0]["footprint"]))
            assert overlap.area > 0
            point = overlap.representative_point()
            pixel = page.evaluate("([lon,lat])=>{const p=map.latLngToContainerPoint([lat,lon]);return {x:p.x,y:p.y}}",
                                  [point.x, point.y])
            frame = page.locator("#map").bounding_box()
            page.mouse.click(frame["x"] + pixel["x"], frame["y"] + pixel["y"])
            expect(page.locator(".footprint-popup")).to_be_visible()
            for scene in scenes:
                checkbox = page.locator(f'.footprint-popup input[data-map-key="{scene["key"]}"]')
                expect(checkbox).to_be_enabled()
                checkbox.check()
                assert page.locator(f'#scenesBody tr[data-key="{scene["key"]}"] input').is_checked()
            keys = page.evaluate("[...state.selected]")
            assert set(keys) == {scene["key"] for scene in scenes}
            expect(page.locator("#coverageDates")).to_contain_text("2 个日期")
            page.locator(".leaflet-popup-close-button").click()
            page.locator("#coverageDetails summary").click()
            page.locator("#coverageBlockList [data-move='1']").first.click()
            expect(page.locator("#spatialPriority")).to_have_value("selection")
            page.locator("#spatialPriority").select_option("cloud")
            expect(page.locator("#spatialPriority")).to_have_value("cloud")
            page.locator("#coverageDetails summary").click()
            expect(page.locator("#downloadSelected")).to_be_enabled()
            page.locator("#mapFootprintScope").select_option("selected")
            page.wait_for_function("()=>Spatial.summary?.selected_count===2 && footprintLayer.getLayers().length===2")
            before = page.evaluate("[...state.selected]")
            page.locator("#mapFootprintDate").select_option(scenes[0]["date"])
            assert page.evaluate("footprintLayer.getLayers().length") == 1
            assert page.evaluate("[...state.selected]") == before
            page.locator("#mapFootprintDate").select_option("all")
            page.locator("#fitFootprints").click()
            page.wait_for_function("()=>!Spatial.zooming && !Spatial.fitPending && map.getBounds().contains(footprintLayer.getBounds())")
            page.screenshot(path=str(output / (profile + "-map-selection.png")), full_page=True)
            assert not failures, failures
            return {"status": "passed", "profile": profile, "scene_keys": keys,
                    "persistent_dashed_footprints": True, "actual_overlap_map_click": True,
                    "map_list_synchronized": True, "two_acquisition_dates": True,
                    "manual_priority_adjustment": True, "download_button_enabled": True,
                    "selected_full_footprint_overview": True, "date_filter_preserves_selection": True}
        finally:
            browser.close()


def overview_acceptance(origin, profile, scenes, summary, output):
    """Validate the real default recommendation and large grouped extents, with no injected selection."""
    from playwright.sync_api import expect, sync_playwright
    from browser_flow import open_data_page

    failures = []
    with sync_playwright() as driver:
        browser = driver.chromium.launch(channel="msedge", headless=True)
        try:
            page = browser.new_page(viewport={"width": 1440, "height": 940})
            page.route(re.compile(r"^https://"), lambda route: route.abort())
            page.on("pageerror", lambda error: failures.append(str(error)))
            open_data_page(page, origin)
            expect(page.locator("#sourceProfile")).to_have_value(profile)
            expected = set(summary["recommended_keys"])
            page.wait_for_function("n=>Spatial.summary?.selected_count===n", arg=len(expected))
            page.wait_for_function("()=>Spatial.summary?.footprint_groups && !Spatial.zooming && !Spatial.fitPending && map.getBounds().contains(footprintLayer.getBounds())")
            chosen = set(page.evaluate("[...state.selected]"))
            assert chosen == expected
            expect(page.locator("#mapFootprintScope")).to_have_value("selected")
            expect(page.locator("#mapFootprintDetail")).to_have_value("overpass")
            groups = page.evaluate("footprintLayer.getLayers().length")
            expected_groups = sum(bool(g["selected_keys"]) for g in page.evaluate("Spatial.summary.footprint_groups"))
            assert groups == expected_groups and groups <= len(chosen)
            selected_coverage = page.evaluate("Spatial.summary.coverage_percent")
            all_available = unary_union([shape(b["intersection"]) for b in summary["blocks"] if b["intersection"]])
            selected_union = unary_union([shape(b["intersection"]) for b in summary["blocks"]
                                         if b["key"] in chosen and b["intersection"]])
            lost_km2 = polygon_area(all_available.difference(selected_union))
            assert lost_km2 < 1e-6
            page.screenshot(path=str(output / (profile + "-overview.png")), full_page=True)
            old_height = page.locator("#map").bounding_box()["height"]
            page.locator("#mapExpand").click()
            expect(page.locator("#mapExpand")).to_have_attribute("aria-pressed", "true")
            assert page.locator("#map").bounding_box()["height"] > old_height + 100
            assert set(page.evaluate("[...state.selected]")) == chosen
            assert page.evaluate("map.getBounds().contains(footprintLayer.getBounds())")
            page.screenshot(path=str(output / (profile + "-overview-expanded.png")), full_page=True)
            page.locator("#mapExpand").click()
            expect(page.locator("#mapExpand")).to_have_attribute("aria-pressed", "false")
            page.locator("#mapFootprintDetail").select_option("products")
            assert page.evaluate("footprintLayer.getLayers().length") == len(chosen)
            date = next(s["date"] for s in scenes if s["key"] in chosen)
            page.locator("#mapFootprintDate").select_option(date)
            assert page.evaluate("footprintLayer.getLayers().length") == sum(s["key"] in chosen and s["date"] == date for s in scenes)
            assert set(page.evaluate("[...state.selected]")) == chosen
            assert page.evaluate("Spatial.summary.coverage_percent") == selected_coverage
            page.locator("#mapFootprintScope").select_option("candidates")
            assert page.evaluate("footprintLayer.getLayers().length") == len(scenes)
            assert set(page.evaluate("[...state.selected]")) == chosen
            expect(page.locator(".leaflet-control-scale")).to_be_visible()
            assert not failures, failures
            return {"status": "passed", "profile": profile, "candidate_count": len(scenes),
                    "default_selected_count": len(chosen), "default_overpass_groups": groups,
                    "coverage_percent": selected_coverage, "recommendation_lost_km2": lost_km2,
                    "selected_keys": sorted(chosen), "full_extent_fitted": True,
                    "product_expansion_and_date_filter_preserve_selection": True,
                    "all_candidates_accessible": True, "scale_bar_visible": True,
                    "expanded_map_preserves_selection_and_full_extent": True}
        finally:
            browser.close()


def boundary_acceptance(origin, output):
    from playwright.sync_api import expect, sync_playwright
    from browser_flow import open_data_page
    from core.geometry import save_aoi

    region = box(117.061, 39.301, 117.067, 39.307).difference(box(117.063, 39.303, 117.064, 39.304)).union(
        box(117.070, 39.301, 117.073, 39.304))
    path = save_aoi(region, str(output / "多地块删除验收.shp"))
    failures = []
    with sync_playwright() as driver:
        browser = driver.chromium.launch(channel="msedge", headless=True)
        try:
            page = browser.new_page(viewport={"width": 1440, "height": 940})
            page.route(re.compile(r"^https://"), lambda route: route.abort())
            page.on("pageerror", lambda error: failures.append(str(error)))
            open_data_page(page, origin)
            page.locator('[data-tab="import"]').click()
            page.locator("#importBoundary").click()
            page.locator("#pathDialogInput").fill(path)
            page.locator("#confirmPath").click()
            expect(page.locator("#aoiName")).to_have_text("多地块删除验收")
            assert page.evaluate("aoiLayer.getLayers().length") == 2
            page.locator("#editBoundary").click()
            assert page.evaluate("state.editing")
            page.locator("#mapDeleteAoi").click()
            expect(page.locator("#aoiName")).to_have_text("尚未设置研究区")
            assert page.evaluate("aoiLayer.getLayers().length===0 && footprintLayer.getLayers().length===0 && !state.editing && editHandler===null && state.plan===null && state.query===null")
            expect(page.locator("#coveragePanel")).to_be_hidden()
            expect(page.locator("#searchScenes")).to_be_disabled()
            page.locator("#mapDrawPolygon").click()
            page.locator("#mapDeleteAoi").click()
            expect(page.locator("#drawHint")).to_be_hidden()
            assert page.evaluate("drawHandler===null && state.aoi===null")
            assert not failures, failures
            return {"status": "passed", "imported_multipart_shp_with_hole": True,
                    "delete_during_edit": True, "cancel_unfinished_drawing": True,
                    "map_selection_and_plan_cleared": True}
        finally:
            browser.close()


def check(directory, output, executable=None, *, route="both", native_assets=(), browser=False,
          download_timeout=3600, native_cache=None):
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    products = [read_reference(path) for path in sorted(directory.glob("*.SAFE.zip"))]
    assert len(products) == 10, "This acceptance case requires the user's ten exact SAFE ZIPs"
    by_id = {product["product_id"]: product for product in products}
    reference_rows = [reference_scene(product) for product in products]
    union = unary_union([shape(product["footprint"]) for product in products])
    report_path = output / "live-mosaic-report.json"
    report = {"status": "running", "verified_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
              "executable": str(executable) if executable else "source", "reference_directory": str(directory),
              "aoi_basis": "Constructed from ten official footprints; no user SHP was specified",
              "reference_coverage": coverage_summary(union, reference_rows, list(by_id), "cloud"),
              "catalogues": [], "mosaics": [], "native_s3": [], "native_source_members": [], "browser_checks": []}

    def save():
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")

    opts = normalize_options({"mode": "research", "layout": "mosaic", "priority": "cloud",
                              "mask_clouds": True, "max_cloud": 100})
    native = NativeSources(products, opts["bands"])
    save()
    try:
        with local_app(executable, output) as (origin, api):
            def post(action, value):
                with api.post(origin + "/api/" + action, json=value, timeout=(5, 240)) as response:
                    if response.status_code != 200:
                        raise RuntimeError(response.json().get("error") or f"{action}: HTTP {response.status_code}")
                    return response.json()

            def run(action, value, timeout=1800):
                job = post(action, value)["job_id"]
                deadline, last = time.monotonic() + timeout, 0
                while time.monotonic() < deadline:
                    with api.get(origin + "/api/job", params={"id": job}, timeout=5) as response:
                        response.raise_for_status()
                        state = response.json()
                    if state["status"] == "completed":
                        return state["result"]
                    if state["status"] not in ("running", "cancelling"):
                        report["failed_job"] = {"operation": action, **{key: state.get(key) for key in
                                               ("status", "message", "error", "io_diagnostic")}}
                        save()
                        raise RuntimeError(state.get("error") or state.get("message") or state["status"])
                    if time.monotonic() - last >= 15:
                        print(f"{action}: {state.get('message', 'running')}", flush=True)
                        last = time.monotonic()
                    time.sleep(.5)
                post("cancel", {"job_id": job})
                raise RuntimeError(f"{action} timed out; cancellation requested")

            for _ in range(180):
                with api.get(origin + "/api/bootstrap", timeout=5) as response:
                    boot = response.json()
                if not boot["accounts"]["restoring"]:
                    break
                time.sleep(.5)
            else:
                raise RuntimeError("Saved-account restoration did not finish")
            original_output = boot["preferences"]["output_dir"]
            report.update(version=boot["version"], saved_oauth_connected=boot["connected"],
                          saved_s3_connected=boot["s3_connected"])
            save()
            try:
                profiles = (["s2-cdse-s3", "s2-earth-search"] if route == "both" else
                            [{"process": "s2-process", "native": "s2-cdse-s3", "public": "s2-earth-search"}[route]])
                for profile in profiles:
                    if profile == "s2-process":
                        assert boot["connected"], "Saved Sentinel Hub OAuth account is not connected"
                    geometry = post("aoi", {"geometry": mapping(union), "name": "十个官方包覆盖范围核验"})["geometry"]
                    criteria = {"start": "2026-04-08", "end": "2026-04-20", "max_cloud": 100}
                    found = run("search", {"profile": profile, "geometry": geometry, **criteria}, 600)
                    exact = {scene["title"]: scene for scene in found["scenes"] if scene["title"] in by_id}
                    assert set(exact) == set(by_id), f"{profile}: exact official product versions missing from current catalogue"
                    catalogue = {"profile": profile, "total_candidates": len(found["scenes"]), "exact_products": []}
                    for product_id, scene in exact.items():
                        reference = by_id[product_id]
                        region = shape(scene["products"][0]["footprint"])
                        official = shape(reference["footprint"])
                        assert scene["tiles"] == [reference["tile"]]
                        if scene.get("time_kind") == "product_start":
                            assert scene["date"] == reference["acquisition_utc"][:10]
                        else:
                            assert int(utc_time(scene["datetime"]).timestamp()) == int(utc_time(reference["acquisition_utc"]).timestamp())
                        catalogue["exact_products"].append({"product_id": product_id, "catalogue_key": scene["key"],
                            "acquisition_utc": scene["datetime"], "cloud": scene["cloud_max"],
                            "footprint_symmetric_difference_km2": polygon_area(region.symmetric_difference(official)),
                            "footprint_hausdorff_degrees": region.hausdorff_distance(official),
                            "exact_version_matched": True, "catalogue_footprint": mapping(region)})
                    selected_keys = [exact[product_id]["key"] for product_id in by_id]
                    summary = post("coverage", {"profile": profile, "geometry": geometry, "criteria": criteria,
                        "query_id": found["query"]["id"], "scene_keys": selected_keys, "options": opts})
                    catalogue["selected_coverage"] = summary
                    report["catalogues"].append(catalogue)
                    save()
                    print(f"Exact current catalogue identities checked: {profile}, 10 products", flush=True)
                    if browser:
                        report["browser_checks"].append(overview_acceptance(origin, profile, found["scenes"], summary, output))
                        save()

                    geometry = post("aoi", {"geometry": mapping(SEAM), "name": "两日期接缝核验区"})["geometry"]
                    found = run("search", {"profile": profile, "geometry": geometry, **criteria}, 600)
                    pair_ids = [p["product_id"] for p in products if p["tile"] in ("T50SMJ", "T50SNJ")
                                and not p["acquisition_utc"].startswith("2026-04-18")]
                    selected = [scene for product_id in pair_ids for scene in found["scenes"] if scene["title"] == product_id]
                    assert len(selected) == 2
                    if browser:
                        report["browser_checks"].append(map_acceptance(origin, profile, selected, SEAM, output))
                        save()
                    request = {"profile": profile, "geometry": geometry, "criteria": criteria,
                               "query_id": found["query"]["id"], "scene_keys": [s["key"] for s in selected],
                               "options": opts, "output_dir": str(output / profile)}
                    plan = post("plan", request)
                    assert plan["kind"] == "mosaic" and plan["scene_count"] == 1 and plan["source_count"] == 2
                    request["plan_fingerprint"] = plan["fingerprint"]
                    if profile == "s2-cdse-s3" and native_cache:
                        destination = output / profile / plan["folder_name"] / "官方原件缓存"
                        assert not destination.exists(), "Cache seeding requires a fresh acceptance directory"
                        shutil.copytree(native_cache, destination)
                        report["native_cache_seeded_from"] = str(native_cache)
                        save()
                    result = run("download", request, download_timeout)
                    assert len(result["files"]) == 1
                    proof = compare_mosaic(result["files"][0], SEAM, selected, opts, native)
                    assert len(proof["used_products"]) == 2 and len(result["used_dates"]) == 2
                    if profile == "s2-cdse-s3":
                        table = json.loads(Path(result["files"][0]["bundle"]["sources"]).read_text(encoding="utf-8"))
                        assert len(table["native_sources"]) == 2
                        for key, evidence in table["native_sources"].items():
                            product = by_id[key]
                            scene, = [scene for scene in selected if scene["key"] == key]
                            targets = {target["asset"]: target for target in native_source_plan(scene, opts)["targets"]}
                            rows = json.loads(Path(evidence["manifest"]).read_text(encoding="utf-8"))["items"]
                            assert len(rows) == 14 and len(evidence["manifest_checked_members"]) == 13
                            with zipfile.ZipFile(product["zip"]) as archive:
                                for row in rows:
                                    path = Path(row["path"])
                                    name = targets[row["asset"]]["name"]
                                    member, = [member for member in archive.namelist() if member.endswith("/" + name)]
                                    with archive.open(member) as stream:
                                        reference_sha = hashlib.file_digest(stream, "sha256").hexdigest()
                                    digest = sha256(path)
                                    assert digest == reference_sha == row["sha256"]
                                    assert path.stat().st_size == archive.getinfo(member).file_size == row["size"]
                                    report["native_source_members"].append({"product_id": product["product_id"],
                                        "name": name, "status": row["status"], "bytes": path.stat().st_size,
                                        "sha256": digest, "byte_identical_to_official_safe": True,
                                        "zip_member_crc_verified": True, "decode": decode_original(path)})
                                    save()
                            print(f"All 14 source objects are byte-identical to the official SAFE: {key}", flush=True)
                    repeated = run("download", request, download_timeout)
                    assert repeated["files"][0]["status"] == "skipped" and repeated["used_dates"] == result["used_dates"]
                    report["mosaics"].append({"profile": profile, "geometry": geometry, "manifest": result["manifest"],
                        "file": result["files"][0], "cached_complete": True, "used_dates": result["used_dates"], **proof})
                    save()
                    print(f"Actual network mosaic/native pixel comparison passed: {profile}", flush=True)
                    if browser:
                        report["browser_checks"].append(boundary_acceptance(origin, output / profile))
                        assert api.get(origin + "/api/bootstrap", timeout=5).json()["aoi"] is None
                        save()

                if native_assets:
                    assert boot["s3_connected"], "Current saved CDSE S3 account is not connected"
                    product, = [p for p in products if p["tile"] == "T50TMK"]
                    point = shape(product["footprint"]).representative_point()
                    geometry = post("aoi", {"geometry": mapping(box(point.x - .0008, point.y - .0008,
                                                                     point.x + .0008, point.y + .0008))})["geometry"]
                    criteria = {"collection": "sentinel-2-l2a", "start": "2026-04-11", "end": "2026-04-11", "radar_mode": ""}
                    found = run("search", {"profile": "cdse-original", "geometry": geometry, **criteria}, 600)
                    scene, = [s for s in found["scenes"] if s["title"].removesuffix(".SAFE") == product["product_id"].removesuffix(".SAFE")]
                    request = {"profile": "cdse-original", "geometry": geometry, "criteria": criteria,
                        "query_id": found["query"]["id"], "scene_keys": [scene["key"]], "output_dir": str(output / "native-s3"),
                        "options": {"mode": "assets", "assets": list(native_assets), "transport": "cdse-s3"}}
                    plan = post("plan", request)
                    assert plan["requires"] == ["cdse-s3"] and len(plan["targets"]) == len(native_assets)
                    result = run("download", request, 3600)
                    rows = json.loads(Path(result["manifest"]).read_text(encoding="utf-8"))["items"]
                    with zipfile.ZipFile(product["zip"]) as archive:
                        for file, target, row in zip(result["files"], plan["targets"], rows, strict=True):
                            path = Path(file["path"])
                            member, = [name for name in archive.namelist() if name.endswith("/" + target["name"])]
                            with archive.open(member) as stream:
                                reference_sha = hashlib.file_digest(stream, "sha256").hexdigest()
                            digest = sha256(path)
                            receipt = json.loads(path.with_suffix(".starmap.json").read_text(encoding="utf-8"))
                            assert digest == reference_sha == row["sha256"] == receipt["sha256"]
                            assert path.stat().st_size == archive.getinfo(member).file_size == target["bytes"]
                            report["native_s3"].append({"product_id": product["product_id"], "asset": target["asset"],
                                "status": file["status"], "bytes": path.stat().st_size, "sha256": digest,
                                "byte_identical_to_official_safe": True, "zip_member_crc_verified": True,
                                "decode": decode_original(path)})
                            save()
                            print(f"Current S3 key, exact 2026 member and full decode checked: {target['asset']}", flush=True)
                    repeated = run("download", request, 3600)
                    cached = json.loads(Path(repeated["manifest"]).read_text(encoding="utf-8"))["items"]
                    assert all(file["status"] == "skipped" for file in repeated["files"])
                    for row in cached:
                        receipt = json.loads(Path(row["path"]).with_suffix(".starmap.json").read_text(encoding="utf-8"))
                        assert all(key in row and row[key] == receipt.get(key) for key in CACHE_EVIDENCE)
                    report["native_s3_cached_evidence_complete"] = True
                report["status"] = "passed"
            finally:
                post("preferences", {"output_dir": original_output})
                report["output_preference_restored"] = True
                save()
    except Exception as error:
        report.update(status="failed", error=str(error) or type(error).__name__)
        raise
    finally:
        native.close()
        save()
    print(json.dumps({"status": report["status"], "version": report["version"],
                      "mosaics": len(report["mosaics"]), "native_files": len(report["native_s3"]),
                      "report": str(report_path)}, ensure_ascii=False), flush=True)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--exe", type=Path)
    parser.add_argument("--route", choices=("both", "native", "process", "public"), default="both")
    parser.add_argument("--native-assets", help="Optional comma-separated exact S3 asset IDs")
    parser.add_argument("--browser", action="store_true", help="Verify actual Edge map selection and SHP deletion")
    parser.add_argument("--download-timeout", type=int, default=3600, help="Maximum seconds for each download job")
    parser.add_argument("--native-cache", type=Path, help="Seed a fresh native test from previously verified original-object cache")
    args = parser.parse_args()
    assets = tuple(a.strip() for a in args.native_assets.split(",")) if args.native_assets else ()
    if assets and (any(not a for a in assets) or len(assets) != len(set(assets))):
        parser.error("Specify distinct, nonempty native asset IDs")
    if args.download_timeout < 1:
        parser.error("Specify a positive download timeout")
    check(args.directory.resolve(strict=True), args.output,
          args.exe.resolve(strict=True) if args.exe else None, route=args.route, native_assets=assets,
          browser=args.browser, download_timeout=args.download_timeout,
          native_cache=args.native_cache.resolve(strict=True) if args.native_cache else None)


if __name__ == "__main__":
    main()
