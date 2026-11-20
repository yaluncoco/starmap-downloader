"""Smoke-test the Windows executable from a clean working directory, without CDSE credentials."""
from __future__ import annotations

import argparse
import json
import re
import socket
import subprocess
import tempfile
import time
from pathlib import Path

import requests


def check_executable(executable, *, set_output=None, require_output=None, require_version=None,
                     require_update_repository=None, require_no_update_channel=False, expect_update_status=None):
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    origin = f"http://127.0.0.1:{port}"
    launch = subprocess.STARTUPINFO()
    launch.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    with tempfile.TemporaryDirectory(prefix="starmap-package-") as folder, requests.Session() as session:
        session.trust_env = False
        temporary = Path(folder)
        process = subprocess.Popen([executable, "--server", "--port", str(port)], cwd=temporary, startupinfo=launch)
        try:
            for _ in range(100):
                if process.poll() is not None:
                    raise RuntimeError(f"Package exited with code {process.returncode}")
                try:
                    if session.get(origin + "/health", timeout=.4).json()["ok"]:
                        break
                except requests.exceptions.RequestException:
                    time.sleep(.125)
            else:
                raise RuntimeError("Package did not start a local server")
            home = session.get(origin, timeout=3)
            token = re.search(r'<meta name="app-token" content="([^"<>]+)"', home.text)[1]
            session.headers["X-App-Token"] = token
            bootstrap = session.get(origin + "/api/bootstrap", timeout=3).json()
            version = bootstrap["version"]
            repository = bootstrap.get("update_repository")
            if require_version is not None:
                assert version == require_version, f"Expected version {require_version}, got {version}"
            if require_update_repository is not None:
                assert repository == require_update_repository, "Build-pinned GitHub update repository mismatch"
            if require_no_update_channel:
                assert "update_repository" in bootstrap and repository is None, "Expected an unconfigured update channel"
            if require_output is not None:
                assert Path(bootstrap["preferences"]["output_dir"]) == require_output, "Existing output preference was not loaded"
            for asset in ("/app.js", "/styles.css", "/vendor/leaflet/leaflet.js", "/vendor/leaflet-draw/leaflet.draw.js"):
                session.get(origin + asset, timeout=3).raise_for_status()
            boundary = {"type": "Polygon", "coordinates": [[[444430, 4416850], [444870, 4416850],
                                                           [444870, 4417420], [444430, 4417420], [444430, 4416850]]]}

            def call(route, data, timeout=4):
                result = session.post(origin + "/api/" + route, json=data, timeout=timeout)
                result.raise_for_status()
                return result.json()

            if set_output is not None:
                call("preferences", {"output_dir": str(set_output)})
            area = call("aoi", {"geometry": boundary, "source_crs": "EPSG:32650"})
            assert 116.34 < area["center"][0] < 116.36 and 39.89 < area["center"][1] < 39.91
            path = temporary / "验证输出" / "边界.shp"
            call("export-aoi", {"geometry": area["geometry"], "path": str(path)})
            imported = call("aoi", {"path": str(path)})
            assert imported["bounds"] == area["bounds"]
            check = {"packaged_version": version, "loopback_resources": "ok", "projected_shapefile_roundtrip": "ok"}
            if "update_repository" in bootstrap:
                check["update_repository"] = repository
            if expect_update_status is not None:
                update = call("check-update", {}, timeout=30)
                assert update["status"] == expect_update_status, f"Unexpected update result: {update}"
                if expect_update_status == "current":
                    assert update["latest_version"] == version, "The published GitHub release is not this build"
                check["update_status"] = update["status"]
            print(json.dumps(check))
        finally:
            if process.poll() is None:
                process.terminate()
            process.wait(timeout=5)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("exe", type=Path)
    parser.add_argument("--set-output", type=Path, help="Save a known output path in isolated test preferences")
    parser.add_argument("--require-output", type=Path, help="Assert an existing output preference was loaded")
    parser.add_argument("--require-version")
    channel = parser.add_mutually_exclusive_group()
    channel.add_argument("--require-update-repository")
    channel.add_argument("--require-no-update-channel", action="store_true")
    parser.add_argument("--expect-update-status", choices=("unconfigured", "not_published", "current", "available"))
    args = parser.parse_args()
    check_executable(args.exe.resolve(strict=True), set_output=args.set_output, require_output=args.require_output,
                     require_version=args.require_version, require_update_repository=args.require_update_repository,
                     require_no_update_channel=args.require_no_update_channel, expect_update_status=args.expect_update_status)
