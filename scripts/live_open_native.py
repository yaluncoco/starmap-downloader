"""Real multi-file check of Sentinel-2 L1C and Landsat through the selected Windows binary."""
from __future__ import annotations

import argparse
import base64
import datetime as dt
import hashlib
import json
import os
import time
from pathlib import Path

import numpy as np
import rasterio
import requests

from live_public import BBOX, head_aws_object, local_app

CDSE_SEARCH = "https://stac.dataspace.copernicus.eu/v1/search"
L1C_KEY = "S2A_50TMK_20240610_0_L1C"
L1C_SAFE = "S2A_MSIL1C_20240610T030531_N0510_R075_T50TMK_20240610T054711"
L_KEY = "LC08_L2SP_123032_20240611_02_T1"
L_RELEASE = "LC08_L2SP_123032_20240611_20240628_02_T1"
AZURE_ROOT = "https://landsateuwest.blob.core.windows.net/landsat-c2/"
AZURE_TOKEN = "https://planetarycomputer.microsoft.com/api/sas/v1/token/landsateuwest/landsat-c2"


from browser_flow import open_data_page

def digest(path, constructor):
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, constructor).hexdigest()


def response(url, verb="GET", **kwargs):
    try:
        result = requests.request(verb, url, timeout=(10, 40), allow_redirects=False, **kwargs)
    except requests.RequestException as error:
        raise RuntimeError(f"Independent source request failed ({type(error).__name__})") from None
    if result.status_code not in (200, 206):
        status = result.status_code
        result.close()
        # In particular, don't include a signed URL or its query parameters in a traceback.
        raise RuntimeError(f"Independent source returned HTTP {status}")
    return result


def azure_source(url, token, cap):
    assert url.startswith(AZURE_ROOT) and "?" not in url
    with response(url+"?"+token, "HEAD") as result:
        length = int(result.headers["Content-Length"])
        assert 0 < length <= cap, f"Unexpected source size: {length}"
        tag = result.headers["ETag"]
        raw = result.headers.get("Content-MD5")
        md5 = base64.b64decode(raw, validate=True).hex() if raw else None
    return length, tag, md5


def compare_anchors(url, token, path, etag, length):
    n = 16384
    checks = []
    with path.open("rb") as handle:
        for begin in (0, length//2, length-n):
            end = begin+n-1
            hdr = {"Range": f"bytes={begin}-{end}", "If-Match": etag, "Accept-Encoding": "identity"}
            with response(url+"?"+token,"GET",headers=hdr,stream=True) as ranged:
                assert ranged.status_code == 206 and ranged.headers["ETag"] == etag
                assert ranged.headers["Content-Range"] == f"bytes {begin}-{end}/{length}"
                try:
                    blob = ranged.content
                except requests.RequestException:
                    raise RuntimeError("Independent range read failed") from None
                handle.seek(begin)
                assert len(blob) == n and handle.read(n) == blob
            checks.append([begin,end])
    return checks


def verify_compiled_ui(origin, out):
    from playwright.sync_api import expect, sync_playwright
    with sync_playwright() as driver:
        browser = driver.chromium.launch(channel=os.environ.get("STARMAP_TEST_BROWSER", "msedge"), headless=True)
        page = browser.new_page(viewport={"width":1360,"height":900})
        errors=[]
        page.on("pageerror",lambda error:errors.append(str(error)))
        page.route("https://**/*",lambda route:route.abort())  # map tiles are out of scope; Python still reaches the data providers.
        open_data_page(page, origin)
        expect(page.locator("#outputDirectory")).not_to_have_value("")
        page.locator("#sourceProfile").select_option("s2-l1c-aws")
        page.locator('[data-tab="coordinates"]').click()
        page.locator("#coordinates").fill(BBOX)
        page.locator("#applyCoordinates").click()
        expect(page.locator("#aoiName")).to_have_text("坐标研究区")
        page.locator("#startDate").fill("2024-06-10")
        page.locator("#endDate").fill("2024-06-10")
        page.locator("#maxCloud").press("End")
        page.locator("#searchScenes").click()
        expect(page.locator("#scenesBody")).to_contain_text(L1C_SAFE,timeout=60000)
        for alias in ("B01","B10","metadata.xml"):
            page.locator(f'#rawMultiAssets input[value="{alias}"]').check()
        page.locator("#outputDirectory").fill(str(out))
        page.locator("#downloadSelected").click()
        expect(page.locator("#planDetails")).to_contain_text("3 个")
        page.locator("#confirmDownload").click()
        expect(page.locator("#jobStatus")).to_have_text("下载完成",timeout=20000)
        expect(page.locator("#jobFiles .job-file")).to_have_count(3)
        for entry in page.locator("#jobFiles .job-file").all():
            expect(entry).to_contain_text("已有")
        assert not errors
        browser.close()
    return {"profile":"s2-l1c-aws","checked_files":3,"verified_existing_after_real_search":True}


def check(exe, out, with_measurement):
    out.mkdir(parents=True, exist_ok=True)
    report = {"started_utc": dt.datetime.now(dt.timezone.utc).isoformat(), "native_batches": []}
    record = out / "open-native-report.json"
    with response(CDSE_SEARCH, "POST", json={"collections": ["sentinel-2-l1c"], "ids": [L1C_SAFE], "limit": 5}) as r:
        official, = [v for v in r.json()["features"] if v["id"] == L1C_SAFE]
    with local_app(exe, out) as (origin, api):
        def post(action, payload):
            r=api.post(origin+"/api/"+action,json=payload,timeout=(5,180))
            if not r.ok:
                raise RuntimeError(f"{action}: {r.json().get('error',r.status_code)}")
            return r.json()

        def job(action, payload, timeout_seconds=200):
            jobid=post(action,payload)["job_id"]
            deadline=time.monotonic()+timeout_seconds
            while time.monotonic() < deadline:
                snap=api.get(origin+"/api/job",params={"id":jobid},timeout=5).json()
                if snap["status"] == "completed":
                    return snap["result"]
                if snap["status"] not in ("running", "cancelling"):
                    raise RuntimeError(f"{action}: {snap.get('error') or snap['message']}")
                time.sleep(.5)
            post("cancel", {"job_id":jobid})
            raise RuntimeError(f"Timed out during {action}")

        report["version"]=api.get(origin+"/api/bootstrap",timeout=5).json()["version"]
        area=post("aoi",{"coordinates":BBOX})["geometry"]
        cases=[("s2-l1c-aws",L1C_KEY,L1C_SAFE+".SAFE","2024-06-10","public",["B01","B10","metadata.xml"],BBOX),
               ("landsat-pc-l2",L_KEY,L_RELEASE,"2024-06-11","pc-public",["mtl.json","qa_pixel",*(["red"] if with_measurement else [])],BBOX)]
        history=[("LT04_L2SP_015033_19821118_02_T1","LT04_L2SP_015033_19821118_20200918_02_T1","1982-11-18","-77.08,38.88,-77.03,38.91"),
                 ("LT05_L2SP_123032_19950714_02_T2","LT05_L2SP_123032_19950714_20200912_02_T2","1995-07-14",BBOX),
                 ("LE07_L2SP_123032_20000719_02_T1","LE07_L2SP_123032_20000719_20200917_02_T1","2000-07-19",BBOX),
                 ("LC09_L2SP_123032_20240619_02_T1","LC09_L2SP_123032_20240619_20240620_02_T1","2024-06-19",BBOX)]
        cases += [("landsat-pc-l2",item,release,day,"pc-public",["mtl.json","qa_pixel"],bounds) for item,release,day,bounds in history]
        for profile, item, product, day, transport, aliases, bounds in cases:
            print(f"Live multi-file batch: {profile} / {item}: {aliases}",flush=True)
            geometry=area if bounds == BBOX else post("aoi",{"coordinates":bounds})["geometry"]
            base={"profile":profile,"geometry":geometry}
            criteria={"start":day,"end":day,"max_cloud":100}
            search=job("search",{**base,**criteria})
            found, = [row for row in search["scenes"] if row["key"] == item and row["title"] == product]
            assert found["raw"]["available"]
            req={**base,"criteria":criteria,"query_id":search["query"]["id"],"scene_keys":[item],"output_dir":str(out),
                 "options":{"mode":"assets","assets":aliases,"transport":transport}}
            plan=post("plan",req)
            assert len(plan["targets"]) == len(aliases) == plan["requests"] and not plan["requires"]
            assert plan["transports"] == [transport] and [target["asset"] for target in plan["targets"]] == aliases
            proofs=[]
            token=""
            if transport == "pc-public":
                with response(AZURE_TOKEN) as r:
                    token=r.json()["token"]
            for target in plan["targets"]:
                if transport == "public":
                    size, tag=head_aws_object(target["url"],10*1024**2)
                    proofs.append((size,tag,tag[1:-1]))
                else:
                    proofs.append(azure_source(target["url"],token,(100 if target["asset"]=="red" else 10)*1024**2))
            finished=job("download",req,timeout_seconds=620 if transport == "pc-public" and with_measurement else 240)
            manifest_text=Path(finished["manifest"]).read_text(encoding="utf-8")
            if token:
                assert token not in manifest_text and "?sv=" not in manifest_text and "sig=" not in manifest_text
            rows=json.loads(manifest_text)["items"]
            native=[]
            for target, file, row, (size, tag, md5) in zip(plan["targets"], finished["files"], rows, proofs, strict=True):
                path=Path(file["path"])
                assert file["status"] == "completed" and row["asset"] == target["asset"] and row["product"] == item
                local_sha=digest(path,"sha256")
                assert row["etag"] == tag and row["url"] == target["url"] and "?" not in row["url"]
                assert local_sha == row["sha256"] and path.stat().st_size == row["size"] == size
                detail={"asset":target["asset"],"name":target["name"],"bytes":size,"sha256":local_sha,
                        "etag":tag,"path":str(path),"upstream_md5":md5,"format_validation":row["format_validation"]}
                if md5:
                    assert md5 == digest(path,lambda:hashlib.md5(usedforsecurity=False))
                    detail["provider_checksum_verified"] = row["provider_checksum_verified"]
                if transport == "public":
                    assert row["safe_product"] == L1C_SAFE and row["l1c_index_etag"]
                    alias=target["asset"] if path.suffix==".jp2" else "granule_metadata"
                    official_asset=official["assets"][alias]
                    assert size == official_asset["file:size"]
                    if path.suffix==".jp2":
                        wanted=official_asset["file:checksum"]
                        assert wanted.startswith("1620") and len(wanted) == 68
                        actual=digest(path,"sha3_256")
                        assert wanted[4:] == actual
                        detail["independent_cdse_sha3_256"] = actual
                if transport=="pc-public" and not md5 and target.get("raster"):
                    detail["source_range_bytes_matched"] = compare_anchors(target["url"],token,path,tag,size)
                if target.get("raster"):
                    with rasterio.open(path) as ds:
                        assert ds.shape == tuple(target["raster"]["shape"]) and ds.crs.to_epsg() == target["raster"]["epsg"]
                        data=ds.read(1,out_shape=(96,96),masked=True)
                        assert data.count() and np.min(data.compressed()) < np.max(data.compressed())
                        detail.update(grid=[ds.width,ds.height],epsg=ds.crs.to_epsg(),dtype=ds.dtypes[0])
                native.append(detail)
            repeated=job("download",req)
            assert len(repeated["files"]) == len(native) and all(row["status"]=="skipped" for row in repeated["files"])
            report["native_batches"].append({"profile":profile,"product":product,"transport":transport,"manifest":finished["manifest"],
                                             "repeat_verified":True,"bbox_wgs84":bounds,"files":native})
            record.write_text(json.dumps(report,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
        if with_measurement and exe:
            print("Installed UI: real L1C query, multi-select and checked reuse",flush=True)
            report["installed_browser_flow"] = verify_compiled_ui(origin,out)
            record.write_text(json.dumps(report,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    return report


if __name__ == "__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--exe",type=Path)
    parser.add_argument("--output",type=Path,required=True)
    parser.add_argument("--landsat-measurement",action="store_true",help="Include ~87 MiB Landsat red-band COG")
    args=parser.parse_args()
    result=check(args.exe.resolve(strict=True) if args.exe else None,args.output.resolve(),args.landsat_measurement)
    print(json.dumps(result,ensure_ascii=True,indent=2))
