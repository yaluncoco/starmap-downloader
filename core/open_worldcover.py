"""Official ESA WorldCover annual composites and classified maps, without credentials."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
from bisect import bisect_left
from concurrent.futures import ThreadPoolExecutor, as_completed
from functools import lru_cache
from pathlib import Path
from xml.etree import ElementTree as ET

import requests
from shapely.geometry import box, mapping
from shapely.strtree import STRtree

from .catalogue import date_range
from .errors import UserError
from .geometry import area_km2
from .http import HttpClient, check_cancel
from .s3_source import WC_AWS_SUFFIX, WC_VERSIONS, worldcover_object, worldcover_prefix, worldcover_tile, worldcover_url

S2_ANNUAL = "worldcover-s2"
S1_ANNUAL = "worldcover-s1"
LAND_COVER = "worldcover-map"
PROFILE_LAYERS = {S2_ANNUAL: ("S2RGBNIR", "NDVI", "SWIR"), S1_ANNUAL: ("S1VVVHratio",), LAND_COVER: ("Map",)}
PROVIDER = "ESA WorldCover / AWS Open Data (CC BY 4.0)"
CREDIT = "ESA WorldCover project; contains modified Copernicus Sentinel data processed by the ESA WorldCover consortium"
PROOF_KIND = "worldcover-annual"
INDEX_SHA256 = "562c859911552aacf9f0b65eb809ef746f3192bcee554db3788052d27d85d0b5"
# Bit order of the distributed index; see scripts/make_worldcover_grid.py.
COMPOSITE_LAYERS = (*PROFILE_LAYERS[S2_ANNUAL], *PROFILE_LAYERS[S1_ANNUAL])
MAX_SCENES, MAX_GROUPS, WORKERS = 300, 96, 5
MAX_OBJECT = 4 * 1024**3
MAX_XML = 1024**2
NS = "{http://s3.amazonaws.com/doc/2006-03-01/}"
ETAG = re.compile(r'"([0-9a-f]{32})(?:-([1-9][0-9]{0,4}))?"', re.ASCII)
DECIMAL = re.compile(r"[1-9][0-9]{0,10}", re.ASCII)
TIFF_TYPES = ("image/tiff", "application/octet-stream", "binary/octet-stream")
# dtype, band count, NoData, gain, offset, pixels per degree, product_type, published order
LAYOUTS = {
    "S2RGBNIR": ("uint16", 4, 0, .0001, 0, 12000, "Sentinel-2 median L2A (RGBNIR) composite",
                "Band 1: B04 (Red), Band 2: B03 (Green), Band 3: B02 (Blue), Band 4: B08 (Infrared)"),
    "NDVI": ("uint8", 3, 255, .008, -1, 12000, "Sentinel-2 NDVI (NDVI-p90, NDVI-p50, NDVI-p10) composite",
             "Band 1: NDVI-p90, Band2: NDVI-p50, Band 3: NDVI-p10"),
    "SWIR": ("uint8", 2, 255, .004, 0, 6000, "Sentinel-2 SWIR (B11 p50, B12 p50) composite",
             "Band 1: B11-p50, Band 2: B12-p50"),
    "S1VVVHratio": ("uint16", 3, 0, .001, -45, 12000, "Sentinel-1 median backscatter (VV, VH, VH/VV) composite",
                    "Band 1: VV, Band 2: VH, Band 3: VH/VV ratio"),
    "Map": ("uint8", 1, 0, 1, 0, 12000, "LandCover Map", None),
}
LABELS = {"S2RGBNIR": "RGBNIR · B04 / B03 / B02 / B08", "NDVI": "NDVI · p90 / p50 / p10",
          "SWIR": "SWIR · B11 / B12 中位数（约 20 m）", "S1VVVHratio": "Gamma0 · VV / VH / VH/VV",
          "Map": "土地覆盖分类 · 11 类"}


def _interval(year):
    return f"{year:04d}-01-01T00:00:00Z", f"{year:04d}-12-31T23:59:59Z"


def _version_tag(year):
    return "V1.0.0" if year == 2020 else "V2.0.0"


def _expected_raster(year, tile, layer):
    dtype, count, fill, scale, offset, ppd, kind, band_order = LAYOUTS[layer]
    side = 3 if layer == "Map" else 1
    w, _, _, n = worldcover_tile(tile, side)
    version = _version_tag(year)
    first, last = _interval(year)
    tags = {"product_tile": tile, "product_type": kind, "product_version": version, "algorithm_version": version,
            "product_grid": f"{side}x{side} degree tiling grid", "time_start": first, "time_end": last}
    if band_order:
        tags["bands"] = band_order
    return {"epsg": 4326, "shape": [ppd*side, ppd*side], "transform": [1/ppd, 0, w, 0, -1/ppd, n],
            "dtype": dtype, "nodata": fill, "scale": scale, "offset": offset, "count": count, "layout": "COG",
            "tags": tags, **({"colorinterp": ["palette"]} if layer == "Map" else {})}


@lru_cache(maxsize=2)
def _grid(is_map):
    try:
        data = (Path(__file__).resolve().parent / "data" / "worldcover-grid.json").read_bytes()
        if hashlib.sha256(data).hexdigest() != INDEX_SHA256:
            raise ValueError
        doc = json.loads(data)
        rows = doc["map" if is_map else "composite"]
        geoms = tuple(box(*worldcover_tile(tile, 3 if is_map else 1)) for tile, _ in rows)
        return rows, geoms, STRtree(geoms), doc["source_sha256"]
    except (OSError, ValueError, TypeError, KeyError):
        raise UserError("ESA WorldCover 瓦片索引已损坏或缺失，请重新安装软件。") from None


def _int(value):
    return int(value) if isinstance(value, str) and DECIMAL.fullmatch(value) else None


def _tag(value):
    return ETAG.fullmatch(value) if isinstance(value, str) else None


def _list_group(group, wanted, cancel):
    bucket, prefix = group
    host = f"https://{bucket}{WC_AWS_SUFFIX}/"
    http = HttpClient()
    try:
        with http.request("GET", host, params={"list-type": "2", "prefix": prefix, "max-keys": "1000"},
                          stream=True, timeout=(10, 35), cancel=cancel, allow_redirects=False) as response:
            if response.status_code != 200:
                raise UserError(f"ESA WorldCover 官方对象目录暂不可用（HTTP {response.status_code}），请重试。")
            data = bytearray()
            for block in response.iter_content(64 * 1024):
                check_cancel(cancel)
                data.extend(block)
                if len(data) > MAX_XML:
                    raise ValueError
        if b"<!DOCTYPE" in data or b"<!ENTITY" in data:
            raise ValueError
        doc = ET.fromstring(data)
        contents = doc.findall(NS + "Contents")
        if (doc.tag != NS + "ListBucketResult" or doc.findtext(NS + "Name") != bucket or doc.findtext(NS + "Prefix") != prefix
                or doc.findtext(NS + "IsTruncated") != "false" or len(contents) > 1000
                or doc.findtext(NS + "KeyCount") != str(len(contents))):
            raise ValueError
        found = {}
        for entry in contents:
            key = entry.findtext(NS + "Key")
            if not key or not key.startswith(prefix):
                raise ValueError
            if key not in wanted:
                continue
            size, tag = _int(entry.findtext(NS + "Size")), entry.findtext(NS + "ETag")
            if key in found or not size or size > MAX_OBJECT or (parsed := _tag(tag)) is None:
                raise ValueError
            if parsed[2] is not None and int(parsed[2]) > 10000:
                raise ValueError
            found[key] = (size, tag)
        return {host + key: value for key, value in found.items()}
    except (ET.ParseError, ValueError, requests.RequestException):
        raise UserError("ESA WorldCover 对象目录不完整或身份不符；没有返回可能错误的瓦片，请重试。") from None
    finally:
        http.close()


def search_worldcover(geometry, profile, start, end, *, cancel=None, progress=None):
    start, stop = date_range(start, end, dt.date(1900, 1, 1))
    years = [year for year in WC_VERSIONS if dt.date(year, 1, 1) < stop and start < dt.date(year + 1, 1, 1)]
    if not years:
        return []
    check_cancel(cancel)
    is_map = profile == LAND_COVER
    rows, cells, tree, source_hashes = _grid(is_map)
    layers = PROFILE_LAYERS[profile]
    groups, candidates = {}, []
    area = area_km2(geometry)
    for idx in sorted(tree.query(geometry, predicate="intersects")):
        check_cancel(cancel)
        cell = cells[idx]
        overlap = geometry.intersection(cell)
        if overlap.is_empty or overlap.area <= 0:
            continue  # Touching only, including a tile entirely inside an AOI hole.
        cover = round(min(100, 100 * area_km2(overlap) / area), 2)
        tile, mask = rows[idx]
        for year in years:
            candidates.append((year, tile, cell, cover, mask))
            if len(candidates) > MAX_SCENES:
                raise UserError(f"ESA WorldCover 研究区跨越超过 {MAX_SCENES} 个年度瓦片；请缩小范围或分批检索。")
            for layer in layers:
                if _advertised(year, layer, mask):
                    url = worldcover_url(year, tile, layer)
                    group = worldcover_prefix(year, tile, layer)
                    groups.setdefault(group, set()).add(url.split("/", 3)[3])
                    if len(groups) > MAX_GROUPS:
                        raise UserError("ESA WorldCover 研究区跨越过多纬度带；请缩小研究区或按年分批检索。")
    if progress:
        progress(f"核对 {len(candidates)} 个 ESA WorldCover 年度瓦片与公开对象目录", .05, None)
    live = {}
    if groups:
        pool = ThreadPoolExecutor(max_workers=WORKERS)
        try:
            futures = [pool.submit(_list_group, group, keys, cancel) for group, keys in groups.items()]
            for i, future in enumerate(as_completed(futures), 1):
                check_cancel(cancel)
                live.update(future.result())
                if progress:
                    progress(f"核对 ESA WorldCover 目录 {i}/{len(groups)}", .05 + .9*i/len(groups), None)
        finally:
            pool.shutdown(cancel_futures=True)
    results = []
    for year, tile, cell, cover, mask in sorted(candidates, key=lambda row: (row[0], row[1]), reverse=True):
        check_cancel(cancel)
        key = f"ESA_WorldCover_10m_{year}_{WC_VERSIONS[year]}_{tile}"
        first, last = _interval(year)
        assets, missing = [], []
        grid_sha = source_hashes[f"map{year}" if is_map else "composite"]
        for layer in layers:
            url = worldcover_url(year, tile, layer)
            if url not in live:
                reason = "公开对象目录无此文件" if _advertised(year, layer, mask) else "官方网格未发布此层"
                missing.append(f"{layer}：{reason}")
                continue
            size, tag = live[url]
            assets.append({"id": layer, "name": f"{key}_{layer}.tif", "title": LABELS[layer], "type": "image/tiff", "roles": ["data"],
                           "bytes": size, "public_url": url, "raster": _expected_raster(year, tile, layer),
                           "identity": {"kind": PROOF_KIND, "year": year, "tile": tile, "layer": layer, "archive_etag": tag,
                                        "grid_sha256": grid_sha, "version": WC_VERSIONS[year], "attribution": CREDIT}})
        results.append({"key": key, "title": key, "date": str(year), "datetime": first, "start_utc": first, "end_utc": last,
                        "platform": "Sentinel-1" if profile == S1_ANNUAL else "Sentinel-2" if profile == S2_ANNUAL else "ESA WorldCover",
                        "tiles": [tile], "tile_count": 1, "coverage_percent": cover, "cloud_min": None, "cloud_max": None,
                        "time": f"年度 · {WC_VERSIONS[year]}", "products": [{"id": key, "name": key, "footprint": mapping(cell)}],
                        "raw": {"uuid": None, "names": [key], "assets": assets, "public_provider": PROVIDER,
                                "available": bool(assets), "unavailable_reason": "；".join(missing) if not assets else None,
                                "source_note": "；".join(missing) if assets and missing else "",
                                "primary_public": assets[0]["id"] if assets and profile != S2_ANNUAL else None}})
    return results


def _advertised(year, layer, mask):
    i = year - 2020
    return bool(mask & (1 << (i if layer == "Map" else i*len(COMPOSITE_LAYERS) + COMPOSITE_LAYERS.index(layer))))


def check_worldcover_target(item):
    data = worldcover_object(item["url"])
    proof = item.get("identity") or {}
    if data is not None:
        year, tile, layer = data
        key = f"ESA_WorldCover_10m_{year}_{WC_VERSIONS[year]}_{tile}"
        rows, _, _, source_hashes = _grid(layer == "Map")
        ix = bisect_left(rows, tile, key=lambda row: row[0])
        sha = source_hashes[f"map{year}" if layer == "Map" else "composite"]
        tag = _tag(proof.get("archive_etag"))
        if (ix < len(rows) and rows[ix][0] == tile and _advertised(year, layer, rows[ix][1]) and item["transport"] == "public" and item["key"] == key and item["name"] == f"{key}_{layer}.tif"
                and item["asset"] == layer and type(item.get("bytes")) is int and 0 < item["bytes"] <= MAX_OBJECT
                and item.get("raster") == _expected_raster(year, tile, layer) and not item.get("uuid")
                and proof.get("kind") == PROOF_KIND and (proof.get("year"), proof.get("tile"), proof.get("layer")) == data
                and proof.get("version") == WC_VERSIONS[year] and proof.get("grid_sha256") == sha
                and proof.get("attribution") == CREDIT and tag and (tag[2] is None or 1 <= int(tag[2]) <= 10000)):
            return proof
    raise UserError("ESA WorldCover 目标与所选来源、年份、版本、瓦片或图层不符；请重新检索。")


def check_worldcover_headers(headers):
    # Plaintext and SSE-S3 are supported. A KMS / SSE-C ETag is not a content checksum.
    return (headers.get("Content-Type", "").split(";", 1)[0].strip().lower() in TIFF_TYPES
            and headers.get("Content-Encoding", "identity").lower() == "identity"
            and headers.get("x-amz-server-side-encryption", "") in ("", "AES256")
            and not headers.get("x-amz-server-side-encryption-customer-algorithm"))


def pin_worldcover(http, item, cancel):
    proof = check_worldcover_target(item)
    tag, size = proof["archive_etag"], item["bytes"]
    count = _tag(tag)[2]
    headers = {"Accept-Encoding": "identity", "If-Match": tag}

    def request(params, status):
        with http.request("HEAD", item["url"], params=params, headers=headers, stream=True, timeout=(10, 30),
                          cancel=cancel, allow_redirects=False) as response:
            if response.status_code in (403, 404, 410):
                raise UserError("ESA WorldCover 没有提供所选精确对象；不会换用其他年份、瓦片或图层。请重新检索。")
            if (response.status_code != status or response.headers.get("ETag") != tag or not check_worldcover_headers(response.headers)):
                raise UserError("ESA WorldCover 对象已变化或源校验不可用；请重新检索，未保存文件。")
            return response.headers

    if request(None, 200).get("Content-Length") != str(size):
        raise UserError("ESA WorldCover 对象长度与目录不同；请重新检索，未保存文件。")
    if count is None:
        return size
    first = request({"partNumber": 1}, 206)
    part = _int(first.get("Content-Length"))
    if (not part or part > size or first.get("Content-Range") != f"bytes 0-{part-1}/{size}"
            or first.get("x-amz-mp-parts-count") != count or (size+part-1)//part != int(count)):
        raise UserError("ESA WorldCover 对象的上传分段信息无法核对；未保存文件。")
    return part


def verify_worldcover_bytes(path, item, part_bytes, cancel):
    tag = _tag(item["identity"]["archive_etag"])
    size = item["bytes"]
    sha, combined = hashlib.sha256(), hashlib.md5(usedforsecurity=False)
    if not 0 < part_bytes <= size or (size+part_bytes-1)//part_bytes != int(tag[2] or 1):
        raise UserError("ESA WorldCover 对象的分段和长度不匹配，未保存文件。")
    with path.open("rb") as fp:
        for start in range(0, size, part_bytes):
            part = hashlib.md5(usedforsecurity=False)
            remaining = min(size-start, part_bytes)
            while remaining:
                check_cancel(cancel)
                block = fp.read(min(1024**2, remaining))
                if not block:
                    raise UserError("ESA WorldCover 文件长度不符，无法通过源校验。")
                remaining -= len(block)
                part.update(block)
                sha.update(block)
            combined.update(part.digest())
        if fp.read(1):
            raise UserError("ESA WorldCover 文件长度不符，无法通过源校验。")
    # Only accept the inferred equal-sized boundaries when the source tag actually matches.
    checksum = combined.hexdigest() if tag[2] else part.hexdigest()
    if checksum != tag[1]:
        raise UserError("ESA WorldCover 文件字节与源 ETag 校验不符；未使用该文件。若为已有文件，请移开它与同名校验记录后重试。")
    return sha.hexdigest(), "AWS multipart ETag (MD5 of part MD5s)" if tag[2] else "AWS single-part ETag (MD5)"
