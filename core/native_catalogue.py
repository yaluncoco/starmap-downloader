"""Native CDSE STAC: all published collections, without needing download credentials."""
from __future__ import annotations

import datetime as dt
import re
from urllib.parse import unquote, urlparse
from uuid import UUID

from shapely.errors import GEOSException
from shapely.geometry import mapping, shape

from .catalogue import date_range
from .errors import UserError
from .geometry import area_km2
from .http import HttpClient, check_cancel
from .process_catalogue import _polygon_area
from .scene_identity import utc_text, utc_time
from .s3_source import ESSENTIAL_EXTENSIONS, cdse_object

STAC_ROOT = "https://stac.dataspace.copernicus.eu/v1"
DOWNLOAD_ROOT = "https://download.dataspace.copernicus.eu/odata/v1"
COLLECTION_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}\Z", re.ASCII)
PRODUCT_PATH = re.compile(r"/odata/v1/Products\(([a-fA-F0-9-]{36})\)(/(?:Nodes\([^/]+\)/)*\$value)?\Z", re.ASCII)
NODE = re.compile(r"/Nodes\(([^/]+)\)")


def collection_id(value):
    if not isinstance(value, str) or not COLLECTION_ID.fullmatch(value):
        raise UserError("请选择有效的 CDSE 目录类别。")
    return value


def _json(response, resource):
    try:
        if response.status_code != 200:
            raise UserError(f"{resource}暂时不可用（HTTP {response.status_code}）。")
        data = response.json()
        if not isinstance(data, dict):
            raise ValueError
        return data
    except ValueError:
        raise UserError(f"{resource}返回了无法识别的数据。") from None
    finally:
        response.close()


def _next(data, path):
    for link in data.get("links", []):
        if isinstance(link, dict) and link.get("rel") == "next":
            target = urlparse(link.get("href", ""))
            if target.scheme != "https" or target.netloc != "stac.dataspace.copernicus.eu" or target.path != path or target.fragment:
                raise UserError("官方目录返回了不受信任的分页地址。")
            return link
    return None


def collection_group(key):
    for prefix, label in (("sentinel-1", "Sentinel-1 雷达"), ("sentinel-2", "Sentinel-2 光学"),
                          ("sentinel-3", "Sentinel-3 海陆观测"), ("sentinel-5p", "Sentinel-5P 大气"),
                          ("sentinel-6", "Sentinel-6 高度计"), ("cop-dem", "Copernicus DEM"),
                          ("landsat", "Landsat"), ("opengeohub", "Landsat / 合成图"), ("modis", "MODIS"),
                          ("clms", "CLMS 土地监测"), ("ccm", "Copernicus 贡献任务"),
                          ("nisar", "NISAR"), ("era5", "ERA5 再分析"), ("s2glc", "地表覆盖")):
        if key.startswith(prefix):
            return label
    return "其他官方类别"


def list_collections():
    http = HttpClient()
    url = STAC_ROOT + "/collections"
    params = {"limit": 1000}
    output, seen = {}, set()
    try:
        while url:
            data = _json(http.request("GET", url, params=params, timeout=(10, 22)), "官方类别目录")
            params = None
            if not isinstance(data.get("collections"), list):
                raise UserError("官方类别目录未返回集合列表。")
            for value in data["collections"]:
                if not isinstance(value, dict):
                    continue
                key = value.get("id")
                if not isinstance(key, str) or not COLLECTION_ID.fullmatch(key):
                    continue
                output[key] = {"id": key, "title": str(value.get("title") or key)[:200], "group": collection_group(key),
                               "static": key.startswith(("cop-dem-", "s2glc"))}
            next_link = _next(data, "/v1/collections")
            url = next_link["href"] if next_link else None
            if url in seen or len(seen) > 120:
                raise UserError("官方类别目录分页未推进。")
            seen.add(url)
    finally:
        http.close()
    if not output:
        raise UserError("官方目录没有返回可选类别。")
    return sorted(output.values(), key=lambda value: (value["group"], value["title"], value["id"]))


def download_asset(href):
    """Resolve only a path on the official CDSE product/Nodes host; never arbitrary asset URLs."""
    if (not isinstance(href, str) or len(href) > 2048 or not href.startswith("https://")
            or any(ord(c) < 32 or ord(c) == 127 for c in href) or re.search(r"%(?![a-fA-F0-9]{2})", href)):
        return None
    try:
        p = urlparse(href)
        if p.netloc not in ("download.dataspace.copernicus.eu", "zipper.dataspace.copernicus.eu") or p.params or p.query or p.fragment:
            return None
        match = PRODUCT_PATH.fullmatch(p.path)
        if not match:
            return None
        key = str(UUID(match[1]))
        names = [unquote(v, errors="strict") for v in NODE.findall(p.path)]
    except (ValueError, UnicodeError):
        return None
    if any(not name or name in {".", ".."} or any(c in name for c in "/\\%")
           or any(ord(c) < 32 or ord(c) == 127 for c in name) for name in names):
        return None
    if names:
        return key, names[0], names[-1], "https://download.dataspace.copernicus.eu" + p.path
    return key, None, None, f"{DOWNLOAD_ROOT}/Products({key})/$value"


def product_stem(value):
    return re.sub(r"(?i)\.(zip|safe|sen3|nc|tif|tiff|jp2|json|xml|txt|hdf|hdf5|h5|h4)$", "", value).casefold()


def asset_details(alias, asset, filename):
    length, checksum = asset.get("file:size"), asset.get("file:checksum")
    roles = asset.get("roles")
    return {"id": alias, "name": filename, "type": str(asset.get("type") or "")[:100],
            "roles": [role for role in roles if isinstance(role, str)] if isinstance(roles, list) else [],
            "bytes": length if type(length) is int and length > 0 else None,
            "provider_sha256": checksum[4:].lower() if isinstance(checksum, str) and re.fullmatch(r"1220[0-9a-fA-F]{64}", checksum) else None,
            "provider_sha3_256": checksum[4:].lower() if isinstance(checksum, str) and re.fullmatch(r"1620[0-9a-fA-F]{64}", checksum) else None,
            "provider_md5": checksum[6:].lower() if isinstance(checksum, str) and re.fullmatch(r"d50110[0-9a-fA-F]{32}", checksum) else None}


def raw_item(feature, geometry, chosen, start=None, stop=None, *, radar_mode="", aoi_area=None):
    try:
        identifier = feature["id"]
        if not isinstance(identifier, str) or not 1 <= len(identifier) <= 300 or feature.get("collection") != chosen:
            return None
        region = shape(feature["geometry"])
        if not region.is_valid or region.geom_type not in ("Polygon", "MultiPolygon"):
            return None
        overlap = region.intersection(geometry)
        if overlap.is_empty or overlap.area <= 0:
            return None
        props = feature["properties"]
        date = utc_time(props.get("datetime") or props.get("start_datetime"))
        first = utc_time(props.get("start_datetime") or utc_text(date))
        last = utc_time(props.get("end_datetime") or utc_text(date))
        sar_mode = props.get("sar:instrument_mode") or props.get("s1:instrument_mode")
        if last < first or (radar_mode and sar_mode != radar_mode):
            return None
        if start is not None and (first.date() >= stop or last.date() < start):
            return None
    except (AttributeError, TypeError, ValueError, KeyError, GEOSException):
        return None
    asset_map = feature.get("assets")
    inputs = {}
    links = {}
    s3_items = {}
    if isinstance(asset_map, dict):
        for alias, asset in asset_map.items():
            if not isinstance(asset, dict) or not isinstance(alias, str) or not 1 <= len(alias) <= 150:
                continue
            inputs[alias] = asset
            alternate = asset.get("alternate")
            alt = alternate.get("https") if isinstance(alternate, dict) else None
            result = (download_asset(alt.get("href")) if isinstance(alt, dict) else None) or download_asset(asset.get("href"))
            if result:
                links[alias] = result
            s3_alt = alternate.get("s3") if isinstance(alternate, dict) else None
            channel = cdse_object(asset.get("href")) or (cdse_object(s3_alt.get("href")) if isinstance(s3_alt, dict) else None)
            # Bind the object to the product identifier, rather than treating STAC as an arbitrary URL list.
            if channel and any(product_stem(part) == product_stem(identifier) for part in channel[0]):
                s3_items[alias] = channel
    product_links = {pid for alias, (pid, _, filename, _) in links.items()
                     if not filename and (alias.casefold() == "product" or (
                         isinstance(inputs[alias].get("roles"), list) and "archive" in inputs[alias]["roles"]))}
    matching_nodes = {pid for pid, container, _, _ in links.values() if container and product_stem(container) == product_stem(identifier)}
    candidates = product_links | matching_nodes
    unique = next(iter(candidates)) if len(candidates) == 1 else None
    assets, names = [], {identifier}
    for alias, asset in inputs.items():
        odata = links.get(alias)
        if not odata or odata[0] != unique or not odata[2] or product_stem(odata[1]) != product_stem(identifier):
            odata = None
        s3 = s3_items.get(alias)
        if s3 and odata and s3[0][-1] != odata[2]:
            # Per-asset metadata must refer to the same filename on both routes.
            s3 = None
        if not odata and not s3:
            continue
        filename = odata[2] if odata else s3[0][-1]
        if not any(filename.lower().endswith(ext) for ext in ESSENTIAL_EXTENSIONS):
            continue
        if odata:
            names.add(odata[1])
        details = asset_details(alias, asset, filename)
        if odata:
            details.update(uuid=unique, url=odata[3])
        if s3:
            details.update(s3_url=s3[1])
        assets.append(details)
    data = [asset for asset in assets if "data" in asset["roles"]]
    # An original *single file* may use S3, but do not claim to have an archive for a directory product.
    primary_s3 = (data[0]["id"] if len(data) == 1 and data[0].get("s3_url") and product_stem(data[0]["name"]) == product_stem(identifier)
                  else None)
    try:
        cloud = float(props.get("eo:cloud_cover"))
        cloud = cloud if 0 <= cloud <= 100 else None
    except (ValueError, TypeError):
        cloud = None
    return {"key": identifier, "title": identifier, "platform": str(props.get("platform") or chosen)[:70], "datetime": utc_text(date),
            "date": date.date().isoformat(), "time": date.strftime("%H:%M:%S"),
            "start_utc": utc_text(first), "end_utc": utc_text(last), "tiles": [sar_mode] if sar_mode in ("IW", "EW", "SM", "WV") else [], "tile_count": 1,
            "cloud_min": cloud, "cloud_max": cloud, "coverage_percent": round(min(100, 100 * _polygon_area(overlap) / (aoi_area or area_km2(geometry))), 1),
            "products": [{"id": identifier, "name": identifier, "footprint": mapping(region)}],
            "raw": {"uuid": unique, "names": sorted(names), "assets": assets, "collection": chosen,
                    "available": unique is not None or bool(assets), "primary_s3": primary_s3}}


def search_original(geometry, collection, start, end, *, radar_mode="", cancel=None, progress=None, limit=500, row_parser=raw_item):
    collection = collection_id(collection)
    body = {"intersects": mapping(geometry), "collections": [collection], "limit": 100}
    if start is not None or end is not None:
        start, stop = date_range(start, end, dt.date(1900, 1, 1))
        body["datetime"] = f"{start.isoformat()}T00:00:00Z/{stop.isoformat()}T00:00:00Z"
    else:
        stop = None
    if radar_mode:
        body["filter-lang"] = "cql2-json"
        body["filter"] = {"op": "=", "args": [{"property": "sar:instrument_mode"}, radar_mode]}
    area = area_km2(geometry)
    url, method, params = STAC_ROOT + "/search", "POST", None
    http, by_id, seen = HttpClient(), {}, set()
    try:
        while url:
            check_cancel(cancel)
            data = _json(http.request(method, url, json=body, params=params, cancel=cancel, timeout=(12, 38)), "官方原始产品目录")
            if not isinstance(data.get("features"), list):
                raise UserError("官方目录检索没有返回产品列表。")
            for feature in data["features"]:
                check_cancel(cancel)
                row = row_parser(feature, geometry, collection, start, stop, radar_mode=radar_mode, aoi_area=area)
                if row:
                    current = by_id.get(row["key"])
                    if current is not None and current != row:
                        raise UserError("官方目录返回同一标识的不同产品记录，请重新检索。")
                    by_id[row["key"]] = row
            if len(by_id) > limit:
                raise UserError("原始产品超过单次检索上限，请缩小日期或区域。")
            if progress:
                progress(f"原始产品目录 · 已匹配 {len(by_id)} 个产品", min(.95, len(by_id)/limit))
            link = _next(data, "/v1/search")
            if not link:
                break
            if len(seen) >= 300:
                raise UserError("原始产品超过单次检索上限，请缩小日期或区域。")
            url = link["href"]
            method = str(link.get("method") or "GET").upper()
            if method == "GET":
                body, params = None, None
            elif method == "POST" and isinstance(link.get("body"), dict):
                body = ({**(body or {}), **link["body"]} if link.get("merge") else link["body"])
            else:
                raise UserError("官方目录分页请求格式不正确。")
            signature = (url, str(body))
            if signature in seen:
                raise UserError("官方原始产品目录分页未推进。")
            seen.add(signature)
    finally:
        http.close()
    return sorted(by_id.values(), key=lambda item: (item["datetime"], item["key"]), reverse=True)
