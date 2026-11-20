"""Opt-in authenticated raw smoke; sensitive input is local-only and never echoed."""
from __future__ import annotations

import argparse
import getpass
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.errors import UserError
from core.geometry import parse_coordinates
from core.native_catalogue import search_original
from core.raw_auth import RawAuth
from core.raw_download import download_original, raw_plan

SAFE = "S2A_MSIL2A_20240610T030531_N0510_R075_T50TMK_20240610T081949"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--token", action="store_true", help="Paste a temporary CDSE access token at the hidden prompt")
    parser.add_argument("--whole-product", action="store_true", help="Also verify the original product bundle (may be larger than 1 GB)")
    args = parser.parse_args()
    if not sys.stdin.isatty():
        parser.error("Run in a local terminal; do not put your credentials on the command line or in a chat.")
    auth = RawAuth()
    try:
        if args.token:
            auth.connect({"method": "token", "token": getpass.getpass("CDSE access token (hidden): ")})
        else:
            auth.connect({"method": "account", "username": getpass.getpass("CDSE email (hidden): "),
                          "password": getpass.getpass("CDSE password (hidden): "),
                          "totp": getpass.getpass("Current TOTP (if enabled, otherwise Enter): ")})
        geometry = parse_coordinates("116.350,39.900,116.355,39.905")
        collection = "sentinel-2-l2a"
        rows = search_original(geometry, collection, "2024-06-10", "2024-06-10")
        selected = [row for row in rows if row["key"] == SAFE]
        if len(selected) != 1:
            raise UserError("找不到固定测试产品；请核查官方目录，不会替换为其他影像。")
        for mode in ("metadata", "original") if args.whole_product else ("metadata",):
            plan = raw_plan(geometry, selected, {"collection": collection, "mode": mode, "asset": None})
            report = download_original(geometry, plan, args.output_dir.resolve(), auth, progress=lambda message, *_: print(message, flush=True))
            if len(report["files"]) != 1 or report["files"][0]["status"] != "completed":
                raise UserError("请使用空的验证输出文件夹，以确认本次真实下载了字节。")
            proof, = json.loads(Path(report["manifest"]).read_text(encoding="utf-8"))["items"]
            print(json.dumps({"LIVE_ORIGINAL_VERIFIED": {"mode": mode, "product": proof["product"], "sha256": proof["sha256"],
                              "bytes": proof["size"], "provider_checksum_verified": proof.get("provider_checksum_verified"),
                              "manifest": report["manifest"]}}, ensure_ascii=False))
        return 0
    except UserError as error:
        print(str(error), file=sys.stderr)
        return 1
    finally:
        auth.clear()


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    raise SystemExit(main())
