"""Microsoft Planetary Computer MODIS C6.1 catalogue and byte-preserving verification."""
from __future__ import annotations

import base64
import datetime as dt
import hashlib
import math
import re
from itertools import product
from xml.etree import ElementTree as ET

import numpy as np
import rasterio
import requests
from shapely.errors import GEOSException
from shapely.geometry import box, mapping, shape
from shapely import transform

from .catalogue import date_range
from .errors import UserError
from .http import check_cancel
from .modis_source import COLLECTIONS, FAMILIES, HOST, LAYERS, PREFIXES, granule, modis_object, object_name, object_url
from .open_native import PC_SEARCH, search_native_rows
from .scene_identity import utc_text, utc_time

PROVIDER = "NASA LP DAAC / Microsoft Planetary Computer (MODIS Collection 6.1)"
PROOF = "modis-061"
MODIS_MIN_DATE = dt.date(2000, 1, 1)
COLLECTION_ROOT = PC_SEARCH.removesuffix("/search") + "/collections/"
RADIUS = 6371007.181
# MODLAND definitions use an exact sphere and 36 × 18 nominal tiles. Published origins
# are rounded differently in two generations of HDF-EOS exports; allow at most 5 mm.
TILE_M = math.pi * RADIUS / 18
MAX_TILES = 96
MAX_XML = 256 * 1024
MAX_HDF = 2 * 1024**3
MAX_COG = 512 * 1024**2
TAG = re.compile(r'"[\x21\x23-\x5b\x5d-\x7e]{1,128}"', re.ASCII)
DECIMAL = re.compile(r"[1-9][0-9]{0,10}", re.ASCII)
MD5 = re.compile(r"[a-fA-F0-9]{32}", re.ASCII)
HDF4 = b"\x0e\x03\x13\x01"
DOCTYPE = re.compile(rb'''<!DOCTYPE\s+GranuleMetaDataFile\s+SYSTEM\s+(?:"[^"<>\[\]]{1,512}"|'[^'<>\[\]]{1,512}')\s*>''')
OCTET_TYPES = ("application/octet-stream", "binary/octet-stream")


def _native(x, y, *_):
    x, y = np.deg2rad(x), np.deg2rad(y)
    return RADIUS * x * np.cos(y), RADIUS * y


def _overlap_tiles(geometry, cancel=None):
    # Densify in geographic coordinates before the nonlinear forward transform. Do not
    # rely on the coarse STAC four-corner footprint at sinusoidal tile edges.
    projected = transform(geometry.segmentize(.01), _native, interleaved=False)
    if not projected.is_valid or projected.is_empty or projected.area <= 0:
        raise UserError("研究区无法映射到 MODIS 正弦网格，请检查边界。")
    areas, total = {}, projected.area
    minx, miny, maxx, maxy = projected.bounds
    for h, v in product(range(36), range(18)):
        x0, x1, y0, y1 = (h - 18) * TILE_M, (h - 17) * TILE_M, (8 - v) * TILE_M, (9 - v) * TILE_M
        if x1 <= minx or x0 >= maxx or y1 <= miny or y0 >= maxy:
            continue
        check_cancel(cancel)
        area = projected.intersection(box(x0, y0, x1, y1)).area
        if area > .000001:
            areas[h, v] = round(min(100, 100 * area / total), 1)
            if len(areas) > MAX_TILES:
                raise UserError(f"研究区覆盖超过 {MAX_TILES} 个 MODLAND 瓦片；请拆分研究区后检索。")
    return areas


def _sinu(crs):
    if crs is None or not crs.is_projected:
        return False
    params = crs.to_dict()
    return (params.get("proj") == "sinu" and params.get("units") == "m" and isinstance(params.get("R"), (int, float))
            and abs(params["R"] - RADIUS) < .00001 and all(params.get(k) == 0 for k in ("lon_0", "x_0", "y_0"))
            and not ({"towgs84", "pm", "datum", "ellps", "a", "b", "es", "axis"} & params.keys()))


def _grid_values(ref):
    n = FAMILIES[ref.family][0]
    return [n, n], (TILE_M / n, 0, (ref.h - 18) * TILE_M, 0, -TILE_M / n, (9 - ref.v) * TILE_M)


def _grid(ref, pixel_shape, affine, expected=None):
    size, ideal = _grid_values(ref)
    return (list(pixel_shape) == size and len(affine) == 6
            and all(type(v) in (int, float) and math.isfinite(v) for v in affine)
            and affine[1] == affine[3] == 0
            and all(abs(v-w) <= (.005 if i in (2, 5) else .000005) for i, (v, w) in enumerate(zip(affine, ideal)))
            and (expected is None or all(abs(v-w) <= .000001 for v, w in zip(affine, expected))))


def _period(ref, first, last, source):
    """Validate exact dates without substituting a catalogue midpoint for the labelled day."""
    start = ref.nominal
    end = start + dt.timedelta(days=FAMILIES[ref.family][1] - 1)
    if ref.family == "43A4":
        observation_start = start - dt.timedelta(days=8)
        valid = last == end and (first == observation_start if source == "cog" else first in (start, observation_start))
    else:
        # Some upstream products end on 31 December, others publish the full nominal
        # period into January. Store the actual provider date, not an invented clamp.
        valid = first == start and last in (end, min(end, dt.date(start.year, 12, 31)))
    if not valid:
        raise ValueError("MODIS dates")
    return {"from": first.isoformat(), "through": last.isoformat()}


def _day(value):
    day = dt.date.fromisoformat(value)
    if day.isoformat() != value:
        raise ValueError("MODIS ISO date")
    return day


def _raster(ref, layer, affine):
    spec = LAYERS[ref.family][layer]
    return {"crs": "MODLAND Sinusoidal", "sphere_radius_m": RADIUS, "shape": _grid_values(ref)[0], "transform": list(affine),
            "dtype": spec.dtype, "nodata": spec.nodata, "tiff_scale": spec.stored_gain, "tiff_offset": spec.offset,
            "physical_scale": spec.gain, "physical_offset": spec.offset, "units": spec.units,
            "value_note": spec.notice or ("Use DN × physical_scale, not the inverse factor stored in the TIFF." if spec.tiff_gain else None)}


def _max_bytes(layer):
    return MAX_XML if layer == "metadata" else MAX_HDF if layer == "hdf" else MAX_COG


def _asset(ref, alias, value, proof, affine):
    if not isinstance(value, dict) or value.get("href") != object_url(ref, alias):
        return None
    try:
        size = value.get("file:size")
        if size is not None and (type(size) is not int or not 0 < size <= _max_bytes(alias)):
            return None
        raster = None
        if alias not in ("hdf", "metadata"):
            spec = LAYERS[ref.family][alias]
            band, = value["raster:bands"]
            reliability = alias.endswith("_pixel_reliability")
            if (band["data_type"] not in ((spec.dtype, "int8") if reliability else (spec.dtype,))
                    or band.get("scale", spec.gain) != spec.gain or band.get("offset", spec.offset) != spec.offset
                    or band.get("nodata", spec.nodata) not in ((spec.nodata, -1) if reliability else (spec.nodata,))
                    or not _grid(ref, value.get("proj:shape", _grid_values(ref)[0]), value.get("proj:transform", affine), affine)):
                return None
            raster = _raster(ref, alias, affine)
        title = "原始 HDF4 · 全部数据层" if alias == "hdf" else "NASA 配套 XML · 瓦片元数据" if alias == "metadata" else f"COG · {alias}"
        return {"id": alias, "name": object_name(ref, alias), "title": title, "pc_url": object_url(ref, alias), "bytes": size,
                "raster": raster, "identity": {**proof, "layer": alias}}
    except (KeyError, TypeError, ValueError, AttributeError):
        return None


def modis_row(feature, collection, start, stop, overlaps):
    try:
        ref = granule(feature["id"])
        if (feature["type"] != "Feature" or feature["collection"] != collection or ref.collection != collection
                or not start <= ref.nominal < stop or (ref.h, ref.v) not in overlaps):
            return None
        p, supplied = feature["properties"], feature["assets"]
        wkt = p["proj:wkt2"]
        affine = p["proj:transform"]
        first, last = utc_time(p["start_datetime"]), utc_time(p["end_datetime"])
        if (p["modis:tile-id"] != ref.tile_id or type(p["modis:horizontal-tile"]) is not int or p["modis:horizontal-tile"] != ref.h
                or type(p["modis:vertical-tile"]) is not int or p["modis:vertical-tile"] != ref.v
                or first > last or not isinstance(wkt, str) or len(wkt) > 64000 or not _sinu(rasterio.crs.CRS.from_wkt(wkt))
                or not _grid(ref, p["proj:shape"], affine)):
            return None
        window = _period(ref, first.date(), last.date(), "catalogue")
        catalog_item = f"{COLLECTION_ROOT}{collection}/items/{ref.key}"
        proof = {"kind": PROOF, "product": ref.key, "collection": collection, "version": "061", "short_name": ref.short,
                 "tile": ref.tile, "nominal_date": ref.nominal.isoformat(), "catalogue_item": catalog_item, "catalogue_period": window,
                 "nasa_xml_url": object_url(ref, "metadata") if "metadata" in supplied else None}
        assets = [entry for alias in ("hdf", "metadata", *LAYERS[ref.family])
                  if (entry := _asset(ref, alias, supplied.get(alias), proof, affine)) is not None]
        ids = {a["id"] for a in assets}
        if "metadata" in supplied and "metadata" not in ids:
            # A missing XML and a listed XML pointing somewhere else are not equivalent.
            assets = [a for a in assets if a["id"] != "hdf"]
        region = shape(feature["geometry"])
        footprints = [{"id": ref.key, "footprint": mapping(region)}] if region.geom_type in ("Polygon", "MultiPolygon") and region.is_valid else []
    except (AttributeError, TypeError, KeyError, ValueError, OverflowError, GEOSException, rasterio.errors.CRSError):
        return None
    note = f"目录 {window['from']} 至 {window['through']} · {sum(a['raster'] is not None for a in assets)} 个 COG"
    if ref.family == "43A4":
        note = f"16 天观测 {(ref.nominal-dt.timedelta(days=8)).isoformat()} 至 {window['through']} · {sum(a['raster'] is not None for a in assets)} 个 COG"
    if "metadata" not in ids:
        note += " · 无配套 XML"
    nominal = dt.datetime.combine(ref.nominal, dt.time(), tzinfo=dt.timezone.utc)
    return {"key": ref.key, "title": ref.key, "platform": PREFIXES[ref.prefix], "datetime": utc_text(nominal), "date": nominal.date().isoformat(),
            "time": "标称日", "tiles": [ref.tile], "tile_count": 1, "cloud_min": None, "cloud_max": None,
            "coverage_percent": overlaps[ref.h, ref.v], "products": footprints,
            "raw": {"uuid": None, "names": [ref.key], "assets": assets, "available": bool(assets), "public_provider": PROVIDER,
                    "primary_pc": "hdf" if any(a["id"] == "hdf" for a in assets) else None,
                    "preferred_metadata": "metadata", "source_note": note,
                    "unavailable_reason": None if assets else "目录中没有通过文件路径与网格核对的 HDF、XML 或 COG"}}


def search_modis(geometry, collection, start, end, *, cancel=None, progress=None):
    if collection not in COLLECTIONS:
        raise UserError("无效的 MODIS Collection 6.1 类别。")
    start, stop = date_range(start, end, MODIS_MIN_DATE)
    if progress:
        progress("计算与研究区相交的 MODLAND 瓦片", .01)
    check_cancel(cancel)
    try:
        overlaps = _overlap_tiles(geometry, cancel)
    except GEOSException:
        raise UserError("研究区无法映射到 MODIS 正弦网格，请检查边界。") from None
    if not overlaps:
        return []
    tile_ids = [f"510{h:02d}0{v:02d}" for h, v in overlaps]
    return search_native_rows(PC_SEARCH, collection, "token", geometry, start, stop,
                              lambda feature, area: modis_row(feature, collection, start, stop, overlaps),
                              query_filter={"modis:tile-id": {"in": tile_ids}}, send_geometry=False, cancel=cancel, progress=progress)


def is_modis(item):
    return (isinstance(item.get("url"), str) and item["url"].startswith(f"https://{HOST}/")) or (item.get("identity") or {}).get("kind") == PROOF


def check_modis_target(item):
    try:
        ref, alias = modis_object(item["url"])
        proof = item["identity"]
        period = proof["catalogue_period"]
        if (item["transport"] != "pc-public" or item["key"] != ref.key or item["names"] != [ref.key] or item["uuid"] is not None
                or item["asset"] != alias or item["name"] != object_name(ref, alias) or proof["kind"] != PROOF or proof["product"] != ref.key
                or proof["collection"] != ref.collection or proof["version"] != "061" or proof["short_name"] != ref.short
                or proof["tile"] != ref.tile or proof["nominal_date"] != ref.nominal.isoformat() or proof["layer"] != alias
                or proof["catalogue_item"] != f"{COLLECTION_ROOT}{ref.collection}/items/{ref.key}"
                or proof["nasa_xml_url"] not in (None, object_url(ref, "metadata"))
                or alias == "metadata" and proof["nasa_xml_url"] != item["url"]
                or period != _period(ref, _day(period["from"]), _day(period["through"]), "catalogue")):
            raise ValueError
        size = item["bytes"]
        if size is not None and (type(size) is not int or not 0 < size <= _max_bytes(alias)):
            raise ValueError
        r = item["raster"]
        if alias in ("hdf", "metadata"):
            if r is not None:
                raise ValueError
        elif not _grid(ref, r["shape"], r["transform"]) or r != _raster(ref, alias, r["transform"]):
            raise ValueError
    except (AttributeError, TypeError, KeyError, ValueError, OverflowError):
        raise UserError("MODIS 所选文件的 Collection 6.1 产品号、标称日、路径、图层或网格不一致。") from None
    return ref, alias


def modis_scope(alias):
    return "modis-source" if alias in ("hdf", "metadata") else "modis-cog"


def modis_blob_request(http, method, url, headers, cancel, access, *, status=None):
    if method not in ("GET", "HEAD") or access is None or not modis_object(url):
        raise UserError("MODIS 文件没有匹配的只读对象通道。")
    for attempt in range(2):
        response = http.request(method, url, headers=headers, auth=access, stream=True, timeout=(12, 90),
                                cancel=cancel, status=status, allow_redirects=False)
        code = response.status_code
        if code in (200, 206):
            return response
        response.close()
        if code in (401, 403) and attempt == 0:
            access.clear()
            continue
        if code in (401, 403, 404, 409, 410):
            raise UserError("MODIS 档案没有提供所选对象或暂时不能读取。未换用其他日期、瓦片或版本。")
        if code in (301, 302, 303, 307, 308):
            raise UserError("MODIS 对象返回重定向；没有访问新地址或转发授权。")
        if code == 412:
            raise UserError("MODIS 对象版本已变化；请重新检索，未拼接或替换此文件。")
        raise UserError(f"MODIS 对象读取失败（HTTP {code}），没有换用其他版本。")


def modis_content_type(headers, alias):
    want = {"hdf": ("application/x-hdf", "application/x-hdf4"), "metadata": ("text/xml", "application/xml")}.get(alias, ("image/tiff",))
    return (headers.get("Content-Encoding", "identity").lower().strip() == "identity"
            and headers.get("Content-Type", "").split(";", 1)[0].lower().strip() in (*OCTET_TYPES, *want))


def _full_md5(headers):
    a, b = headers.get("Content-MD5"), headers.get("x-ms-blob-content-md5")
    if a and b and a != b:
        raise ValueError
    raw = base64.b64decode(a or b, validate=True)
    if len(raw) != 16:
        raise ValueError
    return raw.hex()


def _head(http, ref, alias, cancel, access, *, expected_size=None, expected_tag=None):
    url = object_url(ref, alias)
    if expected_tag is not None and (not isinstance(expected_tag, str) or not TAG.fullmatch(expected_tag)):
        raise UserError("MODIS 已有文件缺少有效的对象版本记录；请将文件和 .starmap.json 移出后重试。")
    headers = {"Accept-Encoding": "identity", **({"If-Match": expected_tag} if expected_tag else {})}
    with modis_blob_request(http, "HEAD", url, headers, cancel, access) as response:
        try:
            tag, length = response.headers.get("ETag", ""), response.headers.get("Content-Length", "")
            if (response.status_code != 200 or not TAG.fullmatch(tag) or not DECIMAL.fullmatch(length)
                    or not 0 < int(length) <= _max_bytes(alias) or expected_size is not None and int(length) != expected_size
                    or expected_tag is not None and expected_tag != tag or not modis_content_type(response.headers, alias)):
                raise ValueError
            return {"size": int(length), "etag": tag, "md5": _full_md5(response.headers)}
        except (ValueError, TypeError):
            raise UserError("MODIS 源对象缺少一致的长度、版本、文件类型或完整 MD5，未保存。") from None


def verify_xml(data, ref):
    try:
        # Strip the external NASA declaration. ElementTree does not load it; also
        # reject internal subsets/entities and alternate byte encodings explicitly.
        if not 0 < len(data) <= MAX_XML or b"\x00" in data:
            raise ValueError
        sanitized = DOCTYPE.sub(b"", data, count=1)
        if b"<!DOCTYPE" in sanitized or b"<!ENTITY" in sanitized:
            raise ValueError
        root = ET.fromstring(sanitized)
        data_root = root.find("GranuleURMetaData")
        g = data_root.find("ECSDataGranule")
        collection = data_root.find("CollectionMetaData")
        file, = data_root.findall("./DataFiles/DataFileContainer")
        ranges = data_root.find("RangeDateTime")
        period = _period(ref, _day(ranges.findtext("RangeBeginningDate")), _day(ranges.findtext("RangeEndingDate")), "xml")
        size, md5 = file.findtext("FileSize", ""), file.findtext("Checksum", "")
        if (root.tag != "GranuleMetaDataFile" or g.findtext("LocalGranuleID") != ref.filename
                or collection.findtext("ShortName") != ref.short or collection.findtext("VersionID") not in ("61", "061")
                or file.findtext("DistributedFileName") != ref.filename or file.findtext("ChecksumType", "").upper() != "MD5"
                or not DECIMAL.fullmatch(size) or not 64 < int(size) <= MAX_HDF or not MD5.fullmatch(md5)):
            raise ValueError
        return {"hdf_name": ref.filename, "hdf_bytes": int(size), "hdf_md5": md5.lower(), "nasa_period": period}
    except (ET.ParseError, AttributeError, TypeError, ValueError, OverflowError):
        raise UserError("NASA 配套 XML 的产品号、版本、日期或 HDF 完整校验值不符，未保存。") from None


def pin_modis(http, item, accesses, cancel, *, tag=None):
    ref, alias = check_modis_target(item)
    result = _head(http, ref, alias, cancel, accesses[modis_scope(alias)], expected_size=item["bytes"], expected_tag=tag)
    if alias != "hdf" or item["identity"]["nasa_xml_url"] is None:
        return result
    access = accesses["modis-source"]
    xml = _head(http, ref, "metadata", cancel, access)
    headers = {"If-Match": xml["etag"], "Accept-Encoding": "identity"}
    try:
        with modis_blob_request(http, "GET", object_url(ref, "metadata"), headers, cancel, access) as response:
            if (response.status_code != 200 or response.headers.get("ETag") != xml["etag"]
                    or response.headers.get("Content-Length") != str(xml["size"]) or not modis_content_type(response.headers, "metadata")
                    or _full_md5(response.headers) != xml["md5"]):
                raise ValueError
            data = bytearray()
            for block in response.iter_content(65536):
                check_cancel(cancel)
                data.extend(block)
                if len(data) > xml["size"]:
                    raise ValueError
            if len(data) != xml["size"] or hashlib.md5(data, usedforsecurity=False).hexdigest() != xml["md5"]:
                raise ValueError
    except (ValueError, TypeError, requests.RequestException):
        raise UserError("MODIS 配套 XML 的版本、长度或源 MD5 不符，未保存对应 HDF。") from None
    parsed = verify_xml(data, ref)
    if parsed["hdf_md5"] != result["md5"] or parsed["hdf_bytes"] != result["size"]:
        raise UserError("NASA XML 与所选 HDF 的长度或 MD5 不一致，拒绝发布。")
    result["nasa_xml"] = {**parsed, "etag": xml["etag"], "sha256": hashlib.sha256(data).hexdigest()}
    return result


def verify_modis_file(path, item, pinned, cancel):
    ref, alias = check_modis_target(item)
    check_cancel(cancel)
    if alias == "hdf":
        with path.open("rb") as handle:
            if handle.read(4) != HDF4 or path.stat().st_size <= 64:
                raise UserError("所选 MODIS 主文件不是 HDF4，未保存。")
        return {"container": "HDF4", "nasa_xml_verified": pinned.get("nasa_xml"),
                "format_validation": "HDF4 magic and full Azure Content-MD5" + (" + NASA XML size/MD5" if "nasa_xml" in pinned else " (no XML in catalogue); not an SDS decode")}
    if alias == "metadata":
        return {"container": "XML", **verify_xml(path.read_bytes(), ref), "format_validation": "NASA granule XML syntax and exact HDF granule identity"}
    spec, expected = LAYERS[ref.family][alias], item["raster"]
    try:
        with rasterio.Env(GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR", GDAL_PAM_ENABLED="NO"), rasterio.open(path) as ds:
            summary = ds.tags()
            band = ds.tags(1)
            desc = ds.descriptions[0] or ""
            long_name = band.get("long_name", summary.get("long_name", ""))
            def matches(label):
                return (label.endswith(spec.description) if ref.family == "15A2H" else label == spec.description)
            if (ds.driver != "GTiff" or ds.count != 1 or not _sinu(ds.crs) or ds.tags(ns="IMAGE_STRUCTURE").get("LAYOUT") != "COG"
                    or not _grid(ref, ds.shape, tuple(ds.transform)[:6], expected["transform"]) or ds.dtypes[0] != spec.dtype
                    or (ds.nodata, ds.scales[0], ds.offsets[0]) != (spec.nodata, spec.stored_gain, spec.offset)
                    or not matches(desc) or not matches(long_name) or summary.get("LOCALGRANULEID") != ref.filename
                    or summary.get("VERSIONID") not in ("61", "061") or summary.get("SHORTNAME") != ref.short
                    or int(summary["HORIZONTALTILENUMBER"]) != ref.h or int(summary["VERTICALTILENUMBER"]) != ref.v):
                raise ValueError
            period = _period(ref, _day(summary["RANGEBEGINNINGDATE"]), _day(summary["RANGEENDINGDATE"]), "cog")
            n = 0
            for _, window in ds.block_windows(1):
                check_cancel(cancel)
                n += ds.read(1, window=window, masked=False).size
            if n != ds.width * ds.height:
                raise ValueError
    except (AttributeError, IndexError, KeyError, TypeError, ValueError, OverflowError, rasterio.errors.RasterioError):
        raise UserError("MODIS COG 的产品、图层、观测窗口、正弦网格、缩放或像元解码校验失败。") from None
    return {"container": "COG", "embedded_observation_period": period, "decoded_cells": n,
            "format_validation": "all base raster blocks, exact SDS/layer description, COG layout, MODLAND CRS/grid and acquisition metadata"}


def verify_modis_transfer(response, alias, size, tag, md5):
    """206 may supply range MD5; only x-ms-blob-content-md5 identifies the full blob."""
    try:
        headers = response.headers
        if not isinstance(tag, str) or not TAG.fullmatch(tag) or not isinstance(md5, str) or not MD5.fullmatch(md5):
            raise ValueError
        if not modis_content_type(headers, alias) or headers.get("ETag") != tag:
            raise ValueError
        if response.status_code == 200:
            if headers.get("Content-Length") != str(size) or _full_md5(headers) != md5:
                raise ValueError
        elif "x-ms-blob-content-md5" in headers and _full_md5({"Content-MD5": headers["x-ms-blob-content-md5"]}) != md5:
            raise ValueError
    except (TypeError, ValueError):
        raise UserError("MODIS 传输的对象版本、类型、长度或源 MD5 与预检不符，未保存。") from None
