"""One spatially allocated, source-traceable Sentinel-2 mosaic on a shared UTM grid."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
import shutil
from collections import OrderedDict
from contextlib import ExitStack
from uuid import uuid4

import numpy as np
import rasterio
from pyproj import Transformer
from rasterio.enums import ColorInterp
from rasterio.transform import from_origin
from rasterio.windows import Window
from shapely import contains_xy, prepare, transform
from shapely.geometry import box, mapping

from . import VERSION
from .errors import Cancelled, UserError, keep_download_result
from .download_cache import ArrayCache
from .http import HttpClient, check_cancel
from .integrity import audit_raster, checked_local_receipt, publish_file, sha256_file
from .local_paths import io_path
from .planning import Tile, build_plan
from .research_bundle import (CLEAR_CLASSES, NODATA, SCL_NODATA, ResearchBundle, bundle_paths,
                              checked_bundle, has_bundle_files, is_research, make_block, process_block,
                              public_block, transfer_options)
from .scene_identity import expected_products, source_fingerprint
from .spatial_selection import (coverage_summary, footprint, ordered_scenes, polygonal,
                                priority_option, split_process_scenes)

PUBLIC = "s2-earth-search"
NATIVE = "s2-cdse-s3"
SCHEMA = 2


def _rows(geometry, scenes, options):
    rows = list(scenes)
    if options.get("profile") not in (PUBLIC, NATIVE):
        rows = [row for scene in scenes for row in
                ([scene] if scene.get("overpass_key") else split_process_scenes(geometry, [scene]))]
    if not 1 <= len(rows) <= 500:
        raise UserError("每次拼接请选择 1～500 个影像区块。")
    if len({row["key"] for row in rows}) != len(rows):
        raise UserError("拼接影像中有重复的产品。")
    return ordered_scenes(rows, priority_option(options))


def _raw_options(options):
    return {**options, "mode": "research", "count": len(options["bands"]), "dtype": "float32",
            "output_bands": list(options["bands"]), "nodata": NODATA, "mask_clouds": False}


def _identities(rows, options):
    if options.get("profile") == PUBLIC:
        keys = list(dict.fromkeys(options["bands"] + ["SCL"]))
        result = []
        for row in rows:
            src = row["public"]
            missing = set(keys) - src["assets"].keys()
            if missing:
                raise UserError("所选瓦片缺少拼接需要的公开波段：" + ", ".join(sorted(missing)) + "。")
            result.append({"id": src["source_id"], "utc": row["datetime"], "tile": row["tiles"][0],
                           "assets": {key: src["assets"][key] for key in keys}})
    elif options.get("profile") == NATIVE:
        from .native_mosaic import native_source_plan
        result = [{**expected_products(row)[0], "assets": native_source_plan(row, options)["targets"]} for row in rows]
    else:
        result = [expected_products(row)[0] for row in rows]
    if len({product["id"] for product in result}) != len(result):
        raise UserError("同一产品被重复选择，请保留一个。")
    return result


def _projected_regions(geometry, rows, epsg):
    converter = Transformer.from_crs(4326, epsg, always_xy=True)
    return [transform(polygonal(geometry.intersection(footprint(row))).segmentize(.05),
                      converter.transform, interleaved=False) for row in rows]


def _crop(tile, mask, resolution):
    y, x = np.nonzero(mask)
    if not len(x):
        return None
    x0, y0, x1, y1 = int(x.min()), int(y.min()), int(x.max()) + 1, int(y.max()) + 1
    bounds = (tile.bounds[0] + x0 * resolution, tile.bounds[3] - y1 * resolution,
              tile.bounds[0] + x1 * resolution, tile.bounds[3] - y0 * resolution)
    return Tile(tile.index, tile.row + y0, tile.col + x0, x1 - x0, y1 - y0, bounds, True), (slice(y0, y1), slice(x0, x1))


def build_mosaic_plan(geometry, scenes, options, *, tile_size=None, cancel=None, epsg=None):
    check_cancel(cancel)
    rows = _rows(geometry, scenes, options)
    identities = _identities(rows, options)
    coverage = coverage_summary(geometry, rows, [row["key"] for row in rows], priority_option(options), cancel=cancel)
    public = options.get("profile") == PUBLIC
    native = options.get("profile") == NATIVE
    plan, tiles, projected = build_plan(geometry, options, 1, tile_size=tile_size or (512 if public else 1024), cancel=cancel, epsg=epsg)
    regions = _projected_regions(geometry, rows, plan["epsg"])
    for region in regions:
        prepare(region)
    requests, pixels = 0, 0
    # Conservative upper bounds: all selected overlapping products may be needed for cloud/NoData fallback.
    for tile in tiles:
        check_cancel(cancel)
        if not tile.intersects:
            continue
        frame = box(*tile.bounds)
        for region in regions:
            if not region.intersects(frame):
                continue
            if region.contains(frame):
                requests += 1
                pixels += tile.width * tile.height
                continue
            overlap = region.intersection(frame)
            if overlap.is_empty or overlap.area <= 0:
                continue
            x0, y0, x1, y1 = overlap.bounds
            width = min(tile.width, math.ceil((x1 - tile.bounds[0]) / plan["resolution"]) -
                        math.floor((x0 - tile.bounds[0]) / plan["resolution"]))
            height = min(tile.height, math.ceil((tile.bounds[3] - y0) / plan["resolution"]) -
                         math.floor((tile.bounds[3] - y1) / plan["resolution"]))
            requests += 1
            pixels += max(0, width) * max(0, height)
    identity = source_fingerprint(identities)
    fingerprint = hashlib.sha256(json.dumps({"mosaic_schema": SCHEMA, "grid": plan["fingerprint"],
                                              "sources": identities, "regions": [mapping(footprint(row)) for row in rows]},
                                             sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()[:12]
    if not is_research(options):
        plan["bytes_per_scene"] += plan["width"] * plan["height"] * 3  # SCL + uint16 provenance.
    native_bytes = sum(target.get("bytes") or 0 for item in identities for target in item.get("assets", [])) if native else 0
    plan.update(kind="mosaic", layout="mosaic", transport="cdse-s3" if native else "public" if public else "sentinel-hub",
                fingerprint=fingerprint, source_fingerprint=identity, source_count=len(rows),
                product_keys=[row["key"] for row in rows], dates=coverage["dates"], coverage=coverage,
                requests=None if public or native else requests, max_requests=requests,
                source_objects=sum(len(item["assets"]) for item in identities) if native else len(identities) * (len(options["bands"]) + 1) if public else None,
                estimated_pu=None if public or native else round(max(.005 * requests, pixels / (512 * 512) * (len(options["bands"]) + 1) / 3), 2),
                uncompressed_mb=round(plan["bytes_per_scene"] / 1024**2, 2),
                folder_name=("S2_native_mosaic_" if native else "S2_public_mosaic_" if public else "S2L2A_mosaic_") + fingerprint,
                source_note="逐像元来源图关联每个产品的采集时间；云与 NoData 补齐只使用已选产品。")
    plan["pixel_mask_contract"] = "inverse-project pixel centres into the original WGS84 AOI and product footprints"
    if native:
        plan.update(requires=["cdse-s3"], native_source_bytes=native_bytes,
                    native_size_unknown=any(not target.get("bytes") for item in identities for target in item["assets"]),
                    native_source_note="先下载并校验完整原始波段和 XML；原件缓存可复用，不代表完整 SAFE。")
    return plan, tiles, projected


def _valid(raw, scl, options):
    present = np.isfinite(raw) & (raw != NODATA)
    valid = present.all(axis=0)
    if options["mask_clouds"]:
        valid &= np.isin(scl, CLEAR_CLASSES)
    if options.get("allocation_mode", options["mode"]) == "ndvi":
        red = raw[options["bands"].index("B04")]
        nir = raw[options["bands"].index("B08")]
        valid &= np.abs(red + nir) > 1e-6
    return valid


def _analysis(raw, scl, options):
    valid = _valid(raw, scl, options)
    if options["mode"] == "rgb":
        values = np.zeros((4, *scl.shape), dtype=np.uint8)
        for index, band in enumerate(raw):
            values[index, valid] = np.rint(np.clip(band[valid] * 637.5, 0, 255)).astype(np.uint8)
        values[3, valid] = 255
    elif options["mode"] == "ndvi":
        red = raw[options["bands"].index("B04")]
        nir = raw[options["bands"].index("B08")]
        values = np.full((1, *scl.shape), NODATA, dtype=np.float32)
        np.divide(nir - red, nir + red, out=values[0], where=valid)
    else:
        values = raw.copy()
        values[:, ~valid] = NODATA
    return values, valid


class _ProcessReader:
    def __init__(self, http, auth, plan, options, folder, cancel, report):
        self.http, self.auth, self.plan, self.options = http, auth, plan, _raw_options(options)
        self.cancel, self.report = cancel, report
        self.path = folder / (".mosaic-request-" + uuid4().hex + ".tif")

    def read(self, scene, tile, inside):
        from .download import _parts
        from .process_request import fetch_tile, process_payload

        products = expected_products(scene)
        try:
            meta = fetch_tile(self.http, self.auth, process_payload(tile, self.plan, scene, self.options, products=products),
                              self.path, options=self.options, products=products, cancel=self.cancel, status=self.report)
            grid = from_origin(tile.bounds[0], tile.bounds[3], self.plan["resolution"], self.plan["resolution"])
            values = _parts(tile, grid, self.plan, transfer_options(self.options), self.path)
            blocks, _ = process_block(values, inside, self.options, products, meta)
            return blocks["unmasked"], blocks["scl"][0], meta
        finally:
            self.path.unlink(missing_ok=True)

    def close(self):
        self.path.unlink(missing_ok=True)


class _PublicReader:
    def __init__(self, http, plan, options, cancel, cache_dir=None, report=None):
        self.http, self.plan, self.options, self.cancel = http, plan, _raw_options(options), cancel
        self.cache_dir, self.report = cache_dir, report
        self.opened, self.versions, self.transport, self.timing = OrderedDict(), {}, {}, {}

    def _close(self, key):
        group = self.opened.pop(key)
        sources = group.sources
        try:
            old_timing = self.timing.get(key, {})
            self.timing[key] = {"source_open_seconds": old_timing.get("source_open_seconds", 0) + group.open_seconds,
                                "source_read_seconds": old_timing.get("source_read_seconds", 0) + group.read_seconds,
                                "open_count": old_timing.get("open_count", 0) + 1,
                                "band_workers": len(group.threads)}
            records = self.transport.setdefault(key, {})
            for band, src in sources.items():
                stream = src[1]
                old = records.get(band, {})
                records[band] = {"etag": stream.etag, "object_bytes": stream.size,
                                 "bytes_received": old.get("bytes_received", 0) + stream.bytes_received,
                                 "range_requests": old.get("range_requests", 0) + stream.requests,
                                 "cached_ranges": old.get("cached_ranges", 0) + stream.cached_pages,
                                 "range_failures": old.get("range_failures", 0) + stream.range_failures,
                                 "coalesced_fallbacks": old.get("coalesced_fallbacks", 0) + stream.coalesced_fallbacks}
        finally:
            group.close()

    def read(self, scene, tile, inside):
        from .public_sources import PublicSources

        key = scene["key"]
        assets = {band: scene["public"]["assets"][band] for band in self.options["bands"] + ["SCL"]}
        if key not in self.opened:
            while len(self.opened) >= 2:
                self._close(next(iter(self.opened)))
            group = PublicSources(assets, self.cancel, cache_dir=self.cache_dir, status=self.report)
            try:
                for band, src in group.sources.items():
                    version = (src[1].etag, src[1].size)
                    if (key, band) in self.versions and self.versions[key, band] != version:
                        raise UserError("拼接期间源对象版本变化，已停止发布影像。")
                    self.versions[key, band] = version
                self.opened[key] = group
            except BaseException:
                group.close()
                raise
        self.opened.move_to_end(key)
        group = self.opened[key]
        sources = group.sources
        grid = from_origin(tile.bounds[0], tile.bounds[3], self.plan["resolution"], self.plan["resolution"])
        inputs = group.read(tile.bounds, grid, f"EPSG:{self.plan['epsg']}", tile.height, tile.width)
        blocks, _ = public_block(inputs, inside, self.options)
        return blocks["unmasked"], blocks["scl"][0], {
            "sources": [{"catalogue_id": scene["public"]["source_id"], "match": "safe_id"}], "assets": assets,
            "source_versions": {band: [src[1].etag, src[1].size] for band, src in sources.items()}}

    def close(self):
        while self.opened:
            self._close(next(iter(self.opened)))


class _AuxiliaryBundle:
    """Quality/provenance companions for RGB, NDVI and custom spectral mosaics."""
    def __init__(self, target, plan, grid, identity):
        self.plan, self.grid, self.identity, self.target = plan, grid, identity, target
        self.paths = {role: path for role, path in bundle_paths(target).items() if role != "unmasked"}
        self.partials = {role: path.with_name("." + path.stem + "." + uuid4().hex[:10] + ".partial" + path.suffix)
                         for role, path in self.paths.items()}
        self.specs = {"analysis": plan["options"],
                      "scl": {"count": 1, "dtype": "uint8", "nodata": SCL_NODATA, "output_bands": ["SCL"]},
                      "source": {"count": 1, "dtype": "uint16", "nodata": 0, "output_bands": ["source_index"]}}
        self.writers = {}
        self.counts = dict.fromkeys(self.specs, 0)

    def open(self, stack):
        for role, spec in self.specs.items():
            writer = stack.enter_context(rasterio.open(self.partials[role], "w", driver="GTiff",
                width=self.plan["width"], height=self.plan["height"], count=spec["count"], dtype=spec["dtype"],
                crs=f"EPSG:{self.plan['epsg']}", transform=self.grid, nodata=spec["nodata"], compress="DEFLATE",
                predictor=3 if spec["dtype"] == "float32" else 2, tiled=True, blockxsize=512, blockysize=512, BIGTIFF="IF_SAFER"))
            writer.descriptions = tuple(spec["output_bands"])
            self.writers[role] = writer
        if self.plan["options"]["mode"] == "rgb":
            self.writers["analysis"].colorinterp = (ColorInterp.red, ColorInterp.green, ColorInterp.blue, ColorInterp.alpha)
        return self.writers["analysis"]

    def write(self, blocks, counts, window):
        for role, writer in self.writers.items():
            writer.write(blocks[role], window=window)
            self.counts[role] += counts[role]

    def tag(self, common):
        for role, writer in self.writers.items():
            writer.update_tags(**common)
            writer.update_tags(request_fingerprint=self.plan["fingerprint"], source_fingerprint=self.identity,
                               output_role=role, mosaic_schema=str(SCHEMA), valid_pixels=str(self.counts[role]),
                               bands=json.dumps(self.specs[role]["output_bands"]), source_index_file=self.paths["sources"].name)

    def publish(self, expected, provenance, cancel):
        from .download import _save_json

        audited = {"analysis": audit_raster(self.partials["analysis"], self.plan["options"], self.counts["analysis"], cancel)}
        allowed = {item["index"] for item in provenance["sources"] if item["observed_matches"]}
        totals = {"scl": 0, "source": 0}
        with ExitStack() as stack:
            readers = {role: stack.enter_context(rasterio.open(self.partials[role])) for role in self.specs}
            for _, window in readers["source"].block_windows(1):
                check_cancel(cancel)
                src, scl = readers["source"].read(1, window=window), readers["scl"].read(1, window=window)
                if not (set(np.unique(src)) - {0}) <= allowed or not np.isin(scl, (*range(12), SCL_NODATA)).all():
                    raise UserError("拼接影像的质量层或像元来源无效，未发布。")
                values = readers["analysis"].read(window=window)
                valid = values[3] == 255 if self.plan["options"]["mode"] == "rgb" else np.all(values != NODATA, axis=0)
                if np.any(valid & (src == 0)) or np.any(valid & (scl == SCL_NODATA)):
                    raise UserError("拼接有效像元没有来源或质量记录，未发布。")
                totals["source"] += int((src > 0).sum())
                totals["scl"] += int((scl != SCL_NODATA).sum())
        if any(totals[role] != self.counts[role] for role in totals):
            raise UserError("拼接来源图的像元计数不一致，未发布。")
        for role in ("scl", "source"):
            audited[role] = {"size": self.partials[role].stat().st_size, "sha256": sha256_file(self.partials[role], cancel),
                             "valid_pixels": totals[role]}
        _save_json(self.partials["sources"], {**provenance, "mosaic_schema": SCHEMA,
                                             "request_fingerprint": self.plan["fingerprint"], "source_fingerprint": self.identity})
        companions = {role: {"file": self.paths[role].name, **audited[role]} for role in ("scl", "source")}
        companions["sources"] = {"file": self.paths["sources"].name, "size": self.partials["sources"].stat().st_size,
                                  "sha256": sha256_file(self.partials["sources"], cancel)}
        metadata = {**audited["analysis"], "mosaic": {"schema": SCHEMA, **companions}}
        receipt = self.target.with_suffix(".starmap.json")
        receipt_partial = receipt.with_name(receipt.name + "." + uuid4().hex[:10] + ".partial")
        published = []
        try:
            _save_json(receipt_partial, {**expected, **metadata})
            for role in ("scl", "source", "sources", "analysis"):
                check_cancel(cancel)
                publish_file(self.partials[role], self.paths[role])
                published.append(self.paths[role])
            check_cancel(cancel)
            publish_file(receipt_partial, receipt)
            published.append(receipt)
        except BaseException:
            for path in reversed(published):
                path.unlink(missing_ok=True)
            raise
        finally:
            receipt_partial.unlink(missing_ok=True)
        return metadata

    def check(self, receipt, cancel):
        try:
            metadata = receipt["mosaic"]
            if metadata["schema"] != SCHEMA:
                raise ValueError
            for role in ("scl", "source", "sources"):
                row, path = metadata[role], self.paths[role]
                if row["file"] != path.name or path.stat().st_size != row["size"] or sha256_file(path, cancel) != row["sha256"]:
                    raise ValueError
        except (ValueError, KeyError, OSError, TypeError):
            raise UserError("已有拼接影像缺少配套质量层或来源文件，或完整性校验失败；请移走该组文件后重试。") from None

    def result_paths(self):
        return {role: str(self.paths[role]) for role in ("scl", "source", "sources")}

    def cleanup(self):
        for path in self.partials.values():
            path.unlink(missing_ok=True)


def download_mosaic(geometry, scenes, options, output_dir, *, auth=None, cancel=None, progress=None, planned=None, reader=None):
    from .download import _existing_matches, _save_json

    check_cancel(cancel)
    base = io_path(output_dir)
    if not base.is_absolute():
        raise UserError("请选择完整的输出文件夹路径。")
    public = options.get("profile") == PUBLIC
    native = options.get("profile") == NATIVE
    if reader is None and not public and (auth is None or not auth.connected):
        raise UserError("请先连接 CDSE S3 密钥。" if native else "请先连接 Sentinel Hub OAuth 客户端凭据。")
    plan, tiles, projected = planned or build_mosaic_plan(geometry, scenes, options)
    rows = _rows(geometry, scenes, options)
    identities = _identities(rows, options)
    identity = source_fingerprint(identities)
    if plan.get("source_fingerprint") != identity or plan.get("product_keys") != [row["key"] for row in rows]:
        raise UserError("拼接计划与所选产品不一致，请重新确认。")
    regions = _projected_regions(geometry, rows, plan["epsg"])
    geographic_regions = [footprint(row) for row in rows]
    for region in [geometry, *geographic_regions]:
        prepare(region)
    inverse = Transformer.from_crs(plan["epsg"], 4326, always_xy=True)
    folder = base / plan["folder_name"]
    folder.mkdir(parents=True, exist_ok=True)
    grid = from_origin(plan["bounds"][0], plan["bounds"][3], plan["resolution"], plan["resolution"])
    target = folder / f"mosaic_{options['mode']}_{plan['resolution']}m_{identity}.tif"
    receipt = target.with_suffix(".starmap.json")
    expected = {"file": target.name, "request_fingerprint": plan["fingerprint"], "source_fingerprint": identity}
    bundle = ResearchBundle(target, plan, grid, identity) if is_research(options) else _AuxiliaryBundle(target, plan, grid, identity)
    cache_root = folder / ".download-cache" / identity
    cache_identity = {"schema": 1, "grid": plan["fingerprint"], "source": identity}
    blocks_cache = ArrayCache(cache_root, cache_identity, bundle.specs)
    request_cache = ArrayCache(cache_root / "requests", cache_identity,
                               {"unmasked": {"count": len(options["bands"]), "dtype": "float32"},
                                "scl": {"count": 1, "dtype": "uint8"}})
    manifest_path = folder / ("下载记录_" + uuid4().hex[:10] + ".json")
    record = {"scene": "mosaic", "path": str(target), "status": "running", "source_fingerprint": identity,
              "selected_keys": [row["key"] for row in rows], "blocks": []}
    manifest = {"software": f"星图下载器 {VERSION}", "source": "CDSE S3 / exact native Sentinel-2 L2A objects" if native else "Element 84 Earth Search / Sentinel-2 C1 L2A" if public else "CDSE Sentinel Hub Process API / Sentinel-2 L2A",
                "created_at": dt.datetime.now(dt.timezone.utc).isoformat(), "plan": plan, "aoi": mapping(geometry),
                "status": "running", "items": [record], "source_note": plan["source_note"]}
    files, http = [], None
    completed_tiles = 0

    def report(message, fraction=None, **extra):
        if progress:
            progress(message, .9 * completed_tiles / max(1, len(tiles)) if fraction is None else fraction,
                     {"folder": str(folder), "total_scenes": 1, "completed_scenes": 0, **extra})

    try:
        if has_bundle_files(target):
            report("核对已有拼接影像及所有配套文件的 SHA-256")
            cached = checked_local_receipt(target, receipt, expected, cancel)
            if not _existing_matches(target, plan, grid, identity):
                raise UserError("已有拼接影像的网格或参数不匹配，请移走该组文件后重试。")
            if is_research(options):
                checked_bundle(target, cached, plan, grid, identity, cancel)
            else:
                bundle.check(cached, cancel)
            record.update(status="skipped", **cached)
            table = json.loads(bundle_paths(target)["sources"].read_text(encoding="utf-8"))
            used_dates = sorted({item["acquisition_utc"][:10] for item in table["sources"] if item.get("used_pixels", 0)})
            files.append({"path": str(target), "scene": "mosaic", "status": "skipped", "bundle": bundle.result_paths()})
            manifest["status"] = "completed"
            return {"folder": str(folder), "files": files, "manifest": str(manifest_path), "plan": plan, "empty_scenes": [], "used_dates": used_dates}
        max_pixels = max(tile.width * tile.height for tile in tiles)
        required = plan["bytes_per_scene"] * 3.2 + max_pixels * (len(options["bands"]) * 16 + 48) + 16 * 1024**2
        required += plan.get("native_source_bytes", 0) * 1.1
        if shutil.disk_usage(folder).free < required:
            raise UserError(f"拼接输出及临时空间预计至少需要 {required / 1024**2:.0f} MB；请换一个输出文件夹或分区下载。")
        _save_json(folder / "研究区.geojson", {"type": "Feature", "properties": {}, "geometry": mapping(geometry)})
        _save_json(folder / "拼接分配.geojson", {"type": "FeatureCollection", "features": [
            {"type": "Feature", "properties": {key: block[key] for key in ("key", "date", "rank", "cloud", "contribution_percent")},
             "geometry": block["assigned"]} for block in plan["coverage"]["blocks"] if block["assigned"]]})
        _save_json(manifest_path, manifest)
        if reader is None:
            if native:
                from .native_mosaic import NativeReader
                reader = NativeReader(plan, options, folder / "官方原件缓存", auth, cancel, report)
            else:
                http = HttpClient()
                reader = _PublicReader(http, plan, options, cancel, cache_root / "ranges", report) if public else _ProcessReader(http, auth, plan, options, folder, cancel, report)
        used, valid_by_source = np.zeros(len(rows) + 1, dtype=np.int64), np.zeros(len(rows) + 1, dtype=np.int64)
        matches = {item["id"]: set() for item in identities}
        fallback_pixels, inside_pixels = 0, 0

        def restore_evidence(detail):
            if hasattr(reader, "evidence"):
                reader.evidence.update(detail.get("native_sources", {}))
            if hasattr(reader, "versions"):
                for key, bands in detail.get("versions", {}).items():
                    for band, version in bands.items():
                        found = reader.versions.get((key, band))
                        if found is not None and found != tuple(version):
                            raise UserError("保存的拼接区块源版本不一致，未混用影像。")
                        reader.versions[key, band] = tuple(version)

        def cache_evidence():
            versions = {}
            for (key, band), version in getattr(reader, "versions", {}).items():
                versions.setdefault(key, {})[band] = list(version)
            return {"versions": versions, "native_sources": getattr(reader, "evidence", {})}

        with rasterio.Env(GDAL_PAM_ENABLED="NO", GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR"), ExitStack() as outputs:
            bundle.open(outputs)
            for tile in tiles:
                check_cancel(cancel)
                window = Window(tile.col, tile.row, tile.width, tile.height)
                block_key = [tile.row, tile.col, tile.height, tile.width]
                saved_block = blocks_cache.load(block_key, (tile.height, tile.width), cancel)
                if saved_block:
                    blocks, detail = saved_block
                    restore_evidence(detail)
                    used += np.array(detail["used"], dtype=np.int64)
                    valid_by_source += np.array(detail["valid_by_source"], dtype=np.int64)
                    fallback_pixels += detail["fallback_pixels"]
                    inside_pixels += detail["inside_pixels"]
                    record["blocks"].extend(detail["blocks"])
                    for key, methods in detail["matches"].items():
                        matches[key].update(methods)
                    bundle.write(blocks, detail["counts"], window)
                    completed_tiles += 1
                    report(f"复用已校验拼接区块 {completed_tiles}/{len(tiles)}")
                    continue
                start_blocks = len(record["blocks"])
                x = np.broadcast_to((tile.bounds[0] + (np.arange(tile.width) + .5) * plan["resolution"])[None], (tile.height, tile.width))
                y = np.broadcast_to((tile.bounds[3] - (np.arange(tile.height) + .5) * plan["resolution"])[:, None], (tile.height, tile.width))
                longitude, latitude = inverse.transform(x, y)
                inside = contains_xy(geometry, longitude, latitude)
                inside_pixels += int(inside.sum())
                raw = np.full((len(options["bands"]), tile.height, tile.width), NODATA, dtype=np.float32)
                scl = np.full((tile.height, tile.width), SCL_NODATA, dtype=np.uint8)
                source = np.zeros((tile.height, tile.width), dtype=np.uint16)
                primary = np.zeros_like(source)
                for index, (scene, region, geographic_region, identity_row) in enumerate(zip(rows, regions, geographic_regions, identities, strict=True), 1):
                    if not tile.intersects or region.is_empty or not region.intersects(box(*tile.bounds)):
                        continue
                    covered = inside & contains_xy(geographic_region, longitude, latitude)
                    primary[covered & (primary == 0)] = index
                    needed = covered & (~_valid(raw, scl, options) if options.get("fill_gaps", True) else primary == index)
                    cropped = _crop(tile, needed, plan["resolution"])
                    if cropped is None:
                        continue
                    request_tile, slices = cropped
                    keep = needed[slices]
                    report(f"拼接区块 {completed_tiles + 1}/{len(tiles)} · {scene['tiles'][0]} · {scene['date']} · 优先级 {index}")
                    request_key = [scene["key"], request_tile.row, request_tile.col, request_tile.height, request_tile.width,
                                   hashlib.sha256(keep.tobytes()).hexdigest()]
                    saved_request = request_cache.load(request_key, (request_tile.height, request_tile.width), cancel)
                    if saved_request:
                        arrays, detail = saved_request
                        values, quality, metadata = arrays["unmasked"], arrays["scl"][0], detail["metadata"]
                        restore_evidence(detail)
                    else:
                        values, quality, metadata = reader.read(scene, request_tile, keep)
                    # Readers must return source-valid, finite raw values on the exact requested grid.
                    blocks, _ = make_block(values, quality, np.any(np.isfinite(values) & (values != NODATA), axis=0).astype(np.uint16), keep, _raw_options(options))
                    values, quality = blocks["unmasked"], blocks["scl"][0]
                    old = raw[:, slices[0], slices[1]]
                    old_scl = scl[slices]
                    present, old_present = values != NODATA, old != NODATA
                    good, old_good = _valid(values, quality, options), _valid(old, old_scl, options)
                    take = keep & present.any(axis=0) & (~old_present.any(axis=0) | (good & ~old_good) |
                                                               (present.all(axis=0) & ~old_present.all(axis=0)))
                    old[:, take] = values[:, take]
                    old_scl[take] = quality[take]
                    source[slices][take] = index
                    for item in metadata.get("sources", []):
                        if item.get("catalogue_id") != identity_row["id"] or item.get("match") not in ("safe_id", "mgrs_and_utc"):
                            raise UserError("拼接服务返回了未选择的产品来源，未发布影像。")
                        matches[identity_row["id"]].add(item["match"])
                    if take.any() and not matches[identity_row["id"]]:
                        raise UserError("拼接像元没有可核对的产品来源，未发布影像。")
                    if not saved_request:
                        request_cache.save(request_key, {"unmasked": values, "scl": quality[None]},
                                           {"metadata": metadata, **cache_evidence()}, cancel)
                    record["blocks"].append({"key": scene["key"], "source_index": index, "bounds": list(request_tile.bounds),
                                             "width": request_tile.width, "height": request_tile.height,
                                             "needed_pixels": int(keep.sum()), "accepted_pixels": int(take.sum()), **metadata})
                if is_research(options):
                    blocks, counts = make_block(raw, scl, source, inside, options)
                    valid = np.all(blocks["analysis"] != NODATA, axis=0)
                else:
                    analysis, valid = _analysis(raw, scl, options)
                    blocks = {"analysis": analysis, "scl": scl[None], "source": source[None]}
                    counts = {"analysis": int(valid.sum()), "scl": int((scl != SCL_NODATA).sum()), "source": int((source > 0).sum())}
                tile_used = np.bincount(source.ravel(), minlength=len(rows) + 1)
                tile_valid = np.bincount(source[valid], minlength=len(rows) + 1)
                tile_fallback = int(((source > 0) & (source != primary)).sum())
                blocks_cache.save(block_key, blocks,
                                  {"counts": counts, "used": tile_used.tolist(), "valid_by_source": tile_valid.tolist(),
                                   "fallback_pixels": tile_fallback, "inside_pixels": int(inside.sum()),
                                   "blocks": record["blocks"][start_blocks:], "matches": {key: sorted(value) for key, value in matches.items()},
                                   **cache_evidence()}, cancel)
                used += tile_used
                valid_by_source += tile_valid
                fallback_pixels += tile_fallback
                bundle.write(blocks, counts, window)
                completed_tiles += 1
            common = {"source": manifest["source"], "acquisition_utc": "multiple; see source_index_file",
                      "acquisition_dates_utc": json.dumps(plan["dates"]), "layout": "mosaic", "priority": options["priority"],
                      "fill_gaps": str(options.get("fill_gaps", True)), "valid_pixels": str(bundle.counts["analysis"]),
                      "cloud_mask": str(options["mask_clouds"]), "resampling": "nearest",
                      "reflectance_units": "unitless" if options["dtype"] == "float32" and options["mode"] != "ndvi" else "",
                      "mode": options["mode"], "harmonize_values": "False", "fallback_pixels": str(fallback_pixels)}
            bundle.tag(common)
        check_cancel(cancel)
        reader.close()
        if hasattr(reader, "transport"):
            record["transport"] = reader.transport
        if hasattr(reader, "timing"):
            record["source_read_timing"] = reader.timing
        if hasattr(reader, "evidence"):
            record["native_sources"] = reader.evidence
        if not public and record["blocks"] and not any(matches.values()):
            raise UserError("处理服务没有返回可与所选产品匹配的瓦片，未保存拼接影像。请重新检索或选择其他日期。")
        record.update(inside_pixels=inside_pixels, valid_pixels=bundle.counts["analysis"],
                      source_pixels=int(used[1:].sum()), fallback_pixels=fallback_pixels,
                      uncovered_or_no_spectral_pixels=inside_pixels - int(used[1:].sum()),
                      actual_requests=len(record["blocks"]), reused_blocks=blocks_cache.reused, reused_source_blocks=request_cache.reused)
        used_dates = sorted({item["utc"][:10] for index, item in enumerate(identities, 1) if used[index]})
        record["used_dates"] = used_dates
        if not bundle.counts["analysis"] and (not is_research(options) or not bundle.counts["unmasked"]):
            record.update(status="empty", note="所选影像在研究区没有有效输出像元；没有发布空白影像。")
            manifest["status"] = "empty"
            return {"folder": str(folder), "files": [], "manifest": str(manifest_path), "plan": plan, "empty_scenes": ["mosaic"]}
        if not _existing_matches(bundle.partials["analysis"], plan, grid, identity):
            raise UserError("最终拼接影像的网格、像元计数或来源标识不一致，未发布。")
        provenance = {"sources": [{"index": index, "catalogue_id": item["id"], "mgrs_tile": item["tile"],
                                    "acquisition_utc": record.get("native_sources", {}).get(row["key"], {}).get("acquisition_utc", item["utc"]), "cloud": row.get("cloud_max"),
                                    "observed_matches": sorted(matches[item["id"]]), "used_pixels": int(used[index]),
                                    "analysis_pixels": int(valid_by_source[index]), **({"assets": item["assets"]} if public else {})}
                                   for index, (row, item) in enumerate(zip(rows, identities, strict=True), 1)],
                      "provider": manifest["source"], "blocks": record["blocks"], "priority": options["priority"],
                      "selection": "first product in the chosen priority; selected lower-priority products fill invalid or masked pixels; raw backup and SCL follow the final pixel source",
                      "coverage": plan["coverage"], "fallback_pixels": fallback_pixels,
                      "reflectance_conversion": "BOA reflectance = (native DN + XML BOA_ADD_OFFSET) / XML BOA_QUANTIFICATION_VALUE" if native else "BOA reflectance = source DN * STAC scale + offset" if public else "Process API BOA reflectance; harmonizeValues=false",
                      **({"native_sources": record["native_sources"]} if native else {}),
                      "identity_note": "Only selected products are used. safe_id identifies the complete product; mgrs_and_utc cannot distinguish an unreported processing version."}
        provenance.update(getattr(reader, "provenance", {}))
        report("逐块核对光谱、SCL、像元来源与 SHA-256", .94)
        audited = bundle.publish(expected, provenance, cancel)
        record.update(status="completed", **audited)
        if cache_root.is_dir():
            shutil.rmtree(cache_root)
        files.append({"path": str(target), "scene": "mosaic", "status": "completed", "bundle": bundle.result_paths()})
        manifest["status"] = "completed"
        report(f"拼接完成 · 实际使用 {len(used_dates)} 个日期 · 1 份区域输出", 1.0, completed_scenes=1)
        return {"folder": str(folder), "files": files, "manifest": str(manifest_path), "plan": plan, "empty_scenes": [], "used_dates": used_dates}
    except Cancelled as error:
        record["status"], manifest["status"] = "cancelled", "cancelled"
        keep_download_result(error, files, folder, manifest_path, plan)
        raise
    except Exception as error:
        record.update(status="failed", note=str(error) if isinstance(error, UserError) else type(error).__name__)
        manifest["status"] = "failed"
        keep_download_result(error, files, folder, manifest_path, plan)
        raise
    finally:
        try:
            if reader is not None:
                reader.close()
                if hasattr(reader, "transport"):
                    record["transport"] = reader.transport
                if hasattr(reader, "timing"):
                    record["source_read_timing"] = reader.timing
                if hasattr(reader, "evidence"):
                    record["native_sources"] = reader.evidence
            record["reused_blocks"] = blocks_cache.reused
            record["reused_source_blocks"] = request_cache.reused
            bundle.cleanup()
            _save_json(manifest_path, manifest)
        finally:
            if http is not None:
                http.close()
