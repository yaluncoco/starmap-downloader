"""Live check without credentials: regional COG exports, full AWS objects, and CDSE catalogue probes."""
from __future__ import annotations

import argparse
import binascii
import datetime as dt
import hashlib
import json
import os
import re
import socket
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from contextlib import contextmanager
from pathlib import Path

import numpy as np
import rasterio
import requests

ROOT = Path(__file__).resolve().parents[1]
BBOX = "116.350,39.900,116.355,39.905"
B04_SAFE = "S2A_MSIL2A_20240610T030531_N0510_R075_T50TMK_20240610T081949.SAFE"
DEM30 = "Copernicus_DSM_COG_10_N39_00_E116_00_DEM"
DEM90 = "Copernicus_DSM_COG_30_N39_00_E116_00_DEM"
S1_IW = "S1A_IW_GRDH_1SDV_20240612T101408_20240612T101433_054289_069A97_BEF9"
S1_EW = "S1A_EW_GRDM_1SDH_20240616T063959_20240616T064103_054345_069C81_527B"
SVALBARD = "15.60,78.22,15.67,78.25"
PUBLIC_CASES = [
    ("s2-earth-search", B04_SAFE, 10, "B04", 2337, 2508, (0.11, 0.13)),
    ("dem-30", DEM30, 30, "DSM", 252, 300, (48, 51)),
    ("dem-90", DEM90, 90, "DSM", 30, 35, (48, 51)),
]
NATIVE_CASES = [
    ("sentinel-1-grd", "2024-06-08", "2024-06-13", "IW"),
    ("sentinel-2-l1c", "2024-06-10", "2024-06-10", ""),
    ("sentinel-2-l2a", "2024-06-10", "2024-06-10", ""),
    ("sentinel-2-global-mosaics", "2024-04-01", "2024-12-31", ""),
    ("sentinel-3-olci-1-efr-ntc", "2024-06-10", "2024-06-11", ""),
    ("sentinel-3-sl-1-rbt-ntc", "2024-06-10", "2024-06-11", ""),
    ("sentinel-5p-l2-no2-offl", "2024-06-10", "2024-06-12", ""),
    ("landsat-c2-l1-oli-tirs", "2024-06-10", "2024-06-22", ""),
    ("cop-dem-glo-30-dged-cog", None, None, ""),
    ("cop-dem-glo-90-dged-cog", None, None, ""),
    ("clms_dmp_global_300m_10daily_v1_cog", "2024-06-01", "2024-06-20", ""),
    ("modis-terra-mod09q1", "2024-06-01", "2024-06-22", ""),
    ("sentinel-6-p4-2-ntc", "2024-06-09", "2024-06-11", "", "-126,34,-123,37"),
]


@contextmanager
def local_app(executable, out, settings_dir=None):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    command = [str(executable)] if executable else [sys.executable, str(ROOT / "app.py")]
    opts = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
    if settings_dir is not None:
        opts["env"] = {**os.environ, "LOCALAPPDATA": str(settings_dir)}
    with subprocess.Popen([*command, "--server", "--port", str(port)], cwd=out, stdout=subprocess.DEVNULL, **opts) as proc, requests.Session() as session:
        try:
            session.trust_env = False
            origin = f"http://127.0.0.1:{port}"
            for _ in range(100):
                if proc.poll() is not None:
                    raise RuntimeError("Desktop program stopped before starting the local verification service.")
                try:
                    home = session.get(origin, timeout=.4)
                    home.raise_for_status()
                    break
                except requests.RequestException:
                    time.sleep(.2)
            else:
                raise RuntimeError("Packaged local service did not start.")
            assert 'id="sourceProfile"' in home.text
            session.headers["X-App-Token"] = re.search(r'<meta name="app-token" content="([^"<>]+)"', home.text)[1]
            yield origin, session
        finally:
            if proc.poll() is None:
                proc.terminate()
            proc.wait(timeout=8)


def head_aws_object(url, cap):
    # Independent request, not the app's transfer path. Require a verified digest-bearing AWS ETag.
    with requests.head(url, timeout=(10, 30), allow_redirects=False) as response:
        assert response.status_code == 200, f"Independent source HEAD: {response.status_code}"
        etag = response.headers["ETag"]
        assert re.fullmatch(r'"[a-f0-9]{32}"', etag)
        assert response.headers.get("X-Amz-Server-Side-Encryption") in (None, "AES256")
        assert not response.headers.get("X-Amz-Server-Side-Encryption-Customer-Algorithm")
        length = int(response.headers["Content-Length"])
        assert 0 < length <= cap, f"Independent HEAD reports {length} bytes; test cap is {cap}"
    return length, etag


def verify_native_file(result, target, head, kind):
    path = Path(result["files"][0]["path"])
    row, = json.loads(Path(result["manifest"]).read_text(encoding="utf-8"))["items"]
    size, etag = head
    assert result["files"][0]["status"] == "completed", "Run in a fresh directory."
    assert row["product"] == target["key"] and row["url"] == target["url"]
    assert row["transport"] == "public" and row["provider_checksum_verified"] == "AWS single-part ETag (MD5)"
    assert row["etag"] == etag and row["size"] == path.stat().st_size == size
    with path.open("rb") as handle:
        sha = hashlib.file_digest(handle, "sha256").hexdigest()
    with path.open("rb") as handle:
        md5 = hashlib.file_digest(handle, lambda: hashlib.md5(usedforsecurity=False)).hexdigest()
    assert sha == row["sha256"] and md5 == etag[1:-1]
    checks = {}
    if kind in ("safe", "calibration"):
        xml = path.read_bytes()
        root = ET.fromstring(xml)
        if kind == "safe":
            assert root.tag.rsplit("}", 1)[-1].lower() == "xfdu"
            crc = f"{binascii.crc_hqx(xml, 0xFFFF):04X}"
            assert crc == target["key"].rsplit("_", 1)[1]
            checks = {"crc16_of_manifest": crc}
        else:
            labels = [(elt.tag.rsplit("}",1)[-1], (elt.text or "").strip()) for elt in root.iter()]
            assert ("polarisation", "VV") in labels and ("missionId", "S1A") in labels and ("mode", "IW") in labels
            checks = {"calibration_polarisation": "VV", "calibration_vectors": sum(tag == "calibrationVector" for tag, _ in labels)}
            assert checks["calibration_vectors"] > 0
    else:
        with rasterio.open(path) as ds:
            data = ds.read(1, out_shape=(128, 128), masked=True)
            assert ds.count == 1 and data.count() > 0 and np.all(np.isfinite(data.compressed()))
            assert data.min() < data.max()
            checks = {"grid": [ds.width, ds.height], "epsg": ds.crs.to_epsg() if ds.crs else None,
                      "dtype": ds.dtypes[0], "sample_min_max": [float(data.min()), float(data.max())]}
            if kind == "dem":
                expect = target["raster"]
                assert checks["epsg"] == 4326 and checks["dtype"] == "float32" and ds.height == expect["height"]
                assert all(abs(x-y) <= 0.001 for x, y in zip(ds.bounds, expect["bounds"]))
            else:
                assert checks["dtype"] == "uint16" and ds.width > 1000 and ds.height > 1000
    return {"product": target["key"], "asset": target["name"], "kind": kind, "path": str(path), "manifest": result["manifest"],
            "bytes": size, "sha256": sha, "upstream_etag_md5": md5, "independent_head_verified": True,
            "format_validation": row["format_validation"], **checks}


def check(out, executable=None, check_native=False, include_measurement=False):
    out.mkdir(parents=True, exist_ok=True)
    with local_app(executable, out) as (origin, api):
        def post(action, body):
            response = api.post(origin + "/api/" + action, json=body, timeout=(5, 180))
            if response.status_code != 200:
                raise RuntimeError(f"{action}: {response.json().get('error', response.status_code)}")
            return response.json()

        def run(action, body, wait_seconds=180):
            job = post(action, body)["job_id"]
            deadline = time.monotonic() + wait_seconds
            while time.monotonic() < deadline:
                snapshot = api.get(origin + "/api/job", params={"id": job}, timeout=4).json()
                if snapshot.get("status") == "completed":
                    return snapshot["result"]
                if snapshot.get("status") not in ("running", "cancelling"):
                    raise RuntimeError(f"{action}: {snapshot.get('error') or snapshot.get('message')}")
                time.sleep(.5)
            raise RuntimeError(f"Timed out during {action}.")

        version = api.get(origin + "/api/bootstrap", timeout=5).json()["version"]
        report = {"verified_utc": dt.datetime.now(dt.timezone.utc).isoformat(), "version": version, "executable": str(executable) if executable else None,
                  "bbox_wgs84": BBOX, "auth": "not used", "public_exports": [], "public_native_files": [], "native_catalogue_only": []}
        def save_report():
            (out / "live-report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

        aoi = post("aoi", {"coordinates": BBOX})["geometry"]
        for profile, product, metres, band, valid, pixels, median_bounds in PUBLIC_CASES:
            print(f"Live public export: {profile}", flush=True)
            dates = {"start": None, "end": None} if profile.startswith("dem-") else {"start": "2024-06-10", "end": "2024-06-10", "max_cloud": 100}
            query = {"profile": profile, "geometry": aoi, **dates}
            search = run("search", query)
            item, = [v for v in search["scenes"] if v["title"] == product]
            spec = {"mode": "custom", "resolution": metres, "bands": [band], "max_cloud": 100, "mask_clouds": True}
            if profile.startswith("dem-"):
                spec = {"mode": "dem"}
            req = {**query, "criteria": dates, "query_id": search["query"]["id"], "scene_keys": [item["key"]],
                   "options": spec, "output_dir": str(out)}
            plan = post("plan", req)
            assert plan["kind"] == "public" and plan["estimated_pu"] is None
            result = run("download", req)
            file, = result["files"]
            path = Path(file["path"])
            manifest = json.loads(Path(result["manifest"]).read_text(encoding="utf-8"))
            row, = manifest["items"]
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            assert row["sha256"] == digest
            assert file["status"] == "completed", "Run against a new directory to verify actual downloads."
            assert all(t["range_requests"] > 0 and 0 < t["bytes_received"] <= t["object_bytes"] and t["etag"] for t in row["transport"].values())
            with rasterio.open(path) as ds:
                values = ds.read(1, masked=True)
                assert ds.tags()["catalogue_product_id"] == product
                assert ds.crs.to_epsg() == 32650 and ds.nodata == -9999 and ds.res == (metres, metres) and ds.descriptions == (band,)
                assert values.count() == valid and values.size == pixels
                median = float(np.ma.median(values))
                assert median_bounds[0] <= median <= median_bounds[1]
                report["public_exports"].append({"profile": profile, "product": product, "acquisition_utc": row["acquisition_utc"],
                    "geo_tiff": str(path), "manifest": result["manifest"], "sha256": digest, "grid": [ds.width, ds.height],
                    "epsg": 32650, "resolution_m": metres, "valid_pixels": valid, "pixels": pixels,
                    "range_requests": sum(t["range_requests"] for t in row["transport"].values()),
                    "network_bytes": sum(t["bytes_received"] for t in row["transport"].values()),
                    "min_median_max": [float(values.min()), median, float(values.max())]})
            repeated = run("download", req)
            assert repeated["files"][0]["status"] == "skipped"
            report["public_exports"][-1]["repeat_verified"] = True
            save_report()

        def original(profile, product, criteria, geometry, cases):
            query = {"profile": profile, "geometry": geometry}
            search = run("search", {**query, **criteria})
            item, = [v for v in search["scenes"] if v["key"] == product]
            for mode, alias, name, kind, cap in cases:
                print(f"Live AWS original file: {profile} / {name}", flush=True)
                req = {**query, "criteria": criteria, "query_id": search["query"]["id"], "scene_keys": [item["key"]],
                       "options": {"mode": mode, "asset": alias, "transport": "public"}, "output_dir": str(out)}
                plan = post("plan", req)
                target, = plan["targets"]
                assert plan["kind"] == "original" and plan["transports"] == ["public"] and not plan["requires"]
                assert target["key"] == product and target["name"] == name and target["transport"] == "public"
                head = head_aws_object(target["url"], cap)
                result = run("download", req, wait_seconds=700 if kind == "radar" else 380)
                proof = verify_native_file(result, target, head, kind)
                # Only read the disk on a repeat; do not redownload a verified object.
                assert run("download", req)["files"][0]["status"] == "skipped"
                report["public_native_files"].append({**proof, "profile": profile, "repeat_verified": True})
                save_report()

        original("s1-aws-original", S1_IW, {"start": "2024-06-12", "end": "2024-06-12", "radar_mode": "IW"}, aoi,
                 [("metadata", None, "manifest.safe", "safe", 512*1024),
                  ("asset", "schema-calibration-vv", "calibration-iw-vv.xml", "calibration", 4*1024**2)])
        for profile, key in [("dem-30", DEM30), ("dem-90", DEM90)]:
            original(profile, key, {"start": None, "end": None}, aoi,
                     [("original", None, key+".tif", "dem", 64*1024**2)])
        if include_measurement:
            north = post("aoi", {"coordinates": SVALBARD})["geometry"]
            original("s1-aws-original", S1_EW, {"start": "2024-06-16", "end": "2024-06-16", "radar_mode": "EW"}, north,
                     [("asset", "hv", "ew-hv.tiff", "radar", 160*1024**2)])
        if check_native:
            collections = post("collections", {})["collections"]
            available = {row["id"] for row in collections}
            assert len(available) >= 100 and all(c[0] in available for c in NATIVE_CASES)
            report["cdse_live_collection_count"] = len(available)
            for coll, start, end, radar, *bounds in NATIVE_CASES:
                print(f"Live CDSE catalogue only: {coll}", flush=True)
                geometry = post("aoi", {"coordinates": bounds[0]})["geometry"] if bounds else aoi
                r = run("search", {"profile": "cdse-original", "geometry": geometry, "collection": coll, "start": start, "end": end, "radar_mode": radar})
                items = r["scenes"]
                assert items and any(row["raw"]["available"] for row in items), coll
                row = items[0]
                assets = row["raw"]["assets"]
                if coll.startswith(("cop-dem-", "clms_", "modis-")):
                    assert any(asset.get("s3_url") for asset in assets), coll
                if coll == "sentinel-1-grd":
                    assert S1_IW not in {item["key"] for item in items}, "CDSE and AWS product revisions were mixed"
                report["native_catalogue_only"].append({"collection": coll, "results": len(items), "first_product": row["title"],
                     "first_uuid": row["raw"]["uuid"], "first_asset_count": len(assets),
                     "first_s3_asset_count": sum(bool(a.get("s3_url")) for a in assets),
                     "bbox_wgs84": bounds[0] if bounds else BBOX, "original_bytes_downloaded": False})
                save_report()
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--exe", type=Path, help="PyInstaller/installed executable; default runs from source")
    parser.add_argument("--output", type=Path, required=True, help="Use a fresh folder to force real downloads")
    parser.add_argument("--native-catalogue", action="store_true", help="Check a representative set of official STAC collections; no original bytes")
    parser.add_argument("--radar-measurement", action="store_true", help="Also download a real Sentinel-1 HV measurement TIFF (~132 MiB)")
    args = parser.parse_args()
    result = check(args.output.resolve(), args.exe.resolve(strict=True) if args.exe else None, args.native_catalogue, args.radar_measurement)
    print(json.dumps(result, ensure_ascii=True, indent=2))
