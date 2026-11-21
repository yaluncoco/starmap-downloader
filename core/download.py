from __future__ import annotations

import datetime as dt
import json
import os
import shutil
import uuid
from pathlib import Path
from contextlib import ExitStack

import numpy as np
import rasterio
from rasterio.enums import ColorInterp
from rasterio.features import geometry_mask
from rasterio.transform import from_origin
from rasterio.windows import Window
from shapely.geometry import mapping

from . import VERSION
from .errors import Cancelled, UserError, keep_download_result
from .local_paths import io_path
from .http import HttpClient, check_cancel
from .integrity import audit_raster, checked_local_receipt, publish_file
from .planning import build_plan
from .process_request import fetch_tile, process_payload, resampling
from .scene_identity import expected_products, source_fingerprint
from .research_bundle import (ResearchBundle, bundle_paths, checked_bundle, has_bundle_files, is_research,
                              make_block, process_block, transfer_options)


def _save_json(path, value):
    temp = path.with_name(path.name + ".tmp." + uuid.uuid4().hex[:8])
    try:
        temp.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def _check_grid(dataset, epsg, transform, width, height, options):
    return (dataset.width == width and dataset.height == height and dataset.count == options["count"]
            and dataset.dtypes == (options["dtype"],) * options["count"]
            and dataset.crs is not None and dataset.crs.to_epsg() == epsg
            and dataset.transform.almost_equals(transform, precision=0.00001))


def _existing_matches(path, plan, transform, identity):
    if not path.is_file():
        return False
    try:
        with rasterio.open(path) as dataset:
            tags = dataset.tags()
            options = plan["options"]
            valid = int(tags.get("valid_pixels", "-1"))
            raw = int(tags.get("unmasked_valid_pixels", "0"))
            valid_count_ok = (0 <= valid <= dataset.width * dataset.height and 0 < raw <= dataset.width * dataset.height) if is_research(options) else 0 < valid <= dataset.width * dataset.height
            if not (_check_grid(dataset, plan["epsg"], transform, plan["width"], plan["height"], options)
                    and dataset.descriptions == tuple(options["output_bands"]) and dataset.nodata == options["nodata"]
                    and tags.get("request_fingerprint") == plan["fingerprint"] and tags.get("source_fingerprint") == identity
                    and valid_count_ok):
                return False
            # Catch incomplete/corrupt files without reading the whole raster on every rerun.
            dataset.read(1, window=Window(dataset.width - 1, dataset.height - 1, 1, 1))
            return True
    except (rasterio.errors.RasterioError, ValueError, OSError):
        return False


def _parts(tile, reference_transform, plan, options, tile_file):
    try:
        with rasterio.open(tile_file) as source:
            if not _check_grid(source, plan["epsg"], reference_transform, tile.width, tile.height, options):
                raise UserError("官方返回影像的网格、坐标系或数据类型不符合请求，已停止保存。")
            return source.read()
    except rasterio.errors.RasterioError:
        raise UserError("官方返回的文件不是有效 GeoTIFF，请重试。") from None


def download_scenes(geometry, scenes, options, output_dir, auth, *, cancel=None, progress=None, planned=None):
    if options.get("layout") == "mosaic":
        from .mosaic import download_mosaic
        return download_mosaic(geometry, scenes, options, output_dir, auth=auth,
                               cancel=cancel, progress=progress, planned=planned)
    base = io_path(output_dir)
    if not base.is_absolute():
        raise UserError("请选择完整的输出文件夹路径。")
    if not auth.connected:
        raise UserError("请先连接 API 凭据。")
    check_cancel(cancel)
    plan, tiles, projected = planned if planned is not None else build_plan(geometry, options, len(scenes))
    folder = base / f"S2L2A_{plan['fingerprint']}"
    folder.mkdir(parents=True, exist_ok=True)
    aoi_shape = mapping(projected)
    resolution, bounds = plan["resolution"], plan["bounds"]
    grid_transform = from_origin(bounds[0], bounds[3], resolution, resolution)
    entries = []
    needed = 0
    for scene in scenes:
        check_cancel(cancel)
        products = expected_products(scene)
        identity = source_fingerprint(products)
        target = folder / f"{scene['key']}_{options['mode']}_{resolution}m_{identity}.tif"
        receipt = target.with_suffix(".starmap.json")
        expected = {"file": target.name, "request_fingerprint": plan["fingerprint"], "source_fingerprint": identity}
        cached = None
        existing = has_bundle_files(target) if is_research(options) else target.exists() or receipt.exists()
        if existing:
            cached = checked_local_receipt(target, receipt, expected, cancel)
            if not _existing_matches(target, plan, grid_transform, identity):
                raise UserError("输出目录中已有同名但不完整或参数不匹配的文件，请移动该文件后重试。")
            if is_research(options):
                checked_bundle(target, cached, plan, grid_transform, identity, cancel)
        entries.append((scene, products, identity, target, receipt, expected, cached))
        needed += int(cached is None)
    if needed:
        max_tile_pixels = max(tile.width * tile.height for tile in tiles if tile.intersects)
        tile_bytes = max_tile_pixels * transfer_options(options)["count"] * (4 if options["dtype"] == "float32" else 1)
        required = plan["bytes_per_scene"] * needed * 1.2 + tile_bytes * 4 + 12 * 1024 ** 2
        if shutil.disk_usage(folder).free < required:
            raise UserError(f"按未压缩影像及下载临时空间估算，输出磁盘至少需要约 {required / 1024 ** 2:.0f} MB 可用空间。请换一个文件夹，或分批下载。")

    _save_json(folder / "研究区.geojson", {"type": "Feature", "properties": {}, "geometry": mapping(geometry)})
    now = dt.datetime.now(dt.timezone.utc)
    manifest_path = folder / f"下载记录_{now.strftime('%Y%m%d_%H%M%SZ')}_{uuid.uuid4().hex[:6]}.json"
    manifest = {
        "software": f"星图下载器 {VERSION}", "source": "Copernicus Data Space / Sentinel Hub Process API",
        "created_at": now.isoformat(), "plan": plan, "aoi": mapping(geometry), "items": [], "status": "running",
        "source_note": (("来源图中的正整数对应 .sources.json 的稳定产品索引；先选择数据有效的产品，再按 SCL 掩膜，分析与未掩膜影像使用同一来源。" if is_research(options) else
                         "目录 products 是请求白名单；区块 sources 是处理服务返回的可用瓦片，不代表每个像元的来源。") +
                        "safe_id 表示匹配完整产品标识，mgrs_and_utc 表示匹配唯一的 MGRS 瓦片和采集秒，后者无法区分目录未公开的处理版本。"),
    }
    _save_json(manifest_path, manifest)
    total, completed = plan["requests"], 0
    results = []
    detail = {"folder": str(folder), "total_scenes": len(scenes)}

    def report(message, **extra):
        if progress:
            progress(message, completed / total, {**detail, **extra})

    http = HttpClient()
    try:
        report("输出目录已就绪，准备处理选中的过境影像")
        for scene_index, (scene, products, identity, target, receipt, expected, cached) in enumerate(entries):
            check_cancel(cancel)
            label = f"{scene['platform']} · {scene['date']} {scene['time']} UTC"
            record = {"scene": scene, "path": str(target), "source_fingerprint": identity, "status": "running", "blocks": []}
            manifest["items"].append(record)
            if cached is not None:
                completed += plan["tiles_per_scene"]
                record.update(status="skipped", **cached)
                results.append({"path": str(target), "scene": scene["key"], "status": "skipped",
                                **({"bundle": {role: str(path) for role, path in bundle_paths(target).items() if role != "analysis"}} if is_research(options) else {})})
                _save_json(manifest_path, manifest)
                report(f"已存在匹配的影像，跳过 {label}", completed_scenes=scene_index + 1, file=str(target))
                continue

            bundle = ResearchBundle(target, plan, grid_transform, identity) if is_research(options) else None
            partial = bundle.partials["analysis"] if bundle else target.with_name(target.stem + "." + uuid.uuid4().hex[:8] + ".partial.tif")
            tile_file = folder / (".block_" + uuid.uuid4().hex + ".tif")
            fill = 0 if options["nodata"] is None else options["nodata"]
            valid_pixel_count, matched_blocks = 0, 0
            profile = {"driver": "GTiff", "width": plan["width"], "height": plan["height"], "count": options["count"], "dtype": options["dtype"],
                       "crs": f"EPSG:{plan['epsg']}", "transform": grid_transform, "nodata": options["nodata"], "compress": "DEFLATE",
                       "predictor": 3 if options["dtype"] == "float32" else 2, "tiled": True, "blockxsize": 512, "blockysize": 512, "BIGTIFF": "IF_SAFER"}
            try:
                with ExitStack() as outputs:
                    writer = bundle.open(outputs) if bundle else outputs.enter_context(rasterio.open(partial, "w", **profile))
                    writer.descriptions = tuple(options["output_bands"])
                    if options["mode"] == "rgb":
                        writer.colorinterp = (ColorInterp.red, ColorInterp.green, ColorInterp.blue, ColorInterp.alpha)
                    scene_blocks = 0
                    for tile in tiles:
                        check_cancel(cancel)
                        window = Window(tile.col, tile.row, tile.width, tile.height)
                        values, blocks, counts = None, None, None
                        if tile.intersects:
                            scene_blocks += 1
                            report(f"处理 {scene_index + 1}/{len(scenes)} 景 · {label} · 区块 {scene_blocks}/{plan['tiles_per_scene']}", completed_scenes=scene_index)
                            metadata = fetch_tile(http, auth, process_payload(tile, plan, scene, options, products=products), tile_file,
                                                  options=options, products=products, cancel=cancel, status=report)
                            reference_transform = from_origin(tile.bounds[0], tile.bounds[3], resolution, resolution)
                            values = _parts(tile, reference_transform, plan, transfer_options(options), tile_file)
                            inside = geometry_mask([aoi_shape], out_shape=(tile.height, tile.width), transform=reference_transform, invert=True)
                            if bundle:
                                blocks, counts = process_block(values, inside, options, products, metadata)
                                values = blocks["analysis"]
                            else:
                                values[:, ~inside] = fill
                            if options["mode"] == "rgb":
                                data = values[3] == 255
                                if np.any((values[3] != 0) & ~data) or np.any(values[:3, ~data]):
                                    raise UserError("处理服务返回的透明通道和颜色内容不一致，未保存。")
                            else:
                                present = np.isfinite(values) & (values != options["nodata"])
                                data = np.all(present, axis=0)
                                if np.any(~np.isfinite(values)) or np.any(np.any(present, axis=0) != data):
                                    raise UserError("处理服务返回无效数值或多波段有效范围不一致，未保存。")
                            valid = int(np.count_nonzero(data))
                            if valid and not metadata["sources"]:
                                raise UserError("处理服务返回未能对应所选过境的像元，已停止保存该景。")
                            valid_pixel_count += valid
                            matched_blocks += bool(metadata["sources"])
                            record["blocks"].append({"bounds": list(tile.bounds), "width": tile.width, "height": tile.height, "valid_pixels": valid,
                                                     **({"unmasked_valid_pixels": counts["unmasked"]} if bundle else {}), **metadata})
                            tile_file.unlink(missing_ok=True)
                            completed += 1
                        if bundle:
                            if blocks is None:
                                shape = (tile.height, tile.width)
                                blocks, counts = make_block(np.full((options["count"], *shape), fill, dtype=np.float32),
                                                            np.full(shape, 255), np.zeros(shape), np.zeros(shape, dtype=bool), options)
                            bundle.write(blocks, counts, window)
                        else:
                            if values is None:
                                values = np.full((options["count"], tile.height, tile.width), fill, dtype=options["dtype"])
                            writer.write(values, window=window)
                    writer.update_tags(request_fingerprint=plan["fingerprint"], source_fingerprint=identity, acquisition_utc=scene["datetime"],
                                       source="CDSE Sentinel Hub Process API / Sentinel-2 L2A", bands=json.dumps(options["output_bands"]),
                                       mode=options["mode"], cloud_mask=str(options["mask_clouds"]), resampling=resampling(options),
                                       acquisition_window="STAC granule UTC time +/- 30 seconds; input tiles allowlisted",
                                       reflectance_units="unitless" if options["dtype"] == "float32" and options["mode"] != "ndvi" else "",
                                       rgb_display_scale="2.5 * 255, clipped to 0..255" if options["mode"] == "rgb" else "",
                                       valid_pixels=str(valid_pixel_count), matched_blocks=str(matched_blocks))
                    if bundle:
                        bundle.tag({**writer.tags(), "harmonize_values": "False"})
                check_cancel(cancel)
                if not matched_blocks:
                    raise UserError(f"处理服务没有返回可与所选目录匹配的瓦片（{label}），未保存该景。请重新检索或尝试其他日期。")
                if not valid_pixel_count and (bundle is None or not bundle.counts["unmasked"]):
                    record["status"] = "empty"
                    record["note"] = "可匹配目录瓦片，但研究区没有有效像元；未保存空白影像。"
                    _save_json(manifest_path, manifest)
                    report(f"跳过空影像 · {label}（无有效像元）", completed_scenes=scene_index + 1)
                    continue
                if not _existing_matches(partial, plan, grid_transform, identity):
                    raise UserError("最终影像校验失败，临时文件未发布。")
                report("检查最终 GeoTIFF 区块和 SHA-256", completed_scenes=scene_index)
                if bundle:
                    observed = {product["id"]: set() for product in products}
                    for block in record["blocks"]:
                        for source in block["sources"]:
                            observed[source["catalogue_id"]].add(source["match"])
                    provenance = {"sources": [{"index": index, "catalogue_id": product["id"], "mgrs_tile": product["tile"],
                                                "acquisition_utc": product["utc"], "observed_matches": sorted(observed[product["id"]])}
                                               for index, product in enumerate(products, 1)],
                                  "blocks": record["blocks"], "provider": manifest["source"],
                                  "reflectance_conversion": "Process API BOA reflectance; harmonizeValues=false",
                                  "identity_note": "safe_id matches the complete SAFE identifier; mgrs_and_utc identifies a unique tile/acquisition second but cannot distinguish an unreported processing version."}
                    audited = bundle.publish(expected, provenance, cancel)
                    if not valid_pixel_count:
                        record["note"] = "分析影像已全部掩膜；保留未掩膜光谱、SCL 和来源图，可重新筛选。"
                else:
                    audited = audit_raster(partial, options, valid_pixel_count, cancel)
                    check_cancel(cancel)
                    publish_file(partial, target)
                    try:
                        _save_json(receipt, {**expected, **audited})
                    except Exception:
                        target.unlink(missing_ok=True)
                        raise
                record.update(status="completed", **audited)
                results.append({"path": str(target), "scene": scene["key"], "status": "completed",
                                **({"bundle": bundle.result_paths()} if bundle else {})})
                _save_json(manifest_path, manifest)
                report(f"完成 {scene_index + 1}/{len(scenes)} 景 · {target.name}", completed_scenes=scene_index + 1, file=str(target))
            except Cancelled:
                record["status"] = "cancelled"
                raise
            except Exception as error:
                record["status"] = "failed"
                record["note"] = str(error) if isinstance(error, UserError) else type(error).__name__
                raise
            finally:
                tile_file.unlink(missing_ok=True)
                partial.unlink(missing_ok=True)
                if bundle:
                    bundle.cleanup()
        manifest["status"] = "completed" if results else "empty"
        return {"folder": str(folder), "files": results, "manifest": str(manifest_path), "plan": plan,
                "empty_scenes": [item["scene"]["key"] for item in manifest["items"] if item["status"] == "empty"]}
    except Cancelled as error:
        manifest["status"] = "cancelled"
        keep_download_result(error, results, folder, manifest_path, plan)
        raise
    except Exception as error:
        manifest["status"] = "failed"
        keep_download_result(error, results, folder, manifest_path, plan)
        raise
    finally:
        try:
            _save_json(manifest_path, manifest)
        finally:
            http.close()
