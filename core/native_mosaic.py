"""Regional mosaics from checksum-verified, exact CDSE Sentinel-2 native objects."""
from __future__ import annotations

import copy
import datetime as dt
import hashlib
import math
import xml.etree.ElementTree as ET
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from pathlib import Path, PurePosixPath

import numpy as np
import rasterio
from rasterio.enums import Resampling
from rasterio.transform import Affine, from_origin
from rasterio.vrt import WarpedVRT
from shapely.geometry import Polygon, box, mapping, shape

from .errors import UserError
from .http import check_cancel
from .native_catalogue import search_original
from .raw_download import download_original, raw_plan
from .research_bundle import NODATA, SCL_NODATA
from .scene_identity import SAFE_ID, expected_products, utc_time
from .spatial_selection import footprint

PROFILE = "s2-cdse-s3"
NATIVE_RESOLUTION = {"B01": 60, "B02": 10, "B03": 10, "B04": 10, "B05": 20, "B06": 20,
                     "B07": 20, "B08": 10, "B8A": 20, "B09": 60, "B11": 20, "B12": 20, "SCL": 20}


def native_assets(options):
    return ["product_metadata", "granule_metadata", "safe_manifest"] + [
        f"{band}_{NATIVE_RESOLUTION[band]}m" for band in options["bands"] + ["SCL"]]


def native_source_plan(scene, options):
    return raw_plan(footprint(scene), [scene["native"]["scene"]], {
        "collection": "sentinel-2-l2a", "mode": "assets", "transport": "cdse-s3", "assets": native_assets(options)})


def search_native(geometry, start, end, max_cloud, auth, cache, *, cancel=None, progress=None):
    if not auth.connected:
        raise UserError("请先连接 CDSE S3 密钥，以核对官方边界和采集时间。")
    # The CDSE catalogue can trim valid native pixels from edge granules. Search nearby
    # candidates, then use their exact product XML to perform the final AOI intersection.
    candidates = geometry.buffer(.2).intersection(box(-180, -90, 180, 90))
    scenes = search_original(candidates, "sentinel-2-l2a", start, end, cancel=cancel, progress=progress)

    def resolve(original):
        check_cancel(cancel)
        parsed = SAFE_ID.fullmatch(original["key"])
        if not parsed:
            return None
        scene = {key: copy.deepcopy(value) for key, value in original.items() if key != "raw"}
        identifier = original["key"].removesuffix(".SAFE") + ".SAFE"
        scene.update(key=identifier, title=identifier, platform=parsed[1], tiles=[parsed[3]],
                     overpass_key=parsed[1] + "_" + parsed[2],
                     native={"scene": original, "available": False})
        scene["products"][0].update(id=identifier, acquisition_utc=scene["datetime"])
        try:
            native_source_plan(scene, {"bands": ["B02", "B03", "B04", "B08", "B05", "B06", "B07", "B8A", "B11", "B12"]})
        except UserError:
            scene["native"]["unavailable_reason"] = "此产品缺少完整的研究波段、SCL 或原始 XML 的 S3 入口"
            return scene if footprint(scene).intersection(geometry).area > 0 else None
        plan = raw_plan(footprint(scene), [original], {"collection": "sentinel-2-l2a", "mode": "assets",
                       "transport": "cdse-s3", "assets": ["product_metadata", "granule_metadata"]})

        def update(message, fraction=0, detail=None):
            if progress:
                progress(f"核对官方元数据 · {parsed[3]} · {original['date']}", .5)

        downloaded = download_original(footprint(scene), plan, str(cache), None, s3_auth=auth, cancel=cancel, progress=update)
        paths = {target["asset"]: Path(file["path"]) for target, file in zip(plan["targets"], downloaded["files"], strict=True)}
        try:
            product, tile = _xml(paths["product_metadata"]), _xml(paths["granule_metadata"])
            if (_one(product, "PRODUCT_URI").text.removesuffix(".SAFE") != identifier.removesuffix(".SAFE")
                    or "_" + parsed[3] + "_" not in _one(tile, "TILE_ID").text):
                raise ValueError
            acquired = utc_time(_one(tile, "SENSING_TIME").text)
            cloud = float(_one(product, "Cloud_Coverage_Assessment").text)
            coordinates = [float(value) for value in _one(product, "EXT_POS_LIST").text.split()]
            region = Polygon([(coordinates[i + 1], coordinates[i]) for i in range(0, len(coordinates), 2)])
            if not region.is_valid or region.is_empty or not 0 <= cloud <= 100:
                raise ValueError
        except (ValueError, KeyError, TypeError, AttributeError, ET.ParseError, IndexError, OSError):
            raise UserError("官方 S3 元数据的产品、采集时间或覆盖范围无效，未采用该目录。") from None
        if not start <= acquired.date().isoformat() <= end or cloud > max_cloud or region.intersection(geometry).area <= 0:
            return None
        scene.update(datetime=acquired.isoformat().replace("+00:00", "Z"), date=acquired.date().isoformat(),
                     time=acquired.strftime("%H:%M:%S"), cloud_min=cloud, cloud_max=cloud,
                     footprint_basis="official product XML", time_kind="granule_sensing")
        scene["products"][0].update(footprint=mapping(region), acquisition_utc=scene["datetime"], cloud=cloud)
        scene["native"].update(available=True, catalogue_metadata_manifest=downloaded["manifest"])
        return scene

    with ThreadPoolExecutor(max_workers=3, thread_name_prefix="native-metadata") as pool:
        result = [scene for scene in pool.map(resolve, scenes) if scene is not None]
    return sorted(result, key=lambda scene: (scene["datetime"], scene["key"]), reverse=True)


def _tag(node):
    return node.tag.rsplit("}", 1)[-1]


def _nodes(root, name):
    return [node for node in root.iter() if _tag(node) == name]


def _one(root, name):
    node, = _nodes(root, name)
    return node


def _xml(path):
    if not 0 < path.stat().st_size <= 16 * 1024**2:
        raise ValueError("Invalid native metadata size")
    return ET.fromstring(path.read_bytes())


def native_metadata(paths, targets, scene, cancel=None):
    """Bind source identity, calibration, grids and every selected member to the native SAFE manifest."""
    try:
        product, tile, manifest = (_xml(paths[role]) for role in ("product_metadata", "granule_metadata", "safe_manifest"))
        expected, = expected_products(scene)
        sensing = utc_time(_one(tile, "SENSING_TIME").text)
        datatake = dt.datetime.strptime(SAFE_ID.fullmatch(expected["id"])[2], "%Y%m%dT%H%M%S").replace(tzinfo=dt.timezone.utc)
        if (_tag(product) != "Level-2A_User_Product" or _tag(tile) != "Level-2A_Tile_ID"
                or _one(product, "PRODUCT_URI").text.removesuffix(".SAFE") != expected["id"]
                or sensing != utc_time(expected["utc"])
                or not -60 <= (sensing - datatake).total_seconds() <= 5400
                or "_" + expected["tile"] + "_" not in _one(tile, "TILE_ID").text):
            raise ValueError("Native product identity mismatch")
        coordinate_values = [float(value) for value in _one(product, "EXT_POS_LIST").text.split()]
        if len(coordinate_values) < 8 or len(coordinate_values) % 2:
            raise ValueError("Native footprint invalid")
        polygon = Polygon([(coordinate_values[i + 1], coordinate_values[i]) for i in range(0, len(coordinate_values), 2)])
        if not polygon.is_valid or polygon.is_empty or not footprint(scene).equals(polygon):
            raise ValueError("Native footprint invalid")
        quantification = float(_one(product, "BOA_QUANTIFICATION_VALUE").text)
        if not math.isfinite(quantification) or quantification <= 0:
            raise ValueError("Invalid BOA quantification")
        physical = {node.attrib["bandId"]: ("B8A" if node.attrib["physicalBand"] == "B8A" else
                    "B" + node.attrib["physicalBand"][1:].zfill(2)) for node in _nodes(product, "Spectral_Information")}
        offsets = {physical[node.attrib["band_id"]]: float(node.text) for node in _nodes(product, "BOA_ADD_OFFSET")}
        baseline = float(_one(product, "PROCESSING_BASELINE").text)
        if not math.isfinite(baseline) or baseline < 0:
            raise ValueError("Invalid processing baseline")
        if baseline < 4:
            offsets = {band: 0.0 for band in NATIVE_RESOLUTION if band != "SCL"}
        if any(not math.isfinite(value) for value in offsets.values()):
            raise ValueError("Invalid BOA offsets")
        epsg = int(_one(tile, "HORIZONTAL_CS_CODE").text.split(":")[-1])
        grid_root, grids = _one(tile, "Tile_Geocoding"), {}
        for size in _nodes(grid_root, "Size"):
            resolution = int(size.attrib["resolution"])
            position, = [node for node in _nodes(grid_root, "Geoposition") if int(node.attrib["resolution"]) == resolution]
            width, height = int(_one(size, "NCOLS").text), int(_one(size, "NROWS").text)
            x, y = float(_one(position, "ULX").text), float(_one(position, "ULY").text)
            dx, dy = float(_one(position, "XDIM").text), float(_one(position, "YDIM").text)
            if resolution not in (10, 20, 60) or (dx, dy) != (resolution, -resolution) or min(width, height) < 1:
                raise ValueError("Invalid native grid")
            grids[resolution] = {"width": width, "height": height, "epsg": epsg, "transform": [dx, 0, x, 0, dy, y]}
        declared = {}
        for obj in _nodes(manifest, "dataObject"):
            stream = _one(obj, "byteStream")
            location = _one(stream, "fileLocation").attrib["href"].removeprefix("./")
            parts = PurePosixPath(location).parts
            if not parts or ".." in parts or location.startswith("/"):
                raise ValueError("Invalid SAFE member path")
            checksum = _one(stream, "checksum")
            algorithm = checksum.attrib["checksumName"].lower().replace("-", "")
            if algorithm == "sha3256":
                algorithm = "sha3_256"
            if algorithm not in ("sha256", "sha3_256", "md5"):
                raise ValueError("Unsupported SAFE checksum")
            declared.setdefault(parts[-1], []).append((int(stream.attrib["size"]), algorithm, checksum.text.strip().lower()))
        checked = []
        for target in targets:
            check_cancel(cancel)
            path = paths[target["asset"]]
            if target["asset"] == "safe_manifest":
                continue  # The manifest cannot checksum itself; its HTTPS/S3 identity is retained by the raw receipt.
            size, algorithm, expected_hash = declared[target["name"]][0]
            if len(declared[target["name"]]) != 1 or path.stat().st_size != size:
                raise ValueError("Native member size mismatch")
            digest = hashlib.new(algorithm)
            with path.open("rb") as stream:
                while block := stream.read(1024 * 1024):
                    check_cancel(cancel)
                    digest.update(block)
            if digest.hexdigest() != expected_hash:
                raise ValueError("Native SAFE member checksum mismatch")
            checked.append({"asset": target["asset"], "name": target["name"], "bytes": size,
                            "official_checksum_algorithm": algorithm, "official_checksum": expected_hash})
        return {"product_id": expected["id"], "acquisition_utc": _one(tile, "SENSING_TIME").text,
                "quantification": quantification, "offsets": offsets, "grids": grids,
                "official_footprint": mapping(polygon), "manifest_checked_members": checked,
                "angle_nodes": {name: len(_nodes(tile, name)) for name in ("Sun_Angles_Grid", "Mean_Sun_Angle",
                                                                         "Viewing_Incidence_Angles_Grids", "Mean_Viewing_Incidence_Angle")}}
    except (ValueError, KeyError, TypeError, AttributeError, ET.ParseError, OSError, OverflowError):
        raise UserError("官方 S3 原件的产品标识、定标、网格或 SAFE 清单校验不一致，未发布拼接。") from None


class NativeReader:
    def __init__(self, plan, options, output, auth, cancel, report):
        self.plan, self.options, self.output, self.auth, self.cancel, self.report = plan, options, output, auth, cancel, report
        self.records, self.evidence, self.opened = {}, {}, OrderedDict()

    def _source(self, scene):
        key = scene["key"]
        if key not in self.records:
            plan = native_source_plan(scene, self.options)

            def progress(message, fraction=0, detail=None):
                self.report(f"官方 S3 · {scene['tiles'][0]} · {scene['date']} · {message}")

            result = download_original(footprint(scene), plan, str(self.output), None, s3_auth=self.auth,
                                       cancel=self.cancel, progress=progress)
            paths = {target["asset"]: Path(file["path"])
                     for target, file in zip(plan["targets"], result["files"], strict=True)}
            metadata = native_metadata(paths, plan["targets"], scene, self.cancel)
            self.records[key] = paths, metadata
            self.evidence[key] = {"manifest": result["manifest"], **metadata}
        if key not in self.opened:
            while len(self.opened) >= 2:
                self.opened.popitem(last=False)[1][0].close()
            paths, metadata = self.records[key]
            stack = ExitStack()
            try:
                datasets = {}
                for band in self.options["bands"] + ["SCL"]:
                    alias, resolution = f"{band}_{NATIVE_RESOLUTION[band]}m", NATIVE_RESOLUTION[band]
                    ds = stack.enter_context(rasterio.open(paths[alias]))
                    expected = metadata["grids"][resolution]
                    if (ds.count != 1 or ds.crs is None or ds.crs.to_epsg() != expected["epsg"]
                            or ds.shape != (expected["height"], expected["width"])
                            or ds.transform != Affine(*expected["transform"])):
                        raise UserError("官方原始波段与 XML 的像元网格不一致，未发布拼接。")
                    if band != "SCL" and band not in metadata["offsets"]:
                        raise UserError("官方 XML 缺少所选波段的反射率偏移，未发布拼接。")
                    datasets[band] = ds
                self.opened[key] = stack, datasets
            except BaseException:
                stack.close()
                raise
        self.opened.move_to_end(key)
        return self.opened[key][1], self.records[key][1]

    def read(self, scene, tile, inside):
        datasets, metadata = self._source(scene)
        grid = from_origin(tile.bounds[0], tile.bounds[3], self.plan["resolution"], self.plan["resolution"])
        values, quality = [], None
        for band, ds in datasets.items():
            check_cancel(self.cancel)
            with WarpedVRT(ds, crs=f"EPSG:{self.plan['epsg']}", transform=grid, width=tile.width, height=tile.height,
                           resampling=Resampling.nearest, src_nodata=SCL_NODATA if band == "SCL" else 0,
                           nodata=SCL_NODATA if band == "SCL" else 0) as warped:
                dn = warped.read(1)
            if band == "SCL":
                quality = dn.astype(np.uint8)
            else:
                reflectance = (dn.astype(np.float32) + np.float32(metadata["offsets"][band])) / np.float32(metadata["quantification"])
                reflectance[dn == 0] = NODATA
                values.append(reflectance)
        return np.stack(values), quality, {"sources": [{"catalogue_id": metadata["product_id"], "match": "safe_id"}],
                                          "native_source_manifest": self.evidence[scene["key"]]["manifest"]}

    def close(self):
        while self.opened:
            self.opened.popitem()[1][0].close()


def download_native_mosaic(geometry, scenes, options, output_dir, auth, *, cancel=None, progress=None, planned=None):
    from .mosaic import download_mosaic

    if not auth.connected:
        raise UserError("请先连接 CDSE S3 Access Key 和 Secret Key，以读取官方原始像元。")
    return download_mosaic(geometry, scenes, options, output_dir, auth=auth, cancel=cancel, progress=progress, planned=planned)
