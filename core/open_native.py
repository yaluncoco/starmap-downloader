"""Product-bound native assets from two independent public catalogues.

These are explicit choices. A failure on CDSE never switches to another provider.
"""
from __future__ import annotations

import datetime as dt
import math
import re

from shapely.errors import GEOSException
from shapely.geometry import mapping, shape

from .errors import UserError
from .geometry import area_km2
from .http import HttpClient, check_cancel
from .native_catalogue import _json, asset_details
from .process_catalogue import _polygon_area
from .public_catalogue import BAND_ASSET, EARTH_SEARCH
from .s3_source import S2_L1C_AWS, S2_L1C_BUCKET, landsat_blob_object
from .scene_identity import utc_text, utc_time

S2_L1C_OPEN = "s2-l1c-aws"
LANDSAT_OPEN = "landsat-pc-l2"
L1C_COLLECTION = "sentinel-2-l1c"
LANDSAT_COLLECTION = "landsat-c2-l2"
PC_SEARCH = "https://planetarycomputer.microsoft.com/api/stac/v1/search"
L1C_ITEM = re.compile(r"(S2[A-Z])_([0-9]{2}[A-Z]{3})_(\d{8})_(0|[1-9][0-9]{0,3})_L1C\Z", re.ASCII)
L1C_SAFE = re.compile(r"(S2[A-Z])_MSIL1C_(\d{8})T\d{6}_N\d{4}_R\d{3}_T([0-9]{2}[A-Z]{3})_\d{8}T\d{6}\.SAFE\Z", re.ASCII)
LANDSAT_ITEM = re.compile(r"(LT04|LT05|LE07|LC08|LC09)_(L2SP|L2SR)_(\d{3})(\d{3})_(\d{8})_02_(T1|T2)\Z", re.ASCII)
LANDSAT_ARCHIVE = re.compile(r"(LT04|LT05|LE07|LC08|LC09)_(L2SP|L2SR)_(\d{3})(\d{3})_(\d{8})_(\d{8})_02_(T1|T2)\Z", re.ASCII)
LANDSAT_SENSORS = {"LT04": "tm", "LT05": "tm", "LE07": "etm", "LC08": "oli-tirs", "LC09": "oli-tirs"}
LANDSAT_QA = {"qa_pixel": "QA_PIXEL.TIF", "qa_radsat": "QA_RADSAT.TIF", "qa_aerosol": "SR_QA_AEROSOL.TIF"}
L1C_ASSETS = {**{band: alias for band, alias in BAND_ASSET.items() if band != "SCL"}, "B10": "cirrus"}


def _grid(doc, epsg, dtype, sizes):
    try:
        h, w = doc["proj:shape"]
        t = doc["proj:transform"]
        if (type(epsg) is not int or not (32601 <= epsg <= 32660 or 32701 <= epsg <= 32760)
                or not all(type(n) is int and 1 <= n <= 20000 for n in (h, w)) or len(t) != 6
                or not all(type(n) in (float, int) and math.isfinite(n) for n in t)
                or t[0] not in sizes or t[4] != -t[0] or t[1] or t[3] or dtype not in ("uint8", "uint16", "int16")):
            return None
        return {"epsg": epsg, "shape": [h, w], "transform": list(t), "dtype": dtype}
    except (KeyError, TypeError, ValueError, OverflowError):
        return None


def _epsg(props):
    value = props.get("proj:epsg")
    code = props.get("proj:code")
    return int(code[5:]) if value is None and isinstance(code, str) and re.fullmatch(r"EPSG:32[67]\d{2}", code) else value


def _raster_dtype(asset):
    bands = asset.get("raster:bands")
    if isinstance(bands, list) and len(bands) == 1 and isinstance(bands[0], dict):
        return bands[0].get("data_type")
    return asset.get("data_type")


def _common(feature, chosen, geometry, area, start, stop, max_cloud):
    if feature.get("collection") != chosen or not isinstance(feature.get("assets"), dict):
        raise ValueError
    props = feature["properties"]
    acquired = utc_time(props["datetime"])
    cloud = float(props["eo:cloud_cover"])
    if not start <= acquired.date() < stop or not 0 <= cloud <= max_cloud:
        raise ValueError
    footprint = shape(feature["geometry"])
    if not footprint.is_valid or footprint.geom_type not in ("Polygon", "MultiPolygon"):
        raise ValueError
    overlap = footprint.intersection(geometry)
    if overlap.is_empty or overlap.area <= 0:
        raise ValueError
    return props, {"key": feature["id"], "datetime": utc_text(acquired), "date": acquired.date().isoformat(),
                   "time": acquired.strftime("%H:%M:%S"), "cloud_min": round(cloud, 1), "cloud_max": round(cloud, 1),
                   "tile_count": 1, "coverage_percent": round(min(100, 100 * _polygon_area(overlap) / area), 1)}, footprint


def l1c_row(feature, geometry, area, start, stop, max_cloud):
    try:
        props, row, footprint = _common(feature, L1C_COLLECTION, geometry, area, start, stop, max_cloud)
        ident = L1C_ITEM.fullmatch(row["key"])
        safe = props["s2:product_uri"]
        product = L1C_SAFE.fullmatch(safe)
        if not ident or not product:
            return None
        sat, mgrs, day, slot = ident.groups()
        if (sat != product[1] or mgrs != product[3] or day != row["date"].replace("-", "")
                or abs((dt.date.fromisoformat(row["date"]) - dt.datetime.strptime(product[2], "%Y%m%d").date()).days) > 1):
            return None
        zone, epsg = int(mgrs[:2]), _epsg(props)
        if not 1 <= zone <= 60 or epsg not in (32600+zone, 32700+zone):
            return None
        dt_day = dt.date.fromisoformat(row["date"])
        path = f"tiles/{zone}/{mgrs[2]}/{mgrs[3:]}/{dt_day.year}/{dt_day.month}/{dt_day.day}/{slot}"
        s3, https = f"s3://{S2_L1C_BUCKET}/{path}", f"https://{S2_L1C_AWS}/{path}"
        identity = {"kind": "l1c-index", "url": https+"/tileInfo.json", "path": path, "product": safe.removesuffix(".SAFE"),
                    "datetime": utc_text(utc_time(props["datetime"]))}
        assets = []
        descriptors = feature["assets"]
        declared_index = descriptors.get("tileinfo_metadata")
        if not isinstance(declared_index, dict) or declared_index.get("href") != s3+"/tileInfo.json":
            return None
        for band, alias in {**L1C_ASSETS, "tileInfo.json": "tileinfo_metadata", "metadata.xml": "granule_metadata"}.items():
            name = band+".jp2" if band in L1C_ASSETS else band
            asset = descriptors.get(alias)
            if not isinstance(asset, dict) or asset.get("href") != f"{s3}/{name}":
                continue
            validated = {**asset_details(band, asset, name), "public_url": f"{https}/{name}", "identity": identity}
            if name.endswith(".jp2"):
                grid = _grid(asset, epsg, _raster_dtype(asset), (10,20,60))
                if grid is None or grid["dtype"] != "uint16":
                    continue
                validated["raster"] = grid
            assets.append(validated)
        if not assets:
            return None
        return {**row, "title": safe, "platform": sat, "tiles": ["T"+mgrs], "products": [{"id": safe, "footprint": mapping(footprint)}],
                "raw": {"uuid": None, "collection": L1C_COLLECTION, "names": [safe], "assets": assets, "available": True,
                        "preferred_metadata": "metadata.xml", "public_provider": "Earth Search / AWS Sentinel-2 L1C"}}
    except (AttributeError, KeyError, ValueError, TypeError, OverflowError, GEOSException):
        return None


def landsat_row(feature, geometry, area, start, stop, max_cloud):
    try:
        props, row, footprint = _common(feature, LANDSAT_COLLECTION, geometry, area, start, stop, max_cloud)
        match = LANDSAT_ITEM.fullmatch(row["key"])
        if not match:
            return None
        sat, level, path, wrs_row, day, tier = match.groups()
        if (row["date"].replace("-", "") != day or str(props.get("landsat:collection_number")) not in ("2", "02")
                or int(props["landsat:wrs_path"]) != int(path) or int(props["landsat:wrs_row"]) != int(wrs_row)
                or not 1 <= int(path) <= 233 or not 1 <= int(wrs_row) <= 248):
            return None
        blobs, releases = [], set()
        for alias, asset in feature["assets"].items():
            if not isinstance(alias, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,80}", alias) or not isinstance(asset, dict):
                continue
            url = asset.get("href")
            address = landsat_blob_object(url)
            if not address:
                continue
            container, level_dir, category, sensor, year, movie, scene, release, name = address
            archive = LANDSAT_ARCHIVE.fullmatch(release)
            if (not archive or name[:len(release)+1] != release+"_" or sensor != LANDSAT_SENSORS[sat] or year != day[:4]
                    or (movie, scene) != (path, wrs_row) or (*archive.groups()[:5], archive[7]) != match.groups()):
                continue
            roles = asset.get("roles") or []
            suff = name.rsplit(".",1)[-1].casefold()
            known_qa = alias in LANDSAT_QA and name == f"{release}_{LANDSAT_QA[alias]}"
            if (not isinstance(roles, list) or (suff == "tif" and "data" not in roles and not known_qa)
                    or (suff != "tif" and (suff not in ("json", "xml", "txt") or "metadata" not in roles))):
                continue
            detail = {**asset_details(alias, asset, name), "pc_url": url}
            if suff == "tif":
                grid = _grid({**props, **asset}, _epsg({**props, **asset}), _raster_dtype(asset), (30,))
                if grid is None:
                    continue
                detail["raster"] = grid
            if name == release+"_MTL.json":
                detail["identity"] = {"kind": "landsat-mtl", "product": release, "date": row["date"], "path": int(path), "row": int(wrs_row)}
            releases.add(release)
            blobs.append(detail)
        if len(releases) != 1 or not blobs:
            return None
        release, = releases
        return {**row, "title": release, "platform": f"Landsat {int(sat[-2:])}", "tiles": [path+"/"+wrs_row],
                "products": [{"id": release, "footprint": mapping(footprint)}],
                "raw": {"uuid": None, "collection": LANDSAT_COLLECTION, "names": [release], "assets": blobs, "available": True,
                        "preferred_metadata": "mtl.json", "public_provider": "USGS Landsat / Microsoft Planetary Computer"}}
    except (AttributeError, KeyError, ValueError, TypeError, OverflowError, GEOSException):
        return None


def _cursor(data, url, chosen, token_key):
    links = data.get("links")
    if links is None:
        return None
    if not isinstance(links, list):
        raise UserError("公开目录未返回可识别的分页信息。")
    for link in links:
        if not isinstance(link, dict) or link.get("rel") != "next":
            continue
        href, payload = link.get("href"), link.get("body")
        addressed = href == url or token_key == "token" and href == url+f"?collections={chosen}"
        token = payload.get(token_key) if isinstance(payload, dict) else None
        if (not addressed or link.get("method", "POST") != "POST" or not isinstance(token, (str, int))
                or isinstance(token, bool) or not 1 <= len(str(token)) <= 1600):
            raise UserError("公开目录分页游标或来源无法核对。")
        return token
    return None


def search_open_native(profile, geometry, start, stop, max_cloud, *, cancel=None, progress=None):
    if profile == S2_L1C_OPEN:
        url, chosen, token_key, as_row = EARTH_SEARCH, L1C_COLLECTION, "next", l1c_row
    elif profile == LANDSAT_OPEN:
        url, chosen, token_key, as_row = PC_SEARCH, LANDSAT_COLLECTION, "token", landsat_row
    else:
        raise UserError("未知的公开原始影像目录。")
    return search_native_rows(url, chosen, token_key, geometry, start, stop,
                              lambda feature, area: as_row(feature, geometry, area, start, stop, max_cloud),
                              cancel=cancel, progress=progress)


def search_native_rows(url, chosen, token_key, geometry, start, stop, as_row, *, query_filter=None, send_geometry=True, cancel=None, progress=None):
    query = {"collections": [chosen], "limit": 100,
             "datetime": f"{start}T00:00:00Z/{stop}T00:00:00Z"}
    if send_geometry:
        query["intersects"] = mapping(geometry)
    elif not query_filter:
        raise UserError("目录请求缺少空间条件。")
    if query_filter:
        query["query"] = query_filter
    http, items, seen, cursor = HttpClient(), {}, set(), None
    area = area_km2(geometry)
    try:
        for page in range(80):
            check_cancel(cancel)
            opts = {**query, token_key: cursor} if cursor is not None else query
            data = _json(http.request("POST", url, json=opts, cancel=cancel, timeout=(10, 40)), "公开影像目录")
            if not isinstance(data.get("features"), list):
                raise UserError("公开目录没有提供影像列表。")
            for feature in data["features"]:
                check_cancel(cancel)
                if row := as_row(feature, area):
                    previous = items.get(row["key"])
                    if previous is not None and previous != row:
                        raise UserError("公开目录返回同一标识的冲突影像，未自动选择版本。")
                    items[row["key"]] = row
            if len(items) > 500:
                raise UserError("匹配超过 500 个产品，请缩小研究区或日期范围。")
            if progress:
                progress(f"公开影像目录 · 已匹配 {len(items)} 个产品", min(.95, (page+1)/80))
            cursor = _cursor(data, url, chosen, token_key)
            if cursor is None:
                return sorted(items.values(), key=lambda x: (x["datetime"], x["key"]), reverse=True)
            if cursor in seen:
                raise UserError("公开目录分页没有推进。")
            seen.add(cursor)
        raise UserError("公开目录分页超过上限，请缩小研究区或日期范围。")
    finally:
        http.close()
