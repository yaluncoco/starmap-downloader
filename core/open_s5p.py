"""Public Sentinel-5P TROPOMI L2: CDSE footprints and digest, exact MEEO NetCDF objects."""
from __future__ import annotations

import datetime as dt
import math
import re
import xml.etree.ElementTree as ET

import requests

from .errors import UserError
from .http import HttpClient, check_cancel
from .native_catalogue import asset_details, raw_item, search_original
from .s3_source import S5P_ARCHIVE_HOST, S5P_NAME, s5p_archive_parts
from .scene_identity import utc_time

S5P_PUBLIC = "s5p-aws-l2"
PRODUCTS = {
    "no2": ("二氧化氮 NO₂", "L2__NO2___", "nitrogendioxide_tropospheric_column"),
    "ch4": ("甲烷 CH₄", "L2__CH4___", "methane_mixing_ratio"),
    "co": ("一氧化碳 CO", "L2__CO____", "carbonmonoxide_total_column"),
    "so2": ("二氧化硫 SO₂", "L2__SO2___", "sulfurdioxide_total_vertical_column"),
    "hcho": ("甲醛 HCHO", "L2__HCHO__", "formaldehyde_tropospheric_vertical_column"),
    "o3": ("臭氧总柱 O₃", "L2__O3____", "ozone_total_vertical_column"),
    "aer-ai": ("紫外气溶胶指数", "L2__AER_AI", "aerosol_index_354_388"),
    "aer-lh": ("气溶胶层高度", "L2__AER_LH", "aerosol_mid_height"),
    "cloud": ("云参数", "L2__CLOUD_", "cloud_fraction"),
}
STREAMS = {"offl": "OFFL · 离线", "nrti": "NRTI · 近实时", "rpro": "RPRO · 再处理"}
COLLECTIONS = {f"sentinel-5p-l2-{key}-{stream}": (stream.upper(), *p)
               for stream in STREAMS for key, p in PRODUCTS.items()}
DEFAULT_COLLECTION = "sentinel-5p-l2-no2-offl"
DIGEST_KEYS = ("provider_sha256", "provider_sha3_256", "provider_md5")
ARCHIVE = f"https://{S5P_ARCHIVE_HOST}/"
S3_XML = "{http://s3.amazonaws.com/doc/2006-03-01/}"
S5P_ETAG = re.compile(r'"[a-fA-F0-9]{32}(?:-[0-9]{1,5})?"', re.ASCII)
ARCHIVE_PAGE_LIMIT = 2 * 1024**2


def public_collections():
    return [{"id": key, "title": title, "group": STREAMS[stream.lower()]}
            for key, (stream, title, _, _) in COLLECTIONS.items()]


def s5p_collection(value):
    if not isinstance(value, str) or value not in COLLECTIONS:
        raise UserError("请选择受支持的 Sentinel-5P 大气变量和处理流。")
    return value


def _shape(value):
    return (isinstance(value, (list, tuple)) and len(value) == 2
            and all(type(n) is int and n > 0 for n in value) and value[0] <= 10000 and value[1] <= 1000)


def s5p_row(feature, geometry, chosen, start, stop, *, radar_mode="", aoi_area=None):
    base = raw_item(feature, geometry, chosen, start, stop, aoi_area=aoi_area)
    if base is None:
        return None
    stream, _, kind, variable = COLLECTIONS[chosen]
    product = base["key"]
    name = product + ".nc"
    parts = s5p_archive_parts(name)
    if not parts or parts[:2] != (stream, kind):
        return None
    props = feature["properties"]
    orbit = int(S5P_NAME.fullmatch(product)[5])
    shape = props.get("proj:shape")
    if (props.get("platform") != "sentinel-5p" or props.get("product:type") != kind
            or props.get("sat:absolute_orbit") != orbit or shape is not None and not _shape(shape)
            or not props.get("start_datetime") or not props.get("end_datetime")):
        return None
    first, last = utc_time(base["start_utc"]), utc_time(base["end_utc"])
    if first >= last or (start is not None and last <= dt.datetime.combine(start, dt.time.min, dt.timezone.utc)):
        return None
    expected_cdse = "s3://eodata/Sentinel-5P/TROPOMI/" + "/".join(parts[1:])
    proof = {"kind": "s5p-swath", "product": product, "stream": stream, "orbit": orbit,
             "start_utc": base["start_utc"], "end_utc": base["end_utc"], "shape": shape, "variable": variable}
    candidates = []
    assets = feature.get("assets")
    if isinstance(assets, dict):
        for asset in assets.values():
            if not isinstance(asset, dict) or asset.get("type") not in ("application/netcdf", "application/x-netcdf"):
                continue
            if asset.get("href") != expected_cdse or asset.get("file:local_path", name) != name:
                continue
            detail = asset_details("netcdf", asset, name)
            if "data" in detail["roles"] and detail["bytes"] and any(detail[k] for k in DIGEST_KEYS):
                candidates.append(detail)
    files = [{**candidates[0], "public_url": ARCHIVE + "/".join(parts),
              "identity": proof}] if len(candidates) == 1 else []
    # Do not offer a different release, stream, or COG if an exact original cannot be bound.
    return {**base, "tiles": [f"{stream} · 轨道 {orbit}"], "platform": "Sentinel-5P",
            "raw": {"uuid": None, "names": [product, name], "assets": files, "collection": chosen,
                    "available": bool(files), "primary_public": "netcdf" if files else None,
                    "unavailable_reason": None if files else "官方目录缺少可核验的原件链接或校验值",
                    "public_provider": "CDSE STAC / MEEO Sentinel-5P on AWS"}}


def archive_objects(http, prefix, keys, cancel):
    """List one archive UTC day; trust only exact known keys with a strong object version."""
    token, seen, matches = None, set(), {}
    for _ in range(8):
        params = {"list-type": "2", "prefix": prefix, "max-keys": "1000"}
        if token is not None:
            params["continuation-token"] = token
        response = http.request("GET", ARCHIVE, params=params, cancel=cancel, stream=True, timeout=(10, 26))
        try:
            if response.status_code != 200:
                raise UserError(f"公开档案暂时无法核对文件是否存在（HTTP {response.status_code}），请重新检索。")
            payload = bytearray()
            for block in response.iter_content(64 * 1024):
                check_cancel(cancel)
                payload.extend(block)
                if len(payload) > ARCHIVE_PAGE_LIMIT:
                    raise ValueError
            if b"<!DOCTYPE" in payload.upper() or b"\x00" in payload:
                raise ValueError
            page = ET.fromstring(payload)
            trunc = page.findtext(S3_XML + "IsTruncated")
            if (page.tag != S3_XML + "ListBucketResult" or page.findtext(S3_XML + "Name") != "meeo-s5p"
                    or page.findtext(S3_XML + "Prefix") != prefix or trunc not in ("true", "false")):
                raise ValueError
            for node in page.findall(S3_XML + "Contents"):
                key = node.findtext(S3_XML + "Key")
                if key not in keys:
                    continue
                size, tag = node.findtext(S3_XML + "Size", ""), node.findtext(S3_XML + "ETag", "")
                candidate = (int(size), tag) if re.fullmatch(r"[1-9][0-9]{0,13}", size) and S5P_ETAG.fullmatch(tag) else (None, None)
                if key in matches and candidate != matches[key]:
                    raise ValueError
                matches[key] = candidate
            if trunc == "false" or len(matches) == len(keys):
                return matches
            token = page.findtext(S3_XML + "NextContinuationToken", "")
            if not 1 <= len(token) <= 4096 or any(ord(c) < 32 for c in token) or token in seen:
                raise ValueError
            seen.add(token)
        except (ET.ParseError, ValueError, requests.RequestException):
            raise UserError("公开档案索引不完整或无法校验，未把未知对象标成可下载。请重新检索。") from None
        finally:
            response.close()
    raise UserError("公开档案索引超过分页上限，请缩小日期范围。")


def search_s5p(geometry, collection, start, end, *, cancel=None, progress=None):
    def catalogue_progress(text, fraction):
        if progress:
            progress(text, fraction * .60)

    rows = search_original(geometry, s5p_collection(collection), start, end, cancel=cancel,
                           progress=catalogue_progress, row_parser=s5p_row)
    prefixes = {}
    for row in rows:
        raw = row["raw"]
        if raw["available"]:
            asset, = raw["assets"]
            key = asset["public_url"].removeprefix(ARCHIVE)
            prefixes.setdefault(key.rsplit("/", 1)[0] + "/", {})[key] = (raw, asset)
    if not prefixes:
        return rows
    http = HttpClient()
    try:
        for idx, (prefix, targets) in enumerate(prefixes.items(), 1):
            check_cancel(cancel)
            if progress:
                progress(f"核对公开档案 · 第 {idx}/{len(prefixes)} 天的原始文件", .60 + .38 * (idx-1)/len(prefixes))
            found = archive_objects(http, prefix, targets, cancel)
            for key, (raw, asset) in targets.items():
                length, tag = found.get(key, (None, None))
                if length == asset["bytes"] and tag:
                    asset["identity"]["archive_etag"] = tag
                else:
                    reason = "公开档案未列出该精确版本" if key not in found else "公开档案对象长度或版本标识无法核对"
                    raw.update(available=False, assets=[], primary_public=None, unavailable_reason=reason)
        return rows
    finally:
        http.close()


def verify_swath(path, proof, cancel=None):
    """After the whole-file source digest, verify swath identity and read every core array element."""
    import h5py
    import numpy as np

    def scalar(value):
        value = np.asarray(value)
        if value.size != 1:
            raise ValueError
        atom = value.reshape(-1)[0]
        return atom.item() if isinstance(atom, np.generic) else atom

    def text(value):
        v = scalar(value)
        if isinstance(v, bytes):
            v = v.decode("ascii")
        if not isinstance(v, str) or len(v) > 250:
            raise ValueError
        return v

    def local_object(parent, key, cls):
        if not isinstance(parent.get(key, getlink=True), h5py.HardLink):
            raise ValueError
        obj = parent[key]
        if not isinstance(obj, cls):
            raise ValueError
        return obj

    def numeric(group, name, kinds):
        dset = local_object(group, name, h5py.Dataset)
        if (dset.dtype.kind not in kinds or dset.is_virtual or dset.external
                or (dset.chunks and math.prod(dset.chunks) * dset.dtype.itemsize > 64 * 1024 * 1024)):
            raise ValueError
        plist = dset.id.get_create_plist()
        if any(plist.get_filter(i)[0] not in (1, 2, 3, 4, 5, 6) for i in range(plist.get_nfilters())):
            raise ValueError
        return dset

    def valid(arr, fill):
        return np.isfinite(arr) & (arr != fill) if fill is not None else np.isfinite(arr)

    check_cancel(cancel)
    try:
        with h5py.File(path, "r", rdcc_nbytes=16 * 1024 * 1024) as dataset:
            attrs = dataset.attrs
            start = text(attrs["time_coverage_start"])
            end = text(attrs["time_coverage_end"])
            orbit = scalar(attrs["orbit"])
            if (text(attrs["id"]).removesuffix(".nc") != proof["product"] or type(orbit) is not int or orbit != proof["orbit"]
                    or abs((utc_time(start) - utc_time(proof["start_utc"])).total_seconds()) > 1
                    or abs((utc_time(end) - utc_time(proof["end_utc"])).total_seconds()) > 1 or utc_time(start) >= utc_time(end)):
                raise UserError("NetCDF 内的产品编号、轨道或观测时段与目录不一致，未保存。")
            group = local_object(dataset, "PRODUCT", h5py.Group)
            names = ("latitude", "longitude", "qa_value", proof["variable"])
            fields = [numeric(group, key, kinds) for key, kinds in zip(names, ("f", "f", "ui", "fiu"), strict=True)]
            shape = fields[0].shape
            if (len(shape) != 3 or shape[0] != 1 or not _shape(shape[1:]) or any(d.shape != shape for d in fields)
                    or len({d.id for d in fields}) != len(fields)
                    or proof["shape"] is not None and tuple(proof["shape"]) != shape[1:]):
                raise UserError("NetCDF 观测条带或数据变量维度与目录不一致，未保存。")
            fills = [scalar(d.attrs["_FillValue"]) if "_FillValue" in d.attrs else None for d in fields]
            geolocated = qa_values = science_values = 0
            for offset in range(0, shape[1], 256):
                check_cancel(cancel)
                lat, lon, qa, science = (d[0, offset:offset + 256, :] for d in fields)
                lat_ok, lon_ok, qa_ok, science_ok = (valid(a, f) for a,f in zip((lat, lon, qa, science), fills, strict=True))
                if (np.any(lat_ok & (np.abs(lat) > 90.0001)) or np.any(lon_ok & (np.abs(lon) > 180.0001))):
                    raise UserError("NetCDF 条带经纬度超出物理范围，未保存。")
                geo = lat_ok & lon_ok
                geolocated += int(np.count_nonzero(geo))
                qa_values += int(np.count_nonzero(geo & qa_ok))
                science_values += int(np.count_nonzero(geo & science_ok))
            if not geolocated:
                raise UserError("NetCDF 条带缺少有效经纬度，未保存。")
            return {"product": proof["product"], "orbit": orbit, "stream": proof["stream"], "start_utc": start, "end_utc": end,
                    "shape": list(shape), "decoded_variables": list(names), "geolocated_pixels": geolocated,
                    "qa_non_fill_geolocated": qa_values, "science_non_fill_geolocated": science_values}
    except (KeyError, TypeError, ValueError, OSError, RuntimeError, OverflowError):
        raise UserError("无法完整解码 NetCDF 的观测条带位置、质量或主要变量，未保存。") from None
