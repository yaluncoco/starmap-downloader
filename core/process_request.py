"""Build a scene-locked request and read the Process API's TIFF + metadata archive."""
from __future__ import annotations

import datetime as dt
import json
import re
import shutil
import tarfile

import requests

from .errors import UserError, TransientDownloadError
from .http import PROCESS_URL, check_cancel, tls_error_kind, tls_error_message, wait_retry
from .scene_identity import SAFE_ID, expected_products, utc_text, utc_time
from .research_bundle import is_research, transfer_options

MGRS_PATH = re.compile(r"(?:^|/)tiles/(\d{1,2})/([A-Z])/([A-Z]{2})(?:/|$)", re.I | re.ASCII)


def resampling(options):
    # A categorical SCL mask must not be interpolated across class boundaries.
    return "NEAREST" if options["mask_clouds"] or is_research(options) else "BILINEAR"


def evalscript(options, products):
    research = is_research(options)
    bands = list(dict.fromkeys(options["bands"] + (["SCL"] if options["mask_clouds"] or research else []) + ["dataMask"]))
    invalid = "s.dataMask === 0" + (" || [0,1,3,8,9,10].includes(s.SCL)" if options["mask_clouds"] else "")
    if research:
        expression = "[" + ",".join("s." + band for band in options["bands"]) + ",s.SCL,match.index]"
        invalid = "s.dataMask === 0 || ![" + ",".join("s." + band for band in options["bands"]) + "].every(function(v) { return isFinite(v); }) || !isFinite(s.SCL) || s.SCL !== Math.floor(s.SCL) || s.SCL < 0 || s.SCL > 11"
        invalid_value = "[" + ",".join(["-9999"] * options["count"] + ["fallbackSCL", "0"]) + "]"
        sample_type, nodata = "FLOAT32", ", nodataValue: -9999"
    elif options["mode"] == "rgb":
        expression = "[Math.min(255,Math.max(0,s.B04*2.5*255)),Math.min(255,Math.max(0,s.B03*2.5*255)),Math.min(255,Math.max(0,s.B02*2.5*255)),255]"
        invalid_value, sample_type, nodata = "[0,0,0,0]", "UINT8", ""
    elif options["mode"] == "ndvi":
        expression = "[(s.B08+s.B04 === 0) ? -9999 : (s.B08-s.B04)/(s.B08+s.B04)]"
        invalid_value, sample_type, nodata = "[-9999]", "FLOAT32", ", nodataValue: -9999"
    else:
        expression = "[" + ",".join("s." + band for band in options["bands"]) + "]"
        invalid_value = "[" + ",".join(["-9999"] * options["count"]) + "]"
        sample_type, nodata = "FLOAT32", ", nodataValue: -9999"
    return f'''//VERSION=3
var allowed = {json.dumps(products, separators=(",", ":"))};
var candidates = 0;
var researchPackage = {str(research).lower()};
function setup() {{
  return {{input: {json.dumps(bands)}, mosaicking: "TILE",
           output: {{id: "default", bands: {transfer_options(options)['count']}, sampleType: "{sample_type}"{nodata}}}}};
}}
''' + r'''
function identify(tile) {
  var path = String(tile.dataPath || "");
  var utc = Date.parse(tile.date);
  if (!isFinite(utc)) return null;
  var safe = path.match(/(?:^|\/)(S2[A-Z]_MSIL2A_\d{8}T\d{6}_N\d+_R\d+_T\d{2}[A-Z]{3}_\d{8}T\d{6})(?:\.SAFE)?(?:\/|$)/i);
  var mgrs = path.match(/(?:^|\/)tiles\/(\d{1,2})\/([A-Z])\/([A-Z]{2})(?:\/|$)/i);
  for (var i = 0; i < allowed.length; i++) {
    var product = allowed[i];
    if (safe) {
      if (safe[1].toUpperCase() === product.id) return {id: product.id, index: i + 1, method: "safe_id"};
    } else if (mgrs) {
      var code = "T" + ("0" + mgrs[1]).slice(-2) + (mgrs[2] + mgrs[3]).toUpperCase();
      // Some results represent the same granule time without fractional seconds.
      if (code === product.tile && Math.floor(utc / 1000) === Math.floor(Date.parse(product.utc) / 1000)) {
        return {id: product.id, index: i + 1, method: "mgrs_and_utc"};
      }
    }
  }
  return null;
}
function preProcessScenes(collections) {
  var tiles = collections.scenes.tiles;
  candidates = tiles.length;
  var matches = tiles.map(identify);
  var exact = {};
  var approximate = {};
  matches.forEach(function(match) {
    if (!match) return;
    if (match.method === "safe_id") exact[match.id] = true;
    else approximate[match.id] = (approximate[match.id] || 0) + 1;
  });
  collections.scenes.tiles = tiles.filter(function(tile, i) {
    var match = matches[i];
    return match && (match.method === "safe_id" || (!exact[match.id] && approximate[match.id] === 1));
  });
  if (researchPackage) collections.scenes.tiles.sort(function(a, b) {
    return identify(a).index - identify(b).index || String(a.dataPath).localeCompare(String(b.dataPath));
  });
  return collections;
}
function updateOutputMetadata(scenes, inputMetadata, outputMetadata) {
  var sources = (scenes.tiles || []).map(function(tile) {
    var match = identify(tile);
    if (!match) return null;
    var source = {catalogue_id: match.id, match: match.method, date: tile.date, data_path: tile.dataPath};
    if (researchPackage) source.source_index = match.index;
    return source;
  }).filter(function(source) { return source !== null; });
  outputMetadata.userData = {candidates: candidates, sources: sources};
}
''' + f'''
function evaluatePixel(samples, scenes) {{
  {"var fallbackSCL = 255;" if research else ""}
  for (var i = 0; i < samples.length; i++) {{
    var s = samples[i];
    {"var match = scenes && identify(scenes.tiles[i]); if (!match) continue; if (fallbackSCL === 255 && isFinite(s.SCL) && s.SCL === Math.floor(s.SCL) && s.SCL >= 0 && s.SCL <= 11) fallbackSCL = s.SCL;" if research else ""}
    if ({invalid}) continue;
    return {expression};
  }}
  return {invalid_value};
}}
'''


def process_payload(tile, plan, scene, options, *, products=None):
    products = products if products is not None else expected_products(scene)
    times = [utc_time(product["utc"]) for product in products]
    # A SAFE name carries datatake time, not sensing time for this location.
    begin = min(times) - dt.timedelta(seconds=30)
    end = max(times) + dt.timedelta(seconds=30)
    method = resampling(options)
    processing = {"upsampling": method, "downsampling": method}
    if is_research(options):
        processing["harmonizeValues"] = False  # Preserve negative BOA reflectance, not only positive values.
    return {
        "input": {
            "bounds": {"bbox": list(tile.bounds), "properties": {"crs": f"http://www.opengis.net/def/crs/EPSG/0/{plan['epsg']}"}},
            # Cloud selection was applied to the catalogue allowlist. Avoid an independent cloud cutoff here.
            "data": [{"type": "sentinel-2-l2a", "dataFilter": {"timeRange": {"from": utc_text(begin), "to": utc_text(end)},
                      "maxCloudCoverage": 100, "mosaickingOrder": "mostRecent"}, "processing": processing}],
        },
        "output": {"width": tile.width, "height": tile.height, "responses": [
            {"identifier": "default", "format": {"type": "image/tiff"}},
            {"identifier": "userdata", "format": {"type": "application/json"}},
        ]},
        "evalscript": evalscript(options, products),
    }


def _read_archive(archive, target, max_bytes, products):
    """Read known members only; never extract paths named by a remote archive."""
    metadata = None
    seen = set()
    allowed = {product["id"]: product for product in products}
    try:
        with tarfile.open(archive, "r|*") as bundle:
            for member in bundle:
                name = member.name.removeprefix("./")
                if name not in ("default.tif", "userdata.json") or name in seen or not member.isfile():
                    raise ValueError("Unexpected response member")
                if not 0 < member.size <= (max_bytes if name == "default.tif" else 2 * 1024 * 1024):
                    raise ValueError("Unexpected response size")
                seen.add(name)
                with bundle.extractfile(member) as stream:
                    if name == "default.tif":
                        with target.open("wb") as out:
                            shutil.copyfileobj(stream, out, length=256 * 1024)
                    else:
                        metadata = json.load(stream)
        if seen != {"default.tif", "userdata.json"} or not isinstance(metadata, dict) or not isinstance(metadata.get("sources"), list):
            raise ValueError("Source metadata missing")
        candidates = metadata.get("candidates")
        sources = metadata["sources"]
        if type(candidates) is not int or not 0 <= len(sources) <= candidates:
            raise ValueError("Invalid candidate count")
        indices = {product["id"]: index for index, product in enumerate(products, 1)}
        for source in sources:
            if (not isinstance(source, dict) or source.get("catalogue_id") not in allowed
                    or source.get("match") not in ("safe_id", "mgrs_and_utc")
                    or not isinstance(source.get("data_path"), str) or not source["data_path"]
                    or not isinstance(source.get("date"), str)):
                raise ValueError("Source identity not requested")
            if "source_index" in source and (type(source["source_index"]) is not int or source["source_index"] != indices[source["catalogue_id"]]):
                raise ValueError("Invalid pixel source index")
            product = allowed[source["catalogue_id"]]
            acquired = utc_time(source["date"])
            identifiers = {match[0].removesuffix(".SAFE") for match in SAFE_ID.finditer(source["data_path"].upper())}
            if source["match"] == "safe_id":
                if product["id"] not in identifiers:
                    raise ValueError("Invalid source identifier")
            else:
                mgrs = MGRS_PATH.search(source["data_path"].upper())
                code = "T" + mgrs[1].zfill(2) + mgrs[2] + mgrs[3] if mgrs else None
                if identifiers or code != product["tile"] or acquired.replace(microsecond=0) != utc_time(product["utc"]).replace(microsecond=0):
                    raise ValueError("Invalid source tile or acquisition time")
    except (tarfile.TarError, EOFError, ValueError, UnicodeError, TypeError, AttributeError):
        raise UserError("影像服务未返回可核验的 GeoTIFF 与来源记录，未保存该景。") from None
    return {"candidates": candidates, "sources": sources}


def fetch_tile(http, auth, payload, target, *, options, products, cancel=None, status=None):
    archive = target.with_suffix(".part")
    raw_bytes = payload["output"]["width"] * payload["output"]["height"] * transfer_options(options)["count"] * (1 if options["dtype"] == "uint8" else 4)
    max_bytes = raw_bytes * 2 + 4 * 1024 ** 2
    refreshed = False
    transfer_failures = 0
    try:
        while True:
            check_cancel(cancel)
            token = auth.token(cancel)
            response = None
            try:
                response = http.request("POST", PROCESS_URL, headers={"Authorization": "Bearer " + token, "Accept": "application/tar"},
                                        json=payload, stream=True, timeout=(15, 150), cancel=cancel, status=status, attempts=1)
                if response.status_code == 401 and not refreshed:
                    auth.token(cancel, force=True)
                    refreshed = True
                    continue
                if response.status_code in (401, 403):
                    raise UserError("下载权限不足或凭据已失效，请检查 Sentinel Hub 客户端和账户额度。")
                if response.status_code == 402:
                    raise UserError("账户处理额度不足，请在官方 Dashboard 检查额度。")
                if response.status_code != 200:
                    raise UserError(f"影像处理请求失败（HTTP {response.status_code}）。请缩小区域或调整参数。")
                size = 0
                with archive.open("wb") as out:
                    for chunk in response.iter_content(256 * 1024):
                        check_cancel(cancel)
                        size += len(chunk)
                        if size > max_bytes:
                            raise UserError("影像区块超过预计大小，已停止下载。")
                        if chunk:
                            out.write(chunk)
                check_cancel(cancel)
                metadata = _read_archive(archive, target, max_bytes, products)
                return {"processing_units": response.headers.get("X-ProcessingUnits-Spent"), **metadata}
            except (TransientDownloadError, requests.ConnectionError, requests.Timeout, requests.exceptions.ChunkedEncodingError, requests.exceptions.ContentDecodingError) as error:
                check_cancel(cancel)
                if isinstance(error, requests.exceptions.SSLError) and tls_error_kind(error) != "interrupted":
                    raise UserError(tls_error_message(error, "影像处理")) from None
                transfer_failures += 1
                if transfer_failures >= 3:
                    raise TransientDownloadError("影像传输多次中断，可继续原任务；已校验影像和区块进度已保留。") from None
                if status:
                    status(f"影像区块中断，正在重试（{transfer_failures}/2）；已完成区块保留")
                if response is not None:
                    response.close()
                    response = None
                wait_retry(transfer_failures - 1, cancel, getattr(error, "retry_after", None))
            finally:
                if response is not None:
                    response.close()
    finally:
        archive.unlink(missing_ok=True)
