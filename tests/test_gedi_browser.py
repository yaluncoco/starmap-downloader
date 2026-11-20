"""Exercise GEDI through the real desktop HTML and local HTTP service."""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

from scripts.browser_flow import open_data_page

import pytest
from shapely.geometry import mapping

from test_gedi import TOKEN, gedi_setup


@pytest.mark.browser
def test_gedi_ui_auth_parameters_reference_download_and_switch(server, gedi_setup, tmp_path):
    pw = pytest.importorskip("playwright.sync_api")
    _, reference, geometry = gedi_setup
    boundary = tmp_path / "GEDI研究区.geojson"
    boundary.write_text(json.dumps(mapping(geometry)), encoding="utf-8")
    errors = []
    with pw.sync_playwright() as driver:
        browser = driver.chromium.launch(channel=os.environ.get("STARMAP_TEST_BROWSER", "msedge"), headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 960})
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.route(re.compile(r"^https://"), lambda route: route.abort())
        open_data_page(page, server.origin)
        pw.expect(page.locator("#outputDirectory")).not_to_have_value("")
        page.locator("#sourceProfile").select_option("gedi-earthdata")
        pw.expect(page.locator("#gediCollection")).to_have_value("gedi02-a-003")
        pw.expect(page.locator("#gediAuth")).to_be_hidden()
        assert page.locator("#gediAuth").evaluate("element => !!element.closest('#accountsDialog')")
        pw.expect(page.locator("#cloudOptions")).to_be_hidden()
        pw.expect(page.locator("#processAuth")).to_be_hidden()
        assert page.locator("#gediCollection option").count() >= 13
        page.locator('[data-tab="import"]').click()
        page.locator("#importBoundary").click()
        page.locator("#pathDialogInput").fill(str(boundary))
        page.locator("#confirmPath").click()
        pw.expect(page.locator("#aoiName")).to_have_text("GEDI研究区")
        page.locator("#startDate").fill("2024-06-10")
        page.locator("#endDate").fill("2024-06-10")
        page.locator("#outputDirectory").fill(str(tmp_path / "界面GEDI结果"))
        for field in page.locator("#gediFields input").all():
            field.set_checked(field.input_value() in ("rh95", "rh98"))
        page.locator("#chooseGediReference").click()
        page.locator("#pathDialogInput").fill(str(reference))
        page.locator("#confirmPath").click()
        page.locator("#gediSampleReference").check()
        pw.expect(page.locator("#gediResolution")).to_be_disabled()
        page.locator("#searchScenes").click()
        pw.expect(page.locator("#scenesBody tr")).to_have_count(1)
        pw.expect(page.locator("#scenesBody")).to_contain_text("V003")
        pw.expect(page.locator("#downloadSelected")).to_be_disabled()
        page.locator("#manageAccounts").click()
        page.locator('[data-account="earthdata"]').click()
        page.locator("#earthdataToken").fill(TOKEN)
        page.locator("#earthdataConnect").click()
        pw.expect(page.locator("#earthdataConnectedPanel")).to_be_visible()
        pw.expect(page.locator("#earthdataToken")).to_have_value("")
        page.locator("#accountComplete").click()
        pw.expect(page.locator("#downloadSelected")).to_be_enabled()
        screenshot = os.environ.get("STARMAP_GEDI_SCREENSHOT")
        if screenshot:
            page.locator(".sidebar-scroll").evaluate("element => element.scrollTop = 0")
            screenshot_path = Path(screenshot)
            page.screenshot(path=screenshot_path.with_name(screenshot_path.stem + "-main.png"), full_page=True)
        page.locator("#downloadSelected").click()
        pw.expect(page.locator("#downloadDialog")).to_be_visible()
        pw.expect(page.locator("#planDetails")).to_contain_text("6 × 5")
        pw.expect(page.locator("#planDetails")).to_contain_text("EPSG:32650")
        pw.expect(page.locator("#planDetails")).to_contain_text("rh95")
        page.set_viewport_size({"width": 1366, "height": 768})
        pw.expect(page.locator("#confirmDownload")).to_be_in_viewport(ratio=1)
        if screenshot:
            page.screenshot(path=Path(screenshot), full_page=True)
        page.locator("#confirmDownload").click()
        pw.expect(page.locator("#jobStatus")).to_have_text("下载完成", timeout=15000)
        pw.expect(page.locator("#jobMessage")).to_contain_text("4 个足迹")
        assert len(list((tmp_path / "界面GEDI结果").rglob("gedi_metrics.tif"))) == 1
        page.locator("#gediCollection").select_option("gedi04-b-021")
        pw.expect(page.locator("#scenesTableWrap")).to_be_hidden()
        pw.expect(page.locator("#gediFootprintFields")).to_be_hidden()
        pw.expect(page.locator("#gediSampleField")).to_be_hidden()
        pw.expect(page.locator("#gediProductNote")).to_contain_text("2023-03-16")
        page.locator("#sourceProfile").select_option("s2-earth-search")
        pw.expect(page.locator("#gediOptions")).to_be_hidden()
        pw.expect(page.locator("#publicAuth")).to_be_visible()
        page.locator("#sourceProfile").select_option("gedi-earthdata")
        page.locator("#manageAccounts").click()
        page.locator('[data-account="earthdata"]').click()
        page.locator("#earthdataDisconnect").click()
        pw.expect(page.locator("#earthdataAuthFields")).to_be_visible()
        pw.expect(page.locator("#earthdataConnectedPanel")).to_be_hidden()
        assert not errors
        browser.close()
