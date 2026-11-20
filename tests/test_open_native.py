"""Independent source bindings, anonymous SAS scope, and multi-asset execution."""
from __future__ import annotations

import base64
import datetime as dt
import hashlib
import json
from pathlib import Path
from urllib.parse import urlsplit

import pytest
import requests
from shapely.geometry import box, mapping

from conftest import BBOX, StubResponse
from core.errors import UserError
from core.http import HttpClient
from core.native_catalogue import asset_details
from core.open_native import L1C_COLLECTION, LANDSAT_COLLECTION, PC_SEARCH, _cursor, l1c_row, landsat_row
from core.public_token import PC_TOKEN, PlanetaryAccess
from core.raw_download import _validated_digests, _verify_l1c_index, raw_plan
from core.s3_source import PC_LANDSAT_HOST, S2_L1C_BUCKET

AOI = box(116.350,39.900,116.355,39.905)
DATE = dt.date(2024,6,11)
STOP = DATE+dt.timedelta(days=1)
LANDSAT = "LC08_L2SP_123032_20240611_02_T1"
LANDSAT_RELEASE = "LC08_L2SP_123032_20240611_20240628_02_T1"
L_BASE = f"https://{PC_LANDSAT_HOST}/landsat-c2/level-2/standard/oli-tirs/2024/123/032/"
LP = L_BASE+LANDSAT_RELEASE+"/"+LANDSAT_RELEASE+"_"
S2_ID = "S2A_50TMK_20240610_0_L1C"
SAFE = "S2A_MSIL1C_20240610T030531_N0510_R075_T50TMK_20240610T054711.SAFE"
L1C_PATH = "tiles/50/T/MK/2024/6/10/0"
L1C_PREFIX = f"s3://{S2_L1C_BUCKET}/{L1C_PATH}/"
S2_TIME = "2024-06-10T03:16:50.553000Z"
MTL = json.dumps({"LANDSAT_METADATA_FILE": {"PRODUCT_CONTENTS":{"LANDSAT_PRODUCT_ID": LANDSAT_RELEASE},
                                           "IMAGE_ATTRIBUTES":{"DATE_ACQUIRED": "2024-06-11", "WRS_PATH":123, "WRS_ROW":32}}}).encode()
AUX = b"valid metadata file for this fixture\n"
ETAG = '"test-stable-index"'


def pc_feature():
    return {"id": LANDSAT, "collection": LANDSAT_COLLECTION, "geometry": mapping(AOI),
            "properties": {"datetime": "2024-06-11T02:52:35Z", "eo:cloud_cover": 9.33, "landsat:collection_number": "02",
                           "landsat:wrs_path": "123", "landsat:wrs_row": "032", "proj:epsg": 32650,
                           "proj:shape": [7881, 7761], "proj:transform": [30,0,360285,0,-30,4582515]},
            "assets": {"mtl.json": {"href": LP+"MTL.json", "roles": ["metadata"]},
                       "ang": {"href": LP+"ANG.txt", "roles": ["metadata"]},
                       "qa_pixel": {"href": LP+"QA_PIXEL.TIF", "roles": ["cloud", "water-mask"], "raster:bands":[{"data_type":"uint16"}]},
                       "rendered_preview": {"href": "https://render.example.test/result.jpg", "roles":["overview"]}}}


def s2_feature():
    return {"id": S2_ID, "collection": L1C_COLLECTION, "geometry": mapping(AOI),
            "properties": {"datetime": S2_TIME, "eo:cloud_cover": 8.9, "s2:product_uri": SAFE, "proj:epsg": 32650},
            "assets": {"cirrus": {"href": L1C_PREFIX+"B10.jp2", "roles":["data"], "raster:bands":[{"data_type":"uint16"}],
                                  "proj:shape":[1830,1830], "proj:transform":[60,0,399960,0,-60,4500000]},
                       "tileinfo_metadata": {"href": L1C_PREFIX+"tileInfo.json", "roles":["metadata"]},
                       "granule_metadata": {"href": L1C_PREFIX+"metadata.xml", "roles":["metadata"]}}}


def test_l1c_binds_each_url_to_selected_mgrs_slot_and_day():
    doc = s2_feature()
    assert {x["id"] for x in l1c_row(doc, AOI, .3, DATE-dt.timedelta(days=1), DATE, 50)["raw"]["assets"]} == {"B10","tileInfo.json","metadata.xml"}
    doc["assets"]["cirrus"]["href"] = L1C_PREFIX.replace("/10/0/", "/10/1/")+"B10.jp2"
    item = l1c_row(doc, AOI, .3, DATE-dt.timedelta(days=1), DATE, 50)
    assert {a["id"] for a in item["raw"]["assets"]} == {"tileInfo.json","metadata.xml"}
    doc["properties"]["s2:product_uri"] = SAFE.replace("50TMK", "50SMM")
    assert l1c_row(doc, AOI, .3, DATE-dt.timedelta(days=1), DATE, 50) is None


@pytest.mark.parametrize("state", ["good","wrong-product","wrong-time","changed-after"])
def test_l1c_index_rejects_wrong_or_changed_source(state):
    item = l1c_row(s2_feature(), AOI, .3, DATE-dt.timedelta(days=1), DATE, 50)
    target, = raw_plan(AOI,[item],{"mode":"asset","asset":"B10","collection":L1C_COLLECTION})["targets"]
    stamp = S2_TIME if state != "wrong-time" else "2024-06-10T03:16:51Z"
    id = SAFE.removesuffix(".SAFE") if state != "wrong-product" else "other-SAFE"
    doc = json.dumps({"path": L1C_PATH, "productName": id,"timestamp": stamp}).encode()
    class Http:
        def request(self, method, url, **kw):
            assert url.endswith("/tileInfo.json")
            if method == "HEAD":
                assert kw["headers"]["If-Match"] == ETAG
                return StubResponse(412 if state == "changed-after" else 200, headers={"ETag": ETAG})
            return StubResponse(content=doc, headers={"ETag": ETAG})
    if state in ("wrong-product","wrong-time"):
        with pytest.raises(UserError, match="采集时间"):
            _verify_l1c_index(Http(), target, None)
    else:
        assert _verify_l1c_index(Http(), target, None) == ETAG
        if state == "changed-after":
            with pytest.raises(UserError, match="版本已变化"):
                _verify_l1c_index(Http(), target, None, ETAG)
        else:
            assert _verify_l1c_index(Http(), target, None, ETAG) == ETAG


def test_landsat_requires_one_processing_release_and_vetted_account():
    doc=pc_feature()
    first=landsat_row(doc,AOI,.3,DATE,STOP,20)
    assert first["title"] == LANDSAT_RELEASE
    assert {a["id"] for a in first["raw"]["assets"]} == {"mtl.json", "ang", "qa_pixel"}
    doc["assets"]["ang"]["href"] = LP.replace("20240628", "20240701")+"ANG.txt"
    assert landsat_row(doc,AOI,.3,DATE,STOP,20) is None
    doc["assets"]["ang"]["href"] = LP.replace(PC_LANDSAT_HOST, "other.blob.core.windows.net")+"ANG.txt"
    assert {a["id"] for a in landsat_row(doc,AOI,.3,DATE,STOP,20)["raw"]["assets"]} == {"mtl.json", "qa_pixel"}


def test_cursor_only_accepts_fixed_stac_and_sane_token():
    good = {"rel":"next","method":"POST","href":PC_SEARCH+"?collections=landsat-c2-l2","body":{"token":"next:same", "limit":900}}
    assert _cursor({"links":[good]},PC_SEARCH,LANDSAT_COLLECTION,"token") == "next:same"
    for bad in ({**good,"href":"https://other.example.test/search"}, {**good,"body":{"token":True}}, {**good,"method":"GET"}):
        with pytest.raises(UserError,match="分页"):
            _cursor({"links":[bad]},PC_SEARCH,LANDSAT_COLLECTION,"token")


def test_multi_plan_refuses_duplicate_or_unrelated_asset():
    scene=landsat_row(pc_feature(),AOI,.3,DATE,STOP,20)
    opts={"mode":"assets","transport":"pc-public","collection":LANDSAT_COLLECTION}
    for selected in ([],["missing"],["mtl.json","mtl.json"],[[]],"mtl.json"):
        with pytest.raises(UserError):
            raw_plan(AOI,[scene],{**opts,"assets":selected})
    with pytest.raises(UserError):
        raw_plan(AOI,[scene,scene],{**opts,"assets":["mtl.json"]})
    plan=raw_plan(AOI,[scene],{**opts,"assets":["ang","mtl.json"]})
    assert not plan["requires"] and plan["requests"] == 2
    assert [a["asset"] for a in plan["targets"]] == ["ang","mtl.json"]
    meta=raw_plan(AOI,[scene],{**opts,"mode":"metadata"})
    assert meta["targets"][0]["asset"] == "mtl.json"


def test_real_multihash_sha3_family_and_malformed_md5(tmp_path):
    sample=tmp_path/"source"
    sample.write_bytes(b"sha3 test bytes")
    digest=hashlib.sha3_256(sample.read_bytes()).hexdigest()
    item=asset_details("B01",{"file:checksum":"1620"+digest},"B01.jp2")
    assert _validated_digests(sample,item,None,None)[1] == "SHA3-256"
    malformed=asset_details("metadata",{"file:checksum":"d50120"+digest},"metadata.xml")
    assert not malformed["provider_sha3_256"] and not malformed["provider_md5"]
    with pytest.raises(UserError,match="SHA3-256"):
        _validated_digests(sample,{**item,"provider_sha3_256":"0"*64},None,None)


@pytest.mark.parametrize("bad_sp", ["rwd","w","rl&sp=r"])
def test_pc_rejects_unsupported_or_ambiguous_sas_permissions(bad_sp):
    class Http:
        def request(self, method, url, **kw):
            assert method == "GET" and url == PC_TOKEN
            return StubResponse(data={"token":f"sr=c&sp={bad_sp}&sig=synthetic", "msft:expiry":expiry()})
    auth=PlanetaryAccess(Http())
    for method,url in [("POST",LP+"MTL.json"),("GET",LP+"MTL.json?x=1"),("GET","https://elsewhere.example.test/asset"),("GET",LP+"MTL.json")]:
        with pytest.raises(UserError):
            requests.Request(method,url,auth=auth).prepare()


def expiry():
    return (dt.datetime.now(dt.timezone.utc)+dt.timedelta(hours=2)).isoformat()


@pytest.fixture
def pc_provider(monkeypatch):
    state={"tokens":0,"blob_calls":[],"deny_once":False,"wrong_mtl":False}
    def request(_, method, url, **kwargs):
        if url == PC_SEARCH:
            assert method == "POST" and kwargs["json"]["collections"] == [LANDSAT_COLLECTION]
            return StubResponse(data={"features":[pc_feature()]})
        if url == PC_TOKEN:
            state["tokens"]+=1
            return StubResponse(data={"token":f"sr=c&sp=rl&sig=temporary-fixture-{state['tokens']}", "msft:expiry":expiry()})
        assert method == "GET" and url.startswith(LP) and "?" not in url
        signed=requests.Request(method,url,headers=kwargs["headers"],auth=kwargs["auth"]).prepare()
        assert urlsplit(signed.url).query.endswith(f"sig=temporary-fixture-{state['tokens']}")
        state["blob_calls"].append(url)
        if state["deny_once"]:
            state["deny_once"]=False
            return StubResponse(403)
        data=MTL if url.endswith("MTL.json") else AUX
        if url.endswith("MTL.json") and state["wrong_mtl"]:
            data=data.replace(b"20240628",b"20240701")
        md5=base64.b64encode(hashlib.md5(data,usedforsecurity=False).digest()).decode()
        return StubResponse(content=data,headers={"Content-MD5":md5,"Content-Length":str(len(data)),"ETag":'"not-a-digest"'})
    monkeypatch.setattr(HttpClient,"request",request)
    return state


def request_batch(client, out):
    area=client.post("aoi",{"coordinates":BBOX})["geometry"]
    criteria={"start":"2024-06-11","end":"2024-06-11","max_cloud":20}
    base={"geometry":area,"profile":"landsat-pc-l2"}
    search=client.wait(client.post("search",{**base,**criteria})["job_id"])["result"]
    return {**base,"criteria":criteria,"query_id":search["query"]["id"],"scene_keys":[LANDSAT],"output_dir":str(out),
            "options":{"mode":"assets","assets":["ang","mtl.json"],"transport":"pc-public"}}


def test_multifile_job_partial_success_then_safe_retry(client, pc_provider, tmp_path):
    dest=tmp_path/"原始文件"
    request=request_batch(client,dest)
    pc_provider.update(deny_once=True, wrong_mtl=True)
    first=client.wait(client.post("download",request)["job_id"],"failed")
    assert "MTL" in first["error"] and pc_provider["tokens"] == 2
    assert len(list(dest.rglob("*.txt"))) == 1 and not list(dest.rglob("*.part"))
    assert not any(p.name.endswith(".json") and "MTL" in p.name for p in dest.rglob("*"))
    pc_provider["wrong_mtl"]=False
    second=client.wait(client.post("download",request)["job_id"])["result"]
    assert [x["status"] for x in second["files"]] == ["skipped","completed"]
    assert Path(second["files"][1]["path"]).read_bytes() == MTL
    assert pc_provider["blob_calls"].count(LP+"ANG.txt") == 2  # one 403, then success; no second-job download
    for log in dest.rglob("*.json"):
        assert "temporary-fixture" not in log.read_text(encoding="utf-8")


@pytest.mark.browser
def test_ui_selects_multiple_landsat_files_across_filter(server, pc_provider, tmp_path):
    import os
    import re
    pw=pytest.importorskip("playwright.sync_api")
    with pw.sync_playwright() as driver:
        browser=driver.chromium.launch(channel=os.environ.get("STARMAP_TEST_BROWSER","msedge"),headless=True)
        page=browser.new_page(viewport={"width":1360,"height":900})
        failures=[]
        page.on("pageerror",lambda e:failures.append(str(e)))
        page.route(re.compile(r"^https://"),lambda route:route.abort())
        page.goto(server.origin,wait_until="domcontentloaded")
        pw.expect(page.locator("#outputDirectory")).not_to_have_value("")
        page.locator("#sourceProfile").select_option("landsat-pc-l2")
        pw.expect(page.locator("#rawMode")).to_have_value("assets")
        pw.expect(page.locator("#cloudOptions")).to_be_visible()
        pw.expect(page.locator("#publicAuth")).to_be_visible()
        page.locator('[data-tab="coordinates"]').click()
        page.locator("#coordinates").fill(BBOX)
        page.locator("#applyCoordinates").click()
        page.locator("#startDate").fill("2024-06-11")
        page.locator("#endDate").fill("2024-06-11")
        page.locator("#searchScenes").click()
        pw.expect(page.locator("#scenesBody")).to_contain_text(LANDSAT_RELEASE)
        page.locator('#rawMultiAssets input[value="ang"]').check()
        page.locator("#rawFileFilter").fill("mtl.json")
        page.locator('#rawMultiAssets input[value="mtl.json"]').check()
        pw.expect(page.locator("#rawMultiCount")).to_contain_text("已选 2 个")
        page.locator("#outputDirectory").fill(str(tmp_path/"测试多选"))
        page.locator("#downloadSelected").click()
        pw.expect(page.locator("#planDetails")).to_contain_text("2 个")
        pw.expect(page.locator("#planDetails")).to_contain_text("Microsoft Planetary Computer")
        page.locator("#confirmDownload").click()
        pw.expect(page.locator("#jobStatus")).to_have_text("下载完成",timeout=12000)
        assert not failures and not pc_provider["wrong_mtl"]
        browser.close()
