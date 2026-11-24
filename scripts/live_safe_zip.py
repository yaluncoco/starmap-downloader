"""Opt-in full SAFE ZIP download, real Edge comparison and offline native JP2 checks."""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import sys
import threading
import time
import zipfile
from pathlib import Path
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import numpy as np
import rasterio
from rasterio.windows import Window
from requests.utils import get_environ_proxies, select_proxy

from core.account_store import AccountStore
from core.errors import UserError
from core.geometry import parse_coordinates
from core.http import HttpClient
from core.monthly_scenes import _LocalSceneReader
from core.native_mosaic import NATIVE_RESOLUTION
from core.planning import normalize_options, Tile
from core.raw_auth import RawAuth
from core.raw_download import download_original
from core.safe_zip import PROFILE, build_zip_plan, prepare_zip_source, search_zip

PRODUCT = "S2A_MSIL2A_20240610T030531_N0510_R075_T50TMK_20240610T081949.SAFE"
GEOMETRY = "116.350,39.900,116.355,39.905"


def write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def sha256(path):
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def browser_download(auth, plan, output, trusted):
    from playwright.sync_api import sync_playwright
    target = plan["targets"][0]
    proxy_url = select_proxy(target["url"], get_environ_proxies(target["url"]))
    options = {"channel": os.environ.get("STARMAP_TEST_BROWSER", "msedge"), "headless": True}
    if proxy_url:
        parsed = urlsplit(proxy_url)
        proxy = {"server": f"{parsed.scheme}://{parsed.hostname}:{parsed.port}",
                 "bypass": "127.0.0.1,localhost"}
        if parsed.username:
            proxy.update(username=unquote(parsed.username), password=unquote(parsed.password or ""))
        options["proxy"] = proxy
    with sync_playwright() as driver:
        browser = driver.chromium.launch(**options)
        page = browser.new_page(accept_downloads=True)
        # Navigate to the actual Browser origin. Its UI scripts are unnecessary for this endpoint comparison.
        page.route("**/*", lambda route: route.abort() if route.request.resource_type in
                   ("script", "stylesheet", "image", "font") else route.continue_())
        page.goto("https://browser.dataspace.copernicus.eu/", wait_until="domcontentloaded", timeout=60000)
        started = time.monotonic()
        with page.expect_download(timeout=1800000) as event:
            transfer = page.evaluate("""async ({url, token, name}) => {
                const started=performance.now();
                const response=await fetch(url,{headers:{Authorization:'Bearer '+token,Accept:'application/octet-stream'}});
                if(!response.ok)throw new Error('Official download HTTP '+response.status);
                const bytes=await response.blob(), seconds=(performance.now()-started)/1000;
                const link=document.createElement('a');link.href=URL.createObjectURL(bytes);link.download=name;
                document.body.appendChild(link);link.click();
                return {status:response.status,bytes:bytes.size,seconds,etag:response.headers.get('ETag')};
            }""", {"url": target["url"], "token": auth.token(), "name": target["name"] + ".zip"})
        download = event.value
        path = output / download.suggested_filename
        path.parent.mkdir(parents=True, exist_ok=True)
        download.save_as(str(path))
        assert path.name == target["name"] + ".zip" and download.failure() is None
        browser.close()
    result = {**transfer, "path": str(path), "wall_seconds_including_browser_save": time.monotonic() - started,
              "sha256": sha256(path), "actual_edge_blob_download": True,
              "official_browser_origin": "https://browser.dataspace.copernicus.eu",
              "same_product_download_endpoint": target["url"], "tls_verification": True,
              "configured_proxy": bool(proxy_url), "mb_per_second": transfer["bytes"] / transfer["seconds"] / 1024**2}
    result["exact_zip_bytes_equal"] = path.stat().st_size == trusted.stat().st_size and result["sha256"] == sha256(trusted)
    if not result["exact_zip_bytes_equal"]:
        # A dynamically packed ZIP may change its container while retaining the complete original SAFE.
        with zipfile.ZipFile(path) as left, zipfile.ZipFile(trusted) as right:
            left_names, right_names = left.namelist(), right.namelist()
            assert sorted(left_names) == sorted(right_names)
            for name in left_names:
                with left.open(name) as a, right.open(name) as b:
                    assert hashlib.file_digest(a, "sha256").digest() == hashlib.file_digest(b, "sha256").digest()
        result["all_safe_member_bytes_equal"] = True
    else:
        result["all_safe_member_bytes_equal"] = True
    return result


def local_jp2_check(scene, downloaded, output):
    options = normalize_options({"mode": "research", "resolution": 10, "max_cloud": 100, "layout": "mosaic"})
    options["profile"] = PROFILE
    original_request = HttpClient.request
    def forbidden(*args, **kwargs):
        raise AssertionError("Offline preparation or pixel checks attempted HTTP")
    HttpClient.request = forbidden
    try:
        started = time.monotonic()
        prepared = prepare_zip_source(scene, downloaded, options, output / ".download-cache" / "native",
            progress=lambda message, *_: print(message, flush=True))
        metadata, actual = prepared["metadata"], prepared["scene"]
        grid = metadata["grids"][10]
        reader = _LocalSceneReader({actual["key"]: prepared}, options, {"epsg": grid["epsg"], "resolution": 10}, None)
        archive = Path(downloaded["files"][0]["path"])
        comparisons = []
        try:
            with zipfile.ZipFile(archive) as bundle:
                names = bundle.namelist()
                for index, (column, row) in enumerate(((100, 100), (5000, 5000), (10000, 10000)), 1):
                    side = 256
                    bounds = (grid["transform"][2] + column * 10, grid["transform"][5] - (row + side) * 10,
                              grid["transform"][2] + (column + side) * 10, grid["transform"][5] - row * 10)
                    tile = Tile(index, row, column, side, side, bounds, True)
                    values, quality, _ = reader.read(actual, tile, np.ones((side, side), dtype=bool))
                    for band_index, band in enumerate(options["bands"] + ["SCL"]):
                        resolution = NATIVE_RESOLUTION[band]
                        name, = [name for name in names if name.endswith(f"_{band}_{resolution}m.jp2")
                                 and f"/IMG_DATA/R{resolution}m/" in name]
                        # Decode independent bytes extracted directly from the ZIP, rather than the processing cache.
                        path = output / ("reference-" + band + ".jp2")
                        if not path.exists():
                            with bundle.open(name) as source, path.open("wb") as target:
                                while block := source.read(1024 * 1024):
                                    target.write(block)
                        with rasterio.open(path) as ds:
                            assert ds.driver == "JP2OpenJPEG"
                            dn = ds.read(1, window=Window(column * 10 // resolution, row * 10 // resolution,
                                                         side * 10 // resolution, side * 10 // resolution))
                        factor = resolution // 10
                        dn = np.repeat(np.repeat(dn, factor, axis=0), factor, axis=1)
                        if band == "SCL":
                            np.testing.assert_array_equal(quality, dn.astype(np.uint8))
                        else:
                            expected = (dn.astype(np.float32) + np.float32(metadata["offsets"][band])) / np.float32(metadata["quantification"])
                            expected[dn == 0] = -9999
                            np.testing.assert_array_equal(values[band_index], expected)
                    comparisons.append({"window": [column, row, side, side], "spectral_bands": len(options["bands"]),
                                        "scl_equal": True, "all_values_exact": True})
        finally:
            reader.close()
        return {"status": "passed", "real_jp2_driver": "JP2OpenJPEG", "http_requests_during_local_check": 0,
                "seconds": time.monotonic() - started, "windows": comparisons,
                "native_boa_offsets": metadata["offsets"], "quantification": metadata["quantification"],
                "actual_tile_acquisition_utc": metadata["acquisition_utc"],
                "selected_members_verified_against_safe_manifest": len(metadata["manifest_checked_members"])}
    finally:
        HttpClient.request = original_request


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--browser-compare", action="store_true")
    parser.add_argument("--local-check", action="store_true")
    args = parser.parse_args()
    output = args.out.resolve()
    output.mkdir(parents=True, exist_ok=True)
    report = {"status": "running", "started_utc": dt.datetime.now(dt.timezone.utc).isoformat(), "profile": PROFILE}
    auth = RawAuth()
    requests = []
    original_request = HttpClient.request
    def audit(client, method, url, **kwargs):
        parsed = urlsplit(url)
        if method == "GET" and parsed.netloc in ("download.dataspace.copernicus.eu", "zipper.dataspace.copernicus.eu"):
            assert parsed.path.endswith("/$value") and "/Nodes(" not in parsed.path
        requests.append({"method": method, "host": parsed.netloc, "path": parsed.path})
        return original_request(client, method, url, **kwargs)
    HttpClient.request = audit
    try:
        store = AccountStore(Path(os.environ["LOCALAPPDATA"]) / "CopernicusDownloader")
        saved = store.get("odata")
        if not saved or store.load_error:
            raise UserError("本机没有可读取的 CDSE 账号。")
        if saved.get("requires_totp"):
            auth.restore_refresh(saved["refresh_token"])
            auth.on_refresh = store.remember_refresh
        else:
            auth.connect(saved)
        saved.clear()
        geometry = parse_coordinates(GEOMETRY)
        selected = [row for row in search_zip(geometry, "2024-06-10", "2024-06-10", 100) if row["key"] == PRODUCT]
        assert len(selected) == 1, "Fixed official product was unavailable; do not substitute"
        scene = selected[0]
        plan = build_zip_plan(geometry, selected)
        report.update(product=PRODUCT, uuid=scene["raw"]["uuid"], plan=plan)
        write(output / "report.json", report)
        started = time.monotonic()
        transfer = {"first_byte_seconds": None, "last_byte_seconds": None, "last_received_bytes": 0}
        last_print = [0.]
        def progress(message, fraction=0, detail=None):
            detail = detail or {}
            if detail.get("transfer_phase") == "download":
                elapsed = time.monotonic() - started
                if transfer["first_byte_seconds"] is None:
                    transfer["first_byte_seconds"] = elapsed
                transfer.update(last_byte_seconds=elapsed, last_received_bytes=detail["received_bytes"])
            if time.monotonic() - last_print[0] > 10 or detail.get("transfer_phase") != "download":
                print(message, round(fraction * 100, 1), flush=True)
                last_print[0] = time.monotonic()
                report.update(transfer=transfer, elapsed_seconds=time.monotonic() - started)
                write(output / "report.json", report)
        downloaded = download_original(geometry, plan, str(output / "app-download"), auth,
                                       cancel=threading.Event(), progress=progress)
        path = Path(downloaded["files"][0]["path"])
        receipt = json.loads(path.with_suffix(".starmap.json").read_bytes())
        assert path.name == PRODUCT + ".zip"
        app = {"path": str(path), "bytes": path.stat().st_size, "sha256": sha256(path),
               "status": downloaded["files"][0]["status"], "wall_seconds": time.monotonic() - started,
               "safe_validation": receipt["safe_zip_validation"], **transfer}
        if app["last_byte_seconds"]:
            app["mb_per_second_including_service_wait"] = app["bytes"] / app["last_byte_seconds"] / 1024**2
            app["post_transfer_validation_seconds"] = app["wall_seconds"] - app["last_byte_seconds"]
        report.update(app_download=app, requests=requests)
        write(output / "download-result.json", downloaded)
        write(output / "scene.json", scene)
        write(output / "report.json", report)
        if args.local_check:
            report["local_jp2_check"] = local_jp2_check(scene, downloaded, output / "local-check")
            write(output / "report.json", report)
        if args.browser_compare:
            print("开始真实 Edge 官方整包接口对照", flush=True)
            report["browser_download"] = browser_download(auth, plan, output / "browser-download", path)
        report.update(status="passed", completed_utc=dt.datetime.now(dt.timezone.utc).isoformat(),
                      raw_members_or_process_requests=0, requests=requests)
        write(output / "report.json", report)
        print(json.dumps({"status": report["status"], "product": PRODUCT, "bytes": app["bytes"],
                          "app_download_status": app["status"],
                          "browser_exact_zip_bytes_equal": report.get("browser_download", {}).get("exact_zip_bytes_equal"),
                          "local_jp2_check": report.get("local_jp2_check", {}).get("status")}, ensure_ascii=False), flush=True)
    except Exception as error:
        report.update(status="failed", error_type=type(error).__name__, requests=requests)
        write(output / "report.json", report)
        # Connection exceptions can carry credentials or signed URLs; only record the exception type.
        print(json.dumps({"status": "failed", "error_type": type(error).__name__}), flush=True)
        return 1
    finally:
        auth.clear()
        HttpClient.request = original_request
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
