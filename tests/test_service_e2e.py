import json
import socket
import time
from http.client import HTTPResponse
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
import rasterio
from shapely.geometry import box, mapping

import core.service as service
from core.planning import build_plan
from conftest import P1, TRIANGLE


def test_http_session_is_local_and_credentials_are_not_persisted(client, server):
    with client.session as session:
        assert session.get(server.origin + "/health", timeout=5).json()["ok"]
        assert session.get(server.origin + "/api/bootstrap", timeout=5).status_code == 403
        assert session.get(server.origin + "/", headers={"Host": "example.test"}, timeout=5).status_code == 403
        assert session.post(server.origin + "/api/disconnect", headers={**client.headers, "Origin": "https://example.test"}, json={}, timeout=5).status_code == 403
    client.post("connect", {"client_id": "mock-client", "client_secret": "mock-secret"})
    client.post("preferences", {"client_secret": "mock-secret", "access_token": "mock-token"})
    preferences = json.loads(server.service.pref_file.read_text(encoding="utf-8"))
    assert preferences["client_id"] == "mock-client" and set(preferences) == {"client_id", "output_dir"}


@pytest.mark.parametrize("rejection", ["session", "origin"])
def test_rejected_post_with_delayed_body_returns_complete_403(client, server, rejection):
    body = b"x" * (64 * 1024)
    headers = {"Host": f"127.0.0.1:{server.server_port}", "Content-Length": str(len(body))}
    if rejection == "origin":
        headers.update(client.headers)
        headers["Origin"] = "https://example.test"
    request = "POST /api/retry-download HTTP/1.1\r\n" + "".join(f"{key}: {value}\r\n" for key, value in headers.items()) + "\r\n"
    with socket.create_connection(("127.0.0.1", server.server_port), timeout=3) as connection:
        connection.sendall(request.encode("ascii"))
        time.sleep(.02)
        connection.sendall(body)
        response = HTTPResponse(connection)
        response.begin()
        assert response.status == 403 and json.loads(response.read())["error"]
    assert server.service.active_job() is None


def test_full_workflow_multiblock_selection_and_resume(client, provider, tmp_path, monkeypatch):
    monkeypatch.setattr(service, "build_plan", lambda *args, **kwargs: build_plan(*args, tile_size=16, **kwargs))
    request = client.search(TRIANGLE, cloud=20)
    assert request["scene_keys"] == [P1]
    options = request["options"]
    client.post("plan", {**request, "options": {**options, "max_cloud": 21}}, status=400)
    client.post("plan", {**request, "geometry": mapping(box(116, 39, 116.2, 39.2))}, status=400)
    client.post("plan", {**request, "query_id": "old-search"}, status=400)
    plan = client.post("plan", request)
    assert plan["tiles_per_scene"] > 1
    request["output_dir"] = str(tmp_path / "研究输出")
    job = client.wait(client.post("download", request)["job_id"])
    result = job["result"]
    assert len(provider.process_calls) == plan["tiles_per_scene"]
    assert "新保存 1 景" in job["message"]
    with rasterio.open(result["files"][0]["path"]) as dataset:
        assert (dataset.width, dataset.height, dataset.count) == (plan["width"], plan["height"], 4)
        assert dataset.crs.to_epsg() == 32650 and dataset.res == (10, 10) and dataset.nodata == -9999
        assert dataset.descriptions == ("B04", "B03", "B02", "B08")
        values = dataset.read(1)
        assert set(np.unique(values)) == {np.float32(.25), np.float32(-9999)}
        assert 0 < int(dataset.tags()["valid_pixels"]) < dataset.width * dataset.height
    manifest = json.loads(Path(result["manifest"]).read_text(encoding="utf-8"))
    assert manifest["status"] == "completed" and all(block["sources"] for block in manifest["items"][0]["blocks"])
    assert (Path(result["folder"]) / "研究区.geojson").is_file()
    assert not list(Path(result["folder"]).glob("*.partial.tif"))
    second = client.wait(client.post("download", request)["job_id"])["result"]
    assert second["files"][0]["status"] == "skipped"
    assert len(provider.process_calls) == plan["tiles_per_scene"]
    assert client.post("aoi", {"clear": True}) is None
    assert client.get("bootstrap").json()["aoi"] is None
    client.post("plan", request, status=400)


@pytest.mark.parametrize("problem,error", [("unmatched", "未能对应"), ("wrong_grid", "网格")])
def test_invalid_raster_is_not_published(client, tmp_path, provider, problem, error):
    setattr(provider, problem, True)
    request = client.search(cloud=20)
    folder = tmp_path / "output"
    job = client.wait(client.post("download", {**request, "output_dir": str(folder)})["job_id"], status="failed")
    assert error in job["error"]
    files = list(folder.rglob("*"))
    assert not any(path.suffix in (".tif", ".part") for path in files)
    manifest = json.loads(next(folder.rglob("下载记录_*.json")).read_text(encoding="utf-8"))
    assert manifest["status"] == "failed" and manifest["items"][0]["status"] == "failed"


def test_all_masked_scene_is_skipped_not_saved(client, tmp_path, provider):
    provider.empty = True
    request = client.search(cloud=20)
    result = client.wait(client.post("download", {**request, "output_dir": str(tmp_path / "output")})["job_id"])["result"]
    assert result["empty_scenes"] == request["scene_keys"] and result["files"] == []
    assert not list(Path(result["folder"]).glob("*.tif"))


def test_cancellation_preserves_completed_scene_and_cleans_current(client, tmp_path, provider):
    provider.block_number = 2
    request = client.search()
    folder = tmp_path / "output"
    job_id = client.post("download", {**request, "output_dir": str(folder)})["job_id"]
    assert provider.trap_started.wait(4)
    client.post("disconnect", {}, status=400)
    client.post("aoi", {"clear": True}, status=400)
    client.post("cancel", {"job_id": job_id})
    assert client.wait(job_id, "cancelled")["detail"]["folder"]
    assert len(list(folder.rglob("*.tif"))) == 1
    manifest = json.loads(next(folder.rglob("下载记录_*.json")).read_text(encoding="utf-8"))
    assert manifest["status"] == "cancelled"
    assert [x["status"] for x in manifest["items"]] == ["completed", "cancelled"]


def test_native_dialog_route_is_available_only_for_desktop_and_does_not_relax_csp(client, server):
    assert not client.get("bootstrap").json()["native_dialogs"]
    client.post("dialog", {"kind": "choose_folder"}, status=404)
    chosen = []
    server.dialogs = SimpleNamespace(open=lambda request: {"path": chosen.append(request) or "C:/test"})
    assert client.get("bootstrap").json()["native_dialogs"]
    assert client.post("dialog", {"kind": "choose_folder", "current": "C:/old"}) == {"path": "C:/test"}
    assert chosen == [{"kind": "choose_folder", "current": "C:/old"}]
    csp = client.session.get(server.origin, timeout=5).headers["Content-Security-Policy"]
    assert "'unsafe-eval'" not in csp


def test_process_does_not_reuse_tampered_file(client, tmp_path, provider):
    request = {**client.search(cloud=20), "output_dir": str(tmp_path / "tamper")}
    result = client.wait(client.post("download", request)["job_id"])["result"]
    output = Path(result["files"][0]["path"])
    original = output.read_bytes()
    mutable = bytearray(original)
    mutable[-1] ^= 1
    output.write_bytes(mutable)  # same-length corruption should not be skipped
    failure = client.wait(client.post("download", request)["job_id"], status="failed")
    assert "完整性" in failure["error"]
    assert len(provider.process_calls) == 1
