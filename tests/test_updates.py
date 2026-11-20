from __future__ import annotations

import hashlib
import json
import os
import re
import threading
from collections import deque
from pathlib import Path

from scripts.browser_flow import open_data_page

import pytest
import requests
from requests.structures import CaseInsensitiveDict

from core import updates
from core.errors import Cancelled, UserError
from core.http import check_cancel

REPO = "Publisher/starmap-downloader"
LATEST = f"https://api.github.com/repos/{REPO}/releases/latest"
ASSET = f"https://api.github.com/repos/{REPO}/releases/assets/17"
CDN = "https://release-assets.githubusercontent.com/github-production-release-asset/file?key=synthetic"
TAG = "v99.10.0"
NAME = "StarmapDownloader-Setup-99.10.0-win-x64.exe"
PAYLOAD = b"MZ fixture only; not an executable"
SHA256 = hashlib.sha256(PAYLOAD).hexdigest()


def asset(**overrides):
    return {"id": 17, "size": len(PAYLOAD), "name": NAME, "state": "uploaded", "digest": f"sha256:{SHA256}",
            # Neither metadata URL is authoritative for outbound requests.
            "url": "https://example.invalid/unknown", "browser_download_url": "https://example.invalid/installer", **overrides}


def release(**overrides):
    return {"tag_name": TAG, "draft": False, "prerelease": False, "assets": [asset()],
            "html_url": "https://example.invalid/release", **overrides}


class Response:
    def __init__(self, status=200, *, headers=None, chunks=()):
        self.status_code = status
        self.headers = CaseInsensitiveDict(headers or {})
        self.chunks = chunks

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def iter_content(self, _):
        for chunk in self.chunks:
            if isinstance(chunk, Exception):
                raise chunk
            yield chunk


class Transport:
    def __init__(self):
        self.queue = deque()
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        assert self.queue, f"Unexpected GitHub request to {url}"
        expected, response = self.queue.popleft()
        assert url == expected
        assert kwargs["stream"] is True
        assert kwargs["allow_redirects"] is False
        assert all(value > 0 for value in kwargs["timeout"])
        assert "Authorization" not in kwargs["headers"]
        if isinstance(response, Exception):
            raise response
        return response

    def metadata(self, data=None):
        self.queue.append((LATEST, Response(chunks=[json.dumps(release() if data is None else data).encode()])))

    def respond(self, url, status=200, **kwargs):
        self.queue.append((url, Response(status, **kwargs)))


@pytest.fixture
def transport(monkeypatch):
    fake = Transport()
    monkeypatch.setattr(updates, "_anonymous_session", lambda: fake)
    yield fake
    assert not fake.queue


@pytest.mark.parametrize("text", ["v1", "1.2.3.4", "v1.2.3-beta.1", "1.2.3+test", "01.2.3", "v1.2.3\n", "１.２.３", "v1.2.99999999999", None])
def test_only_canonical_stable_versions(text):
    assert updates.version_key(text) is None


def test_versions_are_compared_numerically():
    assert updates.version_key("v1.10.0") > updates.version_key("1.9.99")
    assert updates.version_key("v2.0.0") > updates.version_key("1.99.99")
    assert updates.version_key("v1.0.0") == updates.version_key("1.0.0")
    assert updates.version_key("v1.1") == updates.version_key("1.1.0")
    assert updates.version_key("v1.1.1") > updates.version_key("v1.1")
    assert updates.version_key("v1.10") > updates.version_key("v1.9.7")


@pytest.mark.parametrize("value", ["https://github.com/a/b", "a/..", "../b", "-a/b", "a/b?x", "a/b/c", "a/b%20", "a@b/c", "a/b\n"])
def test_repository_is_owner_and_repo_not_a_url(value):
    with pytest.raises(ValueError, match="owner/repo"):
        updates.validate_repository(value)


def test_anonymous_session_ignores_environment_credentials():
    with updates._anonymous_session() as session:
        assert session.trust_env is False and session.verify is True
        assert session.auth is None and "Authorization" not in session.headers


def test_latest_uses_only_public_stable_release_endpoint(transport):
    transport.metadata()
    updater = updates.UpdateClient(REPO)
    latest = updater.latest()
    assert latest.tag == TAG and latest.version == "99.10.0"
    assert latest.installer == updates.Installer(17, NAME, len(PAYLOAD), SHA256)
    assert updater.release_page(latest) == f"https://github.com/{REPO}/releases/tag/{TAG}"
    assert transport.calls[0][1]["headers"]["Accept"] == "application/vnd.github+json"


def test_two_component_first_release_preserves_its_installer_name(transport):
    first_name = "StarmapDownloader-Setup-1.1-win-x64.exe"
    transport.metadata(release(tag_name="v1.1", assets=[asset(name=first_name)]))
    latest = updates.UpdateClient(REPO).latest()
    assert latest.tag == "v1.1" and latest.version == "1.1"
    assert latest.installer.name == first_name


@pytest.mark.parametrize("data", [release(draft=True), release(prerelease=True), release(tag_name="v99.10.0-rc.1"), release(tag_name="legacy")])
def test_ignores_draft_prerelease_and_incompatible_tags(transport, data):
    transport.metadata(data)
    assert updates.UpdateClient(REPO).latest() is None


@pytest.mark.parametrize("assets", [[], [asset(name="different-win-x64.exe")], [asset(name=NAME.replace("x64", "arm64"))],
                                     [asset(digest=None)], [asset(digest="sha256:12")], [asset(size=0)],
                                     [asset(size=True)], [asset(size=updates._MAX_INSTALLER + 1)],
                                     [asset(id="17")], [asset(state="new")], [asset(), asset()]])
def test_new_release_with_missing_or_unverifiable_installer_only_has_page(transport, assets):
    transport.metadata(release(assets=assets))
    assert updates.UpdateClient(REPO).latest().installer is None


def test_first_release_or_private_repo_returns_not_found(transport):
    transport.respond(LATEST, 404)
    assert updates.UpdateClient(REPO).latest() is None


@pytest.mark.parametrize("response, error", [
    (Response(429), "限制"), (Response(403, headers={"X-RateLimit-Remaining": "0"}), "限制"),
    (Response(403, headers={"Retry-After": "8"}), "限制"), (Response(403), "HTTP 403"),
    (Response(302, headers={"Location": "https://example.invalid/moved"}), "HTTP 302"),
    (requests.Timeout("private diagnostic"), "检查网络"),
    (Response(chunks=[b"<html>"]), "无法识别"), (Response(chunks=[b"[]"]), "无法识别"),
    (Response(chunks=[b" " * (updates._MAX_METADATA + 1)]), "过大"),
])
def test_lookup_errors_are_actionable_and_do_not_expose_request_data(transport, response, error):
    transport.queue.append((LATEST, response))
    with pytest.raises(UserError, match=error) as raised:
        updates.UpdateClient(REPO).latest()
    assert "private diagnostic" not in str(raised.value)


def fetch(tmp_path, *, cancel=None, progress=lambda *_: None):
    installer = updates.Installer(17, NAME, len(PAYLOAD), SHA256)
    return updates.UpdateClient(REPO).download(installer, tmp_path, cancel=cancel or threading.Event(), progress=progress)


@pytest.mark.parametrize("redirect", [False, True])
def test_download_verifies_size_and_digest_before_exe_is_visible(transport, tmp_path, redirect):
    if redirect:
        transport.respond(ASSET, 302, headers={"Location": CDN})
    url = CDN if redirect else ASSET
    transport.respond(url, chunks=[PAYLOAD[:8], PAYLOAD[8:]], headers={"Content-Length": str(len(PAYLOAD))})
    fractions = []

    def progress(_, fraction):
        assert not (tmp_path / NAME).exists()
        fractions.append(fraction)

    destination = fetch(tmp_path, progress=progress)
    assert destination == tmp_path / NAME and destination.read_bytes() == PAYLOAD
    assert fractions == sorted(fractions) and fractions[-1] > .9
    assert list(tmp_path.iterdir()) == [destination]
    assert transport.calls[0][1]["headers"]["Accept"] == "application/octet-stream"
    if os.name == "nt":
        assert b"ZoneId=3" in Path(f"{destination}:Zone.Identifier").read_bytes()


@pytest.mark.parametrize("where", [None, "http://release-assets.githubusercontent.com/a", "https://outside.invalid/a",
                                    "https://release-assets.githubusercontent.com.outside.invalid/a", "/relative/path",
                                    "https://release-assets.githubusercontent.com@outside.invalid/a",
                                    "https://outside.invalid@release-assets.githubusercontent.com/a",
                                    "https://release-assets.githubusercontent.com:444/a", "https://127.0.0.1/a",
                                    "https://release-assets.githubusercontent.com/a#fragment", " https://release-assets.githubusercontent.com/a"])
def test_download_refuses_redirect_outside_trusted_github_cdn(transport, tmp_path, where):
    transport.respond(ASSET, 302, headers={"Location": where} if where is not None else {})
    with pytest.raises(UserError, match="可信"):
        fetch(tmp_path)
    assert not any(tmp_path.iterdir())


def test_redirect_chain_is_bounded(transport, tmp_path):
    transport.respond(ASSET, 302, headers={"Location": CDN})
    for _ in range(3):
        transport.respond(CDN, 302, headers={"Location": CDN})
    with pytest.raises(UserError, match="次数过多"):
        fetch(tmp_path)
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("headers, chunks, error", [
    ({"Content-Length": "1"}, [PAYLOAD], "响应"),
    ({"Content-Encoding": "gzip"}, [PAYLOAD], "响应"),
    ({}, [PAYLOAD[:-1]], "校验"), ({}, [PAYLOAD + b"extra"], "大小"),
    ({}, [b"_" * len(PAYLOAD)], "SHA-256"),
    ({}, [PAYLOAD[:2], requests.ConnectionError("private signed location")], "检查网络"),
])
def test_bad_download_removes_partial_and_does_not_replace_previous_file(transport, tmp_path, headers, chunks, error):
    existing = tmp_path / NAME
    existing.write_bytes(b"existing file")
    transport.respond(ASSET, chunks=chunks, headers=headers)
    with pytest.raises(UserError, match=error) as raised:
        fetch(tmp_path)
    assert "private signed location" not in str(raised.value)
    assert existing.read_bytes() == b"existing file" and list(tmp_path.iterdir()) == [existing]


def test_cancelling_update_removes_partial(transport, tmp_path):
    cancel = threading.Event()
    transport.respond(ASSET, chunks=[PAYLOAD[:4], PAYLOAD[4:]])
    with pytest.raises(Cancelled):
        fetch(tmp_path, cancel=cancel, progress=lambda _, fraction: cancel.set() if fraction else None)
    assert not list(tmp_path.iterdir())


def test_http_update_flow_requires_checked_tag_and_explicit_action(client, server, transport, monkeypatch):
    current_version = "1.1"
    monkeypatch.setattr("core.service.VERSION", current_version)
    assert client.session.post(server.origin + "/api/check-update", json={}, timeout=4).status_code == 403
    assert client.post("check-update", {}) == {"status": "unconfigured", "current_version": current_version}
    assert "未配置" in client.post("open-update-release", {}, status=400)["error"]
    server.service.updates = updates.UpdateClient(REPO)
    assert client.get("bootstrap").json()["update_repository"] == REPO
    assert "重新检查" in client.post("download-update", {"tag": TAG}, status=400)["error"]
    opened = []
    monkeypatch.setattr("core.service.webbrowser.open", lambda url: opened.append(url) or True)
    assert client.post("open-update-release", {"url": "https://example.invalid/"})["opened"]
    assert opened == [f"https://github.com/{REPO}/releases"]
    transport.respond(LATEST, 404)
    assert client.post("check-update", {})["status"] == "not_published"
    transport.metadata(release(tag_name=f"v{current_version}"))
    current = client.post("check-update", {})
    assert current["status"] == "current" and not current["ahead"]
    transport.metadata(release(tag_name="v1.1.0"))
    equivalent = client.post("check-update", {})
    assert equivalent["status"] == "current" and not equivalent["ahead"]
    transport.metadata(release(tag_name="v1.0.99"))
    older = client.post("check-update", {})
    assert older["status"] == "current" and older["ahead"]
    transport.metadata(release(assets=[]))
    assert client.post("check-update", {})["installer_bytes"] is None
    assert "没有可校验" in client.post("download-update", {"tag": TAG}, status=400)["error"]
    transport.metadata()
    offer = client.post("check-update", {})
    assert offer == {"status": "available", "current_version": current_version, "latest_version": "99.10.0", "tag": TAG,
                     "installer_bytes": len(PAYLOAD)}
    assert not (server.service.config_dir / "updates").exists()
    assert client.post("open-update-release", {"tag": TAG, "url": "https://example.invalid/"})["opened"]
    assert opened[-1] == f"https://github.com/{REPO}/releases/tag/{TAG}"
    assert "重新检查" in client.post("download-update", {"tag": "v99.9.0"}, status=400)["error"]
    transport.respond(ASSET, chunks=[PAYLOAD])
    job = client.wait(client.post("download-update", {"tag": TAG, "url": "https://example.invalid/"})["job_id"])
    assert job["kind"] == "update" and "手动运行" in job["message"]
    assert Path(job["result"]["files"][0]["path"]).read_bytes() == PAYLOAD
    assert len(opened) == 2  # The verified installer was never launched.
    transport.respond(LATEST, 429)
    assert "限制" in client.post("check-update", {}, status=400)["error"]
    assert "重新检查" in client.post("download-update", {"tag": TAG}, status=400)["error"]


def test_installer_download_cannot_interrupt_an_imagery_job(client, server, transport):
    server.service.updates = updates.UpdateClient(REPO)
    finish = threading.Event()

    def imaging(job):
        finish.wait(5)
        check_cancel(job.cancel_event)
        return {"files": [], "plan": {}, "empty_scenes": []}

    active = server.service._start_job("download", imaging)
    try:
        transport.metadata()
        assert client.post("check-update", {})["status"] == "available"
        assert "已有任务" in client.post("download-update", {"tag": TAG}, status=400)["error"]
        assert [url for url, _ in transport.calls] == [LATEST]
    finally:
        finish.set()
        client.wait(active["job_id"])


@pytest.mark.browser
def test_desktop_update_dialog_no_automatic_actions_and_manual_verified_download(server, transport, monkeypatch):
    current_version = "1.1"
    monkeypatch.setattr("core.service.VERSION", current_version)
    pw = pytest.importorskip("playwright.sync_api")
    server.service.updates = updates.UpdateClient(REPO)
    opened = []
    monkeypatch.setattr("core.service.webbrowser.open", lambda url: opened.append(url) or True)
    failures = []
    with pw.sync_playwright() as driver:
        browser = driver.chromium.launch(channel=os.environ.get("STARMAP_TEST_BROWSER", "msedge"), headless=True)
        page = browser.new_page(viewport={"width": 1100, "height": 740})
        page.route(re.compile(r"^https://"), lambda route: route.abort())
        page.on("pageerror", lambda error: failures.append(str(error)))
        open_data_page(page, server.origin)
        button = page.get_by_role("button", name="检查更新", exact=True)
        pw.expect(button).to_be_enabled()
        assert transport.calls == [] and opened == []
        transport.queue.append((LATEST, requests.Timeout()))
        button.click()
        dialog = page.get_by_role("dialog", name="软件更新")
        pw.expect(dialog).to_be_visible()
        pw.expect(page.locator("#updateCurrent")).to_have_text(f"当前版本 v{current_version}")
        pw.expect(page.locator("#updateMessage")).to_contain_text("检查网络")
        pw.expect(page.locator("#downloadUpdate")).to_be_hidden()
        with page.expect_response("**/api/open-update-release"):
            page.locator("#openUpdateRelease").click()
        assert opened == [f"https://github.com/{REPO}/releases"]
        transport.metadata(release(tag_name=f"v{current_version}.0"))
        page.locator("#retryUpdate").click()
        pw.expect(page.locator("#updateMessage")).to_have_text("当前已是最新稳定版。")
        pw.expect(page.locator("#downloadUpdate")).to_be_hidden()
        transport.metadata(release(assets=[]))
        page.locator("#retryUpdate").click()
        pw.expect(page.locator("#updateMessage")).to_contain_text("没有可校验")
        pw.expect(page.locator("#downloadUpdate")).to_be_hidden()
        transport.metadata()
        page.locator("#retryUpdate").click()
        pw.expect(page.locator("#updateMessage")).to_contain_text("v99.10.0")
        pw.expect(page.locator("#downloadUpdate")).to_be_enabled()
        assert [url for url, _ in transport.calls] == [LATEST] * 4
        assert not (server.service.config_dir / "updates").exists()
        transport.respond(ASSET, chunks=[PAYLOAD])
        page.locator("#downloadUpdate").click()
        pw.expect(dialog).to_be_hidden()
        pw.expect(page.locator("#jobTitle")).to_have_text("软件更新")
        pw.expect(page.locator("#jobStatus")).to_have_text("校验通过")
        pw.expect(page.locator("#jobMessage")).to_contain_text("手动运行")
        pw.expect(page.locator("#openResultFolder")).to_be_visible()
        assert (server.service.config_dir / "updates" / NAME).read_bytes() == PAYLOAD
        assert opened == [f"https://github.com/{REPO}/releases"] and not failures
        browser.close()
