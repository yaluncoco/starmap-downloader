"""Search ASF geometry; offer only exact IW SLC ZIPs present in the historical public mirror."""
from __future__ import annotations

import datetime as dt
import json
import re
from dataclasses import dataclass

from shapely.errors import GEOSException
from shapely.geometry import mapping, shape

from .errors import UserError
from .geometry import area_km2
from .http import HttpClient, check_cancel
from .scene_identity import utc_text, utc_time

S1_SLC = "s1-slc-seasia"
SLC_COLLECTION = "sentinel-1-iw-slc-seasia-v1.1"
SLC_PROOF = "s1-iw-slc-asf-zip"
ASF_SEARCH = "https://api.daac.asf.alaska.edu/services/search/param"
SLC_ROOT = "https://sentinel1-slc-seasia-pds.s3.ap-southeast-1.amazonaws.com/datasets/slc/v1.1/"
SLC_PROVIDER = "ASF DAAC · Sentinel-1 IW SLC Southeast Asia v1.1"
SLC_FIRST = dt.date(2014, 10, 6)
SLC_LAST = dt.date(2024, 4, 4)
MAX_ZIP = 24 * 1024**3
MAX_FEATURES = 150
_SLC = re.compile(
    r"S1([AB])_IW_SLC__1S(SV|DV|SH|DH)_(\d{8}T\d{6})_(\d{8}T\d{6})_(\d{6})_([0-9A-F]{6})_[0-9A-F]{4}", re.ASCII
)
_ETAG = re.compile(r'"[0-9a-f]{32}(?:-[1-9][0-9]{0,4})?"', re.ASCII)
_MD5 = re.compile(r"[0-9a-f]{32}", re.ASCII)
_POL = {"SV": ("VV",), "DV": ("VV", "VH"), "SH": ("HH",), "DH": ("HH", "HV")}


@dataclass(frozen=True)
class SlcRef:
    product: str
    satellite: str
    polarisations: tuple
    start: dt.datetime
    end: dt.datetime
    orbit: int
    datatake: str

    @property
    def url(self):
        return f"{SLC_ROOT}{self.start:%Y/%m/%d}/{self.product}/{self.product}.zip"


def slc_ref(product):
    m = _SLC.fullmatch(product) if isinstance(product, str) else None
    if not m:
        raise ValueError("IW SLC product identifier")
    satellite, pol, start, end, orbit, datatake = m.groups()
    a, b = [dt.datetime.strptime(t, "%Y%m%dT%H%M%S").replace(tzinfo=dt.timezone.utc) for t in (start, end)]
    if a > b or not SLC_FIRST <= a.date() <= SLC_LAST:
        raise ValueError("archive date")
    return SlcRef(product, satellite, _POL[pol], a, b, int(orbit), datatake)


def _size(value):
    return type(value) is int and 0 < value <= MAX_ZIP


def _etag(value):
    return isinstance(value, str) and bool(_ETAG.fullmatch(value))


def _md5(value):
    return isinstance(value, str) and bool(_MD5.fullmatch(value))


def zip_type(headers):
    return (headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
            in ("application/zip", "application/octet-stream", "binary/octet-stream", "application/x-zip-compressed")
            and headers.get("Content-Encoding", "identity").lower() == "identity")


def is_slc(item):
    url, proof = item.get("url"), item.get("identity") or {}
    return (isinstance(url, str) and url.startswith(SLC_ROOT)) or proof.get("kind") == SLC_PROOF


def check_slc_target(item):
    try:
        ref = slc_ref(item["key"])
        proof = item["identity"]
        if (item["transport"] == "public" and item["asset"] == "zip" and item["url"] == ref.url
                and item["name"] == ref.product + ".zip" and proof["kind"] == SLC_PROOF
                and proof["product"] == ref.product and _etag(proof["archive_etag"])
                and _size(item["bytes"]) and _md5(item["provider_md5"])):
            return ref
    except (KeyError, TypeError, ValueError):
        pass
    raise UserError("SLC ZIP 缺少可核对的精确产品、公开对象版本或 ASF 全文件 MD5，请重新检索。")


def _asf_feature(feature, geometry, start, stop):
    try:
        p = feature["properties"]
        if p.get("processingLevel") != "SLC" or p.get("beamModeType") != "IW":
            return None
        ref = slc_ref(p["sceneName"])
        if not start <= ref.start.date() < stop:
            return None
        if (p["fileName"] != ref.product + ".zip" or p["fileID"] != ref.product + "-SLC"
                or p["platform"] != "Sentinel-1" + ref.satellite or p["orbit"] != ref.orbit
                or set(p["polarization"].split("+")) != set(ref.polarisations)
                or utc_time(p["startTime"]).replace(microsecond=0) != ref.start
                or utc_time(p["stopTime"]).replace(microsecond=0) != ref.end
                or not _size(p["bytes"]) or not _md5(p["md5sum"])):
            return None
        footprint = shape(feature["geometry"])
        if (footprint.geom_type not in ("Polygon", "MultiPolygon") or not footprint.is_valid or footprint.is_empty
                or footprint.bounds[2] - footprint.bounds[0] > 12):
            return None
        west, south, east, north = footprint.bounds
        if not (-180 <= west < east <= 180 and -90 <= south < north <= 90):
            return None
        overlap = geometry.intersection(footprint)
        if overlap.is_empty or overlap.area <= 0:
            return None
        return ref, p["bytes"], p["md5sum"], footprint, overlap
    except (KeyError, ValueError, TypeError, AttributeError, GEOSException, OverflowError):
        return None


def _search_features(http, geometry, start, stop, cancel):
    # Use a small WKT envelope upstream, then intersect the true polygon (including holes) locally.
    params = {"platform": "Sentinel-1A,Sentinel-1B", "processingLevel": "SLC", "beamMode": "IW",
              "start": f"{start}T00:00:00Z", "end": f"{stop}T00:00:00Z", "intersectsWith": geometry.envelope.wkt,
              "output": "geojson", "maxResults": MAX_FEATURES + 1}
    response = http.request("GET", ASF_SEARCH, params=params, cancel=cancel, stream=True, timeout=(12, 40))
    try:
        if response.status_code != 200:
            raise UserError(f"ASF SLC 目录不可用（HTTP {response.status_code}）。")
        data = bytearray()
        for block in response.iter_content(64 * 1024):
            check_cancel(cancel)
            data.extend(block)
            if len(data) > 4 * 1024**2:
                raise UserError("ASF 返回的 SLC 目录超过大小限制，请缩小研究区或日期范围。")
        try:
            doc = json.loads(data)
            if doc["type"] != "FeatureCollection" or not isinstance(doc["features"], list):
                raise ValueError
        except (TypeError, KeyError, ValueError, UnicodeError):
            raise UserError("ASF 返回的 SLC 目录格式无效。") from None
        if len(doc["features"]) > MAX_FEATURES:
            raise UserError(f"ASF 目录命中超过 {MAX_FEATURES} 项，请缩小研究区或日期范围。")
        return doc["features"]
    finally:
        response.close()


def _mirror_tag(http, ref, size, cancel):
    response = http.request("HEAD", ref.url, headers={"Accept-Encoding": "identity"}, cancel=cancel, timeout=(12, 30))
    try:
        if response.status_code in (404, 410):
            return None  # Not mirrored at this exact name. Never substitute a different processing revision.
        if response.status_code != 200:
            raise UserError(f"SLC 公开存档无法核对对象（HTTP {response.status_code}）。")
        tag = response.headers.get("ETag")
        if not zip_type(response.headers) or response.headers.get("Content-Length") != str(size) or not _etag(tag):
            return None
        return tag
    finally:
        response.close()


def _area(geom):
    if geom.geom_type in ("Polygon", "MultiPolygon"):
        return area_km2(geom)
    return sum(_area(g) for g in geom.geoms) if geom.geom_type == "GeometryCollection" else 0.0


def search_slc(geometry, first, last, *, cancel=None, progress=None):
    start = max(dt.date.fromisoformat(first), SLC_FIRST)
    stop = min(dt.date.fromisoformat(last), SLC_LAST) + dt.timedelta(days=1)
    if stop <= start:
        return []
    if (stop - start).days > 62:
        raise UserError("SLC 历史存档每次最多检索 62 个 UTC 日，请分批查询。")
    if progress:
        progress("查询 ASF 的 IW SLC 原始产品", .08)
    http = HttpClient()
    try:
        candidates = {}
        for feature in _search_features(http, geometry, start, stop, cancel):
            check_cancel(cancel)
            candidate = _asf_feature(feature, geometry, start, stop)
            if candidate is None:
                continue
            ref, size, digest, footprint, _ = candidate
            existing = candidates.get(ref.product)
            if existing and (existing[1:3] != (size, digest) or not existing[3].equals(footprint)):
                raise UserError("ASF 目录为同一个 SLC 产品返回了不一致的版本信息；未加入下载列表。")
            candidates[ref.product] = candidate
        area = area_km2(geometry)
        rows = []
        for i, (ref, size, digest, footprint, overlap) in enumerate(candidates.values(), 1):
            tag = _mirror_tag(http, ref, size, cancel)
            if progress and (i % 4 == 0 or i == len(candidates)):
                progress(f"核对 SLC 存档中的精确 ZIP · {i}/{len(candidates)}", .15 + .84 * i / len(candidates))
            if tag is None:
                continue
            geo = mapping(footprint)
            asset = {"id": "zip", "name": ref.product + ".zip", "bytes": size, "public_url": ref.url, "roles": ["data"],
                     "provider_md5": digest, "identity": {"kind": SLC_PROOF, "product": ref.product, "archive_etag": tag}}
            rows.append({"key": ref.product, "title": ref.product, "platform": "Sentinel-1" + ref.satellite,
                         "datetime": utc_text(ref.start), "date": ref.start.date().isoformat(), "time": ref.start.strftime("%H:%M:%S"),
                         "tiles": ["IW · " + "+".join(ref.polarisations)], "tile_count": 1, "cloud_min": None, "cloud_max": None,
                         "coverage_percent": round(min(100.0, _area(overlap) / area * 100), 1),
                         "products": [{"id": ref.product, "footprint": geo}],
                         "raw": {"uuid": None, "names": [ref.product], "available": True, "collection": SLC_COLLECTION,
                                 "public_provider": SLC_PROVIDER, "primary_public": "zip", "assets": [asset]}})
        check_cancel(cancel)
        return sorted(rows, key=lambda row: (row["datetime"], row["key"]), reverse=True)
    finally:
        http.close()
