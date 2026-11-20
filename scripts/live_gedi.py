"""Public CMR check, optional private-console Earthdata download, and grid audit."""
from __future__ import annotations

import argparse
import csv
import getpass
import hashlib
import json
import os
import sys
import tempfile
import threading
import time
from contextlib import ExitStack
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import numpy as np
import rasterio

from core.earthdata_auth import EarthdataAuth
from core.errors import UserError
from core.gedi_catalogue import CHECKSUMS, COLLECTIONS, GEDI, search_gedi
from core.gedi_download import build_gedi_plan, download_gedi, normalize_options
from core.geometry import parse_coordinates


def audit_download(result):
    """Compare downloaded bytes to CMR and observation grids to complete CSV rows."""
    folder = Path(result["folder"])
    manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    checks = {"provider_checksums": [], "footprint_grid_audit": None}
    for source in manifest["sources"]:
        checksum = source["source"].get("checksum")
        if checksum:
            digest = hashlib.new(CHECKSUMS[checksum["algorithm"]][0])
            with Path(source["path"]).open("rb") as file:
                while block := file.read(1024 * 1024):
                    digest.update(block)
            if digest.hexdigest() != checksum["value"]:
                raise UserError("独立核对 NASA 原始文件校验值失败。")
            checks["provider_checksums"].append({"name": Path(source["path"]).name, "algorithm": checksum["algorithm"], "verified": True})
        else:
            checks["provider_checksums"].append({"name": Path(source["path"]).name, "verified": None, "reason": "CMR did not supply a checksum"})
    table = folder / "footprints.csv"
    if not table.is_file():
        return checks
    fields = result["plan"]["scalar_fields"]
    samples = {}
    total, in_grid = 0, 0
    field_counts = [0] * len(fields)
    with table.open(encoding="utf-8-sig", newline="") as file:
        for row in csv.DictReader(file):
            total += 1
            if not row.get("grid_row") or not row.get("grid_col"):
                continue
            in_grid += 1
            pixel = (int(row["grid_row"]), int(row["grid_col"]))
            if pixel not in samples and len(samples) < 64:
                samples[pixel] = {"sums": [0.0] * len(fields), "counts": [0] * len(fields), "shots": 0}
            for band, field in enumerate(fields):
                value = row.get(field["id"])
                if value:
                    field_counts[band] += 1
                    if pixel in samples:
                        samples[pixel]["sums"][band] += float(value)
                        samples[pixel]["counts"][band] += 1
            if pixel in samples:
                samples[pixel]["shots"] += 1
    if total != result["statistics"]["accepted_shots"]:
        raise UserError("足迹表格行数与完成记录不一致。")
    counts_path = folder / "gedi_counts.tif"
    if counts_path.is_file():
        with rasterio.open(counts_path) as counts, rasterio.open(folder / "gedi_metrics.tif") as metrics:
            totals = np.zeros(counts.count, dtype="uint64")
            for _, window in counts.block_windows(1):
                totals += counts.read(window=window).sum(axis=(1, 2), dtype="uint64")
            if list(totals) != [in_grid, *field_counts]:
                raise UserError("独立核对 GEDI 观测数量失败。")
            for (row, col), sample in samples.items():
                window = rasterio.windows.Window(col, row, 1, 1)
                observed = metrics.read(window=window)[:, 0, 0]
                expected = [s / n if n else -9999 for s, n in zip(sample["sums"], sample["counts"], strict=True)]
                if not np.allclose(observed, expected, rtol=1e-6, atol=1e-6):
                    raise UserError("独立核对 GEDI 像元均值失败。")
        checks["footprint_grid_audit"] = {"table_rows": total, "in_grid": in_grid, "mean_pixels_checked": len(samples), "verified": True}
    else:
        checks["footprint_grid_audit"] = {"table_rows": total, "in_grid": in_grid, "reason": "No observation grid was requested or no footprints fell inside it"}
    return checks


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--exe", type=Path, help="Verify the packaged desktop executable through its local API")
    parser.add_argument("--collection", choices=list(COLLECTIONS), default="gedi04-a-003")
    parser.add_argument("--bounds", default="116,36,117,37", help="west,south,east,north in WGS84")
    parser.add_argument("--start", default="2020-06-01")
    parser.add_argument("--end", default="2020-06-30")
    parser.add_argument("--output", type=Path, default=ROOT / "verification" / "gedi" / "live")
    parser.add_argument("--download", action="store_true", help="Download one exact granule after a private local authentication prompt")
    parser.add_argument("--auth", choices=("token", "account"), default="token")
    parser.add_argument("--reference", type=Path)
    parser.add_argument("--resolution", type=int, default=100)
    parser.add_argument("--fields", help="Optional comma-separated scalar/profile fields")
    args = parser.parse_args()
    geometry = parse_coordinates(args.bounds)
    args.output = args.output.resolve()
    args.output.mkdir(parents=True, exist_ok=True)
    report_path = args.output / "live-gedi-report.json"
    report = {"collection": args.collection, "bounds": list(geometry.bounds), "start": args.start,
              "end": args.end, "cmr_live_search": False, "live_authenticated_download": False}
    criteria = {"start": args.start, "end": args.end, "collection": args.collection}
    cancel = threading.Event()
    last_progress = 0.0
    def progress(message, fraction):
        nonlocal last_progress
        now = time.monotonic()
        if now - last_progress > 1 or fraction == 1:
            print(f"{fraction * 100:.1f}% · {message}", flush=True)
            last_progress = now
    auth = EarthdataAuth()
    stack = ExitStack()
    api = None
    try:
        if args.exe:
            from live_public import local_app
            from live_s2_tiles import Local
            settings = stack.enter_context(tempfile.TemporaryDirectory(prefix="starmap-gedi-settings-"))
            executable = args.exe.resolve(strict=True)
            origin, session = stack.enter_context(local_app(executable, args.output, Path(settings)))
            api = Local(origin, session)
            bootstrap = session.get(origin + "/api/bootstrap", timeout=5).json()
            report.update(executable=str(executable), packaged_version=bootstrap["version"])
            area = api.post("aoi", {"coordinates": args.bounds})
            search = api.job("search", {"profile": GEDI, "geometry": area["geometry"], **criteria}, timeout=300)["result"]
            scenes = search["scenes"]
        else:
            scenes = search_gedi(geometry, args.collection, args.start, args.end, cancel=cancel, progress=progress)
        report.update(cmr_live_search=True, matches=len(scenes))
        usable = [s for s in scenes if s["gedi"]["available"]]
        if not usable:
            raise UserError("此范围没有可用 GEDI 文件，请调整日期与研究区。")
        selected = min(usable, key=lambda s: s["gedi"].get("estimated_bytes") or float("inf"))
        report["selected"] = selected["gedi"]
        print(f"CMR 找到 {len(scenes)} 个文件；选择 {selected['title']}（约 {(selected['gedi']['estimated_bytes'] or 0) / 1024**2:.1f} MB）。")
        if args.download:
            if args.auth == "token":
                token = os.environ.get("EARTHDATA_TOKEN") or getpass.getpass("仅在本机输入 Earthdata User Token（不会保存）：")
                credentials = {"method": "token", "token": token}
                token = ""
            else:
                username = input("Earthdata 用户名：").strip()
                password = getpass.getpass("Earthdata 密码（不会保存）：")
                credentials = {"method": "account", "username": username, "password": password}
                password = ""
            try:
                if api:
                    with session.post(origin + "/api/earthdata-connect", json=credentials, timeout=65) as response:
                        if response.status_code != 200:
                            raise UserError(response.json().get("error", "本机 Earthdata 连接失败。"))
                else:
                    auth.connect(credentials)
            finally:
                credentials.clear()
            options = {"mode": "aligned", "resolution": args.resolution, "reference_path": str(args.reference.resolve()) if args.reference else ""}
            if args.fields:
                options["fields"] = [s.strip() for s in args.fields.split(",") if s.strip()]
            if api:
                request = {"profile": GEDI, "geometry": area["geometry"], "criteria": criteria,
                           "query_id": search["query"]["id"], "scene_keys": [selected["key"]],
                           "options": options, "output_dir": str(args.output)}
                request["plan_fingerprint"] = api.post("plan", request)["fingerprint"]
                result = api.job("download", request, timeout=7200)["result"]
            else:
                plan = build_gedi_plan(geometry, [selected], normalize_options(options, args.collection), criteria)
                result = download_gedi(geometry, plan, str(args.output), auth, cancel=cancel, progress=progress)
            report.update(live_authenticated_download=True, result_folder=result["folder"],
                          statistics=result["statistics"], checks=audit_download(result))
        else:
            report["note"] = "Public catalogue check only. Authenticated data transfer has not been performed."
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"report": str(report_path), "cmr_live_search": True, "live_authenticated_download": report["live_authenticated_download"]}, ensure_ascii=False))
    except (UserError, RuntimeError) as error:
        report["error"] = str(error)
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(str(error), file=sys.stderr)
        raise SystemExit(1) from None
    except KeyboardInterrupt:
        cancel.set()
        raise SystemExit(130) from None
    finally:
        auth.clear()
        stack.close()


if __name__ == "__main__":
    main()
