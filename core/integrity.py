"""Local integrity helpers. A local SHA-256 is not a checksum from a data provider."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import numpy as np
import rasterio

from .errors import UserError
from .http import check_cancel


def sha256_file(path, cancel=None):
    digest = hashlib.sha256()
    with Path(path).open("rb") as file:
        while block := file.read(1024 * 1024):
            check_cancel(cancel)
            digest.update(block)
    return digest.hexdigest()


def publish_file(partial, target):
    """Publish completed data without replacing a concurrently created destination."""
    try:
        if os.name == "nt":
            partial.rename(target)
        else:
            os.link(partial, target)
    except FileExistsError:
        raise UserError("输出目录中已出现同名文件，未覆盖。") from None


def checked_local_receipt(path, receipt, expected, cancel):
    try:
        data = json.loads(receipt.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or any(data.get(key) != value for key, value in expected.items()):
            raise ValueError
        if path.stat().st_size != data["size"] or sha256_file(path, cancel) != data["sha256"]:
            raise ValueError
        return data
    except (OSError, ValueError, KeyError, UnicodeError):
        raise UserError("已有文件缺少匹配的完整性记录或内容已变化；请移走文件和同名 .starmap.json 后重试。") from None


def audit_raster(path, options, valid_pixels, cancel, *, allow_empty=False):
    """Decode every output block; an empty analysis is allowed only with a valid raw backup."""
    total = 0
    per_band = [0] * options["count"] if options["mode"] in ("rtc", "research_unmasked") else None
    try:
        with rasterio.open(path) as raster:
            for _, window in raster.block_windows(1):
                check_cancel(cancel)
                block = raster.read(window=window)
                if options["mode"] == "rgb":
                    valid = block[3] == 255
                    if np.any((block[3] != 0) & ~valid) or np.any(block[:3, ~valid]):
                        raise ValueError
                else:
                    present = np.isfinite(block) & (block != options["nodata"])
                    any_present = np.any(present, axis=0)
                    valid = np.all(present, axis=0) if per_band is None else any_present
                    if np.any(~np.isfinite(block)) or (per_band is None and np.any(any_present != valid)):
                        raise ValueError
                    if per_band is not None:
                        per_band = [n + int(band.sum()) for n, band in zip(per_band, present, strict=True)]
                total += int(np.count_nonzero(valid))
    except (ValueError, OSError, rasterio.errors.RasterioError):
        raise UserError("最终 GeoTIFF 存在无法解码的区块或不一致的掩膜，未保存。") from None
    if (not total and not allow_empty) or total != valid_pixels:
        raise UserError("最终 GeoTIFF 有效像元计数不一致，未保存。")
    result = {"size": path.stat().st_size, "sha256": sha256_file(path, cancel), "valid_pixels": total}
    if per_band is not None:
        result["valid_pixels_per_band"] = dict(zip(options["output_bands"], per_band, strict=True))
    return result
