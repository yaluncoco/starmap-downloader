from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass

from pyproj import Transformer
from shapely import normalize, prepare, transform
from shapely.geometry import box, mapping

from .errors import UserError
from .http import check_cancel
from .spatial_selection import priority_option

ALL_BANDS = ["B01", "B02", "B03", "B04", "B05", "B06", "B07", "B08", "B8A", "B09", "B11", "B12"]
RESEARCH_BANDS = ["B02", "B03", "B04", "B08", "B05", "B06", "B07", "B8A", "B11", "B12"]
MODES = {
    "research": {"label": "研究分析包 · 10 波段 + SCL + 未掩膜备份 + 像元来源", "bands": RESEARCH_BANDS, "output_bands": RESEARCH_BANDS, "dtype": "float32", "nodata": -9999.0},
    "rgb": {"label": "自然色浏览图", "bands": ["B04", "B03", "B02"], "output_bands": ["Red", "Green", "Blue", "Alpha"], "dtype": "uint8", "nodata": None},
    "rgbnir": {"label": "RGB + 近红外反射率", "bands": ["B04", "B03", "B02", "B08"], "output_bands": ["B04", "B03", "B02", "B08"], "dtype": "float32", "nodata": -9999.0},
    "all": {"label": "12 波段反射率", "bands": ALL_BANDS, "output_bands": ALL_BANDS, "dtype": "float32", "nodata": -9999.0},
    "ndvi": {"label": "NDVI", "bands": ["B04", "B08"], "output_bands": ["NDVI"], "dtype": "float32", "nodata": -9999.0},
}


def normalize_options(value):
    mode = str(value.get("mode", "research"))
    try:
        resolution = float(value.get("resolution", 10))
        max_cloud = float(value.get("max_cloud", 20))
    except (ValueError, TypeError):
        raise UserError("分辨率或云量格式不正确。") from None
    if resolution not in (10, 20, 60):
        raise UserError("请选择 10、20 或 60 米输出分辨率。")
    resolution = int(resolution)
    if not 0 <= max_cloud <= 100:
        raise UserError("云量应为 0～100%。")
    if mode == "custom":
        bands = value.get("bands") or []
        if not isinstance(bands, list) or not bands or any(b not in ALL_BANDS for b in bands) or len(set(bands)) != len(bands):
            raise UserError("请选择至少一个有效的 Sentinel-2 L2A 波段（不含 B10）。")
        spec = {"label": "自选波段反射率", "bands": bands, "output_bands": bands, "dtype": "float32", "nodata": -9999.0}
    elif mode in MODES:
        spec = MODES[mode].copy()
    else:
        raise UserError("未知的影像输出类型。")
    layout = value.get("layout", "series")
    if not isinstance(layout, str) or layout not in ("series", "mosaic"):
        raise UserError("请选择跨日期拼接或逐产品时间序列。")
    fill_gaps = value.get("fill_gaps", True)
    if not isinstance(fill_gaps, bool):
        raise UserError("拼接补齐选项无效。")
    return {**spec, "mode": mode, "resolution": resolution, "max_cloud": max_cloud,
            "mask_clouds": bool(value.get("mask_clouds", True)), "count": len(spec["output_bands"]),
            "layout": layout, "priority": priority_option(value), "fill_gaps": fill_gaps}


@dataclass(frozen=True)
class Tile:
    index: int
    row: int
    col: int
    width: int
    height: int
    bounds: tuple
    intersects: bool


def build_plan(geometry, options, scene_count=1, tile_size=2048, *, cancel=None):
    if options.get("mode") == "research":
        tile_size = min(tile_size, 1024)  # Bound memory for two 10-band rasters and quality/provenance.
    if not 1 <= int(scene_count) <= 500:
        raise UserError("每批请选择 1～500 次过境，较长时间序列请分批下载。")
    west, south, east, north = geometry.bounds
    center = geometry.centroid
    if south < -80 or north > 84:
        raise UserError("区域 GeoTIFF 使用 UTM，要求纬度在 80°S～84°N。高纬产品可尝试原始产品目录。")
    if east - west > 8 or north - south > 8:
        raise UserError("研究区跨度超过 8°。请分区下载，避免使用服务不支持的全球等面积投影。")
    zone = max(1, min(60, int((center.x + 180) // 6) + 1))
    epsg = (32600 if center.y >= 0 else 32700) + zone
    crs_label = f"WGS84 / UTM {zone}{'N' if center.y >= 0 else 'S'}"
    transformer = Transformer.from_crs(4326, epsg, always_xy=True)
    dense = geometry.segmentize(0.05)
    projected = transform(dense, transformer.transform, interleaved=False)
    prepare(projected)
    resolution = options["resolution"]
    minx, miny, maxx, maxy = projected.bounds
    minx, miny = math.floor(minx / resolution) * resolution, math.floor(miny / resolution) * resolution
    width = max(1, math.ceil((maxx - minx) / resolution))
    height = max(1, math.ceil((maxy - miny) / resolution))
    maxx, maxy = minx + width * resolution, miny + height * resolution
    cols, rows = math.ceil(width / tile_size), math.ceil(height / tile_size)
    if cols * rows > (16384 if tile_size < 1024 else 4096) or width * height > 2_000_000_000:
        raise UserError("单景输出过大。请提高输出像元大小或将研究区分开下载。")
    tiles = []
    requested_pixels = 0
    for row in range(rows):
        check_cancel(cancel)
        for col in range(cols):
            x, y = col * tile_size, row * tile_size
            w, h = min(tile_size, width - x), min(tile_size, height - y)
            tile_bounds = (minx + x * resolution, maxy - (y + h) * resolution, minx + (x + w) * resolution, maxy - y * resolution)
            frame = box(*tile_bounds)
            intersects = projected.contains(frame) or (projected.intersects(frame) and projected.intersection(frame).area > 0)
            tiles.append(Tile(len(tiles), y, x, w, h, tile_bounds, intersects))
            if intersects:
                requested_pixels += w * h
    count = sum(tile.intersects for tile in tiles)
    research = options.get("mode") == "research"
    estimated_pu = requested_pixels / (512 * 512) * (len(options["bands"]) + int(options["mask_clouds"] or research)) / 3 * scene_count
    bytes_per_sample = 1 if options["dtype"] == "uint8" else 4
    bytes_per_pixel = bytes_per_sample * options["count"]
    if research:
        bytes_per_pixel = bytes_per_pixel * 2 + 1 + 2  # Main + unmasked + uint8 SCL + uint16 source.
    # Do not reuse files produced under an older grid, mask or source-selection contract.
    fingerprint = hashlib.sha256(json.dumps({"grid_version": 5, "geometry": mapping(normalize(geometry)), "options": options}, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:12]
    public = {
        "epsg": epsg, "crs_label": crs_label, "width": width, "height": height,
        "bounds": [minx, miny, maxx, maxy], "resolution": resolution, "scene_count": scene_count,
        "tiles_per_scene": count, "requests": count * scene_count,
        "estimated_pu": round(max(0.005 * count * scene_count, estimated_pu), 2),
        "uncompressed_mb": round(width * height * bytes_per_pixel * scene_count / (1024 * 1024), 2),
        "bytes_per_scene": width * height * bytes_per_pixel,
        "transfer_bands": options["count"] + 2 if research else options["count"],
        "fingerprint": fingerprint, "options": options,
    }
    return public, tiles, projected

