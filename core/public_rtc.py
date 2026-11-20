"""Bind Planetary Computer Sentinel-1 RTC COGs to the precise catalogue acquisition and source revision."""
from __future__ import annotations

import datetime as dt
import math
import re

from shapely.errors import GEOSException
from shapely.geometry import mapping, shape

from .errors import UserError
from .open_native import PC_SEARCH, search_native_rows
from .process_catalogue import _polygon_area
from .s3_source import RTC_GRD, RTC_POLARIZATIONS, rtc_blob_object
from .scene_identity import utc_text, utc_time

S1_RTC = "s1-rtc-pc"
RTC_COLLECTION = "sentinel-1-rtc"
RTC_PROVIDER = "Microsoft Planetary Computer / Sentinel-1 RTC (Catalyst)"
RTC_BANDS = ("VV", "VH", "HH", "HV")
RTC_MODES = ("", "IW", "EW")
RTC_NODATA = -32768.0
RTC_NATIVE_STEPS = (10, 20, 25, 40, 60)
OBJECT_LIMIT = 64 * 1024**3


def rtc_options(value):
    bands = value.get("bands", ["VV"])
    resolution = value.get("resolution", 10)
    if value.get("mode", "rtc") != "rtc":
        raise UserError("Sentinel-1 RTC 只导出所选极化的线性 γ⁰ GeoTIFF。")
    if (not isinstance(bands, list) or not 1 <= len(bands) <= len(RTC_BANDS)
            or any(b not in RTC_BANDS for b in bands) or len(set(bands)) != len(bands)):
        raise UserError("请选择至少一个可用的雷达极化（VV、VH、HH、HV），不能重复。")
    if type(resolution) is not int or resolution not in (10, 20, 40, 60):
        raise UserError("RTC 输出像元大小可选 10、20、40 或 60 米。")
    if value.get("mask_clouds", False) is not False:
        raise UserError("Sentinel-1 RTC 不使用光学云掩膜。")
    return {"mode": "rtc", "profile": S1_RTC, "label": "RTC 极化 · 线性 γ⁰ 后向散射", "bands": bands,
            "output_bands": bands, "resolution": resolution, "nodata": RTC_NODATA, "dtype": "float32",
            "count": len(bands), "mask_clouds": False}


def _grid(props):
    epsg = props.get("proj:epsg")
    code = props.get("proj:code")
    if epsg is None and isinstance(code, str) and re.fullmatch(r"EPSG:32[67][0-9]{2}", code):
        epsg = int(code[5:])
    if type(epsg) is not int or not (32601 <= epsg <= 32660 or 32701 <= epsg <= 32760):
        raise ValueError
    if code is not None and code != f"EPSG:{epsg}":
        raise ValueError
    height, width = props["proj:shape"]
    transform = props["proj:transform"]
    if (any(type(n) is not int or not 1 <= n <= 80000 for n in (height, width))
            or not isinstance(transform, list) or len(transform) not in (6, 9)
            or any(type(x) not in (int, float) or not math.isfinite(x) for x in transform)
            or transform[0] not in RTC_NATIVE_STEPS or transform[4] != -transform[0] or transform[1] or transform[3]
            or (len(transform) == 9 and transform[6:] != [0, 0, 1])):
        raise ValueError
    return {"epsg": epsg, "shape": [height, width], "transform": transform[:6]}


def _rtc_asset(value, base, band, grid):
    try:
        address = value["href"]
        identity = rtc_blob_object(address)
        if not identity or identity[0][:-5] != base or identity[1] != band:
            return None
        roles = value["roles"]
        media = value["type"]
        raster, = value["raster:bands"]
        size = value["file:size"]
        if (not isinstance(roles, list) or "data" not in roles or not isinstance(media, str)
                or media.split(";", 1)[0].strip().casefold() != "image/tiff"
                or type(size) is not int or not 1 <= size <= OBJECT_LIMIT
                or raster["data_type"] != "float32" or raster["nodata"] != RTC_NODATA
                or raster["spatial_resolution"] != grid["transform"][0]
                or raster.get("scale", 1) != 1 or raster.get("offset", 0) != 0):
            return None
        for key, expected in (("proj:shape", grid["shape"]), ("proj:epsg", grid["epsg"]), ("proj:code", f"EPSG:{grid['epsg']}")):
            if key in value and value[key] != expected:
                return None
        if "proj:transform" in value:
            other = value["proj:transform"]
            if other not in (grid["transform"], grid["transform"] + [0, 0, 1]):
                return None
        checksum = value.get("file:checksum")
        return identity[0], {**grid, "url": address, "dtype": "float32", "nodata": RTC_NODATA, "scale": 1, "offset": 0,
                             "object_bytes": size, "catalogue_object_md5": (checksum.lower() if isinstance(checksum, str)
                                                                              and re.fullmatch(r"[0-9a-fA-F]{32}", checksum) else None),
                             "pc_scope": "rtc"}
    except (KeyError, AttributeError, TypeError, ValueError):
        return None


def rtc_row(feature, geometry, area, start, stop, radar_mode=""):
    try:
        key, props = feature["id"], feature["properties"]
        base = key.removesuffix("_rtc")
        match = RTC_GRD.fullmatch(base)
        if feature.get("collection") != RTC_COLLECTION or not key.endswith("_rtc") or not match:
            return None
        platform, mode = match["sat"], match["mode"]
        if (props["platform"].casefold() != "sentinel-1" + platform[-1].lower() or props["sar:product_type"] != "GRD"
                or props["sar:instrument_mode"] != mode or radar_mode and radar_mode != mode
                or type(props["sat:absolute_orbit"]) is not int or props["sat:absolute_orbit"] != int(match["orbit"])):
            return None
        acquired = utc_time(props["datetime"])
        first, last = (utc_time(props[k]) for k in ("start_datetime", "end_datetime"))
        named_first, named_last = (dt.datetime.strptime(match[k], "%Y%m%dT%H%M%S").replace(tzinfo=dt.timezone.utc) for k in ("start", "stop"))
        if (not start <= acquired.date() < stop or not first <= acquired <= last or first >= last
                or abs((first - named_first).total_seconds()) > 1 or abs((last - named_last).total_seconds()) > 1):
            return None
        declared = props["sar:polarizations"]
        if (not isinstance(declared, list) or not declared or any(b not in RTC_POLARIZATIONS[match["pol"]] for b in declared)
                or len(declared) != len(set(declared))):
            return None
        region = shape(feature["geometry"])
        if region.geom_type not in ("Polygon", "MultiPolygon") or not region.is_valid:
            return None
        overlap = region.intersection(geometry)
        if overlap.is_empty or overlap.area <= 0:
            return None
        grid = _grid(props)
        checked = {band: item for band in RTC_BANDS if band in declared
                   and (item := _rtc_asset(feature["assets"].get(band.lower()), base, band, grid)) is not None}
        if not checked or len({item[0] for item in checked.values()}) != 1:
            return None
        product = next(iter(checked.values()))[0]
        assets = {band: item[1] for band, item in checked.items()}
    except (AttributeError, KeyError, TypeError, ValueError, OverflowError, GEOSException):
        return None
    return {"key": key, "title": product + " · RTC", "platform": "Sentinel-1" + platform[-1], "datetime": utc_text(acquired),
            "date": acquired.date().isoformat(), "time": acquired.strftime("%H:%M:%S"), "tiles": [mode + " · " + "/".join(assets)],
            "tile_count": 1, "cloud_min": None, "cloud_max": None, "coverage_percent": round(min(100, 100 * _polygon_area(overlap) / area), 1),
            "products": [{"id": product, "footprint": mapping(region)}], "public": {"id": key, "source_id": product, "assets": assets}}


def search_rtc(geometry, start, stop, radar_mode="", **kwargs):
    if radar_mode not in RTC_MODES:
        raise UserError("Sentinel-1 RTC 支持 IW 和 EW 目录模式。")
    return search_native_rows(PC_SEARCH, RTC_COLLECTION, "token", geometry, start, stop,
                              lambda feature, area: rtc_row(feature, geometry, area, start, stop, radar_mode),
                              query_filter={"sar:instrument_mode": {"eq": radar_mode}} if radar_mode else None, **kwargs)
