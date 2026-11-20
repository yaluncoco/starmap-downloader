from __future__ import annotations

import json
import math
import re
from pathlib import Path

from .local_paths import io_path

import shapefile
from pyproj import CRS, Geod, Transformer
from shapely import force_2d, transform, wkt
from shapely.errors import GEOSException
from shapely.geometry import MultiPolygon, Polygon, box, mapping, shape
from shapely.ops import unary_union
from shapely.validation import explain_validity

from .errors import UserError

WGS84 = CRS.from_epsg(4326)
GEOD = Geod(ellps="WGS84")


def _polygon_parts(geometry):
    if isinstance(geometry, Polygon):
        return [geometry]
    if isinstance(geometry, MultiPolygon):
        return list(geometry.geoms)
    raise UserError("研究区必须是多边形。请使用面状 SHP、Polygon 或 MultiPolygon。")


def area_km2(geometry) -> float:
    total = 0.0
    for polygon in _polygon_parts(geometry):
        x, y = polygon.exterior.xy
        area = abs(GEOD.polygon_area_perimeter(x, y)[0])
        for ring in polygon.interiors:
            x, y = ring.xy
            area -= abs(GEOD.polygon_area_perimeter(x, y)[0])
        total += max(0.0, area)
    return total / 1_000_000


def normalize_aoi(value, source_crs=None):
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            raise UserError("边界不是有效的 GeoJSON。") from None
    if not isinstance(value, dict):
        raise UserError("边界数据格式不正确。")
    crs_metadata = value.get("crs") or {}
    properties = crs_metadata.get("properties", {}) if isinstance(crs_metadata, dict) else None
    if not isinstance(properties, dict):
        raise UserError("GeoJSON 的源坐标系格式不正确。")
    legacy_crs = properties.get("name")
    kind = value.get("type")
    if kind == "FeatureCollection":
        features = value.get("features") or []
        if not features:
            raise UserError("边界文件没有任何面要素。")
        polygons = [normalize_aoi(f, source_crs or legacy_crs) for f in features]
        geometry = unary_union(polygons)
    elif kind == "Feature":
        return normalize_aoi(value.get("geometry"), source_crs or legacy_crs)
    else:
        try:
            geometry = force_2d(shape(value))
        except (ValueError, TypeError, KeyError, IndexError, AttributeError, GEOSException):
            raise UserError("无法解析边界坐标。") from None
        _polygon_parts(geometry)
        if source_crs or legacy_crs:
            try:
                crs = CRS.from_user_input(source_crs or legacy_crs)
                if not crs.equals(WGS84):
                    transformer = Transformer.from_crs(crs, WGS84, always_xy=True)
                    geometry = transform(geometry, transformer.transform, interleaved=False)
            except Exception:
                raise UserError("无法识别源坐标系。请输入 EPSG 编号，或提供正确的 .prj 文件。") from None
    _polygon_parts(geometry)
    if geometry.is_empty:
        raise UserError("研究区为空。")
    if not geometry.is_valid:
        raise UserError("边界自相交或有无效环，请在地图中重画或用 GIS 修复。" + explain_validity(geometry))
    bounds = geometry.bounds
    if not all(math.isfinite(v) for v in bounds):
        raise UserError("边界包含无效坐标。")
    west, south, east, north = bounds
    if not (-180 <= west <= east <= 180 and -90 <= south <= north <= 90):
        raise UserError("经纬度超出范围。经度应为 −180～180，纬度应为 −90～90；投影坐标请设置源坐标系。")
    if east - west > 180:
        raise UserError("首版暂不支持跨越 180° 经线的研究区，请分为东西两部分下载。")
    if area_km2(geometry) < 0.000001:
        raise UserError("研究区面积过小或坐标点在一条直线上。")
    return geometry


def describe_aoi(geometry, name="研究区") -> dict:
    return {
        "geometry": mapping(geometry),
        "name": str(name)[:80],
        "area_km2": round(area_km2(geometry), 4),
        "bounds": list(geometry.bounds),
        "center": [geometry.centroid.x, geometry.centroid.y],
        "vertices": sum(len(p.exterior.coords) + sum(len(r.coords) for r in p.interiors) for p in _polygon_parts(geometry)),
    }


def parse_coordinates(text: str):
    text = str(text).strip()
    if not text:
        raise UserError("请填写四点坐标、矩形范围或 GeoJSON。")
    if text.upper().startswith(("POLYGON", "MULTIPOLYGON")):
        try:
            return normalize_aoi(mapping(wkt.loads(text)))
        except UserError:
            raise
        except Exception:
            raise UserError("WKT 多边形格式不正确。") from None
    if text.startswith("{"):
        return normalize_aoi(text)
    if text.startswith("["):
        try:
            value = json.loads(text)
        except ValueError:
            raise UserError("坐标 JSON 格式不正确。") from None
        if len(value) == 4 and all(isinstance(v, (int, float)) for v in value):
            points = None
            bbox = value
        else:
            points = value[0] if len(value) == 1 and isinstance(value[0], list) and value[0] and isinstance(value[0][0], list) else value
            bbox = None
    else:
        lines = [v.strip() for v in re.split(r"[;；\n]+", text) if v.strip()]
        values = []
        for line in lines:
            try:
                values.append([float(v) for v in re.split(r"[,，\s]+", line) if v])
            except ValueError:
                raise UserError("坐标只能包含数字。每行填写 经度,纬度。") from None
        if len(values) == 1 and len(values[0]) == 4:
            bbox, points = values[0], None
        else:
            points, bbox = values, None
    try:
        if bbox is not None:
            west, south, east, north = bbox
            if not west < east or not south < north:
                raise UserError("矩形范围顺序应为：西经度、南纬度、东经度、北纬度。")
            geometry = box(west, south, east, north)
        else:
            if len(points) < 3 or any(len(p) != 2 for p in points):
                raise UserError("至少需要三个点。每行填写一组 经度,纬度。四点会自动闭合。")
            geometry = Polygon(points)
    except UserError:
        raise
    except Exception:
        raise UserError("坐标格式不正确，请按每行 经度,纬度 输入。") from None
    return normalize_aoi(mapping(geometry))


def load_aoi(path, source_crs=None):
    path = io_path(path)
    if not path.is_file():
        raise UserError("找不到边界文件，请重新选择。")
    if path.stat().st_size > 100 * 1024 * 1024:
        raise UserError("边界文件超过 100 MB，请先合并或简化面要素。")
    if path.suffix.lower() in (".geojson", ".json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            raise UserError("无法读取 GeoJSON，请检查文件内容和编码。") from None
        return normalize_aoi(data, source_crs)
    if path.suffix.lower() != ".shp":
        raise UserError("请选择 .shp、.geojson 或 .json 边界文件。")
    prj = path.with_suffix(".prj")
    crs = source_crs
    if not crs and prj.is_file():
        try:
            crs = prj.read_text(encoding="utf-8-sig")
        except (OSError, UnicodeError):
            raise UserError("无法读取 .prj 文件，请手动填写源坐标系 EPSG 编号。") from None
    if not crs:
        raise UserError("SHP 没有 .prj。请在源坐标系框中填写 EPSG 编号（如 EPSG:4326），然后重新导入。")
    try:
        with shapefile.Reader(str(path), encoding="utf-8", encodingErrors="replace") as reader:
            features = [{"type": "Feature", "geometry": s.__geo_interface__, "properties": {}} for s in reader.iterShapes()]
    except Exception:
        raise UserError("无法读取 SHP。请将 .shp、.shx、.dbf 放在同一文件夹，并确认它是面数据。") from None
    return normalize_aoi({"type": "FeatureCollection", "features": features}, crs)


def save_aoi(geometry, path, name="研究区"):
    path = io_path(path)
    if not path.is_absolute():
        raise UserError("请选择完整的保存路径。")
    if path.suffix.lower() not in (".shp", ".geojson", ".json"):
        path = path.with_suffix(".shp")
    targets = [path]
    if path.suffix.lower() == ".shp":
        targets = [path.with_suffix(ext) for ext in (".shp", ".shx", ".dbf", ".prj", ".cpg")]
    if any(p.exists() for p in targets):
        raise UserError("目标文件已存在，请换一个文件名。")
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        if path.suffix.lower() == ".shp":
            with shapefile.Writer(str(path), shapeType=shapefile.POLYGON, encoding="utf-8") as writer:
                writer.field("name", "C", size=120)
                writer.field("area_km2", "N", size=20, decimal=4)
                writer.shape(mapping(geometry))
                writer.record(name, area_km2(geometry))
            path.with_suffix(".prj").write_text(WGS84.to_wkt(version="WKT1_ESRI"), encoding="utf-8")
            path.with_suffix(".cpg").write_text("UTF-8", encoding="ascii")
        else:
            path.write_text(json.dumps({"type": "FeatureCollection", "features": [{"type": "Feature", "geometry": mapping(geometry), "properties": {"name": name, "area_km2": area_km2(geometry)}}]}, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        raise UserError("边界保存失败，请检查文件夹权限。") from None
    return str(path)

