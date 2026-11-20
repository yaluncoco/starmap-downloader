"""CDSE Sentinel-2 L3 product identity plus matching public Source Cooperative COGs."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
import re

import requests
from shapely.errors import GEOSException
from shapely.geometry import shape

from .errors import UserError
from .http import HttpClient, check_cancel
from .native_catalogue import STAC_ROOT, asset_details, raw_item, search_original
from .scene_identity import UTC, utc_text, utc_time
from .s3_source import L3_BANDS, L3_PRODUCT, l3_mirror_object, l3_mirror_url, mgrs_epsg

S2_L3_PUBLIC = "s2-l3-quarterly"
COLLECTION = "sentinel-2-global-mosaics"
PROVIDER = "CDSE STAC / Taylor Geospatial Engine (Source Cooperative)"
PRODUCT_TYPE = "S2MSI_L3__MCQ"
PROOF_KIND = "l3-mosaic"
DOC_LIMIT = 128 * 1024
ETAG = re.compile(r'"[a-fA-F0-9]{32}(?:-[1-9][0-9]{0,5})?"', re.ASCII)
SHA = re.compile(r"[a-f0-9]{64}", re.ASCII)
SHAPE = [10008, 10008]
NODATA = -32768
REFLECTANCE_SCALE = .0001  # Provider's documented gain; stored TIFF has scale 1, offset 0.
CHECKSUM_KEYS = ("provider_sha256", "provider_sha3_256", "provider_md5")


def quarter(identifier):
    if not l3_mirror_url(identifier):
        raise ValueError
    year, q, tile, _, _ = L3_PRODUCT.fullmatch(identifier).groups()
    year, q = int(year), int(q[1])
    first = dt.datetime(year, 3 * q - 2, 1, tzinfo=UTC)
    stop = dt.datetime(year + (q == 4), 1 if q == 4 else 3 * q + 1, 1, tzinfo=UTC)
    return first, stop, f"{year} Q{q}", tile, mgrs_epsg(tile)


def _interval(props, first, stop):
    t = props.get("datetime")
    return (utc_time(props["start_datetime"]) == first and utc_time(props["end_datetime"]) == stop - dt.timedelta(seconds=1)
            and (t is None or utc_time(t) == first))


def _type(props, epsg, tile):
    return (props.get("product:type") == PRODUCT_TYPE and props.get("processing:level") == "L3"
            and props.get("constellation") == "sentinel-2" and props.get("gsd") == 10
            and props.get("grid:code") == f"MGRS-{tile}" and props.get("proj:code") == f"EPSG:{epsg}")


def _transform(value):
    if (not isinstance(value, list) or len(value) not in (6, 9) or any(type(x) not in (int, float) or not math.isfinite(x) for x in value)
            or value[:2] != [10, 0] or value[3:5] != [0, -10] or not (0 < value[2] < 1_000_000 and 0 < value[5] <= 10_100_000)
            or len(value) == 9 and value[6:] != [0, 0, 1]):
        raise ValueError
    return value[:6]


def _raster(epsg, transform):
    return {"epsg": epsg, "shape": SHAPE, "transform": transform, "dtype": "int16", "count": 1,
            "nodata": NODATA, "scale": 1, "offset": 0, "layout": "COG"}


def _band(asset, band, url, epsg):
    descriptor = asset_details(band, asset, band + ".tif")
    if (asset.get("href") != url or not 0 < (descriptor["bytes"] or 0) <= 512 * 1024**2 or "data" not in descriptor["roles"]
            or descriptor["type"].split(";", 1)[0].strip().lower() != "image/tiff" or asset.get("proj:shape") != SHAPE
            or asset.get("proj:code", f"EPSG:{epsg}") != f"EPSG:{epsg}" or asset.get("proj:epsg", epsg) != epsg
            or asset.get("gsd", 10) != 10 or asset.get("data_type", "int16") != "int16" or asset.get("nodata", NODATA) != NODATA):
        raise ValueError
    return {**descriptor, "raster": _raster(epsg, _transform(asset["proj:transform"]))}


def official_row(feature, geometry, chosen, start, stop, *, radar_mode="", aoi_area=None):
    # Use the official footprint intersected with the AOI and the product interval, not a snapshot date.
    if (row := raw_item(feature, geometry, chosen, start, stop, radar_mode=radar_mode, aoi_area=aoi_area)) is None:
        return None
    try:
        key = row["key"]
        first, until, label, tile, epsg = quarter(key)
        props = feature["properties"]
        if (not _type(props, epsg, tile) or not _interval(props, first, until)
                or first.date() >= stop or until.date() <= start):
            return None
        assets = feature["assets"]
        base = f"s3://eodata/Global-Mosaics/Sentinel-2/{PRODUCT_TYPE}/{first:%Y/%m/%d}/{key}"
        bands = {}
        for alias in L3_BANDS:
            asset = assets.get(alias)
            if not isinstance(asset, dict) or asset.get("data_type") != "int16" or asset.get("nodata") != NODATA:
                continue
            try:
                bands[alias] = _band(asset, alias, f"{base}/{alias}.tif", epsg)
            except (AttributeError, TypeError, KeyError, ValueError, OverflowError):
                continue
        if len({tuple(b["raster"]["transform"]) for b in bands.values()}) > 1:
            bands.clear()  # Do not select an arbitrary reference grid for conflicting official bands.
        row.update(title=key, platform="Sentinel-2 L3", date=label,
                   time=f"{first.date().isoformat()} 至 {(until.date()-dt.timedelta(days=1)).isoformat()}", tiles=[tile],
                   datetime=utc_text(first), cloud_min=None, cloud_max=None,
                   period={"from": utc_text(first), "until_exclusive": utc_text(until)})
        row["raw"] = {"uuid": None, "names": [key], "collection": COLLECTION, "assets": [], "available": False,
                      "public_provider": PROVIDER, "unavailable_reason": "CDSE 目录已找到；尚未核对公开镜像。"}
        row["_official"] = {"bands": bands, "bbox": feature["bbox"]}
        return row
    except (AttributeError, TypeError, ValueError, KeyError, OverflowError):
        return None


def _mirror_index(http, url, cancel):
    response = http.request("GET", url, headers={"Accept-Encoding": "identity"}, stream=True, timeout=(10, 30), cancel=cancel)
    try:
        if response.status_code in (403, 404, 410):
            return None, "CDSE 目录有记录，但公开镜像缺少该季度瓦片的索引；不替换其他季度或产品。"
        tag, size = response.headers.get("ETag", ""), response.headers.get("Content-Length", "")
        if (response.status_code != 200 or not ETAG.fullmatch(tag) or not re.fullmatch(r"[1-9][0-9]{0,5}", size)
                or int(size) > DOC_LIMIT or response.headers.get("Content-Encoding", "identity").lower() != "identity"):
            raise ValueError
        payload = bytearray()
        for block in response.iter_content(8192):
            check_cancel(cancel)
            payload.extend(block)
            if len(payload) > int(size):
                raise ValueError
        if len(payload) != int(size):
            raise ValueError
        document = json.loads(payload)
        return (document, tag, int(size), hashlib.sha256(payload).hexdigest()), None
    except (ValueError, UnicodeError, requests.RequestException):
        return None, "公开镜像索引读取不完整或无法固定版本；请重试或使用 CDSE 官方原始产品。"
    finally:
        response.close()


def _near_bounds(left, right, tolerance):
    return (isinstance(left, (list, tuple)) and isinstance(right, (list, tuple)) and len(left) == len(right) == 4
            and all(type(a) in (float, int) and type(b) in (float, int) and math.isfinite(a) and math.isfinite(b)
                    and abs(a - b) <= tolerance for a, b in zip(left, right)))


def resolve_mirror(row, http, cancel):
    key = row["key"]
    meta = row.pop("_official")
    raw = row["raw"]
    if not meta["bands"]:
        raw["unavailable_reason"] = "官方目录未提供可交叉核对的反射率波段与网格。"
        return row
    first, until, label, tile, epsg = quarter(key)
    url = l3_mirror_url(key)
    index, reason = _mirror_index(http, url, cancel)
    if not index:
        raw["unavailable_reason"] = reason
        return row
    official = f"{STAC_ROOT}/collections/{COLLECTION}/items/{key}"
    doc, tag, size, digest = index
    try:
        props = doc["properties"]
        footprint = shape(doc["geometry"])
        origins = {tuple(member["raster"]["transform"]) for member in meta["bands"].values()}
        transform, = origins
        x, y = transform[2], transform[5]
        from_links = {link.get("href") for link in doc["links"] if link.get("rel") == "derived_from"}
        if (doc["type"] != "Feature" or doc["id"] != key or doc["collection"] != "mosaics" or from_links != {official}
                or not _type(props, epsg, tile) or not _interval(props, first, until) or props.get("proj:shape") != SHAPE
                or not _near_bounds(props.get("proj:bbox"), [x, y-10*SHAPE[0], x+10*SHAPE[1], y], 1e-6)
                or not footprint.is_valid or footprint.geom_type not in ("Polygon", "MultiPolygon")
                or not _near_bounds(doc.get("bbox"), meta["bbox"], .00002)
                or not _near_bounds(footprint.bounds, doc.get("bbox"), .00002)):
            raise ValueError
        proof = {"kind": PROOF_KIND, "product": key, "quarter": label, "period": row["period"],
                 "tile": tile, "epsg": epsg, "transform": list(transform), "reflectance_scale": REFLECTANCE_SCALE,
                 "cdse_item": official, "index_url": url, "index_etag": tag, "index_bytes": size, "index_sha256": digest}
        for band, original in meta["bands"].items():
            public = l3_mirror_url(key, band)
            try:
                entry = _band(doc["assets"][band], band, public, epsg)
            except (AttributeError, TypeError, KeyError, ValueError, OverflowError):
                continue
            if (entry["raster"] != original["raster"] or entry["bytes"] != original["bytes"]
                    or any(entry[k] and original[k] and entry[k] != original[k] for k in CHECKSUM_KEYS)):
                continue
            raw["assets"].append({**entry, **{k: original[k] or entry[k] for k in CHECKSUM_KEYS},
                                  "public_url": public, "identity": proof})
    except (AttributeError, TypeError, KeyError, ValueError, OverflowError, GEOSException):
        raw["assets"].clear()
        raw["unavailable_reason"] = "CDSE 与公开镜像的产品标识、季度或网格不一致；未使用镜像。"
        return row
    raw["available"] = bool(raw["assets"])
    raw["unavailable_reason"] = None if raw["available"] else "未找到路径、大小与网格均匹配的公开波段。"
    return row


def search_l3(geometry, start, end, *, cancel=None, progress=None):
    rows = search_original(geometry, COLLECTION, start, end, row_parser=official_row, cancel=cancel, progress=progress)
    if len(rows) > 100:
        raise UserError("匹配超过 100 个季度瓦片；请缩短日期范围或缩小研究区以核对公开镜像。")
    http = HttpClient()
    try:
        for i, row in enumerate(rows):
            check_cancel(cancel)
            if progress:
                progress(f"核对 L3 季度公开镜像 · {i+1}/{len(rows)}", min(.99, .35 + .64 * (i+1)/len(rows)))
            resolve_mirror(row, http, cancel)
    finally:
        http.close()
    return rows


def check_l3_target(item):
    try:
        proof = item["identity"]
        key, band = item["key"], item["asset"]
        first, until, label, tile, epsg = quarter(key)
        transform = _transform(proof["transform"])
        size, index_size = item["bytes"], proof["index_bytes"]
        if (l3_mirror_object(item["url"]) != (key, band) or item["name"] != band + ".tif" or item["transport"] != "public"
                or item["names"] != [key] or proof["kind"] != PROOF_KIND or proof["product"] != key or proof["quarter"] != label
                or proof["epsg"] != epsg or proof["tile"] != tile or proof["reflectance_scale"] != REFLECTANCE_SCALE
                or proof["period"] != {"from": utc_text(first), "until_exclusive": utc_text(until)}
                or proof["cdse_item"] != f"{STAC_ROOT}/collections/{COLLECTION}/items/{key}"
                or proof["index_url"] != l3_mirror_url(key) or not ETAG.fullmatch(proof["index_etag"])
                or not SHA.fullmatch(proof["index_sha256"]) or type(index_size) is not int or not 0 < index_size <= DOC_LIMIT
                or type(size) is not int or not 0 < size <= 512 * 1024**2 or item["raster"] != _raster(epsg, transform)):
            raise ValueError
    except (AttributeError, TypeError, KeyError, ValueError, OverflowError):
        raise UserError("L3 文件缺少受信任的季度、镜像路径、网格或完整长度。") from None
    return proof


def _pinned_head(http, url, cancel, size, tag=None, *, raster=False):
    if tag is not None and (not isinstance(tag, str) or not ETAG.fullmatch(tag)):
        raise UserError("L3 文件缺少有效的源对象版本，请移出文件及校验记录后重试。")
    headers = {"Accept-Encoding": "identity", **({"If-Match": tag} if tag else {})}
    response = http.request("HEAD", url, headers=headers, stream=True, timeout=(10, 30), cancel=cancel)
    try:
        etag = response.headers.get("ETag", "")
        if response.status_code in (403, 404, 410):
            raise UserError("L3 镜像缺少所选瓦片或波段；没有改用其他季度、瓦片或产品。")
        if (response.status_code != 200 or not ETAG.fullmatch(etag) or tag is not None and etag != tag
                or response.headers.get("Content-Length") != str(size)
                or response.headers.get("Content-Encoding", "identity").lower() != "identity"
                or raster and response.headers.get("Content-Type", "").split(";", 1)[0].lower().strip()
                   not in ("image/tiff", "application/octet-stream")):
            raise UserError("L3 镜像的对象长度或版本已变化；请重新检索，未保存该文件。")
        return etag
    finally:
        response.close()


def verify_l3_listing(http, item, cancel):
    proof = check_l3_target(item)
    return _pinned_head(http, proof["index_url"], cancel, proof["index_bytes"], proof["index_etag"])


def pin_l3_object(http, item, cancel, tag=None):
    check_l3_target(item)
    return _pinned_head(http, item["url"], cancel, item["bytes"], tag, raster=True)
