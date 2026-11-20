"""Authenticated GEDI originals, footprint tables and aligned observation grids."""
from __future__ import annotations

import csv
import copy
import datetime as dt
import hashlib
import json
import math
import shutil
import sqlite3
import tempfile
from contextlib import ExitStack
from pathlib import Path

import h5py
import numpy as np
import rasterio
import requests
from pyproj import Transformer
from rasterio.features import geometry_mask
from rasterio.transform import Affine, from_origin
from rasterio.vrt import WarpedVRT
from rasterio.warp import Resampling
from shapely import intersects_xy, normalize, transform
from shapely.geometry import mapping

from . import VERSION
from .earthdata_auth import request_data
from .errors import UserError, keep_download_result
from .gedi_catalogue import BEAMS, CHECKSUMS, COLLECTIONS, collection_id
from .http import HttpClient, check_cancel
from .integrity import checked_local_receipt, publish_file, sha256_file
from .local_paths import io_path
from .planning import build_plan

NODATA = -9999.0
RESOLUTIONS = (10, 20, 30, 40, 60, 90, 100, 250, 500, 1000)
CHUNK_SHOTS = 8192
UTC = dt.timezone.utc


def _fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def normalize_options(value, key):
    key = collection_id(key)
    spec = COLLECTIONS[key]
    mode = value.get("mode", "aligned")
    if mode not in (("original", "aligned") if spec["kind"] == "raster" else ("original", "footprints", "aligned")):
        raise UserError("请选择 GEDI 原始文件、足迹表格或对齐 GeoTIFF。栅格产品不能导出为激光足迹。")
    if mode == "original":
        return {"collection": key, "mode": mode, "fields": [], "beams": list(BEAMS), "quality": "all",
                "nondegraded": False, "min_sensitivity": 0, "reference_path": "", "sample_reference": False, "resolution": 1000}
    fields = value.get("fields", [f["id"] for f in spec["fields"] if f["default"]])
    allowed = {f["id"] for f in spec["fields"]}
    if (not isinstance(fields, list) or any(not isinstance(f, str) or f not in allowed for f in fields)
            or len(set(fields)) != len(fields) or spec["kind"] == "footprint" and not fields):
        raise UserError("请勾选此 GEDI 产品支持的导出参数。")
    fields = [f["id"] for f in spec["fields"] if f["id"] in fields]
    if mode == "aligned" and spec["kind"] == "footprint" and not any(f["id"] in fields and f["kind"] in ("scalar", "derived") for f in spec["fields"]):
        raise UserError("对齐栅格至少需要一个标量参数；完整波形和剖面数组保存在足迹表格中。")
    beams = value.get("beams", list(BEAMS))
    if not isinstance(beams, list) or not beams or any(b not in BEAMS for b in beams) or len(set(beams)) != len(beams):
        raise UserError("请至少选择一个有效的 GEDI 波束。")
    quality = value.get("quality", "recommended")
    if quality not in ("recommended", "all"):
        raise UserError("请选择按本产品官方质量标志筛选，或保留全部足迹。")
    try:
        sensitivity = float(value.get("min_sensitivity", 0))
        resolution = float(value.get("resolution", 1000 if spec["kind"] == "raster" else 10))
    except (ValueError, TypeError):
        raise UserError("GEDI 敏感度或像元大小无效。") from None
    if not math.isfinite(sensitivity) or not 0 <= sensitivity <= 1 or resolution not in RESOLUTIONS:
        raise UserError("GEDI 敏感度应在 0–1，像元大小需从提供的数值中选择。")
    if sensitivity and spec["level"] == "L1B":
        raise UserError("GEDI L1B 未提供 L2 敏感度，不能使用 sensitivity 筛选。")
    nondegraded, sample = value.get("nondegraded", True), value.get("sample_reference", False)
    if not isinstance(nondegraded, bool) or not isinstance(sample, bool):
        raise UserError("GEDI 质量和影像采样选项无效。")
    reference = value.get("reference_path", "")
    if not isinstance(reference, str) or len(reference) > 2048:
        raise UserError("参考 GeoTIFF 路径无效。")
    reference = reference.strip()
    if reference and (not Path(reference).expanduser().is_absolute() or Path(reference).suffix.lower() not in (".tif", ".tiff")):
        raise UserError("请选择已有 GeoTIFF 的完整本地路径。")
    if sample and (not reference or spec["kind"] != "footprint"):
        raise UserError("足迹采样需要选择已有参考 GeoTIFF。")
    return {"collection": key, "mode": mode, "fields": fields, "beams": [b for b in BEAMS if b in beams],
            "quality": quality, "nondegraded": nondegraded, "min_sensitivity": sensitivity,
            "reference_path": str(Path(reference).expanduser()) if reference else "",
            "sample_reference": sample, "resolution": int(resolution)}


def _reference_grid(path):
    try:
        target = io_path(path)
        stat = target.stat()
        with rasterio.open(target) as raster:
            if not raster.crs or raster.width < 1 or raster.height < 1 or not all(math.isfinite(x) for x in raster.transform[:6]) or abs(raster.transform.determinant) < 1e-15:
                raise ValueError
            if raster.count > 128:
                raise UserError("参考影像超过 128 波段，请先选择需要的影像。")
            nodata = [float(n) if n is not None and math.isfinite(n) else None for n in raster.nodatavals]
            reference = {"path": str(target.resolve()), "size": stat.st_size, "mtime_ns": stat.st_mtime_ns,
                         "bands": [{"index": i, "description": raster.descriptions[i - 1], "units": raster.units[i - 1],
                                    "scale": raster.scales[i - 1], "offset": raster.offsets[i - 1], "nodata": nodata[i - 1]}
                                   for i in raster.indexes]}
            return {"width": raster.width, "height": raster.height, "transform": list(raster.transform)[:6],
                    "crs": raster.crs.to_wkt(), "epsg": raster.crs.to_epsg(), "crs_label": raster.crs.to_string(),
                    "bounds": list(raster.bounds), "pixel_size": list(raster.res),
                    "pixel_unit": "degree" if raster.crs.is_geographic else raster.crs.linear_units,
                    "reference": reference, "grid_source": "reference"}
    except (OSError, ValueError, rasterio.errors.RasterioError):
        raise UserError("无法读取参考 GeoTIFF，或该文件没有有效坐标系与像元网格。") from None


def build_gedi_plan(geometry, scenes, options, criteria, *, reference_grid=None):
    criteria = {name: criteria[name] for name in ("start", "end", "collection")}
    key = options["collection"]
    spec = COLLECTIONS[key]
    targets = []
    for scene in scenes:
        source = scene.get("gedi", {})
        if source.get("collection") != key or not source.get("available"):
            raise UserError("所选 GEDI 文件不属于该版本或没有可用下载入口，请重新检索。")
        targets.append({"name": source["name"], "source": source,
                        "relative_path": f"original/{source['concept_id']}-r{source['revision_id']}/{source['name']}"})
    scalar = [f for f in spec["fields"] if f["id"] in options["fields"] and f["kind"] in ("scalar", "derived")]
    grid = None
    if options["mode"] != "original" and (options["mode"] == "aligned" or options["reference_path"]):
        if reference_grid is not None:
            grid = copy.deepcopy(reference_grid)
        elif options["reference_path"]:
            grid = _reference_grid(options["reference_path"])
        else:
            grid_options = {"resolution": options["resolution"], "bands": [f["id"] for f in scalar] or ["value"],
                            "count": len(scalar) or 1, "mask_clouds": False, "dtype": "float32"}
            plan, _, _ = build_plan(geometry, grid_options, 1)
            grid = {"width": plan["width"], "height": plan["height"], "transform": list(from_origin(plan["bounds"][0], plan["bounds"][3], plan["resolution"], plan["resolution"]))[:6],
                    "crs": f"EPSG:{plan['epsg']}", "epsg": plan["epsg"], "crs_label": plan["crs_label"],
                    "bounds": plan["bounds"], "pixel_size": [plan["resolution"]] * 2, "pixel_unit": "metre",
                    "reference": None, "grid_source": "existing_utm_rule"}
        pixels = grid["width"] * grid["height"]
        if pixels > 2_000_000_000:
            raise UserError("参考网格过大，请选择较小的区域影像。")
    fingerprint = _fingerprint({"gedi_export_version": 1, "geometry": mapping(normalize(geometry)),
                                "options": options, "criteria": criteria, "grid": grid,
                                "sources": sorted((t["source"] for t in targets), key=lambda x: x["concept_id"])})[:16]
    root_name = key.replace("-", "_").upper()
    folder = f"{root_name}/exports/{fingerprint}"
    return {"kind": "gedi", "fingerprint": fingerprint, "options": options, "criteria": criteria,
            "collection_title": spec["title"], "data_kind": spec["kind"], "scene_count": len(scenes),
            "targets": targets, "requires": ["earthdata"], "requests": len(targets),
            "estimated_bytes": sum(t["source"].get("estimated_bytes") or 0 for t in targets),
            "fields": [f for f in spec["fields"] if f["id"] in options["fields"]],
            "scalar_fields": scalar, "grid": grid, "root_name": root_name, "folder_name": folder,
            "quality_paths": spec["quality_paths"],
            "output_bands": [f["id"] + "_mean" for f in scalar] if spec["kind"] == "footprint" else ["source_bands"]}


def _dataset(group, paths, *, required=True):
    for path in paths:
        value = group.get(path)
        if isinstance(value, h5py.Dataset):
            return value
    if required:
        raise UserError("GEDI 文件缺少所需参数：" + " / ".join(paths) + "。请核对产品与版本。")
    return None


def _clean_values(dataset, values):
    result = np.asarray(values, dtype=np.float64)
    valid = np.isfinite(result) & (result > -9999)
    for key in ("_FillValue", "missing_value"):
        if key in dataset.attrs:
            for fill in np.asarray(dataset.attrs[key]).reshape(-1):
                try:
                    valid &= result != float(fill)
                except (TypeError, ValueError):
                    pass
    return np.where(valid, result, np.nan)


def _read_shots(dataset, begin, end, total, *, profile=False):
    if dataset.ndim == 1 and dataset.shape[0] == total:
        values = dataset[begin:end]
    elif dataset.ndim == 2 and dataset.shape[0] == total:
        values = dataset[begin:end, :]
    elif profile and dataset.ndim == 2 and dataset.shape[1] == total:
        values = dataset[:, begin:end].T
    else:
        raise UserError("GEDI 参数维度与足迹数不一致：" + dataset.name.rsplit("/", 1)[-1])
    return _clean_values(dataset, values)


def _verify_source(path, spec, cancel=None, *, decode_all=False):
    try:
        if spec["kind"] == "raster":
            with rasterio.open(path) as raster:
                if not raster.crs or not raster.count or not raster.width or not raster.height:
                    raise ValueError
                if decode_all:
                    for _, window in raster.block_windows(1):
                        check_cancel(cancel)
                        raster.read(window=window)
                return {"format": "GeoTIFF", "crs": raster.crs.to_string(), "width": raster.width,
                        "height": raster.height, "count": raster.count, "pixel_size": list(raster.res)}
        with h5py.File(path, "r") as file:
            beams = [name for name in BEAMS if name in file]
            if not beams:
                raise ValueError
            for name in beams:
                group = file[name]
                shots = _dataset(group, ["shot_number"])
                if shots.ndim != 1:
                    raise ValueError
                total = shots.shape[0]
                for paths in (["delta_time", "geolocation/delta_time"],
                              ["lat_lowestmode", "geolocation/lat_lowestmode", "geolocation/latitude_bin0"],
                              ["lon_lowestmode", "geolocation/lon_lowestmode", "geolocation/longitude_bin0"]):
                    dataset = _dataset(group, paths)
                    if dataset.shape != (total,):
                        raise ValueError
                    if total:
                        dataset[0], dataset[-1]
            if decode_all:
                def decode(_, dataset):
                    if not isinstance(dataset, h5py.Dataset) or dataset.shape is None:
                        return
                    check_cancel(cancel)
                    if dataset.ndim == 0:
                        dataset[()]
                    elif not dataset.size:
                        return
                    elif dataset.chunks:
                        for selection in dataset.iter_chunks():
                            check_cancel(cancel)
                            dataset[selection]
                    elif dataset.size:
                        row_size = max(1, dataset.dtype.itemsize * math.prod(dataset.shape[1:]))
                        step = max(1, 4 * 1024**2 // row_size)
                        for begin in range(0, dataset.shape[0], step):
                            check_cancel(cancel)
                            dataset[begin:begin + step]
                file.visititems(decode)
            return {"format": "HDF5", "beams": beams}
    except (OSError, ValueError, rasterio.errors.RasterioError):
        raise UserError("下载内容不是可读的 GEDI HDF5 / GeoTIFF，未保存；请核对 Earthdata 授权。") from None


def _fetch_source(http, auth, target, root, spec, cancel, progress):
    source = target["source"]
    path = root / target["relative_path"]
    path.parent.mkdir(parents=True, exist_ok=True)
    receipt = path.with_name(path.name + ".starmap.json")
    expected = {"source_fingerprint": _fingerprint(source)}
    if path.exists() or receipt.exists():
        record = checked_local_receipt(path, receipt, expected, cancel)
        _verify_source(path, spec, cancel, decode_all=not source.get("checksum") and not record.get("all_datasets_decoded"))
        progress("已有 GEDI 原始文件通过 SHA-256 校验 · " + path.name, 1.0)
        return path, {**record, "path": str(path), "status": "skipped"}
    size = source.get("size")
    if size and shutil.disk_usage(path.parent).free < size + 16 * 1024**2:
        raise UserError("磁盘剩余空间不足以保存所选 GEDI 原始文件。")
    partial = path.with_name(path.name + ".part")
    if partial.exists():
        raise UserError("GEDI 下载目录中已有未确认的 .part 文件，请移走后重试。")
    version_tag = None
    try:
        for attempt in range(3):
            check_cancel(cancel)
            headers = {"If-Match": version_tag} if version_tag else {}
            response = request_data(http, auth, source["url"], cancel=cancel, headers=headers,
                                    stream=True, timeout=(12, 45))
            try:
                if response.status_code != 200:
                    raise UserError(f"NASA GEDI 文件下载失败（HTTP {response.status_code}）。请重新检索，核对文件权限。")
                if "html" in response.headers.get("Content-Type", "").lower():
                    raise UserError("NASA 返回了登录页面，请在 Earthdata Search 首次授权该数据中心，再重新连接。")
                if response.headers.get("Content-Encoding", "identity").lower() != "identity":
                    raise UserError("NASA 返回了未支持的压缩传输，无法核对原始文件长度。")
                length = response.headers.get("Content-Length")
                if length is not None and (not length.isdigit() or size is not None and int(length) != size):
                    raise UserError("NASA 文件长度与所选 CMR 修订不一致，请重新检索。")
                etag = response.headers.get("ETag")
                strong = etag if etag and etag.startswith('"') and etag.endswith('"') else None
                if version_tag and strong != version_tag:
                    raise UserError("GEDI 文件版本在传输中发生变化，未保存。请重新检索。")
                version_tag = strong
                sha = hashlib.sha256()
                checksum = source.get("checksum")
                digest = hashlib.new(CHECKSUMS[checksum["algorithm"]][0]) if checksum else None
                received = 0
                with partial.open("xb") as writer:
                    for block in response.iter_content(1024 * 1024):
                        check_cancel(cancel)
                        if not block:
                            continue
                        writer.write(block)
                        sha.update(block)
                        if digest:
                            digest.update(block)
                        received += len(block)
                        expected_size = size or (int(length) if length else None)
                        if expected_size and received > expected_size:
                            raise UserError("GEDI 接收字节数超过所选文件长度，未保存。")
                        progress(f"下载 GEDI · {path.name} · {received / 1024**2:.1f} MB", min(0.99, received / expected_size) if expected_size else 0.0)
                expected_size = size or (int(length) if length else None)
                if not received or expected_size and received != expected_size:
                    raise requests.exceptions.ChunkedEncodingError()
                if digest and digest.hexdigest() != checksum["value"]:
                    raise UserError("GEDI 文件未通过 NASA 官方 " + checksum["algorithm"] + " 校验，未保存。")
                check_cancel(cancel)
                progress("核对 GEDI 原始文件格式与完整性 · " + path.name, 0.99)
                format_info = _verify_source(partial, spec, cancel, decode_all=not checksum)
                record = {**expected, "size": received, "sha256": sha.hexdigest(), "source": source,
                          "provider_checksum_verified": checksum, "all_datasets_decoded": not bool(checksum), "etag": strong, **format_info}
                publish_file(partial, path)
                receipt.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
                progress("GEDI 原始文件校验完成 · " + path.name, 1.0)
                return path, {**record, "path": str(path), "status": "saved"}
            except (requests.ConnectionError, requests.Timeout, requests.exceptions.ChunkedEncodingError):
                if attempt == 2:
                    raise UserError("GEDI 文件传输多次中断，未发布不完整文件；已完成的原始文件保留。") from None
                if not source.get("checksum") and not version_tag:
                    raise UserError("GEDI 文件传输中断且缺少官方校验值或固定版本标识，请重新检索。") from None
                progress("GEDI 传输中断，重新接收并核对同一文件版本…", 0.0)
            finally:
                response.close()
                partial.unlink(missing_ok=True)
        raise AssertionError("GEDI retry limit")
    finally:
        partial.unlink(missing_ok=True)


def _epoch(group):
    master = _dataset(group, ["ancillary/master_time_epoch"], required=False)
    if master is None:
        return dt.datetime(2018, 1, 1, tzinfo=UTC)
    seconds = float(np.asarray(master[()]).reshape(-1)[0])
    # GPS-UTC has been 18 s throughout the GEDI mission. The ancillary epoch
    # includes this offset; subtract it once when converting GPS to UTC.
    epoch = dt.datetime(1980, 1, 6, tzinfo=UTC) + dt.timedelta(seconds=seconds - 18)
    if abs((epoch - dt.datetime(2018, 1, 1, tzinfo=UTC)).total_seconds()) > 60:
        raise UserError("GEDI 时间基准与产品说明不一致，请检查文件版本。")
    return epoch


def _waveform(group, name, index):
    prefix = "rx" if name == "rxwaveform" else "tx"
    start = int(_dataset(group, [prefix + "_sample_start_index"])[index]) - 1
    count = int(_dataset(group, [prefix + "_sample_count"])[index])
    dataset = _dataset(group, [name])
    if count == 0:
        return []
    if start < 0 or not 0 <= count <= 65535 or start + count > dataset.size:
        raise UserError("GEDI 波形采样索引超出数据范围。")
    return _json_array(_clean_values(dataset, dataset[start:start + count]))


def _json_array(values):
    return [float(value) if math.isfinite(value) else None for value in np.asarray(values).reshape(-1)]


def _rows(path, spec, plan, geometry, stats, cancel):
    options = plan["options"]
    first = dt.datetime.fromisoformat(plan["criteria"]["start"]).replace(tzinfo=UTC)
    stop = dt.datetime.fromisoformat(plan["criteria"]["end"]).replace(tzinfo=UTC) + dt.timedelta(days=1)
    with h5py.File(path, "r") as file:
        for beam in options["beams"]:
            check_cancel(cancel)
            if beam not in file:
                continue
            group = file[beam]
            shot_numbers = _dataset(group, ["shot_number"])
            total = len(shot_numbers)
            stats["total_shots"] += total
            if not total:
                continue
            latitude = _dataset(group, ["lat_lowestmode", "geolocation/lat_lowestmode", "geolocation/latitude_bin0"])
            longitude = _dataset(group, ["lon_lowestmode", "geolocation/lon_lowestmode", "geolocation/longitude_bin0"])
            times = _dataset(group, ["delta_time", "geolocation/delta_time"])
            epoch = _epoch(group)
            begin_delta, stop_delta = (first - epoch).total_seconds(), (stop - epoch).total_seconds()
            quality_datasets = [(name, _dataset(group, [name])) for name in plan["quality_paths"]]
            degrade = _dataset(group, ["degrade_flag", "geolocation/degrade"], required=options["nondegraded"])
            sensitivity = _dataset(group, ["sensitivity"], required=bool(options["min_sensitivity"]))
            field_datasets = {}
            for field in plan["fields"]:
                if field["kind"] == "derived":
                    field_datasets[field["id"]] = (_dataset(group, ["elev_lowestmode"]), _dataset(group, ["geophys_corr/geoid"]))
                elif field["kind"] != "waveform":
                    field_datasets[field["id"]] = _dataset(group, field["paths"])
            for begin in range(0, total, CHUNK_SHOTS):
                check_cancel(cancel)
                end = min(total, begin + CHUNK_SHOTS)
                lat = _read_shots(latitude, begin, end, total)
                lon = _read_shots(longitude, begin, end, total)
                delta = _read_shots(times, begin, end, total)
                inside = (np.isfinite(lat) & np.isfinite(lon) & (np.abs(lat) <= 90) & (np.abs(lon) <= 180)
                          & np.isfinite(delta) & (delta >= begin_delta) & (delta < stop_delta))
                candidates = np.flatnonzero(inside)
                if candidates.size:
                    inside[candidates] &= intersects_xy(geometry, lon[candidates], lat[candidates])
                stats["in_aoi_and_dates"] += int(inside.sum())
                quality = {name: _read_shots(dataset, begin, end, total) for name, dataset in quality_datasets}
                if options["quality"] == "recommended":
                    for values in quality.values():
                        inside &= values == spec["quality_value"]
                degraded = _read_shots(degrade, begin, end, total) if degrade is not None else None
                if options["nondegraded"]:
                    inside &= degraded == 0
                sensitive = _read_shots(sensitivity, begin, end, total) if sensitivity is not None else None
                if options["min_sensitivity"]:
                    inside &= np.isfinite(sensitive) & (sensitive >= options["min_sensitivity"]) & (sensitive <= 1)
                selected = np.flatnonzero(inside)
                if not selected.size:
                    continue
                field_values = {}
                for field in plan["fields"]:
                    name = field["id"]
                    if field["kind"] == "waveform":
                        continue
                    dataset = field_datasets[name]
                    if field["kind"] == "derived":
                        values = _read_shots(dataset[0], begin, end, total) - _read_shots(dataset[1], begin, end, total)
                    else:
                        values = _read_shots(dataset, begin, end, total, profile=field["kind"] == "profile")
                    if field["index"] is not None:
                        if values.ndim != 2 or values.shape[1] <= field["index"]:
                            raise UserError("GEDI RH 参数缺少所选百分位。")
                        values = values[:, field["index"]]
                    if field["kind"] in ("scalar", "derived") and values.ndim != 1:
                        raise UserError("GEDI 标量参数维度异常：" + name)
                    field_values[name] = values
                numbers = shot_numbers[begin:end]
                if not np.issubdtype(numbers.dtype, np.integer):
                    raise UserError("GEDI shot_number 不是精确的整型标识，无法安全关联。")
                for index in selected:
                    check_cancel(cancel)
                    number = str(int(numbers[index]))
                    row = {"shot_number": number, "shot_id": "GEDI:" + number, "beam": beam,
                           "datetime_utc": (epoch + dt.timedelta(seconds=float(delta[index]))).isoformat().replace("+00:00", "Z"),
                           "longitude": float(lon[index]), "latitude": float(lat[index]), "delta_time": float(delta[index])}
                    for name, values in quality.items():
                        value = values[index]
                        row[name] = int(value) if np.isfinite(value) else None
                    if degraded is not None:
                        row["degrade_flag"] = int(degraded[index]) if np.isfinite(degraded[index]) else None
                    for field in plan["fields"]:
                        name = field["id"]
                        if field["kind"] == "waveform":
                            row[name] = _waveform(group, name, begin + int(index))
                        elif field["kind"] == "profile":
                            row[name] = _json_array(field_values[name][index])
                        else:
                            value = float(field_values[name][index])
                            row[name] = value if math.isfinite(value) else None
                    yield row


class _ObservationGrid:
    def __init__(self, plan, database):
        self.grid, self.fields = plan["grid"], plan["scalar_fields"]
        self.database = database
        self.tile_columns = math.ceil(self.grid["width"] / 256)
        columns = ["pixel INTEGER PRIMARY KEY", "tile INTEGER", "shots INTEGER"]
        for band in range(len(self.fields)):
            columns.extend((f"s{band} REAL", f"c{band} INTEGER"))
        database.execute("CREATE TABLE cells (" + ",".join(columns) + ")")
        database.execute("CREATE INDEX cells_by_tile ON cells(tile)")
        updates = ["shots=cells.shots+1"]
        for band in range(len(self.fields)):
            updates.extend((f"s{band}=cells.s{band}+excluded.s{band}", f"c{band}=cells.c{band}+excluded.c{band}"))
        self.insert = ("INSERT INTO cells VALUES (" + ",".join("?" for _ in columns)
                       + ") ON CONFLICT(pixel) DO UPDATE SET " + ",".join(updates))

    def add(self, row, index):
        r, c = divmod(index, self.grid["width"])
        values = [index, r // 256 * self.tile_columns + c // 256, 1]
        for field in self.fields:
            value = row.get(field["id"])
            if value is not None and math.isfinite(value):
                values.extend((value, 1))
            else:
                values.extend((0.0, 0))
        self.database.execute(self.insert, values)

    def write(self, stage, cancel, progress):
        grid = self.grid
        if self.database.execute("SELECT 1 FROM cells LIMIT 1").fetchone() is None:
            return []
        profile = {"driver": "GTiff", "width": grid["width"], "height": grid["height"],
                   "transform": Affine(*grid["transform"]), "crs": grid["crs"], "tiled": True,
                   "blockxsize": 256, "blockysize": 256, "compress": "DEFLATE", "BIGTIFF": "IF_SAFER"}
        means, counts = stage / "gedi_metrics.tif", stage / "gedi_counts.tif"
        with rasterio.open(means, "w", count=len(self.fields), dtype="float32", nodata=NODATA, **profile) as values_writer, rasterio.open(counts, "w", count=len(self.fields) + 1, dtype="uint32", nodata=0, **profile) as count_writer:
            for band, field in enumerate(self.fields, 1):
                values_writer.set_band_description(band, field["id"] + "_mean")
                if field["units"]:
                    values_writer.set_band_unit(band, field["units"])
                values_writer.update_tags(band, aggregation="arithmetic mean of valid footprint values", parameter=field["id"], uncertainty_note="Means of SE or interval bounds are descriptive, not pooled uncertainty")
                count_writer.set_band_description(band + 1, field["id"] + "_count")
            count_writer.set_band_description(1, "shot_count")
            blocks = math.ceil(grid["height"] / 256) * self.tile_columns
            for block, (_, window) in enumerate(values_writer.block_windows(1)):
                check_cancel(cancel)
                r, c, h, w = map(int, (window.row_off, window.col_off, window.height, window.width))
                tile = r // 256 * self.tile_columns + c // 256
                records = self.database.execute("SELECT * FROM cells WHERE tile=?", (tile,))
                values = np.full((len(self.fields), h, w), NODATA, dtype="float32")
                count = np.zeros((len(self.fields) + 1, h, w), dtype="uint32")
                for record in records:
                    row, col = divmod(record[0], grid["width"])
                    row, col = row - r, col - c
                    count[0, row, col] = record[2]
                    for band in range(len(self.fields)):
                        n = record[4 + band * 2]
                        count[band + 1, row, col] = n
                        if n:
                            values[band, row, col] = record[3 + band * 2] / n
                values_writer.write(values, window=window)
                count_writer.write(count, window=window)
                if block % 128 == 0:
                    progress(f"写入 GEDI 对齐网格 · {block + 1}/{blocks} 区块", 0.95 + 0.04 * (block + 1) / blocks)
            for writer in (values_writer, count_writer):
                writer.update_tags(source="NASA GEDI footprints", interpolation="none", footprint_diameter_m="approximately 25", pixel_membership="footprint centre", outside_observations="NoData")
        return [means, counts]


def _sample_reference(raster, x, y):
    values = next(raster.sample([(x, y)], masked=True))
    return {f"reference_band_{index}": None if np.ma.is_masked(value) or not np.isfinite(value) else float(value) * raster.scales[index - 1] + raster.offsets[index - 1]
            for index, value in zip(raster.indexes, values, strict=True)}


def _export_footprints(paths, spec, plan, geometry, stage, cancel, progress):
    stats = {"total_shots": 0, "in_aoi_and_dates": 0, "accepted_shots": 0, "duplicate_shots": 0,
             "in_reference_grid": 0, "outside_reference_grid": 0, "empty_granules": []}
    grid, options = plan["grid"], plan["options"]
    aggregate = None
    columns = ["shot_number", "shot_id", "beam", "granule", "collection", "datetime_utc", "longitude", "latitude", "delta_time"]
    columns += plan["quality_paths"]
    columns += ["degrade_flag"]
    columns += [f["id"] for f in plan["fields"]]
    if grid:
        columns += ["x", "y", "grid_row", "grid_col"]
    reference = grid.get("reference") if grid else None
    if options["sample_reference"]:
        columns += [f"reference_band_{b['index']}" for b in reference["bands"]]
    # User-selected degrade_flag may be a quality field; keep CSV columns unique.
    columns = list(dict.fromkeys(columns))
    table, points = stage / "footprints.csv", stage / "footprints.geojson"
    transform_xy = Transformer.from_crs(4326, grid["crs"], always_xy=True) if grid else None
    inverse = ~Affine(*grid["transform"]) if grid else None
    with ExitStack() as stack:
        raster = stack.enter_context(rasterio.open(reference["path"])) if reference and options["sample_reference"] else None
        database_path = stage / "deduplicate.sqlite"
        database = sqlite3.connect(database_path)
        stack.callback(database.close)
        database.execute("CREATE TABLE seen (shot_number TEXT PRIMARY KEY)")
        if options["mode"] == "aligned":
            aggregate = _ObservationGrid(plan, database)
        csv_file = stack.enter_context(table.open("w", encoding="utf-8-sig", newline=""))
        json_file = stack.enter_context(points.open("w", encoding="utf-8"))
        writer = csv.DictWriter(csv_file, fieldnames=columns)
        writer.writeheader()
        json_file.write('{"type":"FeatureCollection","features":[')
        first_feature = True
        for index, path in enumerate(paths):
            before = stats["accepted_shots"]
            progress(f"读取 GEDI 足迹 · {index + 1}/{len(paths)} · 已保留 {before} 个", index / len(paths))
            for row in _rows(path, spec, plan, geometry, stats, cancel):
                if database.execute("INSERT OR IGNORE INTO seen VALUES (?)", (row["shot_number"],)).rowcount == 0:
                    stats["duplicate_shots"] += 1
                    continue
                row["granule"], row["collection"] = path.name, options["collection"]
                if grid:
                    x, y = transform_xy.transform(row["longitude"], row["latitude"])
                    col_float = inverse.a * x + inverse.b * y + inverse.c
                    row_float = inverse.d * x + inverse.e * y + inverse.f
                    inside = math.isfinite(col_float) and math.isfinite(row_float) and 0 <= col_float < grid["width"] and 0 <= row_float < grid["height"]
                    r, c = (math.floor(row_float), math.floor(col_float)) if inside else (None, None)
                    row.update(x=x if math.isfinite(x) else None, y=y if math.isfinite(y) else None, grid_row=r, grid_col=c)
                    stats["in_reference_grid" if inside else "outside_reference_grid"] += 1
                    if inside and aggregate:
                        aggregate.add(row, r * grid["width"] + c)
                    if raster:
                        row.update(_sample_reference(raster, x, y) if inside else {f"reference_band_{b}": None for b in raster.indexes})
                stats["accepted_shots"] += 1
                writer.writerow({column: json.dumps(row[column], separators=(",", ":"), allow_nan=False) if isinstance(row.get(column), list) else row.get(column) for column in columns})
                if not first_feature:
                    json_file.write(",")
                first_feature = False
                json.dump({"type": "Feature", "id": row["shot_id"], "geometry": {"type": "Point", "coordinates": [row["longitude"], row["latitude"]]}, "properties": row}, json_file, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
                if stats["accepted_shots"] % 8192 == 0:
                    progress(f"导出 GEDI · 已保留 {stats['accepted_shots']} 个足迹", (index + 0.5) / len(paths))
            database.commit()
            if stats["accepted_shots"] == before:
                stats["empty_granules"].append(path.name)
        json_file.write("]}")
        artifacts = [table, points]
        if aggregate:
            database.commit()
            progress("写入对齐的 GEDI 均值与观测数量网格", 0.95)
            artifacts.extend(aggregate.write(stage, cancel, progress))
    database_path.unlink(missing_ok=True)
    schema = {"fields": plan["fields"], "columns": columns, "geojson_crs": "EPSG:4326",
              "shot_number": "String in GeoJSON; import CSV as text. shot_id is a safe text join key for spreadsheets.",
              "quality_paths": plan["quality_paths"], "quality_filter": options["quality"], "vertical_datum": "Elevations are WGS84 ellipsoidal unless explicitly named egm2008; RH is relative height.",
              "reference_sampling": "Pixel containing footprint centre; file scale and offset applied; masked pixels are null.",
              "aggregation": "Arithmetic mean by footprint centre, no interpolation; per-field counts and shot_count. Averaged SE/intervals are not pooled uncertainty.",
              "profile_arrays": "Native array ordering retained; waveform start indices are 1-based in source HDF5."}
    schema_path = stage / "fields.json"
    schema_path.write_text(json.dumps(schema, ensure_ascii=False, indent=2), encoding="utf-8")
    artifacts.append(schema_path)
    return artifacts, stats


def _export_rasters(paths, plan, geometry, stage, cancel, progress):
    grid = plan["grid"]
    project = Transformer.from_crs(4326, grid["crs"], always_xy=True)
    projected = transform(geometry.segmentize(0.05), project.transform, interleaved=False)
    artifacts, empty = [], []
    for index, path in enumerate(paths):
        check_cancel(cancel)
        progress(f"按参考网格裁剪 GEDI 栅格 · {index + 1}/{len(paths)}", index / len(paths))
        output = stage / (path.stem + "_aligned.tif")
        valid_count = 0
        with rasterio.open(path) as source, WarpedVRT(source, crs=grid["crs"], transform=Affine(*grid["transform"]),
                                                    width=grid["width"], height=grid["height"], resampling=Resampling.nearest,
                                                    dtype="float32", nodata=NODATA) as warped:
            with rasterio.open(output, "w", driver="GTiff", width=grid["width"], height=grid["height"], count=source.count,
                               dtype="float32", crs=grid["crs"], transform=Affine(*grid["transform"]), nodata=NODATA,
                               tiled=True, blockxsize=256, blockysize=256, compress="DEFLATE", BIGTIFF="IF_SAFER") as writer:
                for band in source.indexes:
                    writer.set_band_description(band, source.descriptions[band - 1] or path.stem)
                    if source.units[band - 1]:
                        writer.set_band_unit(band, source.units[band - 1])
                for _, window in writer.block_windows(1):
                    check_cancel(cancel)
                    values = warped.read(window=window, masked=True).astype("float64")
                    outside = geometry_mask([mapping(projected)], out_shape=(int(window.height), int(window.width)),
                                            transform=writer.window_transform(window), invert=False)
                    valid = ~np.ma.getmaskarray(values) & np.isfinite(values.data) & ~outside[None, :, :]
                    converted = values.data * np.asarray(source.scales)[:, None, None] + np.asarray(source.offsets)[:, None, None]
                    result = np.where(valid, converted, NODATA).astype("float32")
                    valid_count += int(np.any(valid, axis=0).sum())
                    writer.write(result, window=window)
                writer.update_tags(source="NASA GEDI " + COLLECTIONS[plan["options"]["collection"]]["level"],
                                   source_filename=path.name, resampling="nearest", source_scales=json.dumps(source.scales),
                                   source_offsets=json.dumps(source.offsets), native_resolution="approximately 1 km",
                                   resolution_note="Alignment changes grid spacing, not the observation resolution")
        if valid_count:
            artifacts.append(output)
        else:
            output.unlink()
            empty.append(path.name)
    return artifacts, {"empty_granules": empty}


def _reuse_export(folder, plan, cancel):
    manifest_path = folder / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest["status"] != "completed" or manifest["fingerprint"] != plan["fingerprint"]:
            raise ValueError
        files = []
        for record in manifest["artifacts"]:
            name = record["name"]
            if not isinstance(name, str) or Path(name).name != name:
                raise ValueError
            path = folder / name
            check_cancel(cancel)
            if path.stat().st_size != record["size"] or sha256_file(path, cancel) != record["sha256"]:
                raise ValueError
            files.append({"path": str(path), "status": "skipped"})
        return files, manifest["statistics"]
    except (OSError, ValueError, TypeError, KeyError):
        raise UserError("已有 GEDI 导出结果缺少匹配的校验记录或文件已变化，请移动该导出目录后重试。") from None


def download_gedi(geometry, plan, output_dir, auth, *, cancel=None, progress=None):
    retained = []
    try:
        return _download_gedi(geometry, plan, output_dir, auth, retained, cancel=cancel, progress=progress)
    except Exception as error:
        files = [{"path": r["path"], "status": r["status"]} for r in retained]
        keep_download_result(error, files, io_path(output_dir) / plan["root_name"], None, plan)
        raise


def _download_gedi(geometry, plan, output_dir, auth, source_records, *, cancel=None, progress=None):
    progress = progress or (lambda *_: None)
    base = io_path(output_dir)
    base.mkdir(parents=True, exist_ok=True)
    folder = base / plan["folder_name"]
    root = base / plan["root_name"]
    spec = COLLECTIONS[plan["options"]["collection"]]
    check_cancel(cancel)
    if plan["grid"] and plan["grid"]["reference"]:
        if _reference_grid(plan["options"]["reference_path"]) != plan["grid"]:
            raise UserError("参考 GeoTIFF 已变化，请重新确认 GEDI 下载计划。")
    http = HttpClient()
    paths = []
    try:
        for index, target in enumerate(plan["targets"]):
            def source_progress(message, fraction):
                progress(message, 0.65 * (index + fraction) / len(plan["targets"]))
            path, record = _fetch_source(http, auth, target, root, spec, cancel, source_progress)
            paths.append(path)
            source_records.append(record)
    finally:
        http.close()
    if folder.exists():
        files, stats = _reuse_export(folder, plan, cancel)
        files.append({"path": str(folder / "manifest.json"), "status": "skipped"})
        if plan["options"]["mode"] == "original":
            files = [{"path": r["path"], "status": r["status"]} for r in source_records] + files
        return {"folder": str(folder), "files": files, "plan": plan, "empty_scenes": stats.get("empty_granules", []), "statistics": stats}
    folder.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=".gedi-export-", dir=folder.parent))
    try:
        reference = plan["grid"].get("reference") if plan["grid"] else None
        reference_sha = sha256_file(reference["path"], cancel) if reference else None
        if plan["options"]["mode"] == "original":
            artifacts, stats = [], {"empty_granules": []}
        elif spec["kind"] == "raster":
            artifacts, stats = _export_rasters(paths, plan, geometry, stage, cancel,
                                              lambda message, fraction: progress(message, 0.65 + 0.3 * fraction))
        else:
            artifacts, stats = _export_footprints(paths, spec, plan, geometry, stage, cancel,
                                                  lambda message, fraction: progress(message, 0.65 + 0.3 * fraction))
        if reference and _reference_grid(plan["options"]["reference_path"]) != plan["grid"]:
            raise UserError("参考 GeoTIFF 在导出过程中发生变化，未发布对齐结果。")
        records = []
        for path in artifacts:
            check_cancel(cancel)
            if path.suffix == ".tif":
                with rasterio.open(path) as raster:
                    for _, window in raster.block_windows(1):
                        check_cancel(cancel)
                        raster.read(window=window)
                    if raster.width != plan["grid"]["width"] or raster.height != plan["grid"]["height"] or raster.transform != Affine(*plan["grid"]["transform"]) or raster.crs != rasterio.crs.CRS.from_user_input(plan["grid"]["crs"]):
                        raise UserError("GEDI 输出网格与参考网格不一致，未发布结果。")
            records.append({"name": path.name, "size": path.stat().st_size, "sha256": sha256_file(path, cancel)})
        aoi = stage / "aoi.geojson"
        aoi.write_text(json.dumps({"type": "FeatureCollection", "features": [{"type": "Feature", "properties": {}, "geometry": mapping(geometry)}]}, ensure_ascii=False), encoding="utf-8")
        records.append({"name": aoi.name, "size": aoi.stat().st_size, "sha256": sha256_file(aoi, cancel)})
        manifest = {"software": "星图下载器 " + VERSION, "status": "completed", "fingerprint": plan["fingerprint"],
                    "source": "NASA Earthdata / CMR; LP DAAC and ORNL DAAC", "plan": plan,
                    "sources": source_records, "reference_sha256": reference_sha, "statistics": stats, "artifacts": records}
        (stage / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
        check_cancel(cancel)
        if folder.exists():
            raise UserError("GEDI 导出目录已出现，未覆盖现有结果。")
        stage.rename(folder)
        files = [{"path": str(folder / r["name"]), "status": "saved"} for r in records]
        files.append({"path": str(folder / "manifest.json"), "status": "saved"})
        if plan["options"]["mode"] == "original":
            files = [{"path": r["path"], "status": r["status"]} for r in source_records] + files
        progress(f"GEDI 下载与导出完成 · {stats.get('accepted_shots', 0)} 个足迹" if spec["kind"] == "footprint" and plan["options"]["mode"] != "original" else "GEDI 下载与导出完成", 1.0)
        return {"folder": str(folder), "files": files, "plan": plan, "empty_scenes": stats.get("empty_granules", []), "statistics": stats}
    finally:
        if stage.exists():
            shutil.rmtree(stage)
