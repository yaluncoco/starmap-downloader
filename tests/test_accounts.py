"""Persist complete platform inputs with DPAPI and restore them across real services."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from conftest import AppClient, StubResponse, serve
from core.account_store import ACCOUNT_FILE, AccountStore, WindowsProtection
from core.earthdata_auth import EDL_TOKEN_URL
from core.errors import UserError
from core.http import HttpClient, TOKEN_URL

OAUTH = {"client_id": "account-test-client", "client_secret": "account-test-client-secret"}
ODATA = {"method": "account", "username": "account-test@example.test", "password": "account-test-password", "totp": "123456"}
S3 = {"access_key": "account_test_s3_key", "secret_key": "account-test-s3-private-secret"}
EARTHDATA = {"method": "account", "username": "account-test-earthdata-user", "password": "account-test-earthdata-password"}
EDL_TOKEN = "account-test-earthdata-token-20261120"


class AccountProvider:
    def __init__(self):
        self.calls = []
        self.refresh = ""
        self.counter = 0
        self.reject_oauth = False
        self.reject_refresh = False
        self.totp = ODATA["totp"]
        self.block_oauth = False
        self.block_grant = None
        self.started = threading.Event()
        self.release = threading.Event()

    def _raw_tokens(self):
        self.counter += 1
        self.refresh = "account-test-refresh-" + str(self.counter)
        return StubResponse(data={"access_token": "account-test-raw-access-" + str(self.counter),
                                  "refresh_token": self.refresh, "expires_in": 600})

    def request(self, method, url, **kwargs):
        self.calls.append((url, kwargs))
        if url == EDL_TOKEN_URL:
            assert kwargs["auth"] == (EARTHDATA["username"], EARTHDATA["password"])
            return StubResponse(data={"access_token": EDL_TOKEN})
        assert url == TOKEN_URL and method == "POST"
        data = kwargs["data"]
        if self.block_grant == data["grant_type"]:
            self.started.set()
            assert self.release.wait(5), "Authentication blocker was never released"
        if data["grant_type"] == "client_credentials":
            if self.block_oauth:
                self.started.set()
                assert self.release.wait(5), "Restore blocker was never released"
            assert data["client_id"] == OAUTH["client_id"] and data["client_secret"] == OAUTH["client_secret"]
            if self.reject_oauth:
                return StubResponse(status=401, data={"error": "invalid_client"})
            return StubResponse(data={"access_token": "account-test-oauth-access", "expires_in": 300})
        if data["grant_type"] == "refresh_token":
            if self.reject_refresh or data["refresh_token"] != self.refresh:
                return StubResponse(status=401, data={"error": "invalid_grant"})
            return self._raw_tokens()
        assert data["grant_type"] == "password" and data["client_id"] == "cdse-public"
        assert data["username"] == ODATA["username"] and data["password"] == ODATA["password"]
        if data.get("totp") != self.totp:
            return StubResponse(status=401, data={"error": "invalid_grant"})
        return self._raw_tokens()


@pytest.fixture
def account_provider(monkeypatch):
    provider = AccountProvider()
    monkeypatch.setattr(HttpClient, "request", lambda _, *args, **kwargs: provider.request(*args, **kwargs))
    return provider


def wait_accounts(client):
    deadline = time.monotonic() + 6
    while time.monotonic() < deadline:
        status = client.get("accounts").json()
        if not status["restoring"]:
            return status
        time.sleep(.02)
    pytest.fail("Account restoration did not finish")


def login_all(client):
    for route, value in [("connect", OAUTH), ("raw-connect", ODATA), ("s3-connect", S3), ("earthdata-connect", EARTHDATA)]:
        result = client.post(route, value)
        assert result["connected"] and result["saved"]


def assert_no_api_secrets(client):
    text = client.get("bootstrap").text + client.get("accounts").text
    for value in (OAUTH["client_secret"], ODATA["password"], S3["access_key"], S3["secret_key"], EARTHDATA["password"], EDL_TOKEN):
        assert value not in text


def test_all_four_accounts_restore_and_rotating_session_survives_restart(account_provider, tmp_path):
    config = tmp_path / "settings"
    with serve(config) as server:
        client = AppClient(server.origin)
        assert client.get("accounts").json()["login_required"]
        login_all(client)
        assert not client.get("accounts").json()["login_required"]
        assert_no_api_secrets(client)
        stored = (config / ACCOUNT_FILE).read_bytes()
        preferences = (config / "preferences.json").read_bytes()
        for value in (OAUTH["client_secret"], ODATA["password"], S3["secret_key"], EARTHDATA["password"], EDL_TOKEN, ODATA["totp"]):
            assert value.encode() not in stored and value.encode() not in preferences
        client.session.close()
    initial_refresh = account_provider.refresh
    with serve(config) as server:
        client = AppClient(server.origin)
        status = wait_accounts(client)
        assert all(value["connected"] and value["saved"] for value in status["providers"].values())
        assert not status["login_required"] and not status["storage_error"]
        assert_no_api_secrets(client)
        assert initial_refresh != account_provider.refresh
        assert AccountStore(config).get("odata")["refresh_token"] == account_provider.refresh
        server.service.raw_auth.token(force=True)
        assert AccountStore(config).get("odata")["refresh_token"] == account_provider.refresh
        client.session.close()
    current_refresh = account_provider.refresh
    with serve(config) as server:
        client = AppClient(server.origin)
        assert all(item["connected"] for item in wait_accounts(client)["providers"].values())
        assert account_provider.refresh != current_refresh
        client.session.close()


@pytest.mark.skipif(os.name != "nt", reason="Windows DPAPI is the product credential store")
def test_dpapi_roundtrip_in_a_second_python_process(tmp_path):
    config = tmp_path / "新用户登录信息"
    store = AccountStore(config)
    store.put("oauth", OAUTH)
    store.put("earthdata", {"method": "token", "token": EDL_TOKEN})
    code = "from core.account_store import AccountStore; import sys; s=AccountStore(sys.argv[1]); assert not s.load_error; assert s.get('oauth')['client_secret']=='account-test-client-secret'; assert s.get('earthdata')['token']=='account-test-earthdata-token-20261120'"
    result = subprocess.run([sys.executable, "-X", "utf8", "-c", code, str(config)], capture_output=True, timeout=15)
    assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")


def test_invalid_login_is_not_saved_and_unrelated_preferences_remain(account_provider, tmp_path):
    config = tmp_path / "settings"
    with serve(config) as server:
        client = AppClient(server.origin)
        client.post("preferences", {"output_dir": str(tmp_path / "existing outputs"), "password": "do-not-persist"})
        client.post("connect", {"client_id": OAUTH["client_id"]}, 400)
        assert not (config / ACCOUNT_FILE).exists()
        preferences = client.get("bootstrap").json()["preferences"]
        assert preferences["output_dir"].endswith("existing outputs") and "password" not in preferences
        assert client.get("accounts").json()["login_required"]
        client.session.close()


def test_logout_deletes_only_selected_account_and_clear_all_requires_login_again(account_provider, tmp_path):
    config = tmp_path / "settings"
    with serve(config) as server:
        client = AppClient(server.origin)
        login_all(client)
        assert client.post("earthdata-disconnect", {}) == {"connected": False, "saved": False}
        stored = AccountStore(config).snapshot()
        assert set(stored) == {"oauth", "odata", "s3"}
        assert not client.get("accounts").json()["providers"]["earthdata"]["saved"]
        client.session.close()
    with serve(config) as server:
        client = AppClient(server.origin)
        status = wait_accounts(client)
        assert not status["providers"]["earthdata"]["connected"]
        assert all(status["providers"][key]["connected"] for key in ("oauth", "odata", "s3"))
        assert not status["login_required"]
        assert client.post("accounts-clear", {})["login_required"]
        assert not (config / ACCOUNT_FILE).exists()
        assert_no_api_secrets(client)
        client.session.close()
    with serve(config) as server:
        client = AppClient(server.origin)
        status = wait_accounts(client)
        assert status["login_required"] and not status["has_saved_accounts"] and not status["has_connected_accounts"]
        client.session.close()


def test_failed_saved_login_keeps_other_accounts_and_can_reuse_saved_secret(account_provider, tmp_path):
    config = tmp_path / "settings"
    with serve(config) as server:
        client = AppClient(server.origin)
        login_all(client)
        client.session.close()
    account_provider.reject_oauth = True
    with serve(config) as server:
        client = AppClient(server.origin)
        status = wait_accounts(client)
        assert status["login_required"] and status["providers"]["oauth"]["error"]
        assert status["providers"]["oauth"]["saved"] and not status["providers"]["oauth"]["connected"]
        assert all(status["providers"][key]["connected"] for key in ("odata", "s3", "earthdata"))
        assert_no_api_secrets(client)
        account_provider.reject_oauth = False
        assert client.post("connect", {"client_id": OAUTH["client_id"], "client_secret": "", "use_saved": True})["saved"]
        assert not client.get("accounts").json()["login_required"]
        client.session.close()


def test_mfa_refresh_failure_requests_fresh_code_without_resaving_code(account_provider, tmp_path):
    config = tmp_path / "settings"
    with serve(config) as server:
        client = AppClient(server.origin)
        client.post("raw-connect", ODATA)
        client.session.close()
    account_provider.reject_refresh = True
    account_provider.totp = "654321"
    with serve(config) as server:
        client = AppClient(server.origin)
        status = wait_accounts(client)
        assert status["login_required"] and "两步验证码" in status["providers"]["odata"]["error"]
        assert client.post("raw-connect", {"method": "account", "username": ODATA["username"], "password": "", "totp": "654321", "use_saved": True})["saved"]
        stored = AccountStore(config).get("odata")
        assert stored["password"] == ODATA["password"] and stored["requires_totp"] is True and "totp" not in stored
        assert "654321" not in (config / ACCOUNT_FILE).read_bytes().decode("ascii", errors="ignore")
        client.session.close()


def test_saved_secret_cannot_be_reused_for_a_changed_identity(account_provider, tmp_path):
    with serve(tmp_path / "settings") as server:
        client = AppClient(server.origin)
        client.post("raw-connect", ODATA)
        client.post("raw-disconnect", {})
        # Recreate a saved account while leaving its session disconnected, as after restore failure.
        server.service.accounts.store.put("odata", {key: value for key, value in ODATA.items() if key != "totp"})
        result = client.post("raw-connect", {"method": "account", "username": "different@example.test", "password": "", "use_saved": True}, 400)
        assert "邮箱和密码" in result["error"]
        assert AccountStore(tmp_path / "settings").get("odata")["username"] == ODATA["username"]
        client.session.close()


def test_corrupt_encrypted_store_prompts_and_can_be_cleared(account_provider, tmp_path):
    config = tmp_path / "settings"
    config.mkdir()
    (config / ACCOUNT_FILE).write_bytes(b"damaged encrypted account file")
    with serve(config) as server:
        client = AppClient(server.origin)
        status = wait_accounts(client)
        assert status["login_required"] and status["storage_error"] and not status["has_connected_accounts"]
        assert client.post("accounts-clear", {})["storage_error"] == ""
        assert not (config / ACCOUNT_FILE).exists()
        assert client.post("connect", OAUTH)["saved"]
        client.session.close()


def test_restore_never_blocks_bootstrap_or_account_status_and_blocks_mutation(account_provider, tmp_path):
    config = tmp_path / "settings"
    with serve(config) as server:
        client = AppClient(server.origin)
        client.post("connect", OAUTH)
        client.session.close()
    account_provider.block_oauth = True
    with serve(config) as server:
        try:
            assert account_provider.started.wait(3)
            client = AppClient(server.origin)
            started = time.monotonic()
            assert client.get("accounts").json()["restoring"]
            assert client.get("bootstrap").json()["accounts"]["restoring"]
            assert time.monotonic() - started < 1
            client.post("accounts-clear", {}, 400)
            client.post("connect", OAUTH, 400)
        finally:
            account_provider.release.set()
        assert not wait_accounts(client)["login_required"]
        client.session.close()


def test_save_failure_is_visible_and_does_not_claim_a_persistent_login(account_provider, tmp_path, monkeypatch):
    with serve(tmp_path / "settings") as server:
        def fail(*_args):
            raise UserError("测试：保存文件夹不可写。")
        monkeypatch.setattr(server.service.accounts.store, "put", fail)
        client = AppClient(server.origin)
        result = client.post("connect", OAUTH)
        assert result["connected"] and not result["saved"] and result["save_error"]
        status = client.get("accounts").json()
        assert status["has_connected_accounts"] and not status["has_saved_accounts"] and status["storage_error"]
        client.session.close()


@pytest.mark.parametrize("provider,refresh", [("oauth", False), ("odata", False), ("oauth", True), ("odata", True)])
def test_status_and_bootstrap_stay_responsive_during_login_or_token_refresh(account_provider, tmp_path, provider, refresh):
    with serve(tmp_path / "settings") as server:
        client = AppClient(server.origin)
        route, value, manager = ("connect", OAUTH, server.service.auth) if provider == "oauth" else (
            "raw-connect", ODATA, server.service.raw_auth)
        if refresh:
            client.post(route, value)
        account_provider.block_grant = "client_credentials" if provider == "oauth" else "refresh_token" if refresh else "password"
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(manager.token, force=True) if refresh else pool.submit(client.post, route, value)
            try:
                assert account_provider.started.wait(3)
                started = time.monotonic()
                for endpoint in ("accounts", "bootstrap"):
                    response = client.session.get(f"{server.origin}/api/{endpoint}", headers=client.headers, timeout=.75)
                    assert response.status_code == 200
                    assert OAUTH["client_secret"] not in response.text and ODATA["password"] not in response.text
                assert time.monotonic() - started < 1
            finally:
                account_provider.release.set()
            pending.result(timeout=5)
        assert client.get("accounts").json()["providers"][provider]["connected"]
        client.session.close()


def test_a_slow_saved_provider_does_not_delay_independent_account_restoration(account_provider, tmp_path):
    config = tmp_path / "settings"
    with serve(config) as server:
        client = AppClient(server.origin)
        login_all(client)
        client.session.close()
    account_provider.block_oauth = True
    with serve(config) as server:
        client = AppClient(server.origin)
        try:
            assert account_provider.started.wait(3)
            deadline = time.monotonic() + 1
            while time.monotonic() < deadline:
                status = client.get("accounts").json()
                if all(status["providers"][name]["connected"] for name in ("odata", "s3", "earthdata")):
                    break
                time.sleep(.02)
            else:
                pytest.fail("One slow provider prevented independent saved accounts from restoring")
            assert status["restoring"] and not status["providers"]["oauth"]["connected"]
        finally:
            account_provider.release.set()
        assert not wait_accounts(client)["login_required"]
        assert_no_api_secrets(client)
        client.session.close()


def test_status_reads_allowlisted_metadata_without_copying_saved_secrets(account_provider, tmp_path, monkeypatch):
    with serve(tmp_path / "settings") as server:
        client = AppClient(server.origin)
        login_all(client)

        def reject_snapshot():
            raise AssertionError("Display status must not copy the saved credential payload")

        monkeypatch.setattr(server.service.accounts.store, "snapshot", reject_snapshot)
        assert_no_api_secrets(client)
        status = client.get("accounts").json()
        assert all(value["saved"] and value["connected"] for value in status["providers"].values())
        assert status["providers"]["odata"]["identifier"] == ODATA["username"]
        client.session.close()


def test_failed_atomic_replace_preserves_saved_accounts_and_cleans_temporary_file(tmp_path, monkeypatch):
    store = AccountStore(tmp_path)
    store.put("oauth", OAUTH)
    before = store.path.read_bytes()

    def fail_replace(*_args):
        raise OSError("Synthetic locked file")

    monkeypatch.setattr(os, "replace", fail_replace)
    with pytest.raises(UserError, match="无法更新"):
        store.put("s3", S3)
    assert store.path.read_bytes() == before
    assert set(store.snapshot()) == {"oauth"}
    assert AccountStore(tmp_path).get("oauth") == OAUTH
    assert not list(tmp_path.glob(ACCOUNT_FILE + ".*.tmp"))


def test_deeply_nested_encrypted_json_is_recoverable_without_crashing_startup(tmp_path):
    protection = WindowsProtection()
    data = b"[" * 2000 + b"0" + b"]" * 2000
    (tmp_path / ACCOUNT_FILE).write_bytes(b"STARMAP-ACCOUNTS-1\n" + protection.protect(data))
    store = AccountStore(tmp_path)
    assert store.load_error and not store.snapshot()
    store.clear()
    assert not (tmp_path / ACCOUNT_FILE).exists()
