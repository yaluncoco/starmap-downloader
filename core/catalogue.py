from __future__ import annotations

import calendar
import datetime as dt
import re
from urllib.parse import urlparse

from shapely.geometry import shape
from shapely.ops import unary_union

from .errors import UserError
from .geometry import area_km2
from .http import CATALOGUE_URL, HttpClient, check_cancel

UTC = dt.timezone.utc


def date_range(start, end, minimum=dt.date(2015, 6, 23)):
    try:
        start_date, end_date = dt.date.fromisoformat(start), dt.date.fromisoformat(end)
    except (ValueError, TypeError):
        raise UserError("请选择有效的开始和结束日期。") from None
    if start_date > end_date:
        raise UserError("开始日期不能晚于结束日期。")
    if start_date < minimum:
        raise UserError(f"开始日期不能早于 {minimum.isoformat()}。")
    if end_date > dt.datetime.now(UTC).date():
        raise UserError("结束日期不能晚于今天。")
    return start_date, end_date + dt.timedelta(days=1)


def month_chunks(start, end_exclusive):
    chunks = []
    while start < end_exclusive:
        days = calendar.monthrange(start.year, start.month)[1]
        next_month = start.replace(day=days) + dt.timedelta(days=1)
        stop = min(end_exclusive, next_month)
        chunks.append((start, stop))
        start = stop
    return chunks


def cloud_value(product):
    for attribute in product.get("Attributes", []):
        if attribute.get("Name") == "cloudCover":
            try:
                return float(attribute["Value"])
            except (ValueError, TypeError, KeyError):
                pass
    return None


def find_scenes(geometry, start, end, max_cloud, *, cancel=None, progress=None):
    start_date, end_exclusive = date_range(start, end)
    try:
        max_cloud = float(max_cloud)
    except (TypeError, ValueError):
        raise UserError("云量应为 0～100%。") from None
    if not 0 <= max_cloud <= 100:
        raise UserError("云量应为 0～100%。")
    # A bounding box prevents URL-length failures for detailed SHP boundaries.
    # Every returned footprint is intersected with the exact AOI below.
    search_wkt = geometry.envelope.wkt
    chunks = month_chunks(start_date, end_exclusive)
    records = {}
    http = HttpClient()
    try:
        for index, (begin, stop) in enumerate(chunks):
            check_cancel(cancel)
            query = (
                "Collection/Name eq 'SENTINEL-2' and "
                "Attributes/OData.CSC.StringAttribute/any(a:a/Name eq 'productType' and a/OData.CSC.StringAttribute/Value eq 'S2MSI2A') and "
                f"OData.CSC.Intersects(area=geography'SRID=4326;{search_wkt}') and "
                f"ContentDate/Start ge {begin.isoformat()}T00:00:00Z and "
                f"ContentDate/Start lt {stop.isoformat()}T00:00:00Z and "
                "Attributes/OData.CSC.DoubleAttribute/any(a:a/Name eq 'cloudCover' and "
                f"a/OData.CSC.DoubleAttribute/Value le {max_cloud:g})"
            )
            url, params = CATALOGUE_URL, {"$filter": query, "$expand": "Attributes", "$top": 1000, "$orderby": "ContentDate/Start asc"}
            seen_pages = set()
            while url:
                check_cancel(cancel)
                if progress:
                    progress(f"检索 {begin.isoformat()} 至 {(stop - dt.timedelta(days=1)).isoformat()} · 已读取 {len(records)} 个产品", index / max(1, len(chunks)))
                response = http.request("GET", url, params=params, timeout=(15, 90), cancel=cancel, status=lambda m: progress(m, index / len(chunks)) if progress else None)
                try:
                    if response.status_code != 200:
                        raise UserError(f"影像目录检索失败（HTTP {response.status_code}）。请缩小日期范围后重试。")
                    data = response.json()
                except ValueError:
                    raise UserError("影像目录返回了无效数据，请稍后重试。") from None
                finally:
                    response.close()
                if not isinstance(data, dict) or not isinstance(data.get("value"), list):
                    raise UserError("官方目录返回格式发生变化，请检查软件更新。")
                for product in data["value"]:
                    if not product.get("GeoFootprint") or not product.get("Online", True):
                        continue
                    try:
                        footprint = shape(product["GeoFootprint"])
                        if not footprint.is_valid or not footprint.intersects(geometry):
                            continue
                    except Exception:
                        continue
                    records[str(product["Id"])] = product
                next_link = data.get("@odata.nextLink")
                if next_link:
                    parsed, original = urlparse(next_link), urlparse(CATALOGUE_URL)
                    if parsed.scheme != original.scheme or parsed.netloc != original.netloc or parsed.path != original.path:
                        raise UserError("目录分页地址不正确，检索已停止。")
                    if next_link in seen_pages or len(seen_pages) > 500:
                        raise UserError("检索结果过多，请缩小时间范围或研究区。")
                    seen_pages.add(next_link)
                url, params = next_link, None
            if progress:
                progress(f"已完成 {index + 1}/{len(chunks)} 个时间段", (index + 1) / len(chunks))
    finally:
        http.close()
    grouped = {}
    for product in records.values():
        name = product.get("Name", "")
        match = re.match(r"(S2[ABC])_MSIL2A_(\d{8}T\d{6})_N\d+_R\d+_(T\w{5})_", name)
        if not match:
            continue
        platform, acquisition, tile = match.groups()
        key = f"{platform}_{acquisition}"
        group = grouped.setdefault(key, {"platform": platform, "acquisition": acquisition, "tiles": {}})
        old = group["tiles"].get(tile)
        # Prefer the latest processing baseline/production for duplicate tiles.
        if old is None or name > old["Name"]:
            group["tiles"][tile] = product
    scenes = []
    aoi_area = area_km2(geometry)
    for key, group in grouped.items():
        products = list(group["tiles"].values())
        cloud = [cloud_value(p) for p in products]
        cloud = [v for v in cloud if v is not None]
        footprints = [shape(p["GeoFootprint"]) for p in products]
        coverage = unary_union(footprints).intersection(geometry)
        coverage_percent = min(100.0, area_km2(coverage) / aoi_area * 100) if coverage.geom_type in ("Polygon", "MultiPolygon") and not coverage.is_empty else 0.0
        acquired = dt.datetime.strptime(group["acquisition"], "%Y%m%dT%H%M%S").replace(tzinfo=UTC)
        scenes.append({
            "key": key,
            "platform": group["platform"],
            "acquisition": group["acquisition"],
            "datetime": acquired.isoformat().replace("+00:00", "Z"),
            "date": acquired.date().isoformat(),
            "time": acquired.strftime("%H:%M:%S"),
            "tiles": sorted(group["tiles"]),
            "tile_count": len(products),
            "cloud_min": round(min(cloud), 1) if cloud else None,
            "cloud_max": round(max(cloud), 1) if cloud else None,
            "coverage_percent": round(coverage_percent, 1),
            "products": [{"id": p["Id"], "name": p["Name"], "cloud": cloud_value(p), "footprint": p["GeoFootprint"], "s3_path": p.get("S3Path")} for p in products],
        })
    return sorted(scenes, key=lambda v: (v["datetime"], v["platform"]), reverse=True)

