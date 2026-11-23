"""Actual Edge city/month controls and cancellation with an unavailable map."""
from __future__ import annotations

import os
import json
import threading
from pathlib import Path

import pytest

from conftest import feature
from core.http import check_cancel
from scripts.browser_flow import open_data_page
from test_monthly import DEC, JAN


def diagnostic(page, server, errors):
    state = page.evaluate("()=>({profile:state.profile,busy:state.busy,jobId:state.jobId,aoiPending:state.aoiPending,query:state.query,timeMode:$('timeMode').value,startMonth:$('startMonth').value,endMonth:$('endMonth').value,months:[...Monthly.groups.values()].map(g=>({month:g.month,count:g.selected.size,include:g.include})),estimate:$('downloadEstimate').textContent,toasts:$('toastContainer').textContent})")
    state["jobs"] = [{key: job.data.get(key) for key in ("kind", "status", "message", "error")} for job in list(server.service.jobs.values())]
    state["page_errors"] = errors
    return json.dumps(state, ensure_ascii=False)


@pytest.mark.browser
def test_full_city_month_picker_plan_cancel_resume_and_abandon(client, server, provider, tmp_path, monkeypatch):
    pw = pytest.importorskip("playwright.sync_api")
    client.post("connect", {"client_id": "mock-client", "client_secret": "mock-secret"})
    provider.catalogue = [feature(DEC, "2024-12-20T03:16:50Z", cloud=20), feature(JAN, "2025-01-10T03:16:50Z", cloud=1)]
    errors = []
    with pw.sync_playwright() as driver:
        browser = driver.chromium.launch(channel=os.environ.get("STARMAP_TEST_BROWSER", "msedge"), headless=True)
        page = browser.new_page(viewport={"width": 1366, "height": 768})
        page.on("pageerror", lambda error: errors.append(str(error)))
        open_data_page(page, server.origin)
        pw.expect(page.locator("#cityChoice")).to_have_value("110000")
        page.locator("#applyCity").click()
        pw.expect(page.locator("#aoiName")).to_have_text("北京市")
        assert page.evaluate("state.aoi.vertices") > 14000
        assert 16000 < page.evaluate("state.aoi.area_km2") < 17000
        pw.expect(page.locator("#timeMode")).to_have_value("monthly")
        page.locator("#startMonth").fill("2024-12")
        page.locator("#endMonth").fill("2025-02")
        page.locator("#resolution").select_option("60")
        page.locator("#outputDirectory").fill(str(tmp_path / "city-download"))
        page.locator("#searchScenes").click()
        pw.expect(page.locator("#monthlyResultTabs [data-month]")).to_have_count(3)
        try:
            page.wait_for_function("()=>!state.busy&&state.plan?.kind==='monthly'", timeout=15000)
        except Exception:
            pytest.fail(diagnostic(page, server, errors))
        planning_started, release_plan = threading.Event(), threading.Event()
        actual_plan = server.service._plan_source
        def delayed_plan(*args, cancel=None, **kwargs):
            if not planning_started.is_set():
                planning_started.set()
                assert release_plan.wait(5)
            return actual_plan(*args, cancel=cancel, **kwargs)
        monkeypatch.setattr(server.service, "_plan_source", delayed_plan)
        page.evaluate("schedulePlan()")
        assert planning_started.wait(3)
        page.locator('#aoiTabs [data-tab="import"]').click()
        pw.expect(page.locator("#tab-import")).to_be_visible()
        page.locator('#aoiTabs [data-tab="city"]').click()
        # A late selection/render callback must queue the newest plan, instead
        # of invalidating the active preview and leaving Download disabled.
        page.evaluate("schedulePlan()")
        release_plan.set()
        page.wait_for_function("()=>!state.busy&&state.plan?.kind==='monthly'", timeout=15000)
        page.reload()
        page.wait_for_function("()=>!state.busy&&state.plan?.kind==='monthly'", timeout=15000)
        pw.expect(page.locator("#monthlyFields")).to_be_visible()
        pw.expect(page.locator("#dateFields")).to_be_hidden()
        pw.expect(page.locator("#companionSection")).to_be_hidden()
        page.locator("#resolution").select_option("60")
        page.wait_for_function("()=>!state.busy&&state.plan?.months[0]?.plan?.resolution===60", timeout=15000)
        page.locator("#monthResultSelect").select_option("2025-01")
        pw.expect(page.locator("#scenesBody")).to_contain_text("2025-01-10")
        page.locator('[data-include-month="2025-01"]').uncheck()
        page.wait_for_function("()=>!state.busy&&state.plan?.output_count===1", timeout=15000)
        page.locator("#downloadSelected").click()
        pw.expect(page.locator("#planDetails")).to_contain_text("北京市")
        pw.expect(page.locator("#planDetails")).to_contain_text("2024-12")
        pw.expect(page.locator("#planDetails")).to_contain_text("2025-02")
        pw.expect(page.locator("#planDetails")).to_contain_text("未选择，不生成文件")
        provider.block_number = 1
        page.locator("#confirmDownload").click()
        page.wait_for_function("()=>state.jobId && state.busy")
        assert provider.trap_started.wait(20), diagnostic(page, server, errors)
        page.evaluate("document.querySelector('.map-panel').hidden=true")
        pw.expect(page.locator("#taskbar")).to_be_visible()
        pw.expect(page.locator("#cancelTaskbar")).to_be_in_viewport(ratio=1)
        page.locator("#cancelTaskbar").click()
        pw.expect(page.locator("#jobStatus")).to_have_text("已取消", timeout=15000)
        page.wait_for_function("()=>!state.busy", timeout=15000)
        pw.expect(page.locator("[data-retry-download]")).to_have_count(1)
        pw.expect(page.locator("[data-abandon-download]")).to_have_count(1)
        provider.block_number = 2
        provider.trap_started.clear()
        page.locator("[data-retry-download]").click()
        page.wait_for_function("()=>state.jobId && state.busy")
        pw.expect(page.locator("#cancelTaskbar")).to_be_enabled()
        page.locator("#cancelTaskbar").click()
        pw.expect(page.locator("#jobStatus")).to_have_text("已取消", timeout=15000)
        page.wait_for_function("()=>!state.busy", timeout=15000)
        page.locator("[data-abandon-download]").click()
        pw.expect(page.locator("#resumeDownloads")).to_be_hidden()
        assert server.service.download_tasks.summaries() == []
        assert list((tmp_path / "city-download").rglob("研究区.shp"))
        assert not errors, errors
        browser.close()


@pytest.mark.browser
def test_map_initialization_failure_keeps_city_accounts_search_and_cancel_available(server, monkeypatch):
    pw = pytest.importorskip("playwright.sync_api")
    started = threading.Event()
    def search(*args, cancel=None, **kwargs):
        started.set()
        cancel.wait(8)
        check_cancel(cancel)
        return []
    monkeypatch.setattr(server.service, "_search_profile", search)
    errors = []
    with pw.sync_playwright() as driver:
        browser = driver.chromium.launch(channel=os.environ.get("STARMAP_TEST_BROWSER", "msedge"), headless=True)
        page = browser.new_page(viewport={"width": 1366, "height": 768})
        page.add_init_script("window.ResizeObserver=class{observe(){throw new Error('verification map failure')}}")
        page.on("pageerror", lambda error: errors.append(str(error)))
        open_data_page(page, server.origin)
        assert page.evaluate("map") is None
        page.locator("#sourceProfile").select_option("s2-earth-search")
        page.locator("#applyCity").click()
        pw.expect(page.locator("#aoiName")).to_have_text("北京市")
        assert page.evaluate("state.aoi.vertices") > 14000
        page.locator("#manageAccounts").click()
        pw.expect(page.locator("#accountsDialog")).to_be_visible()
        page.locator('#accountsDialog .modal-footer [data-close]').click()
        page.locator("#searchScenes").click()
        assert started.wait(5), diagnostic(page, server, errors)
        pw.expect(page.locator("#cancelTaskbar")).to_be_enabled()
        page.locator("#cancelTaskbar").click()
        pw.expect(page.locator("#scenesLoading")).to_be_hidden(timeout=12000)
        pw.expect(page.locator("#searchScenes")).to_be_enabled()
        assert not errors, errors
        browser.close()
