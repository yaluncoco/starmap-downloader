"""Check exported pixel centers against direct live samples of the recorded COG objects."""
from __future__ import annotations

import argparse
import json
import sys
from contextlib import ExitStack
from pathlib import Path

import numpy as np
import rasterio
from pyproj import Transformer
from shapely.geometry import Point, shape

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core.http import HttpClient
from core.range_file import RangeFile


def check(report_path):
    results = []
    http = HttpClient()
    try:
        for evidence in json.loads(report_path.read_text(encoding="utf-8"))["public_exports"]:
            raster_path = Path(evidence["geo_tiff"])
            source = json.loads(Path(evidence["manifest"]).read_text(encoding="utf-8"))["items"][0]
            geometry = shape(json.loads((raster_path.parent / "研究区.geojson").read_text(encoding="utf-8"))["geometry"])
            with ExitStack() as stack, rasterio.Env(GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR", GDAL_PAM_ENABLED="NO"):
                output = stack.enter_context(rasterio.open(raster_path))
                inv = Transformer.from_crs(output.crs, 4326, always_xy=True)
                points = [(r, c) for r in np.unique(np.linspace(0, output.height-1, 10, dtype=int))
                          for c in np.unique(np.linspace(0, output.width-1, 10, dtype=int))]
                centers = [output.xy(r, c) for r,c in points]
                out_band = output.read(1, masked=True)
                observed = [out_band[r, c] for r, c in points]
                coords_wgs84 = [inv.transform(*p) for p in centers]
                expected_inside = [geometry.covers(Point(*p)) for p in coords_wgs84]
                samples, object_etags = {}, {}
                for band, spec in source["assets"].items():
                    stream = stack.enter_context(RangeFile(http, spec["url"]))
                    stream.defer_errors = True
                    def opener(name, mode="rb", *, _stream=stream, _url=spec["url"], **kwargs):
                        if name != _url or mode not in ("r", "rb"):
                            raise FileNotFoundError
                        return _stream
                    ds = stack.enter_context(rasterio.open(spec["url"], opener=opener))
                    tr = Transformer.from_crs(output.crs, ds.crs, always_xy=True)
                    samples[band] = [pixel[0] for pixel in ds.sample((tr.transform(*p) for p in centers), masked=True)]
                    if stream.failure:
                        raise stream.failure
                    assert stream.etag == source["transport"][band]["etag"], "COG changed since export; please use fresh evidence."
                    object_etags[band] = stream.etag
                band = output.descriptions[0]
                spec = source["assets"][band]
                numeric = invalid = 0
                max_diff = 0.0
                for i, valid in enumerate(expected_inside):
                    raw = samples[band][i]
                    scl = samples["SCL"][i] if "SCL" in samples else None
                    valid = valid and not np.ma.is_masked(raw) and (scl is None or not np.ma.is_masked(scl) and scl in (2,4,5,6,7,11))
                    if valid:
                        assert not np.ma.is_masked(observed[i])
                        rounded = np.float32(raw) * np.float32(spec["scale"]) + np.float32(spec["offset"])
                        diff = abs(float(rounded)-float(observed[i]))
                        assert diff <= 2e-6, (evidence["profile"], points[i], diff)
                        max_diff = max(max_diff, diff)
                        numeric += 1
                    else:
                        assert np.ma.is_masked(observed[i]), (evidence["profile"], points[i])
                        invalid += 1
                assert numeric and invalid
                results.append({"profile": evidence["profile"], "checks": len(points), "valid_values_match": numeric, "nodata_matches": invalid,
                                "max_absolute_error": max_diff, "object_etags": object_etags})
    finally:
        http.close()
    return results


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    args = parser.parse_args()
    output = check(args.report)
    encoded = json.dumps(output, ensure_ascii=True, indent=2)
    args.report.with_name("pixel-reference-check.json").write_text(encoded + "\n", encoding="utf-8")
    print(encoded)
