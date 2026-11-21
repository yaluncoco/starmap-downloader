"""Compare actual mosaic pixels with direct native JP2 pixel centres in user SAFE ZIPs."""
from __future__ import annotations

import argparse
import json
import sys
from collections import OrderedDict
from pathlib import Path

import numpy as np
import rasterio
from pyproj import Transformer
from rasterio.enums import Resampling
from rasterio.vrt import WarpedVRT
from rasterio.windows import Window
from shapely import contains_xy
from shapely.geometry import box, mapping, shape

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core.mosaic import build_mosaic_plan, download_mosaic
from core.planning import normalize_options
from core.scene_identity import SAFE_ID
from core.spatial_selection import ordered_scenes
from check_safe_reference import read_reference


def reference_scene(product):
    parsed = SAFE_ID.fullmatch(product["product_id"])
    return {"key": product["product_id"], "title": product["product_id"], "overpass_key": parsed[1] + "_" + parsed[2],
            "datetime": product["acquisition_utc"], "date": product["acquisition_utc"][:10],
            "time": product["acquisition_utc"][11:19], "platform": product["platform"], "tiles": [product["tile"]],
            "tile_count": 1, "cloud_min": product["cloud"], "cloud_max": product["cloud"],
            "products": [{"id": product["product_id"], "footprint": product["footprint"],
                          "acquisition_utc": product["acquisition_utc"], "cloud": product["cloud"]}]}


class NativeSources:
    def __init__(self, products, bands, epsg=32650):
        self.products = {p["product_id"].removesuffix(".SAFE"): p for p in products}
        self.bands, self.epsg, self.opened = bands, epsg, OrderedDict()

    def source(self, identifier, band):
        product = self.products[identifier.removesuffix(".SAFE")]
        resolution = 20 if band == "SCL" or band in ("B05", "B06", "B07", "B8A", "B11", "B12") else 10
        suffix = f"_{band}_{resolution}m.jp2"
        member, = [value for key, value in product["assets"].items() if key.endswith(suffix)]
        key = product["product_id"], band
        if key not in self.opened:
            while len(self.opened) >= 24:
                self.opened.popitem(last=False)[1].close()
            self.opened[key] = rasterio.open("/vsizip/" + Path(product["zip"]).as_posix() + "/" + member)
        self.opened.move_to_end(key)
        ds = self.opened[key]
        grid = product["grids"][str(resolution)]
        assert ds.count == 1 and ds.crs.to_epsg() == grid["epsg"]
        assert ds.width == grid["width"] and ds.height == grid["height"]
        assert list(ds.transform)[:6] == grid["transform"]
        return ds, product

    def read(self, scene, tile, inside):
        # The export path uses GDAL nearest-neighbour warping of native DN.
        from rasterio.transform import from_origin

        grid = from_origin(tile.bounds[0], tile.bounds[3], (tile.bounds[2] - tile.bounds[0]) / tile.width,
                           (tile.bounds[3] - tile.bounds[1]) / tile.height)
        spectra, quality = [], None
        for band in self.bands + ["SCL"]:
            ds, product = self.source(scene["key"], band)
            with WarpedVRT(ds, crs=f"EPSG:{self.epsg}", transform=grid, width=tile.width, height=tile.height,
                           resampling=Resampling.nearest, src_nodata=255 if band == "SCL" else 0,
                           nodata=255 if band == "SCL" else 0) as virtual:
                values = virtual.read(1)
            if band == "SCL":
                quality = values.astype(np.uint8)
            else:
                valid = values != 0
                reflectance = (values.astype(np.float32) + np.float32(product["boa_offsets"][band])) / np.float32(product["quantification"])
                reflectance[~valid] = -9999
                spectra.append(reflectance)
        return np.stack(spectra), quality, {"sources": [{"catalogue_id": scene["key"].removesuffix(".SAFE"), "match": "safe_id"}],
                                           "verification_source": "native JP2 inside the unchanged official SAFE ZIP"}

    def sample_native(self, identifier, band, x, y, output_crs):
        # The oracle uses explicit pixel indices, independently of the warp/export path.
        ds, product = self.source(identifier, band)
        tx, ty = Transformer.from_crs(output_crs, ds.crs, always_xy=True).transform(x, y)
        col = np.floor((tx - ds.transform.c) / ds.transform.a).astype(np.int64)
        row = np.floor((ty - ds.transform.f) / ds.transform.e).astype(np.int64)
        inside = (row >= 0) & (row < ds.height) & (col >= 0) & (col < ds.width)
        result = np.full(x.shape, 255 if band == "SCL" else 0, dtype=np.uint16)
        if inside.any():
            x0, x1, y0, y1 = int(col[inside].min()), int(col[inside].max()) + 1, int(row[inside].min()), int(row[inside].max()) + 1
            values = ds.read(1, window=Window(x0, y0, x1 - x0, y1 - y0), masked=False)
            result[inside] = values[row[inside] - y0, col[inside] - x0]
        if band == "SCL":
            return result.astype(np.uint8)
        valid = result != 0
        converted = (result.astype(np.float32) + np.float32(product["boa_offsets"][band])) / np.float32(product["quantification"])
        converted[~valid] = -9999
        return converted

    def close(self):
        while self.opened:
            self.opened.popitem()[1].close()


def compare_mosaic(file, geometry, scenes, opts, native):
    def identifier(scene):
        return scene["products"][0]["id"].removesuffix(".SAFE")

    with rasterio.open(file["path"]) as main, rasterio.open(file["bundle"]["unmasked"]) as backup, \
         rasterio.open(file["bundle"]["scl"]) as quality, rasterio.open(file["bundle"]["source"]) as source:
        assert main.count == backup.count == 10
        assert main.descriptions == tuple(opts["bands"])
        assert all(ds.transform == main.transform and ds.crs == main.crs and ds.shape == main.shape for ds in (backup, quality, source))
        x = np.broadcast_to((main.transform.c + (np.arange(main.width) + .5) * main.transform.a)[None], main.shape)
        y = np.broadcast_to((main.transform.f + (np.arange(main.height) + .5) * main.transform.e)[:, None], main.shape)
        longitude, latitude = Transformer.from_crs(main.crs, 4326, always_xy=True).transform(x, y)
        inside = contains_xy(geometry, longitude, latitude)
        expected = np.full((10, *main.shape), -9999, dtype=np.float32)
        expected_scl = np.full(main.shape, 255, dtype=np.uint8)
        expected_source = np.zeros(main.shape, dtype=np.uint16)
        best_quality = np.zeros(main.shape, dtype=np.uint8)
        rows = ordered_scenes(scenes, opts["priority"])
        for index, scene in enumerate(rows, 1):
            spectra = np.stack([native.sample_native(identifier(scene), band, x, y, main.crs) for band in opts["bands"]])
            scl = native.sample_native(identifier(scene), "SCL", x, y, main.crs)
            covered = inside & contains_xy(shape(scene["products"][0]["footprint"]), longitude, latitude)
            present = spectra != -9999
            # Rank complete clear spectra above complete masked spectra above partial spectra.
            level = present.any(axis=0).astype(np.uint8) + present.all(axis=0).astype(np.uint8)
            clear = present.all(axis=0) & (np.isin(scl, (2, 4, 5, 6, 7, 11)) if opts["mask_clouds"] else True)
            level[clear] = 3
            take = covered & (level > best_quality)
            expected[:, take] = spectra[:, take]
            expected_scl[take], expected_source[take], best_quality[take] = scl[take], index, level[take]
        analysis = expected.copy()
        analysis[:, best_quality != 3] = -9999
        observed = main.read()
        raw = backup.read()
        observed_source = source.read(1)
        if not np.array_equal(observed_source, expected_source):
            mismatch = np.argwhere(observed_source != expected_source)
            examples = [{"row": int(row), "col": int(col), "x": float(x[row, col]), "y": float(y[row, col]),
                         "longitude": float(longitude[row, col]), "latitude": float(latitude[row, col]),
                         "observed_source": int(observed_source[row, col]), "reference_source": int(expected_source[row, col]),
                         "observed_scl": int(quality.read(1)[row, col]), "reference_scl": int(expected_scl[row, col])}
                        for row, col in mismatch[:8]]
            print(json.dumps({"source_mismatch_pixels": len(mismatch), "examples": examples}), flush=True)
            raise AssertionError("Pixel source or geographic allocation differs from native reference")
        assert np.array_equal(quality.read(1), expected_scl), "SCL differs from native 20 m nearest-neighbour samples"
        assert np.array_equal(observed == -9999, analysis == -9999), "Analysis NoData/cloud mask differs"
        assert np.array_equal(raw == -9999, expected == -9999), "Unmasked NoData differs"
        valid = raw != -9999
        error = float(np.max(np.abs(raw[valid] - expected[valid]))) if valid.any() else 0
        assert error <= 2e-7, "Reflectance conversion or source pixel alignment differs"
        np.testing.assert_allclose(observed, analysis, rtol=0, atol=2e-7)
        table = json.loads(Path(file["bundle"]["sources"]).read_text(encoding="utf-8"))
        used = []
        for item in table["sources"]:
            pixels = int((expected_source == item["index"]).sum())
            assert item["used_pixels"] == pixels
            assert item["catalogue_id"].removesuffix(".SAFE") == identifier(rows[item["index"] - 1])
            if pixels:
                used.append({"product_id": item["catalogue_id"], "date": item["acquisition_utc"][:10], "pixels": pixels})
        return {"bands": list(main.descriptions), "shape": [main.width, main.height], "epsg": main.crs.to_epsg(),
                "transform": list(main.transform)[:6], "native_pixel_centres_compared": int(valid.sum()),
                "max_absolute_reflectance_error": error, "exact_scl_match": True, "exact_source_match": True,
                "exact_nodata_match": True, "outside_aoi_nodata": True, "used_products": used}


def check(directory, output, *, resume=False):
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    products = [read_reference(path) for path in sorted(directory.glob("*.SAFE.zip"))]
    assert len(products) == 10
    scenes = [reference_scene(p) for p in products]
    opts = normalize_options({"mode": "research", "layout": "mosaic", "priority": "cloud", "mask_clouds": True, "max_cloud": 100})
    report = {"status": "running", "reference_directory": str(directory), "cases": []}
    path = output / "mosaic-native-pixel-report.json"
    previous = json.loads(path.read_text(encoding="utf-8")).get("cases", []) if resume and path.is_file() else []
    native = NativeSources(products, opts["bands"])
    try:
        cases = []
        for scene in scenes:
            point = shape(scene["products"][0]["footprint"]).representative_point()
            cases.append((scene["key"], box(point.x - .0008, point.y - .0008, point.x + .0008, point.y + .0008), [scene]))
        pair = [s for s in scenes if s["tiles"][0] in ("T50SMJ", "T50SNJ") and s["date"] != "2026-04-18"]
        cases.append(("cross-date-seam", box(117.060, 39.30, 117.075, 39.32), pair))
        for label, geometry, selected in cases:
            plan = build_mosaic_plan(geometry, selected, opts)
            native.epsg = plan[0]["epsg"]
            result = download_mosaic(geometry, selected, opts, str(output / "exports"), planned=plan, reader=native)
            assert len(result["files"]) == 1
            prior = next((case for case in previous if case["case"] == label and
                          case["file"]["path"] == result["files"][0]["path"] and
                          json.dumps(case["geometry"], sort_keys=True) == json.dumps(mapping(geometry), sort_keys=True)), None)
            # Cached bundles have just passed all stored SHA-256 checks; retain their completed native-pixel evidence.
            proof = ({key: value for key, value in prior.items() if key not in ("case", "geometry", "manifest", "file")}
                     if prior and result["files"][0]["status"] == "skipped"
                     else compare_mosaic(result["files"][0], geometry, selected, opts, native))
            if label == "cross-date-seam":
                assert len(proof["used_products"]) == 2 and len({p["date"] for p in proof["used_products"]}) == 2
            report["cases"].append({"case": label, "geometry": mapping(geometry), "manifest": result["manifest"],
                                     "file": result["files"][0], **proof})
            path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"Native pixel comparison passed: {label}", flush=True)
        report["status"] = "passed"
    except Exception as error:
        report.update(status="failed", error=str(error))
        raise
    finally:
        native.close()
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"status": report["status"], "cases": len(report["cases"]), "report": str(path)}, ensure_ascii=False), flush=True)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()
    check(args.directory, args.output, resume=args.resume)


if __name__ == "__main__":
    main()
