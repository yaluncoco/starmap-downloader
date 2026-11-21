"""Sentinel-2 analysis, unmasked reflectance, SCL and pixel provenance on one grid."""
from __future__ import annotations

import json
from contextlib import ExitStack
from uuid import uuid4

import numpy as np
import rasterio

from .errors import UserError
from .http import check_cancel
from .integrity import audit_raster, publish_file, sha256_file

SCHEMA = 1
NODATA = -9999.0
SCL_NODATA = 255  # SCL 0 is a real class, so keep it distinct from outside the AOI.
CLEAR_CLASSES = (2, 4, 5, 6, 7, 11)
NATIVE_RESOLUTION = {"B02": 10, "B03": 10, "B04": 10, "B08": 10,
                     "B05": 20, "B06": 20, "B07": 20, "B8A": 20, "B11": 20, "B12": 20}


def is_research(options):
    return options.get("mode") == "research"


def transfer_options(options):
    # Only transmit unmasked spectra + SCL + index. The analysis mask is applied locally.
    return {**options, "count": options["count"] + 2} if is_research(options) else options


def bundle_paths(target):
    return {"analysis": target, "unmasked": target.with_name(target.stem + ".unmasked.tif"),
            "scl": target.with_name(target.stem + ".scl.tif"),
            "source": target.with_name(target.stem + ".source.tif"),
            "sources": target.with_name(target.stem + ".sources.json")}


def has_bundle_files(target):
    return any(path.exists() for path in bundle_paths(target).values()) or target.with_suffix(".starmap.json").exists()


def output_specs(options):
    return {"analysis": options,
            "unmasked": {**options, "mode": "research_unmasked"},
            "scl": {"mode": "scl", "count": 1, "dtype": "uint8", "nodata": SCL_NODATA, "output_bands": ["SCL"]},
            "source": {"mode": "source", "count": 1, "dtype": "uint16", "nodata": 0, "output_bands": ["source_index"]}}


def make_block(raw, scl, source, inside, options):
    """Keep every available raw band; require all bands only in the analysis raster."""
    raw = np.asarray(raw, dtype=np.float32).copy()
    scl = np.asarray(scl, dtype=np.float32).copy()
    source = np.asarray(source, dtype=np.float32).copy()
    if (raw.shape != (options["count"], *inside.shape) or scl.shape != inside.shape
            or source.shape != inside.shape):
        raise UserError("研究分析包的光谱、SCL 或来源网格不一致，未保存。")
    raw[:, ~inside] = NODATA
    scl[~inside] = SCL_NODATA
    source[~inside] = 0
    present = np.isfinite(raw) & (raw != NODATA)
    raw[~present] = NODATA
    any_data = present.any(axis=0)
    if (np.any(~np.isfinite(scl)) or np.any(scl != np.floor(scl))
            or np.any(~(((scl >= 0) & (scl <= 11)) | (scl == SCL_NODATA)))
            or np.any(~np.isfinite(source)) or np.any(source != np.floor(source))
            or np.any((source < 0) | (source > 500)) or np.any((source > 0) != any_data)):
        raise UserError("研究分析包含无效的 SCL 分类或无法对应光谱值的像元来源，未保存。")
    analysis_valid = present.all(axis=0)
    if options["mask_clouds"]:
        analysis_valid &= np.isin(scl, CLEAR_CLASSES)
    analysis = raw.copy()
    analysis[:, ~analysis_valid] = NODATA
    blocks = {"analysis": analysis, "unmasked": raw, "scl": scl.astype(np.uint8)[None],
              "source": source.astype(np.uint16)[None]}
    counts = {"analysis": int(analysis_valid.sum()), "unmasked": int(any_data.sum()),
              "scl": int((scl != SCL_NODATA).sum()), "source": int((source > 0).sum())}
    return blocks, counts


def public_block(inputs, inside, options):
    raw = np.stack([inputs[band] for band in options["bands"]])
    scl = np.where(np.isfinite(inputs["SCL"]), inputs["SCL"], SCL_NODATA)
    present = np.isfinite(raw) & (raw != NODATA)
    source = present.any(axis=0).astype(np.uint16)
    return make_block(raw, scl, source, inside, options)


def process_block(values, inside, options, products, metadata):
    if values.shape[0] != options["count"] + 2 or not np.isfinite(values).all():
        raise UserError("处理服务返回的研究分析包布局或光谱数值无效，未保存。")
    raw, scl, source = values[:-2], values[-2], values[-1]
    available = set()
    for item in metadata["sources"]:
        index = item.get("source_index")
        if (type(index) is not int or not 1 <= index <= len(products)
                or products[index - 1]["id"] != item["catalogue_id"]):
            raise UserError("处理服务未返回可核验的逐像元来源索引，未保存。")
        available.add(index)
    used = set(source[inside & (source > 0)].tolist())
    if not used <= available or np.any((source > 0) & (scl == SCL_NODATA) & inside):
        raise UserError("处理服务返回的像元来源不在当前区块来源记录内，未保存。")
    return make_block(raw, scl, source, inside, options)


def _grid_matches(ds, plan, transform, spec, identity, role):
    tags = ds.tags()
    return (ds.width == plan["width"] and ds.height == plan["height"] and ds.count == spec["count"]
            and ds.crs is not None and ds.crs.to_epsg() == plan["epsg"]
            and ds.transform.almost_equals(transform, precision=1e-5)
            and ds.dtypes == (spec["dtype"],) * spec["count"] and ds.nodata == spec["nodata"]
            and ds.descriptions == tuple(spec["output_bands"])
            and tags.get("bands") == json.dumps(spec["output_bands"])
            and tags.get("request_fingerprint") == plan["fingerprint"] and tags.get("source_fingerprint") == identity
            and tags.get("bundle_schema") == str(SCHEMA) and tags.get("output_role") == role)


def _source_table(document, plan, identity):
    if (not isinstance(document, dict) or document.get("schema") != SCHEMA
            or document.get("request_fingerprint") != plan["fingerprint"] or document.get("source_fingerprint") != identity
            or not isinstance(document.get("sources"), list) or not 1 <= len(document["sources"]) <= 500):
        raise ValueError("Source table missing")
    indices, ids = set(), set()
    for item in document["sources"]:
        if (not isinstance(item, dict) or type(item.get("index")) is not int or not 1 <= item["index"] <= 500
                or item["index"] in indices or not isinstance(item.get("catalogue_id"), str) or not item["catalogue_id"]
                or item["catalogue_id"] in ids or not isinstance(item.get("observed_matches"), list)
                or any(method not in ("safe_id", "mgrs_and_utc") for method in item["observed_matches"])):
            raise ValueError("Invalid source table")
        indices.add(item["index"])
        ids.add(item["catalogue_id"])
    return {item["index"] for item in document["sources"] if item["observed_matches"]}


def checked_bundle(target, receipt, plan, transform, identity, cancel=None):
    """A matching main TIFF alone never establishes a complete research package."""
    paths, specs = bundle_paths(target), output_specs(plan["options"])
    try:
        bundle = receipt["bundle"]
        if not isinstance(bundle, dict) or bundle.get("schema") != SCHEMA:
            raise ValueError
        for role in ("unmasked", "scl", "source", "sources"):
            check_cancel(cancel)
            record, path = bundle[role], paths[role]
            if (record["file"] != path.name or path.stat().st_size != record["size"]
                    or sha256_file(path, cancel) != record["sha256"]):
                raise ValueError
        source_data = json.loads(paths["sources"].read_text(encoding="utf-8"))
        _source_table(source_data, plan, identity)
        if receipt["unmasked_valid_pixels"] != bundle["unmasked"]["valid_pixels"] or receipt["unmasked_valid_pixels"] <= 0:
            raise ValueError
        for role, spec in specs.items():
            with rasterio.open(paths[role]) as ds:
                if not _grid_matches(ds, plan, transform, spec, identity, role):
                    raise ValueError
                ds.read(1, window=rasterio.windows.Window(ds.width - 1, ds.height - 1, 1, 1))
        return {role: str(paths[role]) for role in ("unmasked", "scl", "source", "sources")}
    except (OSError, ValueError, KeyError, TypeError, UnicodeError, rasterio.errors.RasterioError):
        raise UserError("已有研究分析包缺少配套文件或完整性校验失败；请移走该组影像及 .starmap.json 后重试。") from None


class ResearchBundle:
    def __init__(self, target, plan, transform, identity):
        self.target, self.plan, self.transform, self.identity = target, plan, transform, identity
        self.paths = bundle_paths(target)
        uid = uuid4().hex[:12]
        self.partials = {role: path.with_name("." + path.stem + "." + uid + ".partial" + path.suffix)
                         for role, path in self.paths.items()}
        self.specs = output_specs(plan["options"])
        self.counts = dict.fromkeys(self.specs, 0)
        self.writers = {}

    def open(self, stack):
        for role, spec in self.specs.items():
            ds = stack.enter_context(rasterio.open(self.partials[role], "w", driver="GTiff", width=self.plan["width"],
                                                  height=self.plan["height"], count=spec["count"], dtype=spec["dtype"],
                                                  crs=f"EPSG:{self.plan['epsg']}", transform=self.transform, nodata=spec["nodata"],
                                                  compress="DEFLATE", predictor=3 if spec["dtype"] == "float32" else 2,
                                                  tiled=True, blockxsize=512, blockysize=512, BIGTIFF="IF_SAFER"))
            ds.descriptions = tuple(spec["output_bands"])
            for band in ds.indexes:
                ds.set_band_unit(band, "1")
            self.writers[role] = ds
        return self.writers["analysis"]

    def write(self, blocks, counts, window):
        for role, writer in self.writers.items():
            writer.write(blocks[role], window=window)
            self.counts[role] += counts[role]

    def tag(self, common):
        for role, writer in self.writers.items():
            writer.update_tags(**common)
            writer.update_tags(request_fingerprint=self.plan["fingerprint"], source_fingerprint=self.identity,
                               bundle_schema=str(SCHEMA), output_role=role, valid_pixels=str(self.counts[role]),
                               bands=json.dumps(self.specs[role]["output_bands"]), mode=self.specs[role]["mode"],
                               unmasked_valid_pixels=str(self.counts["unmasked"]), resampling="nearest",
                               cloud_mask=str(role == "analysis" and self.plan["options"]["mask_clouds"]),
                               source_index_file=self.paths["sources"].name,
                               reflectance_units="unitless" if role in ("analysis", "unmasked") else "",
                               scl_classes="0..11; 255=outside source/AOI", cloud_mask_kept_scl=json.dumps(CLEAR_CLASSES),
                               spectral_native_resolution_m=json.dumps(NATIVE_RESOLUTION, sort_keys=True))

    def _audit(self, source_data, cancel):
        allowed = _source_table(source_data, self.plan, self.identity)
        totals = dict.fromkeys(self.specs, 0)
        with ExitStack() as stack:
            readers = {role: stack.enter_context(rasterio.open(self.partials[role])) for role in self.specs}
            for role, ds in readers.items():
                if not _grid_matches(ds, self.plan, self.transform, self.specs[role], self.identity, role):
                    raise UserError("研究分析包的配套文件网格或标识不一致，未发布。")
            for _, window in readers["analysis"].block_windows(1):
                check_cancel(cancel)
                observed = {role: ds.read(window=window) for role, ds in readers.items()}
                if not np.isfinite(observed["unmasked"]).all():
                    raise UserError("未掩膜影像包含非有限反射率，未发布。")
                inside = np.ones(observed["scl"].shape[1:], dtype=bool)
                expected, counts = make_block(observed["unmasked"], observed["scl"][0], observed["source"][0], inside, self.plan["options"])
                if any(not np.array_equal(observed[role], expected[role]) for role in readers):
                    raise UserError("分析影像、未掩膜备份和 SCL 掩膜不一致，未发布。")
                indices = set(np.unique(observed["source"]).tolist()) - {0}
                if not indices <= allowed:
                    raise UserError("来源图存在未记录的产品索引，未发布。")
                for role, count in counts.items():
                    totals[role] += count
        if totals != self.counts or not totals["unmasked"]:
            raise UserError("研究分析包的有效像元计数不一致或没有原始光谱像元，未发布。")
        audited = {}
        for role in ("analysis", "unmasked"):
            audited[role] = audit_raster(self.partials[role], self.specs[role], totals[role], cancel,
                                          allow_empty=(role == "analysis"))
        for role in ("scl", "source"):
            audited[role] = {"size": self.partials[role].stat().st_size, "sha256": sha256_file(self.partials[role], cancel),
                             "valid_pixels": totals[role]}
        return audited

    def publish(self, expected, provenance, cancel=None):
        source_data = {**provenance, "schema": SCHEMA, "request_fingerprint": self.plan["fingerprint"],
                       "source_fingerprint": self.identity, "bands": self.plan["options"]["bands"],
                       "native_resolution_m": NATIVE_RESOLUTION, "output_resolution_m": self.plan["resolution"],
                       "resampling": "nearest", "source_index_nodata": 0, "scl_nodata": SCL_NODATA,
                       "cloud_mask_kept_scl": list(CLEAR_CLASSES),
                       "selection": provenance.get("selection", "first data-valid allowlisted product in catalogue-ID order; cloud mask applied after selection"),
                       "metadata_note": "Regional derived data. Full product XML, angle grids, AOT/WVP and other original layers require the complete SAFE mode."}
        self.partials["sources"].write_text(json.dumps(source_data, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
        try:
            audited = self._audit(source_data, cancel)
        except (rasterio.errors.RasterioError, ValueError, OSError) as error:
            raise UserError("研究分析包校验失败，临时文件未发布。") from error
        bundle = {"schema": SCHEMA}
        for role in ("unmasked", "scl", "source"):
            bundle[role] = {"file": self.paths[role].name, **audited[role]}
        bundle["sources"] = {"file": self.paths["sources"].name, "size": self.partials["sources"].stat().st_size,
                             "sha256": sha256_file(self.partials["sources"], cancel)}
        metadata = {**audited["analysis"], "unmasked_valid_pixels": self.counts["unmasked"], "bundle": bundle}
        receipt = self.target.with_suffix(".starmap.json")
        receipt_partial = receipt.with_name(receipt.name + "." + uuid4().hex[:8] + ".partial")
        published = []
        try:
            receipt_partial.write_text(json.dumps({**expected, **metadata}, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
            # The main TIFF and receipt are published last; rollback only files created by this attempt.
            for role in ("unmasked", "scl", "source", "sources", "analysis"):
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

    def result_paths(self):
        return {role: str(self.paths[role]) for role in ("unmasked", "scl", "source", "sources")}

    def cleanup(self):
        for path in self.partials.values():
            path.unlink(missing_ok=True)
