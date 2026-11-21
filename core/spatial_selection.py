"""Product-level selection, footprint coverage and deterministic spatial priority."""
from __future__ import annotations

import copy
import math

from shapely.errors import GEOSException
from shapely.geometry import GeometryCollection, mapping, shape
from shapely.ops import unary_union

from .errors import UserError
from .geometry import area_km2
from .scene_identity import SAFE_ID, utc_time

PRIORITIES = {"cloud": "云量较低优先", "newest": "日期较新优先", "selection": "手动排列优先"}


def priority_option(value):
    priority = value.get("priority", "cloud")
    if not isinstance(priority, str) or priority not in PRIORITIES:
        raise UserError("请选择云量、日期或手动排列的拼接优先级。")
    return priority


def available(scene):
    if "native" in scene:
        return scene["native"].get("available") is True
    if "raw" in scene:
        return scene["raw"].get("available") is True
    if "gedi" in scene:
        return scene["gedi"].get("available") is True
    return True


def polygonal(geometry):
    if geometry.geom_type in ("Polygon", "MultiPolygon"):
        return geometry
    if geometry.geom_type == "GeometryCollection":
        return unary_union([polygonal(part) for part in geometry.geoms])
    return GeometryCollection()


def polygon_area(geometry):
    if geometry.is_empty or geometry.area < 1e-13:
        return 0.0
    if geometry.geom_type in ("Polygon", "MultiPolygon"):
        # Intersections insert collinear lon/lat vertices. Follow those straight map edges
        # consistently instead of treating every new segment as a different long geodesic.
        return area_km2(geometry.segmentize(.01))
    return sum(polygon_area(part) for part in getattr(geometry, "geoms", ()))


def footprint(scene):
    regions = []
    try:
        for product in scene.get("products", []):
            region = shape(product["footprint"])
            if (region.geom_type not in ("Polygon", "MultiPolygon") or region.is_empty
                    or not region.is_valid or not all(math.isfinite(n) for n in region.bounds)):
                raise ValueError
            regions.append(region)
        if not regions:
            raise ValueError
        return polygonal(unary_union(regions))
    except (GEOSException, ValueError, KeyError, TypeError, AttributeError):
        raise UserError("所选影像缺少有效的产品覆盖范围，请重新检索。") from None


def split_process_scenes(geometry, scenes):
    """Keep each whitelist to one tile, while retaining the original overpass identity."""
    result = []
    area = polygon_area(geometry)
    for scene in scenes:
        for product in scene["products"]:
            parsed = SAFE_ID.fullmatch(product["id"])
            if not parsed:
                raise UserError("影像目录的产品标识无法匹配，请重新检索。")
            acquired = utc_time(product["acquisition_utc"])
            row = copy.deepcopy(scene)
            cloud = product.get("cloud")
            row.update(key=product["id"], title=product["id"], overpass_key=scene["key"],
                       products=[copy.deepcopy(product)], tiles=[parsed[3]], tile_count=1,
                       datetime=product["acquisition_utc"], date=acquired.date().isoformat(),
                       time=acquired.strftime("%H:%M:%S"), acquisition_start=product["acquisition_utc"],
                       acquisition_end=product["acquisition_utc"], cloud_min=cloud, cloud_max=cloud)
            row["coverage_percent"] = round(min(100, 100 * polygon_area(footprint(row).intersection(geometry)) / area), 3)
            result.append(row)
    return sorted(result, key=lambda row: (row["datetime"], row["key"]), reverse=True)


def ordered_scenes(scenes, priority="cloud"):
    if priority == "selection":
        return list(scenes)

    def rank(scene):
        cloud = scene.get("cloud_max")
        cloud = float(cloud) if cloud is not None else math.inf
        try:
            date = utc_time(scene["datetime"]).timestamp()
        except (ValueError, TypeError, KeyError, AttributeError):
            date = -math.inf
        return ((cloud, -date, scene["key"]) if priority == "cloud"
                else (-date, cloud, scene["key"]))

    return sorted(scenes, key=rank)


def coverage_summary(geometry, scenes, selected_keys, priority="cloud"):
    """All decisions use actual footprint/AOI intersections, never the basemap grid."""
    priority_option({"priority": priority})
    known = {scene["key"]: scene for scene in scenes}
    if len(selected_keys) != len(set(selected_keys)) or any(key not in known for key in selected_keys):
        raise UserError("影像区块选择已经变化，请重新选择。")
    area = polygon_area(geometry)
    regions, missing = {}, []
    for scene in scenes:
        try:
            regions[scene["key"]] = polygonal(geometry.intersection(footprint(scene)))
        except UserError:
            missing.append(scene["key"])
    if any(key in missing for key in selected_keys):
        raise UserError("所选影像缺少可核对的覆盖范围，无法计算拼接，请重新检索。")
    chosen = ordered_scenes([known[key] for key in selected_keys], priority)
    covered, overlap = GeometryCollection(), GeometryCollection()
    assigned, ranks = {}, {}
    for rank, scene in enumerate(chosen, 1):
        key = scene["key"]
        region = regions[key]
        overlap = polygonal(unary_union([overlap, covered.intersection(region)]))
        assigned[key] = polygonal(region.difference(covered))
        covered = polygonal(unary_union([covered, region]))
        ranks[key] = rank
    recommendation, accumulated = [], GeometryCollection()
    for scene in ordered_scenes([s for s in scenes if available(s) and s["key"] in regions], priority):
        region = regions[scene["key"]]
        extra = polygonal(region.difference(accumulated))
        # Sub-square-metre coordinate noise is not another useful image block.
        if polygon_area(extra) > 1e-6:
            recommendation.append(scene["key"])
            accumulated = polygonal(unary_union([accumulated, region]))
    uncovered = polygonal(geometry.difference(covered))

    def percent(region):
        return round(max(0.0, min(100.0, 100 * polygon_area(region) / area)), 4)

    def geojson(region):
        return None if region.is_empty else mapping(region)

    blocks = []
    for scene in scenes:
        key = scene["key"]
        region = regions.get(key, GeometryCollection())
        allocated = assigned.get(key, GeometryCollection())
        blocks.append({"key": key, "date": scene.get("date"), "acquisition_utc": scene.get("datetime"),
                       "tiles": scene.get("tiles", []), "selected": key in ranks, "rank": ranks.get(key),
                       "cloud": scene.get("cloud_max"), "intersection": geojson(region),
                       "assigned": geojson(allocated), "coverage_percent": percent(region),
                       "contribution_percent": percent(allocated), "contribution_km2": polygon_area(allocated)})
    return {"priority": priority, "priority_label": PRIORITIES[priority], "candidate_count": len(scenes),
            "selected_count": len(chosen), "tile_count": len({tile for s in chosen for tile in s.get("tiles", [])}),
            "dates": sorted({s["date"] for s in chosen if s.get("datetime") and s.get("date")}),
            "coverage_percent": percent(covered), "overlap_percent": percent(overlap),
            "uncovered_percent": percent(uncovered), "aoi_area_km2": area,
            "covered": geojson(covered), "overlap": geojson(overlap), "uncovered": geojson(uncovered),
            "blocks": blocks, "ordered_keys": [s["key"] for s in chosen], "recommended_keys": recommendation,
            "redundant_keys": [s["key"] for s in chosen if polygon_area(assigned[s["key"]]) <= 1e-6],
            "missing_footprint_keys": missing,
            "coverage_note": "产品边界的几何覆盖率；不代表无云或有有效光谱。拼接时保留实际像元来源。"}
