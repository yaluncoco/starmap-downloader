"""Map cache reuse, corruption, provider fallback and bounded stalled requests."""
from __future__ import annotations

import struct
import threading
import time
import zlib

import pytest

from core.errors import UserError
from core.map_tiles import MapTiles


def png(value=235):
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xffffffff)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 256, 256, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress((b"\x00" + bytes([value]) * 768) * 256)) + chunk(b"IEND", b""))


class Response:
    def __init__(self, content, status=200):
        self.content, self.status_code = content, status
        self.closed = False

    def iter_content(self, size):
        yield self.content

    def close(self):
        self.closed = True


class Factory:
    def __init__(self, handle):
        self.handle, self.calls = handle, []

    def __call__(self):
        factory = self
        class Session:
            headers = {}
            def __enter__(self):
                return self
            def __exit__(self, *args):
                pass
            def request(self, method, url, **kwargs):
                factory.calls.append((method, url, kwargs))
                return factory.handle(url)
        return Session()


def test_map_provider_fallback_disk_reuse_and_corruption_detection(tmp_path):
    original = png()
    factory = Factory(lambda url: Response(original, status=503 if "cartocdn" in url else 200))
    cache = MapTiles(tmp_path, session_factory=factory)
    assert cache.get("standard", 4, 13, 6) == (original, "image/png")
    assert len(factory.calls) == 2 and cache.status()["fallbacks"] == 1
    assert all(call[2]["allow_redirects"] is False and call[2]["timeout"] == (3, 5) for call in factory.calls)
    assert cache.get("standard", 4, 13, 6)[0] == original and len(factory.calls) == 2
    damaged = bytearray(original)
    damaged[-7] ^= 1
    (tmp_path / "standard-4-13-6.tile").write_bytes(damaged)
    assert cache.get("standard", 4, 13, 6)[0] == original
    assert cache.status()["corrupt_cache"] == 1 and len(factory.calls) == 4
    offline = Factory(lambda url: (_ for _ in ()).throw(AssertionError("A checked tile should be served without network")))
    restarted = MapTiles(tmp_path, session_factory=offline)
    assert restarted.get("standard", 4, 13, 6)[0] == original and not offline.calls


def test_map_cache_has_a_fixed_size_and_invalid_upstream_is_never_cached(tmp_path):
    factory = Factory(lambda url: Response(png()))
    cache = MapTiles(tmp_path, max_tiles=2, session_factory=factory)
    for x in range(3):
        cache.get("standard", 2, x, 1)
    assert cache.status()["cached_tiles"] == 2
    assert not (tmp_path / "standard-2-0-1.tile").exists()
    bad = bytearray(png())
    bad[-6] ^= 1
    other = tmp_path / "invalid"
    invalid = MapTiles(other, session_factory=Factory(lambda url: Response(bytes(bad))))
    with pytest.raises(UserError, match="底图暂不可用"):
        invalid.get("satellite", 2, 1, 1)
    assert not list(other.glob("*.tile"))
    with pytest.raises(UserError):
        cache.get("standard", 0, 1, 0)
    with pytest.raises(UserError):
        cache.get("https://example.com", 2, 1, 1)


def test_stalled_map_headers_are_cancelled_with_a_finite_budget(tmp_path, monkeypatch):
    real_timer = threading.Timer
    monkeypatch.setattr("core.map_tiles.threading.Timer", lambda interval, callback: real_timer(.03, callback))
    release = threading.Event()
    def stalled(url):
        release.wait(3)
        return Response(png())
    cache = MapTiles(tmp_path, session_factory=Factory(stalled))
    begin = time.monotonic()
    try:
        with pytest.raises(UserError, match="底图暂不可用"):
            cache.get("standard", 2, 1, 1)
        assert time.monotonic() - begin < 2
        assert not list(tmp_path.glob("*.tile"))
    finally:
        release.set()
