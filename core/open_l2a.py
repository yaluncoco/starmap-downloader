"""Earth Search Collection-1 source COGs, matched to the full SAFE and archive index."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
import re

import requests

from .errors import UserError
from .http import check_cancel
from .native_catalogue import asset_details
from .open_native import search_native_rows
from .public_catalogue import BAND_ASSET, EARTH_SEARCH, _earth_item
from .s3_source import ES_COLLECTION, ES_ITEM, l2a_cog_object, l2a_cog_url, mgrs_epsg
from .scene_identity import utc_text, utc_time

S2_L2A_COG = "s2-l2a-cog"
PROVIDER = "Element 84 Earth Search / AWS Sentinel-2 Collection-1 L2A COG"
SAFE = re.compile(r"(S2[A-Z])_MSIL2A_(\d{8}T\d{6})_N(\d{2})(\d{2})_R\d{3}_(T\d{2}[A-Z]{3})_(\d{8}T\d{6})\.SAFE", re.ASCII)
STRIP = re.compile(r"(S2[A-Z])_OPER_MSI_L2A_DS_([A-Z0-9]{4})_(\d{8}T\d{6})_S(\d{8}T\d{6})_N(\d{2}\.\d{2})", re.ASCII)
TILE_ID = re.compile(r"(S2[A-Z])_OPER_MSI_L2A_TL_([A-Z0-9]{4})_(\d{8}T\d{6})_A(\d{6})_(T\d{2}[A-Z]{3})_N(\d{2}\.\d{2})", re.ASCII)
RODA = "s3://sentinel-s2-l2a/"
SHA = re.compile(r"[0-9a-f]{64}", re.ASCII)
ETAG = re.compile(r'"[0-9a-fA-F]{32}(?:-[1-9]\d{0,5})?"', re.ASCII)
SPECTRAL = {band for band in BAND_ASSET if band != "SCL"}
EXTRAS = {"AOT": ("aot", "AOT.tif", "uint16", 20, 1), "WVP": ("wvp", "WVP.tif", "uint16", 20, 1),
          "CLD": ("cloud", "CLD_20m.tif", "uint8", 20, 1), "SNW": ("snow", "SNW_20m.tif", "uint8", 20, 1),
          "TCI": ("visual", "TCI.tif", "uint8", 10, 3)}
RASTER = {band: (alias, f"{band}.tif", "uint8" if band == "SCL" else "uint16",
                 60 if band in ("B01", "B09") else 10 if band in ("B02", "B03", "B04", "B08") else 20, 1)
          for band, alias in BAND_ASSET.items()} | EXTRAS
META = {"metadata.xml": "granule_metadata", "tileInfo.json": "tileinfo_metadata", "product_metadata.xml": "product_metadata"}
FILES = {**{band: (alias, name) for band, (alias, name, *_) in RASTER.items()}, **{name: (alias, name) for name, alias in META.items()}}


def _identity(key, safe, when, datastrip, granule, datatake, archive_path):
    platform, tile, stamp = ES_ITEM.fullmatch(key).groups()
    satellite, sensing, n1, n2, named_tile, built = SAFE.fullmatch(safe).groups()
    epsg = mgrs_epsg(tile[1:])
    baseline = n1 + "." + n2
    strip, gran = STRIP.fullmatch(datastrip), TILE_ID.fullmatch(granule)
    safe_utc = dt.datetime.strptime(sensing, "%Y%m%dT%H%M%S").replace(tzinfo=dt.timezone.utc)
    strip_utc = dt.datetime.strptime(stamp, "%Y%m%dT%H%M%S").replace(tzinfo=dt.timezone.utc)
    day = when.date()
    path = f"tiles/{int(tile[1:3])}/{tile[3]}/{tile[4:]}/{day.year}/{day.month}/{day.day}"
    if (not epsg or platform != satellite or named_tile != tile or not strip or not gran
            or strip.groups() != (satellite, gran[2], built, stamp, baseline)
            or gran.groups() != (satellite, strip[2], built, gran[4], tile, baseline)
            or datatake != f"G{satellite}_{sensing}_{gran[4]}_N{baseline}"
            or max(abs((when - time).total_seconds()) for time in (safe_utc, strip_utc)) > 3 * 3600
            or not re.fullmatch(re.escape(path) + r"/(?:0|[1-9]\d{0,3})", archive_path, re.ASCII)):
        raise ValueError
    return epsg, baseline, built, safe_utc


def _grid(asset, band, epsg, dtype, resolution, count):
    raster = asset["raster:bands"]
    t = asset["proj:transform"]
    w = 109800 // resolution
    if (len(raster) != count or asset.get("proj:shape") != [w, w] or len(t) not in (6, 9)
            or any(type(x) not in (int, float) or not math.isfinite(x) for x in t)
            or t[0] != resolution or t[1] != 0 or t[3] != 0 or t[4] != -resolution
            or not (0 < t[2] < 1_000_000 and 0 < t[5] <= 10_100_000)
            or len(t) == 9 and t[6:] != [0, 0, 1]):
        return None
    if asset.get("proj:epsg", epsg) != epsg or asset.get("proj:code", f"EPSG:{epsg}") != f"EPSG:{epsg}":
        return None
    scale, offset = raster[0].get("scale", 1), raster[0].get("offset", 0)
    if band in SPECTRAL:
        eo, = asset["eo:bands"]
        if eo["name"] != band or scale != .0001 or offset not in (0, -.1):
            return None
    elif (scale, offset) != (.001 if band in ("AOT", "WVP") else 1, 0):
        return None
    if any(b.get("data_type") != dtype or b.get("nodata") != 0 or b.get("spatial_resolution") != resolution
           or b.get("scale", 1) != scale or b.get("offset", 0) != offset for b in raster):
        return None
    return {"epsg": epsg, "shape": [w, w], "transform": t[:6], "dtype": dtype, "count": count,
            "nodata": 0, "scale": scale, "offset": offset, "layout": "COG"}


def l2a_row(feature, geometry, area, start, stop, max_cloud):
    base = _earth_item(feature, geometry, area, start, stop, max_cloud)
    if base is None:
        return None
    try:
        key, safe = base["key"], base["title"]
        props = feature["properties"]
        satellite, tile, _ = ES_ITEM.fullmatch(key).groups()
        acq = utc_time(base["datetime"])
        ds_id, granule, datatake = (props[field] for field in ("s2:datastrip_id", "s2:tile_id", "s2:datatake_id"))
        archive_path, = {link["href"][len(RODA):-len("/metadata.xml")]
                         for link in feature["links"] if isinstance(link, dict) and link.get("rel") == "via"
                         and isinstance(link.get("href"), str) and link["href"].startswith(RODA)
                         and link["href"].endswith("/metadata.xml")}
        epsg, baseline, built, _ = _identity(key, safe, acq, ds_id, granule, datatake, archive_path)
        if (props.get("proj:epsg", epsg) != epsg or props.get("proj:code", f"EPSG:{epsg}") != f"EPSG:{epsg}"
                or props["platform"] != "sentinel-2" + satellite[-1].lower()
                or props["s2:product_type"] != "S2MSI2A" or props["s2:processing_baseline"] != baseline
                or props["grid:code"] != "MGRS-" + tile[1:] or props.get("storage:requester_pays") is not False
                or utc_time(props["s2:generation_time"]).strftime("%Y%m%dT%H%M%S") != built):
            return None
        assets, origin = [], None
        descriptors = feature["assets"]
        for band, (alias, name) in FILES.items():
            item = descriptors.get(alias)
            if not isinstance(item, dict):
                continue
            expected = l2a_cog_url(key, name)
            if not expected or item.get("href") != expected:
                continue
            entry = asset_details(band, item, name)
            if not entry["provider_sha256"] or not 0 < (entry["bytes"] or 0) <= (2 * 1024**3 if band in RASTER else 5 * 1024**2):
                continue
            media = entry["type"].split(";", 1)[0].strip().lower()
            if band in RASTER:
                if media != "image/tiff" or not set(entry["roles"]).intersection(("data", "visual")):
                    continue
                _, _, dtype, resolution, count = RASTER[band]
                try:
                    grid = _grid(item, band, epsg, dtype, resolution, count)
                except (AttributeError, KeyError, TypeError, ValueError):
                    continue
                if grid is None:
                    continue
                xy = [grid["transform"][2], grid["transform"][5]]
                if origin is not None and origin != xy:
                    return None  # Two valid-looking objects describe incompatible origins for the same tile.
                origin = xy
                entry["raster"] = grid
            elif "metadata" not in entry["roles"] or media != ("application/json" if band == "tileInfo.json" else "application/xml"):
                continue
            assets.append({**entry, "public_url": expected})
        idx = next((v for v in assets if v["id"] == "tileInfo.json"), None)
        if not idx or idx["bytes"] > 65536 or not origin:
            return None
        proof = {"kind": "l2a-index", "item": key, "product": safe.removesuffix(".SAFE"), "tile": tile[1:],
                 "datetime": utc_text(acq), "datastrip": ds_id, "granule": granule, "datatake": datatake, "archive_path": archive_path,
                 "epsg": epsg, "origin": origin,
                 "index_url": idx["public_url"], "index_sha256": idx["provider_sha256"], "index_bytes": idx["bytes"]}
        base.pop("public")
        return {**base, "raw": {"uuid": None, "collection": ES_COLLECTION, "names": [safe], "available": True,
                                "preferred_metadata": "product_metadata.xml", "public_provider": PROVIDER,
                                "assets": [{**v, "identity": proof} for v in assets]}}
    except (AttributeError, KeyError, ValueError, TypeError, OverflowError):
        return None


def search_l2a(geometry, start, stop, max_cloud, **kwargs):
    return search_native_rows(EARTH_SEARCH, ES_COLLECTION, "next", geometry, start, stop,
                              lambda f, area: l2a_row(f, geometry, area, start, stop, max_cloud), **kwargs)


def check_l2a_target(item):
    try:
        proof = item["identity"]
        key, band = item["key"], item["asset"]
        acq = utc_time(proof["datetime"])
        name = FILES[band][1]
        epsg, _, _, _ = _identity(key, proof["product"] + ".SAFE", acq, proof["datastrip"], proof["granule"],
                                 proof["datatake"], proof["archive_path"])
        size, index_size = item["bytes"], proof["index_bytes"]
        origin = proof["origin"]
        if (l2a_cog_object(item["url"]) != (key, name) or name != item["name"] or item["transport"] != "public"
                or proof["kind"] != "l2a-index" or proof["item"] != key or proof["tile"] != ES_ITEM.fullmatch(key)[2][1:]
                or item["names"] != [proof["product"] + ".SAFE"] or epsg != proof["epsg"]
                or not isinstance(origin, list) or len(origin) != 2
                or any(type(v) not in (int, float) or not math.isfinite(v) for v in origin)
                or not (0 < origin[0] < 1_000_000 and 0 < origin[1] <= 10_100_000)
                or proof["index_url"] != l2a_cog_url(key, "tileInfo.json")
                or not SHA.fullmatch(proof["index_sha256"]) or not SHA.fullmatch(item["provider_sha256"])
                or type(index_size) is not int or not 0 < index_size <= 65536
                or type(size) is not int or not 0 < size <= (2 * 1024**3 if band in RASTER else 5 * 1024**2)
                or (name == "tileInfo.json" and (item["provider_sha256"], size) != (proof["index_sha256"], index_size))):
            raise ValueError
        raster = item.get("raster")
        if band in RASTER:
            _, _, dtype, resolution, count = RASTER[band]
            w = 109800 // resolution
            expected = {"epsg": epsg, "shape": [w, w], "transform": [resolution, 0, origin[0], 0, -resolution, origin[1]],
                        "dtype": dtype, "count": count, "nodata": 0, "layout": "COG"}
            scale = .0001 if band in SPECTRAL else .001 if band in ("AOT", "WVP") else 1
            if (any(raster[k] != v for k, v in expected.items()) or raster["scale"] != scale
                    or raster["offset"] not in ((0, -.1) if band in SPECTRAL else (0,))):
                raise ValueError
        elif raster is not None:
            raise ValueError
    except (AttributeError, KeyError, ValueError, TypeError, OverflowError):
        raise UserError("L2A 源文件缺少一致的原始网格、瓦片索引或源 SHA-256，未下载。") from None
    return proof


def verify_l2a_xml(root, proof, name):
    def tag(node):
        return node.tag.rsplit("}", 1)[-1]

    def one(nodes, key):
        field, = [node for node in nodes if tag(node) == key]
        return field

    def text(nodes, key):
        return one(nodes, key).text.strip()

    try:
        safe = SAFE.fullmatch(proof["product"] + ".SAFE")
        base = safe[3] + "." + safe[4]
        general = one(root, "General_Info")
        if name == "metadata.xml":
            grid = one(one(root, "Geometric_Info"), "Tile_Geocoding")
            if (tag(root) != "Level-2A_Tile_ID" or text(general, "TILE_ID") != proof["granule"]
                    or text(general, "DATASTRIP_ID") != proof["datastrip"]
                    or abs((utc_time(text(general, "SENSING_TIME")) - utc_time(proof["datetime"])).total_seconds()) >= .001
                    or text(grid, "HORIZONTAL_CS_CODE") != f"EPSG:{proof['epsg']}"):
                raise ValueError
            for res in (10, 20, 60):
                geo, = [node for node in grid if tag(node) == "Geoposition" and node.get("resolution") == str(res)]
                size, = [node for node in grid if tag(node) == "Size" and node.get("resolution") == str(res)]
                if ([float(text(geo, k)) for k in ("ULX", "ULY")] != proof["origin"]
                        or (float(text(geo, "XDIM")), float(text(geo, "YDIM"))) != (res, -res)
                        or any(int(text(size, k)) != 109800 // res for k in ("NROWS", "NCOLS"))):
                    raise ValueError
        else:
            info = one(general, "Product_Info")
            take = one(info, "Datatake")
            if (tag(root) != "Level-2A_User_Product" or text(info, "PRODUCT_URI") != proof["product"] + ".SAFE"
                    or text(info, "PROCESSING_BASELINE") != base or take.get("datatakeIdentifier") != proof["datatake"]
                    or text(take, "SPACECRAFT_NAME") != f"Sentinel-2{safe[1][-1]}"
                    or utc_time(text(take, "DATATAKE_SENSING_START")).strftime("%Y%m%dT%H%M%S") != safe[2]):
                raise ValueError
    except (AttributeError, KeyError, ValueError, TypeError, OverflowError):
        raise UserError("L2A XML 的 SAFE、数据条带、采集时间或网格与所选瓦片不符，未保存。") from None


def verify_l2a_index(http, item, cancel, etag=None):
    proof = check_l2a_target(item)
    headers = {"Accept-Encoding": "identity"}
    if etag:
        headers["If-Match"] = etag
    response = http.request("HEAD" if etag else "GET", proof["index_url"], headers=headers, stream=True,
                            allow_redirects=False, timeout=(10, 30), cancel=cancel)
    try:
        tag = response.headers.get("ETag", "")
        if (response.status_code != 200 or not ETAG.fullmatch(tag) or etag and tag != etag
                or response.headers.get("Content-Encoding", "identity").lower() != "identity"
                or response.headers.get("Content-Length") != str(proof["index_bytes"])):
            raise UserError("L2A 公共索引不可用、长度不符或已改版，未发布原始文件。")
        if etag:
            return tag
        payload = bytearray()
        for block in response.iter_content(8192):
            check_cancel(cancel)
            payload.extend(block)
            if len(payload) > proof["index_bytes"]:
                raise ValueError
        if len(payload) != proof["index_bytes"] or hashlib.sha256(payload).hexdigest() != proof["index_sha256"]:
            raise UserError("L2A 瓦片索引 SHA-256 与目录不符，未使用该景。")
        doc = json.loads(payload)
        tile, when = proof["tile"], utc_time(proof["datetime"])
        _, _, _, safe_at = _identity(proof["item"], proof["product"] + ".SAFE", when, proof["datastrip"], proof["granule"],
                                     proof["datatake"], proof["archive_path"])
        days = {when.date(), safe_at.date()}
        base_paths = {f"products/{day.year}/{day.month}/{day.day}/{proof['product']}" for day in days}
        origin = doc["tileOrigin"]
        idx_crs = origin["crs"]["properties"]["name"]
        product = doc["productPath"]
        if (doc["productName"] != proof["product"] or product not in base_paths
                or doc["datastrip"]["id"] != proof["datastrip"] or utc_time(doc["timestamp"]) != when
                or not re.fullmatch(re.escape(product) + r"/datastrip/(?:0|[1-9]\d{0,3})", doc["datastrip"]["path"], re.ASCII)
                or doc["utmZone"] != int(tile[:2]) or doc["latitudeBand"] != tile[2] or doc["gridSquare"] != tile[3:]
                or doc["path"] != proof["archive_path"] or origin["type"] != "Point" or origin["coordinates"] != proof["origin"]
                or not re.fullmatch(r"urn:ogc:def:crs:EPSG:[0-9.]*:" + str(proof["epsg"]), idx_crs)):
            raise ValueError
        return tag
    except (AttributeError, KeyError, ValueError, TypeError, OverflowError, requests.RequestException):
        raise UserError("L2A 瓦片索引的 SAFE、采集时间、数据条带或网格与所选产品不符。") from None
    finally:
        response.close()
