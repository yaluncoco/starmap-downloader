"""Compare saved public monthly pixels with direct, checksum-verified native COGs.

Uses explicit native pixel indexes; no app reader, warping, cache, or HTTP requests.
Checks stratified positions for every used product and every partial-spectrum pixel.
"""
from __future__ import annotations

import argparse
import collections
import hashlib
import json
import time
from contextlib import ExitStack
from pathlib import Path

import numpy as np
import rasterio
from pyproj import Transformer
from rasterio.windows import Window

NODATA = -9999.0
CLEAR = (2, 4, 5, 6, 7, 11)


def samples(ds, points):
    """Read each native block once, preserving the original stored values."""
    points = np.asarray(points, dtype=np.int64)
    result = np.empty((ds.count, len(points)), dtype=np.dtype(ds.dtypes[0]))
    bh, bw = ds.block_shapes[0]
    groups = collections.defaultdict(list)
    for index, (row, col) in enumerate(points):
        groups[int(row) // bh, int(col) // bw].append((index, int(row), int(col)))
    for (br, bc), entries in groups.items():
        y, x = br * bh, bc * bw
        data = ds.read(window=Window(x, y, min(bw, ds.width - x), min(bh, ds.height - y)))
        indexes, rows, cols = np.asarray(entries, dtype=np.int64).T
        result[:, indexes] = data[:, rows - y, cols - x]
    return result


def native_samples(ds, east, north, crs, quality=False):
    x, y = Transformer.from_crs(crs, ds.crs, always_xy=True).transform(east, north)
    inverse = ~ds.transform
    col = np.floor(inverse.a * x + inverse.b * y + inverse.c).astype(np.int64)
    row = np.floor(inverse.d * x + inverse.e * y + inverse.f).astype(np.int64)
    inside = (row >= 0) & (row < ds.height) & (col >= 0) & (col < ds.width)
    result = np.full(len(x), 255 if quality else 0, dtype=np.dtype(ds.dtypes[0]))
    result[inside] = samples(ds, np.column_stack((row[inside], col[inside])))[0]
    return result


def check(manifest, report_path):
    began = time.monotonic()
    document = json.loads(manifest.read_text(encoding="utf-8"))
    assert document["pipeline"] == "download-scenes-mosaic-clip" and document["status"] == "completed"
    file, = document["clipped_result"]["files"]
    table = json.loads(Path(file["bundle"]["sources"]).read_text(encoding="utf-8"))
    originals = {item["product"]: json.loads(Path(item["manifest"]).read_text(encoding="utf-8"))
                 for item in document["sources"]}
    positions, partial = collections.defaultdict(list), collections.defaultdict(set)
    report = {"status": "running", "manifest": str(manifest),
              "reference": "complete saved public COGs, provider SHA-256 and explicit native pixel indexes",
              "network_requests": 0, "sources": []}

    def save():
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    with ExitStack() as stack:
        output = stack.enter_context(rasterio.open(file["path"]))
        raw = stack.enter_context(rasterio.open(file["bundle"]["unmasked"]))
        quality = stack.enter_context(rasterio.open(file["bundle"]["scl"]))
        source = stack.enter_context(rasterio.open(file["bundle"]["source"]))
        assert raw.count == output.count == len(output.descriptions)
        for _, window in source.block_windows(1):
            indexes = source.read(1, window=window)
            available = raw.read(window=window) != NODATA
            incomplete = available.any(axis=0) & ~available.all(axis=0)
            for index in np.unique(indexes[indexes > 0]):
                y, x = np.nonzero(indexes == index)
                picks = np.unique([0, len(y) // 2, len(y) - 1])
                points = [(int(y[p] + window.row_off), int(x[p] + window.col_off)) for p in picks]
                score = hashlib.sha256(f"{index}:{window.row_off}:{window.col_off}".encode()).hexdigest()
                positions[int(index)].append((score, points))
                positions[int(index)].sort(key=lambda entry: entry[0])
                del positions[int(index)][24:]
                py, px = np.nonzero(incomplete & (indexes == index))
                partial[int(index)].update((int(y + window.row_off), int(x + window.col_off)) for y, x in zip(py, px))
        for entry in table["sources"]:
            index = entry["index"]
            if not entry["used_pixels"]:
                continue
            points = sorted(set(point for _, group in positions[index] for point in group) | partial[index])
            assert points
            rows, cols = np.asarray(points).T
            east = output.transform.c + (cols + .5) * output.transform.a + (rows + .5) * output.transform.b
            north = output.transform.f + (cols + .5) * output.transform.d + (rows + .5) * output.transform.e
            np.testing.assert_array_equal(samples(source, points)[0], index)
            observed, expected = samples(raw, points), []
            original = originals[entry["catalogue_id"]]
            assert original["status"] == "completed"
            assets = {item["asset"]: item for item in original["items"]}
            hashes = {}
            for band in (*output.descriptions, "SCL"):
                item = assets[band]
                path = Path(item["path"])
                before = path.stat()
                with path.open("rb") as stream:
                    digest = hashlib.file_digest(stream, "sha256").hexdigest()
                assert digest == item["sha256"] == item["source_checksums"]["SHA-256"]
                hashes[band] = digest
                spec = item["catalogue_raster"]
                with rasterio.open(path) as native:
                    assert native.driver == "GTiff" and native.count == 1
                    assert native.crs.to_epsg() == spec["epsg"] and list(native.shape) == spec["shape"]
                    assert native.transform.almost_equals(rasterio.Affine(*spec["transform"]), precision=1e-6)
                    stored = native_samples(native, east, north, output.crs, quality=band == "SCL")
                assert path.stat().st_mtime_ns == before.st_mtime_ns and path.stat().st_size == before.st_size
                if band == "SCL":
                    expected_scl = stored
                else:
                    converted = (stored.astype(np.float64) * spec["scale"] + spec["offset"]).astype(np.float32)
                    converted[stored == spec["nodata"]] = NODATA
                    expected.append(converted)
            expected = np.stack(expected)
            np.testing.assert_array_equal(observed == NODATA, expected == NODATA)
            valid = expected != NODATA
            max_error = float(np.max(np.abs(observed[valid] - expected[valid]))) if valid.any() else 0
            assert max_error <= 2e-7
            np.testing.assert_allclose(observed, expected, rtol=0, atol=2e-7)
            np.testing.assert_array_equal(samples(quality, points)[0], expected_scl)
            analysis_valid = valid.all(axis=0)
            if document["plan"]["options"]["mask_clouds"]:
                analysis_valid &= np.isin(expected_scl, CLEAR)
            analysis = expected.copy()
            analysis[:, ~analysis_valid] = NODATA
            np.testing.assert_allclose(samples(output, points), analysis, rtol=0, atol=2e-7)
            result = {"product": entry["catalogue_id"], "source_index": index,
                      "positions": len(points), "partial_spectral_positions": len(partial[index]),
                      "band_values": int(expected.size), "max_absolute_error": max_error,
                      "exact_scl": True, "exact_nodata": True, "main_quality_mask": True,
                      "provider_sha256": hashes}
            report["sources"].append(result)
            save()
            print(entry["catalogue_id"], len(points), len(partial[index]), max_error, flush=True)
    report.update(status="passed", used_sources=len(report["sources"]),
                  positions=sum(item["positions"] for item in report["sources"]),
                  partial_spectral_positions=sum(item["partial_spectral_positions"] for item in report["sources"]),
                  band_values=sum(item["band_values"] for item in report["sources"]),
                  seconds=time.monotonic() - began)
    save()
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path, help="Completed research-mode public 月度处理记录.json")
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    args.report.parent.mkdir(parents=True, exist_ok=True)
    result = check(args.manifest.resolve(), args.report.resolve())
    print(json.dumps({key: result[key] for key in ("status", "used_sources", "positions", "partial_spectral_positions", "band_values", "seconds")}))


if __name__ == "__main__":
    main()
