"""Verify a tiny real download; never print or save authentication credentials."""
from __future__ import annotations

import argparse
import getpass
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import numpy as np
import rasterio

from core.download import download_scenes
from core.errors import UserError
from core.geometry import parse_coordinates
from core.http import AuthManager
from core.planning import normalize_options
from core.process_catalogue import find_scenes


def main():
    parser = argparse.ArgumentParser(description="用真实 CDSE 服务下载一个约 44×57 像元的测试区域。")
    parser.add_argument("--prompt", action="store_true", help="从交互式终端读取凭据；Secret 不回显")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "verification" / "live")
    options = parser.parse_args()
    if options.prompt and not sys.stdin.isatty():
        parser.error("--prompt 需要交互式终端。")
    if not options.prompt and sys.stdin.isatty():
        parser.error("请使用 --prompt；自动化也可从标准输入提供 client_id 和 client_secret 的 JSON。")
    auth = AuthManager()
    try:
        if options.prompt:
            auth.connect(input("Client ID: "), getpass.getpass("Client Secret（不显示）: "))
        else:
            credentials = json.load(sys.stdin)
            try:
                auth.connect(credentials["client_id"], credentials["client_secret"])
            finally:
                credentials.clear()
        print("AUTHENTICATION_OK", flush=True)
        geometry = parse_coordinates("116.350,39.900,116.355,39.905")
        scenes = find_scenes(geometry, "2024-06-10", "2024-06-10", 100, auth)
        if not scenes:
            raise UserError("官方目录未返回测试影像。")
        settings = normalize_options({"mode": "rgbnir", "resolution": 10, "max_cloud": 100, "mask_clouds": False})
        result = download_scenes(geometry, [scenes[0]], settings, options.output_dir.resolve(), auth,
                                 progress=lambda message, *rest: print(message, flush=True))
        if not result["files"]:
            raise UserError("该景无有效像元，没有生成测试 GeoTIFF。")
        downloaded = result["files"][0]
        with rasterio.open(downloaded["path"]) as dataset:
            band = dataset.read(1, masked=True)
            values = band.compressed()
            if not values.size or not np.isfinite(values).all():
                raise UserError("GeoTIFF 未包含有效反射率。")
            summary = {"path": downloaded["path"], "status": downloaded["status"], "size": [dataset.width, dataset.height],
                       "epsg": dataset.crs.to_epsg(), "bands": list(dataset.descriptions), "dtype": dataset.dtypes[0],
                       "nodata": dataset.nodata, "valid_pixels": int(values.size),
                       "red_range": [float(values.min()), float(values.max())], "manifest": result["manifest"]}
        print(json.dumps({"LIVE_DOWNLOAD_VERIFIED": summary}, ensure_ascii=False), flush=True)
        return 0
    except (ValueError, TypeError, KeyError, AttributeError):
        print("凭据输入格式不正确；请使用 --prompt 从本地终端输入。", file=sys.stderr)
        return 1
    except UserError as error:
        print(str(error), file=sys.stderr)
        return 1
    finally:
        auth.clear()


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    raise SystemExit(main())
