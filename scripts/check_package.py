"""Smoke-test the Windows executable from a clean working directory, without CDSE credentials."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import socket
import subprocess
import tempfile
import time
from pathlib import Path

import requests
from shapely.geometry import shape


def check_executable(executable, *, set_output=None, require_output=None, require_version=None,
                     require_update_repository=None, require_no_update_channel=False, expect_update_status=None, require_gedi=False, require_batch=False, require_accounts=False, require_monthly=False, require_safe_zip=False):
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    origin = f"http://127.0.0.1:{port}"
    launch = subprocess.STARTUPINFO()
    launch.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    with tempfile.TemporaryDirectory(prefix="starmap-package-") as folder, requests.Session() as session:
        session.trust_env = False
        temporary = Path(folder)
        environment = os.environ.copy()
        if set_output is None and require_output is None:
            environment["LOCALAPPDATA"] = str(temporary / "AppData")
        process = subprocess.Popen([executable, "--server", "--port", str(port)], cwd=temporary, startupinfo=launch, env=environment)
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
            if require_safe_zip:
                assert home.text.index('value="s2-cdse-zip"') < home.text.index('value="s2-process"')
                assert 'id="zipProcessing"' in home.text and 'id="installUpdate"' in home.text
                app_script = session.get(origin + "/app.js", timeout=3).text
                assert 'profile:"s2-cdse-zip"' in app_script and 'api("install-update"' in app_script
                rejected = session.post(origin + "/api/install-update", json={"tag": "v99.9.0"}, timeout=4)
                assert rejected.status_code == 400 and "先下载并校验" in rejected.json()["error"]
            boundary = {"type": "Polygon", "coordinates": [[[444430, 4416850], [444870, 4416850],
                                                           [444870, 4417420], [444430, 4417420], [444430, 4416850]]]}

            def call(route, data, timeout=4):
                result = session.post(origin + "/api/" + route, json=data, timeout=timeout)
                result.raise_for_status()
                return result.json()

            accounts_checked = False
            if require_accounts or "accounts" in bootstrap:
                account_status = session.get(origin + "/api/accounts", timeout=3).json()
                assert set(account_status["providers"]) == {"oauth", "odata", "s3", "earthdata"}
                assert 'src="accounts.js"' in home.text and 'id="accountComplete"' in home.text
                assert 'id="manageAccountsSidebar"' not in home.text and 'id="loginBeforeBatch"' not in home.text
                session.get(origin + "/accounts.js", timeout=3).raise_for_status()
                if set_output is None and require_output is None:
                    assert account_status["login_required"] and not account_status["has_saved_accounts"]
                    assert not account_status["storage_error"]
                accounts_checked = True
            gedi_checked = False
            if require_gedi or "gedi_collections" in bootstrap:
                collections = bootstrap.get("gedi_collections")
                expected = {"gedi01-b-003", "gedi01-b-002", "gedi02-a-003", "gedi02-a-002",
                            "gedi02-b-003", "gedi02-b-002", "gedi03-003", "gedi03-002",
                            "gedi04-a-003", "gedi04-a-021", "gedi04-b-021", "gedi04-c-003", "gedi04-c-002"}
                assert isinstance(collections, list) and len(collections) == len(expected)
                assert {item["id"] for item in collections} == expected, "Packaged GEDI collections are incomplete"
                assert bootstrap.get("gedi_default") == "gedi02-a-003"
                had_saved_earthdata = bootstrap.get("accounts", {}).get("providers", {}).get("earthdata", {}).get("saved", False)
                if not had_saved_earthdata:
                    assert bootstrap.get("earthdata_connected") is False
                invalid = session.post(origin + "/api/earthdata-connect", json={"token": "short"}, timeout=4)
                assert invalid.status_code == 400 and "Earthdata" in invalid.json()["error"]
                if not had_saved_earthdata:
                    connected = call("earthdata-connect", {"token": "synthetic-package-check-earthdata-token"})
                    assert connected["connected"] is True and connected["verified"] is False, "Local token state mismatch"
                    if accounts_checked:
                        assert connected["saved"] is True
                    assert session.get(origin + "/api/bootstrap", timeout=3).json()["earthdata_connected"] is True
                    assert call("earthdata-disconnect", {})["connected"] is False
                    assert session.get(origin + "/api/bootstrap", timeout=3).json()["earthdata_connected"] is False
                gedi_checked = True
            if set_output is not None:
                call("preferences", {"output_dir": str(set_output)})
            area = call("aoi", {"geometry": boundary, "source_crs": "EPSG:32650"})
            assert 116.34 < area["center"][0] < 116.36 and 39.89 < area["center"][1] < 39.91
            path = temporary / "验证输出" / "边界.shp"
            call("export-aoi", {"geometry": area["geometry"], "path": str(path)})
            imported = call("aoi", {"path": str(path)})
            assert imported["bounds"] == area["bounds"]
            monthly_checked = False
            if require_monthly or "monthly" in bootstrap:
                for asset in ("/cities.js", "/monthly.js", "/map.js", "/workflow.css"):
                    session.get(origin + asset, timeout=3).raise_for_status()
                assert 'id="cityChoice"' in home.text and 'id="startMonth"' in home.text
                assert 'id="cancelTaskbar"' in home.text and 'id="retryBasemap"' in home.text
                catalogue_response = session.get(origin + "/api/cities", timeout=5)
                catalogue_response.raise_for_status()
                catalogue = catalogue_response.json()
                assert len(catalogue["cities"]) == 371 and len(catalogue["provinces"]) == 34
                assert catalogue["source"]["crs"] == "EPSG:4326"
                city = call("aoi", {"city_id": "110000"}, timeout=10)
                assert city["name"] == "北京市" and city["vertices"] > 14000
                assert 16000 < city["area_km2"] < 17000 and city["region"]["city_id"] == "110000"
                city_path = temporary / "验证输出" / "北京市完整行政边界.shp"
                call("export-aoi", {"city_id": "110000", "path": str(city_path)}, timeout=10)
                roundtrip = call("aoi", {"path": str(city_path)}, timeout=10)
                assert roundtrip["vertices"] == city["vertices"] and roundtrip["bounds"] == city["bounds"]
                assert shape(roundtrip["geometry"]).equals(shape(city["geometry"]))
                basemap = session.get(origin + "/map/admin-basemap.geojson", timeout=5)
                basemap.raise_for_status()
                assert basemap.json()["type"] == "FeatureCollection" and len(basemap.json()["features"]) == 34
                rejected = session.post(origin + "/api/monthly-download", json={"output_dir": str(temporary / "monthly-no-output")}, timeout=4)
                assert rejected.status_code == 400 and "下载计划" in rejected.json()["error"]
                assert not (temporary / "monthly-no-output").exists()
                monthly_checked = True
            batch_checked = False
            if require_batch or "batch_sources" in bootstrap:
                sources = bootstrap.get("batch_sources")
                expected_sources = {"s2-process", "s2-earth-search", "s2-l1c-aws", "s2-l2a-cog", "s2-l3-quarterly", "landsat-pc-l2",
                                    "s1-aws-original", "s1-slc-seasia", "s1-rtc-pc", "sen3-aws-original", "s5p-aws-l2",
                                    "worldcover-s2", "worldcover-s1", "worldcover-map", "dem-30", "dem-90", "gedi-earthdata", "cdse-original",
                                    "modis-13Q1-061", "modis-09Q1-061", "modis-11A2-061", "modis-15A2H-061", "modis-43A4-061"}
                if tuple(map(int, bootstrap["version"].split("."))) >= (1, 5, 0):
                    expected_sources.add("s2-cdse-s3")
                    session.get(origin + "/spatial.js", timeout=3).raise_for_status()
                    assert 'value="s2-cdse-s3"' in home.text
                if tuple(map(int, bootstrap["version"].split("."))) >= (1, 9, 0):
                    expected_sources.add("s2-cdse-zip")
                assert isinstance(sources, list) and {s["id"] for s in sources} == expected_sources
                assert bootstrap.get("batch") is None
                assert 'id="manageAccounts"' in home.text and 'id="companionSources"' in home.text
                for asset in ("/batch.js", "/assets/globe-orbit.svg", "/assets/globe-orbit.ico", "/assets/globe-orbit.png"):
                    session.get(origin + asset, timeout=3).raise_for_status()
                svg = session.get(origin + "/assets/globe-orbit.svg", timeout=3).content
                assert hashlib.sha256(svg).hexdigest() == "ff0ca0015bcdb3f73441fbc3719bd8fd2f727800ad87ed2b73f353e50d9b8581"
                rejected = session.post(origin + "/api/batch-search", json={"profile": "s2-earth-search", "geometry": area["geometry"],
                                        "start": "2024-06-10", "end": "2024-06-10", "shared_dates": {"start": "2024-06-10", "end": "2024-06-10"},
                                        "companions": []}, timeout=4)
                assert rejected.status_code == 400 and "附带" in rejected.json()["error"]
                for route in ("batch-plan", "batch-download"):
                    rejected = session.post(origin + "/api/" + route, json={"output_dir": str(temporary / "batch-no-output")}, timeout=4)
                    assert rejected.status_code == 400 and "联合检索" in rejected.json()["error"]
                assert not (temporary / "batch-no-output").exists()
                batch_checked = True
            check = {"packaged_version": version, "loopback_resources": "ok", "projected_shapefile_roundtrip": "ok"}
            if accounts_checked:
                check["account_platforms_and_startup_contract"] = "ok (Windows encrypted saving, no real credentials)"
            if batch_checked:
                check["batch_sources"] = len(sources)
                check["batch_routes_and_icon"] = "ok"
            if gedi_checked:
                check["gedi_collections"] = len(collections)
                check["earthdata_local_routes"] = "ok (synthetic token, no NASA authentication)"
            if monthly_checked:
                check["city_catalogue"] = len(catalogue["cities"])
                check["monthly_resources_full_city_shp_and_offline_basemap"] = "ok"
            if require_safe_zip:
                check["default_safe_zip_and_install_retry_resources"] = "ok"
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
    parser.add_argument("--require-gedi", action="store_true", help="Require GEDI collections and local authentication routes")
    parser.add_argument("--require-batch", action="store_true", help="Require companion sources, batch routes and the supplied icon")
    parser.add_argument("--require-accounts", action="store_true", help="Require centralized accounts, encrypted saving and startup state")
    parser.add_argument("--require-monthly", action="store_true", help="Require full city boundaries, monthly controls and the offline map")
    parser.add_argument("--require-safe-zip", action="store_true", help="Require default whole SAFE ZIP and automatic installer resources")
    channel = parser.add_mutually_exclusive_group()
    channel.add_argument("--require-update-repository")
    channel.add_argument("--require-no-update-channel", action="store_true")
    parser.add_argument("--expect-update-status", choices=("unconfigured", "not_published", "current", "available"))
    args = parser.parse_args()
    check_executable(args.exe.resolve(strict=True), set_output=args.set_output, require_output=args.require_output,
                     require_version=args.require_version, require_update_repository=args.require_update_repository,
                     require_no_update_channel=args.require_no_update_channel, expect_update_status=args.expect_update_status,
                     require_gedi=args.require_gedi, require_batch=args.require_batch, require_accounts=args.require_accounts,
                     require_monthly=args.require_monthly, require_safe_zip=args.require_safe_zip)
