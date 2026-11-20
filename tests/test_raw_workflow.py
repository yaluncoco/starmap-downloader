"""Mock-CDSE integration, including authorization separation and byte-level failure cases."""
from __future__ import annotations

import hashlib
import io
import json
import threading
import zipfile
from pathlib import Path

import pytest
import requests
from shapely.geometry import box, mapping

from conftest import BBOX, P1, P2, StubResponse
from core.errors import UserError
from core.http import CATALOGUE_URL, TOKEN_URL, HttpClient, check_cancel
from core.native_catalogue import DOWNLOAD_ROOT, STAC_ROOT, download_asset, raw_item
from core.raw_download import _stream

KEY = P1.removesuffix(".SAFE")
UUID = "59fdc3e6-a362-4bbd-99d1-e7dd038f5052"
OTHER_UUID = "e23ac462-d702-47c3-b276-280fcf435fdc"
COLL = "sentinel-2-l2a"
PRODUCT_URL = f"{DOWNLOAD_ROOT}/Products({UUID})/$value"
ASSET = "manifest.safe"
ASSET_URL = f"{DOWNLOAD_ROOT}/Products({UUID})/Nodes({P1})/Nodes({ASSET})/$value"
XML = b"<?xml version='1.0'?><XFDU><metadata>fixture</metadata></XFDU>"
TOKEN = "temporary-test-token-only"


def bundle(root=P1, body=b"this payload is validated by the CRC"):
    content = io.BytesIO()
    with zipfile.ZipFile(content, "w") as archive:
        archive.writestr(root + "/manifest.safe", body)
    return content.getvalue()


def feature(legacy=False):
    href = ASSET_URL.replace("download.dataspace", "zipper.dataspace") if legacy else ASSET_URL
    return {"id": KEY, "collection": COLL, "geometry": mapping(box(116, 39, 117, 40)),
            "properties": {"datetime": "2024-06-10T03:05:31.024Z", "platform": "sentinel-2a", "eo:cloud_cover": 10},
            "assets": {"manifest": {"type": "application/xml", "alternate": {"https": {"href": href}}, "file:size": len(XML),
                                    "file:checksum": "1220" + hashlib.sha256(XML).hexdigest()}}}


class RawProvider:
    def __init__(self):
        self.calls = []
        self.identity = P1
        self.payload = bundle()
        self.raw_status = 200
        self.headers = {}
        self.asset = XML
        self.token_exchanges = []
        self.started = threading.Event()
        self.pause = False

    def request(self, method, url, *, cancel=None, **kwargs):
        check_cancel(cancel)
        headers = kwargs.get("headers", {})
        self.calls.append((method, url, headers))
        if url == STAC_ROOT + "/collections":
            return StubResponse(data={"collections": [{"id": COLL, "title": "Sentinel 2 L2A"}, {"id": "sentinel-1-grd", "title": "Radar"},
                                                       {"id": "cop-dem-glo-30-dged-cog", "title": "CopDEM"}]})
        if url == STAC_ROOT + "/search":
            assert "Authorization" not in headers
            assert kwargs["json"]["collections"] == [COLL]
            assert kwargs["json"]["limit"] == 100
            wrong_collection = {**feature(), "collection": "sentinel-2-l1c"}
            return StubResponse(data={"features": [wrong_collection, feature(legacy=True)]})
        if url == f"{CATALOGUE_URL}({UUID})":
            assert "Authorization" not in headers
            return StubResponse(data={"Id": UUID, "Name": self.identity, "Online": True})
        if url == TOKEN_URL:
            form = kwargs["data"]
            assert form["client_id"] == "cdse-public"
            self.token_exchanges.append(form["grant_type"])
            if form["grant_type"] == "password":
                assert form["totp"] == "123456"
                return StubResponse(data={"access_token": "access-from-test", "refresh_token": "refresh-for-test", "expires_in": 20})
            return StubResponse(data={"access_token": "refreshed-test-token", "refresh_token": "refresh-for-test", "expires_in": 300})
        assert url in (PRODUCT_URL, ASSET_URL), url
        assert headers["Authorization"].removeprefix("Bearer ") in (TOKEN, "refreshed-test-token")
        content = self.payload if url == PRODUCT_URL else self.asset
        if self.pause:
            started = self.started

            class Paused(StubResponse):
                def iter_content(self, size):
                    yield content[:12]
                    started.set()
                    assert cancel.wait(6)
                    yield content[12:]

            cls = Paused
        else:
            cls = StubResponse
        return cls(self.raw_status, content=content, headers={"Content-Length": str(len(content)), "ETag": '"test-version"', **self.headers})


@pytest.fixture
def raw_provider(monkeypatch):
    upstream = RawProvider()
    monkeypatch.setattr(HttpClient, "request", lambda _, *args, **kwargs: upstream.request(*args, **kwargs))
    return upstream


def search_raw(client):
    area = client.post("aoi", {"coordinates": BBOX})
    crit = {"start": "2024-06-10", "end": "2024-06-10", "collection": COLL, "radar_mode": ""}
    search = {"profile": "cdse-original", "geometry": area["geometry"]}
    job = client.wait(client.post("search", {**search, **crit})["job_id"])
    assert len(job["result"]["scenes"]) == 1
    return {**search, "query_id": job["result"]["query"]["id"], "scene_keys": [KEY], "criteria": crit, "options": {"mode": "original"}}


def auth_token(client):
    result = client.post("raw-connect", {"method": "token", "token": TOKEN})
    assert result == {"connected": True, "verified": False}


def test_raw_full_product_plan_download_and_tamper(client, raw_provider, tmp_path):
    collections = client.post("collections", {})["collections"]
    assert {s["id"] for s in collections} == {COLL, "sentinel-1-grd", "cop-dem-glo-30-dged-cog"}
    assert client.post("collections", {})["collections"] == collections
    assert sum(call[1] == STAC_ROOT + "/collections" for call in raw_provider.calls) == 1
    request = {**search_raw(client), "output_dir": str(tmp_path / "原始输出")}
    assert client.post("plan", request)["targets"][0]["uuid"] == UUID
    client.post("plan", {**request, "criteria": {**request["criteria"], "end": "2024-06-11"}}, status=400)
    client.post("plan", {**request, "criteria": {**request["criteria"], "collection": "sentinel-2-l1c"}}, status=400)
    client.post("plan", {**request, "profile": "dem-30"}, status=400)
    client.post("download", request, status=400)
    auth_token(client)
    assert not client.get("bootstrap").json()["connected"]
    saved = client.wait(client.post("download", request)["job_id"])["result"]
    path = Path(saved["files"][0]["path"])
    assert path.read_bytes() == raw_provider.payload
    record = json.loads(Path(saved["manifest"]).read_text(encoding="utf-8"))["items"][0]
    assert record["odata_name"] == P1 and record["uuid"] == UUID and record["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert record["provider_checksum_verified"] is None
    repeat = client.wait(client.post("download", request)["job_id"])["result"]
    assert repeat["files"][0]["status"] == "skipped"
    path.write_bytes(bytes(len(raw_provider.payload)))
    assert "完整性" in client.wait(client.post("download", request)["job_id"], "failed")["error"]
    assert sum(call[1] == PRODUCT_URL for call in raw_provider.calls) == 1
    assert TOKEN not in str(client.get("bootstrap").json())


def test_raw_asset_verifies_published_checksum_and_refreshes_account(client, raw_provider, tmp_path, server):
    request = {**search_raw(client), "output_dir": str(tmp_path / "asset"), "options": {"mode": "asset", "asset": "manifest"}}
    client.post("raw-connect", {"method": "account", "username": "me@example.test", "password": "mock-password", "totp": "123456"})
    plan = client.post("plan", request)
    assert plan["estimated_bytes"] == len(XML) and plan["targets"][0]["url"] == ASSET_URL
    saved = client.wait(client.post("download", request)["job_id"])["result"]
    assert Path(saved["files"][0]["path"]).read_bytes() == XML
    assert raw_provider.token_exchanges == ["password", "refresh_token"]
    assert "mock-password" not in server.service.pref_file.read_text(encoding="utf-8")
    record = json.loads(Path(saved["manifest"]).read_text(encoding="utf-8"))["items"][0]
    assert record["provider_checksum_verified"] == "SHA-256"
    raw_provider.asset = XML.replace(b"fixture", b"altered")
    failure = client.wait(client.post("download", {**request, "output_dir": str(tmp_path / "changed")})["job_id"], "failed")
    assert "SHA-256" in failure["error"]
    assert not list((tmp_path / "changed").rglob("*.safe"))


@pytest.mark.parametrize("problem,notice", [("uuid", "UUID"), ("wrong_product", "标识"), ("bad_crc", "ZIP"), ("html", "原始产品"),
                                              ("short", "中断"), ("denied", "权限"), ("expired", "令牌"), ("redirect", "重定向"),
                                              ("empty_container", "标识")])
def test_raw_fail_closed(client, raw_provider, tmp_path, problem, notice):
    request = {**search_raw(client), "output_dir": str(tmp_path / problem)}
    auth_token(client)
    if problem == "uuid":
        raw_provider.identity = P2
    elif problem == "wrong_product":
        raw_provider.payload = bundle(root=P2)
    elif problem == "empty_container":
        raw_provider.payload = bundle(body=b"")
    elif problem == "bad_crc":
        raw_provider.payload = raw_provider.payload.replace(b"this payload", b"this Payload")
    elif problem == "html":
        raw_provider.payload = b"<html>expired token</html>"
    elif problem == "short":
        raw_provider.headers = {"Content-Length": str(len(raw_provider.payload)+5)}
    elif problem == "denied":
        raw_provider.raw_status = 403
    elif problem == "expired":
        raw_provider.raw_status = 401
    else:
        raw_provider.raw_status = 302
        raw_provider.headers = {"Location": "https://other.example.test/file"}
    job = client.wait(client.post("download", request)["job_id"], "failed")
    assert notice in job["error"]
    assert not any(path.suffix in (".part", ".zip") for path in Path(request["output_dir"]).rglob("*"))
    assert not any("other.example" in call[1] for call in raw_provider.calls)
    if problem == "uuid":
        assert all(call[1] != PRODUCT_URL for call in raw_provider.calls)


def test_raw_cancel_clears_partial(client, raw_provider, tmp_path):
    request = {**search_raw(client), "output_dir": str(tmp_path / "cancel")}
    auth_token(client)
    raw_provider.pause = True
    job_id = client.post("download", request)["job_id"]
    assert raw_provider.started.wait(4)
    client.post("cancel", {"job_id": job_id})
    client.wait(job_id, "cancelled")
    assert not any(path.suffix in (".part", ".zip") for path in Path(request["output_dir"]).rglob("*"))


@pytest.mark.parametrize("problem", [None, "offset", "etag", "short_range", "short_body"])
def test_raw_interrupted_connection_resume_is_exact(monkeypatch, tmp_path, problem):
    data = bundle()
    calls = []
    class Broken(StubResponse):
        def iter_content(self, size):
            yield data[:64]
            raise requests.exceptions.ChunkedEncodingError()

    def request(*args):
        sent = args[3]
        calls.append(sent.copy())
        if len(calls) == 1:
            return Broken(headers={"ETag": '"object-v1"', "Content-Length": str(len(data))})
        start = 63 if problem == "offset" else 64
        end = len(data)-(2 if problem == "short_range" else 1)
        body = data[start:end+1]
        if problem == "short_body":
            body = body[:-2]
        return StubResponse(206, content=body, headers={"Content-Range": f"bytes {start}-{end}/{len(data)}",
                                                       "ETag": '"changed"' if problem == "etag" else '"object-v1"'})

    monkeypatch.setattr("core.raw_download._authorized_get", request)
    partial = tmp_path / "receive.part"
    parameters = (object(), object(), {"asset": None, "name": P1}, partial, None, lambda *args: None)
    if problem:
        with pytest.raises(UserError):
            _stream(*parameters)
    else:
        assert _stream(*parameters)["transfer_attempts"] == 2
        assert partial.read_bytes() == data
        assert calls[1]["If-Range"] == '"object-v1"' and calls[1]["Range"] == "bytes=64-"


def test_legacy_and_unrelated_asset_identity():
    row = raw_item(feature(legacy=True), box(116.35, 39.9, 116.355, 39.905), COLL)
    assert row["raw"]["assets"][0]["url"] == ASSET_URL
    assert not download_asset(ASSET_URL.replace("GRANULE", "..").replace(ASSET, "%2e%2e"))
    bad = feature()
    bad["assets"]["manifest"]["alternate"]["https"]["href"] = ASSET_URL.replace(P1, P2)
    assert not raw_item(bad, box(116.35,39.9,116.355,39.905), COLL)["raw"]["available"]


@pytest.mark.parametrize("pagination", ["valid", "repeat", "external"])
def test_official_pagination_preserves_exact_result_scope(monkeypatch, pagination):
    from core.native_catalogue import search_original
    calls = []
    second = {**feature(), "id": P2.removesuffix(".SAFE"), "assets": {}, "properties": {"datetime": "2024-06-05T10:00:00Z"}}
    def request(_self, method, url, **kwargs):
        assert method == "POST" and url == STAC_ROOT + "/search"
        body = kwargs["json"]
        calls.append(body)
        href = "https://unrelated.example.test/v1/search" if pagination == "external" else url
        link = {"rel": "next", "href": href, "method": "POST", "body": {"token": "cursor-confirmed"}, "merge": True}
        if "token" not in body:
            return StubResponse(data={"features": [feature()], "links": [link]})
        assert body["collections"] == [COLL] and body["datetime"].endswith("2024-06-11T00:00:00Z")
        return StubResponse(data={"features": [second, {**feature(), "collection": "sentinel-2-l1c"}],
                                  "links": [link] if pagination == "repeat" else []})
    monkeypatch.setattr(HttpClient, "request", request)
    args = (box(116.35, 39.9, 116.355, 39.905), COLL, "2024-06-05", "2024-06-10")
    if pagination == "valid":
        assert [r["key"] for r in search_original(*args)] == [KEY, second["id"]]
    else:
        with pytest.raises(UserError, match="分页"):
            search_original(*args)
    assert len(calls) == (1 if pagination == "external" else 2)
