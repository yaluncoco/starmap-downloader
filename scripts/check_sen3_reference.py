"""Independently check native geolocation and thermal/QA values in the Beijing SEN3 sample."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import h5py
import numpy as np


LST_PRODUCT = "S3B_SL_2_LST____20240305T132901_20240305T133201_20240306T120123_0179_090_224_0540_PS2_O_NT_004.SEN3"
BBOX = (116.350, 39.900, 116.355, 39.905)
REF_CELL = (1000, 1303)  # Independently read from the same raw archive release before integration.


def check(receipt):
    record = json.loads(receipt.read_text(encoding="utf-8"))
    assert record["status"] == "completed"
    files = {item["file"]: Path(item["path"]) for item in record["items"] if item["product"] == LST_PRODUCT}
    assert set(("geodetic_in.nc", "LST_in.nc", "flags_in.nc")) <= files.keys()

    def scalar(data, key):
        return np.asarray(data.attrs[key]).item()

    def angle(data):
        stored = data[...]
        valid = (stored != scalar(data, "_FillValue")) & (stored >= scalar(data, "valid_min")) & (stored <= scalar(data, "valid_max"))
        return stored.astype(np.float64)*scalar(data, "scale_factor") + scalar(data, "add_offset"), valid

    west, south, east, north = BBOX
    centre_lat, centre_lon = np.radians([(south+north)/2, (west+east)/2])
    with h5py.File(files["geodetic_in.nc"]) as geo:
        lat, lat_ok = angle(geo["latitude_in"])
        lon, lon_ok = angle(geo["longitude_in"])
        assert lat.shape == lon.shape == (1200, 1500)
        valid = lat_ok & lon_ok
        geo_count = int(np.count_nonzero(valid))
        centres = int(np.count_nonzero(valid & (lon >= west) & (lon <= east) & (lat >= south) & (lat <= north)))
        a, b = np.radians(lat), np.radians(lon)
        hav = np.sin((a-centre_lat)/2)**2 + np.cos(a)*np.cos(centre_lat)*np.sin((b-centre_lon)/2)**2
        hav[~valid] = np.inf
        cell = tuple(int(x) for x in np.unravel_index(np.argmin(hav), hav.shape))
        assert cell == REF_CELL and centres == 0
        assert abs(lat[cell] - 39.905833) < 1e-7 and abs(lon[cell] - 116.349742) < 1e-7
        distance = float(2 * 6371008.8 * np.arcsin(np.sqrt(hav[cell])))
        assert 438.9 < distance < 439.1
    with h5py.File(files["LST_in.nc"]) as temperature, h5py.File(files["flags_in.nc"]) as flags:
        v = temperature["LST"]
        assert v.shape == flags["confidence_in"].shape == lat.shape
        raw, exception, confidence, clouds = int(v[cell]), int(temperature["exception"][cell]), int(flags["confidence_in"][cell]), int(flags["cloud_in"][cell])
        assert raw != scalar(v, "_FillValue") and (raw, exception, confidence, clouds) == (-23257, 0, 16648, 640)
        scaled = raw * scalar(v, "scale_factor") + scalar(v, "add_offset")
    return {"product": LST_PRODUCT, "bbox": BBOX, "grid_shape": list(lat.shape), "geolocated_pixels": geo_count,
            "pixel_centres_in_aoi": centres, "nearest_cell_zero_based": cell, "nearest_wgs84_lat_lon": [float(lat[cell]), float(lon[cell])],
            "distance_from_aoi_centre_m": distance, "raw_lst_dn": raw, "scale_and_offset_lst_K": scaled,
            "raw_exception": exception, "raw_confidence": confidence, "raw_cloud_flags": clouds,
            "note": "Not a cropped output or a claim that the AOI has a valid cloud-free observation; no QA filter was applied."}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("live_sen3_report", type=Path)
    args = parser.parse_args()
    src = args.live_sen3_report.resolve(strict=True)
    report = json.loads(src.read_text(encoding="utf-8"))
    case = next(c for c in report["cases"] if c["product"] == LST_PRODUCT and c["scope"] == "complete")
    evidence = check(Path(case["receipt"]))
    evidence["version"] = report["version"]
    summary = json.dumps(evidence, ensure_ascii=False, indent=2)
    src.with_name("sen3-lst-geolocation-check.json").write_text(summary + "\n", encoding="utf-8")
    print(summary)


if __name__ == "__main__":
    main()
