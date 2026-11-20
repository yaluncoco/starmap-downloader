"""Version-specific GEDI products and public NASA CMR granule search."""
from __future__ import annotations

import copy
import datetime as dt
import math
import re
from urllib.parse import unquote, urlparse

from shapely.geometry import MultiPolygon, Polygon, box, mapping

from .earthdata_auth import data_url
from .errors import UserError
from .http import HttpClient, check_cancel

GEDI = "gedi-earthdata"
DEFAULT_COLLECTION = "gedi02-a-003"
CMR_URL = "https://cmr.earthdata.nasa.gov/search/granules.umm_json"
BEAMS = ("BEAM0000", "BEAM0001", "BEAM0010", "BEAM0011", "BEAM0101", "BEAM0110", "BEAM1000", "BEAM1011")
CHECKSUMS = {"MD5": ("md5", 32), "SHA-1": ("sha1", 40), "SHA-256": ("sha256", 64), "SHA-512": ("sha512", 128)}


def _field(name, title, units="", *, paths=None, kind="scalar", index=None, default=False):
    return {"id": name, "title": title, "units": units, "paths": paths or [name],
            "kind": kind, "index": index, "default": default}


FIELDS = {
    "L1B": [
        _field("elevation_bin0", "接收窗口起点椭球高", "m", paths=["geolocation/elevation_bin0"], default=True),
        _field("elevation_lastbin", "接收窗口终点椭球高", "m", paths=["geolocation/elevation_lastbin"], default=True),
        _field("solar_elevation", "太阳高度角", "degree", paths=["geolocation/solar_elevation"], default=True),
        _field("rx_sample_count", "接收波形采样数", "samples", default=True),
        _field("rxwaveform", "完整接收波形（表格中的数组）", "DN", kind="waveform"),
        _field("txwaveform", "完整发射波形（表格中的数组）", "DN", kind="waveform"),
    ],
    "L2A": [
        *[_field(f"rh{n}", f"RH{n} · 相对高度", "m", paths=["rh"], index=n, default=n in (50, 95, 98, 100))
          for n in (25, 50, 75, 90, 95, 98, 100)],
        _field("elev_lowestmode", "最低模态椭球高", "m", default=True),
        _field("elev_highestreturn", "最高回波椭球高", "m"),
        _field("sensitivity", "波束穿透敏感度", "1", default=True),
        _field("rh", "完整 RH0–RH100（数组）", "m", kind="profile"),
        _field("geoid", "EGM2008 大地水准面高", "m", paths=["geophys_corr/geoid"]),
        _field("elev_lowestmode_egm2008", "最低模态 EGM2008 高程（椭球高 − geoid）", "m", kind="derived"),
    ],
    "L2B": [
        _field("cover", "冠层覆盖比例", "1", default=True),
        _field("pai", "植物面积指数 PAI", "m2/m2", default=True),
        _field("fhd_normal", "叶高多样性 FHD", "1", default=True),
        _field("sensitivity", "波束穿透敏感度", "1", default=True),
        _field("elev_lowestmode", "最低模态椭球高", "m", paths=["elev_lowestmode", "geolocation/elev_lowestmode"]),
        _field("cover_z", "冠层覆盖垂直剖面（数组）", "1", kind="profile"),
        _field("pai_z", "PAI 垂直剖面（数组）", "m2/m2", kind="profile"),
        _field("pavd_z", "植物面积体密度 PAVD（数组）", "m2/m3", kind="profile"),
    ],
    "L4A": [
        _field("agbd", "地上生物量密度 AGBD", "Mg/ha", default=True),
        _field("agbd_se", "AGBD 预测标准误", "Mg/ha", default=True),
        _field("agbd_pi_lower", "AGBD 预测区间下限", "Mg/ha", default=True),
        _field("agbd_pi_upper", "AGBD 预测区间上限", "Mg/ha", default=True),
        _field("sensitivity", "波束穿透敏感度", "1"),
        _field("elev_lowestmode", "最低模态椭球高", "m"),
    ],
    "L4C": [
        _field("wsci", "波形结构复杂度 WSCI", "1", default=True),
        _field("wsci_pi_lower", "WSCI 预测区间下限", "1", default=True),
        _field("wsci_pi_upper", "WSCI 预测区间上限", "1", default=True),
        _field("sensitivity", "波束穿透敏感度", "1"),
    ],
}

# These are exact official collections, not aliases for "latest". Reprocessed
# releases must remain explicit so a saved query can never silently change version.
_PRODUCTS = [
    ("gedi02-a-003", "L2A", "003", "GEDI02_A", "C3974616071-LPCLOUD", "冠层高度 / 地面高程", "2019-04-04", None),
    ("gedi02-b-003", "L2B", "003", "GEDI02_B", "C3974616135-LPCLOUD", "覆盖度 / PAI / 垂直结构", "2019-04-04", None),
    ("gedi04-a-003", "L4A", "3", "GEDI_L4A_AGB_Density_V3_2508", "C4212593885-ORNL_CLOUD", "足迹地上生物量密度", "2019-04-04", None),
    ("gedi04-c-003", "L4C", "3", "GEDI_L4C_WSCI_V3_2520", "C4302347663-ORNL_CLOUD", "足迹波形结构复杂度", "2019-04-04", None),
    ("gedi01-b-003", "L1B", "003", "GEDI01_B", "C3974616058-LPCLOUD", "定位与原始波形", "2019-04-04", None),
    ("gedi03-003", "L3", "3", "GEDI_L3_LandSurface_Metrics_V3_2525", "C4322280365-ORNL_CLOUD", "1 km 地表高度 / 观测数量栅格", "2019-04-18", "2025-07-09"),
    ("gedi02-a-002", "L2A", "002", "GEDI02_A", "C2142771958-LPCLOUD", "冠层高度 / 地面高程", "2019-04-04", "2025-07-10"),
    ("gedi02-b-002", "L2B", "002", "GEDI02_B", "C2142776747-LPCLOUD", "覆盖度 / PAI / 垂直结构", "2019-04-04", "2025-07-10"),
    ("gedi04-a-021", "L4A", "2.1", "GEDI_L4A_AGB_Density_V2_1_2056", "C2237824918-ORNL_CLOUD", "足迹地上生物量密度", "2019-04-17", "2025-07-09"),
    ("gedi04-c-002", "L4C", "2", "GEDI_L4C_WSCI_2338", "C3049900163-ORNL_CLOUD", "足迹波形结构复杂度", "2019-04-17", "2025-07-09"),
    ("gedi01-b-002", "L1B", "002", "GEDI01_B", "C2142749196-LPCLOUD", "定位与原始波形", "2019-04-04", "2025-07-10"),
    ("gedi03-002", "L3", "2", "GEDI_L3_LandSurface_Metrics_V2_1952", "C2153683336-ORNL_CLOUD", "1 km 地表高度 / 观测数量栅格", "2019-04-18", "2025-07-09"),
    ("gedi04-b-021", "L4B", "2.1", "GEDI_L4B_Gridded_Biomass_V2_1_2299", "C2792577683-ORNL_CLOUD", "1 km 生物量 / 不确定性栅格", "2019-04-18", "2023-03-16"),
]
COLLECTIONS = {}
for key, level, version, short_name, concept, title, first, last in _PRODUCTS:
    is_v3 = version in ("003", "3")
    quality = ({"L1B": ["stale_return_flag"], "L2A": ["l2a_quality_flag_rel3"],
                "L2B": ["l2b_quality_flag_rel3"], "L4A": ["l4a_quality_flag_rel3"],
                "L4C": ["l4c_quality_flag_rel3"]} if is_v3 else
               {"L1B": ["stale_return_flag"], "L2A": ["quality_flag"],
                "L2B": ["l2b_quality_flag"], "L4A": ["l4_quality_flag"], "L4C": ["wsci_quality_flag"]}).get(level, [])
    COLLECTIONS[key] = {"id": key, "level": level, "version": version, "short_name": short_name,
                        "concept_id": concept, "title": f"GEDI {level} · {title} · V{version}",
                        "group": "V003 · 当前再处理版本" if is_v3 else "V002 / V2.1 · 历史版本",
                        "kind": "raster" if level in ("L3", "L4B") else "footprint",
                        "first_date": first, "last_date": last, "fields": FIELDS.get(level, []),
                        "quality_paths": quality, "quality_value": 0 if level == "L1B" else 1}


def collection_id(value):
    key = value or DEFAULT_COLLECTION
    if not isinstance(key, str) or key not in COLLECTIONS:
        raise UserError("请选择支持的 GEDI 产品和明确的数据版本。")
    return key


def public_collections():
    return copy.deepcopy(list(COLLECTIONS.values()))


def _footprint(umm):
    geometry = umm.get("SpatialExtent", {}).get("HorizontalSpatialDomain", {}).get("Geometry", {})
    polygons = []
    try:
        for entry in geometry.get("GPolygons", []):
            points = entry.get("Boundary", {}).get("Points", [])
            coords = [(p["Longitude"], p["Latitude"]) for p in points]
            if len(coords) >= 3:
                polygon = Polygon(coords)
                # Do not draw a nearly-global polygon when a track crosses the dateline.
                if polygon.is_valid and not polygon.is_empty and polygon.bounds[2] - polygon.bounds[0] <= 180:
                    polygons.append(polygon)
        if polygons:
            return mapping(polygons[0] if len(polygons) == 1 else MultiPolygon(polygons))
        rectangles = geometry.get("BoundingRectangles", [])
        if len(rectangles) == 1:
            r = rectangles[0]
            if r["WestBoundingCoordinate"] < r["EastBoundingCoordinate"]:
                return mapping(box(r["WestBoundingCoordinate"], r["SouthBoundingCoordinate"],
                                   r["EastBoundingCoordinate"], r["NorthBoundingCoordinate"]))
    except (KeyError, TypeError, ValueError):
        pass
    return None


def granule_scene(item, key):
    spec = COLLECTIONS[key]
    umm, meta = item.get("umm", {}), item.get("meta", {})
    reference = umm.get("CollectionReference", {})
    if (reference.get("ShortName") != spec["short_name"] or str(reference.get("Version")) != spec["version"]
            or meta.get("collection-concept-id", spec["concept_id"]) != spec["concept_id"]):
        raise UserError("CMR 返回了其他 GEDI 产品或版本，请重新检索；未替换所选版本。")
    identifier, revision = meta.get("concept-id"), meta.get("revision-id")
    if not isinstance(identifier, str) or not re.fullmatch(r"G\d+-[A-Z0-9_]+", identifier) or not isinstance(revision, int) or revision < 1:
        raise UserError("CMR 缺少 GEDI 文件的明确标识和修订号。")
    suffixes = (".tif", ".tiff") if spec["kind"] == "raster" else (".h5", ".hdf5")
    urls = {}
    for link in umm.get("RelatedUrls", []):
        url = link.get("URL")
        if link.get("Type") != "GET DATA" or not isinstance(url, str):
            continue
        name = unquote(urlparse(url).path.rsplit("/", 1)[-1])
        if not name.lower().endswith(suffixes):
            continue
        data_url(url)
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,240}", name) or name.startswith("."):
            raise UserError("CMR 返回了无效的 GEDI 文件名。")
        urls.setdefault(name, url)
    if len(urls) > 1:
        raise UserError("GEDI 粒度包含多个主文件，请检查软件支持；未任意选择文件。")
    available = bool(urls)
    name, url = next(iter(urls.items())) if available else (str(umm.get("GranuleUR", identifier)), None)
    records = umm.get("DataGranule", {}).get("ArchiveAndDistributionInformation", [])
    record = next((r for r in records if r.get("Name") == name), records[0] if len(records) == 1 else {})
    size = record.get("SizeInBytes")
    if size is not None and (not isinstance(size, int) or not 0 < size <= 128 * 1024**3):
        raise UserError("CMR 返回了无效的 GEDI 文件长度。")
    estimate = size
    if not estimate and record.get("Size") is not None:
        units = {"B": 1, "KB": 1000, "MB": 1000**2, "GB": 1000**3}
        try:
            value = float(record["Size"]) * units[record.get("SizeUnit", "MB")]
            if math.isfinite(value) and value > 0:
                estimate = round(value)
        except (ValueError, TypeError, KeyError, OverflowError):
            pass
    checksum = record.get("Checksum")
    if checksum:
        algorithm, digest = checksum.get("Algorithm"), checksum.get("Value")
        if algorithm not in CHECKSUMS or not isinstance(digest, str) or not re.fullmatch(r"[0-9a-fA-F]{" + str(CHECKSUMS[algorithm][1]) + r"}", digest):
            raise UserError("CMR 返回了未支持或无效的 GEDI 官方校验值。")
        checksum = {"algorithm": algorithm, "value": digest.lower()}
    temporal = umm.get("TemporalExtent", {})
    interval = temporal.get("RangeDateTime", {})
    start = interval.get("BeginningDateTime") or temporal.get("SingleDateTime")
    end = interval.get("EndingDateTime") or start
    try:
        timestamp = dt.datetime.fromisoformat(start.replace("Z", "+00:00")).astimezone(dt.timezone.utc)
        dt.datetime.fromisoformat(end.replace("Z", "+00:00"))
    except (TypeError, ValueError, AttributeError):
        raise UserError("CMR 返回了无效的 GEDI 观测时间。") from None
    footprint = _footprint(umm)
    return {"key": identifier, "title": name, "platform": "GEDI / ISS", "datetime": start,
            "date": timestamp.date().isoformat(), "time": timestamp.strftime("%H:%M:%S"),
            "tiles": [spec["level"], "V" + spec["version"], "1 km 栅格" if spec["kind"] == "raster" else "激光足迹"],
            "tile_count": 1, "cloud_min": None, "cloud_max": None, "coverage_percent": None,
            "products": [{"id": identifier, "name": name, "footprint": footprint}] if footprint else [],
            "gedi": {"collection": key, "available": available, "name": name, "url": url,
                     "concept_id": identifier, "revision_id": revision, "size": size,
                     "estimated_bytes": estimate, "checksum": checksum, "start": start, "end": end,
                     "unavailable_reason": "目录未提供受支持的 HTTPS 主文件" if not available else ""}}


def search_gedi(geometry, key, start, end, *, cancel=None, progress=None):
    key = collection_id(key)
    if geometry.bounds[1] > 52 or geometry.bounds[3] < -52:
        if progress:
            progress("GEDI 足迹主要覆盖约 51.6°S–51.6°N，该区域在轨道观测范围之外。", 1.0)
        return []
    stop = (dt.date.fromisoformat(end) + dt.timedelta(days=1)).isoformat()
    params = {"collection_concept_id": COLLECTIONS[key]["concept_id"],
              "bounding_box": ",".join(format(c, ".10g") for c in geometry.bounds),
              "temporal": f"{start}T00:00:00Z,{stop}T00:00:00Z", "page_size": 200,
              "sort_key[]": ["start_date", "producer_granule_id"]}
    http, scenes, seen_tokens = HttpClient(), {}, set()
    headers, page = {}, 1
    try:
        while True:
            check_cancel(cancel)
            if progress:
                progress(f"检索 NASA CMR · {COLLECTIONS[key]['title']} · 已读取 {len(scenes)} 个文件", 0.05)
            response = http.request("GET", CMR_URL, params=params, headers=headers,
                                    timeout=(12, 45), cancel=cancel)
            try:
                if response.status_code != 200:
                    raise UserError(f"NASA CMR 检索失败（HTTP {response.status_code}），请缩小区域或日期后重试。")
                data = response.json()
                items = data.get("items") if isinstance(data, dict) else None
                if not isinstance(items, list):
                    raise UserError("NASA CMR 返回格式已变化，请检查软件更新。")
                token = response.headers.get("CMR-Search-After")
            except ValueError:
                raise UserError("NASA CMR 没有返回有效目录数据。") from None
            finally:
                response.close()
            for item in items:
                scene = granule_scene(item, key)
                scenes[scene["key"]] = scene
            if len(scenes) > 5000 or page > 30:
                raise UserError("GEDI 文件超过检索上限，请缩小日期范围或分区检索；结果未截断发布。")
            if len(items) < 200:
                break
            if token:
                if token in seen_tokens:
                    raise UserError("NASA CMR 分页重复，请稍后重新检索。")
                seen_tokens.add(token)
                headers = {"CMR-Search-After": token}
            else:
                params["page_num"] = page + 1
            page += 1
        if progress:
            progress(f"NASA CMR 检索完成 · {len(scenes)} 个文件；目录相交不等于有效足迹覆盖。", 1.0)
        return sorted(scenes.values(), key=lambda scene: (scene["datetime"], scene["key"]), reverse=True)
    finally:
        http.close()
