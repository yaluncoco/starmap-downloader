"""Opt-in native Edge SAFE transfer with a bounded prefix or complete product queue."""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.account_store import AccountStore
from core.browser_download import EdgeDownload
from core.errors import Cancelled, UserError, io_diagnostic
from core.geometry import parse_coordinates
from core.local_paths import io_path
from core.raw_auth import RawAuth
from core.raw_download import download_original
from core.safe_zip import build_zip_plan, search_zip, verify_safe_archive

PRODUCTS = ("S2A_MSIL2A_20240610T030531_N0510_R075_T50TMK_20240610T081949.SAFE",
            "S2B_MSIL2A_20240605T030529_N0510_R075_T50TMK_20240605T052413.SAFE")


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--products", type=int, choices=(1, 2), default=2)
    parser.add_argument("--workers", type=int, choices=(1, 2, 3, 4), default=2)
    parser.add_argument("--engine", choices=("edge", "http", "http-direct"), default="edge")
    parser.add_argument("--prefix-mib", type=int, default=0, help="Cancel once every selected product has received this many MiB")
    args = parser.parse_args()
    import webview
    output = args.out.resolve()
    output.mkdir(parents=True, exist_ok=True)
    window = webview.create_window("官方完整产品下载验收", html="<html><body></body></html>", hidden=True)
    engine = EdgeDownload(window)
    outcome = {"status": "failed"}

    def execute():
        auth, cancel = RawAuth(), threading.Event()
        report = {"status": "running", "started_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
                  "engine": args.engine, "workers": args.workers, "prefix_mib": args.prefix_mib,
                  "tls_verification": True, "uses_saved_account_without_persisting_secrets": True}
        started = time.monotonic()
        last_print, last_detail, first = 0., {}, None
        try:
            saved = AccountStore(Path(os.environ["LOCALAPPDATA"]) / "CopernicusDownloader").get("odata")
            if not saved:
                raise UserError("本机没有已保存的 CDSE 账号。")
            if saved.get("requires_totp"):
                auth.restore_refresh(saved["refresh_token"])
            else:
                auth.connect(saved)
            saved.clear()
            geometry = parse_coordinates("116.350,39.900,116.355,39.905")
            available = search_zip(geometry, "2024-06-01", "2024-06-11", 100)
            report["available_products"] = [scene["title"] for scene in available]
            first_scene = next((scene for scene in available if scene["title"] == PRODUCTS[0]), None)
            if first_scene is None:
                raise UserError("官方 STAC 检索没有返回对照产品。")
            scenes = [first_scene]
            if args.products == 2:
                second_scene = next((scene for scene in available if scene["title"] == PRODUCTS[1]), None)
                if second_scene is None:
                    raise UserError("官方 STAC 检索没有返回第二个不同的产品。")
                scenes.append(second_scene)
            plan = build_zip_plan(geometry, scenes, {"download_engine": args.engine, "download_workers": args.workers})
            report.update(plan=plan, selected_product_ids=[target["uuid"] for target in plan["targets"]])
            started = time.monotonic()

            def progress(message, fraction=0, detail=None):
                nonlocal last_print, last_detail, first
                detail = detail or {}
                last_detail = detail
                now = time.monotonic()
                if detail.get("received_bytes") and first is None:
                    first = now
                if args.prefix_mib:
                    rows = detail.get("product_queue")
                    if rows and all(row["received_bytes"] >= args.prefix_mib * 1024**2 for row in rows):
                        cancel.set()
                    elif not rows and args.products == 1 and (detail.get("received_bytes") or 0) >= args.prefix_mib * 1024**2:
                        cancel.set()
                if now - last_print >= 15:
                    print(json.dumps({"progress": round(fraction or 0, 3), "received_mib": round((detail.get("received_bytes") or 0) / 1024**2, 1),
                        "mib_per_second": round((detail.get("bytes_per_second") or 0) / 1024**2, 2),
                        "active": detail.get("active_downloads"), "phase": detail.get("transfer_phase")}), flush=True)
                    last_print = now
                    write(output / "progress.json", {"seconds": now - started, "fraction": fraction, "detail": detail})

            try:
                result = download_original(geometry, plan, str(output), auth, cancel=cancel, progress=progress, browser=engine)
            except Cancelled:
                if not args.prefix_mib:
                    raise
                seconds = time.monotonic() - started
                received = last_detail.get("received_bytes") or 0
                report.update(status="prefix-received-and-cancelled", received_bytes=received, seconds=seconds,
                    first_byte_seconds=first - started if first else None,
                    aggregate_mib_per_second=received / max(.001, time.monotonic() - first) / 1024**2 if first else None,
                    product_queue=last_detail.get("product_queue"))
            else:
                seconds = time.monotonic() - started
                receipts, total_bytes = [], 0
                for file, target in zip(result["files"], plan["targets"], strict=True):
                    path = io_path(file["path"])
                    receipt = json.loads(path.with_suffix(".starmap.json").read_text(encoding="utf-8"))
                    with path.open("rb") as stream:
                        assert hashlib.file_digest(stream, "sha256").hexdigest() == receipt["sha256"]
                    assert receipt["safe_zip_validation"] == verify_safe_archive(path, target)
                    assert path.name == target["name"] + ".zip"
                    total_bytes += path.stat().st_size
                    receipts.append({"path": str(path), "sha256": receipt["sha256"], "bytes": path.stat().st_size,
                                     "safe_zip_validation": receipt["safe_zip_validation"], "engine": receipt["transport_engine"]})
                report.update(status="passed", result=result, seconds_including_first_validation=seconds,
                    total_bytes=total_bytes, aggregate_mib_per_second=total_bytes / seconds / 1024**2,
                    independently_reverified_files=receipts)
                # Retry through a different execution choice. Completed archives
                # must stay in the same folder and require no download response.
                alternate = build_zip_plan(geometry, scenes, {"download_engine": "http-direct", "download_workers": 1})
                assert alternate["folder_name"] == plan["folder_name"] and alternate["fingerprint"] != plan["fingerprint"]
                reused = download_original(geometry, alternate, str(output), auth)
                assert all(file["status"] == "skipped" for file in reused["files"])
                report["switch_engine_reuses_verified_files"] = True
            report["active_native_operations_after_return"] = len(engine.jobs)
        except Exception as error:
            report.update(status="failed", error_type=type(error).__name__)
            report["diagnostic"] = io_diagnostic(error)
            if getattr(error, "browser_interrupt", None):
                report["browser_interrupt"] = error.browser_interrupt
            if isinstance(error, UserError):
                report["message"] = str(error)
        finally:
            auth.clear()
            engine.close()
            report["wall_seconds"] = time.monotonic() - started
            write(output / "report.json", report)
            outcome["status"] = report["status"]
            print(json.dumps({k: v for k, v in report.items() if k not in ("plan", "result", "independently_reverified_files", "product_queue")}, ensure_ascii=False), flush=True)

    webview.start(execute, gui="edgechromium", debug=False, private_mode=True)
    return 0 if outcome["status"] in ("passed", "prefix-received-and-cancelled") else 1


if __name__ == "__main__":
    raise SystemExit(main())
