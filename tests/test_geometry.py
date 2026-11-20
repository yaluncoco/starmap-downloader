import pytest
import shapefile
from pyproj import CRS, Transformer
from shapely.geometry import MultiPolygon, Polygon, box, mapping
from shapely import transform

from core.errors import UserError
from core.geometry import load_aoi, normalize_aoi, parse_coordinates, save_aoi
from core.planning import build_plan, normalize_options


@pytest.mark.parametrize("text", [
    "116.350,39.900,116.355,39.905",
    "116.350，39.900；116.355，39.900\n116.355，39.905\n116.350，39.905",
    "[[116.35,39.9],[116.355,39.9],[116.355,39.905],[116.35,39.905]]",
    "POLYGON((116.35 39.9, 116.355 39.9, 116.355 39.905, 116.35 39.905, 116.35 39.9))",
])
def test_coordinate_formats(text):
    assert parse_coordinates(text).equals(box(116.35, 39.9, 116.355, 39.905))


@pytest.mark.parametrize("text", ["[[]]", "[[1],3]", "116,40\n117,41\n118,42", "179,-1,-179,1", "-179,-1,179,1", "116,91,117,92"])
def test_invalid_coordinates(text):
    with pytest.raises(UserError):
        parse_coordinates(text)


def test_shp_round_trip_keeps_hole_and_multipart(tmp_path):
    geometry = MultiPolygon([Polygon([(116, 39), (117, 39), (117, 40), (116, 40)], holes=[[(116.2, 39.2), (116.5, 39.2), (116.5, 39.5), (116.2, 39.5)]]), box(115, 39, 115.2, 39.2)])
    shp = tmp_path / "边界.shp"
    save_aoi(geometry, shp)
    assert all(shp.with_suffix(suffix).exists() for suffix in (".shp", ".shx", ".dbf", ".prj", ".cpg"))
    assert load_aoi(shp).equals(geometry)
    geojson = tmp_path / "边界.geojson"
    assert load_aoi(save_aoi(geometry, geojson)).equals(geometry)
    with pytest.raises(UserError, match="已存在"):
        save_aoi(geometry, shp)


def test_projected_shp_reads_prj_or_user_override(tmp_path):
    geometry = box(116.35, 39.90, 116.355, 39.905)
    projected = transform(geometry, Transformer.from_crs(4326, 32650, always_xy=True).transform, interleaved=False)
    path = tmp_path / "utm.shp"
    with shapefile.Writer(str(path), shapeType=shapefile.POLYGON) as writer:
        writer.field("name", "C")
        writer.shape(mapping(projected))
        writer.record("UTM")
    with pytest.raises(UserError, match="没有 .prj"):
        load_aoi(path)
    assert load_aoi(path, "EPSG:32650").hausdorff_distance(geometry) < 1e-9
    path.with_suffix(".prj").write_text("invalid", encoding="utf-8")
    assert load_aoi(path, "EPSG:32650").hausdorff_distance(geometry) < 1e-9
    path.with_suffix(".prj").write_text(CRS.from_epsg(32650).to_wkt(version="WKT1_ESRI"), encoding="utf-8")
    assert load_aoi(path).hausdorff_distance(geometry) < 1e-9


def test_planning_utm_and_canonical_geometry():
    options = normalize_options({"mode": "rgbnir", "resolution": "10"})
    area = box(116.35, 39.9, 116.355, 39.905)
    plan, tiles, _ = build_plan(area, options, 3, tile_size=16)
    reverse = Polygon(list(area.exterior.coords)[::-1])
    assert build_plan(reverse, options, 3, tile_size=16)[0]["fingerprint"] == plan["fingerprint"]
    assert (plan["epsg"], plan["width"], plan["height"]) == (32650, 44, 57)
    assert plan["bytes_per_scene"] == 44 * 57 * 4 * 4
    assert plan["requests"] == 3 * sum(t.intersects for t in tiles)
    assert sum(t.width * t.height for t in tiles) == 44 * 57
    with pytest.raises(UserError, match="跨度"):
        build_plan(box(100, 30, 112, 34), options)


def test_invalid_aoi_and_options():
    with pytest.raises(UserError, match="源坐标系"):
        normalize_aoi({"type": "Polygon", "crs": "bad"})
    with pytest.raises(UserError, match="波段"):
        normalize_options({"mode": "custom", "bands": [["bad"]]})
    with pytest.raises(UserError, match="分辨率"):
        normalize_options({"resolution": 10.5})
