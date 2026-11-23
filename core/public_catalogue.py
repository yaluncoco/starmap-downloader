"""Credential-free catalogues; keep catalogue and raster identities separate from CDSE OData."""
from __future__ import annotations

import datetime as dt
import math
import re

from shapely.errors import GEOSException
from shapely.geometry import box, mapping, shape

from .errors import UserError
from .geometry import area_km2
from .http import HttpClient, check_cancel
from .native_catalogue import _json
from .process_catalogue import _polygon_area
from .scene_identity import SAFE_ID, utc_text, utc_time
from .s3_source import ES_COLLECTION, ES_HOST, ES_ITEM

S2_PUBLIC = "s2-earth-search"
DEM_30 = "dem-30"
DEM_90 = "dem-90"
DEM = {DEM_30: ("30", "10"), DEM_90: ("90", "30")}
EARTH_SEARCH = "https://earth-search.aws.element84.com/v1/search"
BAND_ASSET = dict(zip("B01 B02 B03 B04 B05 B06 B07 B08 B8A B09 B11 B12 SCL".split(),
                     "coastal blue green red rededge1 rededge2 rededge3 nir nir08 nir09 swir16 swir22 scl".split()))
DEM_TILE = re.compile(r"Copernicus_DSM_COG_(10|30)_([NS])(\d{2})_00_([EW])(\d{3})_00_DEM\Z", re.ASCII)


def _coverage(aoi_area, overlap):
    return round(min(100, 100 * _polygon_area(overlap) / aoi_area), 1)


def _asset_for(key, value, band, item_match, epsg):
    platform, tile, granule = item_match.groups()
    expected = f"https://{ES_HOST}/{ES_COLLECTION}/{int(tile[1:3])}/{tile[3]}/{tile[4:]}/{granule[:4]}/{int(granule[4:6])}/{key}/{band}.tif"
    if not isinstance(value, dict) or value.get("href") != expected:
        return None
    try:
        raster, = value["raster:bands"]
        height, width = value["proj:shape"]
        transform = value["proj:transform"]
        scale, offset = float(raster.get("scale", 1)), float(raster.get("offset", 0))
        dtype, nodata = raster["data_type"], raster["nodata"]
        if (not all(type(n) is int and 1 <= n <= 12000 for n in (height, width)) or len(transform) != 6
                or not all(type(n) in (int, float) and math.isfinite(n) for n in transform)
                or transform[0] not in (10, 20, 60) or transform[1] or transform[3] or transform[4] != -transform[0]
                or dtype != ("uint8" if band == "SCL" else "uint16") or nodata != 0
                or not math.isfinite(scale) or scale <= 0 or not math.isfinite(offset)
                or band == "SCL" and (scale, offset) != (1, 0)
                or band != "SCL" and "scale" not in raster):
            return None
    except (KeyError, AttributeError, ValueError, TypeError):
        return None
    return {"url": expected, "shape": [height, width], "transform": transform, "epsg": epsg,
            "dtype": dtype, "nodata": nodata, "scale": scale, "offset": offset}


def _earth_item(feature, geometry, area, start, stop, max_cloud):
    try:
        props, key = feature["properties"], feature["id"]
        item_match = ES_ITEM.fullmatch(key)
        safe = props["s2:product_uri"]
        product = SAFE_ID.fullmatch(safe)
        acquired = utc_time(props["datetime"])
        cloud, epsg = float(props["eo:cloud_cover"]), props.get("proj:epsg")
        if epsg is None and isinstance(props.get("proj:code"), str) and re.fullmatch(r"EPSG:32[67]\d{2}", props["proj:code"]):
            epsg = int(props["proj:code"][5:])
        if (feature.get("collection") != ES_COLLECTION or not item_match or not product or product[1] != item_match[1]
                or product[3] != item_match[2] or not start <= acquired.date() < stop or not 0 <= cloud <= max_cloud
                or type(epsg) is not int or epsg not in (32600 + int(product[3][1:3]), 32700 + int(product[3][1:3]))):
            return None
        region = shape(feature["geometry"])
        if region.geom_type not in ("Polygon", "MultiPolygon") or not region.is_valid:
            return None
        overlap = region.intersection(geometry)
        if overlap.is_empty or overlap.area <= 0:
            return None
        assets = {band: asset for band, name in BAND_ASSET.items()
                  if (asset := _asset_for(key, feature["assets"].get(name), band, item_match, epsg)) is not None}
    except (KeyError, AttributeError, ValueError, TypeError, OverflowError, GEOSException):
        return None
    return {"key": key, "title": safe, "platform": item_match[1], "datetime": utc_text(acquired), "date": acquired.date().isoformat(),
            "time": acquired.strftime("%H:%M:%S"), "tiles": [item_match[2]], "tile_count": 1,
            "cloud_min": cloud, "cloud_max": cloud, "coverage_percent": _coverage(area, overlap),
            "products": [{"id": safe, "footprint": mapping(region), "acquisition_utc": utc_text(acquired), "cloud": cloud}],
            "public": {"id": key, "source_id": safe, "assets": assets}}


def search_earth(geometry, start, stop, max_cloud, *, cancel=None, progress=None):
    query = {"collections": [ES_COLLECTION], "intersects": mapping(geometry), "limit": 100,
             "datetime": f"{start}T00:00:00Z/{stop}T00:00:00Z"}
    http, seen, items = HttpClient(), set(), {}
    area = area_km2(geometry)
    cursor = None
    try:
        for _ in range(100):
            body = {**query, "next": cursor} if cursor is not None else query
            data = _json(http.request("POST", EARTH_SEARCH, json=body, cancel=cancel, timeout=(10, 30)), "Earth Search 影像目录")
            if not isinstance(data.get("features"), list) or not isinstance(data.get("links", []), list):
                raise UserError("Earth Search 未返回有效目录。")
            for feature in data["features"]:
                check_cancel(cancel)
                row = _earth_item(feature, geometry, area, start, stop, max_cloud)
                if row:
                    items[row["key"]] = row
            if len(items) > 500:
                raise UserError("找到超过 500 个匹配瓦片，请缩短日期或缩小研究区。")
            if progress:
                progress(f"Earth Search · 已匹配 {len(items)} 个瓦片", min(.95, len(seen)/100))
            next_link = next((link for link in data.get("links", []) if isinstance(link, dict) and link.get("rel") == "next"), None)
            if next_link is None:
                return sorted(items.values(), key=lambda row: (row["datetime"], row["key"]), reverse=True)
            next_body = next_link.get("body")
            cursor = next_body.get("next") if isinstance(next_body, dict) else None
            if (next_link.get("href") != EARTH_SEARCH or not isinstance(cursor, str) or not 0 < len(cursor) <= 500 or cursor in seen):
                raise UserError("Earth Search 分页未推进，或分页链接不受信任。")
            seen.add(cursor)
        raise UserError("公开目录分页过多，请缩短日期或缩小研究区。")
    finally:
        http.close()


def _dem_row(profile, key, geometry, area):
    nominal, code = DEM[profile]
    match = DEM_TILE.fullmatch(key)
    if not match or match[1] != code:
        return None
    y, x = int(match[3]) * (1 if match[2] == "N" else -1), int(match[5]) * (1 if match[4] == "E" else -1)
    if not -90 <= y < 90 or not -180 <= x < 180:
        return None
    region = box(x, y, x+1, y+1)
    overlap = region.intersection(geometry)
    if overlap.is_empty or overlap.area <= 0:
        return None
    url = f"https://copernicus-dem-{nominal}m.s3.amazonaws.com/{key}/{key}.tif"
    return {"key": key, "title": key, "platform": f"Copernicus DEM {nominal}m", "datetime": None, "date": "静态", "time": "",
            "tiles": [f"{match[2]}{match[3]}/{match[4]}{match[5]}"], "tile_count": 1, "cloud_min": None, "cloud_max": None,
            "coverage_percent": _coverage(area, overlap), "products": [{"id": key, "footprint": mapping(region)}],
            "raw": {"uuid": None, "names": [key], "available": True, "collection": f"cop-dem-glo-{nominal}",
                    "public_provider": "Copernicus DEM AWS Open Data", "primary_public": "DSM", "assets": [
                        {"id": "DSM", "name": key + ".tif", "public_url": url, "bytes": None, "roles": ["data"],
                         "raster": {"epsg": 4326, "bounds": list(region.bounds), "dtype": "float32",
                                    "height": 3600 if nominal == "30" else 1200}}]},
            "public": {"id": key, "source_id": key, "assets": {"DSM": {"url": url, "epsg": 4326, "bounds": list(region.bounds),
                                                                        "dtype": "float32", "scale": 1, "offset": 0}}}}


def search_dem(geometry, profile, *, cancel=None, progress=None):
    west, south, east, north = geometry.bounds
    if east-west > 8 or north-south > 8:
        raise UserError("研究区跨度超过 8°，请分区检索并导出 DEM。")
    nominal, _ = DEM[profile]
    http = HttpClient()
    if progress:
        progress(f"读取公开 GLO-{nominal} 瓦片索引", .1)
    try:
        response = http.request("GET", f"https://copernicus-dem-{nominal}m.s3.amazonaws.com/tileList.txt", stream=True, cancel=cancel, timeout=(10, 25))
        try:
            if response.status_code != 200:
                raise UserError(f"公开 DEM 索引不可用（HTTP {response.status_code}）。")
            catalog = bytearray()
            for part in response.iter_content(128 * 1024):
                check_cancel(cancel)
                catalog.extend(part)
                if len(catalog) > 4 * 1024**2:
                    raise UserError("公开 DEM 索引超出预期大小。")
        finally:
            response.close()
    finally:
        http.close()
    area = area_km2(geometry)
    matches = []
    seen = set()
    count = 0
    try:
        for key in catalog.decode("ascii").splitlines():
            if not key or key in seen:
                continue
            seen.add(key)
            match = DEM_TILE.fullmatch(key)
            if not match:
                raise ValueError
            count += 1
            x = int(match[5]) * (1 if match[4] == "E" else -1)
            y = int(match[3]) * (1 if match[2] == "N" else -1)
            if x < east and x + 1 > west and y < north and y + 1 > south:
                if row := _dem_row(profile, key, geometry, area):
                    matches.append(row)
        if count < 1000:
            raise ValueError
    except ValueError:
        raise UserError("公开 DEM 瓦片索引格式不正确。") from None
    check_cancel(cancel)
    return sorted(matches, key=lambda row: row["key"])
