"""Download every selected scene, mosaic offline, then clip the complete month."""
from __future__ import annotations

import copy
import datetime as dt
import hashlib
import json
import math
import shutil
from collections import OrderedDict
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import rasterio
from pyproj import Transformer
from rasterio.enums import Resampling
from rasterio.transform import Affine, from_origin
from rasterio.vrt import WarpedVRT
from rasterio.windows import Window
from shapely import normalize, transform
from shapely.geometry import box, mapping
from shapely.ops import unary_union

from . import VERSION
from .batch_workflow import _safe_error, _write_json
from .download import download_scenes
from .download_queue import ProductProgress, run_product_queue
from .errors import Cancelled, UserError, keep_download_result
from .http import check_cancel
from .local_paths import io_path
from .mosaic import PUBLIC, NATIVE, _identities, _rows, build_mosaic_plan, download_mosaic
from .native_mosaic import NATIVE_RESOLUTION, native_metadata, native_source_plan
from .planning import build_plan
from .public_download import read_on_grid
from .raw_download import download_original, raw_plan
from .research_bundle import NODATA, SCL_NODATA, bundle_paths
from .scene_identity import expected_products
from .safe_zip import PROFILE as ZIP, download_options as zip_download_options, prepare_zip_source, storage_plan as zip_storage_plan, zip_source_plan
from .spatial_selection import footprint, polygon_area

PIPELINE = "download-scenes-mosaic-clip"
SCHEMA = 1
STAGES = (("download_scenes", "下载全部整景"), ("local_mosaic", "本地整月拼接"), ("clip_region", "裁剪完整研究区"))


def _product_id(scene):
    if "public" in scene:
        products = scene.get("products", [])
        identifier = scene["public"]["source_id"]
        if len(products) != 1 or products[0].get("id") != identifier:
            raise UserError("公开整景的完整产品标识与本月所选来源不一致。")
        return identifier
    return expected_products(scene)[0]["id"]


def _raw_options(options, *, export=False):
    return {**options, "mode": "research", "bands": list(options["bands"]),
            "output_bands": list(options["bands"]), "count": len(options["bands"]),
            "dtype": "float32", "nodata": NODATA, "layout": "series" if export else "mosaic",
            "mask_clouds": False if export else options["mask_clouds"],
            "allocation_mode": options["mode"]}


def _grid_footprint(grid):
    affine = Affine(*grid["transform"])
    height, width = grid["shape"]
    rectangle = box(affine.c, affine.f + height * affine.e, affine.c + width * affine.a, affine.f)
    converter = Transformer.from_crs(grid["epsg"], 4326, always_xy=True)
    return transform(rectangle.segmentize(1000), converter.transform, interleaved=False)


def _public_source_plan(scene, options):
    raw = scene["public"].get("native_raw")
    if raw is None:
        raise UserError("本月所选影像缺少可核验的完整 COG 波段或源 SHA-256。请重新检索，或选择官方 S3 区域路线。")
    if raw.get("names") != [scene["public"]["source_id"]]:
        raise UserError("整景原件的完整产品标识与本月所选影像不一致。")
    assets = {asset["id"]: asset for asset in raw["assets"]}
    for band in options["bands"] + ["SCL"]:
        advertised = scene["public"]["assets"][band]
        grid = (assets.get(band) or {}).get("raster") or {}
        if any(grid.get(field) != advertised[field] for field in
               ("epsg", "shape", "transform", "dtype", "nodata", "scale", "offset")):
            raise UserError("整景原件的波段网格或定标与所选公开影像不一致。")
    original = {**scene, "raw": raw}
    return raw_plan(footprint(scene), [original], {
        "collection": raw["collection"], "mode": "assets", "transport": "public",
        "assets": list(options["bands"]) + ["SCL", "tileInfo.json", "metadata.xml", "product_metadata.xml"]})


def build_monthly_scene_plan(geometry, scenes, options, *, cancel=None):
    """Plan sizes and identity only; no remote pixels or mosaic allocation are read."""
    check_cancel(cancel)
    rows = _rows(geometry, scenes, options)
    identities = _identities(rows, options)
    city = build_plan(geometry, options, 1, tile_size=1024, cancel=cancel)
    source_plans, extents = [], []
    source_bytes, source_unknown, pu = 0, False, 0.0
    raw_options = _raw_options(options, export=True)
    for scene in rows:
        check_cancel(cancel)
        if options["profile"] == PUBLIC:
            planned = _public_source_plan(scene, options)
            extents.append(_grid_footprint(scene["public"]["assets"][options["bands"][0]]))
        elif options["profile"] == ZIP:
            planned = zip_source_plan(scene, options)
            extents.append(footprint(scene))
        elif options["profile"] == NATIVE:
            planned = native_source_plan(scene, options)
            extents.append(footprint(scene))
        else:
            planned = build_plan(footprint(scene), raw_options, 1, tile_size=1024,
                                 cancel=cancel, epsg=city[0]["epsg"])
            extents.append(footprint(scene))
        source_plans.append(planned)
        if isinstance(planned, tuple):
            source_bytes += planned[0]["bytes_per_scene"]
            pu += planned[0]["estimated_pu"]
        else:
            source_bytes += (planned.get("estimated_bytes") or 0) if options["profile"] == ZIP else sum(
                target.get("bytes") or 0 for target in planned["targets"])
            source_unknown |= any(not target.get("bytes") for target in planned["targets"])
    whole_options = _raw_options(options)
    whole_estimate = build_plan(unary_union(extents), whole_options, 1, tile_size=1024,
                                cancel=cancel, epsg=city[0]["epsg"])[0]
    selected_footprint = unary_union([footprint(row) for row in rows])
    area = polygon_area(geometry)
    coverage = min(100.0, 100 * polygon_area(geometry.intersection(selected_footprint)) / area)
    signature = {"schema": SCHEMA, "pipeline": PIPELINE, "geometry": mapping(normalize(geometry)),
                 "options": options, "sources": identities,
                 "source_plans": [entry[0] if isinstance(entry, tuple) else entry for entry in source_plans]}
    fingerprint = hashlib.sha256(json.dumps(signature, sort_keys=True, separators=(",", ":"),
                                             allow_nan=False).encode()).hexdigest()[:16]
    storage_id = fingerprint
    if options["profile"] == ZIP:
        storage_signature = {**signature, "options": {k: v for k, v in options.items() if k not in ("download_engine", "download_workers")},
                             "source_plans": [zip_storage_plan(entry) for entry in source_plans]}
        storage_id = hashlib.sha256(json.dumps(storage_signature, sort_keys=True, separators=(",", ":"),
                                              allow_nan=False).encode()).hexdigest()[:16]
    plan = {**city[0], "kind": "monthly-scenes", "fingerprint": fingerprint,
            "pipeline": PIPELINE, "pipeline_schema": SCHEMA,
            "folder_name": "S2_monthly_" + storage_id, "source_count": len(rows),
            **({"storage_fingerprint": storage_id} if options["profile"] == ZIP else {}),
            "product_keys": [row["key"] for row in rows],
            "dates": sorted({row["date"] for row in rows}), "coverage": {"coverage_percent": coverage},
            "estimated_pu": pu if options["profile"] == "s2-process" else None,
            "native_source_bytes": source_bytes, "native_size_unknown": source_unknown,
            "whole_mosaic_bytes": whole_estimate["bytes_per_scene"],
            "staging_required_bytes": math.ceil(source_bytes * 1.1 +
                (whole_estimate["bytes_per_scene"] + city[0]["bytes_per_scene"]) * 3.2 + 128 * 1024**2),
            "source_objects": sum(len(entry["targets"]) if isinstance(entry, dict) else entry[0]["requests"]
                                  for entry in source_plans),
            "requires": ["odata"] if options["profile"] == ZIP else
                        ["cdse-s3"] if options["profile"] == NATIVE else
                        ["sentinel-hub"] if options["profile"] == "s2-process" else [],
            "source_note": "先下载本月全部已选整景；再只读本地文件拼接；最后按原始完整研究区裁剪。"}
    return plan, {"rows": rows, "source_plans": source_plans, "city": city,
                  "whole_options": whole_options, "export_options": raw_options}


class _LocalSceneReader:
    """Only opens already downloaded files; it has no HTTP client or fetch fallback."""
    def __init__(self, records, options, plan, cancel):
        self.records, self.options, self.plan, self.cancel = records, options, plan, cancel
        self.opened = OrderedDict()
        self.versions = {}
        self.evidence = {key: record.get("metadata", {}) | {"manifest": record["result"]["manifest"],
                          "local_files": {band: str(path) for band, path in record["paths"].items()}}
                         for key, record in records.items()}
        for key, record in records.items():
            for band, path in record["paths"].items():
                receipt = path.with_suffix(".starmap.json")
                if receipt.is_file():
                    meta = json.loads(receipt.read_text(encoding="utf-8"))
                    self.versions[key, band] = (meta["sha256"], path.stat().st_size)
        self.provenance = {"workflow": PIPELINE, "processing_stage": "local_mosaic",
                           "original_scene_files": self.evidence}

    def _open(self, scene):
        key = scene["key"]
        if key not in self.records:
            raise UserError("本月仍有影像未下载完成，不能开始本地拼接。")
        record = self.records[key]
        if not record["paths"]:
            return {}, record
        if key not in self.opened:
            while len(self.opened) >= 2:
                self.opened.popitem(last=False)[1][0].close()
            stack = ExitStack()
            try:
                datasets = {band: stack.enter_context(rasterio.open(path))
                            for band, path in record["paths"].items()}
                if self.options["profile"] == PUBLIC:
                    for band, ds in datasets.items():
                        asset = scene["public"]["assets"][band]
                        if (ds.driver != "GTiff" or ds.count != 1 or ds.dtypes != (asset["dtype"],)
                                or ds.crs is None or ds.crs.to_epsg() != asset["epsg"]
                                or list(ds.shape) != asset["shape"] or ds.nodata != asset["nodata"]
                                or not ds.transform.almost_equals(Affine(*asset["transform"]), precision=1e-6)):
                            raise UserError("已下载整景的波段网格与所选目录不一致。")
                elif self.options["profile"] in (NATIVE, ZIP):
                    for band, ds in datasets.items():
                        grid = record["metadata"]["grids"][NATIVE_RESOLUTION[band]]
                        if (ds.count != 1 or ds.crs is None or ds.crs.to_epsg() != grid["epsg"]
                                or ds.shape != (grid["height"], grid["width"])
                                or ds.transform != Affine(*grid["transform"])):
                            raise UserError("已下载官方原始波段与已校验 XML 的网格不一致。")
                self.opened[key] = stack, datasets
            except BaseException:
                stack.close()
                raise
        self.opened.move_to_end(key)
        return self.opened[key][1], record

    def extent(self, scene):
        datasets, _ = self._open(scene)
        if not datasets:
            return footprint(scene)
        ds = datasets.get("unmasked", datasets.get(self.options["bands"][0]))
        return _grid_footprint({"epsg": ds.crs.to_epsg(), "shape": list(ds.shape),
                                "transform": list(ds.transform[:6])})

    def read(self, scene, tile, inside):
        check_cancel(self.cancel)
        datasets, record = self._open(scene)
        if not datasets:
            return (np.full((len(self.options["bands"]), tile.height, tile.width), NODATA, np.float32),
                    np.full((tile.height, tile.width), SCL_NODATA, np.uint8), {"sources": []})
        grid = from_origin(tile.bounds[0], tile.bounds[3], self.plan["resolution"], self.plan["resolution"])
        target_crs = f"EPSG:{self.plan['epsg']}"
        if self.options["profile"] == PUBLIC:
            inputs = {band: read_on_grid((ds, SimpleNamespace(failure=None), scene["public"]["assets"][band]),
                       tile.bounds, grid, target_crs, tile.height, tile.width, self.cancel,
                       preserve_nodata=band == "SCL") for band, ds in datasets.items()}
            values = np.stack([np.where(np.isfinite(inputs[band]), inputs[band], NODATA)
                               for band in self.options["bands"]])
            quality = np.where(np.isfinite(inputs["SCL"]), inputs["SCL"], SCL_NODATA)
            methods = ["safe_id"]
        elif self.options["profile"] in (NATIVE, ZIP):
            values, quality = [], None
            metadata = record["metadata"]
            for band, ds in datasets.items():
                check_cancel(self.cancel)
                with WarpedVRT(ds, crs=target_crs, transform=grid, width=tile.width, height=tile.height,
                        resampling=Resampling.nearest, src_nodata=SCL_NODATA if band == "SCL" else 0,
                        nodata=SCL_NODATA if band == "SCL" else 0) as warped:
                    dn = warped.read(1)
                if band == "SCL":
                    quality = dn.astype(np.uint8)
                else:
                    reflectance = (dn.astype(np.float32) + np.float32(metadata["offsets"][band])) / np.float32(metadata["quantification"])
                    reflectance[dn == 0] = NODATA
                    values.append(reflectance)
            values, methods = np.stack(values), ["safe_id"]
        else:
            with WarpedVRT(datasets["unmasked"], crs=target_crs, transform=grid, width=tile.width,
                    height=tile.height, resampling=Resampling.nearest, src_nodata=NODATA, nodata=NODATA) as warped:
                values = warped.read()
            with WarpedVRT(datasets["SCL"], crs=target_crs, transform=grid, width=tile.width,
                    height=tile.height, resampling=Resampling.nearest,
                    src_nodata=SCL_NODATA, nodata=SCL_NODATA) as warped:
                quality = warped.read(1)
            methods = record["matches"]
        product = _product_id(scene)
        return values, quality, {"sources": [{"catalogue_id": product, "match": methods[0]}] if methods else [],
                                  "local_source_manifest": record["result"]["manifest"]}

    def close(self):
        while self.opened:
            self.opened.popitem(last=False)[1][0].close()


class _MonthMosaicReader:
    """Clip the completed local month; never re-read an original scene or reproject."""
    def __init__(self, result, plan, records, cancel):
        self.result, self.plan, self.cancel = result, plan, cancel
        target = Path(result["files"][0]["path"])
        paths = bundle_paths(target)
        self.stack = ExitStack()
        try:
            self.datasets = {role: self.stack.enter_context(rasterio.open(paths[role]))
                             for role in ("unmasked", "scl", "source")}
        except BaseException:
            self.stack.close()
            raise
        table = json.loads(paths["sources"].read_text(encoding="utf-8"))
        receipt = json.loads(target.with_suffix(".starmap.json").read_text(encoding="utf-8"))
        self.sources = {row["catalogue_id"]: row for row in table["sources"]}
        self.evidence = table.get("native_sources", {})
        self.provenance = {"workflow": PIPELINE, "processing_stage": "clip_region",
                           "clip_method": "exact aligned windows of completed local month; no resampling",
                           "whole_month_mosaic": {"path": str(target), "manifest": result["manifest"],
                                                   "sha256": receipt["sha256"]},
                           "original_scene_manifests": [record["result"]["manifest"] for record in records.values()]}
        ds = self.datasets["unmasked"]
        expected = plan["resolution"]
        if ds.crs.to_epsg() != plan["epsg"] or ds.res != (expected, expected):
            self.close()
            raise UserError("整月拼接与城市裁剪的坐标系或分辨率不一致。")

    def read(self, scene, tile, inside):
        check_cancel(self.cancel)
        ds = self.datasets["unmasked"]
        col = (tile.bounds[0] - ds.transform.c) / self.plan["resolution"]
        row = (ds.transform.f - tile.bounds[3]) / self.plan["resolution"]
        if abs(col - round(col)) > 1e-5 or abs(row - round(row)) > 1e-5:
            raise UserError("城市裁剪未与整月影像的像元网格对齐，未进行额外重采样。")
        window = Window(round(col), round(row), tile.width, tile.height)
        values = ds.read(window=window, boundless=True, fill_value=NODATA)
        quality = self.datasets["scl"].read(1, window=window, boundless=True, fill_value=SCL_NODATA)
        source = self.datasets["source"].read(1, window=window, boundless=True, fill_value=0)
        product = _product_id(scene)
        entry = self.sources.get(product)
        if entry is None:
            raise UserError("整月拼接来源表没有该月所选产品，不能裁剪。")
        other = source != entry["index"]
        values[:, other], quality[other] = NODATA, SCL_NODATA
        methods = entry["observed_matches"]
        return values, quality, {"sources": [{"catalogue_id": product, "match": methods[0]}] if methods else [],
                                  "whole_month_mosaic": self.provenance["whole_month_mosaic"]["path"]}

    def close(self):
        self.stack.close()


def download_monthly_scenes(geometry, scenes, options, output_dir, *, auth=None, s3_auth=None,
                            raw_auth=None, cancel=None, progress=None, planned=None, browser=None):
    plan, inputs = planned or build_monthly_scene_plan(geometry, scenes, options, cancel=cancel)
    rows = copy.deepcopy(inputs["rows"])
    folder = io_path(output_dir) / plan["folder_name"]
    folder.mkdir(parents=True, exist_ok=True)
    manifest = folder / "月度处理记录.json"
    records, files, reader, clip_reader = {}, [], None, None
    stages = [{"id": key, "title": title, "status": "pending"} for key, title in STAGES]
    document = {"software": VERSION, "pipeline": PIPELINE, "schema": SCHEMA,
                "fingerprint": plan["fingerprint"], "status": "running", "plan": plan,
                "selected_products": [_product_id(row) for row in rows],
                "stages": stages, "sources": [], "whole_mosaic": None, "clipped_result": None}
    phase = 0

    def now():
        return dt.datetime.now(dt.timezone.utc).isoformat()

    def start(index):
        nonlocal phase
        check_cancel(cancel)
        if index and stages[index - 1]["status"] != "completed":
            raise UserError("上一阶段尚未完成并通过校验，不能进入下一阶段。")
        phase = index
        stages[index].update(status="running", started_at=now())
        _write_json(manifest, document)
        report(STAGES[index][1])

    def finish(result=None):
        stages[phase].update(status="completed", completed_at=now(), result=result)
        _write_json(manifest, document)

    def report(message, fraction=0, detail=None):
        if progress:
            progress(f"{phase + 1}/3 · {STAGES[phase][1]} · {message}",
                     (phase + min(1, max(0, fraction or 0))) / 3,
                     {"month_stage": STAGES[phase][0], "month_stage_title": STAGES[phase][1],
                      "completed_scene_products": len(records), "total_scene_products": len(rows),
                      **(detail or {})})

    def result(status="completed"):
        return {"status": status, "folder": str(folder), "manifest": str(manifest), "pipeline": PIPELINE,
                "plan": plan, "files": files, "empty_scenes": [] if files else ["mosaic"],
                "used_dates": (document["clipped_result"] or {}).get("used_dates", []),
                "whole_mosaic": document["whole_mosaic"], "stages": stages,
                "original_scene_manifests": [record["result"]["manifest"] for record in records.values()]}

    _write_json(manifest, document)
    try:
        if shutil.disk_usage(folder).free < plan["staging_required_bytes"]:
            raise UserError(f"整景、整月拼接与裁剪的输出及临时空间预计至少需要 {plan['staging_required_bytes'] / 1024**3:.1f} GB。请选择空间足够的输出目录。")
        start(0)
        zip_workers = min(zip_download_options(options)["download_workers"], len(rows)) if options["profile"] == ZIP else 1
        zip_meter = ProductProgress([scene["title"] for scene in rows], report, folder=str(folder), workers=zip_workers)
        downloaded_sources = {}

        def receive(index, pair, signal):
            scene, source_plan = pair
            check_cancel(signal)
            source_progress = (lambda message, fraction=0, detail=None: zip_meter.update(index, message, fraction, detail)) if options["profile"] == ZIP else lambda message, fraction=0, detail=None: report(
                f"{index + 1}/{len(rows)} 景 · {scene['tiles'][0]} · {scene['date']} · {message}",
                (index + fraction) / len(rows), detail)
            metadata, matches = {}, ["safe_id"]
            if options["profile"] == ZIP:
                downloaded = download_original(footprint(scene), source_plan, str(folder / "原始ZIP"), raw_auth,
                                               cancel=signal, progress=source_progress, browser=browser)
                paths = {}
            elif options["profile"] in (PUBLIC, NATIVE):
                downloaded = download_original(footprint(scene), source_plan, str(folder / "原始影像"), None,
                                               s3_auth=s3_auth, cancel=signal, progress=source_progress)
                paths = {target["asset"]: Path(file["path"]) for target, file in
                         zip(source_plan["targets"], downloaded["files"], strict=True)}
                if options["profile"] == NATIVE:
                    metadata = native_metadata(paths, source_plan["targets"], scene, signal)
                    paths = {band: paths[f"{band}_{NATIVE_RESOLUTION[band]}m"] for band in options["bands"] + ["SCL"]}
                else:
                    paths = {band: paths[band] for band in options["bands"] + ["SCL"]}
            else:
                downloaded = download_scenes(footprint(scene), [scene], inputs["export_options"],
                    str(folder / "整景导出"), auth, cancel=signal, progress=source_progress, planned=source_plan)
                if downloaded["files"]:
                    entry = downloaded["files"][0]
                    paths = {"unmasked": Path(entry["bundle"]["unmasked"]), "SCL": Path(entry["bundle"]["scl"])}
                    table = json.loads(Path(entry["bundle"]["sources"]).read_text(encoding="utf-8"))
                    matches = table["sources"][0]["observed_matches"]
                else:
                    paths, matches = {}, []
            return {"paths": paths, "result": downloaded, "metadata": metadata, "matches": matches}

        def received(index, record, error):
            scene = rows[index]
            if record is not None:
                records[scene["key"]] = record
                downloaded_sources[index] = {"product": _product_id(scene), "status": "verified",
                                             "manifest": record["result"]["manifest"], "files": record["result"]["files"]}
            elif getattr(error, "download_result", None):
                partial = error.download_result
                downloaded_sources[index] = {"product": _product_id(scene), "status": "cancelled" if isinstance(error, Cancelled) else "failed",
                                             "manifest": partial["manifest"], "files": partial["files"]}
            document["sources"] = [downloaded_sources[i] for i in sorted(downloaded_sources)]
            _write_json(manifest, document)
            if options["profile"] == ZIP:
                zip_meter.complete(index, record, error)

        source_items = list(zip(rows, inputs["source_plans"], strict=True))
        if options["profile"] == ZIP:
            run_product_queue(source_items, receive, zip_workers, cancel=cancel, on_result=received)
        else:
            for index, pair in enumerate(source_items):
                received(index, receive(index, pair, cancel), None)
        if len(records) != len(rows):
            raise UserError("本月未完成全部所选影像的下载和校验，不能拼接。")
        finish({"selected_scene_products": len(rows), "verified_scene_products": len(records)})

        start(1)
        if options["profile"] == ZIP:
            for index, scene in enumerate(rows):
                record = prepare_zip_source(scene, records[scene["key"]]["result"], options,
                    folder / ".download-cache" / "SAFE本地波段", cancel=cancel,
                    progress=lambda message, fraction=0, i=index: report(
                        f"{i + 1}/{len(rows)} 景 · {message}", .1 * (i + fraction) / len(rows)))
                actual = record["scene"]
                if options.get("selected_month") and actual["date"][:7] != options["selected_month"]:
                    raise UserError("ZIP 元数据中的真实瓦片采集日期不属于所选月份，未混入其他月份。")
                if (options.get("selection_start") and actual["date"] < options["selection_start"]
                        or options.get("selection_end") and actual["date"] > options["selection_end"]):
                    raise UserError("ZIP 元数据中的真实瓦片采集日期超出所选范围，未采用该来源。")
                rows[index] = actual
                records[scene["key"]] = record
        reader = _LocalSceneReader(records, options, plan, cancel)
        for scene in rows:
            extent = reader.extent(scene)
            scene["products"][0]["footprint"] = mapping(extent)
        whole_geometry = unary_union([footprint(scene) for scene in rows])
        whole_tile_size = 512 if options["profile"] == PUBLIC and options["resolution"] == 60 else 1024
        whole_plan = build_mosaic_plan(whole_geometry, rows, inputs["whole_options"], tile_size=whole_tile_size,
                                      cancel=cancel, epsg=plan["epsg"])
        reader.plan = whole_plan[0]
        whole = download_mosaic(whole_geometry, rows, inputs["whole_options"], str(folder / "整月拼接"),
                                 reader=reader, planned=whole_plan, cancel=cancel, progress=report)
        document["whole_mosaic"] = whole
        finish(whole)
        start(2)
        if not whole["files"]:
            document["clipped_result"] = {"files": [], "used_dates": []}
            finish({"empty": True})
        else:
            city_plan = build_mosaic_plan(geometry, rows, options, tile_size=1024, cancel=cancel, epsg=plan["epsg"])
            if any(city_plan[0][key] != plan[key] for key in ("width", "height", "epsg", "bounds", "resolution")):
                raise UserError("城市裁剪的网格与原月度计划不一致。")
            clip_reader = _MonthMosaicReader(whole, city_plan[0], records, cancel)
            clipped = download_mosaic(geometry, rows, options, str(folder / "研究区裁剪"),
                                     reader=clip_reader, planned=city_plan, cancel=cancel, progress=report)
            document["clipped_result"], files = clipped, clipped["files"]
            finish(clipped)
        check_cancel(cancel)
        document.update(status="completed", completed_at=now())
        report("本月整景下载、整月拼接和完整边界裁剪完成", 1)
        return result()
    except Exception as error:
        status = "cancelled" if isinstance(error, Cancelled) else "failed"
        stages[phase].update(status=status, error=_safe_error(error), stopped_at=now())
        document.update(status=status, stopped_at=now())
        partial = getattr(error, "download_result", None)
        if partial:
            stages[phase]["partial_result"] = partial
        keep_download_result(error, files, folder, manifest, plan)
        error.download_result.update(result(status))
        raise
    finally:
        if clip_reader is not None:
            clip_reader.close()
        if reader is not None:
            reader.close()
        _write_json(manifest, document)
