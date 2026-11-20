"""Exercise both native Sentinel-2 paths in the running app and audit independent source bytes."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import hashlib
import io
import json
import math
import os
import re
import time
import xml.etree.ElementTree as ET
from pathlib import Path

import numpy as np
from pyproj import Transformer
import rasterio
from rasterio.windows import Window
import requests
import tifffile
from urllib3.exceptions import ProtocolError, ReadTimeoutError

from live_public import local_app

EARTH = "https://earth-search.aws.element84.com/v1/collections/sentinel-2-c1-l2a/items/"
CDSE = "https://stac.dataspace.copernicus.eu/v1/collections/sentinel-2-global-mosaics/items/"
MIRROR = "https://data.source.coop/tge-labs/sentinel-2-quarterly-cloudless-mosaics"
L2A = "s2-l2a-cog"
L3 = "s2-l3-quarterly"
L2A_ALIASES = dict(zip(
    "B01 B02 B03 B04 B05 B06 B07 B08 B8A B09 B11 B12 SCL AOT WVP CLD SNW TCI metadata.xml tileInfo.json product_metadata.xml".split(),
    "coastal blue green red rededge1 rededge2 rededge3 nir nir08 nir09 swir16 swir22 scl aot wvp cloud snow visual granule_metadata tileinfo_metadata product_metadata".split(),
    strict=True))
L3_BANDS = ("B02", "B03", "B04", "B08")
MISSING_L3_KEY = "Sentinel-2_mosaic_2026_Q2_50TMK_0_0"
TAG = re.compile(r'"[a-fA-F0-9]{32}(?:-[1-9]\d{0,5})?"')


@dataclass(frozen=True)
class Case:
    name: str
    profile: str
    key: str
    day: str
    box: str
    members: tuple[str, ...]


SVALBARD = Case("svalbard-l2a", L2A, "S2B_T33XWG_20240605T130710_L2A", "2024-06-05", "15.60,78.22,15.67,78.25", tuple(L2A_ALIASES))
BEIJING = Case("beijing-l2a", L2A, "S2A_T50TMK_20240610T031035_L2A", "2024-06-10", "116.350,39.900,116.355,39.905",
               ("B01", "metadata.xml", "tileInfo.json", "product_metadata.xml"))
EASTER = Case("rapa-nui-l3", L3, "Sentinel-2_mosaic_2024_Q2_12JXR_0_0", "2024-06-10", "-109.36,-27.13,-109.34,-27.11", L3_BANDS)
BEIJING_L3 = Case("beijing-l3", L3, "Sentinel-2_mosaic_2024_Q2_50TMK_0_0", "2024-06-10", BEIJING.box, L3_BANDS)
CASES = (SVALBARD, BEIJING, EASTER, BEIJING_L3)


class Local:
    def __init__(self, origin, session):
        self.origin, self.session = origin, session

    def post(self, route, value, status=200):
        r = self.session.post(f"{self.origin}/api/{route}", json=value, timeout=12)
        if r.status_code != status:
            raise RuntimeError(f"Local {route}: HTTP {r.status_code}: {r.json().get('error')}")
        return r.json()

    def job(self, route, value, expected="completed", timeout=900):
        key = self.post(route, value)["job_id"]
        end = time.monotonic() + timeout
        message = None
        while time.monotonic() < end:
            snap = self.session.get(f"{self.origin}/api/job", params={"id": key}, timeout=10).json()
            if snap["message"] != message:
                message = snap["message"]
                if route == "download":
                    print(message, flush=True)
            if snap["status"] not in ("running", "cancelling"):
                if snap["status"] != expected:
                    raise RuntimeError(f"Local {route} finished {snap['status']}: {snap.get('error') or message}")
                return snap
            time.sleep(.45)
        self.post("cancel", {"job_id": key})
        raise RuntimeError(f"Local {route} did not finish before validation timeout")


from browser_flow import open_data_page

def digest(path):
    with Path(path).open("rb") as fd:
        return hashlib.file_digest(fd, "sha256").hexdigest()


def response(session, url, *, status=200, method="GET", **kwargs):
    for attempt in range(4):
        try:
            r = session.request(method, url, allow_redirects=False, timeout=(10, 42), **kwargs)
        except (requests.ConnectionError, requests.Timeout) as error:
            if attempt == 3 or (isinstance(error,requests.exceptions.SSLError) and "UNEXPECTED_EOF_WHILE_READING" not in str(error)):
                raise  # Never bypass a certificate or protocol validation error.
            time.sleep(2 ** (attempt+1))
            continue
        if r.status_code == status:
            return r
        with r:
            if r.status_code not in (429, 500, 502, 503, 504) or attempt == 3:
                raise RuntimeError(f"Independent provider {method} {url}: got {r.status_code}, expected {status}")
            try:
                wait = float(r.headers.get("Retry-After", ""))
            except ValueError:
                wait = 2 ** (attempt+1)
            time.sleep(max(1, min(wait, 60)))
    raise AssertionError("unreachable")


def read_json(session, url):
    with response(session, url) as r:
        return r.json(), hashlib.sha256(r.content).hexdigest()


def range_bytes(session, url, tag, total, start, size):
    assert 0 < size <= 4 * 1024**2 and 0 <= start < start+size <= total
    last = start+size-1
    headers = {"Range": f"bytes={start}-{last}", "If-Match": tag, "Accept-Encoding": "identity"}
    for attempt in range(3):
        with response(session, url, status=206, stream=True, headers=headers) as r:
            assert r.headers["Content-Range"] == f"bytes {start}-{last}/{total}" and r.headers["ETag"] == tag
            assert r.headers["Content-Length"] == str(size) and r.headers.get("Content-Encoding", "identity").lower() == "identity"
            try:
                data = r.raw.read(size+1)
            except (ProtocolError, ReadTimeoutError):
                if attempt == 2:
                    raise
                data = b""
        if len(data) == size:
            return data
        time.sleep(2 ** (attempt+1))
    raise RuntimeError("Independent reference source byte range was truncated")


def verify_tiff(session, case, member, path, url, size, tag, asset, props):
    """Decode TIFF blocks separately from GDAL. Use remote ranges where no full source SHA exists."""
    epsg = props.get("proj:epsg") or int(props["proj:code"][5:])
    # For L2A the complete local SHA was already independently checked against Earth Search.
    remote = case.profile == L3 or member in ("B04", "TCI")
    header_length = min(size, 512*1024)
    raster = asset.get("raster:bands")
    n, nodata, dtype, gain, offset = ((len(raster), raster[0]["nodata"], raster[0]["data_type"], raster[0].get("scale",1), raster[0].get("offset",0))
                                    if case.profile == L2A else (1,-32768,"int16",1,0))
    with Path(path).open("rb") as local:
        hdr = local.read(header_length)
        assert len(hdr) == header_length
        if remote:
            assert range_bytes(session,url,tag,size,0,header_length) == hdr
        with tifffile.TiffFile(io.BytesIO(hdr)) as tif, rasterio.open(path) as ds:
            p = tif.pages[0]
            assert p.is_tiled and p.samplesperpixel == n == ds.count and p.dtype == np.dtype(dtype) and ds.dtypes == (dtype,) * n
            assert ds.nodatavals == (nodata,) * n and int(p.nodata) == nodata and ds.tags(ns="IMAGE_STRUCTURE")["LAYOUT"] == "COG"
            assert ds.scales == (gain,) * n and ds.offsets == (offset,) * n
            assert list(ds.shape) == [p.imagelength, p.imagewidth] == asset["proj:shape"]
            geo = p.geotiff_tags
            sx, sy, _ = geo["ModelPixelScale"]
            i, j, _, ulx, uly, _ = geo["ModelTiepoint"]
            assert (i,j) == (0,0) and geo["ProjectedCSTypeGeoKey"] == epsg == ds.crs.to_epsg()
            assert list(ds.transform)[:6] == [sx,0,ulx,0,-sy,uly] == asset["proj:transform"][:6]
            w,s,e,north = [float(v) for v in case.box.split(",")]
            x,y = Transformer.from_crs(4326,epsg,always_xy=True).transform((w+e)*.5,(s+north)*.5)
            h, w = ds.shape
            row, col = max(0,min(h-7, math.floor((uly-y)/sy)-3)), max(0,min(w-9,math.floor((x-ulx)/sx)-4))
            windows = [(row,col), (h-73,w-75), (min(h-7,75), min(w-9,97))]
            cols_per_image, rows_per_image = math.ceil(w/p.tilewidth), math.ceil(h/p.tilelength)
            cache, compared, non_fill, at_aoi = {}, 0, 0, []
            for r,c in windows:
                ref = np.full((7,9,ds.count),nodata,dtype=dtype)
                for yy in range(r,r+7):
                    for xx in range(c,c+9):
                        index = (yy//p.tilelength)*cols_per_image + xx//p.tilewidth
                        for band in range(ds.count) if p.planarconfig == 2 else (None,):
                            segment = index+(cols_per_image*rows_per_image*band if band is not None else 0)
                            if segment not in cache:
                                pos, length = p.dataoffsets[segment],p.databytecounts[segment]
                                tile = None
                                if length:
                                    local.seek(pos)
                                    raw = local.read(length)
                                    assert len(raw) == length
                                    if remote:
                                        assert range_bytes(session,url,tag,size,pos,length) == raw
                                    tile,_,_ = p.decode(raw,segment)
                                    assert tile is not None
                                else:
                                    assert pos == 0
                                cache[segment] = (tile,length)
                            tile,_ = cache[segment]
                            if tile is not None:
                                values = tile[0,yy % p.tilelength,xx % p.tilewidth,:]
                                if band is None:
                                    ref[yy-r,xx-c,:] = values
                                else:
                                    ref[yy-r,xx-c,band], = values
                got = ds.read(window=Window(c,r,9,7)).transpose(1,2,0)
                np.testing.assert_array_equal(got,ref)
                compared += got.size
                non_fill += int(np.count_nonzero(got!=nodata))
                if not at_aoi:
                    at_aoi.extend(int(v) for v in ref[3,4])
            report = {"epsg":epsg,"shape":list(ds.shape),"source_resolution_m":sx,"dtype":dtype,"channels":ds.count,
                      "nodata":nodata,"tiff_scale":gain,"tiff_offset":offset,"exact_sample_values":compared,
                      "sample_values_not_nodata":non_fill,"aoi_center_dn":at_aoi,
                      "matched_range_bytes":(len(hdr)+sum(length for _,length in cache.values())) if remote else 0}
            if case.profile == L3:
                report["mirror_reflectance_gain"] = .0001
                report["aoi_center_reflectance"] = [dn*.0001 if dn!=nodata else None for dn in at_aoi]
            return report


def mirror_index_url(key):
    _,_,year,quarter,tile,i,j = key.split("_")
    return f"{MIRROR}/{year}/{quarter}/{tile}_{i}_{j}/{key}.json"


def check_item(case, target, file, row, official, mirror, index_hash):
    member = target["asset"]
    uid = case.key
    path = Path(file["path"])
    assert row["asset"] == member and row["product"] == uid and row["status"] == file["status"] == "completed"
    object_ = (mirror or official)["assets"][member if mirror else L2A_ALIASES[member]]
    src, size = object_["href"], object_["file:size"]
    assert src == target["url"] == row["url"] and "?" not in src and target["bytes"] == size == row["size"] == path.stat().st_size
    tag = row["etag"]
    assert TAG.fullmatch(tag)
    sha = digest(path)
    receipt = json.loads(path.with_suffix(".starmap.json").read_bytes())
    assert row["sha256"] == receipt["sha256"] == sha and receipt["etag"] == tag
    record = {"member":member,"name":target["name"],"bytes":size,"sha256":sha,"etag":tag,"path":str(path)}
    props = official["properties"]
    with requests.Session() as session:
        if mirror:
            with response(session,src,method="HEAD",headers={"If-Match":tag,"Accept-Encoding":"identity"}) as r:
                assert r.headers["Content-Length"] == str(size) and r.headers["ETag"] == tag
            assert official["assets"][member]["file:size"] == size
            assert official["assets"][member]["proj:transform"][:6] == object_["proj:transform"][:6]
            assert row["l3_index_sha256"] == index_hash and row["provider_checksum_verified"] is None
            assert row["aws_single_part_md5"] is None and receipt["reflectance_scale"] == .0001
            record["cdse_size_and_grid_matched"] = True
        else:
            assert object_["file:checksum"] == "1220"+sha and "SHA-256" in row["provider_checksum_verified"]
            assert row["l2a_index_sha256"] == official["assets"]["tileinfo_metadata"]["file:checksum"][4:]
            assert row["safe_product"]+".SAFE" == props["s2:product_uri"]
            record["earth_search_sha256_matched"] = True
        if path.suffix == ".tif":
            record.update(verify_tiff(session,case,member,path,src,size,tag,object_,props))
        elif member == "tileInfo.json":
            data = json.loads(path.read_bytes())
            assert data["productName"]+".SAFE" == props["s2:product_uri"]
            assert data["datastrip"]["id"] == props["s2:datastrip_id"]
            assert f"{data['utmZone']:02d}{data['latitudeBand']}{data['gridSquare']}" == props["grid:code"][5:]
            assert data["path"] in next(link["href"] for link in official["links"] if link["rel"] == "via")
        else:
            root = ET.parse(path).getroot()
            if member == "metadata.xml":
                assert root.findtext(".//TILE_ID") == props["s2:tile_id"]
                assert root.findtext(".//DATASTRIP_ID") == props["s2:datastrip_id"]
                assert root.findtext(".//HORIZONTAL_CS_CODE") == (props.get("proj:code") or "EPSG:"+str(props["proj:epsg"]))
            else:
                assert root.findtext(".//PRODUCT_URI") == props["s2:product_uri"]
                assert root.find(".//Datatake").get("datatakeIdentifier") == props["s2:datatake_id"]
    return record


def criteria(case, day=None):
    ret = {"start": day or case.day, "end": day or case.day}
    if case.profile == L2A:
        ret["max_cloud"] = 100
    return ret


def audited_delivery(out, plan, case, version):
    folder = (out / plan["folder_name"]).resolve()
    expected = [(t["asset"], t["url"]) for t in plan["targets"]]
    for candidate in sorted(folder.glob("下载记录_*.json"), key=lambda p:p.stat().st_mtime, reverse=True):
        data = json.loads(candidate.read_bytes())
        if (data["status"] != "completed" or data["software"] != version or data["source"] != plan["source"]
                or [(i["asset"], i["url"]) for i in data["items"]] != expected):
            continue
        if all(i["status"]=="completed" and i["product"]==case.key and Path(i["path"]).resolve().parent==folder for i in data["items"]):
            return {"manifest":str(candidate),"files":[{"path":i["path"],"status":i["status"]} for i in data["items"]]}
    raise RuntimeError("No matching completed transfer log was found in the audited output folder")


def verify_case(api, out, case, timeout, audit):
    print(f"Installed/source check: {case.name}, {len(case.members)} files",flush=True)
    with requests.Session() as session:
        official,_ = read_json(session,(EARTH if case.profile==L2A else CDSE)+case.key)
        assert official["id"] == case.key
        mirror,index_hash = read_json(session,mirror_index_url(case.key)) if case.profile == L3 else (None,None)
        if mirror:
            assert mirror["id"] == case.key
            dates = ("start_datetime","end_datetime","grid:code","proj:code")
            assert [official["properties"][v] for v in dates] == [mirror["properties"][v] for v in dates]
            assert official["properties"]["start_datetime"][:10] < case.day < official["properties"]["end_datetime"][:10]
    geometry = api.post("aoi",{"coordinates":case.box})["geometry"]
    base = {"profile":case.profile,"geometry":geometry}
    c = criteria(case)
    search = api.job("search",{**base,**c},timeout=150)["result"]
    scene, = [row for row in search["scenes"] if row["key"]==case.key]
    assert scene["raw"]["available"] and set(case.members).issubset(a["id"] for a in scene["raw"]["assets"])
    if case.profile == L3:
        assert scene["date"] == "2024 Q2" and scene["period"]["from"] == official["properties"]["start_datetime"]
        assert scene["raw"]["uuid"] is None and len(scene["raw"]["assets"]) == 4
    req = {**base,"criteria":c,"query_id":search["query"]["id"],"scene_keys":[case.key],"output_dir":str(out),
           "options":{"mode":"assets","transport":"public","assets":sorted(case.members)}}
    plan = api.post("plan",req)
    assert plan["requires"] == [] and plan["requests"] == len(case.members)
    if audit:
        state = api.session.get(f"{api.origin}/api/bootstrap",timeout=12).json()
        finished = audited_delivery(out,plan,case,state["version"])
    else:
        finished = api.job("download",req,timeout=timeout)["result"]
    manifest = json.loads(Path(finished["manifest"]).read_bytes())
    refs = zip(plan["targets"],finished["files"],manifest["items"],strict=True)
    with ThreadPoolExecutor(max_workers=2) as pool:
        pending = [pool.submit(check_item,case,target,file,row,official,mirror,index_hash) for target,file,row in refs]
        checked = [v.result() for v in pending]
    assert plan["estimated_bytes"] == sum(r["bytes"] for r in checked)
    assert any(r.get("sample_values_not_nodata",0) > 0 for r in checked)
    if case == SVALBARD:
        rgb, = [r for r in checked if r["member"]=="TCI"]
        assert rgb["channels"] == 3 and len(rgb["aoi_center_dn"]) == 3 and any(rgb["aoi_center_dn"])
        # This 100%-cloud fixture includes saturated white; unequal RGB values are not a product invariant.
        assert any(r["sample_values_not_nodata"] < r["exact_sample_values"] for r in checked if "exact_sample_values" in r)
    # Local corruption with unchanged length. L2A also proves a forged local receipt cannot replace the STAC SHA-256.
    victim = Path(next(file["path"] for file in finished["files"] if Path(file["path"]).suffix == (".json" if case.profile==L2A else ".tif")))
    receipt = victim.with_suffix(".starmap.json")
    saved_receipt = receipt.read_bytes()
    with victim.open("r+b") as fd:
        before = fd.read(1)
        after = bytes([before[0]^0x10])
        fd.seek(0)
        fd.write(after)
    if case.profile==L2A:
        receipt.write_text(json.dumps({**json.loads(saved_receipt),"sha256":digest(victim)}),encoding="utf-8")
    try:
        failed = api.job("download",req,expected="failed",timeout=90)
        assert "完整性校验失败" in failed["error"]
        with victim.open("rb") as fd:
            assert fd.read(1) == after
        assert not list(out.rglob("*.part"))
    finally:
        with victim.open("r+b") as fd:
            fd.write(before)
        receipt.write_bytes(saved_receipt)
    reuse = api.job("download",req,timeout=180)["result"]
    assert len(reuse["files"]) == len(checked) and all(f["status"]=="skipped" for f in reuse["files"])
    return {"case":case.name,"item":case.key,"query_utc_day":case.day,"aoi":case.box,"manifest":finished["manifest"],
            "period":scene.get("period"),"bytes":plan["estimated_bytes"],"members":checked,"manifest_version":manifest["software"],
            "delivery": "previous completed transfer independently audited" if audit else "new transfer",
            "verified_reuse":len(reuse["files"]),"same_length_tamper_rejected_without_overwrite":True}


def check_missing(api, out):
    c = criteria(BEIJING_L3, "2026-06-10")
    b = {"profile":L3,"geometry":api.post("aoi",{"coordinates": BEIJING.box})["geometry"]}
    d = api.job("search",{**b,**c},timeout=150)["result"]
    row, = [row for row in d["scenes"] if row["key"]==MISSING_L3_KEY]
    assert not row["raw"]["available"] and "缺少该季度瓦片的索引" in row["raw"]["unavailable_reason"]
    req = {**b,"criteria":c,"query_id":d["query"]["id"],"scene_keys":[row["key"]],"output_dir":str(out),
           "options":{"mode":"assets","transport":"public","assets":["B04"]}}
    api.post("plan",req,status=400)
    api.post("download",req,status=400)
    return {"item":row["key"],"reason":row["raw"]["unavailable_reason"],"plan_and_download_refused":True}


def verify_browser(origin, out, cases):
    from playwright.sync_api import expect, sync_playwright
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel=os.environ.get("STARMAP_TEST_BROWSER","msedge"),headless=True)
        page = browser.new_page(viewport={"width":1360,"height":900})
        errors=[]
        page.on("pageerror",lambda err: errors.append(str(err)))
        page.route("https://**/*",lambda route: route.abort())  # Browser map tiles only; imagery requests use the live server.
        open_data_page(page, origin)
        expect(page.locator("#outputDirectory")).not_to_have_value("")
        names=[]
        for case in cases:
            page.locator("#sourceProfile").select_option(case.profile)
            expect(page.locator("#publicAuth")).to_be_visible()
            expect(page.locator("#rawOptions")).to_be_visible()
            if case.profile == L3:
                expect(page.locator("#cloudOptions")).to_be_hidden()
                expect(page.locator("#rawMetadataOnly")).to_be_disabled()
            else:
                page.locator("#maxCloud").fill("100")
            page.locator('[data-tab="coordinates"]').click()
            page.locator("#coordinates").fill(case.box)
            page.locator("#applyCoordinates").click()
            expect(page.locator("#aoiName")).to_have_text("坐标研究区")
            page.locator("#startDate").fill(case.day)
            page.locator("#endDate").fill(case.day)
            page.locator("#searchScenes").click()
            row = page.locator(f'#scenesBody tr[data-key="{case.key}"]')
            expect(row).to_be_visible(timeout=120000)
            expect(row).to_contain_text("2024 Q2" if case.profile==L3 else "T33XWG" if case==SVALBARD else "T50TMK")
            page.locator("#selectNone").click()
            row.locator("input").check()
            page.locator("#rawMode").select_option("assets")
            for band in case.members:
                page.locator(f'#rawMultiAssets input[value="{band}"]').check()
            page.locator("#outputDirectory").fill(str(out))
            page.locator("#downloadSelected").click(timeout=20000)
            expect(page.locator("#planNote")).to_contain_text("DN × 0.0001" if case.profile==L3 else "SHA‑256")
            page.locator("#confirmDownload").click()
            expect(page.locator("#jobStatus")).to_have_text("下载完成",timeout=100000)
            files = page.locator("#jobFiles .job-file")
            expect(files).to_have_count(len(case.members))
            assert all("已有" in s for s in files.all_inner_texts())
            page.locator("#closeJobPanel").click()
            page.locator("#sourceProfile").select_option("s2-earth-search")
            expect(page.locator("#scenesBody tr")).to_have_count(0)
            expect(page.locator("#downloadSelected")).to_be_disabled()
            names.append(case.name)
        missing_disabled = any(case.profile==L3 for case in cases)
        if missing_disabled:
            page.locator("#sourceProfile").select_option(L3)
            page.locator('[data-tab="coordinates"]').click()
            page.locator("#coordinates").fill(BEIJING.box)
            page.locator("#applyCoordinates").click()
            expect(page.locator("#aoiName")).to_have_text("坐标研究区")
            page.locator("#startDate").fill("2026-06-10")
            page.locator("#endDate").fill("2026-06-10")
            page.locator("#searchScenes").click()
            row = page.locator(f'#scenesBody tr[data-key="{MISSING_L3_KEY}"]')
            expect(row).to_be_visible(timeout=120000)
            expect(row).to_contain_text("公开镜像缺少该季度瓦片的索引")
            expect(row.locator("input")).to_be_disabled()
            expect(page.locator("#downloadSelected")).to_be_disabled()
        assert not errors,errors
        browser.close()
    return {"browser_verified_reuse":names,"cleared_selection_on_switch":True,"missing_mirror_disabled":missing_disabled}


def run(out, exe, cases, with_browser, timeout, audit=False):
    out.mkdir(parents=True,exist_ok=True)
    report_file = out / ("s2-native-audit-report.json" if audit else "s2-native-live-report.json")
    report = {"status":"running","binary_sha256":digest(exe) if exe else None,"cases":[]}
    def save():
        report_file.write_text(json.dumps(report,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    try:
        save()
        with local_app(exe,out,out/"settings") as (origin,session):
            api = Local(origin,session)
            for case in cases:
                report["cases"].append(verify_case(api,out,case,timeout,audit))
                save()
            if any(case.profile==L3 for case in cases):
                report["missing_archive"] = check_missing(api,out)
            if with_browser:
                representatives = {}
                for case in cases:
                    representatives.setdefault(case.profile, case)
                report["ui"] = verify_browser(origin,out,representatives.values())
        report["status"] = "completed"
        print(f"Verification completed: {report_file}",flush=True)
    except Exception as error:
        report.update(status="failed",error=f"{type(error).__name__}: {error}")
        raise
    finally:
        save()


if __name__=="__main__":
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--exe",type=Path)
    p.add_argument("--output",type=Path,required=True)
    p.add_argument("--only",choices=[c.name for c in CASES],action="append")
    p.add_argument("--no-browser",action="store_true")
    p.add_argument("--audit-existing",action="store_true",help="Audit earlier completed transfers; do not redownload")
    p.add_argument("--download-timeout",type=int,default=2100,help="seconds per multi-file batch")
    args=p.parse_args()
    cases=[c for c in CASES if c.name in args.only] if args.only else list(CASES[:3])
    run(args.output.resolve(),args.exe.resolve(strict=True) if args.exe else None,cases,not args.no_browser,args.download_timeout,args.audit_existing)
