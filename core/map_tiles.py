"""Finite map requests, trusted WGS84 providers and a bounded, checked disk cache."""
from __future__ import annotations

import hashlib
import json
import struct
import threading
import time
import uuid
import zlib
from pathlib import Path

import requests

from . import VERSION
from .errors import Cancelled, UserError
from .cancellable_http import request_with_cancel

PROVIDERS = {
    "standard": (
        ("carto", "https://a.basemaps.cartocdn.com/light_all/{z}/{x}/{y}.png"),
        ("osm", "https://tile.openstreetmap.org/{z}/{x}/{y}.png"),
    ),
    "satellite": (
        ("esri", "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}"),
        ("esri-backup", "https://services.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}"),
    ),
}
MAX_TILE_BYTES = 1024**2


def _image_type(data):
    if len(data) > MAX_TILE_BYTES:
        raise ValueError("oversized tile")
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        offset, header, ended = 8, False, False
        while offset + 12 <= len(data):
            length = struct.unpack(">I", data[offset:offset + 4])[0]
            stop = offset + 12 + length
            if stop > len(data):
                raise ValueError("truncated PNG")
            kind, payload = data[offset + 4:offset + 8], data[offset + 8:stop - 4]
            if zlib.crc32(kind + payload) & 0xffffffff != struct.unpack(">I", data[stop - 4:stop])[0]:
                raise ValueError("PNG CRC mismatch")
            if kind == b"IHDR":
                if header or length != 13 or struct.unpack(">II", payload[:8]) not in ((256, 256), (512, 512)):
                    raise ValueError("invalid tile dimensions")
                header = True
            if kind == b"IEND":
                ended = header and length == 0 and stop == len(data)
                break
            offset = stop
        if not ended:
            raise ValueError("incomplete PNG")
        return "image/png"
    if len(data) > 32 and data.startswith(b"\xff\xd8\xff") and data.endswith(b"\xff\xd9"):
        return "image/jpeg"
    raise ValueError("not a map image")


class MapTiles:
    def __init__(self, root, *, max_bytes=128 * 1024**2, max_tiles=2048, session_factory=requests.Session):
        self.root = Path(root)
        self.max_bytes, self.max_tiles = max_bytes, max_tiles
        self.session_factory = session_factory
        self.lock = threading.RLock()
        self.slots = threading.BoundedSemaphore(4)
        self.health, self.entries = {}, {}
        self.counters = {"cache_hits": 0, "downloads": 0, "fallbacks": 0, "failures": 0, "stale_hits": 0, "corrupt_cache": 0}
        if self.root.is_dir():
            for path in self.root.glob("*.tile"):
                try:
                    self.entries[path.stem] = (path.stat().st_size, path.stat().st_mtime)
                except OSError:
                    continue
        self._prune()

    def status(self):
        with self.lock:
            return {**self.counters, "cached_tiles": len(self.entries), "cache_bytes": sum(row[0] for row in self.entries.values()),
                    "offline_basemap": True}

    @staticmethod
    def _key(kind, z, x, y):
        if kind not in PROVIDERS or any(type(number) is not int for number in (z, x, y)) or not 0 <= z <= 19 or not 0 <= x < 2**z or not 0 <= y < 2**z:
            raise UserError("底图坐标或类型无效。")
        return f"{kind}-{z}-{x}-{y}"

    def _read(self, key):
        with self.lock:
            if key not in self.entries:
                return None
            try:
                path = self.root / (key + ".tile")
                if not 0 < path.stat().st_size <= MAX_TILE_BYTES:
                    raise ValueError
                data = path.read_bytes()
                meta_path = self.root / (key + ".json")
                if meta_path.stat().st_size > 8192:
                    raise ValueError
                meta = json.loads(meta_path.read_bytes())
                if hashlib.sha256(data).hexdigest() != meta["sha256"] or _image_type(data) != meta["mime"]:
                    raise ValueError
                self.entries[key] = (len(data), time.time())
                return data, meta["mime"], time.time() - meta["saved_at"] > 30 * 86400
            except (OSError, ValueError, KeyError, TypeError, struct.error):
                self.entries.pop(key, None)
                self.counters["corrupt_cache"] += 1
                for suffix in (".tile", ".json"):
                    try:
                        (self.root / (key + suffix)).unlink(missing_ok=True)
                    except OSError:
                        pass
                return None

    def _prune(self):
        with self.lock:
            total = sum(row[0] for row in self.entries.values())
            for key, (size, _) in sorted(self.entries.items(), key=lambda row: row[1][1]):
                if total <= self.max_bytes and len(self.entries) <= self.max_tiles:
                    break
                for suffix in (".tile", ".json"):
                    try:
                        (self.root / (key + suffix)).unlink(missing_ok=True)
                    except OSError:
                        pass
                self.entries.pop(key, None)
                total -= size

    def _save(self, key, data, mime, provider):
        temporary = self.root / (key + "." + uuid.uuid4().hex + ".tmp")
        try:
            with self.lock:
                self.root.mkdir(parents=True, exist_ok=True)
                temporary.write_bytes(data)
                temporary.replace(self.root / (key + ".tile"))
                temporary.write_text(json.dumps({"sha256": hashlib.sha256(data).hexdigest(), "mime": mime,
                                                "provider": provider, "saved_at": time.time()}), encoding="utf-8")
                temporary.replace(self.root / (key + ".json"))
                self.entries[key] = (len(data), time.time())
                self._prune()
        except OSError:
            pass  # A full/read-only cache must not prevent the map from displaying.
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass

    def get(self, kind, z, x, y):
        key = self._key(kind, z, x, y)
        cached = self._read(key)
        if cached and not cached[2]:
            with self.lock:
                self.counters["cache_hits"] += 1
            return cached[:2]
        if not self.slots.acquire(timeout=4):
            if cached:
                return cached[:2]
            raise UserError("底图线路忙，请稍后重试；离线行政边界仍可使用。")
        try:
            # Another browser request may already have populated this tile.
            current = self._read(key)
            if current and not current[2]:
                with self.lock:
                    self.counters["cache_hits"] += 1
                return current[:2]
            for index, (provider, template) in enumerate(PROVIDERS[kind]):
                with self.lock:
                    if self.health.get(provider, {}).get("retry_at", 0) > time.monotonic():
                        continue
                try:
                    cancel = threading.Event()
                    deadline = threading.Timer(8, cancel.set)
                    deadline.daemon = True
                    deadline.start()
                    with self.session_factory() as session:
                        session.headers.update({"User-Agent": f"StarmapDownloader/{VERSION} (local desktop map)"})
                        with request_with_cancel(session, "GET", template.format(z=z, x=x, y=y), cancel=cancel,
                                                 timeout=(3, 5), stream=True, allow_redirects=False) as response:
                            if response.status_code != 200:
                                raise ValueError("provider unavailable")
                            data = bytearray()
                            for chunk in response.iter_content(65536):
                                data.extend(chunk)
                                if len(data) > MAX_TILE_BYTES:
                                    raise ValueError("oversized tile")
                            content = bytes(data)
                    mime = _image_type(content)
                    self._save(key, content, mime, provider)
                    with self.lock:
                        self.health[provider] = {"failures": 0, "retry_at": 0}
                        self.counters["downloads"] += 1
                        self.counters["fallbacks"] += bool(index)
                    return content, mime
                except (Cancelled, requests.RequestException, ValueError, OSError, struct.error):
                    with self.lock:
                        failures = self.health.get(provider, {}).get("failures", 0) + 1
                        self.health[provider] = {"failures": failures, "retry_at": time.monotonic() + 30 if failures >= 3 else 0}
                finally:
                    deadline.cancel()
            if cached:
                with self.lock:
                    self.counters["stale_hits"] += 1
                return cached[:2]
            with self.lock:
                self.counters["failures"] += 1
            raise UserError("在线底图暂不可用，已提供离线行政边界；可点击重试底图。")
        finally:
            self.slots.release()
