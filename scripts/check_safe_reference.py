"""Read real Sentinel-2 SAFE ZIP metadata; optionally verify every official member."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import time
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path, PurePosixPath

from pyproj import Transformer
from shapely import transform
from shapely.geometry import Polygon, mapping


def local_tag(node):
    return node.tag.rsplit("}", 1)[-1]


def nodes(root, name):
    return [node for node in root.iter() if local_tag(node) == name]


def one(root, name):
    values = nodes(root, name)
    if len(values) != 1:
        raise ValueError(f"Expected one {name}, found {len(values)}")
    return values[0]


def read_reference(path, *, deep=False):
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        if len(names) != len(set(names)):
            raise ValueError("Duplicate SAFE members")
        roots = {PurePosixPath(name).parts[0] for name in names}
        if roots != {path.name.removesuffix(".zip")}:
            raise ValueError("ZIP root does not match the product name")
        root_name, = roots
        product_xml = root_name + "/MTD_MSIL2A.xml"
        tile_xml, = [name for name in names if name.endswith("/MTD_TL.xml")]
        manifest_name = root_name + "/manifest.safe"
        product = ET.fromstring(archive.read(product_xml))
        tile = ET.fromstring(archive.read(tile_xml))
        manifest = ET.fromstring(archive.read(manifest_name))
        coordinates = [float(n) for n in one(product, "EXT_POS_LIST").text.split()]
        # SAFE EXT_POS_LIST has latitude first; GeoJSON and map inputs use longitude first.
        footprint = Polygon([(coordinates[i + 1], coordinates[i]) for i in range(0, len(coordinates), 2)])
        if not footprint.is_valid or footprint.is_empty:
            raise ValueError("Invalid official footprint")
        epsg = int(one(tile, "HORIZONTAL_CS_CODE").text.split(":")[-1])
        geocoding = one(tile, "Tile_Geocoding")
        grids = {}
        for size in nodes(geocoding, "Size"):
            resolution = size.attrib["resolution"]
            position, = [p for p in nodes(geocoding, "Geoposition") if p.attrib["resolution"] == resolution]
            width, height = int(one(size, "NCOLS").text), int(one(size, "NROWS").text)
            ulx, uly = float(one(position, "ULX").text), float(one(position, "ULY").text)
            dx, dy = float(one(position, "XDIM").text), float(one(position, "YDIM").text)
            grids[resolution] = {"epsg": epsg, "width": width, "height": height,
                                 "transform": [dx, 0, ulx, 0, dy, uly],
                                 "bounds": [ulx, uly + height * dy, ulx + width * dx, uly]}
        x0, y0, x1, y1 = grids["10"]["bounds"]
        native_outline = Polygon([(x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0)])
        projected_outline = transform(native_outline.segmentize(1000),
                                      Transformer.from_crs(epsg, 4326, always_xy=True).transform, interleaved=False)
        cloud = float(one(product, "Cloud_Coverage_Assessment").text)
        band_ids = {node.attrib["bandId"]: ("B" + node.attrib["physicalBand"][1:].zfill(2)
                                           if node.attrib["physicalBand"] != "B8A" else "B8A")
                    for node in nodes(product, "Spectral_Information")}
        offsets = {band_ids[node.attrib["band_id"]]: float(node.text) for node in nodes(product, "BOA_ADD_OFFSET")}
        assets = {PurePosixPath(name).name: name for name in names if name.lower().endswith((".jp2", ".xml", ".safe"))}
        checks = []
        for obj in nodes(manifest, "dataObject"):
            stream = one(obj, "byteStream")
            location = one(stream, "fileLocation").attrib["href"].removeprefix("./")
            if ".." in PurePosixPath(location).parts or location.startswith("/"):
                raise ValueError("Unexpected manifest path")
            name = root_name + "/" + location
            info = archive.getinfo(name)
            checksum = one(stream, "checksum")
            record = {"member": name, "bytes": info.file_size, "crc32": f"{info.CRC:08x}",
                      "checksum_algorithm": checksum.attrib["checksumName"], "official_checksum": checksum.text.lower()}
            if info.file_size != int(stream.attrib["size"]):
                raise ValueError(f"Official length mismatch: {name}")
            checks.append(record)
        record = {"zip": str(path.resolve()), "zip_bytes": path.stat().st_size, "product_id": root_name,
                  "tile": re.search(r"_(T\d{2}[A-Z]{3})_", root_name)[1],
                  "platform": root_name[:3], "acquisition_utc": one(tile, "SENSING_TIME").text,
                  "cloud": cloud, "footprint": mapping(footprint), "native_grid_outline": mapping(projected_outline),
                  "grids": grids, "quantification": float(one(product, "BOA_QUANTIFICATION_VALUE").text),
                  "boa_offsets": offsets, "members": len(names), "manifest_members": len(checks),
                  "metadata": {"product": product_xml, "granule": tile_xml, "manifest": manifest_name},
                  "angle_nodes": {key: len(nodes(tile, key)) for key in ("Sun_Angles_Grid", "Mean_Sun_Angle",
                                                                        "Viewing_Incidence_Angles_Grids", "Mean_Viewing_Incidence_Angle")},
                  "assets": assets, "member_integrity": checks, "deep_integrity": False}
        if deep:
            expected = {row["member"]: row for row in checks}
            last = time.monotonic()
            for index, info in enumerate(archive.infolist()):
                if info.is_dir():
                    continue
                row = expected.get(info.filename)
                algorithm = (row["checksum_algorithm"] if row else "SHA-256").lower().replace("-", "")
                algorithm = "sha3_256" if algorithm == "sha3256" else algorithm
                digest = hashlib.new(algorithm)
                with archive.open(info) as stream:
                    while chunk := stream.read(4 * 1024**2):
                        digest.update(chunk)
                # Reading to EOF also verifies the ZIP CRC, including non-manifest members.
                if row:
                    if digest.hexdigest() != row["official_checksum"]:
                        raise ValueError(f"Official checksum mismatch: {info.filename}")
                    row.update(official_checksum_verified=True, zip_crc_verified=True)
                if time.monotonic() - last > 15:
                    print(f"{root_name}: verified {index + 1}/{len(names)} members", flush=True)
                    last = time.monotonic()
            record.update(deep_integrity=True, all_zip_members_crc_verified=True,
                          all_manifest_lengths_and_checksums_verified=True)
        return record


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--fixture", type=Path)
    parser.add_argument("--deep", action="store_true")
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    report = {"status": "running", "reference_directory": str(args.directory), "products": []}
    paths = sorted(args.directory.glob("*.SAFE.zip"))
    if not paths:
        raise ValueError("No official Sentinel-2 SAFE ZIPs found")
    try:
        for path in paths:
            record = read_reference(path, deep=args.deep)
            report["products"].append(record)
            args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"Verified {'all members' if args.deep else 'metadata'}: {path.name}", flush=True)
        report.update(status="passed", product_count=len(paths),
                      distinct_tiles=sorted({row["tile"] for row in report["products"]}),
                      acquisition_dates=sorted({row["acquisition_utc"][:10] for row in report["products"]}))
        if args.fixture:
            keys = ("product_id", "tile", "platform", "acquisition_utc", "cloud", "footprint", "grids", "boa_offsets", "quantification", "angle_nodes")
            fixture = {"reference": "Ten official SAFE ZIPs supplied by the user in D:/Datasets/2604; metadata read without modification",
                       "products": [{key: row[key] for key in keys} for row in report["products"]]}
            args.fixture.parent.mkdir(parents=True, exist_ok=True)
            args.fixture.write_text(json.dumps(fixture, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as error:
        report.update(status="failed", error=str(error))
        raise
    finally:
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({key: report[key] for key in ("status", "product_count", "distinct_tiles", "acquisition_dates")}), flush=True)


if __name__ == "__main__":
    main()
