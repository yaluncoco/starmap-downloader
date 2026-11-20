"""Search the same Sentinel Hub collection/index used by the Process API."""
from __future__ import annotations

import datetime as dt

from shapely.errors import GEOSException
from shapely.geometry import shape
from shapely.ops import unary_union

from .catalogue import date_range, month_chunks
from .errors import UserError
from .geometry import area_km2
from .http import SH_CATALOGUE_URL, HttpClient, check_cancel
from .scene_identity import SAFE_ID, utc_text, utc_time


def cloud_limit(value):
    try:
        value = float(value)
    except (ValueError, TypeError):
        raise UserError("云量应为 0～100%。") from None
    if not 0 <= value <= 100:
        raise UserError("云量应为 0～100%。")
    return value


def _polygon_area(geometry):
    if geometry.geom_type in ("Polygon", "MultiPolygon"):
        return area_km2(geometry)
    if geometry.geom_type == "GeometryCollection":
        return sum(_polygon_area(part) for part in geometry.geoms)
    return 0.0


def _product(feature, geometry, begin, stop, max_cloud):
    try:
        identifier = feature["id"]
        match = SAFE_ID.fullmatch(identifier)
        if not match:
            return None
        properties = feature["properties"]
        acquired = utc_time(properties["datetime"])
        if not begin <= acquired.date() < stop:
            return None
        cloud = properties.get("eo:cloud_cover")
        # Unknown cloud is only included when the user asks for an unfiltered catalogue.
        if cloud is None:
            if max_cloud < 100:
                return None
        else:
            cloud = float(cloud)
            if not 0 <= cloud <= max_cloud:
                return None
        footprint = shape(feature["geometry"])
        if footprint.geom_type not in ("Polygon", "MultiPolygon") or not footprint.is_valid:
            return None
        coverage = footprint.intersection(geometry)
        if coverage.is_empty or coverage.area <= 0:
            return None
    except (ValueError, TypeError, KeyError, AttributeError, GEOSException):
        return None
    return match.groups(), acquired, coverage, {"id": identifier, "name": identifier, "cloud": cloud,
                                               "footprint": feature["geometry"], "acquisition_utc": utc_text(acquired)}


def find_scenes(geometry, start, end, max_cloud, auth, *, cancel=None, progress=None):
    start_date, end_exclusive = date_range(start, end)
    max_cloud = cloud_limit(max_cloud)
    if not auth.connected:
        raise UserError("请先连接 API，以检索与下载服务一致的影像目录。")
    chunks = month_chunks(start_date, end_exclusive)
    groups = {}
    http = HttpClient()
    try:
        for chunk_index, (begin, stop) in enumerate(chunks):
            body = {"bbox": list(geometry.bounds), "datetime": f"{begin.isoformat()}T00:00:00Z/{stop.isoformat()}T00:00:00Z",
                    "collections": ["sentinel-2-l2a"], "limit": 100}
            fraction = chunk_index / len(chunks)
            seen_cursors = set()
            while True:
                check_cancel(cancel)
                if progress:
                    progress(f"检索 {begin.isoformat()} 至 {(stop - dt.timedelta(days=1)).isoformat()} · 已匹配 {len(groups)} 次过境", fraction)
                for force in (False, True):
                    headers = {"Authorization": "Bearer " + auth.token(cancel, force=force)}
                    response = http.request("POST", SH_CATALOGUE_URL, json=body, headers=headers, cancel=cancel,
                                            timeout=(15, 45), status=(lambda message: progress(message, fraction)) if progress else None)
                    try:
                        if response.status_code == 401 and not force:
                            continue
                        if response.status_code != 200:
                            raise UserError(f"Sentinel Hub 影像目录检索失败（HTTP {response.status_code}），请检查凭据、额度和日期范围。")
                        data = response.json()
                        break
                    except ValueError:
                        raise UserError("影像目录返回格式不正确，请稍后重试。") from None
                    finally:
                        response.close()
                if not isinstance(data, dict) or not isinstance(data.get("features"), list):
                    raise UserError("影像目录未返回有效的 STAC 清单。")
                for feature in data["features"]:
                    candidate = _product(feature, geometry, begin, stop, max_cloud)
                    if not candidate:
                        continue
                    (platform, datatake, tile), acquired, coverage, product = candidate
                    tiles = groups.setdefault((platform, datatake), {})
                    if tile not in tiles or product["id"] > tiles[tile][2]["id"]:
                        tiles[tile] = acquired, coverage, product
                context = data.get("context") or {}
                if not isinstance(context, dict):
                    raise UserError("影像目录分页格式不正确，请稍后重试。")
                cursor = context.get("next")
                if cursor is None:
                    break
                signature = str(cursor)
                if signature in seen_cursors or len(seen_cursors) >= 1000:
                    raise UserError("检索结果过多或分页未推进，请将日期或研究区分批查询。")
                seen_cursors.add(signature)
                body["next"] = cursor
            if progress:
                progress(f"已完成 {chunk_index + 1}/{len(chunks)} 个时间段", (chunk_index + 1) / len(chunks))
    finally:
        http.close()

    scenes = []
    aoi_area = area_km2(geometry)
    for (platform, datatake), tiles in groups.items():
        check_cancel(cancel)
        times, coverage, products = zip(*(tiles[tile] for tile in sorted(tiles)))
        clouds = [p["cloud"] for p in products if p["cloud"] is not None]
        covered = min(100.0, 100 * _polygon_area(unary_union(coverage)) / aoi_area)
        earliest, latest = min(times), max(times)
        scenes.append({
            "key": f"{platform}_{datatake}", "platform": platform, "acquisition": datatake,
            "datetime": utc_text(earliest), "acquisition_start": utc_text(earliest), "acquisition_end": utc_text(latest),
            "date": earliest.date().isoformat(), "time": earliest.strftime("%H:%M:%S"),
            "tiles": sorted(tiles), "tile_count": len(tiles),
            "cloud_min": round(min(clouds), 1) if clouds else None, "cloud_max": round(max(clouds), 1) if clouds else None,
            "coverage_percent": round(covered, 1), "catalogue": "Sentinel Hub STAC", "products": list(products),
        })
    return sorted(scenes, key=lambda scene: (scene["datetime"], scene["platform"]), reverse=True)
