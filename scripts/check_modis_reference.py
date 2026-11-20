"""Independent MODIS object and HDF4-vs-COG reference verification (not packaged)."""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
from pathlib import Path
from xml.etree import ElementTree as ET

import numpy as np
import rasterio
import requests
from pyhdf.SD import SD, SDC  # Optional verifier dependency, independent of GDAL.

HOST = "modiseuwest.blob.core.windows.net"
TOKEN = "https://planetarycomputer.microsoft.com/api/sas/v1/token/modiseuwest"
GRANULE = re.compile(r"(MOD|MYD|MCD)[A-Z0-9]+\.A[0-9]{7}\.h[0-9]{2}v[0-9]{2}\.061\.[0-9]{13}")
ALIAS = re.compile(r"[A-Za-z0-9_]{1,64}")


def verify_ref(root, key, alias, doc, tokens, fetch):
    assert GRANULE.fullmatch(key) and ALIAS.fullmatch(alias)
    short, yday, tile, *_ = key.split(".")
    scope = "modis-061" if alias in ("hdf", "metadata") else "modis-061-cogs"
    name = key+".hdf" if alias == "hdf" else key+".hdf.xml" if alias == "metadata" else key+"_"+alias+".tif"
    safe_url = f"https://{HOST}/{scope}/{short}/{tile[1:3]}/{tile[4:]}/{yday[1:]}/{name}"
    assert doc["url"] == safe_url
    path = root/("sparse-source" if scope == "modis-061" else "cog-reference")/name
    path.parent.mkdir(parents=True, exist_ok=True)
    if fetch and not path.exists():
        tmp = path.with_name(name+".part")
        try:
            if scope not in tokens:
                with requests.get(f"{TOKEN}/{scope}", timeout=25, allow_redirects=False) as r:
                    assert r.status_code == 200
                    token = r.json()["token"]
                    assert token and "?" not in token and "#" not in token
                    tokens[scope] = token
            with requests.get(safe_url+"?"+tokens[scope], headers={"Accept-Encoding":"identity","If-Match":doc["tag"]},
                              stream=True, timeout=(15,75), allow_redirects=False) as r:
                assert r.status_code == 200 and r.headers["Content-MD5"] == doc["md5"] and r.headers["ETag"] == doc["tag"]
                assert r.headers["Content-Length"] == str(doc["bytes"]) and r.headers.get("Content-Encoding", "identity") == "identity"
                with tmp.open("wb") as fd:
                    for b in r.iter_content(1024**2):
                        fd.write(b)
            assert tmp.stat().st_size == doc["bytes"] and _md5(tmp) == base64.b64decode(doc["md5"]).hex()
            tmp.replace(path)
        except requests.RequestException:
            raise RuntimeError(f"Independent provider read failed for {name}") from None
        finally:
            tmp.unlink(missing_ok=True)
    assert path.stat().st_size == doc["bytes"]
    md5 = _md5(path)
    assert md5 == base64.b64decode(doc["md5"]).hex(), f"Azure digest differs for {name}"
    with path.open("rb") as fd:
        sha = hashlib.file_digest(fd, "sha256").hexdigest()
    return path, {"url": safe_url, "etag": doc["tag"], "bytes": doc["bytes"], "sha256": sha, "source_md5": md5}


def _md5(path):
    with path.open("rb") as fd:
        return hashlib.file_digest(fd, lambda: hashlib.md5(usedforsecurity=False)).hexdigest()


def nasa_xml(path, key, records):
    data = path.read_bytes()
    assert b"<!ENTITY" not in data and b"\x00" not in data and b"[" not in data.split(b"<GranuleMetaDataFile>")[0]
    r = ET.fromstring(data)  # Does not resolve the external DTD.
    g = r.find("GranuleURMetaData")
    f = g.find("./DataFiles/DataFileContainer")
    assert r.tag == "GranuleMetaDataFile" and g.findtext("./CollectionMetaData/ShortName") == key.split(".")[0]
    assert g.findtext("./CollectionMetaData/VersionID") in ("61", "061")
    assert g.findtext("./ECSDataGranule/LocalGranuleID") == f.findtext("DistributedFileName") == key+".hdf"
    md5 = f.findtext("Checksum").lower()
    size = int(f.findtext("FileSize"))
    assert f.findtext("ChecksumType") == "MD5" and re.fullmatch(r"[a-f0-9]{32}", md5)
    if hdf := records.get("hdf"):
        assert hdf["bytes"] == size and hdf["source_md5"] == md5
    return {"hdf_bytes": size, "hdf_md5": md5,
            "range_beginning": g.findtext("./RangeDateTime/RangeBeginningDate"), "range_ending": g.findtext("./RangeDateTime/RangeEndingDate")}


def compare(hdf, key, alias, path, out):
    with rasterio.open(path) as ds:
        src = ds.read(1, masked=False)
        tags = {**ds.tags(), **ds.tags(1)}
        assert (ds.tags(ns="IMAGE_STRUCTURE").get("LAYOUT"), ds.count, tags["LOCALGRANULEID"], tags["VERSIONID"]) == ("COG", 1, key+".hdf", "61")
        out.update(tiff_dtype=ds.dtypes[0], tiff_nodata=ds.nodata, tiff_scale=ds.scales[0], tiff_offset=ds.offsets[0],
                   embedded_dates=[tags["RANGEBEGINNINGDATE"],tags["RANGEENDINGDATE"]], long_name=ds.descriptions[0])
    if hdf is None:
        return
    sds = hdf.select(alias.replace("_", " ") if key.split(".")[0].endswith("13Q1") else alias)
    try:
        original = sds.get()
        a = sds.attributes()
    finally:
        sds.endaccess()
    fill = a.get("_FillValue")
    assert src.shape == original.shape
    mismatch = src != original
    changed = int(np.count_nonzero(mismatch))
    if alias.endswith("_pixel_reliability"):
        assert out["tiff_dtype"] == "uint8" and out["tiff_nodata"] == 255 and fill == -1 and original.dtype.name == "int8"
        assert np.array_equal(mismatch, (src == 255) & (original == -1)) and np.array_equal(src, original.astype("uint8"))
    else:
        assert not changed and src.dtype == original.dtype
        if isinstance(fill, int):
            assert out["tiff_nodata"] == fill
    if a.get("scale_factor") is not None:
        assert a["scale_factor"] == out["tiff_scale"] and a["add_offset"] == out["tiff_offset"]
    out.update(compared_cells=src.size, differing_raw_values=changed, unexpected_differences=0, hdf_dtype=original.dtype.name,
               hdf_fill=fill, hdf_scale=a.get("scale_factor"), hdf_offset=a.get("add_offset"),
               hdf_fill_cells=int(np.count_nonzero(original==fill)) if isinstance(fill,int) else None)
    if alias in ("QC_Day", "QC_Night"):
        temp = hdf.select(f"LST_{alias[3:]}_1km")
        try:
            out["valid_LST_pixels_with_QA_zero"] = int(np.count_nonzero((src==0) & (temp.get()!=0)))
        finally:
            temp.endaccess()


def audit(root, fetch):
    docs = json.loads((root/"sparse-objects.json").read_bytes())
    records, tokens = {}, {}
    initial = Path.cwd()
    if fetch:
        (root/"sparse-source").mkdir(exist_ok=True)
    try:
        # HDF4 on Windows does not support arbitrary Unicode Python paths. These
        # ASCII filenames are opened relative to the reference subdirectory.
        os.chdir(root/"sparse-source")
        for key, group in docs.items():
            checked = {alias: verify_ref(root, key, alias, entry, tokens, fetch) for alias, entry in group.items()}
            rows = records[key] = {alias: evidence for alias, (_,evidence) in checked.items()}
            hdf = SD(key+".hdf", SDC.READ) if "hdf" in checked else None
            try:
                if "metadata" in rows:
                    rows["metadata"]["nasa_xml"] = nasa_xml(checked["metadata"][0],key,rows)
                for alias,(path,out) in checked.items():
                    if alias not in ("hdf", "metadata"):
                        compare(hdf,key,alias,path,out)
            finally:
                if hdf is not None:
                    hdf.end()
            print(key, len(rows), "verified", flush=True)
    finally:
        os.chdir(initial)
        tokens.clear()
    layers = [v for row in records.values() for v in row.values() if "compared_cells" in v]
    stats = {"objects": sum(map(len,records.values())), "paired_layers": len(layers), "paired_cells": sum(v["compared_cells"] for v in layers),
             "signed_byte_fill_mapped": sum(v["differing_raw_values"] for v in layers), "unexpected_differences": sum(v["unexpected_differences"] for v in layers)}
    (root/"modis-independent-report.json").write_text(json.dumps({"summary":stats,"products":records},ensure_ascii=False,indent=2),encoding="utf-8")
    print(stats)


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("reference_dir", type=Path)
    p.add_argument("--fetch", action="store_true", help="Fetch missing pinned reference objects over verified TLS.")
    args = p.parse_args()
    audit(args.reference_dir.resolve(strict=True), args.fetch)
