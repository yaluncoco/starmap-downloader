"""Real map drawing, upfront accounts and a shared Sentinel-2/GEDI download."""
from __future__ import annotations

import csv
import json
import os
import re
from pathlib import Path

from scripts.browser_flow import open_data_page

import pytest
import rasterio
from shapely.geometry import mapping, shape

from core.geometry import load_aoi, save_aoi
from test_batch import DATES, S2_PUBLIC, TOKEN, batch_setup


def screenshot(page, suffix):
    target = os.environ.get("STARMAP_BATCH_SCREENSHOT")
    if target:
        base = Path(target)
        base.parent.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=base.with_name(base.stem + "-" + suffix + ".png"), full_page=True)


@pytest.mark.browser
def test_upfront_login_draw_shp_and_download_aligned_batch(server, batch_setup, tmp_path):
    pw = pytest.importorskip("playwright.sync_api")
    errors = []
    with pw.sync_playwright() as driver:
        browser = driver.chromium.launch(channel=os.environ.get("STARMAP_TEST_BROWSER", "msedge"), headless=True)
        page = browser.new_page(viewport={"width": 1366, "height": 768})
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.route(re.compile(r"^https://"), lambda route: route.abort())
        open_data_page(page, server.origin)
        pw.expect(page.locator("#sourceProfile")).to_have_value("s2-process")
        page.locator("#manageAccounts").click()
        page.locator('[data-account="earthdata"]').click()
        pw.expect(page.locator("#earthdataConnect")).to_be_in_viewport(ratio=1)
        assert page.evaluate("state.aoi") is None
        page.locator("#earthdataToken").fill(TOKEN)
        page.locator("#earthdataConnect").click()
        pw.expect(page.locator("#earthdataConnectedPanel")).to_be_visible()
        pw.expect(page.locator("#earthdataToken")).to_have_value("")
        pw.expect(page.locator("#sourceProfile")).to_have_value("s2-process")
        screenshot(page, "login")
        page.locator('#accountsDialog .modal-footer [data-close]').click()
        pw.expect(page.locator("#accountsDialog")).to_be_hidden()
        page.locator("#sourceProfile").select_option(S2_PUBLIC)
        page.locator("#companionPicker summary").click()
        page.locator("#companionFilter").fill("GEDI")
        page.locator('[data-companion="gedi-earthdata"]').check()
        root = page.locator('[data-source-settings="gedi-earthdata"]')
        pw.expect(root.locator('[data-alignment]')).to_have_value("primary")
        for field in root.locator('[data-array="fields"]').all():
            field.set_checked(field.input_value() in ("rh95", "rh98"))
        root.locator('[data-sample-reference]').check()
        page.locator("#imageMode").select_option("ndvi")
        page.locator("#resolution").select_option("20")
        bounds = batch_setup.geometry.bounds
        page.evaluate("bounds => map.fitBounds([[bounds[1],bounds[0]],[bounds[3],bounds[2]]], {padding:[70,60],maxZoom:18,animate:false})", list(bounds))
        page.locator('#aoiTabs [data-tab="map"]').click()
        page.locator("#drawRectangle").click()
        points = page.evaluate("bounds => [[bounds[3],bounds[0]],[bounds[1],bounds[2]]].map(ll => { const p=map.latLngToContainerPoint(ll); const box=$('map').getBoundingClientRect(); return [p.x+box.left,p.y+box.top]; })", list(bounds))
        screenshot(page, "before-draw")
        hits = page.evaluate("points=>points.map(([x,y])=>{const node=document.elementFromPoint(x,y);return {inside_map:$('map').contains(node),id:node?.id,className:node?.getAttribute('class')};})", points)
        assert all(hit["inside_map"] for hit in hits), {"points": points, "hits": hits}
        page.mouse.move(*points[0])
        page.mouse.down()
        page.mouse.move(*points[1], steps=12)
        page.mouse.up()
        pw.expect(page.locator("#aoiName")).to_have_text("地图研究区")
        drawn = page.evaluate("state.aoi.geometry")
        saved_boundary = tmp_path / "画出的共同研究区.shp"
        page.locator("#exportBoundary").click()
        page.locator("#saveBoundary").click()
        page.locator("#pathDialogInput").fill(str(saved_boundary))
        with page.expect_response(lambda response: response.url.endswith("/api/export-aoi")) as exported:
            page.locator("#confirmPath").click()
        assert exported.value.ok and load_aoi(saved_boundary).equals(shape(drawn))
        for name, value in DATES.items():
            page.locator("#" + name + "Date").fill(value)
        output = tmp_path / "联合下载结果"
        page.locator("#outputDirectory").fill(str(output))
        page.locator("#searchScenes").click()
        pw.expect(page.locator('#batchResultTabs [data-result-source]')).to_have_count(2)
        pw.expect(page.locator("#scenesBody tr")).to_have_count(1)
        page.locator('[data-result-source="gedi-earthdata"]').click()
        pw.expect(page.locator("#companionResults")).to_contain_text("2024-06-10")
        pw.expect(page.locator("#companionResults [data-companion-scene]")).to_be_checked()
        page.locator("#selectNone").click()
        pw.expect(page.locator('[data-result-source="gedi-earthdata"]')).to_contain_text("0/1")
        page.locator("#selectAll").click()
        pw.expect(page.locator('[data-result-source="gedi-earthdata"]')).to_contain_text("1/1")
        pw.expect(page.locator("#downloadSelected")).to_be_enabled()
        page.locator("#downloadSelected").click()
        pw.expect(page.locator("#planDetails")).to_contain_text("共同日期")
        pw.expect(page.locator("#planDetails")).to_contain_text("NDVI")
        pw.expect(page.locator("#planDetails")).to_contain_text("rh95")
        pw.expect(page.locator("#planDetails")).to_contain_text("足迹表格附带该影像像元值")
        pw.expect(page.locator("#confirmDownload")).to_be_in_viewport(ratio=1)
        screenshot(page, "plan")
        page.locator("#confirmDownload").click()
        pw.expect(page.locator("#jobStatus")).to_have_text("下载完成", timeout=15000)
        pw.expect(page.locator("#jobMessage")).to_contain_text("2 个来源完成")
        pw.expect(page.locator("#jobSources .job-source-row")).to_have_count(2)
        manifest = json.loads(next(output.rglob("联合下载记录_*.json")).read_text(encoding="utf-8"))
        image_path = Path(manifest["primary_reference"])
        metrics_path = Path(manifest["sources"][1]["results"][0]["folder"]) / "gedi_metrics.tif"
        with rasterio.open(image_path) as image, rasterio.open(metrics_path) as metrics:
            assert image.shape == metrics.shape and image.transform == metrics.transform and image.crs == metrics.crs
        csv_path = metrics_path.parent / "footprints.csv"
        with csv_path.open(encoding="utf-8-sig", newline="") as file:
            rows = list(csv.DictReader(file))
        assert rows and float(rows[0]["reference_band_1"]) == pytest.approx(1 / 3, abs=1e-6)
        assert manifest["shared_dates"] == DATES and shape(manifest["geometry"]).equals(shape(drawn))
        assert page.locator(".brand-mark img").get_attribute("src") == "assets/globe-orbit.svg"
        assert page.locator(".brand-mark img").evaluate("img => img.complete && img.naturalWidth > 0")
        screenshot(page, "completed")
        page.locator("#manageAccounts").click()
        pw.expect(page.locator("#earthdataConnectedPanel")).to_be_visible()
        page.locator('[data-account="cdse"]').click()
        pw.expect(page.locator("#rawAuth")).to_be_visible()
        page.locator('[data-account="oauth"]').click()
        pw.expect(page.locator("#clientId")).to_be_visible()
        page.locator('#accountsDialog .modal-footer [data-close]').click()
        page.locator("#sourceProfile").select_option("gedi-earthdata")
        pw.expect(page.locator("#gediAuth")).to_be_hidden()
        assert page.locator("#gediAuth").evaluate("element => !!element.closest('#accountsDialog')")
        assert not errors
        browser.close()


@pytest.mark.browser
def test_gedi_primary_import_shp_attach_optical_and_empty_result(server, batch_setup, tmp_path):
    pw = pytest.importorskip("playwright.sync_api")
    boundary = tmp_path / "含孔洞的共同区域.shp"
    save_aoi(batch_setup.geometry, boundary)
    errors = []
    with pw.sync_playwright() as driver:
        browser = driver.chromium.launch(channel=os.environ.get("STARMAP_TEST_BROWSER", "msedge"), headless=True)
        page = browser.new_page(viewport={"width": 1366, "height": 768})
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.route(re.compile(r"^https://"), lambda route: route.abort())
        open_data_page(page, server.origin)
        page.locator("#sourceProfile").select_option("gedi-earthdata")
        page.locator("#manageAccounts").click()
        page.locator("#earthdataToken").fill(TOKEN)
        page.locator("#earthdataConnect").click()
        pw.expect(page.locator("#earthdataConnectedPanel")).to_be_visible()
        page.locator('#accountsDialog .modal-footer [data-close]').click()
        page.locator('[data-tab="import"]').click()
        page.locator("#importBoundary").click()
        page.locator("#pathDialogInput").fill(str(boundary))
        page.locator("#confirmPath").click()
        pw.expect(page.locator("#aoiName")).to_have_text("含孔洞的共同区域")
        page.locator("#companionPicker summary").click()
        page.locator("#companionFilter").fill("公开区域裁剪")
        page.locator('[data-companion="s2-earth-search"]').check()
        for name, value in DATES.items():
            page.locator("#" + name + "Date").fill(value)
        page.locator("#outputDirectory").fill(str(tmp_path / "反向联合结果"))
        page.locator("#searchScenes").click()
        pw.expect(page.locator('#batchResultTabs [data-result-source]')).to_have_count(2)
        page.locator('[data-result-source="s2-earth-search"]').click()
        pw.expect(page.locator("#companionResults [data-companion-scene]")).to_be_checked()
        pw.expect(page.locator("#downloadSelected")).to_be_enabled()
        page.locator("#downloadSelected").click()
        page.locator("#confirmDownload").click()
        pw.expect(page.locator("#jobStatus")).to_have_text("下载完成", timeout=15000)
        pw.expect(page.locator("#jobMessage")).to_contain_text("2 个来源完成")
        batch_setup.public.features = []
        page.locator("#searchScenes").click()
        pw.expect(page.locator('[data-result-source="s2-earth-search"]')).to_contain_text("0/0")
        page.locator('[data-result-source="s2-earth-search"]').click()
        pw.expect(page.locator("#companionResults")).to_contain_text("没有匹配数据")
        pw.expect(page.locator("#downloadSelected")).to_be_enabled()
        page.locator("#downloadSelected").click()
        pw.expect(page.locator("#planDetails")).to_contain_text("记录为空")
        page.locator('#downloadDialog [data-close]').first.click()
        assert not errors
        browser.close()
