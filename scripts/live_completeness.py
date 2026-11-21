"""Audit real downloads with saved local accounts; never export credentials."""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import re
import sys
import time
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

import numpy as np
import rasterio
from rasterio.features import geometry_mask
from rasterio.warp import reproject, Resampling, transform_bounds, transform_geom
from rasterio.windows import Window, from_bounds
from shapely.geometry import mapping

from live_public import B04_SAFE, BBOX, DEM90, ROOT, local_app


def raster_summary(path):
    with path.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    with rasterio.open(path) as ds:
        values = ds.read(masked=True)
        return {"path": str(path), "grid": [ds.width, ds.height], "epsg": ds.crs.to_epsg(),
                "pixel_size": list(ds.res), "bands": list(ds.descriptions), "dtype": list(ds.dtypes),
                "nodata": ds.nodata, "valid_pixels_per_band": [int(v.count()) for v in values],
                "finite": bool(np.isfinite(values.compressed()).all()),
                "sha256": digest}


def audit_research(file):
    """Independently verify delivered files, quality values and their numerical relationship."""
    paths = {role: Path(path) for role, path in file["bundle"].items()}
    main_path = Path(file["path"])
    receipt = json.loads(main_path.with_suffix(".starmap.json").read_text(encoding="utf-8"))
    source_table = json.loads(paths["sources"].read_text(encoding="utf-8"))
    assert source_table["sources"] and all(s["catalogue_id"].removesuffix(".SAFE") == B04_SAFE.removesuffix(".SAFE") for s in source_table["sources"])
    companions = {}
    for role, path in paths.items():
        meta = receipt["bundle"][role]
        with path.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        assert meta["file"] == path.name and meta["size"] == path.stat().st_size and meta["sha256"] == digest
        if role != "sources":
            companions[role] = raster_summary(path)
    with rasterio.open(main_path) as main, rasterio.open(paths["unmasked"]) as raw, rasterio.open(paths["scl"]) as quality, rasterio.open(paths["source"]) as provenance:
        assert main.count == raw.count == 10 and main.descriptions == raw.descriptions == ("B02", "B03", "B04", "B08", "B05", "B06", "B07", "B8A", "B11", "B12")
        assert quality.dtypes == ("uint8",) and quality.nodata == 255
        assert provenance.dtypes == ("uint16",) and provenance.nodata == 0
        assert all(ds.shape == main.shape and ds.transform == main.transform and ds.crs == main.crs for ds in (raw, quality, provenance))
        assert all(json.loads(ds.tags()["bands"]) == list(ds.descriptions) for ds in (main, raw, quality, provenance))
        values, masked, scl, source = raw.read(), main.read(), quality.read(1), provenance.read(1)
        present = np.isfinite(values) & (values != -9999)
        kept = present.all(axis=0) & np.isin(scl, (2, 4, 5, 6, 7, 11))
        expected = values.copy()
        expected[:, ~kept] = -9999
        assert np.array_equal(masked, expected)
        assert set(np.unique(scl).tolist()) <= {*range(12), 255}
        assert np.array_equal(source > 0, present.any(axis=0))
        indices = set(np.unique(source).tolist()) - {0}
        assert indices <= {s["index"] for s in source_table["sources"]}
        return {"companions": companions, "all_companion_checksums_verified": True,
                "analysis_valid_pixels": int(kept.sum()), "unmasked_pixels": int(present.any(axis=0).sum()),
                "retained_masked_pixels": int((present.any(axis=0) & ~kept).sum()),
                "scl_classes": sorted(set(np.unique(scl).tolist()) - {255}), "pixel_source_indices": sorted(indices)}


def compare_safe(path, optical_tests, geometry):
    """Use native SAFE JP2 and XML quantification, independently of both app transfer paths."""
    with zipfile.ZipFile(path) as bundle:
        names = bundle.namelist()
        xml, = [n for n in names if n.endswith("/MTD_MSIL2A.xml")]
        root = ET.fromstring(bundle.read(xml))
        quant, = [float(n.text) for n in root.iter() if n.tag.rsplit("}", 1)[-1] == "BOA_QUANTIFICATION_VALUE"]
        offsets = {int(n.attrib["band_id"]): float(n.text) for n in root.iter() if n.tag.rsplit("}", 1)[-1] == "BOA_ADD_OFFSET"}
    band_index = {band: i for i, band in enumerate(("B01", "B02", "B03", "B04", "B05", "B06", "B07", "B08", "B8A", "B09", "B10", "B11", "B12"))}
    assert quant == 10000 and all(i in offsets for band, i in band_index.items() if band != "B10")
    checked = []
    for output in optical_tests:
        if output.get("status") != "passed" or "path" not in output:
            continue
        raw_path = Path(output["bundle"]["unmasked"]) if output.get("bundle") else Path(output["path"])
        with rasterio.open(raw_path) as derived:
            aoi = transform_geom("EPSG:4326", derived.crs, mapping(geometry))
            inside = geometry_mask([aoi], out_shape=derived.shape, transform=derived.transform, invert=True)

            def layer(band, metres, categorical=False):
                member, = [n for n in names if n.endswith(f"_{band}_{metres}m.jp2")]
                url = "/vsizip/" + path.as_posix().removeprefix("//?/") + "/" + member
                with rasterio.open(url) as original:
                    bounds = transform_bounds(derived.crs, original.crs, *derived.bounds, densify_pts=21)
                    w = from_bounds(*bounds, transform=original.transform)
                    col, row = max(0, int(np.floor(w.col_off)) - 2), max(0, int(np.floor(w.row_off)) - 2)
                    end_col = min(original.width, int(np.ceil(w.col_off + w.width)) + 2)
                    end_row = min(original.height, int(np.ceil(w.row_off + w.height)) + 2)
                    window = Window(col, row, end_col - col, end_row - row)
                    source = original.read(1, window=window)
                    dest = np.zeros(derived.shape, dtype=source.dtype)
                    reproject(source, dest, src_transform=original.window_transform(window), src_crs=original.crs,
                              src_nodata=None if categorical else 0, dst_transform=derived.transform, dst_crs=derived.crs,
                              dst_nodata=0, resampling=Resampling.nearest)
                    return dest

            bands = []
            for index, band in enumerate(derived.descriptions, 1):
                metres = 10 if band in ("B02", "B03", "B04", "B08") else 20 if band not in ("B01", "B09") else 60
                dn = layer(band, metres)
                native_present = inside & (dn != 0)
                expected = ((dn.astype(np.float64) + offsets[band_index[band]]) / quant).astype(np.float32)
                actual = derived.read(index)
                present = np.isfinite(actual) & (actual != -9999)
                assert np.any(present) and not np.any(present & ~native_present)
                if output.get("bundle"):
                    assert np.array_equal(present, native_present), f"Unmasked {band} lost native pixels."
                error = np.abs(expected[present].astype(np.float64) - actual[present].astype(np.float64))
                max_error = float(error.max())
                assert max_error <= 2e-6, f"{band} disagrees with official SAFE (max error {max_error})."
                bands.append({"band": band, "native_resolution_m": metres, "pixels_compared": int(present.sum()),
                              "maximum_absolute_error": max_error, "mean_absolute_error": float(error.mean())})
            result = {"output": str(raw_path), "provider_test": output["label"], "bands": bands,
                      "quantification": quant, "offsets_verified_from_product_xml": True}
            if output.get("bundle"):
                expected_scl = layer("SCL", 20, categorical=True).astype(np.uint8)
                expected_scl[~inside] = 255
                with rasterio.open(output["bundle"]["scl"]) as quality:
                    assert np.array_equal(quality.read(1), expected_scl), "SCL differs from the complete SAFE."
                result["scl_exact_match"] = True
            checked.append(result)
    assert checked
    return {"reference_safe": str(path), "independent_native_jp2_comparisons": checked}


def safe_inventory(path):
    """Independently decode the ZIP and XML, including metadata omitted by regional exports."""
    with zipfile.ZipFile(path) as bundle:
        assert bundle.testzip() is None, "A SAFE member failed its ZIP CRC."
        names = bundle.namelist()
        assert names and all(n.startswith(B04_SAFE + "/") for n in names)
        spectra = sorted({m[1] for name in names if (m := re.search(r"_(B(?:0[1-9]|8A|11|12))_\d+m\.jp2$", name))})
        assert set(spectra) == {"B01", "B02", "B03", "B04", "B05", "B06", "B07", "B08", "B8A", "B09", "B11", "B12"}
        layers = {key: sum(bool(re.search(r"_" + key + r"_\d+m\.jp2$", name)) for name in names)
                  for key in ("SCL", "AOT", "WVP")}
        assert all(layers.values())
        qi = [name for name in names if "/QI_DATA/" in name and not name.endswith("/")]
        assert any("CLDPRB" in name for name in qi) and any("SNWPRB" in name for name in qi)
        xml_names = [name for name in names if name.endswith(("/MTD_MSIL2A.xml", "/MTD_TL.xml"))]
        assert any(name.endswith("/MTD_MSIL2A.xml") for name in xml_names)
        assert any(name.endswith("/MTD_TL.xml") for name in xml_names)
        roots = [ET.fromstring(bundle.read(name)) for name in xml_names]
        tags = [elt.tag.rsplit("}", 1)[-1] for root in roots for elt in root.iter()]
        angles = {tag: tags.count(tag) for tag in ("Sun_Angles_Grid", "Mean_Sun_Angle", "Viewing_Incidence_Angles_Grids", "Mean_Viewing_Incidence_Angle")}
        assert all(angles.values())
        return {"path": str(path), "member_count": len(names), "zip_crc_verified": True,
                "spectral_bands": spectra, "auxiliary_layer_members": layers,
                "quality_members": len(qi), "metadata_xml": xml_names, "angle_nodes": angles,
                "members": names}


def cloud_geometry(path):
    """Find an interior cloud patch in the same SAFE for real unmasked-retention testing."""
    from shapely.geometry import box
    with zipfile.ZipFile(path) as archive:
        member, = [n for n in archive.namelist() if n.endswith("_SCL_20m.jp2")]
    with rasterio.open("/vsizip/" + path.as_posix().removeprefix("//?/") + "/" + member) as scl:
        for row_off in range(700, scl.height - 700, 512):
            for col_off in range(700, scl.width - 700, 512):
                window = Window(col_off, row_off, min(512, scl.width - 700 - col_off), min(512, scl.height - 700 - row_off))
                values = scl.read(1, window=window)
                if min(values.shape) < 9:
                    continue
                cloud = np.isin(values, (8, 9, 10)).astype(np.int32)
                integral = np.pad(cloud.cumsum(0, dtype=np.int32).cumsum(1, dtype=np.int32), ((1, 0), (1, 0)))
                sums = integral[9:, 9:] - integral[:-9, 9:] - integral[9:, :-9] + integral[:-9, :-9]
                matches = np.argwhere(sums == 81)
                if not len(matches):
                    continue
                row, col = matches[len(matches) // 2] + 4
                x, y = scl.xy(window.row_off + int(row), window.col_off + int(col))
                return box(*transform_bounds(scl.crs, "EPSG:4326", x - 40, y - 40, x + 40, y + 40))
    raise AssertionError("The reference SAFE has no suitable interior cloud patch.")


def check(out, exe=None, *, research=False, source_zip=None, providers=True):
    out.mkdir(parents=True, exist_ok=True)
    report_path = out / "completeness-report.json"
    report = {"verified_utc": dt.datetime.now(dt.timezone.utc).isoformat(), "research_mode": research,
              "bbox_wgs84": BBOX, "exact_safe": B04_SAFE, "tests": [], "status": "running"}

    def save():
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")

    with local_app(exe, out) as (origin, session):
        def post(route, body):
            with session.post(origin + "/api/" + route, json=body, timeout=(5, 180)) as response:
                if response.status_code != 200:
                    raise RuntimeError(response.json().get("error") or f"Local {route}: HTTP {response.status_code}")
                return response.json()

        def run(route, body, label, timeout=1800):
            job = post(route, body)["job_id"]
            deadline, last_print = time.monotonic() + timeout, 0
            while time.monotonic() < deadline:
                with session.get(origin + "/api/job", params={"id": job}, timeout=5) as response:
                    response.raise_for_status()
                    state = response.json()
                if state["status"] == "completed":
                    return state["result"]
                if state["status"] not in ("running", "cancelling"):
                    raise RuntimeError(state.get("error") or state.get("message") or state["status"])
                if time.monotonic() - last_print > 15:
                    print(f"{label}: {state.get('message', 'running')}", flush=True)
                    last_print = time.monotonic()
                time.sleep(.5)
            post("cancel", {"job_id": job})
            raise RuntimeError(f"Timed out during {label}; cancellation requested.")

        for _ in range(180):
            boot = session.get(origin + "/api/bootstrap", timeout=5).json()
            if not boot["accounts"]["restoring"]:
                break
            time.sleep(.5)
        else:
            raise RuntimeError("Saved-account restoration did not finish.")
        original_output = boot["preferences"]["output_dir"]
        report.update(version=boot["version"], executable=str(exe) if exe else "source",
                      saved_accounts={key: {field: bool(value.get(field)) for field in ("connected", "verified", "saved")}
                                      for key, value in boot["accounts"]["providers"].items()})
        save()
        area = post("aoi", {"coordinates": BBOX})["geometry"]

        def request(profile, criteria, options, expected, geometry=area):
            found = run("search", {"profile": profile, "geometry": geometry, **criteria}, profile + " search", 360)
            if profile == "s2-process":
                matches = [row for row in found["scenes"] if {p["id"].removesuffix(".SAFE") for p in row["products"]} == {expected.removesuffix(".SAFE")}]
            else:
                matches = [row for row in found["scenes"] if row["title"].removesuffix(".SAFE") == expected.removesuffix(".SAFE") or row["key"] == expected]
            assert len(matches) == 1, "The exact requested product was not found."
            req = {"profile": profile, "geometry": geometry, "criteria": criteria, "query_id": found["query"]["id"],
                   "scene_keys": [matches[0]["key"]], "options": options, "output_dir": str(out / profile)}
            plan = post("plan", req)
            return req, plan

        def attempt(label, work):
            print(f"Real transfer: {label}", flush=True)
            row = {"label": label, "status": "running"}
            report["tests"].append(row)
            save()
            try:
                row.update(work(), status="passed")
            except (RuntimeError, AssertionError, OSError, ValueError, zipfile.BadZipFile) as error:
                row.update(status="failed", error=str(error) or type(error).__name__)
                print(f"{label}: {row['error']}", flush=True)
            save()

        try:
            def optical(profile, geometry=area, cloud=False):
                dates = {"start": "2024-06-10", "end": "2024-06-10", "max_cloud": 100}
                options = {"mode": "research" if research else "rgbnir", "resolution": 10, "mask_clouds": True, "max_cloud": 100}
                req, plan = request(profile, dates, options, B04_SAFE, geometry=geometry)
                if cloud:
                    req["output_dir"] = str(out / (profile + "-cloud"))
                result = run("download", req, profile)
                file, = result["files"]
                assert file["status"] == "completed", "Use a fresh directory for real-transfer verification."
                path = Path(file["path"])
                proof = raster_summary(path)
                assert proof["finite"] and proof["epsg"] == 32650
                if not cloud:
                    assert proof["grid"] == [44, 57]
                if research:
                    proof["bundle"] = file.get("bundle")
                    proof["research_audit"] = audit_research(file)
                    if cloud:
                        assert proof["research_audit"]["analysis_valid_pixels"] == 0
                        assert proof["research_audit"]["retained_masked_pixels"] > 0
                        assert set(proof["research_audit"]["scl_classes"]) <= {8, 9, 10}
                        proof["real_cloud_retention_verified"] = True
                else:
                    assert len(proof["bands"]) == 4
                    proof["missing_from_regional_output"] = ["B01", "B05", "B06", "B07", "B8A", "B09", "B11", "B12", "SCL", "unmasked_reflectance", "pixel_source", "SAFE_metadata"]
                proof["manifest"] = result["manifest"]
                assert run("download", req, profile + " repeat")["files"][0]["status"] == "skipped"
                proof["repeat_verified"] = True
                return proof

            attempt("OAuth Process regional export", lambda: optical("s2-process"))
            attempt("Public regional export", lambda: optical("s2-earth-search"))
            if providers:
                def s3():
                    criteria = {"start": None, "end": None, "collection": "cop-dem-glo-90-dged-cog", "radar_mode": ""}
                    req, plan = request("cdse-original", criteria, {"mode": "original", "transport": "cdse-s3"}, DEM90)
                    assert plan["requires"] == ["cdse-s3"]
                    result = run("download", req, "CDSE S3")
                    file, = result["files"]
                    assert file["status"] == "completed"
                    proof = raster_summary(Path(file["path"]))
                    assert proof["grid"] == [1200, 1200] and proof["epsg"] == 4326 and proof["dtype"] == ["float32"]
                    proof["manifest"] = result["manifest"]
                    return proof

                attempt("Saved CDSE S3 actual transfer", s3)
            if source_zip:
                attempt("Independent same-product SAFE inventory", lambda: safe_inventory(source_zip))
            elif providers:
                def original():
                    criteria = {"start": "2024-06-10", "end": "2024-06-10", "collection": "sentinel-2-l2a", "radar_mode": ""}
                    req, plan = request("cdse-original", criteria, {"mode": "original", "transport": "odata"}, B04_SAFE)
                    assert plan["requires"] == ["odata"]
                    result = run("download", req, "Complete Sentinel-2 SAFE", 7200)
                    file, = result["files"]
                    assert file["status"] == "completed"
                    path = Path(file["path"])
                    proof = safe_inventory(path)
                    row, = json.loads(Path(result["manifest"]).read_text(encoding="utf-8"))["items"]
                    with path.open("rb") as stream:
                        digest = hashlib.file_digest(stream, "sha256").hexdigest()
                    assert digest == row["sha256"]
                    proof.update(bytes=path.stat().st_size, sha256=digest, manifest=result["manifest"],
                                 provider_checksum_verified=row.get("provider_checksum_verified"))
                    return proof

                attempt("Saved OData complete SAFE actual transfer", original)
            safe = next((t for t in report["tests"] if t.get("zip_crc_verified") and t["status"] == "passed"), None)
            if safe:
                attempt("Independent regional pixel comparison with official SAFE", lambda: compare_safe(Path(safe["path"]),
                        [t for t in report["tests"] if "regional export" in t["label"]], parse_geometry()))
                if research:
                    cloud_aoi = cloud_geometry(Path(safe["path"]))
                    report["real_cloud_bbox_wgs84"] = list(cloud_aoi.bounds)
                    for profile in ("s2-process", "s2-earth-search"):
                        attempt(profile + " real all-cloud backup", lambda profile=profile: optical(profile, mapping(cloud_aoi), True))
                    attempt("Independent cloudy pixel comparison with official SAFE", lambda: compare_safe(Path(safe["path"]),
                            [t for t in report["tests"] if "real all-cloud backup" in t["label"]], cloud_aoi))
            if providers:
                def gedi():
                    from live_gedi import audit_download
                    geometry = post("aoi", {"coordinates": "116,36,117,37"})["geometry"]
                    criteria = {"start": "2020-06-01", "end": "2020-06-30", "collection": "gedi04-a-003"}
                    found = run("search", {"profile": "gedi-earthdata", "geometry": geometry, **criteria}, "NASA CMR search", 360)
                    available = [s for s in found["scenes"] if s["gedi"]["available"]]
                    assert available
                    chosen = min(available, key=lambda s: s["gedi"].get("estimated_bytes") or float("inf"))
                    req = {"profile": "gedi-earthdata", "geometry": geometry, "criteria": criteria, "query_id": found["query"]["id"],
                           "scene_keys": [chosen["key"]], "options": {"mode": "aligned", "resolution": 100}, "output_dir": str(out / "gedi")}
                    plan = post("plan", req)
                    req["plan_fingerprint"] = plan["fingerprint"]
                    result = run("download", req, "NASA protected GEDI", 7200)
                    return {"product": chosen["title"], "result_folder": result["folder"],
                            "statistics": result["statistics"], "independent_audit": audit_download(result)}

                attempt("Saved Earthdata protected GEDI actual transfer", gedi)
            report["status"] = "passed" if all(t["status"] == "passed" for t in report["tests"]) else "needs_attention"
        finally:
            post("preferences", {"output_dir": original_output})
            report["output_preference_restored"] = True
            save()
    print(json.dumps({"report": str(report_path), "status": report["status"],
                      "tests": [{"label": t["label"], "status": t["status"]} for t in report["tests"]]}, ensure_ascii=False), flush=True)
    return report


def parse_geometry():
    from shapely.geometry import box
    return box(*map(float, BBOX.split(",")))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--exe", type=Path)
    parser.add_argument("--research", action="store_true")
    parser.add_argument("--source-zip", type=Path)
    parser.add_argument("--regional-only", action="store_true")
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    result = check(args.output_dir.resolve(), args.exe.resolve(strict=True) if args.exe else None,
                   research=args.research, source_zip=args.source_zip.resolve(strict=True) if args.source_zip else None,
                   providers=not args.regional_only)
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    raise SystemExit(main())
