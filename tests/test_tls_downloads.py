"""TLS EOF recovery preserves verification, byte identity and retry budgets."""
from __future__ import annotations

import json
import re
import socket
import ssl
import threading
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
import requests
from urllib3.exceptions import MaxRetryError, SSLError as UrllibSSLError

from conftest import StubResponse, archive_bytes
from core import slc_archive
from core.errors import Cancelled, TransientDownloadError, UserError
from core.http import HttpClient, tls_error_kind
from core.range_file import RangeFile
from core.raw_download import _stream
from core.process_request import fetch_tile
from test_catalogue_process import _payload, StubAuth
from test_slc import TAG, target as slc_target


def wrapped(error):
    return requests.exceptions.SSLError(MaxRetryError(None, "/private?signature=secret", UrllibSSLError(error)))


def eof():
    return wrapped(ssl.SSLEOFError(ssl.SSL_ERROR_EOF, "[SSL: UNEXPECTED_EOF_WHILE_READING] private diagnostic"))


@pytest.mark.parametrize("kind", ["certificate", "unknown"])
@pytest.mark.parametrize("stage", ["headers", "range-body", "original-body", "process-body", "slc-body"])
def test_fatal_tls_is_not_retried_or_exposed(monkeypatch, tmp_path, kind, stage):
    original = (ssl.SSLCertVerificationError(1, "certificate chain private diagnostic")
                if kind == "certificate" else ssl.SSLError(1, "private diagnostic"))
    failure, calls, responses = wrapped(original), [], []
    payload = b"immutable source bytes"

    class Broken(StubResponse):
        def iter_content(self, size):
            yield payload[:7]
            raise failure

    http = HttpClient()

    def request(*args, **kwargs):
        calls.append(kwargs)
        assert kwargs.get("verify", True) is True and http.session.verify is True
        if stage == "headers":
            raise failure
        headers = {"ETag": TAG if stage == "slc-body" else '"tls-v1"', "Content-Length": str(len(payload))}
        if stage in ("range-body", "slc-body"):
            headers["Content-Range"] = f"bytes 0-{len(payload)-1}/{len(payload)}"
        if stage == "slc-body":
            headers["Content-Type"] = "application/zip"
        response = Broken(206 if stage in ("range-body", "slc-body") else 200, headers=headers)
        responses.append(response)
        return response

    monkeypatch.setattr(http.session, "request", request)
    monkeypatch.setattr("core.http.wait_retry", lambda *_: pytest.fail("Fatal TLS was retried"))
    monkeypatch.setattr("core.range_file.wait_retry", lambda *_: pytest.fail("Fatal TLS was retried"))
    monkeypatch.setattr("core.raw_download.wait_retry", lambda *_: pytest.fail("Fatal TLS was retried"))
    monkeypatch.setattr("core.process_request.wait_retry", lambda *_: pytest.fail("Fatal TLS was retried"))
    monkeypatch.setattr("core.slc_archive.wait_retry", lambda *_: pytest.fail("Fatal TLS was retried"))
    try:
        with pytest.raises(UserError, match="证书" if kind == "certificate" else "TLS") as result:
            if stage == "headers":
                http.request("GET", "https://example.test/object")
            elif stage == "range-body":
                RangeFile(http, "https://example.test/object", page_size=len(payload))
            elif stage == "process-body":
                body, products, options = _payload()
                fetch_tile(http, StubAuth(), body, tmp_path / "block.tif", options=options, products=products)
            elif stage == "slc-body":
                monkeypatch.setattr(slc_archive, "HttpClient", lambda: http)
                slc_archive.transfer_slc(slc_target(payload), tmp_path / "slc.part", None, lambda *_: None)
            else:
                _stream(http, None, {"asset": None, "name": "native.bin", "transport": "public"},
                        tmp_path / "receive.part", None, lambda *_: None,
                        requester=lambda headers, status: http.request("GET", "https://example.test/object",
                                                                      headers=headers, stream=True, attempts=1))
        assert len(calls) == 1 and all(response.closed for response in responses)
        assert "private" not in str(result.value) and "secret" not in str(result.value)
    finally:
        http.close()


def test_certificate_failure_takes_precedence_over_eof():
    failure = eof()
    failure.__context__ = ssl.SSLCertVerificationError(1, "certificate private")
    assert tls_error_kind(failure) == "certificate"
    assert tls_error_kind(requests.exceptions.SSLError("[SSL: CERTIFICATE_VERIFY_FAILED] private")) == "certificate"
    cyclic = requests.exceptions.SSLError("unknown")
    cyclic.__cause__ = cyclic
    assert tls_error_kind(cyclic) == "unknown"


@contextmanager
def closed_tls_handshake():
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    listener.settimeout(.2)
    stop, accepted = threading.Event(), []

    def receive():
        while not stop.is_set():
            try:
                connection, _ = listener.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            with connection:
                connection.settimeout(2)
                try:
                    hello = connection.recv(65536)
                    accepted.append(hello)
                    connection.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass

    worker = threading.Thread(target=receive, daemon=True)
    worker.start()
    try:
        yield f"https://127.0.0.1:{listener.getsockname()[1]}/object", accepted
    finally:
        stop.set()
        listener.close()
        worker.join(timeout=3)


def test_real_tls_eof_uses_only_the_shared_three_attempts(monkeypatch):
    monkeypatch.setattr("core.range_file.wait_retry", lambda *_: None)
    with closed_tls_handshake() as (url, accepted):
        http = HttpClient()
        http.session.trust_env = False
        try:
            with pytest.raises(requests.exceptions.SSLError) as original:
                http.session.get(url, timeout=(2, 2))
            assert tls_error_kind(original.value) == "interrupted", "Must exercise a real TLS EOF, not a TCP reset"
            before = len(accepted)
            with pytest.raises(TransientDownloadError, match="已保留"):
                RangeFile(http, url)
            assert len(accepted) == before + 3
            assert all(hello and hello[0] == 22 for hello in accepted), "A TLS ClientHello must reach the server"
            assert http.session.verify is True
        finally:
            http.close()


def test_tls_eof_header_retry_remains_cancellable(monkeypatch):
    waits, calls = [], []
    cancel = SimpleNamespace(is_set=lambda: False, wait=lambda delay: waits.append(delay) or True)
    http = HttpClient()

    def request(*_, **kwargs):
        calls.append(kwargs)
        raise eof()

    monkeypatch.setattr(http.session, "request", request)
    try:
        with pytest.raises(Cancelled):
            http.request("GET", "https://example.test/object", cancel=cancel)
        assert len(calls) == 1 and waits == [2]
        assert http.session.verify is True
    finally:
        http.close()


def test_tls_eof_body_retries_smaller_pinned_pages_without_committing_partial_bytes(monkeypatch, tmp_path):
    payload, calls, responses = bytes(range(192)), [], []
    failure = {"remaining": 1}

    class Broken(StubResponse):
        def iter_content(self, size):
            yield self.content[:17]
            raise eof()

    def request(*_, **kwargs):
        headers = kwargs["headers"]
        calls.append(headers.copy())
        start, end = map(int, re.fullmatch(r"bytes=(\d+)-(\d+)", headers["Range"]).groups())
        end = min(end, len(payload) - 1)
        broken = start == 64 and failure["remaining"] > 0
        failure["remaining"] -= int(broken)
        response = (Broken if broken else StubResponse)(206, content=payload[start:end + 1],
            headers={"ETag": '"tls-v1"', "Content-Length": str(end - start + 1),
                     "Content-Range": f"bytes {start}-{end}/{len(payload)}"})
        responses.append(response)
        return response

    http = HttpClient()
    monkeypatch.setattr(http.session, "request", request)
    monkeypatch.setattr("core.range_file.wait_retry", lambda *_: None)
    try:
        with RangeFile(http, "https://example.test/object", page_size=64, cache_pages=3,
                       coalesce_pages=3, cache_dir=tmp_path) as source:
            assert source.read() == payload
            assert source.range_failures == source.coalesced_fallbacks == 1
        assert [call["Range"] for call in calls] == ["bytes=0-63", "bytes=64-191", "bytes=64-127", "bytes=128-191"]
        assert all(call["If-Match"] == '"tls-v1"' for call in calls[1:])
        assert all(response.closed for response in responses)
        calls.clear()
        with RangeFile(http, "https://example.test/object", page_size=64, cache_pages=3, cache_dir=tmp_path) as source:
            assert source.read() == payload and source.requests == 0
        assert not calls
    finally:
        http.close()


def test_original_tls_eof_resumes_only_the_verified_prefix(monkeypatch, tmp_path):
    payload, calls, responses = bytes(range(251)), [], []
    partial = tmp_path / "receive.part"

    class Broken(StubResponse):
        def iter_content(self, size):
            yield payload[:37]
            raise eof()

    def request(headers, _status):
        calls.append(headers.copy())
        if len(calls) == 1:
            response = Broken(headers={"ETag": '"tls-v1"', "Content-Length": str(len(payload))})
        else:
            assert headers["Range"] == "bytes=37-" and headers["If-Match"] == '"tls-v1"'
            receipt = json.loads(partial.with_name(partial.name + ".resume.json").read_text(encoding="utf-8"))
            assert receipt["received"] == 37 and partial.read_bytes() == payload[:37]
            response = StubResponse(206, content=payload[37:], headers={"ETag": '"tls-v1"',
                "Content-Length": str(len(payload) - 37), "Content-Range": f"bytes 37-{len(payload)-1}/{len(payload)}"})
        responses.append(response)
        return response

    monkeypatch.setattr("core.raw_download.wait_retry", lambda *_: None)
    result = _stream(None, None, {"asset": None, "name": "native.bin", "transport": "public"},
                     partial, None, lambda *_: None, requester=request)
    assert result["transfer_attempts"] == 2 and partial.read_bytes() == payload
    assert len(calls) == 2 and all(response.closed for response in responses)


@pytest.mark.parametrize("failures", [1, 3])
def test_process_tls_eof_preserves_payload_and_three_attempt_limit(monkeypatch, tmp_path, failures):
    body, products, options = _payload()
    received, calls = [], []
    content = archive_bytes(("default.tif", b"complete test tiff"),
                            ("userdata.json", b'{"candidates":0,"sources":[]}'))

    class Broken(StubResponse):
        def iter_content(self, size):
            yield content[:37]
            raise eof()

    def request(*_, **kwargs):
        calls.append(kwargs)
        assert kwargs["attempts"] == 1 and kwargs["json"] == body
        response = Broken() if len(calls) <= failures else StubResponse(content=content)
        received.append(response)
        return response

    monkeypatch.setattr("core.process_request.wait_retry", lambda *_: None)
    path = tmp_path / "block.tif"
    args = (SimpleNamespace(request=request), StubAuth(), body, path)
    if failures == 3:
        with pytest.raises(TransientDownloadError):
            fetch_tile(*args, options=options, products=products)
        assert len(calls) == 3 and not path.exists()
    else:
        assert fetch_tile(*args, options=options, products=products)["sources"] == []
        assert len(calls) == 2 and path.read_bytes() == b"complete test tiff"
    assert all(response.closed for response in received) and not path.with_suffix(".part").exists()


@pytest.mark.parametrize("failures", [1, 4])
def test_slc_tls_eof_pins_remaining_prefix_and_four_attempt_limit(monkeypatch, tmp_path, failures):
    payload, received, calls = bytes(range(251)), [], []

    class Broken(StubResponse):
        def iter_content(self, size):
            yield self.content[:37]
            raise eof()

    def request(*_, **kwargs):
        headers = kwargs["headers"]
        calls.append(headers.copy())
        assert kwargs["attempts"] == 1 and headers["If-Match"] == TAG
        start, end = map(int, re.fullmatch(r"bytes=(\d+)-(\d+)", headers["Range"]).groups())
        response = (Broken if len(calls) <= failures else StubResponse)(206, content=payload[start:end+1],
            headers={"ETag": TAG, "Content-Type": "application/zip", "Content-Length": str(end-start+1),
                     "Content-Range": f"bytes {start}-{end}/{len(payload)}"})
        received.append(response)
        return response

    monkeypatch.setattr(slc_archive, "HttpClient", lambda: SimpleNamespace(request=request, close=lambda: None))
    monkeypatch.setattr(slc_archive, "wait_retry", lambda *_: None)
    path = tmp_path / "slc.part"
    if failures == 4:
        with pytest.raises(TransientDownloadError):
            slc_archive.transfer_slc(slc_target(payload), path, None, lambda *_: None)
        assert len(calls) == 4 and path.read_bytes()[:148] == payload[:148]
        receipt = json.loads(path.with_name(path.name + ".ranges.json").read_text(encoding="utf-8"))
        assert receipt["windows"]["0"]["received"] == 148
    else:
        assert slc_archive.transfer_slc(slc_target(payload), path, None, lambda *_: None)["transfer_attempts"] == 2
        assert len(calls) == 2 and path.read_bytes() == payload
    assert calls[1]["Range"] == f"bytes=37-{len(payload)-1}"
    assert all(response.closed for response in received)
