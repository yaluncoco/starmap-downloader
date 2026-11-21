"""GEDI protocol, science fields, exact image grids and full local API workflow."""
from __future__ import annotations

import csv
import datetime as dt
import copy
import hashlib
import io
import json
import threading
from pathlib import Path

import h5py
import numpy as np
import pytest
import rasterio
from pyproj import Transformer
from rasterio.transform import from_origin
from shapely import transform
from shapely.geometry import Polygon, box, mapping

from core.earthdata_auth import EDL_TOKEN_URL, EarthdataAuth, data_url, request_data
from core.errors import UserError
from core.gedi_catalogue import CMR_URL, COLLECTIONS, GEDI, granule_scene, search_gedi
from core.gedi_download import _rows, build_gedi_plan, normalize_options
from core.http import HttpClient, check_cancel
from core.planning import build_plan, normalize_options as optical_options
from conftest import StubResponse

FIXTURES = Path(__file__).parent / "fixtures" / "gedi"
TOKEN = "synthetic-earthdata-token-for-tests"
UTC = dt.timezone.utc
BASE_NUMBER = 10000000000000001
KEY = "gedi02-a-003"


def real_item(name):
    return json.loads((FIXTURES / (name + ".json")).read_text(encoding="utf-8"))["items"][0]


def reference_and_aoi(tmp_path):
    reference = tmp_path / "此前下载的影像.tif"
    x, y = Transformer.from_crs(4326, 32650, always_xy=True).transform(116.35, 39.9)
    affine = from_origin(x - 23.7, y + 31.4, 20, 20)
    values = np.arange(30, dtype="float32").reshape(5, 6)
    values[0, 0] = -9999
    with rasterio.open(reference, "w", driver="GTiff", width=6, height=5, count=1,
                       dtype="float32", crs="EPSG:32650", transform=affine, nodata=-9999) as raster:
        raster.write(values, 1)
        raster.scales, raster.offsets = (0.01,), (-1,)
        raster.set_band_description(1, "已有光学波段")
    ring = [(affine.a * xy[0] + affine.b * xy[1] + affine.c, affine.d * xy[0] + affine.e * xy[1] + affine.f) for xy in [(0, 0), (6, 0), (6, 5), (0, 5), (0, 0)]]
    hole = [(affine.a * xy[0] + affine.b * xy[1] + affine.c, affine.d * xy[0] + affine.e * xy[1] + affine.f) for xy in [(2.1, 2.1), (2.9, 2.1), (2.9, 2.9), (2.1, 2.9), (2.1, 2.1)]]
    project = Transformer.from_crs(32650, 4326, always_xy=True)
    geometry = transform(Polygon(ring, [hole]), project.transform, interleaved=False)
    return reference, affine, geometry


def h5_bytes(key, affine):
    spec = COLLECTIONS[key]
    coords = [(1.5, 1.5), (1.7, 1.6), (4.5, 3.5), (3.5, 2.5), (0.5, 3.5),
              (4.5, 1.5), (2.5, 2.5), (10.5, 10.5), (5.5, 1.5), (0.5, 0.5)]
    project = Transformer.from_crs(32650, 4326, always_xy=True)
    lon, lat = project.transform(*zip(*((affine.a * xy[0] + affine.b * xy[1] + affine.c, affine.d * xy[0] + affine.e * xy[1] + affine.f) for xy in coords)))
    epoch = dt.datetime(2018, 1, 1, tzinfo=UTC)
    delta = np.full(10, (dt.datetime(2024, 6, 10, 12, tzinfo=UTC) - epoch).total_seconds())
    delta[5] = (dt.datetime(2024, 6, 11, tzinfo=UTC) - epoch).total_seconds()
    delta[8] = (dt.datetime(2024, 6, 9, 23, 59, 59, tzinfo=UTC) - epoch).total_seconds()
    quality = np.ones(10, dtype="uint8")
    quality[3] = 0
    degraded = np.zeros(10, dtype="uint8")
    degraded[4] = 2
    out = io.BytesIO()
    with h5py.File(out, "w") as file:
        group = file.create_group("BEAM0101")
        group["shot_number"] = np.arange(BASE_NUMBER, BASE_NUMBER + 10, dtype="uint64")
        group["ancillary/master_time_epoch"] = [1198800018.0]
        prefix = "geolocation/" if spec["level"] in ("L1B", "L2B") else ""
        group[prefix + "delta_time"] = delta
        group[prefix + ("longitude_bin0" if spec["level"] == "L1B" else "lon_lowestmode")] = lon
        group[prefix + ("latitude_bin0" if spec["level"] == "L1B" else "lat_lowestmode")] = lat
        group["geolocation/degrade" if spec["level"] == "L1B" else "degrade_flag"] = degraded
        for name in spec["quality_paths"]:
            group[name] = 1 - quality if spec["quality_value"] == 0 else quality
        if "quality_flag" not in group:
            group["quality_flag"] = 1 - quality  # Trap using an obsolete field on V003.
        group["sensitivity"] = np.r_[np.full(9, 0.98), 0.4].astype("float32")
        rh = np.zeros((10, 101), dtype="float32")
        rh[:, 95] = [10, 20, -9999, 40, 50, 60, 70, 80, 90, 0]
        rh[:, 98] = [12, 22, 30, 42, 52, 62, 72, 82, 92, 0]
        group["rh"] = rh
        for name in ["elev_lowestmode", "elev_highestreturn", "cover", "pai", "fhd_normal",
                     "agbd", "agbd_se", "agbd_pi_lower", "agbd_pi_upper", "wsci", "wsci_pi_lower", "wsci_pi_upper"]:
            group[name] = np.arange(10, dtype="float32")
        profile = np.arange(30, dtype="float32")[None, :] + np.arange(10, dtype="float32")[:, None] * 100
        group["cover_z"] = profile
        group["pai_z"] = profile.T  # The documented HT, MT layout must also work.
        group["pavd_z"] = profile.T
        group["geophys_corr/geoid"] = np.full(10, 3.0)
        group["geolocation/elevation_bin0"] = np.full(10, 150.0)
        group["geolocation/elevation_lastbin"] = np.full(10, 50.0)
        group["geolocation/solar_elevation"] = np.full(10, 35.0)
        for prefix, samples in [("rx", 6), ("tx", 2)]:
            group[prefix + "_sample_count"] = np.full(10, samples, dtype="uint16")
            group[prefix + "_sample_start_index"] = np.arange(10, dtype="uint64") * samples + 1
            group[prefix + "waveform"] = np.arange(10 * samples, dtype="float32")
    return out.getvalue()


def synthetic_item(key, content, geometry, *, filename=None):
    spec = COLLECTIONS[key]
    level = {"L1B": "01_B", "L2A": "02_A", "L2B": "02_B", "L4A": "04_A", "L4C": "04_C"}.get(spec["level"], "04_B")
    filename = filename or f"GEDI{level}_2024161120000_O00001_01_T00001_02_003_01_V003.h5"
    host = "data.lpdaac.earthdatacloud.nasa.gov" if spec["level"] in ("L1B", "L2A", "L2B") else "data.ornldaac.earthdata.nasa.gov"
    url = f"https://{host}/protected/{filename}"
    return {"meta": {"concept-id": "G9999999999-LPCLOUD", "revision-id": 7, "collection-concept-id": spec["concept_id"]},
            "umm": {"GranuleUR": filename.removesuffix(".h5"), "CollectionReference": {"ShortName": spec["short_name"], "Version": spec["version"]},
                    "TemporalExtent": {"RangeDateTime": {"BeginningDateTime": "2024-06-10T00:00:00Z", "EndingDateTime": "2024-06-11T00:00:00Z"}},
                    "RelatedUrls": [{"Type": "GET DATA", "URL": url}],
                    "SpatialExtent": {"HorizontalSpatialDomain": {"Geometry": {"BoundingRectangles": [dict(zip(["WestBoundingCoordinate", "SouthBoundingCoordinate", "EastBoundingCoordinate", "NorthBoundingCoordinate"], geometry.bounds))]}}},
                    "DataGranule": {"ArchiveAndDistributionInformation": [{"Name": filename, "SizeInBytes": len(content), "Checksum": {"Algorithm": "SHA-512", "Value": hashlib.sha512(content).hexdigest()}}]}}}


class GediProvider:
    def __init__(self, item, content):
        self.item, self.content = item, content
        self.calls = []
        self.status = 200
        self.block = False
        self.started = threading.Event()

    @property
    def downloads(self):
        return [row for row in self.calls if "amazonaws.com" in row[1]]

    def request(self, method, url, *, cancel=None, **kwargs):
        check_cancel(cancel)
        self.calls.append((method, url, kwargs))
        if url == EDL_TOKEN_URL:
            assert kwargs["auth"] == ("synthetic-user", "synthetic-password")
            return StubResponse(data={"access_token": TOKEN})
        if url == CMR_URL:
            assert "Authorization" not in kwargs.get("headers", {})
            assert kwargs["params"]["collection_concept_id"] == self.item["meta"]["collection-concept-id"]
            return StubResponse(data={"items": [self.item]})
        if url == self.item["umm"]["RelatedUrls"][0]["URL"]:
            assert kwargs["headers"]["Authorization"] == "Bearer " + TOKEN
            return StubResponse(status=self.status if self.status != 200 else 302,
                                headers={"Location": "https://lp-prod-protected.s3.us-west-2.amazonaws.com/test-file?X-Amz-Signature=synthetic-signature"})
        assert "amazonaws.com" in url
        assert "Authorization" not in kwargs.get("headers", {})
        response = StubResponse(content=self.content, headers={"Content-Length": str(len(self.content)), "ETag": '"test-object-version"'})
        if self.block:
            def body(_):
                yield self.content[:100]
                self.started.set()
                assert cancel.wait(5), "Cancellation not requested"
                check_cancel(cancel)
            response.iter_content = body
        return response


@pytest.fixture
def gedi_setup(monkeypatch, tmp_path):
    reference, affine, geometry = reference_and_aoi(tmp_path)
    content = h5_bytes(KEY, affine)
    upstream = GediProvider(synthetic_item(KEY, content, geometry), content)
    monkeypatch.setattr(HttpClient, "request", lambda _, *args, **kwargs: upstream.request(*args, **kwargs))
    return upstream, reference, geometry


def search_request(client, geometry, key=KEY):
    area = client.post("aoi", {"geometry": mapping(geometry)})
    criteria = {"start": "2024-06-10", "end": "2024-06-10", "collection": key}
    response = client.post("search", {"profile": GEDI, "geometry": area["geometry"], **criteria})
    search = client.wait(response["job_id"])["result"]
    return {"profile": GEDI, "geometry": area["geometry"], "query_id": search["query"]["id"],
            "criteria": criteria, "scene_keys": [s["key"] for s in search["scenes"]],
            "options": {"mode": "aligned", "fields": ["rh95", "rh98"]}}


@pytest.mark.parametrize("fixture,key,algorithm", [
    ("l1b-003", "gedi01-b-003", "SHA-512"), ("l2a-003", KEY, "SHA-512"),
    ("l2b-003", "gedi02-b-003", "SHA-512"), ("l4a-003", "gedi04-a-003", "SHA-512"),
    ("l4c-003", "gedi04-c-003", "SHA-512"), ("l3-003", "gedi03-003", "SHA-256"),
    ("l4b-021", "gedi04-b-021", "SHA-256"),
    ("l2a-002", "gedi02-a-002", None), ("l2b-002", "gedi02-b-002", None),
    ("l4a-021", "gedi04-a-021", "SHA-256"), ("l4c-002", "gedi04-c-002", "SHA-256"),
])
def test_current_official_cmr_metadata(fixture, key, algorithm):
    scene = granule_scene(real_item(fixture), key)
    assert scene["gedi"]["available"] and scene["gedi"]["estimated_bytes"] > 10000
    assert scene["gedi"]["checksum"]["algorithm"] == algorithm if algorithm else scene["gedi"]["checksum"] is None
    assert scene["coverage_percent"] is None  # Bounding polygons are not valid-shot coverage.
    assert scene["gedi"]["name"].endswith(".tif" if COLLECTIONS[key]["kind"] == "raster" else ".h5")


def test_collection_versions_and_unsafe_url_rejected():
    item = real_item("l2a-003")
    with pytest.raises(UserError, match="版本"):
        granule_scene(item, "gedi02-a-002")
    for url in ["http://data.lpdaac.earthdatacloud.nasa.gov/file.h5", "https://evil.example/file.h5",
                "https://data.lpdaac.earthdatacloud.nasa.gov:wrong/file.h5", "https://other-bucket.s3.us-west-2.amazonaws.com/file"]:
        with pytest.raises(UserError):
            data_url(url, redirect=True)


def test_earthdata_redirect_never_sends_token_to_s3(monkeypatch):
    auth = EarthdataAuth()
    auth.connect({"token": TOKEN})
    calls = []
    first = StubResponse(status=302, headers={"Location": "https://lp-prod-protected.s3.us-west-2.amazonaws.com/object?X-Amz-Signature=fake"})
    last = StubResponse(content=b"data")
    def request(_, method, url, **kwargs):
        calls.append(kwargs["headers"])
        return first if len(calls) == 1 else last
    monkeypatch.setattr(HttpClient, "request", request)
    response = request_data(HttpClient(), auth, "https://data.lpdaac.earthdatacloud.nasa.gov/object")
    assert response is last and first.closed
    assert calls[0]["Authorization"] == "Bearer " + TOKEN and "Authorization" not in calls[1]
    first.headers["Location"] = "https://evil.example/collect"
    calls.clear()
    with pytest.raises(UserError, match="凭据"):
        request_data(HttpClient(), auth, "https://data.lpdaac.earthdatacloud.nasa.gov/object")
    assert len(calls) == 1


ORNL_CDN = ("https://d3o6w55j8uz1ro.cloudfront.net/s3-" + "a" * 32 +
            "/ornl-cumulus-prod-protected.s3.us-west-2.amazonaws.com/gedi/file.h5"
            "?Expires=2000000000&Signature=" + "s" * 64 + "&Key-Pair-Id=KEXAMPLE12&A-userid=synthetic")


def test_ornl_signed_cdn_redirect_downloads_without_earthdata_token(monkeypatch):
    auth = EarthdataAuth()
    auth.connect({"token": TOKEN})
    calls = []
    redirect = StubResponse(status=303, headers={"Location": ORNL_CDN})
    final = StubResponse(content=b"data")

    def request(_, method, url, **kwargs):
        calls.append((url, kwargs["headers"]))
        return redirect if len(calls) == 1 else final

    monkeypatch.setattr(HttpClient, "request", request)
    http = HttpClient()
    try:
        assert request_data(http, auth, "https://data.ornldaac.earthdata.nasa.gov/object") is final
        assert calls[0][1]["Authorization"] == "Bearer " + TOKEN
        assert calls[1][0] == ORNL_CDN and "Authorization" not in calls[1][1] and redirect.closed
    finally:
        http.close()


@pytest.mark.parametrize("url", [
    ORNL_CDN.replace("d3o6w55j8uz1ro", "other"),
    ORNL_CDN.replace("ornl-cumulus-prod-protected", "other-bucket"),
    ORNL_CDN.replace("/gedi/file", "/%2e%2e/file"),
    ORNL_CDN.replace("Expires=2000000000", "Expires=invalid"),
    ORNL_CDN + "&Expires=2000000001",
    ORNL_CDN + "&unknown=parameter",
    ORNL_CDN.split("?")[0],
])
def test_ornl_cdn_rejects_other_hosts_buckets_unsigned_and_ambiguous_routes(url):
    with pytest.raises(UserError):
        data_url(url, redirect=True)


def test_ornl_cdn_cannot_be_used_as_an_initial_authenticated_origin():
    assert data_url(ORNL_CDN, redirect=True) == ORNL_CDN
    with pytest.raises(UserError):
        data_url(ORNL_CDN)


def test_full_api_download_exact_reference_grid_and_sparse_values(client, gedi_setup, tmp_path):
    upstream, reference, geometry = gedi_setup
    request = search_request(client, geometry)
    assert len(request["scene_keys"]) == 1
    request["output_dir"] = str(tmp_path / "GEDI结果")
    request["options"].update(reference_path=str(reference), sample_reference=True)
    plan = client.post("plan", request)
    assert plan["requires"] == ["earthdata"] and plan["grid"]["width"] == 6
    assert "Earthdata" in client.post("download", request, status=400)["error"]
    result = client.post("earthdata-connect", {"method": "account", "username": "synthetic-user", "password": "synthetic-password"})
    assert result["verified"]
    assert client.get("bootstrap").json()["earthdata_connected"]
    request["plan_fingerprint"] = plan["fingerprint"]
    job = client.wait(client.post("download", request)["job_id"])
    result = job["result"]
    folder = Path(result["folder"])
    assert result["statistics"]["accepted_shots"] == 4
    assert result["statistics"]["in_aoi_and_dates"] == 6
    assert len(upstream.downloads) == 1
    with rasterio.open(reference) as ref, rasterio.open(folder / "gedi_metrics.tif") as metrics, rasterio.open(folder / "gedi_counts.tif") as counts:
        assert (metrics.crs, metrics.transform, metrics.width, metrics.height) == (ref.crs, ref.transform, ref.width, ref.height)
        assert metrics.descriptions == ("rh95_mean", "rh98_mean")
        assert metrics.read(1)[1, 1] == 15 and metrics.read(2)[1, 1] == 17
        assert metrics.read(1)[0, 0] == 0  # Zero height is a valid observation.
        assert metrics.read(1)[3, 4] == -9999 and metrics.read(2)[3, 4] == 30
        assert metrics.read(1)[2, 2] == -9999  # Hole and unobserved cells stay empty.
        assert counts.read(1)[1, 1] == 2 and counts.read(2)[3, 4] == 0 and counts.read(3)[3, 4] == 1
    points = json.loads((folder / "footprints.geojson").read_text(encoding="utf-8"))["features"]
    assert points[0]["properties"]["shot_number"] == str(BASE_NUMBER)
    assert points[0]["properties"]["grid_row"] == 1 and points[0]["properties"]["grid_col"] == 1
    assert points[0]["properties"]["reference_band_1"] == pytest.approx(-0.93)
    assert points[-1]["properties"]["reference_band_1"] is None
    with (folder / "footprints.csv").open(encoding="utf-8-sig", newline="") as source:
        rows = list(csv.DictReader(source))
    assert rows[0]["shot_number"] == str(BASE_NUMBER) and rows[0]["shot_id"] == "GEDI:" + str(BASE_NUMBER)
    manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["reference_sha256"] == hashlib.sha256(reference.read_bytes()).hexdigest()
    assert manifest["sources"][0]["provider_checksum_verified"]["algorithm"] == "SHA-512"
    assert TOKEN not in json.dumps(manifest) and "synthetic-signature" not in json.dumps(manifest)
    assert "synthetic-password" not in client.get("bootstrap").text
    repeated = client.wait(client.post("download", request)["job_id"])["result"]
    assert all(f["status"] == "skipped" for f in repeated["files"]) and len(upstream.downloads) == 1
    request["options"]["fields"] = ["rh98"]
    request.pop("plan_fingerprint")
    other = client.wait(client.post("download", request)["job_id"])["result"]
    assert other["folder"] != str(folder) and len(upstream.downloads) == 1  # Reuse exact originals across parameter selections.


def test_automatic_grid_matches_previous_optical_rule(gedi_setup):
    upstream, _, geometry = gedi_setup
    scenes = [granule_scene(upstream.item, KEY)]
    criteria = {"start": "2024-06-10", "end": "2024-06-10", "collection": KEY}
    for resolution in (10, 20, 60):
        plan = build_gedi_plan(geometry, scenes, normalize_options({"fields": ["rh95"], "resolution": resolution}, KEY), criteria)
        old, _, _ = build_plan(geometry, optical_options({"resolution": resolution}), 1)
        grid = plan["grid"]
        assert (grid["width"], grid["height"], grid["epsg"], grid["bounds"]) == (old["width"], old["height"], old["epsg"], old["bounds"])
        assert grid["transform"] == list(from_origin(old["bounds"][0], old["bounds"][3], resolution, resolution))[:6]
        again = build_gedi_plan(geometry, scenes, plan["options"], {**criteria, "id": "different-search-job"})
        assert again["fingerprint"] == plan["fingerprint"]


@pytest.mark.parametrize("key", ["gedi01-b-002", "gedi01-b-003", "gedi02-a-002", KEY, "gedi02-b-002", "gedi02-b-003", "gedi04-a-021", "gedi04-a-003", "gedi04-c-002", "gedi04-c-003"])
def test_version_specific_quality_fields_and_parameters(key, tmp_path):
    _, affine, geometry = reference_and_aoi(tmp_path)
    content = h5_bytes(key, affine)
    path = tmp_path / "product.h5"
    path.write_bytes(content)
    spec = COLLECTIONS[key]
    options = normalize_options({"mode": "footprints"}, key)
    plan = build_gedi_plan(geometry, [granule_scene(synthetic_item(key, content, geometry), key)], options,
                           {"start": "2024-06-10", "end": "2024-06-10", "collection": key})
    stats = {"total_shots": 0, "in_aoi_and_dates": 0}
    rows = list(_rows(path, spec, plan, geometry, stats, None))
    assert len(rows) == 4 and rows[0]["shot_number"] == str(BASE_NUMBER)
    assert spec["quality_paths"][0] in rows[0]


def test_vertical_profiles_and_one_based_waveform_indices(tmp_path):
    _, affine, geometry = reference_and_aoi(tmp_path)
    for key, fields in [("gedi02-b-003", ["cover", "pai_z", "pavd_z"]), ("gedi01-b-003", ["rxwaveform", "txwaveform"])]:
        path = tmp_path / (key + ".h5")
        content = h5_bytes(key, affine)
        path.write_bytes(content)
        plan = build_gedi_plan(geometry, [granule_scene(synthetic_item(key, content, geometry), key)],
                               normalize_options({"mode": "footprints", "fields": fields}, key),
                               {"start": "2024-06-10", "end": "2024-06-10", "collection": key})
        rows = list(_rows(path, COLLECTIONS[key], plan, geometry, {"total_shots": 0, "in_aoi_and_dates": 0}, None))
        if key.startswith("gedi02"):
            assert rows[0]["pai_z"] == list(range(30)) and rows[1]["pavd_z"] == list(range(100, 130))
        else:
            assert rows[0]["rxwaveform"] == list(range(6)) and rows[1]["txwaveform"] == [2, 3]


def test_quality_all_preserves_flags_and_optional_sensitivity_filter(tmp_path):
    _, affine, geometry = reference_and_aoi(tmp_path)
    content = h5_bytes(KEY, affine)
    path = tmp_path / "quality.h5"
    path.write_bytes(content)
    with h5py.File(path, "r+") as file:
        quality = file["BEAM0101/l2a_quality_flag_rel3"]
        quality[4] = 255
        quality.attrs["_FillValue"] = np.uint8(255)
    content = path.read_bytes()
    scene = granule_scene(synthetic_item(KEY, content, geometry), KEY)
    criteria = {"start": "2024-06-10", "end": "2024-06-10", "collection": KEY}
    options = normalize_options({"mode": "footprints", "quality": "all", "nondegraded": False}, KEY)
    plan = build_gedi_plan(geometry, [scene], options, criteria)
    rows = list(_rows(path, COLLECTIONS[KEY], plan, geometry, {"total_shots": 0, "in_aoi_and_dates": 0}, None))
    assert len(rows) == 6 and any(r["l2a_quality_flag_rel3"] == 0 for r in rows)
    assert next(r for r in rows if r["shot_number"] == str(BASE_NUMBER + 4))["l2a_quality_flag_rel3"] is None
    plan = build_gedi_plan(geometry, [scene], normalize_options({"mode": "footprints", "min_sensitivity": 0.9}, KEY), criteria)
    rows = list(_rows(path, COLLECTIONS[KEY], plan, geometry, {"total_shots": 0, "in_aoi_and_dates": 0}, None))
    assert len(rows) == 3


def test_auth_failure_and_official_checksum_failure_publish_nothing(client, gedi_setup, tmp_path):
    upstream, _, geometry = gedi_setup
    request = search_request(client, geometry)
    request["output_dir"] = str(tmp_path / "failure")
    client.post("earthdata-connect", {"token": TOKEN})
    upstream.status = 403
    job = client.wait(client.post("download", request)["job_id"], status="failed")
    assert "授权" in job["error"] and TOKEN not in json.dumps(job)
    upstream.status = 200
    upstream.content = b"x" + upstream.content[1:]
    job = client.wait(client.post("download", request)["job_id"], status="failed")
    assert "SHA-512" in job["error"]
    assert not list(Path(request["output_dir"]).rglob("*.h5"))
    assert not list(Path(request["output_dir"]).rglob("*.part"))
    assert not list(Path(request["output_dir"]).rglob("manifest.json"))


@pytest.mark.parametrize("corrupt", [False, True], ids=["valid", "unselected-chunk-corrupt"])
def test_legacy_download_without_cmr_checksum_decodes_all_datasets(client, monkeypatch, tmp_path, corrupt):
    key = "gedi02-a-002"
    _, affine, geometry = reference_and_aoi(tmp_path)
    buffer = io.BytesIO(h5_bytes(key, affine))
    with h5py.File(buffer, "a") as file:
        dataset = file.create_dataset("ancillary/unselected_integrity_probe", data=np.arange(1024, dtype="uint32"),
                                      chunks=(128,), compression="gzip", fletcher32=True)
        file.flush()
        chunk = dataset.id.get_chunk_info_by_coord((0,))
    content = bytearray(buffer.getvalue())
    if corrupt:
        content[chunk.byte_offset + chunk.size - 1] ^= 1
    content = bytes(content)
    item = synthetic_item(key, content, geometry)
    archive = item["umm"]["DataGranule"]["ArchiveAndDistributionInformation"][0]
    archive.pop("Checksum")
    archive.pop("SizeInBytes")
    archive.update(Size=len(content) / 1_000_000, SizeUnit="MB")
    upstream = GediProvider(item, content)
    monkeypatch.setattr(HttpClient, "request", lambda _, *args, **kwargs: upstream.request(*args, **kwargs))
    request = search_request(client, geometry, key)
    request.update(output_dir=str(tmp_path / "legacy"), options={"mode": "original"})
    client.post("earthdata-connect", {"token": TOKEN})
    job = client.wait(client.post("download", request)["job_id"], status="failed" if corrupt else "completed")
    output = Path(request["output_dir"])
    assert not list(output.rglob("*.part"))
    if corrupt:
        assert "可读" in job["error"] and not list(output.rglob("*.h5"))
        assert not list(output.rglob("manifest.json"))
    else:
        result = job["result"]
        source = json.loads((Path(result["folder"]) / "manifest.json").read_text(encoding="utf-8"))["sources"][0]
        assert source["provider_checksum_verified"] is None and source["all_datasets_decoded"] is True
        assert source["size"] == len(content) and source["sha256"] == hashlib.sha256(content).hexdigest()
        assert Path(source["path"]).read_bytes() == content


def test_cancel_transfer_and_stale_reference_plan(client, gedi_setup, tmp_path):
    upstream, reference, geometry = gedi_setup
    request = search_request(client, geometry)
    request["output_dir"] = str(tmp_path / "cancel")
    request["options"]["reference_path"] = str(reference)
    plan = client.post("plan", request)
    client.post("earthdata-connect", {"token": TOKEN})
    with rasterio.open(reference, "r+") as raster:
        raster.update_tags(changed_after_plan="yes")
    request["plan_fingerprint"] = plan["fingerprint"]
    assert "变化" in client.post("download", request, status=400)["error"]
    request.pop("plan_fingerprint")
    upstream.block = True
    job_id = client.post("download", request)["job_id"]
    assert upstream.started.wait(5)
    client.post("cancel", {"job_id": job_id})
    client.wait(job_id, status="cancelled")
    assert not list(Path(request["output_dir"]).rglob("*.part"))
    assert not list(Path(request["output_dir"]).rglob("manifest.json"))


def test_reference_and_collection_changes_are_not_silent(client, gedi_setup):
    _, _, geometry = gedi_setup
    request = search_request(client, geometry)
    request["criteria"]["collection"] = "gedi02-a-002"
    assert "变化" in client.post("plan", request, status=400)["error"]
    with pytest.raises(UserError, match="栅格"):
        normalize_options({"mode": "footprints"}, "gedi04-b-021")
    with pytest.raises(UserError, match="标量"):
        normalize_options({"mode": "aligned", "fields": ["rxwaveform"]}, "gedi01-b-003")


def test_original_download_reuses_checked_files_and_detects_tampering(client, gedi_setup, tmp_path):
    upstream, _, geometry = gedi_setup
    request = search_request(client, geometry)
    request.update(output_dir=str(tmp_path / "original"), options={"mode": "original"})
    client.post("earthdata-connect", {"token": TOKEN})
    first = client.wait(client.post("download", request)["job_id"])["result"]
    original = next(Path(f["path"]) for f in first["files"] if f["path"].endswith(".h5"))
    assert original.read_bytes() == upstream.content
    second = client.wait(client.post("download", request)["job_id"])["result"]
    assert any(f["path"] == str(original) and f["status"] == "skipped" for f in second["files"])
    assert len(upstream.downloads) == 1
    original.write_bytes(b"changed locally")
    job = client.wait(client.post("download", request)["job_id"], status="failed")
    assert "完整性" in job["error"] and len(upstream.downloads) == 1


def test_empty_filtered_footprints_do_not_create_filled_grid(client, gedi_setup, tmp_path):
    upstream, _, geometry = gedi_setup
    request = search_request(client, geometry)
    request["options"]["beams"] = ["BEAM0000"]
    request["output_dir"] = str(tmp_path / "empty")
    client.post("earthdata-connect", {"token": TOKEN})
    result = client.wait(client.post("download", request)["job_id"])["result"]
    folder = Path(result["folder"])
    assert result["statistics"]["accepted_shots"] == 0
    assert not list(folder.glob("*.tif"))
    assert json.loads((folder / "footprints.geojson").read_text(encoding="utf-8"))["features"] == []
    assert len(upstream.downloads) == 1 and list(folder.parents[1].glob("original/*/*.h5"))


def test_gridded_biomass_aligns_and_retains_source_nodata(client, monkeypatch, tmp_path):
    from rasterio.io import MemoryFile
    key = "gedi04-b-021"
    x, y = Transformer.from_crs(4326, 6933, always_xy=True).transform(116.35, 39.9)
    source_transform = from_origin(x - 1000, y + 1000, 1000, 1000)
    with MemoryFile() as memory:
        with memory.open(driver="GTiff", width=2, height=2, count=1, dtype="float32", crs="EPSG:6933", transform=source_transform, nodata=-9999) as raster:
            raster.write(np.array([[10, 20], [30, -9999]], dtype="float32"), 1)
            raster.scales = (0.1,)
            raster.set_band_unit(1, "Mg/ha")
        content = memory.read()
    reference = tmp_path / "500m已有影像.tif"
    with rasterio.open(reference, "w", driver="GTiff", width=4, height=4, count=1, dtype="float32", crs="EPSG:6933", transform=from_origin(x - 1000, y + 1000, 500, 500)) as raster:
        raster.write(np.ones((4, 4), dtype="float32"), 1)
    project = Transformer.from_crs(6933, 4326, always_xy=True)
    geometry = transform(box(x - 1000, y - 1000, x + 1000, y + 1000), project.transform, interleaved=False)
    upstream = GediProvider(synthetic_item(key, content, geometry, filename="GEDI04_B_R01000M_MU.tif"), content)
    monkeypatch.setattr(HttpClient, "request", lambda _, *args, **kwargs: upstream.request(*args, **kwargs))
    request = search_request(client, geometry, key)
    request.update(output_dir=str(tmp_path / "biomass"), options={"mode": "aligned", "reference_path": str(reference)})
    client.post("earthdata-connect", {"token": TOKEN})
    result = client.wait(client.post("download", request)["job_id"])["result"]
    output = next(Path(f["path"]) for f in result["files"] if f["path"].endswith("_aligned.tif"))
    with rasterio.open(output) as raster, rasterio.open(reference) as ref:
        assert raster.transform == ref.transform and raster.crs == ref.crs and raster.shape == ref.shape
        expected = np.array([[1, 1, 2, 2], [1, 1, 2, 2], [3, 3, -9999, -9999], [3, 3, -9999, -9999]], dtype="float32")
        np.testing.assert_array_equal(raster.read(1), expected)
        assert raster.tags()["resampling"] == "nearest" and raster.units == ("Mg/ha",)
    assert not list(Path(result["folder"]).glob("footprints.*"))


def test_cmr_search_after_pages_and_duplicate_granules(monkeypatch):
    first = real_item("l2a-003")
    second = copy.deepcopy(first)
    second["meta"]["concept-id"] = "G9999999998-LPCLOUD"
    calls = []
    def request(_, method, url, **kwargs):
        calls.append(copy.deepcopy(kwargs))
        if len(calls) == 1:
            return StubResponse(data={"items": [first] * 200}, headers={"CMR-Search-After": '["page-two"]'})
        return StubResponse(data={"items": [first, second]})
    monkeypatch.setattr(HttpClient, "request", request)
    scenes = search_gedi(box(110, 30, 120, 42), KEY, "2020-06-01", "2020-06-10")
    assert len(scenes) == 2 and len(calls) == 2
    assert calls[1]["headers"]["CMR-Search-After"] == '["page-two"]'
    assert calls[0]["params"]["temporal"] == "2020-06-01T00:00:00Z,2020-06-11T00:00:00Z"


def test_large_reference_image_uses_bounded_aggregation(client, gedi_setup, tmp_path):
    _, reference, geometry = gedi_setup
    with rasterio.open(reference) as raster:
        affine = raster.transform
    large = tmp_path / "此前区域大影像.tif"
    with rasterio.open(large, "w", driver="GTiff", width=3000, height=3000, count=1,
                       dtype="float32", crs="EPSG:32650", transform=affine, tiled=True, compress="DEFLATE", SPARSE_OK=True):
        pass
    request = search_request(client, geometry)
    request.update(output_dir=str(tmp_path / "large-grid"), options={"mode": "aligned", "reference_path": str(large)})
    client.post("earthdata-connect", {"token": TOKEN})
    result = client.wait(client.post("download", request)["job_id"])["result"]
    with rasterio.open(Path(result["folder"]) / "gedi_metrics.tif") as raster:
        assert raster.shape == (3000, 3000) and raster.count == 6
        assert raster.read(2, window=rasterio.windows.Window(1, 1, 1, 1))[0, 0] == 15
        assert raster.read(2, window=rasterio.windows.Window(2999, 2999, 1, 1))[0, 0] == -9999
