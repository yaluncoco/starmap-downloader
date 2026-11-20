from __future__ import annotations

import io
import json
import re
import tarfile
import threading
import time
from contextlib import contextmanager

import numpy as np
import pytest
import requests
from rasterio.io import MemoryFile
from rasterio.transform import from_bounds
from shapely.geometry import box, mapping

from app import make_server
from core.http import PROCESS_URL, SH_CATALOGUE_URL, TOKEN_URL, HttpClient, check_cancel

P1 = "S2A_MSIL2A_20240610T030531_N0510_R075_T50TMK_20240610T081949.SAFE"
P2 = "S2B_MSIL2A_20240605T030529_N0510_R075_T50TMK_20240605T082015.SAFE"
D1 = "2024-06-10T03:16:50.553Z"
D2 = "2024-06-05T03:16:50.631Z"
BBOX = "116.350,39.900,116.355,39.905"
TRIANGLE = "116.350,39.900\n116.355,39.900\n116.355,39.905"


class StubResponse:
    def __init__(self, status=200, data=None, content=b"", headers=None):
        self.status_code = status
        self.data = data
        self.content = content
        self.headers = headers or {}
        self.closed = False

    def json(self):
        return self.data

    def iter_content(self, size):
        for start in range(0, len(self.content), size):
            yield self.content[start:start + size]

    def close(self):
        self.closed = True


def feature(identifier=P1, acquired=D1, cloud=12.5, footprint=None):
    return {"id": identifier, "properties": {"datetime": acquired, "eo:cloud_cover": cloud},
            "geometry": mapping(footprint if footprint is not None else box(116.20, 39.80, 116.50, 40.00))}


def archive_bytes(*members):
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w") as archive:
        for name, data in members:
            item = tarfile.TarInfo(name)
            item.size = len(data)
            archive.addfile(item, fileobj=io.BytesIO(data))
    return out.getvalue()


class StubProvider:
    def __init__(self):
        self.catalogue = [feature(), feature(P2, D2, cloud=29)]
        self.requests = []
        self.trap_started = threading.Event()
        self.block_number = None
        self.empty = False
        self.unmatched = False
        self.wrong_grid = False

    def request(self, method, url, *, cancel=None, **kwargs):
        self.requests.append((method, url, kwargs))
        check_cancel(cancel)
        if url == TOKEN_URL:
            assert kwargs["data"] == {"grant_type": "client_credentials", "client_id": "mock-client", "client_secret": "mock-secret"}
            return StubResponse(data={"access_token": "mock-token", "expires_in": 300})
        assert kwargs.get("headers", {}).get("Authorization") == "Bearer mock-token"
        if url == SH_CATALOGUE_URL:
            return StubResponse(data={"features": self.catalogue, "context": {}})
        assert url == PROCESS_URL and kwargs["headers"]["Accept"] == "application/tar"
        slot = sum(row[1] == PROCESS_URL for row in self.requests)
        if self.block_number == slot:
            self.trap_started.set()
            if not cancel.wait(6):
                raise AssertionError("Cancellation was never requested")
            check_cancel(cancel)
        payload = kwargs["json"]
        script = payload["evalscript"]
        products = json.loads(re.search(r"^var allowed = (.*);$", script, re.M)[1])
        output = payload["output"]
        width, height = output["width"], output["height"]
        bands, sample_type = re.search(r'bands: (\d+), sampleType: "(\w+)"', script).groups()
        bands = int(bands)
        bbox = payload["input"]["bounds"]["bbox"]
        crs = "EPSG:" + payload["input"]["bounds"]["properties"]["crs"].split("/")[-1]
        fill = -9999 if sample_type == "FLOAT32" else 0
        dtype = sample_type.lower()
        values = np.full((bands, height, width), fill if self.empty else .25, dtype=dtype)
        if sample_type == "UINT8" and not self.empty:
            values[:] = np.asarray([30, 60, 90, 255]).reshape(4, 1, 1)
        with MemoryFile() as mem:
            with mem.open(driver="GTiff", width=width, height=height, count=bands, dtype=dtype, crs=("EPSG:4326" if self.wrong_grid else crs),
                          transform=from_bounds(*bbox, width, height)) as raster:
                raster.write(values)
            tiff = mem.read()
        sources = [{"catalogue_id": p["id"], "match": "safe_id", "date": p["utc"],
                    "data_path": f"s3://example/2024/{p['id']}.SAFE"} for p in products]
        metadata = json.dumps({"candidates": len(sources), "sources": [] if self.unmatched else sources}).encode()
        return StubResponse(content=archive_bytes(("default.tif", tiff), ("userdata.json", metadata)),
                            headers={"X-ProcessingUnits-Spent": "0.01"})

    @property
    def process_calls(self):
        return [args for method, url, args in self.requests if url == PROCESS_URL]


@pytest.fixture
def provider(monkeypatch):
    upstream = StubProvider()
    monkeypatch.setattr(HttpClient, "request", lambda _, *args, **kwargs: upstream.request(*args, **kwargs))
    return upstream


@contextmanager
def serve(config_dir):
    server = make_server(config_dir=config_dir)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield server
    finally:
        server.service.shutdown()
        server.shutdown()
        server.server_close()
        worker.join(3)


@pytest.fixture
def server(provider, tmp_path):
    with serve(tmp_path / "settings") as running:
        yield running


class AppClient:
    def __init__(self, origin):
        self.origin = origin
        self.session = requests.Session()
        self.session.trust_env = False
        home = self.session.get(origin, timeout=5)
        self.token = re.search(r'<meta name="app-token" content="([^"<>]+)"', home.text)[1]
        self.headers = {"X-App-Token": self.token}

    def get(self, route, **kwargs):
        return self.session.get(f"{self.origin}/api/{route}", headers=self.headers, timeout=6, **kwargs)

    def post(self, route, body, status=200):
        resp = self.session.post(f"{self.origin}/api/{route}", headers=self.headers, json=body, timeout=6)
        assert resp.status_code == status, resp.text
        return resp.json()

    def wait(self, job_id, status="completed"):
        now = time.monotonic()
        while time.monotonic() - now < 10:
            snapshot = self.get("job", params={"id": job_id}).json()
            if snapshot["status"] not in ("running", "cancelling"):
                assert snapshot["status"] == status, snapshot
                return snapshot
            time.sleep(.04)
        pytest.fail(f"Job did not finish: {job_id}")

    def search(self, coordinates=BBOX, cloud=100):
        if not self.get("bootstrap").json()["connected"]:
            self.post("connect", {"client_id": "mock-client", "client_secret": "mock-secret"})
        area = self.post("aoi", {"coordinates": coordinates})
        data = self.post("search", {"geometry": area["geometry"], "start": "2024-06-01", "end": "2024-06-10", "max_cloud": cloud})
        result = self.wait(data["job_id"])["result"]
        return {"geometry": area["geometry"], "query_id": result["query"]["id"],
                "scene_keys": [scene["key"] for scene in result["scenes"]],
                "options": {"mode": "rgbnir", "resolution": 10, "max_cloud": cloud, "mask_clouds": False}}


@pytest.fixture
def client(server):
    helper = AppClient(server.origin)
    try:
        yield helper
    finally:
        helper.session.close()
