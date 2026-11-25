"""Exact Sentinel-2 SAFE ZIP downloads and optional local analysis of those archives."""
from __future__ import annotations

import copy
import hashlib
import json
import re
import shutil
import xml.etree.ElementTree as ET
import zipfile
import zlib
from pathlib import Path, PurePosixPath

from shapely import normalize
from shapely.geometry import Polygon, mapping
from shapely.ops import unary_union
from uuid import UUID

from .errors import UserError
from .http import check_cancel
from .native_catalogue import DOWNLOAD_ROOT, download_asset, raw_item, search_original
from .native_mosaic import NATIVE_RESOLUTION, _nodes, _one, _xml, native_metadata
from .raw_download import raw_plan
from .scene_identity import SAFE_ID, utc_text, utc_time
from .spatial_selection import footprint, polygon_area

PROFILE = "s2-cdse-zip"
ARCHIVE_KIND = "cdse-safe-zip"


def download_options(options=None):
    supplied = options or {}
    engine = supplied.get("download_engine", "auto")
    workers = supplied.get("download_workers", 2)
    if engine not in ("auto", "edge", "http", "http-direct"):
        raise UserError("请选择自动、Edge、标准或直连下载。")
    if type(workers) is not int or not 1 <= workers <= 4:
        raise UserError("完整产品同时下载数应为 1～4，不能超过官方连接配额。")
    return {"download_engine": engine, "download_workers": workers}


def check_zip_target(target):
    proof = target.get("identity") or {}
    name = target.get("name", "")
    try:
        identifier = str(UUID(target["uuid"]))
    except (ValueError, KeyError, TypeError, AttributeError):
        raise UserError("整景 ZIP 缺少有效的官方产品编号。") from None
    if (proof.get("kind") != ARCHIVE_KIND or proof.get("uuid") != identifier
            or proof.get("product") != name or not SAFE_ID.fullmatch(name)
            or target.get("transport") != "odata" or target.get("asset") is not None
            or target.get("url") != f"{DOWNLOAD_ROOT}/Products({identifier})/$value"
            or download_asset(target["url"])[0] != identifier):
        raise UserError("整景 ZIP 的官方产品编号、名称或下载入口不一致。")
    return name


def search_zip(geometry, start, end, max_cloud, *, cancel=None, progress=None):
    """Use the official catalogue, preserving the exact product used by Browser downloads."""
    def parse(feature, region, collection, first=None, last=None, **kwargs):
        original = raw_item(feature, region, collection, first, last, **kwargs)
        if original is None:
            return None
        parsed = SAFE_ID.fullmatch(original["key"])
        cloud = original.get("cloud_max")
        if not parsed or cloud is not None and cloud > max_cloud:
            return None
        scene = copy.deepcopy(original)
        name = original["key"].removesuffix(".SAFE") + ".SAFE"
        scene.update(key=name, title=name, platform=parsed[1], tiles=[parsed[3]],
                     overpass_key=parsed[1] + "_" + parsed[2])
        scene["products"][0].update(id=name, acquisition_utc=scene["datetime"])
        scene["raw"]["available"] = bool(scene["raw"]["uuid"])
        if not scene["raw"]["available"]:
            scene["raw"]["unavailable_reason"] = "官方目录没有提供此版本的完整 ZIP 下载入口"
        return scene

    return search_original(geometry, "sentinel-2-l2a", start, end, cancel=cancel,
                           progress=progress, row_parser=parse)


def build_zip_plan(geometry, scenes, options=None):
    original_options = {"collection": "sentinel-2-l2a", "mode": "original", "transport": "odata",
                        "archive_kind": ARCHIVE_KIND}
    options = {**original_options, **download_options(options)}
    plan = raw_plan(geometry, scenes, options)
    for target in plan["targets"]:
        target["name"] = target["key"].removesuffix(".SAFE") + ".SAFE"
        target["identity"] = {"kind": ARCHIVE_KIND, "product": target["name"], "uuid": target["uuid"]}
        check_zip_target(target)
    # Published JP2/member sizes are an estimate of the archive, not a ZIP Content-Length.
    estimated = sum(asset.get("bytes") or 0 for scene in scenes for asset in scene["raw"]["assets"])
    covered = geometry.intersection(unary_union([footprint(scene) for scene in scenes]))
    signature = {"schema": 1, "geometry": mapping(normalize(geometry)), "targets": plan["targets"], "options": original_options}
    # Execution choices belong to the frozen task, while the output/cache path
    # remains compatible with already verified 1.9.0 products.
    storage_id = hashlib.sha256(json.dumps(signature, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:16]
    signature["options"] = options
    fingerprint = hashlib.sha256(json.dumps(signature, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:16]
    plan.update(fingerprint=fingerprint, folder_name="S2_SAFE_ZIP_" + storage_id,
                source="Copernicus Browser / CDSE official whole-product ZIP", requires=["odata"],
                estimated_bytes=estimated or None, size_estimated=True,
                native_source_bytes=estimated, source_count=len(scenes),
                archive_count=len(scenes), pipeline="download-zip",
                download_engine=options["download_engine"], download_workers=min(options["download_workers"], len(scenes)),
                coverage={"coverage_percent": min(100., 100 * polygon_area(covered) / polygon_area(geometry))},
                source_note="逐产品下载与官网相同的完整 SAFE ZIP，保留全部波段、质量图层与元数据。")
    return plan


def zip_source_plan(scene, options):
    return build_zip_plan(footprint(scene), [scene], options)


def storage_plan(plan):
    """Restore the content contract used by 1.9.0 for existing output reuse."""
    value = copy.deepcopy(plan)
    for key in ("download_engine", "download_workers"):
        value.pop(key, None)
        value["options"].pop(key, None)
    value["fingerprint"] = value["folder_name"].removeprefix("S2_SAFE_ZIP_")
    return value


def verify_safe_archive(path, target, cancel=None):
    """Verify all ZIP CRCs and every declared SAFE checksum in one archive read."""
    product = check_zip_target(target)
    root = product + "/"
    try:
        with zipfile.ZipFile(path) as bundle:
            infos = bundle.infolist()
            names = [info.filename for info in infos]
            if not 0 < len(infos) <= 150000 or len(names) != len(set(names)):
                raise ValueError("Invalid archive inventory")
            for name in names:
                parts = PurePosixPath(name).parts
                if (not name.startswith(root) or ".." in parts or any(c in name for c in "\\:\x00")
                        or name.startswith("/")):
                    raise ValueError("Invalid archive member")
            manifest_name, metadata_name = root + "manifest.safe", root + "MTD_MSIL2A.xml"
            if any(not 0 < bundle.getinfo(name).file_size <= 16 * 1024**2 for name in (manifest_name, metadata_name)):
                raise ValueError("Invalid metadata size")
            manifest_bytes = bundle.read(manifest_name)
            product_bytes = bundle.read(metadata_name)
            if any(b"<!DOCTYPE" in data.upper() or b"<!ENTITY" in data.upper() for data in (manifest_bytes, product_bytes)):
                raise ValueError("Unsupported XML declaration")
            manifest, metadata = ET.fromstring(manifest_bytes), ET.fromstring(product_bytes)
            if _one(metadata, "PRODUCT_URI").text.removesuffix(".SAFE") != product.removesuffix(".SAFE"):
                raise ValueError("Product identity mismatch")
            declared = {}
            for obj in _nodes(manifest, "dataObject"):
                stream = _one(obj, "byteStream")
                location = _one(stream, "fileLocation").attrib["href"].removeprefix("./")
                parts = PurePosixPath(location).parts
                if not parts or location.startswith("/") or ".." in parts or any(c in location for c in "\\:\x00"):
                    raise ValueError("Invalid declared member")
                name = root + location
                checksum = _one(stream, "checksum")
                algorithm = checksum.attrib["checksumName"].lower().replace("-", "")
                if algorithm == "sha3256":
                    algorithm = "sha3_256"
                if algorithm not in ("md5", "sha256", "sha3_256"):
                    raise ValueError("Unsupported member checksum")
                value = checksum.text.strip().lower()
                if not re.fullmatch(r"[0-9a-f]{" + str(hashlib.new(algorithm).digest_size * 2) + "}", value):
                    raise ValueError("Invalid declared checksum")
                size = int(stream.attrib["size"])
                if name in declared or name not in names or bundle.getinfo(name).is_dir() or bundle.getinfo(name).file_size != size:
                    raise ValueError("Incomplete SAFE inventory")
                declared[name] = (algorithm, value)
            if not declared:
                raise ValueError("Missing SAFE checksums")
            files, checked, total = 0, 0, 0
            for info in infos:
                check_cancel(cancel)
                if info.is_dir():
                    continue
                files += 1
                total += info.file_size
                evidence = declared.get(info.filename)
                digest = hashlib.new(evidence[0]) if evidence else None
                with bundle.open(info) as source:
                    while block := source.read(1024 * 1024):
                        check_cancel(cancel)
                        if digest:
                            digest.update(block)
                if evidence:
                    if digest.hexdigest() != evidence[1]:
                        raise ValueError("SAFE member checksum mismatch")
                    checked += 1
            if not total or checked != len(declared):
                raise ValueError("Incomplete product")
    except (zipfile.BadZipFile, RuntimeError, NotImplementedError, ValueError, KeyError,
            AttributeError, ET.ParseError, EOFError, OSError, zlib.error):
        raise UserError("完整 SAFE ZIP 的产品身份、成员 CRC 或清单校验未通过，未保存损坏的产品。") from None
    return {"product": product, "zip_files_crc_verified": files, "declared_members_checksum_verified": checked,
            "uncompressed_member_bytes": total, "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest()}


def _copy_member(bundle, member, path, cancel):
    """Reuse verified local members; otherwise rebuild from the already verified ZIP."""
    info = bundle.getinfo(member)
    if path.is_file() and path.stat().st_size == info.file_size:
        crc = 0
        with path.open("rb") as source:
            while block := source.read(1024 * 1024):
                check_cancel(cancel)
                crc = zlib.crc32(block, crc)
        if crc & 0xffffffff == info.CRC:
            return
    partial = path.with_name(path.name + ".part")
    try:
        with bundle.open(member) as source, partial.open("wb") as target:
            while block := source.read(1024 * 1024):
                check_cancel(cancel)
                target.write(block)
        check_cancel(cancel)
        partial.replace(path)
    finally:
        partial.unlink(missing_ok=True)


def prepare_zip_source(scene, downloaded, options, directory, *, cancel=None, progress=None):
    """Extract selected native members locally, then verify SAFE identity and calibration."""
    archive = Path(downloaded["files"][0]["path"])
    receipt = json.loads(archive.with_suffix(".starmap.json").read_text(encoding="utf-8"))
    if receipt.get("uuid") != scene["raw"]["uuid"] or not re.fullmatch(r"[0-9a-f]{64}", receipt.get("sha256", "")):
        raise UserError("完整 ZIP 的本地校验记录与所选产品不一致。")
    folder = directory / receipt["sha256"][:24]
    folder.mkdir(parents=True, exist_ok=True)
    root = scene["title"] + "/"
    try:
        with zipfile.ZipFile(archive) as bundle:
            names = [info.filename for info in bundle.infolist() if not info.is_dir()]
            if not names or any(not name.startswith(root) for name in names):
                raise UserError("ZIP 内的完整 SAFE 产品名称与选择不一致。")
            members = {"product_metadata": root + "MTD_MSIL2A.xml", "safe_manifest": root + "manifest.safe"}
            tiles = [name for name in names if name.startswith(root + "GRANULE/") and name.endswith("/MTD_TL.xml")]
            if len(tiles) != 1:
                raise UserError("所选 SAFE ZIP 没有唯一的原始瓦片元数据。")
            members["granule_metadata"] = tiles[0]
            granule = tiles[0].removesuffix("MTD_TL.xml")
            for band in options["bands"] + ["SCL"]:
                resolution = NATIVE_RESOLUTION[band]
                matches = [name for name in names if name.startswith(granule + f"IMG_DATA/R{resolution}m/")
                           and name.endswith(f"_{band}_{resolution}m.jp2")]
                if len(matches) != 1:
                    raise UserError(f"所选 SAFE ZIP 缺少唯一的原生 {band} 波段。")
                members[f"{band}_{resolution}m"] = matches[0]
            required = sum(bundle.getinfo(member).file_size for member in members.values())
            if shutil.disk_usage(folder).free < required:
                raise UserError("磁盘空间不足以从已保存 ZIP 准备本地分析波段。")
            paths, targets = {}, []
            for index, (asset, member) in enumerate(members.items()):
                check_cancel(cancel)
                path = folder / (asset + Path(member).suffix)
                if progress:
                    progress(f"从已保存 ZIP 准备本地波段 · {index + 1}/{len(members)}", index / len(members))
                _copy_member(bundle, member, path, cancel)
                paths[asset] = path
                targets.append({"asset": asset, "name": PurePosixPath(member).name})
        product, tile = _xml(paths["product_metadata"]), _xml(paths["granule_metadata"])
        coordinates = [float(value) for value in _one(product, "EXT_POS_LIST").text.split()]
        polygon = Polygon([(coordinates[index + 1], coordinates[index]) for index in range(0, len(coordinates), 2)])
        acquired = utc_time(_one(tile, "SENSING_TIME").text)
        local_scene = copy.deepcopy(scene)
        local_scene["products"][0].update(footprint=mapping(polygon), acquisition_utc=utc_text(acquired))
        local_scene.update(datetime=utc_text(acquired), date=acquired.date().isoformat(), time=acquired.strftime("%H:%M:%S"))
        metadata = native_metadata(paths, targets, local_scene, cancel)
    except (zipfile.BadZipFile, KeyError, ValueError, IndexError, OSError) as error:
        raise UserError("已保存 ZIP 的原始波段或元数据无法完整读取，未发布分析影像。") from error
    metadata.update(zip_path=str(archive), zip_sha256=receipt["sha256"], zip_uuid=receipt["uuid"],
                    catalogue_acquisition_utc=scene["datetime"])
    return {"scene": local_scene,
            "paths": {band: paths[f"{band}_{NATIVE_RESOLUTION[band]}m"] for band in options["bands"] + ["SCL"]},
            "result": downloaded, "metadata": metadata, "matches": ["safe_id"]}
