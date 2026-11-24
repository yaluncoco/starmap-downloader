"""Slow background tiles must leave local login and task controls reachable."""
from __future__ import annotations

import os
import threading

import pytest

from conftest import serve
from core.map_tiles import MapTiles
from test_accounts import OAUTH, account_provider
from test_accounts_browser import page_for
from test_map_tiles import png


@pytest.mark.browser
def test_slow_tiles_leave_login_responsive_and_switching_cancels_the_old_queue(account_provider, monkeypatch, tmp_path):
    pw = pytest.importorskip("playwright.sync_api")
    release, started = threading.Event(), threading.Event()
    tile_requests, lock = [], threading.Lock()
    image = png()

    def tiles(_cache, kind, z, x, y):
        if kind == "satellite":
            with lock:
                tile_requests.append((z, x, y))
                if len(tile_requests) >= 3:
                    started.set()
            release.wait(15)
        return image, "image/png"

    monkeypatch.setattr(MapTiles, "get", tiles)
    errors, cancelled = [], []
    try:
        with serve(tmp_path / "settings") as server, pw.sync_playwright() as driver:
            browser = driver.chromium.launch(channel=os.environ.get("STARMAP_TEST_BROWSER", "msedge"), headless=True)
            page = page_for(browser, server.origin, errors)
            page.on("requestfailed", lambda request: cancelled.append(request.url))
            pw.expect(page.locator("#accountsDialog")).to_be_visible()
            page.locator("#accountComplete").click()
            page.locator('[data-basemap="satellite"]').click()
            assert started.wait(3), "The deliberately slow tile requests did not start"
            page.locator("#manageAccounts").click()
            page.locator('[data-account="oauth"]').click()
            page.locator("#clientId").fill(OAUTH["client_id"])
            page.locator("#clientSecret").fill(OAUTH["client_secret"])
            page.locator("#connectButton").click()
            pw.expect(page.locator("#connectedPanel")).to_be_visible(timeout=4000)
            assert not release.is_set(), "Login must complete while the map source is still stalled"
            with lock:
                assert len(tile_requests) == 3, "Tiles must leave browser connections for local API requests"
            page.locator("#accountComplete").click()
            page.locator('[data-basemap="standard"]').click()
            page.wait_for_function("()=>Object.values(baseLayer._tiles).length>0 && Object.values(baseLayer._tiles).every(t=>t.loaded)")
            pw.expect(page.locator("#mapOffline")).to_be_hidden()
            assert any("/map/tiles/satellite/" in url for url in cancelled)
            with lock:
                assert len(tile_requests) == 3, "Removed map tiles must not drain their queued requests"
            assert not errors
            browser.close()
    finally:
        release.set()
