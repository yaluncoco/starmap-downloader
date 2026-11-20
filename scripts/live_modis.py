"""Verify real MODIS C6.1 downloads from the Windows EXE against independent references."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from live_public import local_app
from live_s2_tiles import Local, digest

RAPA = "-109.42,-27.16,-109.39,-27.13"
HAWAII = "-155.60,19.41,-155.55,19.46"
BEIJING = "116.350,39.900,116.355,39.905"
CORE = (
    ("modis-13Q1-061", "MOD13Q1.A2024177.h08v11.061.2024195021002", "2024-06-25", RAPA),
    ("modis-11A2-061", "MYD11A2.A2024177.h08v11.061.2024186040837", "2024-06-25", RAPA),
    ("modis-09Q1-061", "MYD09Q1.A2024177.h08v11.061.2024186040620", "2024-06-25", RAPA),
    ("modis-15A2H-061", "MCD15A2H.A2024177.h08v11.061.2024186035409", "2024-06-25", RAPA),
    ("modis-43A4-061", "MCD43A4.A2024182.h03v07.061.2024191035321", "2024-06-30", HAWAII),
)
EXTRA = (
    ("modis-13Q1-061", "MYD13Q1.A2024185.h08v11.061.2024207132739", "2024-07-03", RAPA),
    ("modis-15A2H-061", "MYD15A2H.A2024185.h08v11.061.2024195054846", "2024-07-03", RAPA),
    ("modis-11A2-061", "MYD11A2.A2024361.h26v05.061.2025004063138", "2024-12-26", BEIJING),
    ("modis-09Q1-061", "MYD09Q1.A2024361.h26v05.061.2025004062944", "2024-12-26", BEIJING),
    ("modis-43A4-061", "MCD43A4.A2024361.h26v05.061.2025004061124", "2024-12-26", BEIJING),
)
NDVI = "250m_16_days_NDVI"


def search(api, profile, coords, start, end):
    geometry=api.post("aoi", {"coordinates":coords})["geometry"]
    criteria={"start":start,"end":end}
    result=api.job("search", {"geometry":geometry,"profile":profile,**criteria}, timeout=150)["result"]
    assert all(start <= row["date"] <= end and row["time"] == "标称日" for row in result["scenes"])
    return {"geometry":geometry,"profile":profile,"criteria":criteria,"query_id":result["query"]["id"]}, result["scenes"]


def case_plan(api, case, out, aliases):
    profile, key, nominal, coords = case
    base, scenes = search(api, profile, coords, nominal, nominal)
    row = next((x for x in scenes if x["key"] == key), None)
    assert row and row["raw"]["available"] and row["tiles"] == [key.split(".")[2]]
    assert set(aliases) <= {a["id"] for a in row["raw"]["assets"]}, (key,aliases)
    request={**base,"scene_keys":[key],"output_dir":str(out),"options":{"mode":"assets","assets":sorted(aliases),"transport":"pc-public"}}
    plan=api.post("plan", request)
    assert plan["transports"] == ["pc-public"] and not plan["requires"] and plan["requests"] == len(aliases)
    return {**request,"plan_fingerprint":plan["fingerprint"]},row


def verify_download(api, request, refs):
    result = api.job("download", request, timeout=900)["result"]
    manifest = Path(result["manifest"])
    text = manifest.read_text(encoding="utf-8")
    assert "sig=" not in text and "?" not in text
    doc = json.loads(text)
    assert doc["status"] == "completed" and tuple(map(int, doc["software"].split("."))) >= (1, 11, 0)
    files = {}
    for r in doc["items"]:
        alias = r["asset"]
        ref = refs[alias]
        assert r["url"] == ref["url"] and r["etag"] == ref["etag"] and r["status"] == "completed"
        assert r["transport"] == "pc-public" and r["provider_checksum_verified"] == "Azure Blob Content-MD5"
        path = Path(r["path"])
        assert path.name == ref["url"].rsplit("/",1)[-1] and path.stat().st_size == ref["bytes"] == r["size"]
        assert digest(path) == r["sha256"] == ref["sha256"] and r["azure_blob_md5"] == ref["source_md5"]
        ident = r["catalogue_identity"]
        assert ident["nominal_date"] == request["criteria"]["start"] and ident["version"] == "061"
        real = r["modis_validation"]
        if alias == "metadata":
            assert real["container"] == "XML" and real["hdf_md5"] == ref["nasa_xml"]["hdf_md5"]
        elif alias == "hdf":
            assert real["container"] == "HDF4"
            if "metadata" in refs:
                assert real["nasa_xml_verified"]["hdf_md5"] == r["azure_blob_md5"]
            else:
                assert real["nasa_xml_verified"] is None
        else:
            assert real["container"] == "COG" and real["decoded_cells"] >= 1200**2
            period = real["embedded_observation_period"]
            assert [period["from"],period["through"]] == ref["embedded_dates"]
            contract = r["catalogue_raster"]
            if alias == NDVI:
                assert contract["physical_scale"] == .0001 and contract["tiff_scale"] == 10000
        files[alias] = {"file":str(path),"size":r["size"],"sha256":r["sha256"],"source_md5":r["azure_blob_md5"],"validation":real}
    assert set(files) == set(refs) and not list(Path(result["folder"]).glob("*.part"))
    return {"files":files,"manifest":str(manifest)}


def failure_paths(api,out, refs):
    case = CORE[2]
    base,rows = search(api,case[0],case[3],case[2],case[2]); row = next(x for x in rows if x["key"] == case[1])
    assert "无配套 XML" in row["raw"]["source_note"] and not any(a["id"]=="metadata" for a in row["raw"]["assets"])
    wrong={**base,"scene_keys":[case[1]],"output_dir":str(out),"options":{"mode":"metadata"}}
    denied=api.post("plan",wrong,400)
    assert "没有可下载" in denied["error"]
    assert not search(api,CORE[4][0],RAPA,CORE[4][2],CORE[4][2])[1]
    rows = search(api,CORE[4][0],HAWAII,"2024-07-01","2024-07-01")[1]
    assert all(row["key"] != CORE[4][1] for row in rows)  # Source period overlaps; nominal day does not.
    wrong_day = search(api, CORE[2][0], BEIJING, "2025-01-01", "2025-01-01")[1]
    assert all(row["key"] != EXTRA[3][1] for row in wrong_day)  # December composite overlaps January.
    request,_ = case_plan(api, CORE[0], out/"故障恢复", [NDVI])
    once = verify_download(api, request, refs)
    healthy = api.job("download", request, timeout=120)["result"]
    assert healthy["files"][0]["status"] == "skipped"
    path = Path(once["files"][NDVI]["file"])
    receipt = path.with_suffix(".starmap.json")
    with path.open("rb+") as f:
        f.seek(-1, os.SEEK_END); b=f.read(1); f.seek(-1, os.SEEK_END); f.write(bytes([b[0] ^ 1]))
    edited = json.loads(receipt.read_bytes())
    edited["sha256"] = digest(path)
    receipt.write_text(json.dumps(edited), encoding="utf-8")
    rejected = api.job("download", request, expected="failed", timeout=120)
    assert "MD5" in rejected["error"] and path.is_file() and digest(path) != refs[NDVI]["sha256"]
    path.replace(path.with_suffix(".rejected")); receipt.replace(receipt.with_suffix(".rejected-record"))
    reread = verify_download(api,request,refs)
    return {"missing_xml":denied["error"],"no_nbar_in_rapa":True,"nominal_not_overlap":True,"composite_after_new_year_excluded":True,
            "reuse":True,"same_length_tamper_rejection":rejected["error"],"recovery":reread["manifest"]}


def validate_browser(origin, out, refs):
    from playwright.sync_api import expect, sync_playwright
    with sync_playwright() as engine:
        browser = engine.chromium.launch(channel=os.environ.get("STARMAP_TEST_BROWSER","msedge"),headless=True)
        page = browser.new_page(viewport={"width":1440,"height":960})
        errors = []
        page.on("pageerror",lambda error:errors.append(str(error)))
        page.route("https://**/*",lambda route:route.abort())
        page.goto(origin,wait_until="domcontentloaded")
        expect(page.locator('#sourceProfile option[value^="modis-"]')).to_have_count(5)
        page.locator("#sourceProfile").select_option(CORE[4][0]); expect(page.locator("#modisDateNote")).to_contain_text("第 9 天")
        page.locator("#sourceProfile").select_option(CORE[0][0]); expect(page.locator("#modisDateNote")).to_contain_text("合成起始日")
        expect(page.locator("#cloudOptions")).to_be_hidden(); expect(page.locator("#processAuth")).to_be_hidden()
        expect(page.locator("#sceneDateHeading")).to_have_text("标称日")
        page.locator('[data-tab="coordinates"]').click(); page.locator("#coordinates").fill(RAPA); page.locator("#applyCoordinates").click()
        expect(page.locator("#aoiName")).to_have_text("坐标研究区")
        for name in ("startDate","endDate"):
            page.locator("#"+name).fill(CORE[0][2])
        page.locator("#searchScenes").click()
        row=page.locator(f'#scenesBody tr[data-key="{CORE[0][1]}"]')
        expect(row.locator("input")).to_be_checked(timeout=90000)
        expect(page.locator("#rawMultiAssets")).to_contain_text("0.0001（不是 TIFF 的 10000）")
        page.locator(f'#rawMultiAssets input[value="{NDVI}"]').check()
        page.locator("#outputDirectory").fill(str(out/"页面核验"))
        page.locator("#downloadSelected").click(timeout=18000)
        expect(page.locator("#planNote")).to_contain_text("完整 MD5")
        page.locator("#confirmDownload").click()
        expect(page.locator("#jobStatus")).to_have_text("下载完成",timeout=120000)
        expect(page.locator("#jobFiles .job-file")).to_have_count(1)
        assert not errors,errors
        path,=list((out/"页面核验").rglob("*.tif")); assert digest(path)==refs[NDVI]["sha256"]
        browser.close()
    return {"five_categories":True,"nominal_days_and_scale_notice":True,"map_coordinates_and_layer_choice":True,"confirmed_real_download_sha256":digest(path)}


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--exe",type=Path)
    p.add_argument("--output",type=Path,required=True)
    p.add_argument("--reference-dir",type=Path,default=Path(__file__).resolve().parents[1]/"verification/modis-next-source")
    p.add_argument("--smoke",action="store_true",help="One small COG instead of the full matrix; for upgrade regression.")
    a=p.parse_args()
    out=a.output.resolve(); out.mkdir(parents=True,exist_ok=True)
    evidence=json.loads((a.reference_dir/"modis-independent-report.json").read_bytes())
    report={"status":"in_progress","reference_summary":evidence["summary"],"archive_cases":{}}
    dest=out/"modis-live-report.json"
    def save():
        dest.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
    with local_app(a.exe.resolve(strict=True) if a.exe else None,out) as (origin,session):
        api=Local(origin,session)
        for case in (CORE[:1] if a.smoke else CORE+EXTRA):
            refs=evidence["products"][case[1]]
            if a.smoke:
                refs={NDVI:refs[NDVI]}
            r,_=case_plan(api,case,out/"产品实传",refs)
            report["archive_cases"][case[1]]=verify_download(api,r,refs)
            save()
        refs={NDVI:evidence["products"][CORE[0][1]][NDVI]}
        if not a.smoke:
            report["failures_and_recovery"]=failure_paths(api,out,refs)
            save()
            report["desktop_page"]=validate_browser(origin,out,refs)
    items=[entry for product in report["archive_cases"].values() for entry in product["files"].values()]
    report.update(status="completed", unique_source_objects=len(items), total_source_bytes=sum(i["size"] for i in items))
    save()
    print(f"Verified {len(items)} exact MODIS objects, {report['total_source_bytes']} bytes; {dest}")


if __name__=="__main__":
    main()
