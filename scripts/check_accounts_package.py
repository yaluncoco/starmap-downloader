"""Verify packaged cold-start login, real DPAPI storage and native startup UI using synthetic inputs."""
from __future__ import annotations

import argparse
import base64
import ctypes
import hashlib
import json
import os
import re
import socket
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core.account_store import ACCOUNT_FILE, AccountStore

TOKEN = "synthetic-account-package-earthdata-token"
KEY = "synthetic_account_s3_key"
SECRET = "synthetic-account-package-s3-secret"


class API:
    def __init__(self, origin):
        self.origin = origin
        self.session = requests.Session()
        self.session.trust_env = False
        home = self.session.get(origin, timeout=5)
        token = re.search(r'<meta name="app-token" content="([^"<>]+)"', home.text)[1]
        self.session.headers["X-App-Token"] = token

    def get(self, route):
        response = self.session.get(self.origin + "/api/" + route, timeout=5)
        response.raise_for_status()
        return response.json()

    def post(self, route, value):
        response = self.session.post(self.origin + "/api/" + route, json=value, timeout=5)
        response.raise_for_status()
        return response.json()


@contextmanager
def running(executable, output, *, native=False):
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    environment = os.environ.copy()
    environment["LOCALAPPDATA"] = str(output / "AppData")
    environment["PYTHONUTF8"] = "1"
    argv = [str(executable), "--port", str(port)]
    if not native:
        argv.append("--server")
    process = subprocess.Popen(argv, cwd=output, env=environment)
    origin = f"http://127.0.0.1:{port}"
    try:
        with requests.Session() as session:
            session.trust_env = False
            for _ in range(160):
                if process.poll() is not None:
                    raise RuntimeError("Packaged application exited before startup")
                try:
                    if session.get(origin + "/health", timeout=.3).json()["ok"]:
                        break
                except requests.RequestException:
                    pass
                time.sleep(.1)
            else:
                raise RuntimeError("Packaged application did not start")
        api = API(origin)
        yield process, api
        api.session.close()
    finally:
        if process.poll() is None:
            if native:
                handle = window_handle(process.pid, timeout=1)
                if handle:
                    ctypes.windll.user32.PostMessageW(handle, 0x0010, 0, 0)
            try:
                process.wait(timeout=4 if native else .1)
            except subprocess.TimeoutExpired:
                process.terminate()
                process.wait(timeout=8)


def window_handle(pid, timeout=10):
    from ctypes import wintypes
    found = []
    user = ctypes.windll.user32
    callback_type = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    user.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user.IsWindowVisible.argtypes = [wintypes.HWND]
    user.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user.EnumWindows.argtypes = [callback_type, wintypes.LPARAM]

    def visit(handle, _):
        owner = ctypes.c_ulong()
        user.GetWindowThreadProcessId(handle, ctypes.byref(owner))
        if owner.value == pid and user.IsWindowVisible(handle):
            caption = ctypes.create_unicode_buffer(500)
            user.GetWindowTextW(handle, caption, len(caption))
            if "星图影像下载器" in caption.value:
                found.append(handle)
        return True

    callback = callback_type(visit)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        user.EnumWindows(callback, 0)
        if found:
            return found[0]
        time.sleep(.1)
    return None


def native_login(process, output):
    ctypes.windll.user32.SetProcessDPIAware()
    handle = window_handle(process.pid)
    assert handle, "Native Windows app window was not created"
    title = ctypes.create_unicode_buffer(500)
    ctypes.windll.user32.GetWindowTextW(handle, title, len(title))
    assert title.value == "星图影像下载器", f"Unexpected native window title: {title.value}"
    command = f'''
[Console]::OutputEncoding=[System.Text.UTF8Encoding]::new($false)
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
$taskRoot=[System.Windows.Automation.AutomationElement]::FromHandle([IntPtr]{handle})
$taskCondition=[System.Windows.Automation.PropertyCondition]::new([System.Windows.Automation.AutomationElement]::NameProperty,'暂不登录，选择数据')
$taskVisible=$false
for($taskCount=0;$taskCount -lt 50;$taskCount++){{
  $taskButton=$taskRoot.FindFirst([System.Windows.Automation.TreeScope]::Descendants,$taskCondition)
  if($null -ne $taskButton -and !$taskButton.Current.IsOffscreen){{$taskVisible=$true;break}}
  Start-Sleep -Milliseconds 100
}}
[pscustomobject]@{{LoginDialogVisible=$taskVisible}} | ConvertTo-Json -Compress
'''
    encoded = base64.b64encode(command.encode("utf-16-le")).decode("ascii")
    result = subprocess.run(["powershell.exe", "-NoProfile", "-EncodedCommand", encoded], capture_output=True, timeout=20)
    assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")
    evidence = json.loads(result.stdout.decode("utf-8-sig"))
    evidence["window_title"] = title.value
    from PIL import ImageGrab
    from ctypes import wintypes
    ctypes.windll.user32.SetForegroundWindow(handle)
    time.sleep(.5)
    rect = wintypes.RECT()
    ctypes.windll.user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    ctypes.windll.user32.GetWindowRect(handle, ctypes.byref(rect))
    ImageGrab.grab(bbox=(rect.left, rect.top, rect.right, rect.bottom)).save(output / "native-startup-login.png")
    assert evidence["LoginDialogVisible"], "Native WebView2 did not expose the startup login dialog"
    return evidence


def verify(executable, output):
    from playwright.sync_api import sync_playwright, expect
    ctypes.windll.user32.SetProcessDPIAware()
    output.mkdir(parents=True, exist_ok=True)
    config = output / "AppData/CopernicusDownloader"
    assert not (config / ACCOUNT_FILE).exists(), "Use a fresh verification output directory"
    errors = []
    with sync_playwright() as driver:
        browser = driver.chromium.launch(channel=os.environ.get("STARMAP_TEST_BROWSER", "msedge"), headless=True)

        def page_for(api):
            page = browser.new_page(viewport={"width": 1366, "height": 768})
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.route(re.compile(r"^https://"), lambda route: route.abort())
            page.goto(api.origin, wait_until="domcontentloaded")
            page.wait_for_selector('#manageAccounts[aria-busy="false"]')
            return page

        for run in range(2):
            with running(executable, output) as (_, api):
                bootstrap = api.get("bootstrap")
                version = bootstrap["version"]
                assert bootstrap["accounts"]["login_required"]
                page = page_for(api)
                expect(page.locator("#accountsDialog")).to_be_visible()
                assert page.locator('.sidebar input[type="password"]').count() == 0
                assert page.evaluate("['clientId','clientSecret','earthdataToken','earthdataUsername','earthdataPassword','rawUsername','rawPassword','rawTotp','rawToken','s3AccessKey','s3SecretKey'].every(id => !!$(id).closest('#accountsDialog'))")
                expect(page.locator("#accountComplete")).to_be_in_viewport(ratio=1)
                if run == 0:
                    page.screenshot(path=output / "packaged-first-launch.png", full_page=True)
                page.locator("#accountComplete").click()
                page.locator("#sourceProfile").select_option("s2-earth-search")
                expect(page.locator("#accountsDialog")).to_be_hidden()
                page.close()
                if run == 1:
                    assert api.post("earthdata-connect", {"method": "token", "token": TOKEN})["saved"]
                    assert api.post("s3-connect", {"access_key": KEY, "secret_key": SECRET})["saved"]
                    encrypted = (config / ACCOUNT_FILE).read_bytes()
                    assert all(value.encode() not in encrypted for value in (TOKEN, KEY, SECRET))
                    store = AccountStore(config)
                    assert store.get("earthdata")["token"] == TOKEN
                    assert store.get("s3") == {"access_key": KEY, "secret_key": SECRET}
        with running(executable, output) as (_, api):
            page = page_for(api)
            expect(page.locator("#accountsDialog")).to_be_hidden()
            assert page.evaluate("state.earthdataConnected && state.s3Connected")
            status = api.get("accounts")
            assert status["has_saved_accounts"] and not status["login_required"]
            assert all(value not in json.dumps(api.get("bootstrap")) + json.dumps(status) for value in (TOKEN, KEY, SECRET))
            page.locator("#manageAccounts").click()
            page.locator('[data-account="earthdata"]').click()
            expect(page.locator("#earthdataAuthState")).to_contain_text("并保存")
            page.screenshot(path=output / "packaged-restored-login.png", full_page=True)
            page.locator("#earthdataDisconnect").click()
            expect(page.locator("#earthdataAuthFields")).to_be_visible()
            assert set(AccountStore(config).snapshot()) == {"s3"}
            page.locator("#clearSavedAccounts").click()
            expect(page.locator("#accountComplete")).to_have_text("暂不登录，选择数据")
            assert not (config / ACCOUNT_FILE).exists()
            page.close()
        browser.close()
    with running(executable, output, native=True) as (process, api):
        assert api.get("accounts")["login_required"]
        native = native_login(process, output)
    assert not errors, errors
    report = {"version": version, "executable_sha256": hashlib.sha256(executable.read_bytes()).hexdigest(),
              "unlogged_two_cold_starts_open_login": True, "all_controls_in_account_modal": True,
              "sidebar_has_no_login_controls": True, "real_windows_dpapi_roundtrip": True,
              "cold_restart_restores_saved_accounts_without_popup": True,
              "delete_one_keeps_other_and_clear_all_deletes_storage": True, "native_startup": native,
              "real_platform_authentication": False,
              "note": "Synthetic Earthdata token and S3 keys are loaded locally; no real authentication or protected transfer was performed."}
    (output / "accounts-package-report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--exe", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    verify(args.exe.resolve(strict=True), args.output.resolve())
