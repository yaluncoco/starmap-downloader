"""Build the compact ESA WorldCover grid bundled with the app (no network at runtime).

Optional authoring tool: pip install pyogrio (not required in the installer).
Provide the original FGB and both release GeoJSONs named in core/data/WorldCover.txt.
The generated index contains only tile IDs and availability bits, not rasters.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

from shapely import from_wkb, box
from shapely.geometry import shape

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from core.s3_source import WC_AWS_SUFFIX, worldcover_tile, worldcover_url

COLUMNS = {"S2RGBNIR": "s2_rgbnir", "NDVI": "s2_ndvi", "SWIR": "s2_swir", "S1VVVHratio": "s1_vvvhratio"}


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def composites(path):
    from pyogrio.raw import read
    fields = [(year, asset, f"{base}_{year}") for year in (2020, 2021) for asset, base in COLUMNS.items()]
    info, _, polygons, values = read(path, columns=["tile", *(field for _, _, field in fields)])
    if info["crs"] != "EPSG:4326" or len(polygons) != 19363:
        raise ValueError("Unexpected composite release index CRS or tile count")
    named = dict(zip(info["fields"], values))
    tiles = {}
    for i, data in enumerate(polygons):
        tile = named["tile"][i]
        if (bounds := worldcover_tile(tile, 1)) is None or tile in tiles or not from_wkb(data).equals(box(*bounds)):
            raise ValueError(f"Inconsistent composite tile {tile}")
        mask = 0
        for bit, (year, asset, field) in enumerate(fields):
            if (uri := named[field][i]) is not None:
                canonical = worldcover_url(year, tile, asset).replace("https://", "s3://").replace(WC_AWS_SUFFIX, "")
                if uri != canonical:
                    raise ValueError(f"Non-canonical composite object for {tile}/{field}")
                mask |= 1 << bit
        if not mask:
            raise ValueError(f"No product associated with composite tile {tile}")
        tiles[tile] = mask
    return tiles


def map_index(paths):
    tiles = {}
    for bit, path in enumerate(paths):
        data = json.loads(path.read_bytes())
        if len(data["features"]) != 2651:
            raise ValueError("Unexpected map release index tile count")
        found = set()
        for row in data["features"]:
            tile = row["properties"]["ll_tile"]
            if (bounds := worldcover_tile(tile, 3)) is None or tile in found or not shape(row["geometry"]).equals(box(*bounds)):
                raise ValueError(f"Inconsistent map tile {tile}")
            found.add(tile)
            tiles[tile] = tiles.get(tile, 0) | (1 << bit)
    return tiles


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("composite", type=Path)
    parser.add_argument("map2020", type=Path)
    parser.add_argument("map2021", type=Path)
    parser.add_argument("--output", type=Path, default=ROOT / "core/data/worldcover-grid.json")
    args = parser.parse_args()
    document = {"version": 1, "source_sha256": {key: digest(getattr(args, key)) for key in ("composite", "map2020", "map2021")},
                "composite": sorted(composites(args.composite).items()), "map": sorted(map_index([args.map2020, args.map2021]).items())}
    args.output.parent.mkdir(exist_ok=True)
    args.output.write_text(json.dumps(document, separators=(",", ":"), ensure_ascii=True) + "\n", encoding="utf-8", newline="\n")
    print(f"{len(document['composite'])} composite tiles, {len(document['map'])} map tiles; SHA-256 {digest(args.output)}")


if __name__ == "__main__":
    main()
