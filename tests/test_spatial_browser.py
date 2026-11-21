"""Actual map clicks, imported multi-part SHP deletion and map/list agreement in Edge."""
from __future__ import annotations

import os
import re
import threading

import pytest
from shapely.geometry import box
from shapely.ops import unary_union

from core.geometry import save_aoi
from scripts.browser_flow import open_data_page
from conftest import D1, P1, P2, feature


def browser_page(driver, server):
    browser = driver.chromium.launch(channel=os.environ.get("STARMAP_TEST_BROWSER", "msedge"), headless=True)
    page = browser.new_page(viewport={"width": 1440, "height": 940})
    page.route(re.compile(r"^https://"), lambda route: route.abort())
    open_data_page(page, server.origin)
    return browser, page


def map_click(page, longitude, latitude):
    point = page.evaluate("([lon,lat]) => {const p=map.latLngToContainerPoint([lat,lon]); return {x:p.x,y:p.y};}", [longitude, latitude])
    frame = page.locator("#map").bounding_box()
    page.mouse.click(frame["x"] + point["x"], frame["y"] + point["y"])


@pytest.mark.browser
def test_persistent_leaf_footprints_select_overlapping_dates_and_download_one_mosaic(server, client, provider, tmp_path):
    pw = pytest.importorskip("playwright.sync_api")
    east = P2.replace("T50TMK", "T50TML")
    provider.catalogue = [feature(P1, D1, 1, box(116.350, 39.900, 116.3535, 39.905)),
                          feature(P2, "2024-06-05T03:16:50.631Z", 9, box(116.350, 39.900, 116.3535, 39.905)),
                          feature(east, "2024-06-05T03:16:35Z", 2, box(116.353, 39.900, 116.356, 39.905))]
    client.search("116.350,39.900,116.356,39.905", cloud=100)
    failures = []
    with pw.sync_playwright() as driver:
        browser, page = browser_page(driver, server)
        page.on("pageerror", lambda error: failures.append(str(error)))
        pw.expect(page.locator("#scenesBody tr")).to_have_count(3)
        pw.expect(page.locator("#coverageSummary")).to_contain_text("已选 2 块")
        pw.expect(page.locator("#coverageSummary")).to_contain_text("100.00%")
        pw.expect(page.locator("#coverageDates")).to_contain_text("2 个日期")
        assert page.evaluate("footprintLayer.getLayers().length") == 3
        assert page.evaluate("[...Spatial.layers.values()].every(g=>g.getLayers().every(l=>l.getLayers().every(p=>p.options.dashArray==='6,5')))")
        page.locator("#scenesBody tr").first.hover()
        page.locator("#sceneCount").hover()
        assert page.evaluate("footprintLayer.getLayers().length") == 3
        # An overlapping point exposes both dates instead of selecting an arbitrary top polygon.
        map_click(page, 116.351, 39.903)
        pw.expect(page.locator(".footprint-popup")).to_contain_text("此处覆盖 2 个产品")
        older = page.get_by_role("checkbox", name="地图选择 T50TMK 2024-06-05", exact=True)
        newer = page.get_by_role("checkbox", name="地图选择 T50TMK 2024-06-10", exact=True)
        older.check()
        newer.uncheck()
        selected = page.evaluate("[...state.selected]")
        assert set(selected) == {P2, east}
        assert page.locator(f'tr[data-key="{P2}"] input').is_checked()
        assert not page.locator(f'tr[data-key="{P1}"] input').is_checked()
        pw.expect(page.locator("#coverageDates")).to_contain_text("1 个日期")
        page.locator(".leaflet-popup-close-button").click()
        page.locator("#coverageDetails summary").click()
        page.locator("#coverageBlockList [data-move='1']").first.click()
        pw.expect(page.locator("#spatialPriority")).to_have_value("selection")
        page.locator("#coverageDetails summary").click()
        page.locator("#outputDirectory").fill(str(tmp_path / "地图区块拼接"))
        pw.expect(page.locator("#downloadSelected")).to_be_enabled()
        page.locator("#downloadSelected").click()
        pw.expect(page.locator("#planDetails")).to_contain_text("2 个区块 / 1 份区域输出")
        pw.expect(page.locator("#planDetails")).to_contain_text("2024-06-05")
        page.locator("#confirmDownload").click()
        pw.expect(page.locator("#jobStatus")).to_have_text("下载完成", timeout=20000)
        pw.expect(page.locator("#jobFiles .job-file")).to_have_count(1)
        assert {p["id"] for call in provider.process_calls for p in __import__("json").loads(
            re.search(r"^var allowed = (.*);$", call["json"]["evalscript"], re.M)[1])} == {P2.removesuffix(".SAFE"), east.removesuffix(".SAFE")}
        assert failures == []
        browser.close()


@pytest.mark.browser
def test_delete_imported_shp_while_editing_and_cancel_an_unfinished_drawing(server, client, tmp_path):
    pw = pytest.importorskip("playwright.sync_api")
    client.search(cloud=100)
    geometry = unary_union([box(116.35, 39.90, 116.355, 39.905).difference(box(116.351, 39.901, 116.352, 39.902)),
                            box(116.36, 39.90, 116.364, 39.904)])
    path = save_aoi(geometry, str(tmp_path / "带孔多地块.shp"))
    with pw.sync_playwright() as driver:
        browser, page = browser_page(driver, server)
        failures = []
        page.on("pageerror", lambda error: failures.append(str(error)))
        page.locator('[data-tab="import"]').click()
        page.locator("#importBoundary").click()
        page.locator("#pathDialogInput").fill(path)
        page.locator("#confirmPath").click()
        pw.expect(page.locator("#aoiName")).to_have_text("带孔多地块")
        assert page.evaluate("aoiLayer.getLayers().length") == 2
        page.locator("#editBoundary").click()
        assert page.evaluate("state.editing")
        page.locator("#mapDeleteAoi").click()
        pw.expect(page.locator("#aoiName")).to_have_text("尚未设置研究区")
        assert page.evaluate("aoiLayer.getLayers().length===0 && footprintLayer.getLayers().length===0 && !state.editing && editHandler===null && state.plan===null && state.query===null")
        assert client.get("bootstrap").json()["aoi"] is None
        pw.expect(page.locator("#coveragePanel")).to_be_hidden()
        pw.expect(page.locator("#searchScenes")).to_be_disabled()
        page.locator("#mapDrawPolygon").click()
        map_click(page, 116.355, 39.901)
        page.locator("#mapDeleteAoi").click()
        pw.expect(page.locator("#drawHint")).to_be_hidden()
        assert page.evaluate("drawHandler===null && state.aoi===null")
        # A fresh rectangle still works after both kinds of deletion.
        page.locator("#mapDrawRectangle").click()
        first = page.evaluate("map.latLngToContainerPoint([39.9,116.35])")
        second = page.evaluate("map.latLngToContainerPoint([39.904,116.354])")
        frame = page.locator("#map").bounding_box()
        page.mouse.move(frame["x"] + first["x"], frame["y"] + first["y"])
        page.mouse.down()
        page.mouse.move(frame["x"] + second["x"], frame["y"] + second["y"], steps=8)
        page.mouse.up()
        pw.expect(page.locator("#aoiName")).to_have_text("地图研究区")
        page.locator("#clearAoi").click()
        pw.expect(page.locator("#aoiName")).to_have_text("尚未设置研究区")
        assert failures == []
        browser.close()


@pytest.mark.browser
def test_clear_queued_during_a_slow_shp_import_cannot_be_restored_by_its_late_response(server, client, monkeypatch, tmp_path):
    pw = pytest.importorskip("playwright.sync_api")
    client.search(cloud=100)
    path = save_aoi(box(116.350, 39.900, 116.355, 39.905), str(tmp_path / "慢速边界.shp"))
    started, release = threading.Event(), threading.Event()
    original, calls = server.service.aoi, []

    def delayed(value):
        calls.append("import" if value.get("path") else "clear")
        if value.get("path"):
            started.set()
            assert release.wait(8)
        return original(value)

    monkeypatch.setattr(server.service, "aoi", delayed)
    with pw.sync_playwright() as driver:
        browser, page = browser_page(driver, server)
        try:
            page.locator('[data-tab="import"]').click()
            page.locator("#importBoundary").click()
            page.locator("#pathDialogInput").fill(path)
            page.locator("#confirmPath").click()
            assert started.wait(3)
            page.locator("#mapDeleteAoi").click()
            release.set()
            pw.expect(page.locator("#aoiName")).to_have_text("尚未设置研究区")
            assert calls == ["import", "clear"]
            assert client.get("bootstrap").json()["aoi"] is None
            assert page.evaluate("!state.aoiPending && state.scenes.length===0 && Spatial.layers.size===0")
        finally:
            release.set()
            browser.close()
