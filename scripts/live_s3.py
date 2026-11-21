"""Audit saved CDSE S3 downloads against members of the exact official Sentinel-2 SAFE."""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import sys
import time
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

import numpy as np
import rasterio

from live_public import B04_SAFE, BBOX, local_app

RESEARCH_ASSETS = ("product_metadata", "granule_metadata", "safe_manifest", "B02_10m", "B03_10m", "B04_10m", "B08_10m",
                   "B05_20m", "B06_20m", "B07_20m", "B8A_20m", "B11_20m", "B12_20m", "SCL_20m")
EXTRA_ASSETS = ("B01_60m", "B09_60m", "AOT_20m", "WVP_20m", "CLD_20m", "SNW_20m", "inspire_metadata", "datastrip_metadata")
CACHE_EVIDENCE = ("size", "expected_bytes", "etag", "content_type", "format_validation", "provider_checksum_verified")


def sha256(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def decode_original(path):
    """Decode every raster block; report native grids and independently parsed XML."""
    if path.suffix.lower() == ".jp2":
        with rasterio.Env(GDAL_PAM_ENABLED="NO", GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR"):
            with rasterio.open(path) as ds:
                bands = []
                for band in ds.indexes:
                    blocks, valid, minimum, maximum = 0, 0, None, None
                    for _, window in ds.block_windows(band):
                        data = ds.read(band, window=window, masked=True).compressed()
                        assert np.isfinite(data).all()
                        blocks += 1
                        valid += data.size
                        if data.size:
                            low, high = float(data.min()), float(data.max())
                            minimum = low if minimum is None else min(minimum, low)
                            maximum = high if maximum is None else max(maximum, high)
                    bands.append({"band": band, "blocks_decoded": blocks, "valid_pixels": int(valid), "min": minimum, "max": maximum})
                return {"kind": "native_jp2", "grid": [ds.width, ds.height], "epsg": ds.crs.to_epsg() if ds.crs else None,
                        "pixel_size": list(ds.res), "dtype": list(ds.dtypes), "nodata": ds.nodata, "bands": bands,
                        "all_blocks_decoded": True}
    root = ET.fromstring(path.read_bytes())
    tags = [node.tag.rsplit("}", 1)[-1] for node in root.iter()]
    return {"kind": "xml", "root": root.tag.rsplit("}", 1)[-1],
            "angle_nodes": {tag: tags.count(tag) for tag in ("Sun_Angles_Grid", "Mean_Sun_Angle",
                                                           "Viewing_Incidence_Angles_Grids", "Mean_Viewing_Incidence_Angle")}}


def check(out, exe, reference, assets, require_transfer=False, require_cache_evidence=False):
    out.mkdir(parents=True, exist_ok=True)
    report_path = out / "s3-completeness-report.json"
    report = {"verified_utc": dt.datetime.now(dt.timezone.utc).isoformat(), "exact_safe": B04_SAFE,
              "bbox_wgs84": BBOX, "executable": str(exe) if exe else "source", "requested_assets": list(assets),
              "reference_safe": str(reference), "reference_sha256": sha256(reference), "status": "running", "files": []}

    def save():
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")

    save()
    with zipfile.ZipFile(reference) as archive, local_app(exe, out) as (origin, api):
        names = archive.namelist()
        assert names and all(name.startswith(B04_SAFE + "/") for name in names)

        def post(action, body):
            with api.post(origin + "/api/" + action, json=body, timeout=(5, 180)) as response:
                if response.status_code != 200:
                    raise RuntimeError(response.json().get("error") or f"{action}: HTTP {response.status_code}")
                return response.json()

        def run(action, body, timeout=7200):
            job = post(action, body)["job_id"]
            deadline, last = time.monotonic() + timeout, 0
            while time.monotonic() < deadline:
                with api.get(origin + "/api/job", params={"id": job}, timeout=5) as response:
                    response.raise_for_status()
                    state = response.json()
                if state["status"] == "completed":
                    return state["result"]
                if state["status"] not in ("running", "cancelling"):
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
        account = boot["accounts"]["providers"]["s3"]
        report.update(version=boot["version"], saved_s3_connected=bool(account["connected"]))
        save()
        try:
            assert account["connected"], "Saved S3 account is not connected"
            geometry = post("aoi", {"coordinates": BBOX})["geometry"]
            criteria = {"collection": "sentinel-2-l2a", "start": "2024-06-10", "end": "2024-06-10", "radar_mode": ""}
            found = run("search", {"profile": "cdse-original", "geometry": geometry, **criteria}, 360)
            scene, = [s for s in found["scenes"] if s["title"].removesuffix(".SAFE") == B04_SAFE.removesuffix(".SAFE")]
            request = {"profile": "cdse-original", "geometry": geometry, "criteria": criteria,
                       "query_id": found["query"]["id"], "scene_keys": [scene["key"]],
                       "options": {"mode": "assets", "assets": list(assets), "transport": "cdse-s3"},
                       "output_dir": str(out / "native-s3")}
            plan = post("plan", request)
            assert plan["requires"] == ["cdse-s3"] and len(plan["targets"]) == len(assets)
            report["planned_bytes"] = plan["estimated_bytes"]
            print(f"CDSE S3: {len(assets)} exact SAFE assets, {plan['estimated_bytes']} bytes", flush=True)
            save()
            result = run("download", request)
            rows = json.loads(Path(result["manifest"]).read_text(encoding="utf-8"))["items"]
            assert len(rows) == len(assets)
            report["manifest"] = result["manifest"]
            for file, row, target in zip(result["files"], rows, plan["targets"], strict=True):
                path = Path(file["path"])
                digest = sha256(path)
                receipt_path = path.with_suffix(".starmap.json")
                receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
                assert row["sha256"] == receipt["sha256"] == digest
                assert row["transport"] == target["transport"] == "cdse-s3"
                assert row["url"] == target["url"] == receipt["url"]
                assert path.stat().st_size == receipt["size"] == target["bytes"]
                member, = [name for name in names if name.endswith("/" + target["name"])]
                assert archive.getinfo(member).file_size == path.stat().st_size
                with archive.open(member) as stream:
                    expected_sha = hashlib.file_digest(stream, "sha256").hexdigest()
                assert digest == expected_sha, f"S3 bytes differ from the exact official SAFE member {target['name']}"
                proof = {"asset": target["asset"], "name": target["name"], "path": str(path), "bytes": path.stat().st_size,
                         "status": file["status"], "sha256": digest, "reference_member": member,
                         "exact_reference_member_sha256": expected_sha, "byte_identical_to_official_safe": True,
                         "zip_member_crc_verified": True, "etag": receipt.get("etag"),
                         "provider_checksum_verified": receipt.get("provider_checksum_verified"), "decode": decode_original(path)}
                report["files"].append(proof)
                print(f"Verified exact SAFE member and full decode: {target['asset']}", flush=True)
                save()
            report["actual_transfer_files"] = sum(f["status"] == "completed" for f in report["files"])
            if require_transfer:
                assert report["actual_transfer_files"] == len(assets), "This run must download all selected assets into a fresh folder"
            repeated = run("download", request)
            cached = json.loads(Path(repeated["manifest"]).read_text(encoding="utf-8"))["items"]
            assert len(cached) == len(rows) and all(f["status"] == "skipped" for f in repeated["files"])
            missing = []
            for row, target in zip(cached, plan["targets"], strict=True):
                receipt = json.loads(Path(row["path"]).with_suffix(".starmap.json").read_text(encoding="utf-8"))
                for key in CACHE_EVIDENCE:
                    if key not in row or row[key] != receipt.get(key):
                        missing.append({"asset": target["asset"], "field": key})
                assert sha256(Path(row["path"])) == row["sha256"] == receipt["sha256"]
            report.update(repeat_checksum_verified=True, cached_manifest_missing_fields=missing,
                          cached_manifest_complete=not missing, repeat_manifest=repeated["manifest"])
            if require_cache_evidence:
                assert not missing, "Repeated download record lost file integrity evidence"
            report["total_verified_bytes"] = sum(f["bytes"] for f in report["files"])
            report["status"] = "passed"
        except Exception as error:
            report.update(status="failed", error=str(error) or type(error).__name__)
            raise
        finally:
            post("preferences", {"output_dir": original_output})
            report["output_preference_restored"] = True
            save()
    print(json.dumps({"report": str(report_path), "status": report["status"], "version": report["version"],
                      "files": len(report["files"]), "verified_bytes": report.get("total_verified_bytes"),
                      "cached_manifest_complete": report.get("cached_manifest_complete")}, ensure_ascii=False), flush=True)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--exe", type=Path)
    parser.add_argument("--source-zip", type=Path, required=True)
    parser.add_argument("--extended", action="store_true", help="Include native B01/B09, AOT/WVP, cloud/snow layers and all five listed metadata files")
    parser.add_argument("--assets", help="Comma-separated exact STAC asset IDs; defaults to the research-source preset")
    parser.add_argument("--require-transfer", action="store_true", help="Require a fresh actual transfer of every selected asset")
    parser.add_argument("--require-cache-evidence", action="store_true", help="Require complete integrity evidence in repeated download records")
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    if args.assets and args.extended:
        parser.error("Use --assets or --extended")
    assets = tuple(a.strip() for a in args.assets.split(",")) if args.assets else RESEARCH_ASSETS + (EXTRA_ASSETS if args.extended else ())
    if not assets or any(not a for a in assets) or len(set(assets)) != len(assets):
        parser.error("Specify distinct, nonempty STAC asset IDs")
    report = check(args.output_dir.resolve(), args.exe.resolve(strict=True) if args.exe else None,
                   args.source_zip.resolve(strict=True), assets, args.require_transfer, args.require_cache_evidence)
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    raise SystemExit(main())
