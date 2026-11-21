"""Drive the actual desktop HTML in headless Edge, with no upstream network or quotas."""
from __future__ import annotations

import os
import re
from pathlib import Path

from scripts.browser_flow import open_data_page

import pytest
from shapely.geometry import box

from core.geometry import save_aoi
from conftest import TRIANGLE
from test_raw_workflow import raw_provider


@pytest.mark.browser
def test_desktop_ui_search_download_and_reuse(server, provider, tmp_path):
    pw = pytest.importorskip("playwright.sync_api")
    failures = []
    with pw.sync_playwright() as driver:
        browser = driver.chromium.launch(channel=os.environ.get("STARMAP_TEST_BROWSER", "msedge"), headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 960})
        page.on("pageerror", lambda error: failures.append(str(error)))
        # Prevent the smoke test from hitting third-party map tile servers.
        page.route(re.compile(r"^https://"), lambda route: route.abort())
        open_data_page(page, server.origin)
        page.locator("#manageAccounts").click()
        page.locator('[data-account="oauth"]').click()
        pw.expect(page.locator("#clientId")).to_be_editable()
        pw.expect(page.locator("#outputDirectory")).not_to_have_value("")
        pw.expect(page.locator("#imageMode")).to_have_value("research")
        pw.expect(page.locator("#searchScenes")).to_be_disabled()
        assert page.locator("#map .leaflet-pane").count() > 0
        page.locator("#clientId").fill("mock-client")
        page.locator("#clientSecret").fill("mock-secret")
        page.locator("#connectButton").click()
        pw.expect(page.locator("#connectedPanel")).to_be_visible()
        pw.expect(page.locator("#clientSecret")).to_have_value("")
        page.locator("#accountComplete").click()

        # The browser exposes the same import UX with a manual path when native dialogs are unavailable.
        shape_path = tmp_path / "本地研究区.shp"
        save_aoi(box(116.35, 39.9, 116.355, 39.905), shape_path)
        page.locator('[data-tab="import"]').click()
        page.locator("#importBoundary").click()
        page.locator("#pathDialogInput").fill(str(shape_path))
        page.locator("#confirmPath").click()
        pw.expect(page.locator("#aoiName")).to_have_text("本地研究区")
        page.locator('[data-tab="coordinates"]').click()
        page.locator("#coordinates").fill(TRIANGLE)
        page.locator("#applyCoordinates").click()
        pw.expect(page.locator("#aoiName")).to_have_text("坐标研究区")

        page.locator("#startDate").fill("2024-06-01")
        page.locator("#endDate").fill("2024-06-10")
        page.locator("#searchScenes").click()
        pw.expect(page.locator("#scenesBody tr")).to_have_count(1)
        pw.expect(page.locator("#scenesBody")).to_contain_text("03:16:50")
        page.locator("#downloadSelected").click()
        pw.expect(page.locator("#downloadDialog")).to_be_visible()
        pw.expect(page.locator("#planDetails")).to_contain_text("EPSG:32650")
        page.locator("#changePlanFolder").click()
        page.locator("#pathDialogInput").fill(str(tmp_path / "校验结果"))
        page.locator("#confirmPath").click()
        pw.expect(page.locator("#planPreviewFolder")).to_contain_text("校验结果")
        page.locator("#confirmDownload").click()
        pw.expect(page.locator("#jobStatus")).to_have_text("下载完成", timeout=12000)
        pw.expect(page.locator("#jobMessage")).to_contain_text("新保存 1 景")
        assert len(list((tmp_path / "校验结果").rglob("*.tif"))) == 4
        assert len(list((tmp_path / "校验结果").rglob("*.sources.json"))) == 1
        assert len(provider.process_calls) == 1

        # A completed queue stays visible without intercepting the next download button click.
        pw.expect(page.locator("#jobPanel")).to_be_visible()
        page.set_viewport_size({"width": 1100, "height": 740})
        page.locator("#downloadSelected").click(timeout=5000)
        pw.expect(page.locator("#downloadDialog")).to_be_visible()
        page.locator("#downloadDialog button[data-close]").first.click()

        page.reload(wait_until="domcontentloaded")
        pw.expect(page.locator("#scenesBody tr")).to_have_count(1)
        page.locator("#downloadSelected").click()
        page.locator("#confirmDownload").click()
        pw.expect(page.locator("#jobStatus")).to_have_text("下载完成", timeout=12000)
        pw.expect(page.locator("#jobMessage")).to_contain_text("已有 1 景")
        assert len(provider.process_calls) == 1
        assert not failures
        screenshot = os.environ.get("STARMAP_TEST_SCREENSHOT")
        if screenshot:
            page.screenshot(path=Path(screenshot), full_page=True)
        browser.close()


@pytest.mark.browser
def test_category_switch_and_raw_asset_workflow(server, raw_provider, tmp_path):
    from test_raw_workflow import COLL, KEY, TOKEN, XML
    pw = pytest.importorskip("playwright.sync_api")
    with pw.sync_playwright() as driver:
        browser = driver.chromium.launch(channel=os.environ.get("STARMAP_TEST_BROWSER", "msedge"), headless=True)
        page = browser.new_page(viewport={"width": 1360, "height": 900})
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.route(re.compile(r"^https://"), lambda route: route.abort())
        open_data_page(page, server.origin)
        pw.expect(page.locator("#outputDirectory")).not_to_have_value("")
        page.locator("#sourceProfile").select_option("dem-90")
        pw.expect(page.locator("#dateFields")).to_be_hidden()
        pw.expect(page.locator("#publicAuth")).to_be_visible()
        page.locator("#sourceProfile").select_option("s2-earth-search")
        pw.expect(page.locator("#startDate")).to_be_visible()
        pw.expect(page.locator("#clientSecret")).to_be_hidden()
        page.locator("#sourceProfile").select_option("cdse-original")
        pw.expect(page.locator("#collectionsStatus")).to_contain_text("3 个官方类别")
        page.locator("#collectionFilter").fill("Radar")
        pw.expect(page.locator("#rawCollection option")).to_have_count(2)
        page.locator("#rawCollection").select_option("sentinel-1-grd")
        pw.expect(page.locator("#radarMode")).to_be_visible()
        page.locator("#collectionFilter").fill("L2A")
        page.locator("#rawCollection").select_option(COLL)
        pw.expect(page.locator("#radarMode")).to_be_hidden()
        page.locator('[data-tab="coordinates"]').click()
        page.locator("#coordinates").fill(TRIANGLE)
        page.locator("#applyCoordinates").click()
        pw.expect(page.locator("#aoiName")).to_have_text("坐标研究区")
        page.locator("#startDate").fill("2024-06-10")
        page.locator("#endDate").fill("2024-06-10")
        page.locator("#searchScenes").click()
        pw.expect(page.locator("#scenesBody")).to_contain_text(KEY)
        pw.expect(page.locator("#downloadSelected")).to_be_disabled()
        page.locator("#rawMode").select_option("asset")
        page.locator("#rawAsset").select_option("manifest")
        page.locator("#manageAccounts").click()
        page.locator('[data-account="cdse"]').click()
        page.locator("#rawLoginMode").select_option("token")
        page.locator("#rawToken").fill(TOKEN)
        page.locator("#rawConnect").click()
        pw.expect(page.locator("#rawConnectedPanel")).to_be_visible()
        pw.expect(page.locator("#rawToken")).to_have_value("")
        page.locator("#accountComplete").click()
        page.locator("#outputDirectory").fill(str(tmp_path / "验证原始资产"))
        page.locator("#downloadSelected").click()
        pw.expect(page.locator("#planDetails")).to_contain_text("原产品范围")
        pw.expect(page.locator("#planDetails")).to_contain_text("manifest.safe")
        page.locator("#confirmDownload").click()
        pw.expect(page.locator("#jobStatus")).to_have_text("下载完成", timeout=15000)
        assert next((tmp_path / "验证原始资产").rglob("*.safe")).read_bytes() == XML
        page.locator("#sourceProfile").select_option("dem-30")
        pw.expect(page.locator("#scenesTableWrap")).to_be_hidden()
        assert not errors
        browser.close()


@pytest.mark.browser
def test_complete_safe_shortcut_preserves_region_dates_and_output(server, raw_provider, tmp_path):
    from test_raw_workflow import COLL
    pw = pytest.importorskip("playwright.sync_api")
    errors = []
    with pw.sync_playwright() as driver:
        browser = driver.chromium.launch(channel=os.environ.get("STARMAP_TEST_BROWSER", "msedge"), headless=True)
        page = browser.new_page(viewport={"width": 1360, "height": 900})
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.route(re.compile(r"^https://"), lambda route: route.abort())
        open_data_page(page, server.origin)
        page.locator("#sourceProfile").select_option("s2-earth-search")
        pw.expect(page.locator("#imageMode")).to_have_value("research")
        page.locator('[data-tab="coordinates"]').click()
        page.locator("#coordinates").fill(TRIANGLE)
        page.locator("#applyCoordinates").click()
        pw.expect(page.locator("#aoiName")).to_have_text("坐标研究区")
        geometry = page.evaluate("state.aoi.geometry")
        page.locator("#startDate").fill("2024-06-01")
        page.locator("#endDate").fill("2024-06-10")
        output = str(tmp_path / "原始产品对照")
        page.locator("#outputDirectory").fill(output)
        page.locator("#s2SafeShortcut").click()
        pw.expect(page.locator("#rawCollection")).to_have_value(COLL)
        pw.expect(page.locator("#rawMode")).to_have_value("original")
        pw.expect(page.locator("#rawTransport")).to_have_value("odata")
        pw.expect(page.locator("#rawWholeProduct")).to_contain_text("完整 Sentinel-2 SAFE")
        pw.expect(page.locator("#startDate")).to_have_value("2024-06-01")
        pw.expect(page.locator("#endDate")).to_have_value("2024-06-10")
        pw.expect(page.locator("#outputDirectory")).to_have_value(output)
        assert page.evaluate("state.aoi.geometry") == geometry
        assert not errors
        browser.close()
