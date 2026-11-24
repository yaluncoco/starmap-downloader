"""Real HTTP concurrency, same-thread GDAL ownership and cancellation of band groups."""
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import re
import threading
import time

import numpy as np
import pytest
from affine import Affine
from rasterio.io import MemoryFile
from rasterio.transform import from_origin
from rasterio.windows import Window, bounds

from core.errors import Cancelled, UserError
from core.public_sources import PublicSources


GRID = from_origin(443000, 4418000, 10, 10)


def payload(value):
    with MemoryFile() as memory:
        with memory.open(driver="GTiff", width=64, height=64, count=1, dtype="uint16", nodata=0,
                         crs="EPSG:32650", transform=GRID, tiled=True, blockxsize=16, blockysize=16) as dataset:
            dataset.write(np.full((1, 64, 64), value, dtype=np.uint16))
        return memory.read()


@contextmanager
def provider(*, stall=None, wrong_range=False):
    content = {f"B{i:02}": payload(1000 + i) for i in range(1, 11)}
    lock, entered, release = threading.Lock(), threading.Event(), threading.Event()
    state = {"active": 0, "peak": 0, "requests": []}
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):
            band = self.path.strip("/")
            start, end = map(int, re.fullmatch(r"bytes=(\d+)-(\d+)", self.headers["Range"]).groups())
            data = content[band]; end = min(end, len(data) - 1)
            with lock:
                state["active"] += 1
                state["peak"] = max(state["peak"], state["active"])
                state["requests"].append(band)
                if state["active"] >= 4:
                    entered.set()
            try:
                assert entered.wait(5), "Four independent reads must reach the server"
                if stall == "headers" and not (wrong_range and band == "B01"):
                    release.wait(5)
                self.send_response(206)
                self.send_header("Content-Range", f"bytes {start + int(wrong_range and band == 'B01')}-{end}/{len(data)}")
                self.send_header("Content-Length", str(end - start + 1))
                self.send_header("ETag", f'"version-{band}"')
                self.end_headers()
                if stall == "body":
                    self.wfile.write(data[start:start + 16]); self.wfile.flush()
                    release.wait(5)
                    start += 16
                self.wfile.write(data[start:end + 1]); self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass
            finally:
                with lock:
                    state["active"] -= 1
            self.close_connection = True
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    assets = {band: {"url": f"http://127.0.0.1:{server.server_port}/{band}", "shape": [64, 64],
                     "transform": list(GRID[:6]), "epsg": 32650, "dtype": "uint16", "nodata": 0,
                     "scale": .0001, "offset": 0} for band in content}
    try:
        yield assets, state, entered, release
    finally:
        release.set(); server.shutdown(); thread.join(3); server.server_close()


def test_bounded_native_reads_keep_datasets_on_their_owner_and_reuse_validated_cache(tmp_path, monkeypatch):
    from core import public_download

    opened, reads, closed, sessions = {}, [], [], set()
    original = public_download._open_source
    def tracked(stack, http, asset, *args, **kwargs):
        source, stream, metadata = original(stack, http, asset, *args, **kwargs)
        owner = threading.get_ident()
        opened[asset["url"]] = owner; sessions.add(id(http.session))
        def closing():
            assert threading.get_ident() == owner
            closed.append(asset["url"])
        stack.callback(closing)
        class Dataset:
            def __getattr__(self, name):
                return getattr(source, name)
            def read(self, *args, **kwargs):
                assert threading.get_ident() == owner
                reads.append(asset["url"])
                return source.read(*args, **kwargs)
        return Dataset(), stream, metadata
    monkeypatch.setattr(public_download, "_open_source", tracked)
    with provider() as (assets, state, _, _):
        window = Window(8, 10, 20, 18)
        transform = GRID @ Affine.translation(window.col_off, window.row_off)
        with PublicSources(assets, cache_dir=tmp_path) as group:
            first = group.read(bounds(window, GRID), transform, "EPSG:32650", 18, 20)
            repeated = group.read(bounds(window, GRID), transform, "EPSG:32650", 18, 20)
            assert list(first) == list(assets)
            for i, band in enumerate(assets, 1):
                np.testing.assert_allclose(first[band], (1000 + i) * .0001, rtol=0, atol=2e-7)
                np.testing.assert_array_equal(first[band], repeated[band])
        assert len(opened) == len(assets) and len(reads) == 2 * len(assets)
        assert sorted(closed) == sorted(opened)
        assert len(sessions) == 4
        assert state["peak"] == 4
        count = len(state["requests"])
        with PublicSources(assets, cache_dir=tmp_path) as restored:
            actual = restored.read(bounds(window, GRID), transform, "EPSG:32650", 18, 20)
            assert all(info[1].requests == 0 for info in restored.sources.values())
            for band in assets:
                np.testing.assert_array_equal(actual[band], first[band])
        assert len(state["requests"]) == count


@pytest.mark.parametrize("phase", ["headers", "body"])
def test_cancel_stalled_band_group_stops_all_workers_without_committing_partial_pages(tmp_path, phase):
    with provider(stall=phase) as (assets, _, entered, release):
        stop, errors = threading.Event(), []
        def load():
            try:
                with PublicSources(assets, stop, cache_dir=tmp_path):
                    raise AssertionError("Stalled source group must be cancelled")
            except BaseException as error:
                errors.append(error)
        task = threading.Thread(target=load); task.start()
        assert entered.wait(3)
        begin = time.monotonic(); stop.set(); task.join(2)
        assert not task.is_alive() and time.monotonic() - begin < 2
        assert len(errors) == 1 and isinstance(errors[0], Cancelled)
        assert not [p for p in tmp_path.rglob("*") if p.is_file() and p.name.isdecimal()]
        assert not [t for t in threading.enumerate() if t.name.startswith("public-band-")]
        release.set()


def test_one_invalid_range_cancels_sibling_reads_and_preserves_the_original_error(tmp_path):
    with provider(stall="headers", wrong_range=True) as (assets, _, _, release):
        stop = threading.Event()
        begin = time.monotonic()
        with pytest.raises(UserError, match="字节区间"):
            PublicSources(assets, stop, cache_dir=tmp_path)
        assert time.monotonic() - begin < 3 and not stop.is_set()
        assert not [t for t in threading.enumerate() if t.name.startswith("public-band-")]
        release.set()
