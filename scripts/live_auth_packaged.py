"""Check three CDSE routes through the Windows executable. Authentication prompts are local and hidden."""
from __future__ import annotations

import argparse
import datetime as dt
import getpass
import hashlib
import json
import sys
import tempfile
import time
import warnings
from pathlib import Path

import rasterio

from live_public import B04_SAFE, BBOX, DEM90, local_app


def login(service, odata_token):
    with warnings.catch_warnings():
        warnings.simplefilter("error", getpass.GetPassWarning)
        if service == "process":
            return "connect", {"client_id": getpass.getpass("Sentinel Hub Client ID: "),
                               "client_secret": getpass.getpass("Sentinel Hub Client Secret: ")}
        if service == "s3":
            return "s3-connect", {"access_key": getpass.getpass("CDSE S3 Access Key: "),
                                  "secret_key": getpass.getpass("CDSE S3 Secret Key: ")}
        if odata_token:
            return "raw-connect", {"method": "token", "token": getpass.getpass("CDSE OData access token: ")}
        return "raw-connect", {"method": "account", "username": getpass.getpass("CDSE email: "),
                               "password": getpass.getpass("CDSE password: "),
                               "totp": getpass.getpass("Current TOTP (leave empty if disabled): ")}


def check(exe, out, service, odata_token, *, preflight=False):
    out.mkdir(exist_ok=True, parents=True)
    if preflight and any(out.iterdir()):
        raise RuntimeError("Use a fresh output folder for the credential-free preflight.")
    with tempfile.TemporaryDirectory(prefix="starmap-test-settings-") as settings, local_app(exe, out, settings) as (origin, session):
        def post(route, body, expected=200):
            response = session.post(f"{origin}/api/{route}", json=body, timeout=(5, 180))
            if response.status_code != expected:
                detail = response.json().get("error") if not response.ok else None
                raise RuntimeError(f"Expected HTTP {expected} from {route}, got {response.status_code}" + (f": {detail}" if detail else ""))
            return response.json()

        def run(route, body, wait=360):
            job = post(route, body)["job_id"]
            deadline = time.monotonic() + wait
            while time.monotonic() < deadline:
                response = session.get(f"{origin}/api/job", params={"id": job}, timeout=5)
                response.raise_for_status()
                state = response.json()
                if state["status"] == "completed":
                    return state["result"]
                if state["status"] not in ("running", "cancelling"):
                    raise RuntimeError(state.get("error") or state["message"])
                time.sleep(.5)
            post("cancel", {"job_id": job})
            raise RuntimeError(f"Timed out during {route}; a fresh directory is needed to retry verification.")

        if not preflight:
            route, private = login(service, odata_token)
            try:
                post(route, private)
            finally:
                private.clear()
        area = post("aoi", {"coordinates": BBOX})["geometry"]
        boot = session.get(f"{origin}/api/bootstrap", timeout=5).json()
        version = boot["version"]
        if preflight:
            assert not any(boot[key] for key in ("connected", "raw_connected", "s3_connected"))

        def preflight_result(route, body, label, *, product=None, plan=None):
            rejected = post(route, body, expected=400)
            assert label in rejected["error"] and "job_id" not in rejected
            assert not any(out.iterdir()), "A rejected operation must not write output files."
            proof = {"verified_utc": dt.datetime.now(dt.timezone.utc).isoformat(), "test_mode": "preflight", "service": service,
                     "version": version, "product": product, "requires": plan["requires"] if plan else ["oauth"], "rejected_operation": route,
                     "http_status": 400, "reason": rejected["error"], "credentials_used": False, "original_bytes_downloaded": False}
            (out / f"auth-preflight-{service}.json").write_text(json.dumps(proof, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            return proof

        dates = {"start": "2024-06-10", "end": "2024-06-10"}
        if service == "process":
            profile, criteria = "s2-process", {**dates, "max_cloud": 100}
            options = {"mode": "custom", "bands": ["B04"], "resolution": 20, "max_cloud": 100, "mask_clouds": False}
        elif service == "odata":
            profile, criteria = "cdse-original", {**dates, "collection": "sentinel-2-l2a", "radar_mode": ""}
            options = {"mode": "metadata", "transport": "odata"}
        else:
            profile, criteria = "cdse-original", {"start": None, "end": None, "collection": "cop-dem-glo-90-dged-cog", "radar_mode": ""}
            options = {"mode": "original", "transport": "cdse-s3"}
        search = {"profile": profile, "geometry": area}
        if preflight and service == "process":
            return preflight_result("search", {**search, **criteria}, "请先连接 Sentinel Hub OAuth")
        found = run("search", {**search, **criteria})
        if service == "process":
            matched = [row for row in found["scenes"] if {p["id"].removesuffix(".SAFE") for p in row["products"]} == {B04_SAFE.removesuffix(".SAFE")}]
        else:
            expected = DEM90 if service == "s3" else B04_SAFE.removesuffix(".SAFE")
            matched = [row for row in found["scenes"] if row["key"] == expected]
        if len(matched) != 1:
            raise RuntimeError("The exact fixed test product is not available. No nearby product was substituted.")
        request = {**search, "criteria": criteria, "query_id": found["query"]["id"], "scene_keys": [matched[0]["key"]],
                   "options": options, "output_dir": str(out)}
        plan = post("plan", request)
        if service != "process":
            wanted = "cdse-s3" if service == "s3" else "odata"
            assert plan["requires"] == [wanted] and plan["transports"] == [wanted]
        if preflight:
            label = "请先连接 CDSE OData" if service == "odata" else "请先连接 CDSE S3"
            return preflight_result("download", request, label, product=matched[0]["title"], plan=plan)
        result = run("download", request)
        file, = result["files"]
        assert file["status"] == "completed", "Use a fresh output folder to confirm real bytes were downloaded."
        path = Path(file["path"])
        row, = json.loads(Path(result["manifest"]).read_text(encoding="utf-8"))["items"]
        with path.open("rb") as handle:
            digest = hashlib.file_digest(handle, "sha256").hexdigest()
        assert digest == row["sha256"]
        if service == "process":
            with rasterio.open(path) as ds:
                assert ds.descriptions == ("B04",) and ds.res == (20.0, 20.0) and ds.crs.to_epsg() == 32650
                assert ds.read(1, masked=True).count() > 0
        elif service == "s3":
            with rasterio.open(path) as ds:
                assert ds.crs.to_epsg() == 4326 and ds.shape == (1200, 1200) and ds.dtypes == ("float32",)
        else:
            assert row["product"] == B04_SAFE.removesuffix(".SAFE") and path.suffix in (".xml", ".safe")
        proof = {"verified_utc": dt.datetime.now(dt.timezone.utc).isoformat(), "test_mode": service, "version": version,
                 "product": matched[0]["title"], "output": str(path), "bytes": path.stat().st_size, "sha256": digest,
                 "receipt": result["manifest"], "provider_checksum_verified": row.get("provider_checksum_verified")}
        (out / f"live-auth-{service}.json").write_text(json.dumps(proof, indent=2, ensure_ascii=False)+"\n", encoding="utf-8")
        return proof


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--exe", type=Path, required=True, help="Path to installed/portable StarmapDownloader.exe (not the setup file)")
    parser.add_argument("--service", choices=("process", "odata", "s3"), required=True)
    parser.add_argument("--odata-token", action="store_true", help="Paste a token rather than a password at the hidden OData prompt")
    parser.add_argument("--preflight", action="store_true", help="Check missing-credential handling only; never prompt or fetch product bytes")
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    if args.odata_token and args.service != "odata":
        parser.error("--odata-token is only for --service odata")
    if args.odata_token and args.preflight:
        parser.error("--preflight cannot be combined with --odata-token")
    if not args.preflight and not sys.stdin.isatty():
        parser.error("Run in a local interactive terminal. Do not put secrets in commands or messages.")
    try:
        proof = check(args.exe.resolve(strict=True), args.output_dir.resolve(), args.service, args.odata_token, preflight=args.preflight)
    except (RuntimeError, OSError, ValueError, AssertionError, getpass.GetPassWarning) as error:
        print(f"Verification did not complete: {error}", file=sys.stderr)
        return 1
    print(json.dumps(proof, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    raise SystemExit(main())
