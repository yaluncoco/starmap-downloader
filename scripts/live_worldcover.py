"""Live ESA annual COG audit for source or installed Windows EXE.

Fetch independent complete references through AWS's regional path endpoint, recheck
ETags from actual uploaded part boundaries, then compare files from the app (which
uses virtual-host URLs) by SHA-256 and by independent tiled TIFF pixel decoding.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import hashlib
import json
import math
import os
import re
import time
from pathlib import Path

import numpy as np
import rasterio
from rasterio.windows import Window
import requests
import tifffile

from live_public import local_app
from live_s2_tiles import Local, digest, response

S2, S1, MAP = "worldcover-s2", "worldcover-s1", "worldcover-map"
RAPA = "-109.42,-27.16,-109.39,-27.13"
SAO = "6.58,0.30,6.63,0.35"
LAYERS = {
    "S2RGBNIR": ("esa-worldcover-s2", "rgbnir", "uint16", 4, 0, .0001, 0, 12000),
    "NDVI": ("esa-worldcover-s2", "ndvi", "uint8", 3, 255, .008, -1, 12000),
    "SWIR": ("esa-worldcover-s2", "swir", "uint8", 2, 255, .004, 0, 6000),
    "S1VVVHratio": ("esa-worldcover-s1", "vvvhratio", "uint16", 3, 0, .001, -45, 12000),
    "Map": ("esa-worldcover", "map", "uint8", 1, 0, 1, 0, 12000),
}


@dataclass(frozen=True)
class Case:
    name: str
    profile: str
    area: str
    dates: tuple[str, str]
    tile: str
    origin: tuple[int, int]  # west, north
    years: tuple[int, ...]
    assets: tuple[str, ...]


CASES = (
    Case("rapa-optical-2021", S2, RAPA, ("2021-06-15", "2021-06-15"), "S28W110", (-110,-27), (2021,), ("NDVI","S2RGBNIR","SWIR")),
    Case("sao-tome-optical-2020", S2, SAO, ("2020-08-17", "2020-08-17"), "N00E006", (6,1), (2020,), ("NDVI","S2RGBNIR","SWIR")),
    Case("sao-tome-radar-2020-and-2021", S1, SAO, ("2020-12-31", "2021-01-01"), "N00E006", (6,1), (2021,2020), ("S1VVVHratio",)),
    Case("rapa-land-cover-2020-and-2021", MAP, RAPA, ("2020-12-31", "2021-01-01"), "S30W111", (-111,-27), (2021,2020), ("Map",)),
)


def product(year, tile):
    return f"ESA_WorldCover_10m_{year}_v{100 if year == 2020 else 200}_{tile}"


def addresses(year, case, layer):
    bucket, directory, *rest = LAYERS[layer]
    filename = product(year, case.tile) + "_" + layer + ".tif"
    key = f"v{100 if year == 2020 else 200}/{year}/map/{filename}" if layer == "Map" else f"{directory}/{year}/{case.tile[:3]}/{filename}"
    return filename, f"https://s3.eu-central-1.amazonaws.com/{bucket}/{key}", f"https://{bucket}.s3.eu-central-1.amazonaws.com/{key}"


def fetch_ref(ref_dir, case, year, layer):
    filename, ref_url, virtual = addresses(year, case, layer)
    path = ref_dir/filename
    with requests.Session() as session:
        session.headers.update({"Accept-Encoding": "identity", "User-Agent": "StarmapWorldCoverIndependentVerifier/1.10"})
        with response(session, ref_url, method="HEAD") as head:
            tag, size = head.headers["ETag"], int(head.headers["Content-Length"])
            assert re.fullmatch(r'"[0-9a-f]{32}(?:-[1-9][0-9]?)?"',tag) and 0 < size < 192*1024**2
            assert head.headers.get("x-amz-server-side-encryption") in (None,"AES256")
        if not path.is_file() or path.stat().st_size != size:
            temp = path.with_suffix(".ref-part")
            try:
                for attempt in range(4):
                    try:
                        with response(session, ref_url, stream=True, headers={"If-Match": tag}) as get:
                            assert get.headers["ETag"] == tag and int(get.headers["Content-Length"]) == size
                            assert get.headers.get("Content-Encoding", "identity") == "identity"
                            with temp.open("wb") as stream:
                                for data in get.iter_content(1024**2):
                                    stream.write(data)
                        assert temp.stat().st_size == size
                        temp.replace(path)
                        break
                    except requests.RequestException:
                        if attempt == 3:
                            raise
                        time.sleep(1+2*attempt)
            finally:
                temp.unlink(missing_ok=True)
        count = int(tag[:-1].split("-")[1]) if "-" in tag else 0
        boundaries = []
        position = 0
        if count:
            for part in range(1,count+1):
                with response(session, ref_url, method="HEAD", status=206, params={"partNumber":part}, headers={"If-Match":tag}) as r:
                    n = int(r.headers["Content-Length"])
                    assert n > 0 and r.headers["Content-Range"] == f"bytes {position}-{position+n-1}/{size}"
                    assert r.headers["ETag"] == tag and int(r.headers["x-amz-mp-parts-count"]) == count
                    boundaries.append(n); position += n
            assert position == size
        else:
            boundaries = [size]
        sha = hashlib.sha256()
        part_digests = []
        with path.open("rb") as src:
            for n in boundaries:
                h = hashlib.md5(usedforsecurity=False)
                remaining = n
                while remaining:
                    buf = src.read(min(remaining,1024**2))
                    assert buf
                    sha.update(buf); h.update(buf); remaining -= len(buf)
                part_digests.append(h.digest())
            assert not src.read(1)
        expected = hashlib.md5(b"".join(part_digests),usedforsecurity=False).hexdigest()+f"-{count}" if count else part_digests[0].hex()
        assert tag == f'"{expected}"', f"Reference {filename} failed its source ETag digest"
        row = {"file": filename, "reference_path": str(path), "reference_url": ref_url, "object_url": virtual,
               "size": size, "etag": tag, "sha256": sha.hexdigest(), "multipart_sizes": boundaries if count else []}
        print(f"REFERENCE VERIFIED {filename} · {size} bytes · {row['sha256']}",flush=True)
        return row


def fetch_all(ref_dir):
    ref_dir.mkdir(parents=True,exist_ok=True)
    with ThreadPoolExecutor(max_workers=5) as pool:
        jobs = [pool.submit(fetch_ref,ref_dir,c,y,a) for c in CASES for y in c.years for a in c.assets]
        refs = [job.result() for job in jobs]
    return {row["file"]: row for row in refs}


def check_raster(path, reference, case, year, layer):
    _, _, dtype, count, fill, scale, offset, px_per_degree = LAYERS[layer]
    period = "V1.0.0" if year==2020 else "V2.0.0"
    width = height = px_per_degree * (3 if layer == "Map" else 1)
    w,n = case.origin
    res = 1/px_per_degree
    with rasterio.open(path) as output, tifffile.TiffFile(reference["reference_path"]) as tf:
        p = tf.pages[0]
        assert p.is_tiled and int(p.nodata)==fill and p.samplesperpixel==count and p.dtype==np.dtype(dtype)
        geo = p.geotiff_tags
        assert geo["GeographicTypeGeoKey"]==4326 and geo["ModelTiepoint"]==[0.,0.,0.,float(w),float(n),0.] and geo["ModelPixelScale"]==[res,res,0.]
        assert (p.imagelength,p.imagewidth)==output.shape==(height,width)
        assert output.crs.to_epsg()==4326 and output.transform[:6]==(res,0.,w,0.,-res,n)
        assert output.count==count and output.dtypes==(dtype,)*count and output.nodatavals==(fill,)*count
        assert output.scales==(scale,)*count and output.offsets==(offset,)*count
        tags=output.tags()
        assert tags["product_tile"]==case.tile and tags["product_version"]==period and tags["time_start"]==f"{year}-01-01T00:00:00Z"
        assert tags["time_end"]==f"{year}-12-31T23:59:59Z" and output.tags(ns="IMAGE_STRUCTURE")["LAYOUT"]=="COG"
        if layer=="Map":
            assert p.colormap.shape==(3,256) and output.colorinterp[0].name=="palette"
            assert output.colormap(1)[10][:3]==(0,100,0)
        x0,y0,x1,y1=map(float,case.area.split(","))
        x,y=(x0+x1)*.5,(y0+y1)*.5
        r,c=math.floor((n-y)/res),math.floor((x-w)/res)
        # Also compare the edges deliberately, where these coastal/island scenes contain source NoData.
        windows=[(r-4,c-5),(0,0),(height-15,width-17),(height//2,width//2),(7,width-17)]
        across=math.ceil(width/p.tilewidth); down=math.ceil(height/p.tilelength)
        planar = p.planarconfig == 2
        fh, cache = tf.filehandle, {}
        def read(r_,c_,channel):
            idx=(r_//p.tilelength)*across+c_//p.tilewidth+(channel*across*down if planar else 0)
            if idx not in cache:
                fh.seek(p.dataoffsets[idx]); data = fh.read(p.databytecounts[idx])
                tile, _, _ = p.decode(data, idx)
                if tile is None:
                    raise RuntimeError("Missing reference TIFF tile")
                cache[idx] = tile[0]
            return cache[idx][r_%p.tilelength,c_%p.tilewidth,0 if planar else channel]
        matched, nodata=0,0
        for row,col in windows:
            arr=output.read(window=Window(col,row,13,11))
            ref=np.empty_like(arr)
            for j in range(11):
                for i in range(13):
                    for k in range(count):
                        ref[k,j,i]=read(row+j,col+i,k)
            np.testing.assert_array_equal(arr,ref)
            matched += ref.size; nodata += int(np.count_nonzero(ref==fill))
        assert 0 < nodata < matched, f"Audit fixture {path.name} must cover fill and valid pixels"
    return {"compared_band_samples": matched, "compared_nodata": nodata, "crs": "EPSG:4326", "shape": [height,width],
            "band_count": count, "dtype": dtype, "nodata":fill, "scale":scale, "offset":offset}


def chosen(api, case, dest):
    geom=api.post("aoi", {"coordinates":case.area})["geometry"]
    query={"profile":case.profile,"geometry":geom,"start":case.dates[0],"end":case.dates[1]}
    feed=api.job("search",query,timeout=120)["result"]
    ids=[product(y,case.tile) for y in case.years]
    assert [row["key"] for row in feed["scenes"]]==ids
    assert all(row["raw"]["available"] and row["coverage_percent"]==100 and set(a["id"] for a in row["raw"]["assets"])==set(case.assets)
               for row in feed["scenes"])
    return {"profile":case.profile,"geometry":geom,"criteria":{"start":case.dates[0],"end":case.dates[1]},
            "query_id":feed["query"]["id"],"scene_keys":ids,"output_dir":str(dest),"options":{
            "mode":"assets","assets":list(case.assets),"transport":"public"} if case.profile==S2 else {"mode":"original","transport":"public"}}


def verify_case(api, case, out, refs, timeout):
    selection=chosen(api,case,out/case.name)
    plan=api.post("plan",selection)
    assert plan["requires"]==[] and plan["requests"]==len(case.assets)*len(case.years)
    files={addresses(y,case,a)[0]: (y,a) for y in case.years for a in case.assets}
    assert plan["estimated_bytes"]==sum(refs[f]["size"] for f in files)
    for target in plan["targets"]:
        ref=refs[target["name"]]
        assert target["name"] in files and target["url"]==ref["object_url"] and target["identity"]["archive_etag"]==ref["etag"]
    result=api.job("download",selection,timeout=timeout)["result"]
    audit=json.loads(Path(result["manifest"]).read_text(encoding="utf-8"))
    assert audit["status"]=="completed" and len(audit["items"])==len(files)
    checked={}
    for row,target in zip(audit["items"],plan["targets"],strict=True):
        ref=refs[target["name"]]; y,a=files[target["name"]]
        assert row["status"]=="completed" and row["etag"]==ref["etag"] and row["url"]==ref["object_url"] and row["size"]==ref["size"]
        assert row["asset"]==a and row["product"]==product(y,case.tile) and row["transport"]=="public"
        path=Path(row["path"])
        assert path.stat().st_size==ref["size"] and digest(path)==row["sha256"]==ref["sha256"]
        assert row["provider_checksum_verified"].startswith("AWS multipart ETag" if ref["multipart_sizes"] else "AWS single-part ETag")
        checked[target["name"]] = {**ref, **check_raster(path, ref, case,y,a), "output":str(path)}
        print(f"APP MATCHED {target['name']} · entire SHA-256 & {checked[target['name']]['compared_band_samples']} tested band samples",flush=True)
    reused=api.job("download",selection,timeout=timeout)["result"]
    assert all(f["status"]=="skipped" for f in reused["files"])
    tamper=None
    if case==CASES[0]:
        # Change the last byte without changing length; a forged receipt SHA must not bypass S3 validation.
        row=audit["items"][0]; path=Path(row["path"]); receipt=path.with_suffix(".starmap.json")
        with path.open("r+b") as stream:
            stream.seek(-1,os.SEEK_END); value=stream.read(1); stream.seek(-1,os.SEEK_END); stream.write(bytes([value[0]^1]))
        meta=json.loads(receipt.read_text(encoding="utf-8")); meta["sha256"]=digest(path)
        receipt.write_text(json.dumps(meta),encoding="utf-8")
        failure=api.job("download",selection,expected="failed",timeout=timeout)
        assert "ETag" in failure["error"] and not list(path.parent.glob("*.part"))
        quarantine=out/"tamper-audit"; quarantine.mkdir(exist_ok=True)
        path.rename(quarantine/path.name); receipt.rename(quarantine/receipt.name)
        restored=api.job("download",selection,timeout=timeout)["result"]
        assert restored["files"][0]["status"]=="completed" and digest(path)==row["sha256"]
        assert all(f["status"]=="skipped" for f in restored["files"][1:])
        tamper={"same_length_corruption_rejected_even_with_new_local_sha256":True,"recovered_from_source":True,"error":failure["error"]}
    return {"case":case.name,"annual_calendar_filter":list(case.dates),"files":list(checked.values()),"verified_reuse":True,"tamper":tamper}


def verify_missing(api, out):
    geometry=api.post("aoi",{"coordinates":RAPA})["geometry"]
    def request(profile,start,end):
        base={"profile":profile,"geometry":geometry,"start":start,"end":end}
        return api.job("search",base,timeout=120)["result"]
    radar=request(S1,"2021-06-15","2021-06-15")
    row,=radar["scenes"]
    assert row["key"]==product(2021,"S28W110") and not row["raw"]["available"] and not row["raw"]["assets"]
    bad=api.post("plan", {"geometry":geometry,"profile":S1,"criteria":{"start":"2021-06-15","end":"2021-06-15"},
                          "query_id":radar["query"]["id"],"scene_keys":[row["key"]],"output_dir":str(out),"options":{"mode":"original"}},400)
    assert "不会换用" in bad["error"] and not request(S2,"2022-01-01","2022-01-02")["scenes"]
    u=addresses(2021,CASES[0],"S1VVVHratio")[1]
    with requests.Session() as session, response(session,u,method="HEAD",status=404):
        pass
    return {"official_missing_404":u,"no_fallback":True,"out_of_range_empty":True,"failure":bad["error"]}


def validate_browser(origin,out):
    from playwright.sync_api import expect,sync_playwright
    with sync_playwright() as p:
        browser=p.chromium.launch(channel=os.environ.get("STARMAP_TEST_BROWSER","msedge"),headless=True)
        page=browser.new_page(viewport={"width":1360,"height":900})
        errors=[]
        page.on("pageerror",lambda error: errors.append(str(error)))
        page.route("https://**/*",lambda route:route.abort())  # Local backend still contacts real AWS.
        page.goto(origin,wait_until="domcontentloaded")
        expect(page.locator("#outputDirectory")).not_to_have_value("")
        page.locator("#sourceProfile").select_option("s2-earth-search")
        page.locator("#startDate").fill("2024-06-10");page.locator("#endDate").fill("2024-06-11")
        page.locator("#sourceProfile").select_option(S2)
        expect(page.locator("#worldcoverInfo")).to_be_visible()
        expect(page.locator("#startDate")).to_have_value("2021-01-01")
        expect(page.locator("#cloudOptions")).to_be_hidden()
        expect(page.locator("#processAuth")).to_be_hidden()
        expect(page.locator("#rawMetadataOnly")).to_be_disabled()
        page.locator('[data-tab="coordinates"]').click()
        page.locator("#coordinates").fill(RAPA);page.locator("#applyCoordinates").click()
        expect(page.locator("#aoiName")).to_have_text("坐标研究区")
        page.locator("#startDate").fill(CASES[0].dates[0]);page.locator("#endDate").fill(CASES[0].dates[1]);page.locator("#searchScenes").click()
        solar=page.locator(f'#scenesBody tr[data-key="{product(2021,"S28W110")}"]')
        expect(solar.locator("input")).to_be_checked(timeout=90000)
        for layer in CASES[0].assets:
            page.locator(f'#rawMultiAssets input[value="{layer}"]').check()
        expect(page.locator("#rawMultiAssets")).to_contain_text("p90 / p50 / p10")
        page.locator("#outputDirectory").fill(str(out/CASES[0].name))
        page.locator("#downloadSelected").click(timeout=20000)
        expect(page.locator("#planNote")).to_contain_text("校验源 ETag")
        page.locator("#confirmDownload").click()
        expect(page.locator("#jobStatus")).to_have_text("下载完成",timeout=90000)
        files=page.locator("#jobFiles .job-file");expect(files).to_have_count(3)
        assert all("已有" in s for s in files.all_inner_texts())
        page.locator("#closeJobPanel").click()
        page.locator("#sourceProfile").select_option(S1)
        expect(page.locator("#scenesBody tr")).to_have_count(0)
        expect(page.locator("#rawOptions")).to_be_hidden()
        page.locator("#searchScenes").click();expect(solar).to_contain_text("官方网格未发布",timeout=90000)
        expect(solar.locator("input")).to_be_disabled();expect(page.locator("#downloadSelected")).to_be_disabled()
        page.locator("#sourceProfile").select_option(MAP)
        page.locator("#startDate").fill("2020-12-31");page.locator("#endDate").fill("2021-01-01");page.locator("#searchScenes").click()
        expect(page.locator("#scenesBody tr")).to_have_count(2,timeout=90000)
        page.locator("#selectAll").click();expect(page.locator("#downloadEstimate")).to_contain_text("2 个原始文件")
        page.locator("#sourceProfile").select_option("s2-earth-search")
        expect(page.locator("#startDate")).to_have_value("2024-06-10")
        expect(page.locator("#endDate")).to_have_value("2024-06-11")
        assert not errors,errors
        browser.close()
    return {"three_source_selectors":True,"cut_off_dates_seeded_and_restored":True,"optical_asset_selection_and_reuse":True,
            "unavailable_radar_disabled":True,"two_calendar_years_map_plan":True}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--exe",type=Path)
    parser.add_argument("--output",type=Path,required=True)
    parser.add_argument("--reference-dir",type=Path)
    parser.add_argument("--reference-only",action="store_true")
    parser.add_argument("--no-browser",action="store_true")
    parser.add_argument("--timeout",type=int,default=750)
    args=parser.parse_args()
    out=args.output.resolve();out.mkdir(parents=True,exist_ok=True)
    refs_path=(args.reference_dir or out/"reference").resolve()
    manifest_path=refs_path/"worldcover-independent-s3.json"
    if not manifest_path.is_file() or args.reference_only:
        refs=fetch_all(refs_path)
        manifest_path.write_text(json.dumps(refs,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    else:
        refs=json.loads(manifest_path.read_text(encoding="utf-8"))
        for name,row in refs.items():
            path=refs_path/name
            assert path.stat().st_size==row["size"] and digest(path)==row["sha256"]
            row["reference_path"]=str(path)
    if args.reference_only:
        print(f"Verified {len(refs)} independent source objects: {manifest_path}",flush=True)
        return
    exe=args.exe.resolve(strict=True) if args.exe else None
    report_path=out/"worldcover-live-report.json"
    report={"status":"running","binary_sha256":digest(exe) if exe else None,"cases":[]}
    def save():
        report_path.write_text(json.dumps(report,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    try:
        save()
        with local_app(exe,out,out/"settings") as (origin,session):
            api=Local(origin,session)
            data=session.get(f"{origin}/api/bootstrap",timeout=10).json();report["version"]=data["version"]
            assert not data["connected"] and not data["raw_connected"] and not data["s3_connected"]
            for case in CASES:
                report["cases"].append(verify_case(api,case,out,refs,args.timeout));save()
            report["negative"]=verify_missing(api,out)
            if not args.no_browser:
                report["ui"]=validate_browser(origin,out)
        report["status"]="completed"
        print(f"Live verification completed: {report_path}",flush=True)
    except Exception as error:
        report.update(status="failed",error=f"{type(error).__name__}: {error}")
        raise
    finally:
        save()


if __name__=="__main__":
    main()
