"""The actual account modal is the only login UI and controls cold-start behavior."""
from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

from conftest import AppClient, serve
from test_accounts import OAUTH, ODATA, S3, EARTHDATA, account_provider, wait_accounts


def capture(page, suffix):
    target = os.environ.get("STARMAP_ACCOUNTS_SCREENSHOT")
    if target:
        prefix = Path(target)
        prefix.parent.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=prefix.with_name(prefix.stem + "-" + suffix + ".png"), full_page=True)


def page_for(browser, origin, errors):
    page = browser.new_page(viewport={"width": 1366, "height": 768})
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.route(re.compile(r"^https://"), lambda route: route.abort())
    page.goto(origin, wait_until="domcontentloaded")
    return page


@pytest.mark.browser
def test_fresh_and_unlogged_restarts_open_modal_and_every_login_control_stays_there(account_provider, tmp_path):
    pw = pytest.importorskip("playwright.sync_api")
    errors = []
    config = tmp_path / "settings"
    with pw.sync_playwright() as driver:
        browser = driver.chromium.launch(channel=os.environ.get("STARMAP_TEST_BROWSER", "msedge"), headless=True)
        for run in range(2):
            with serve(config) as server:
                page = page_for(browser, server.origin, errors)
                pw.expect(page.locator("#accountsDialog")).to_be_visible()
                pw.expect(page.locator("#accountsTitle")).to_have_text("登录账号")
                pw.expect(page.locator("#accountComplete")).to_have_text("暂不登录，选择数据")
                pw.expect(page.locator("#connectButton")).to_be_in_viewport(ratio=1)
                pw.expect(page.locator("#accountComplete")).to_be_in_viewport(ratio=1)
                assert page.locator('.sidebar input[type="password"]').count() == 0
                assert page.locator("#manageAccountsSidebar, #loginBeforeBatch, .sidebar #connectButton, .sidebar #rawConnect").count() == 0
                assert page.evaluate("['clientId','clientSecret','earthdataUsername','earthdataPassword','earthdataToken','rawUsername','rawPassword','rawTotp','rawToken','s3AccessKey','s3SecretKey'].every(id => !!$(id).closest('#accountsDialog'))")
                for name in ("earthdata", "cdse", "oauth"):
                    page.locator('[data-account="' + name + '"]').click()
                    pw.expect(page.locator("#accountComplete")).to_be_in_viewport(ratio=1)
                if run == 0:
                    capture(page, "fresh")
                page.locator("#accountComplete").click()
                page.locator("#sourceProfile").select_option("s2-earth-search")
                pw.expect(page.locator("#accountsDialog")).to_be_hidden()
                pw.expect(page.locator("#sourceProfile")).to_have_value("s2-earth-search")
                assert page.locator('.sidebar input[type="password"]').count() == 0
                assert page.evaluate("$('clientSecret').closest('#accountsDialog') !== null")
                if run == 0:
                    capture(page, "data")
                page.close()
        assert not errors
        browser.close()


@pytest.mark.browser
def test_complete_four_platform_logins_are_saved_and_cold_start_restores_without_popup(account_provider, tmp_path):
    pw = pytest.importorskip("playwright.sync_api")
    errors = []
    config = tmp_path / "settings"
    with pw.sync_playwright() as driver:
        browser = driver.chromium.launch(channel=os.environ.get("STARMAP_TEST_BROWSER", "msedge"), headless=True)
        with serve(config) as server:
            page = page_for(browser, server.origin, errors)
            pw.expect(page.locator("#accountsDialog")).to_be_visible()
            page.locator("#clientId").fill(OAUTH["client_id"])
            page.locator("#clientSecret").fill(OAUTH["client_secret"])
            page.locator("#connectButton").click()
            pw.expect(page.locator("#connectedPanel")).to_be_visible()
            pw.expect(page.locator("#oauthAuthState")).to_contain_text("并保存")
            page.locator('[data-account="earthdata"]').click()
            page.locator("#earthdataLoginMode").select_option("account")
            page.locator("#earthdataUsername").fill(EARTHDATA["username"])
            page.locator("#earthdataPassword").fill(EARTHDATA["password"])
            page.locator("#earthdataConnect").click()
            pw.expect(page.locator("#earthdataConnectedPanel")).to_be_visible()
            page.locator('[data-account="cdse"]').click()
            page.locator("#rawUsername").fill(ODATA["username"])
            page.locator("#rawPassword").fill(ODATA["password"])
            page.locator("#rawTotp").fill(ODATA["totp"])
            page.locator("#rawConnect").click()
            pw.expect(page.locator("#rawConnectedPanel")).to_be_visible()
            page.locator("#s3AccessKey").fill(S3["access_key"])
            page.locator("#s3SecretKey").fill(S3["secret_key"])
            page.locator("#s3Connect").click()
            pw.expect(page.locator("#s3ConnectedPanel")).to_be_visible()
            pw.expect(page.locator("#accountComplete")).to_be_in_viewport(ratio=1)
            for name in ("oauth", "earthdata", "cdse"):
                page.locator('[data-account="' + name + '"]').click()
                pw.expect(page.locator('[data-account="' + name + '"] small')).to_have_text("已连接")
            capture(page, "saved")
            page.locator("#accountComplete").click()
            assert page.evaluate("state.profile") == "s2-process" and page.evaluate("state.aoi") is None
            page.close()
        with serve(config) as server:
            page = page_for(browser, server.origin, errors)
            page.wait_for_selector('#manageAccounts[aria-busy="false"]')
            pw.expect(page.locator("#connectionStatus")).to_contain_text("API 已连接")
            pw.expect(page.locator("#accountsDialog")).to_be_hidden()
            assert page.evaluate("state.connected && state.rawConnected && state.s3Connected && state.earthdataConnected")
            assert page.evaluate("['clientSecret','earthdataPassword','earthdataToken','rawPassword','rawToken','rawTotp','s3SecretKey','s3AccessKey'].every(id => $(''+id).value === '')")
            page.locator("#manageAccounts").click()
            pw.expect(page.locator("#oauthAuthState")).to_contain_text("并保存")
            capture(page, "restored")
            page.locator("#clearSavedAccounts").click()
            pw.expect(page.locator("#accountComplete")).to_have_text("暂不登录，选择数据")
            assert page.evaluate("!Accounts.status.has_saved_accounts && !Accounts.status.has_connected_accounts")
            page.locator("#accountComplete").click()
            page.close()
        with serve(config) as server:
            page = page_for(browser, server.origin, errors)
            pw.expect(page.locator("#accountsDialog")).to_be_visible()
            pw.expect(page.locator("#clientId")).to_have_value("")
            page.close()
        assert not errors
        browser.close()


@pytest.mark.browser
def test_failed_restore_opens_correct_tab_and_reuses_saved_secret_without_disclosing_it(account_provider, tmp_path):
    pw = pytest.importorskip("playwright.sync_api")
    errors = []
    config = tmp_path / "settings"
    with serve(config) as server:
        client = AppClient(server.origin)
        client.post("connect", OAUTH)
        client.session.close()
    account_provider.reject_oauth = True
    with serve(config) as server, pw.sync_playwright() as driver:
        browser = driver.chromium.launch(channel=os.environ.get("STARMAP_TEST_BROWSER", "msedge"), headless=True)
        page = page_for(browser, server.origin, errors)
        pw.expect(page.locator("#accountsDialog")).to_be_visible()
        pw.expect(page.locator('[data-account="oauth"]')).to_have_attribute("aria-selected", "true")
        pw.expect(page.locator("#authError")).to_be_visible()
        pw.expect(page.locator("#clientId")).to_have_value(OAUTH["client_id"])
        pw.expect(page.locator("#clientSecret")).to_have_value("")
        pw.expect(page.locator("#clientSecret")).to_have_attribute("placeholder", "已保存，留空沿用；填写可更新")
        account_provider.reject_oauth = False
        page.locator("#connectButton").click()
        pw.expect(page.locator("#connectedPanel")).to_be_visible()
        pw.expect(page.locator("#authError")).to_be_hidden()
        page.locator("#accountComplete").click()
        assert not errors
        browser.close()


@pytest.mark.browser
def test_mfa_renewal_uses_saved_password_and_only_requests_a_current_code(account_provider, tmp_path):
    pw = pytest.importorskip("playwright.sync_api")
    errors = []
    config = tmp_path / "settings"
    with serve(config) as server:
        client = AppClient(server.origin)
        client.post("raw-connect", ODATA)
        client.session.close()
    account_provider.reject_refresh = True
    account_provider.totp = "654321"
    with serve(config) as server, pw.sync_playwright() as driver:
        browser = driver.chromium.launch(channel=os.environ.get("STARMAP_TEST_BROWSER", "msedge"), headless=True)
        page = page_for(browser, server.origin, errors)
        pw.expect(page.locator("#accountsDialog")).to_be_visible()
        pw.expect(page.locator('[data-account="cdse"]')).to_have_attribute("aria-selected", "true")
        pw.expect(page.locator("#rawAuthError")).to_contain_text("两步验证码")
        pw.expect(page.locator("#rawUsername")).to_have_value(ODATA["username"])
        pw.expect(page.locator("#rawPassword")).to_have_value("")
        page.locator("#rawTotp").fill("654321")
        page.locator("#rawConnect").click()
        pw.expect(page.locator("#rawConnectedPanel")).to_be_visible()
        pw.expect(page.locator("#rawTotp")).to_have_value("")
        assert not errors
        browser.close()
