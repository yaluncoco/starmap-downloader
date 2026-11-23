"""Clip selected public COGs into tiled UTM GeoTIFFs, without substituting a scene."""
from __future__ import annotations

import datetime as dt
import json
import math
import re
import shutil
from contextlib import ExitStack
from pathlib import Path
from uuid import uuid4

import numpy as np
import rasterio
from rasterio.enums import ColorInterp, Resampling
from rasterio.features import geometry_mask
from rasterio.transform import Affine, from_origin
from rasterio.warp import reproject, transform_bounds
from rasterio.windows import Window, from_bounds
from shapely.geometry import mapping

from . import VERSION
from .download import _existing_matches, _save_json
from .download_cache import ArrayCache
from .errors import Cancelled, UserError, keep_download_result
from .http import HttpClient, check_cancel
from .local_paths import io_path
from .integrity import audit_raster, checked_local_receipt, publish_file
from .planning import build_plan
from .public_catalogue import DEM, ES_COLLECTION, S2_PUBLIC
from .public_rtc import RTC_COLLECTION, RTC_PROVIDER, S1_RTC
from .public_token import PlanetaryAccess
from .range_file import RangeFile
from .scene_identity import source_fingerprint
from .research_bundle import (ResearchBundle, checked_bundle, has_bundle_files, is_research,
                              make_block, public_block)


def keys_for(options):
    return options["bands"] + (["SCL"] if options["mask_clouds"] or is_research(options) else [])


def public_plan(geometry, scenes, options, *, cancel=None):
    check_cancel(cancel)
    if options.get("layout") == "mosaic":
        from .mosaic import build_mosaic_plan
        return build_mosaic_plan(geometry, scenes, options, cancel=cancel)
    for scene in scenes:
        check_cancel(cancel)
        missing = set(keys_for(options)) - scene["public"]["assets"].keys()
        if missing:
            raise UserError(f"所选瓦片缺少可核对的公开波段：{', '.join(sorted(missing))}。请更改导出内容或选择其他产品。")
    plan, tiles, projected = build_plan(geometry, options, len(scenes), tile_size=512, cancel=cancel)
    plan.update(kind="public", estimated_pu=None, requests=None, source_objects=len(keys_for(options)) * len(scenes),
                folder_name=f"{options['profile'].replace('-', '_')}_{plan['fingerprint']}")
    if options["profile"] == S1_RTC:
        plan.update(collection=RTC_COLLECTION, processing="linear_gamma0", verification="pinned ETag and Azure MD5 per fetched range")
    return plan, tiles, projected


def _open_source(stack, http, asset, cancel, rtc_access=None, *, cache_dir=None, status=None):
    settings = {}
    if cache_dir is not None:
        settings.update(cache_dir=cache_dir, status=status, page_size=1024**2, cache_pages=8, coalesce_pages=4)
    if rtc_access is not None:
        if asset.get("pc_scope") != "rtc":
            raise UserError("Sentinel-1 RTC 原始对象与裁剪路线不匹配。")
        settings.update(access=rtc_access, range_md5=True, expected_size=asset["object_bytes"],
                        page_size=1024**2, cache_pages=8, coalesce_pages=1)
    stream = stack.enter_context(RangeFile(http, asset["url"], cancel, **settings))
    stream.defer_errors = True
    try:
        # Only one named object is supported; do not expose a second network path to GDAL.
        def opener(name, mode="rb", *args, **kwargs):
            if name != asset["url"] or mode not in ("r", "rb"):
                raise FileNotFoundError
            stream.seek(0)
            return stream
        source = stack.enter_context(rasterio.open(asset["url"], opener=opener))
    except rasterio.errors.RasterioError:
        if stream.failure:
            raise stream.failure
        raise UserError("公开源不是可读取的 GeoTIFF。") from None
    if stream.failure:
        raise stream.failure
    transform = source.transform
    if (source.driver != "GTiff" or source.count != 1 or source.dtypes[0] != asset["dtype"]
            or not source.crs or source.crs.to_epsg() != asset["epsg"] or transform.b or transform.d
            or transform.a <= 0 or transform.e >= 0 or not all(math.isfinite(x) for x in transform[:6])):
        raise UserError("公开源坐标系、波段数或数据类型与目录不符。")
    if rtc_access is not None and (source.scales != (1.0,) or source.offsets != (0.0,)):
        raise UserError("Sentinel-1 RTC 的源缩放与线性 γ⁰ 约定不符。")
    if "shape" in asset:
        if (list(source.shape) != asset["shape"] or source.nodata != asset["nodata"]
                or not transform.almost_equals(Affine(*asset["transform"]), precision=1e-6)):
            raise UserError("公开源像元网格或 NoData 与目录不符。")
    else:
        x0, y0, x1, y1 = asset["bounds"]
        found = source.bounds
        if (not 0 < source.width <= 4000 or not 0 < source.height <= 4000 or
                any(abs(l - r) > abs(p) * 1.1 for l, r, p in zip(found, (x0, y0, x1, y1), (transform.a, transform.e)*2))):
            raise UserError("公开 DEM 地理范围与所选瓦片不一致。")
    return source, stream, asset


def read_on_grid(source_info, bounds, target_transform, crs, height, width, cancel=None, *, preserve_nodata=False):
    source, stream, asset = source_info
    check_cancel(cancel)
    dest = np.full((height, width), np.nan, dtype=np.float32)
    try:
        edge = transform_bounds(crs, source.crs, *bounds, densify_pts=21)
        clipped = (max(edge[0], source.bounds.left), max(edge[1], source.bounds.bottom),
                   min(edge[2], source.bounds.right), min(edge[3], source.bounds.top))
        if clipped[0] >= clipped[2] or clipped[1] >= clipped[3]:
            return dest
        window = from_bounds(*clipped, transform=source.transform)
        row, col = max(0, math.floor(window.row_off)-2), max(0, math.floor(window.col_off)-2)
        end_y = min(source.height, math.ceil(window.row_off + window.height)+2)
        end_x = min(source.width, math.ceil(window.col_off + window.width)+2)
        if (end_y-row)*(end_x-col) > 30_000_000:
            raise UserError("此研究区的单块读取过大；请提高源与输出的分辨率匹配度或分批下载。")
        win = Window(col, row, end_x-col, end_y-row)
        loaded = source.read(1, window=win, masked=not preserve_nodata).astype(np.float32)
        values = loaded.filled(np.nan) if np.ma.isMaskedArray(loaded) else loaded
        if stream.failure:
            raise stream.failure
        if asset["scale"] != 1 or asset["offset"] != 0:
            values *= np.float32(asset["scale"])
            values += np.float32(asset["offset"])
        reproject(values, dest, src_transform=source.window_transform(win), src_crs=source.crs, src_nodata=np.nan,
                  dst_transform=target_transform, dst_crs=crs, dst_nodata=np.nan, resampling=Resampling.nearest)
        return dest
    except rasterio.errors.RasterioError:
        if stream.failure:
            raise stream.failure
        raise UserError("公开源栅格区块读取或重投影失败，未发布影像。") from None


def _output(inputs, valid, options):
    valid = valid.copy()
    selected = [inputs[band] for band in options["bands"]]
    if options["mode"] == "rtc":
        out = np.stack(selected)
        present = valid[np.newaxis, :, :] & np.isfinite(out) & (out != options["nodata"])
        out[~present] = options["nodata"]
        return out, int(np.count_nonzero(np.any(present, axis=0)))
    for data in selected:
        valid &= np.isfinite(data)
    if options["mask_clouds"]:
        valid &= np.isin(inputs["SCL"], (2, 4, 5, 6, 7, 11))
    if options["mode"] == "rgb":
        out = np.zeros((4, *valid.shape), dtype=np.uint8)
        for index, band in enumerate(selected):
            out[index, valid] = np.rint(np.clip(band[valid] * 637.5, 0, 255)).astype(np.uint8)
        out[3, valid] = 255
    else:
        fill = options["nodata"]
        if options["mode"] == "ndvi":
            red, nir = selected
            denom = nir + red
            valid &= np.abs(denom) > 1e-6
            out = np.full((1, *valid.shape), fill, dtype=np.float32)
            np.divide(nir-red, denom, out=out[0], where=valid)
            valid &= np.isfinite(out[0]) & (out[0] != fill)
        else:
            out = np.stack(selected)
            valid &= np.all(out != fill, axis=0)
        out[:, ~valid] = fill
    return out, int(np.count_nonzero(valid))


def download_public(geometry, scenes, options, output_dir, *, cancel=None, progress=None, planned=None):
    if options.get("layout") == "mosaic":
        from .mosaic import download_mosaic
        return download_mosaic(geometry, scenes, options, output_dir,
                               cancel=cancel, progress=progress, planned=planned)
    check_cancel(cancel)
    base = io_path(output_dir)
    if not base.is_absolute():
        raise UserError("请选择绝对输出文件夹。")
    plan, tiles, projected = planned or public_plan(geometry, scenes, options)
    folder = base / plan["folder_name"]
    folder.mkdir(parents=True, exist_ok=True)
    uid = uuid4().hex[:10]
    aoi_shape = mapping(projected)
    resolution, bounds = plan["resolution"], plan["bounds"]
    grid = from_origin(bounds[0], bounds[3], resolution, resolution)
    crs = f"EPSG:{plan['epsg']}"
    records, files, empty = [], [], []
    items = []
    for row in scenes:
        src = row["public"]
        assets = {key: src["assets"][key] for key in keys_for(options)}
        source_id = src["source_id"]
        identity = source_fingerprint({"id": source_id, "assets": assets})
        label = re.sub(r"[^A-Za-z0-9_.-]", "_", source_id.removesuffix(".SAFE"))[:100]
        target = folder / f"{label}_{options['mode']}_{resolution}m_{identity}.tif"
        items.append((row, assets, source_id, identity, target))
    needed = sum(not item[-1].exists() for item in items)
    if needed and shutil.disk_usage(folder).free < plan["bytes_per_scene"] * needed * 3.2 + 16 * 1024**2:
        raise UserError("按未压缩影像估算，输出磁盘空间不足。请缩小区域或分批下载。")
    manifest_path = folder / f"下载记录_{uid}.json"
    profile = options["profile"]
    if profile == S1_RTC:
        provider, collection = RTC_PROVIDER, RTC_COLLECTION
    elif profile == S2_PUBLIC:
        provider, collection = "Element 84 Earth Search / Sentinel-2 C1 L2A", ES_COLLECTION
    else:
        provider, collection = "Copernicus DEM AWS Open Data", f"GLO-{DEM[profile][0]}"
    manifest = {"software": VERSION, "source": provider, "collection": collection,
                "created_at": dt.datetime.now(dt.timezone.utc).isoformat(), "plan": plan, "status": "running", "items": records}
    _save_json(folder / "研究区.geojson", {"type": "Feature", "properties": {}, "geometry": mapping(geometry)})
    _save_json(manifest_path, manifest)
    http = HttpClient()
    rtc_access = PlanetaryAccess(http, cancel, scope="rtc") if profile == S1_RTC else None
    try:
        total = len(items)
        for index, (row, assets, source_id, identity, target) in enumerate(items):
            check_cancel(cancel)
            def report(message, fraction=0.0):
                if progress:
                    progress(f"{index+1}/{total} · {row['platform']} · {message}", (index+fraction)/total,
                             {"folder": str(folder), "completed_scenes": index, "total_scenes": total})
            record = {"scene": row["key"], "catalogue_product_id": source_id, "acquisition_utc": row["datetime"],
                      "catalogue_coverage_percent": row["coverage_percent"], "assets": assets, "status": "running"}
            records.append(record)
            receipt = target.with_suffix(".starmap.json")
            expected = {"file": target.name, "request_fingerprint": plan["fingerprint"], "source_fingerprint": identity}
            bundle = ResearchBundle(target, plan, grid, identity) if is_research(options) else None
            cache_root = folder / ".download-cache" / identity
            blocks_cache = ArrayCache(cache_root, {"schema": 1, "grid": plan["fingerprint"], "source": identity},
                                      bundle.specs if bundle else {"analysis": options})
            partial = bundle.partials["analysis"] if bundle else folder / f".{target.stem}.{uid}.partial.tif"
            try:
                existing = has_bundle_files(target) if bundle else target.exists() or receipt.exists()
                if existing:
                    report("检查已下载内容")
                    meta = checked_local_receipt(target, receipt, expected, cancel)
                    if not _existing_matches(target, plan, grid, identity):
                        raise UserError("已有 GeoTIFF 参数不匹配，请移动该文件后重试。")
                    if bundle:
                        checked_bundle(target, meta, plan, grid, identity, cancel)
                    record.update(status="skipped", path=str(target), **meta)
                else:
                    report("核对公开源波段及版本")
                    valid_count = 0
                    fill = 0 if options["mode"] == "rgb" else options["nodata"]
                    with rasterio.Env(GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR", GDAL_PAM_ENABLED="NO"):
                        with ExitStack() as stack:
                            sources = {band: _open_source(stack, http, asset, cancel, rtc_access,
                                                         cache_dir=cache_root / "ranges", status=report) for band, asset in assets.items()}
                            with ExitStack() as outputs:
                                out = bundle.open(outputs) if bundle else outputs.enter_context(rasterio.open(
                                    partial, "w", driver="GTiff", width=plan["width"], height=plan["height"], count=options["count"],
                                    dtype=options["dtype"], crs=crs, transform=grid, nodata=options["nodata"], compress="DEFLATE",
                                    predictor=3 if options["dtype"] == "float32" else 2, tiled=True, blockxsize=512, blockysize=512, BIGTIFF="IF_SAFER"))
                                if options["mode"] == "rgb":
                                    out.colorinterp = (ColorInterp.red, ColorInterp.green, ColorInterp.blue, ColorInterp.alpha)
                                for b, band in enumerate(options["output_bands"], 1):
                                    out.set_band_description(b, band)
                                    if options["mode"] == "dem":
                                        out.set_band_unit(b, "m")
                                    elif rtc_access is not None:
                                        out.set_band_unit(b, "1")
                                        out.update_tags(b, polarization=band, backscatter="gamma0", scale_domain="linear")
                                for j, tile in enumerate(tiles):
                                    check_cancel(cancel)
                                    window = Window(tile.col, tile.row, tile.width, tile.height)
                                    block_grid = out.window_transform(window)
                                    inside = geometry_mask([aoi_shape], out_shape=(tile.height, tile.width), transform=block_grid, invert=True) if tile.intersects else None
                                    key = [tile.row, tile.col, tile.height, tile.width]
                                    saved_block = blocks_cache.load(key, (tile.height, tile.width), cancel)
                                    valid = 0
                                    if saved_block:
                                        saved_arrays, saved_detail = saved_block
                                        if saved_detail["versions"] != {band: [src[1].etag, src[1].size] for band, src in sources.items()}:
                                            raise UserError("已保存裁剪区块的源版本不一致，未混用影像。请重新检索。")
                                        valid = saved_detail["valid"]
                                        if bundle:
                                            blocks, counts = saved_arrays, saved_detail["counts"]
                                        else:
                                            values = saved_arrays["analysis"]
                                    elif inside is None or not inside.any():
                                        if bundle:
                                            shape = (tile.height, tile.width)
                                            blocks, counts = make_block(np.full((options["count"], *shape), fill, dtype=np.float32),
                                                                        np.full(shape, 255), np.zeros(shape), np.zeros(shape, dtype=bool), options)
                                        else:
                                            values = np.full((options["count"], tile.height, tile.width), fill, dtype=options["dtype"])
                                    else:
                                        inputs = {band: read_on_grid(src, tile.bounds, block_grid, crs, tile.height, tile.width, cancel,
                                                                    preserve_nodata=bool(bundle and band == "SCL")) for band, src in sources.items()}
                                        if bundle:
                                            blocks, counts = public_block(inputs, inside, options)
                                            valid = counts["analysis"]
                                        else:
                                            values, valid = _output(inputs, inside, options)
                                    if not saved_block:
                                        blocks_cache.save(key, blocks if bundle else {"analysis": values},
                                                          {"valid": valid, "counts": counts if bundle else None,
                                                           "versions": {band: [src[1].etag, src[1].size] for band, src in sources.items()}}, cancel)
                                    valid_count += valid
                                    if bundle:
                                        bundle.write(blocks, counts, window)
                                    else:
                                        out.write(values, window=window)
                                    report(f"{'复用已校验区块' if saved_block else '裁剪区块'} {j+1}/{len(tiles)}", .85*(j+1)/len(tiles))
                                out.update_tags(request_fingerprint=plan["fingerprint"], source_fingerprint=identity, catalogue_product_id=source_id,
                                                acquisition_utc=row["datetime"] or "static", source=manifest["source"], cloud_mask=str(options["mask_clouds"]),
                                                resampling="nearest", valid_pixels=str(valid_count),
                                                vertical_reference="EGM2008 orthometric height (DSM)" if options["mode"] == "dem" else "")
                                if rtc_access is not None:
                                    out.update_tags(radar_backscatter="linear_gamma0", per_band_nodata="true")
                                if bundle:
                                    bundle.tag({**out.tags(), "reflectance_units": "unitless", "mode": options["mode"]})
                            record["transport"] = {band: {"etag": src[1].etag, "object_bytes": src[1].size, "bytes_received": src[1].bytes_received,
                                                          "range_requests": src[1].requests, "cached_ranges": src[1].cached_pages} for band, src in sources.items()}
                            record["reused_blocks"] = blocks_cache.reused
                            if rtc_access is not None:
                                for band, src in sources.items():
                                    record["transport"][band]["azure_md5_verified_ranges"] = src[1].verified_range_md5
                    check_cancel(cancel)
                    if not valid_count and (bundle is None or not bundle.counts["unmasked"]):
                        record.update(status="empty", note="研究区没有有效像元；未保存空白影像。")
                        empty.append(row["key"])
                        _save_json(manifest_path, manifest)
                        report("无有效像元，已跳过", 1.0)
                        continue
                    report("校验最终 GeoTIFF", .92)
                    if not _existing_matches(partial, plan, grid, identity):
                        raise UserError("最终 GeoTIFF 网格或来源记录不一致，未保存。")
                    if bundle:
                        provenance = {"sources": [{"index": 1, "catalogue_id": source_id, "acquisition_utc": row["datetime"],
                                                    "observed_matches": ["safe_id"], "assets": assets}],
                                      "provider": manifest["source"],
                                      "reflectance_conversion": "BOA reflectance = source DN * STAC scale + STAC offset; negative values retained",
                                      "identity_note": "Each output is cropped from the exact official Collection-1 source product, with no replacement or mosaicking."}
                        metadata = bundle.publish(expected, provenance, cancel)
                        if not valid_count:
                            record["note"] = "分析影像已全部掩膜；保留未掩膜光谱、SCL 和来源图，可重新筛选。"
                    else:
                        metadata = audit_raster(partial, options, valid_count, cancel)
                        check_cancel(cancel)
                        publish_file(partial, target)
                        try:
                            _save_json(receipt, {**expected, **metadata})
                        except Exception:
                            target.unlink(missing_ok=True)
                            raise
                    record.update(status="completed", path=str(target), **metadata)
                files.append({"path": str(target), "scene": row["key"], "status": record["status"],
                              **({"bundle": bundle.result_paths()} if bundle else {})})
                _save_json(manifest_path, manifest)
                report("校验通过", 1.0)
                if cache_root.is_dir():
                    shutil.rmtree(cache_root)
            except Cancelled:
                record["status"] = "cancelled"
                raise
            except Exception as error:
                record.update(status="failed", note=str(error) if isinstance(error, UserError) else type(error).__name__)
                raise
            finally:
                partial.unlink(missing_ok=True)
                if bundle:
                    bundle.cleanup()
        manifest["status"] = "completed" if files else "empty"
        return {"files": files, "folder": str(folder), "manifest": str(manifest_path), "plan": plan, "empty_scenes": empty}
    except Cancelled as error:
        manifest["status"] = "cancelled"
        keep_download_result(error, files, folder, manifest_path, plan, empty)
        raise
    except Exception as error:
        manifest["status"] = "failed"
        keep_download_result(error, files, folder, manifest_path, plan, empty)
        raise
    finally:
        try:
            _save_json(manifest_path, manifest)
        finally:
            http.close()
