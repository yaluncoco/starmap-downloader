"""Explicit AWS Open Data Sentinel-1 GRD catalogue and native assets (never a CDSE substitute)."""
from __future__ import annotations

import datetime as dt
import re

from shapely.errors import GEOSException
from shapely.geometry import mapping, shape

from .errors import UserError
from .geometry import area_km2
from .http import HttpClient, check_cancel
from .native_catalogue import _json, asset_details
from .process_catalogue import _polygon_area
from .public_catalogue import EARTH_SEARCH
from .s3_source import ESSENTIAL_EXTENSIONS, aws_radar_object
from .scene_identity import utc_text, utc_time

S1_PUBLIC = "s1-aws-original"
S1_CATALOGUE = "sentinel-1-grd"
SAFE_GRD = re.compile(r"(S1[A-Z])_(IW|EW|SM|S[1-6]|WV)_GRD[FMH]_1[AS](SH|SV|DH|DV)_(\d{8}T\d{6})_\d{8}T\d{6}_\d{6}_[0-9A-F]{6}_[0-9A-F]{4}\Z")


def public_radar_row(feature, geometry, area, start, stop, radar_mode):
    try:
        p, ident = feature["properties"], feature["id"]
        product = p["s1:product_identifier"]
        match = SAFE_GRD.fullmatch(product)
        if feature.get("collection") != S1_CATALOGUE or not match or ident not in (product, product.rsplit("_", 1)[0]) or p.get("sar:product_type") != "GRD":
            return None
        platform, instrument, polarization, sensing = match.groups()
        start_time = dt.datetime.strptime(sensing, "%Y%m%dT%H%M%S").replace(tzinfo=dt.timezone.utc)
        acquire = utc_time(p["datetime"])
        actual_mode = p.get("sar:instrument_mode")
        normalized_mode = "SM" if instrument.startswith("S") else instrument
        if actual_mode not in (instrument, normalized_mode) or (radar_mode and radar_mode != normalized_mode):
            return None
        if not start <= acquire.date() < stop or abs((acquire - start_time).total_seconds()) > 600:
            return None
        footprint = shape(feature["geometry"])
        if not footprint.is_valid or footprint.geom_type not in ("Polygon", "MultiPolygon"):
            return None
        overlap = footprint.intersection(geometry)
        if overlap.is_empty or overlap.area <= 0:
            return None
        assets = []
        asset_map = feature.get("assets")
        if not isinstance(asset_map, dict):
            return None
        for alias, descriptor in asset_map.items():
            if not isinstance(alias, str) or not 1 <= len(alias) <= 140 or not isinstance(descriptor, dict):
                continue
            address = aws_radar_object(descriptor.get("href"))
            if not address:
                continue
            (root, year, month, day, mode, pol, folder, *path), url = address
            if (root != "GRD" or not path or folder not in (product, product + ".SAFE") or mode not in (instrument, normalized_mode)
                    or pol != polarization or year != str(start_time.year) or month.lstrip("0") != str(start_time.month)
                    or day.lstrip("0") != str(start_time.day) or not any(path[-1].lower().endswith(ext) for ext in ESSENTIAL_EXTENSIONS)):
                continue
            details = asset_details(alias, descriptor, path[-1])
            assets.append({**details, "public_url": url})
        if not assets:
            return None
    except (AttributeError, KeyError, TypeError, ValueError, GEOSException):
        return None
    return {"key": product, "title": product, "platform": platform, "date": acquire.date().isoformat(), "time": acquire.strftime("%H:%M:%S"),
            "datetime": utc_text(acquire), "cloud_min": None, "cloud_max": None, "tiles": [normalized_mode, *p.get("sar:polarizations", [])],
            "tile_count": 1, "coverage_percent": round(min(100, 100 * _polygon_area(overlap)/area), 1),
            "products": [{"id": product, "footprint": mapping(footprint)}],
            "raw": {"collection": S1_CATALOGUE, "uuid": None, "names": [product], "assets": assets, "available": True,
                    "public_provider": "Earth Search / AWS Sentinel-1 Open Data"}}


def search_public_radar(geometry, start, stop, radar_mode, *, cancel=None, progress=None):
    original = {"collections": [S1_CATALOGUE], "intersects": mapping(geometry), "limit": 100,
                "datetime": f"{start}T00:00:00Z/{stop}T00:00:00Z"}
    area = area_km2(geometry)
    cursor, seen, found = None, set(), {}
    http = HttpClient()
    try:
        for page in range(100):
            check_cancel(cancel)
            body = {**original, "next": cursor} if cursor is not None else original
            data = _json(http.request("POST", EARTH_SEARCH, json=body, cancel=cancel, timeout=(10, 35)), "AWS 雷达目录")
            features = data.get("features")
            if not isinstance(features, list):
                raise UserError("AWS 雷达目录缺少有效产品列表。")
            for value in features:
                check_cancel(cancel)
                if row := public_radar_row(value, geometry, area, start, stop, radar_mode):
                    found[row["key"]] = row
            if len(found) > 500:
                raise UserError("找到超过 500 景公开雷达产品，请缩小日期或研究区。")
            if progress:
                progress(f"AWS 雷达 · 已匹配 {len(found)} 景原始产品", min(.95, (page + 1)/100))
            links = data.get("links") or []
            next_link = next((link for link in links if isinstance(link, dict) and link.get("rel") == "next"), None)
            if next_link is None:
                return sorted(found.values(), key=lambda row: (row["datetime"], row["key"]), reverse=True)
            body = next_link.get("body")
            cursor = body.get("next") if isinstance(body, dict) else None
            if (next_link.get("href") != EARTH_SEARCH or next_link.get("method", "POST") != "POST"
                    or not isinstance(cursor, (str, int)) or not str(cursor) or len(str(cursor)) > 1000 or cursor in seen):
                raise UserError("AWS 雷达目录分页地址或游标无效。")
            seen.add(cursor)
        raise UserError("AWS 雷达目录分页过多，请缩小日期或研究区。")
    finally:
        http.close()
