"""Installed Sentinel-5P live test: exact original bytes, independent CDSE digest and arrays."""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import time
from pathlib import Path

import h5py
import numpy as np
import requests

from live_public import BBOX, local_app

PROFILE = "s5p-aws-l2"
STAC = "https://stac.dataspace.copernicus.eu/v1/collections"
ARCHIVE = "https://meeo-s5p.s3.eu-central-1.amazonaws.com/"
CASES = [
    ("ch4-offl", "2024-06-10", "methane_mixing_ratio", "S5P_OFFL_L2__CH4____20240610T041611_20240610T055741_34501_03_020600_20240614T023809"),
    ("no2-nrti", "2026-11-19", "nitrogendioxide_tropospheric_column", "S5P_NRTI_L2__NO2____20261119T045934_20261119T050434_47156_03_030000_20261119T054643"),
    ("ch4-rpro", "2019-10-01", "methane_mixing_ratio", "S5P_RPRO_L2__CH4____20191001T030531_20191001T044700_10183_03_020400_20221115T100825"),
]
EXTRA_NRTI = [
    ("ch4-nrti", "2026-11-15", "methane_mixing_ratio", "S5P_NRTI_L2__CH4____20261115T061434_20261115T061934_47100_03_030000_20261115T070811"),
    ("hcho-nrti", "2026-11-15", "formaldehyde_tropospheric_vertical_column", "S5P_NRTI_L2__HCHO___20261115T061434_20261115T061934_47100_03_030001_20261115T070812"),
    ("so2-nrti", "2026-11-19", "sulfurdioxide_total_vertical_column", "S5P_NRTI_L2__SO2____20261119T045934_20261119T050434_47156_03_030001_20261119T054906"),
    ("o3-nrti", "2026-11-19", "ozone_total_vertical_column", "S5P_NRTI_L2__O3_____20261119T045934_20261119T050434_47156_03_030001_20261119T054641"),
    ("co-nrti", "2026-11-19", "carbonmonoxide_total_column", "S5P_NRTI_L2__CO_____20261119T045934_20261119T050434_47156_03_030000_20261119T052838"),
    ("cloud-nrti", "2026-11-19", "cloud_fraction", "S5P_NRTI_L2__CLOUD__20261119T045934_20261119T050434_47156_03_030001_20261119T052839"),
    ("aer-lh-nrti", "2026-11-19", "aerosol_mid_height", "S5P_NRTI_L2__AER_LH_20261119T045934_20261119T050434_47156_03_030000_20261119T054643"),
    ("aer-ai-nrti", "2026-11-19", "aerosol_index_354_388", "S5P_NRTI_L2__AER_AI_20261119T045934_20261119T050434_47156_03_030000_20261119T052841"),
]
GAP = "S5P_NRTI_L2__CH4____20261119T045934_20261119T050434_47156_03_030000_20261119T054641"


from browser_flow import open_data_page

def independent_catalogue(collection, identifier):
    url = f"{STAC}/{collection}/items/{identifier}"
    for attempt in range(4):
        with requests.get(url, timeout=(10, 38), allow_redirects=False) as result:
            if result.status_code == 200:
                return result.json()
            if result.status_code not in (429, 500, 502, 503, 504):
                raise RuntimeError(f"Independent CDSE item returned HTTP {result.status_code}")
        time.sleep(2 ** (attempt + 1))
    raise RuntimeError("Independent CDSE item remained unavailable after retries")


def independently_check(path, properties, variable, identifier):
    with h5py.File(path, "r") as f:
        root_id = f.attrs["id"]
        root_id = root_id.decode("ascii") if isinstance(root_id, bytes) else root_id
        assert root_id.removesuffix(".nc") == identifier
        orbit, = np.asarray(f.attrs["orbit"]).reshape(-1)
        assert int(orbit) == properties["sat:absolute_orbit"]
        names = ["latitude", "longitude", "qa_value", variable]
        arrays = [f["PRODUCT"][name] for name in names]
        lat, lon, _, _ = [field[()] for field in arrays]
        assert [1, *properties["proj:shape"]] == list(lat.shape) and all(d.shape == lat.shape for d in arrays)
        geolocated = (abs(lat) <= 90.0001) & (abs(lon) <= 180.0001)
        assert np.any(geolocated)
        primary = arrays[-1][()]
        fill = arrays[-1].attrs.get("_FillValue")
        n = np.count_nonzero(geolocated & np.isfinite(primary) & (primary != fill)) if fill is not None else np.count_nonzero(geolocated & np.isfinite(primary))
        return {"orbit": int(orbit), "shape": list(lat.shape), "variables": names, "science_non_fill_geolocated": int(n)}


def verify_ui(origin, output):
    from playwright.sync_api import expect, sync_playwright

    key = CASES[0][3]
    with sync_playwright() as driver:
        browser = driver.chromium.launch(channel=os.environ.get("STARMAP_TEST_BROWSER", "msedge"), headless=True)
        page = browser.new_page(viewport={"width": 1360, "height": 900})
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.route("https://**/*", lambda route: route.abort())
        open_data_page(page, origin)
        expect(page.locator("#outputDirectory")).not_to_have_value("")
        page.locator("#sourceProfile").select_option(PROFILE)
        expect(page.locator("#s5pCollection option")).to_have_count(27)
        expect(page.locator("#publicAuth")).to_be_visible()
        expect(page.locator("#cloudOptions")).to_be_hidden()
        expect(page.locator("#rawOptions")).to_be_hidden()
        page.locator("#s5pCollection").select_option("sentinel-5p-l2-ch4-offl")
        page.locator('[data-tab="coordinates"]').click()
        page.locator("#coordinates").fill(BBOX)
        page.locator("#applyCoordinates").click()
        expect(page.locator("#aoiName")).to_have_text("坐标研究区")
        for field in ("startDate", "endDate"):
            page.locator("#" + field).fill(CASES[0][1])
        page.locator("#searchScenes").click()
        scene = page.locator(f'#scenesBody tr[data-key="{key}"]')
        expect(scene).to_be_visible(timeout=90000)
        scene.locator('input[type="checkbox"]').check()
        page.locator("#outputDirectory").fill(str(output))
        expect(page.locator("#downloadSelected")).to_be_enabled(timeout=15000)
        page.locator("#downloadSelected").click()
        expect(page.locator("#planNote")).to_contain_text("不裁剪")
        page.locator("#confirmDownload").click()
        expect(page.locator("#jobStatus")).to_have_text("下载完成", timeout=20000)
        expect(page.locator("#jobFiles")).to_contain_text("已有")
        page.locator("#closeJobPanel").click()
        page.locator("#s5pCollection").select_option("sentinel-5p-l2-no2-nrti")
        expect(page.locator("#scenesBody tr")).to_have_count(0)
        assert not errors
        browser.close()
    return {"options": 27, "real_search_and_verified_reuse": True, "cleared_after_category_change": True}


def run(exe, output, all_nrti):
    output.mkdir(parents=True, exist_ok=True)
    out_file = output / "s5p-live-report.json"
    report = {"started_utc": dt.datetime.now(dt.timezone.utc).isoformat(), "bbox_wgs84": BBOX, "status": "running", "cases": []}

    def save():
        out_file.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    with local_app(exe, output) as (origin, session):
        def post(route, value):
            answer = session.post(f"{origin}/api/{route}", json=value, timeout=12)
            if not answer.ok:
                raise RuntimeError(f"Local {route}: {answer.status_code} {answer.json().get('error')}")
            return answer.json()

        def job(route, value):
            key = post(route, value)["job_id"]
            deadline = time.monotonic() + (500 if route == "download" else 150)
            while time.monotonic() < deadline:
                data = session.get(f"{origin}/api/job", params={"id": key}, timeout=8).json()
                if data["status"] == "completed":
                    return data["result"]
                if data["status"] not in ("running", "cancelling"):
                    raise RuntimeError(f"Local {route} failed: {data.get('error') or data.get('message')}")
                time.sleep(0.5)
            raise RuntimeError(f"Local {route} did not finish")

        boot = session.get(f"{origin}/api/bootstrap", timeout=8).json()
        assert len(boot["s5p_collections"]) == 27 and not any(boot[k] for k in ("connected", "raw_connected", "s3_connected"))
        report["version"] = boot["version"]
        save()
        geometry = post("aoi", {"coordinates": BBOX})["geometry"]
        common = {"geometry": geometry, "profile": PROFILE}
        for suffix, day, field, identifier in CASES + (EXTRA_NRTI if all_nrti else []):
            collection = "sentinel-5p-l2-" + suffix
            criteria = {"collection": collection, "start": day, "end": day}
            print(f"Live S5P {suffix} {day}: {identifier}", flush=True)
            search = job("search", {**common, **criteria})
            scene, = [r for r in search["scenes"] if r["key"] == identifier]
            assert scene["raw"]["available"], scene["raw"].get("unavailable_reason")
            request = {**common, "criteria": criteria, "query_id": search["query"]["id"], "scene_keys": [identifier],
                       "output_dir": str(output), "options": {"mode": "original", "transport": "public"}}
            plan = post("plan", request)
            assert not plan["requires"] and plan["transports"] == ["public"]
            item, = plan["targets"]
            cdse = independent_catalogue(collection, identifier)
            assert cdse["id"] == identifier and cdse["collection"] == collection
            asset, = [a for a in cdse["assets"].values() if a.get("href", "").endswith("/" + identifier + ".nc") and "data" in a.get("roles", [])]
            assert asset["href"].startswith("s3://eodata/Sentinel-5P/TROPOMI/")
            key = asset["href"].removeprefix("s3://eodata/Sentinel-5P/TROPOMI/")
            url = ARCHIVE + suffix.split("-")[-1].upper() + "/" + key
            assert url == item["url"] and "?" not in url
            assert re.fullmatch(r"d50110[0-9a-f]{32}", asset["file:checksum"]), "Sample does not have a CDSE source MD5"
            upstream = asset["file:checksum"][6:]
            with requests.head(url, timeout=(10, 35), allow_redirects=False) as head:
                assert head.status_code == 200, f"Independent AWS HEAD returned HTTP {head.status_code}"
                size, tag = int(head.headers["Content-Length"]), head.headers["ETag"]
            assert size == item["bytes"] == asset["file:size"] and 0 < size < 160 * 1024**2
            assert tag == item["identity"]["archive_etag"]
            result = job("download", request)
            record, = json.loads(Path(result["manifest"]).read_text(encoding="utf-8"))["items"]
            file, = result["files"]
            assert file["status"] == "completed", "Use a fresh output directory for a real byte transfer"
            path = Path(file["path"])
            assert record["format_validation"] == "NetCDF swath identity and full core-array decode"
            assert record["url"] == url and record["etag"] == tag and record["product"] == identifier
            assert record["size"] == path.stat().st_size == size
            with path.open("rb") as handle:
                assert hashlib.file_digest(handle, lambda: hashlib.md5(usedforsecurity=False)).hexdigest() == upstream
            with path.open("rb") as handle:
                assert hashlib.file_digest(handle, "sha256").hexdigest() == record["sha256"]
            arrays = independently_check(path, cdse["properties"], field, identifier)
            proof = record["s5p_validation"]
            assert proof["decoded_variables"] == arrays["variables"]
            assert all(proof[k] == arrays[k] for k in ("orbit", "shape", "science_non_fill_geolocated"))
            again = job("download", request)
            assert again["files"][0]["status"] == "skipped"
            report["cases"].append({"collection": collection, "day_utc": day, "product": identifier, "path": str(path), "url": url,
                                     "bytes": size, "cdse_stac_md5": upstream, "sha256": record["sha256"], "aws_object_etag": tag,
                                     "receipt": result["manifest"], "repeat_verified": True, **arrays})
            save()
            print(f"Verified {size:,} bytes, CDSE MD5, orbit {arrays['orbit']} and {field}", flush=True)
        if all_nrti:
            criteria = {"collection": "sentinel-5p-l2-ch4-nrti", "start": "2026-11-19", "end": "2026-11-19"}
            rows = job("search", {**common, **criteria})["scenes"]
            row, = [r for r in rows if r["key"] == GAP]
            gap_url = ARCHIVE + "NRTI/L2__CH4___/2026/11/19/" + GAP + ".nc"
            with requests.head(gap_url, timeout=(10, 30), allow_redirects=False) as head:
                expected = head.status_code
            if expected in (403, 404):
                assert not row["raw"]["available"] and not row["raw"]["assets"]
            elif expected == 200:
                assert row["raw"]["available"], "Previously missing sample now exists but is not selectable"
            else:
                raise RuntimeError(f"Independent availability probe returned unexpected HTTP {expected}")
            report["availability_case"] = {"id": GAP, "independent_head": expected, "selectable": row["raw"]["available"],
                                           "reason": row["raw"].get("unavailable_reason")}
        if exe:
            report["compiled_browser"] = verify_ui(origin, output)
        report["status"] = "completed"
        save()
    print(f"S5P acceptance report: {out_file}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--exe", type=Path, help="Installed StarmapDownloader.exe; omit to run from source")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--all-nrti", action="store_true", help="All nine varieties via real NRTI samples plus the other two streams")
    args = parser.parse_args()
    run(args.exe.resolve(strict=True) if args.exe else None, args.output.resolve(), args.all_nrti)


if __name__ == "__main__":
    main()
