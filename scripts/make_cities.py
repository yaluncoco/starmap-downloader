"""Build immutable, full city boundaries from the attributed GCJ-02 source SHPs."""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import zipfile
from pathlib import Path

import numpy as np
import shapefile
from shapely import make_valid, transform
from shapely.geometry import mapping, shape

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from core.geometry import describe_aoi, normalize_aoi
from core.spatial_selection import polygonal

MUNICIPALITIES = {"110000", "120000", "310000", "500000", "810000", "820000"}


def wgs84_to_gcj02(lon, lat):
    lon, lat = np.asarray(lon, dtype=np.float64), np.asarray(lat, dtype=np.float64)
    inside = (lon >= 72.004) & (lon <= 137.8347) & (lat >= .8293) & (lat <= 55.8271)
    x, y = lon - 105, lat - 35
    dlat = -100 + 2*x + 3*y + .2*y*y + .1*x*y + .2*np.sqrt(np.abs(x))
    dlat += (20*np.sin(6*x*np.pi) + 20*np.sin(2*x*np.pi))*2/3
    dlat += (20*np.sin(y*np.pi) + 40*np.sin(y/3*np.pi))*2/3
    dlat += (160*np.sin(y/12*np.pi) + 320*np.sin(y*np.pi/30))*2/3
    dlon = 300 + x + 2*y + .1*x*x + .1*x*y + .1*np.sqrt(np.abs(x))
    dlon += (20*np.sin(6*x*np.pi) + 20*np.sin(2*x*np.pi))*2/3
    dlon += (20*np.sin(x*np.pi) + 40*np.sin(x/3*np.pi))*2/3
    dlon += (150*np.sin(x/12*np.pi) + 300*np.sin(x/30*np.pi))*2/3
    rad = lat*np.pi/180
    magic = 1 - .00669342162296594323*np.sin(rad)**2
    dlat = dlat*180 / ((6378245*(1-.00669342162296594323)/(magic*np.sqrt(magic)))*np.pi)
    dlon = dlon*180 / (6378245/np.sqrt(magic)*np.cos(rad)*np.pi)
    return np.where(inside, lon+dlon, lon), np.where(inside, lat+dlat, lat)


def gcj02_to_wgs84(lon, lat):
    target_x, target_y = np.asarray(lon, dtype=np.float64), np.asarray(lat, dtype=np.float64)
    x, y = target_x.copy(), target_y.copy()
    for _ in range(10):
        actual_x, actual_y = wgs84_to_gcj02(x, y)
        x -= actual_x-target_x
        y -= actual_y-target_y
    return x, y


def canonical(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")


def build(source, output):
    output.mkdir(parents=True, exist_ok=True)
    receipt = json.loads((source/"source-receipt.json").read_text(encoding="utf-8"))
    for item in receipt["files"]:
        data = (source/item["file"]).read_bytes()
        assert len(data)==item["bytes"] and hashlib.sha256(data).hexdigest()==item["sha256"]
    metadata = {"name": "中国完整城市行政边界", "version": "v24.02.06", "reference_year": 2024,
                "repository": receipt["repository"], "commit": receipt["commit"], "citation": receipt["citation"],
                "license": "MIT", "source_crs": "GCJ-02", "crs": "EPSG:4326",
                "conversion": "Iterative inversion of GCJ-02; no geometry simplification for download boundaries",
                "provenance_file": "cities-source.json"}
    provinces, candidates = {}, []
    with shapefile.Reader(str(source/"province/province.shp"), encoding="utf-8") as reader:
        for feature in reader.iterShapeRecords():
            properties = feature.record.as_dict()
            code, name = str(properties["pr_adcode"]), properties["pr_name"]
            provinces[code] = {"id": code, "name": name, "geometry": shape(feature.shape.__geo_interface__)}
            if code in MUNICIPALITIES:
                candidates.append({"id": code, "name": name, "province_id": code, "province": name,
                                   "municipality": True, "geometry": provinces[code]["geometry"], "source_layer": "province"})
    with shapefile.Reader(str(source/"city/city.shp"), encoding="utf-8") as reader:
        source_city_count = len(reader)
        for feature in reader.iterShapeRecords():
            properties = feature.record.as_dict()
            code, province = str(properties["ct_adcode"]), str(properties["pr_adcode"])
            if province in MUNICIPALITIES:
                continue  # Whole municipalities include all urban districts and rural counties.
            assert province in provinces
            candidates.append({"id": code, "name": properties["ct_name"], "province_id": province,
                               "province": properties["pr_name"], "municipality": False,
                               "geometry": shape(feature.shape.__geo_interface__), "source_layer": "city"})
    assert len({item["id"] for item in candidates})==len(candidates)
    rows, repairs, maximum_inverse_error = [], [], 0.0
    archive_path = output/"cities.zip"
    with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for candidate in sorted(candidates, key=lambda item: item["id"]):
            original = candidate.pop("geometry")
            repair = not original.is_valid
            if repair:
                original = polygonal(make_valid(original))
            converted = transform(original, gcj02_to_wgs84, interleaved=False)
            if not converted.is_valid:
                converted = polygonal(make_valid(converted))
                repair = True
            converted = normalize_aoi(mapping(converted))
            description = describe_aoi(converted, candidate["name"])
            member = candidate["id"] + ".geojson"
            data = canonical({"type": "Feature", "geometry": description["geometry"],
                              "properties": {key: value for key, value in candidate.items()}})
            archive.writestr(member, data)
            rows.append({**candidate, **{key: description[key] for key in ("bounds", "center", "vertices", "area_km2")},
                         "member": member, "sha256": hashlib.sha256(data).hexdigest(), "topology_repaired": repair})
            if repair:
                repairs.append(candidate["id"])
            # Independently confirm inversion on every original source vertex.
            for polygon in ([original] if original.geom_type=="Polygon" else original.geoms):
                for ring in [polygon.exterior, *polygon.interiors]:
                    x,y = np.asarray(ring.coords).T
                    wx,wy = gcj02_to_wgs84(x,y)
                    gx,gy = wgs84_to_gcj02(wx,wy)
                    maximum_inverse_error=max(maximum_inverse_error,float(np.max(np.abs(gx-x))),float(np.max(np.abs(gy-y))))
            print(candidate["name"], description["vertices"], flush=True)
    assert maximum_inverse_error <= 1e-8
    index = {"schema": 1, "source": metadata, "archive_sha256": hashlib.sha256(archive_path.read_bytes()).hexdigest(),
             "cities": rows, "provinces": [{"id": key, "name": value["name"]} for key,value in sorted(provinces.items())]}
    (output/"cities-index.json").write_bytes(canonical(index))
    (output/"cities-source.json").write_bytes(canonical({**receipt, "derived": metadata, "cities": len(rows),
        "original_city_records": source_city_count, "full_municipality_codes": sorted(MUNICIPALITIES),
        "topology_repaired": repairs, "maximum_inverse_roundtrip_error_degrees": maximum_inverse_error,
        "archive_sha256": index["archive_sha256"]}))
    (output/"cities-LICENSE.txt").write_bytes((source/"LICENSE").read_bytes())
    # A light, local administrative basemap remains usable without tile servers.
    features = []
    for code, province in sorted(provinces.items()):
        geometry = province["geometry"]
        if not geometry.is_valid:
            geometry = polygonal(make_valid(geometry))
        geometry = transform(geometry, gcj02_to_wgs84, interleaved=False).simplify(.01, preserve_topology=True)
        if not geometry.is_valid:
            geometry = polygonal(make_valid(geometry))
        features.append({"type": "Feature", "geometry": mapping(geometry), "properties": {"id": code, "name": province["name"]}})
    (output/"admin-basemap.geojson").write_bytes(canonical({"type": "FeatureCollection", "features": features}))
    print(json.dumps({"cities": len(rows), "zip_bytes": archive_path.stat().st_size,
                      "repaired": len(repairs), "maximum_inverse_error": maximum_inverse_error}, ensure_ascii=False), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("--output", type=Path, default=ROOT/"core/data/cities")
    args = parser.parse_args()
    build(args.source, args.output)


if __name__=="__main__":
    main()
