"""Cancel a real public research download, restart, resume and compare with an official SAFE."""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
import time
from pathlib import Path

from shapely.geometry import box, mapping, shape

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core.download_cache import save_json
from core.local_paths import display_path, io_path
from core.planning import normalize_options
from check_mosaic_reference import NativeSources, compare_mosaic
from check_safe_reference import read_reference
from live_public import local_app


def check(executable, reference_dir, output, timeout=1800):
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    settings = output / "settings"
    assert not settings.exists(), "Use a fresh output directory for restart acceptance"
    products = [read_reference(path) for path in sorted(reference_dir.glob("*.SAFE.zip"))]
    product, = [item for item in products if item["tile"] == "T50TMK"]
    point = shape(product["footprint"]).representative_point()
    geometry = box(point.x - .04, point.y - .04, point.x + .04, point.y + .04)
    criteria = {"start": product["acquisition_utc"][:10], "end": product["acquisition_utc"][:10], "max_cloud": 100}
    options = {"mode": "research", "layout": "mosaic", "priority": "cloud", "mask_clouds": True,
               "resolution": 10, "max_cloud": criteria["max_cloud"]}
    destination = output / "original-output"
    report_path = output / "live-resume-report.json"
    report = {"status": "running", "verified_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
              "executable": str(executable), "profile": "s2-earth-search", "product": product["product_id"],
              "geometry": mapping(geometry), "network": "Real provider HTTPS; no network or data stubs",
              "interruption": "User cancellation followed by application restart", "format": "GeoTIFF and scientific companions"}
    native = NativeSources([product], normalize_options(options)["bands"])

    def save():
        save_json(report_path, report)

    class Api:
        def __init__(self, origin, session):
            self.origin, self.session = origin, session

        def get(self, action, **params):
            with self.session.get(self.origin + "/api/" + action, params=params, timeout=10) as reply:
                reply.raise_for_status()
                return reply.json()

        def post(self, action, body):
            with self.session.post(self.origin + "/api/" + action, json=body, timeout=(5, 240)) as reply:
                if reply.status_code in (400, 403):
                    raise RuntimeError(action + ": " + reply.json().get("error", "Request was rejected"))
                reply.raise_for_status()
                return reply.json()

        def wait(self, identifier, expected="completed"):
            deadline, last = time.monotonic() + timeout, 0
            while time.monotonic() < deadline:
                state = self.get("job", id=identifier)
                if state["status"] not in ("running", "cancelling"):
                    assert state["status"] == expected, state.get("error") or state["message"]
                    return state
                if time.monotonic() - last >= 15:
                    print(state["message"], flush=True)
                    last = time.monotonic()
                time.sleep(.25)
            self.post("cancel", {"job_id": identifier})
            raise RuntimeError("Acceptance timeout; cancellation requested")

        def select(self):
            area = self.post("aoi", {"geometry": mapping(geometry), "name": "真实下载恢复验证"})["geometry"]
            found = self.wait(self.post("search", {"profile": "s2-earth-search", "geometry": area, **criteria})["job_id"])["result"]
            selected, = [scene for scene in found["scenes"] if scene["title"] == product["product_id"]]
            request = {"profile": "s2-earth-search", "geometry": area, "criteria": criteria,
                       "query_id": found["query"]["id"], "scene_keys": [selected["key"]],
                       "options": options, "output_dir": str(destination)}
            return request, selected, self.post("plan", request)

    save()
    try:
        with local_app(executable, output, settings) as (origin, session):
            api = Api(origin, session)
            report["version"] = api.get("bootstrap")["version"]
            request, selected, plan = api.select()
            assert plan["width"] > 512 and plan["height"] > 512 and not plan.get("requires")
            first = api.post("download", request)["job_id"]
            started, last = time.monotonic(), 0
            while time.monotonic() - started < timeout:
                state = api.get("job", id=first)
                assert state["status"] in ("running", "cancelling"), state.get("error") or "Download finished before the cancellation point"
                cache_blocks = [path for path in io_path(destination).rglob("*.npz")
                                if "requests" not in path.parts and path.with_suffix(".json").is_file()]
                if cache_blocks:
                    break
                if time.monotonic() - last >= 15:
                    print("First transfer: " + state["message"], flush=True)
                    last = time.monotonic()
                time.sleep(.1)
            else:
                api.post("cancel", {"job_id": first})
                raise RuntimeError("No completed block before timeout; cancellation requested")
            api.post("cancel", {"job_id": first})
            cancellation_started = time.monotonic()
            paused = api.wait(first, "cancelled")
            assert paused["retry_available"]
            assert not list(io_path(destination).rglob("mosaic_research*.tif"))
            pages = [path for path in io_path(destination).rglob("*") if path.is_file()
                     and "ranges" in path.parts and path.name.isdecimal()]
            report.update(cancelled_after_completed_blocks=len(cache_blocks),
                          verified_range_pages_at_cancel=len(pages),
                          verified_cached_range_bytes_at_cancel=sum(path.stat().st_size for path in pages),
                          first_transfer_seconds=round(cancellation_started - started, 3),
                          cancellation_latency_seconds=round(time.monotonic() - cancellation_started, 3))
            save()

        with local_app(executable, output, settings) as (origin, session):
            api = Api(origin, session)
            restored = api.get("bootstrap")
            assert any(task["id"] == first for task in restored["resumable_downloads"])
            assert restored["query"] is None
            api.post("preferences", {"output_dir": str(output / "unrelated-current-output")})
            api.post("aoi", {"coordinates": "6.58,0.30,6.63,0.35"})
            resumed = api.post("retry-download", {"job_id": first})["job_id"]
            completed = api.wait(resumed)
            assert completed["previous_id"] == first and not completed["retry_available"]
            result = completed["result"]
            manifest = json.loads(io_path(result["manifest"]).read_text(encoding="utf-8"))["items"][0]
            assert manifest["reused_blocks"] >= 1
            assert Path(display_path(result["files"][0]["path"])).resolve().is_relative_to(destination.resolve())
            transfer = [row for bands in manifest.get("transport", {}).values() for row in bands.values()]
            report.update(resumed_after_restart=True, original_selection_preserved=True,
                          reused_completed_blocks=manifest["reused_blocks"],
                          resumed_cached_ranges=sum(row["cached_ranges"] for row in transfer),
                          resumed_range_requests=sum(row["range_requests"] for row in transfer),
                          resumed_network_bytes=sum(row["bytes_received"] for row in transfer),
                          manifest=result["manifest"], file=result["files"][0])
            assert report["resumed_cached_ranges"] > 0
            opts = normalize_options({**options, "max_cloud": 100})
            report["official_pixel_comparison"] = compare_mosaic(result["files"][0], geometry, [selected], opts, native)
            repeated_request, _, _ = api.select()
            repeated = api.wait(api.post("download", repeated_request)["job_id"])["result"]
            assert repeated["files"][0]["status"] == "skipped"
            assert api.get("resumable-downloads")["tasks"] == []
            assert not list(io_path(destination).rglob("*.zip"))
            report.update(completed_files_verified_and_skipped=True, status="passed")
            save()
    except Exception as error:
        report.update(status="failed", error=str(error) or type(error).__name__)
        save()
        raise
    finally:
        native.close()
    print(json.dumps({key: report[key] for key in ("status", "version", "reused_completed_blocks", "resumed_cached_ranges", "resumed_network_bytes")}), flush=True)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--exe", type=Path, required=True)
    parser.add_argument("--reference-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--timeout", type=int, default=1800)
    args = parser.parse_args()
    check(args.exe.resolve(strict=True), args.reference_dir.resolve(strict=True), args.output, args.timeout)


if __name__ == "__main__":
    main()
