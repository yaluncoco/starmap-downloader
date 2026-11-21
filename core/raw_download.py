"""Stream selected CDSE product/asset bytes with strict provenance and atomic publication."""
from __future__ import annotations

import base64
import binascii
import datetime as dt
import hashlib
import json
import re
import shutil
import time
import xml.etree.ElementTree as ET
import zipfile
import zlib
from pathlib import Path, PurePosixPath
from urllib.parse import urljoin
from uuid import uuid4

import requests
import rasterio
from shapely.geometry import mapping
from shapely import normalize

from . import VERSION
from .download import _save_json
from .errors import Cancelled, UserError, keep_download_result
from .local_paths import io_path
from .http import CATALOGUE_URL, HttpClient, check_cancel
from .integrity import publish_file
from .native_catalogue import DOWNLOAD_ROOT, _json, download_asset, product_stem
from .open_l2a import check_l2a_target, verify_l2a_index, verify_l2a_xml
from .open_l3 import ETAG as L3_ETAG, PROOF_KIND as L3_MOSAIC, check_l3_target, pin_l3_object, verify_l3_listing
from .open_modis import (PROOF as MODIS, check_modis_target, is_modis, modis_blob_request, modis_scope,
                         pin_modis, verify_modis_file, verify_modis_transfer)
from .open_s5p import DIGEST_KEYS as S5P_DIGEST_KEYS, S5P_ETAG, verify_swath
from .open_sen3 import MD5_TEXT as SEN3_MD5, SEN3_ETAG, SHA256_TEXT as SEN3_SHA256, valid_product_member
from .open_slc import check_slc_target, is_slc
from .slc_archive import transfer_slc, verify_slc_archive
from .open_worldcover import (PROOF_KIND as WORLDCOVER, check_worldcover_headers, check_worldcover_target,
                              pin_worldcover, verify_worldcover_bytes)
from .public_token import PlanetaryAccess
from .s3_source import (ES_HOST, ESSENTIAL_EXTENSIONS, L3_MIRROR_HOST, S1_AWS_HOST, S2_L1C_AWS, S5P_ARCHIVE_HOST, SEN3_ARCHIVE_HOST,
                        SEN3_MANIFEST, SEN3_NAME, WC_HOSTS, is_cdse_url, is_public_url, landsat_blob_object, sen3_archive_object)
from .scene_identity import utc_time

CHUNK = 1024 * 1024
BYTE_RANGE = re.compile(r"bytes ([0-9]+)-([0-9]+)/([0-9]+)\Z", re.ASCII)
ASSET_EXTENSIONS = ESSENTIAL_EXTENSIONS
OPEN_TRANSPORTS = {"public", "pc-public"}
MAX_NATIVE_ASSETS = 100
SLC_VALIDATION = "ASF full ZIP MD5; SAFE identity, all file CRCs, XFDU MD5s and complex I/Q TIFF types"
TRANSFER_ERRORS = (requests.ConnectionError, requests.Timeout, requests.exceptions.ChunkedEncodingError, requests.exceptions.ContentDecodingError)
CACHED_EVIDENCE_FIELDS = (
    "size", "expected_bytes", "etag", "content_type", "format_validation", "provider_checksum_verified", "source_checksums",
    "aws_single_part_md5", "azure_blob_md5", "safe_product", "odata_name", "l1c_index_etag", "l2a_index_etag", "l2a_index_sha256",
    "l3_index_etag", "l3_index_sha256", "reflectance_scale", "s5p_validation", "modis_validation", "asf_zip_md5", "slc_validation",
    "worldcover_upload_part_bytes", "worldcover_grid_sha256",
)
SOURCE_CHECKSUM_FIELDS = (("SHA-256", "provider_sha256", 64), ("SHA3-256", "provider_sha3_256", 64), ("MD5", "provider_md5", 32))


def _asset_target(scene, asset, route):
    for channel, field in (("odata", "url"), ("cdse-s3", "s3_url"), ("public", "public_url"), ("pc-public", "pc_url")):
        if route not in ("auto", channel) or not asset.get(field):
            continue
        return {"key": scene["key"], "name": asset["name"], "uuid": asset.get("uuid") if channel == "odata" else None,
                "names": scene["raw"]["names"], "url": asset[field], "asset": asset["id"], "transport": channel,
                "bytes": asset.get("bytes"), "provider_sha256": asset.get("provider_sha256"),
                "provider_sha3_256": asset.get("provider_sha3_256"), "provider_md5": asset.get("provider_md5"),
                "raster": asset.get("raster"), "identity": asset.get("identity")}
    return None


def raw_files(scenes, mode, asset_id=None, route="auto", asset_ids=None):
    files = []
    if mode == "assets" and (len(scenes) != 1 or not isinstance(asset_ids, list) or not 1 <= len(asset_ids) <= MAX_NATIVE_ASSETS
                             or any(not isinstance(x, str) or not x for x in asset_ids) or len(set(asset_ids)) != len(asset_ids)):
        raise UserError(f"请单选一个产品，并选择 1～{MAX_NATIVE_ASSETS} 个不同的文件。")
    for scene in scenes:
        src = scene["raw"]
        if mode == "assets":
            listed = {a["id"]: a for a in src["assets"] if Path(a["name"]).suffix.lower() in ASSET_EXTENSIONS}
            seen = set()
            for alias in asset_ids:
                candidate = listed.get(alias)
                target = _asset_target(scene, candidate, route) if candidate else None
                if target is None:
                    raise UserError(f"文件 {alias} 不在所选产品或通道中。请重新勾选原始文件。")
                if target["url"] in seen:
                    raise UserError("所选文件包含指向同一对象的重复资产，请只保留一项。")
                seen.add(target["url"])
                files.append(target)
            continue
        if mode == "original" and "sen3" in src:
            status = src["sen3"]
            if not status.get("complete") or len(src["assets"]) != status["declared_members"] + 1:
                raise UserError(status.get("complete_reason") or "此精确版本没有可校验的完整 SEN3，请改选已就绪的成员。")
            for asset in src["assets"]:
                target = _asset_target(scene, asset, route)
                if target is None:
                    raise UserError("完整 SEN3 只支持已核对的公开对象。")
                files.append(target)
            continue
        if mode == "original":
            if src["uuid"] and route in ("auto", "odata"):
                files.append({"key": scene["key"], "name": scene["title"], "uuid": src["uuid"], "names": src["names"],
                              "url": f"{DOWNLOAD_ROOT}/Products({src['uuid']})/$value", "bytes": None, "asset": None, "transport": "odata"})
                continue
            primary = {"public": src.get("primary_public"), "cdse-s3": src.get("primary_s3"), "pc-public": src.get("primary_pc")}
            ids = {alias for alias in primary.values() if alias}
            assets = [a for a in src["assets"] if a["id"] in ids]
        elif mode == "metadata":
            assets = [a for a in src["assets"] if a["name"].lower().endswith((".xml", ".json", ".safe", ".txt"))]
            assets.sort(key=lambda a: (a["id"] != src.get("preferred_metadata"), a["name"].casefold() != "manifest.safe",
                                        not a["name"].casefold().startswith(("mtd_msi", "xfdumanifest")),
                                        "metadata" not in a.get("roles", []), a["name"].casefold()))
        else:
            assets = [a for a in src["assets"] if a["id"] == asset_id and Path(a["name"]).suffix.lower() in ASSET_EXTENSIONS]
        if mode == "original" and route == "auto":
            # S3 single-object fallback is not an inferred OData archive.
            applicable = [target for asset in assets for channel, alias in primary.items()
                          if alias == asset["id"] and (target := _asset_target(scene, asset, channel))]
        else:
            applicable = [target for asset in assets if (target := _asset_target(scene, asset, route))]
        if not applicable:
            if mode == "original":
                raise UserError(f"产品 {scene['title']} 没有此通道可用的官方整包或单一数据主文件。请改选原始文件、元数据或 OData。")
            raise UserError(f"产品 {scene['title']} 在所选通道没有可下载的此类文件。请切换通道或文件类型。")
        files.append(applicable[0])
    if not files:
        raise UserError("请选择至少一个原始产品。")
    return files


def raw_plan(geometry, scenes, options):
    files = raw_files(scenes, options["mode"], options.get("asset"), options.get("transport", "auto"), options.get("assets"))
    estimated = sum(f["bytes"] for f in files) if all(f["bytes"] for f in files) else None
    sources = {file["transport"] for file in files}
    providers = {scene["raw"].get("public_provider") for scene in scenes if scene["raw"].get("public_provider")}
    label = ", ".join(sorted(providers)) if providers else "CDSE STAC / OData and/or S3 native objects"
    signature = {"v": 3, "geometry": mapping(normalize(geometry)), "options": options, "source": label, "transports": sorted(sources)}
    sen3 = all("sen3" in scene["raw"] for scene in scenes)
    if sen3:
        signature.update(v=4, products=sorted(scene["title"] for scene in scenes))
    modis = all(is_modis(file) for file in files)
    if modis:
        for file in files:
            check_modis_target(file)
        signature.update(v=5, targets=sorted(file["url"] for file in files))
    slc = any(is_slc(file) for file in files)
    if slc:
        if options["mode"] != "original":
            raise UserError("SLC 历史存档仅支持完整的原始 ZIP。")
        for file in files:
            check_slc_target(file)
    fingerprint = hashlib.sha256(json.dumps(signature, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:12]
    prefix = "Sentinel-1-SLC" if slc else "MODIS" if modis else "Sentinel-3" if sen3 else "开放原文件" if sources <= OPEN_TRANSPORTS else "CDSE"
    return {"fingerprint": fingerprint, "kind": "original", "folder_name": f"{prefix}_{fingerprint}", "scene_count": len(scenes),
            "requests": len(files), "estimated_bytes": estimated, "options": options, "targets": files, "source": label,
            "transports": sorted(sources), "requires": sorted(sources - OPEN_TRANSPORTS),
            **({"sen3_complete": options["mode"] == "original"} if sen3 else {})}


def _verify_product_identity(http, item, cancel):
    data = _json(http.request("GET", f"{CATALOGUE_URL}({item['uuid']})", params={"$select": "Id,Name,Online,ContentLength,Checksum"},
                              cancel=cancel, timeout=(10, 25)), "官方产品身份核对")
    name = data.get("Name")
    if data.get("Id") != item["uuid"] or not isinstance(name, str) or product_stem(name) != product_stem(item["key"]):
        raise UserError("OData 产品 UUID 或名称与所选目录产品不一致，未下载。")
    if data.get("Online") is False:
        raise UserError("所选 CDSE 原始产品目前离线。请换一个产品或稍后重试。")
    return data


def _authorized_get(http, auth, item, headers, cancel, status):
    url, refreshed, redirects = item["url"], False, 0
    while True:
        check_cancel(cancel)
        link = download_asset(url)
        if not link or link[0] != item["uuid"] or link[3] != url:
            raise UserError("官方目录返回了与所选产品不匹配的下载地址。")
        response = http.request("GET", url, headers={**headers, "Authorization": "Bearer " + auth.token(cancel)}, cancel=cancel,
                                status=status, stream=True, timeout=(15, 95))
        if response.status_code == 401 and not refreshed:
            response.close()
            auth.token(cancel, force=True)
            refreshed = True
            continue
        if response.status_code in (301, 302, 303, 307, 308):
            target = response.headers.get("Location")
            response.close()
            redirects += 1
            if not target or redirects > 3:
                raise UserError("官方原始产品下载重定向无法完成。")
            # Only the canonical official object may receive the bearer. Do not guess storage hosts.
            url = urljoin(url, target)
            if url != item["url"]:
                raise UserError("官方产品返回了未验证的重定向，未向新地址发送凭据。")
            continue
        return response


def _sen3_target(item):
    proof = item.get("identity") or {}
    parts = sen3_archive_object(item["url"])
    if (not parts or item.get("transport") != "public" or proof.get("kind") != "sen3-member"
            or parts[-2] != proof.get("product") or item["key"] not in (parts[-2], parts[-2][:-5])
            or not item.get("bytes") or item["name"] != parts[-1] or item["asset"] != parts[-1]
            or not SEN3_ETAG.fullmatch(proof.get("archive_etag") or "")
            or not SEN3_SHA256.fullmatch(proof.get("manifest_sha256") or "")
            or (parts[-1] != SEN3_MANIFEST and not SEN3_MD5.fullmatch(proof.get("xfdu_md5") or ""))
            or not valid_product_member(SEN3_NAME.fullmatch(parts[-2]), parts[-1], item.get("provider_md5"), proof.get("xfdu_md5"))):
        raise UserError("SEN3 文件缺少一致的精确产品号、路径、清单校验值或对象版本。")
    return proof


def _is_worldcover(item):
    url = item.get("url") or ""
    return ((item.get("identity") or {}).get("kind") == WORLDCOVER
            or any(url.startswith(f"https://{host}/") for host in WC_HOSTS))


def _transport_get(http, auth, s3_auth, item, headers, cancel, status, pc_auth=None):
    if is_modis(item):
        check_modis_target(item)
        return modis_blob_request(http, "GET", item["url"], headers, cancel, pc_auth, status=status)
    if _is_worldcover(item):
        check_worldcover_target(item)  # Verify before any channel can attach credentials or follow a redirect.
    channel = item.get("transport", "odata")
    if channel == "odata":
        return _authorized_get(http, auth, item, headers, cancel, status)
    url = item["url"]
    if url.startswith(f"https://{S5P_ARCHIVE_HOST}/"):
        proof = item.get("identity") or {}
        if (channel != "public" or proof.get("kind") != "s5p-swath" or proof.get("product") != item["key"]
                or item["name"] != item["key"] + ".nc" or not url.endswith("/" + item["name"])
                or not item.get("bytes") or not any(item.get(k) for k in S5P_DIGEST_KEYS)
                or not S5P_ETAG.fullmatch(proof.get("archive_etag") or "")):
            raise UserError("大气条带缺少可核对的官方产品标识、字节数或源校验值。")
    if url.startswith(f"https://{SEN3_ARCHIVE_HOST}/"):
        _sen3_target(item)
    if url.startswith(f"https://{ES_HOST}/"):
        check_l2a_target(item)
    if url.startswith(f"https://{L3_MIRROR_HOST}/"):
        check_l3_target(item)
    kwargs = {}
    if channel == "cdse-s3" and is_cdse_url(url):
        if s3_auth is None or not s3_auth.connected:
            raise UserError("请先在软件中连接 CDSE S3 密钥。")
        kwargs["auth"] = s3_auth
    elif channel == "pc-public" and landsat_blob_object(url) and pc_auth is not None:
        kwargs["auth"] = pc_auth
    elif channel != "public" or not is_public_url(url):
        raise UserError("原始数据通道或对象来源未通过检查。")
    for attempt in range(2):
        response = http.request("GET", url, headers=headers, cancel=cancel, status=status, stream=True, timeout=(15, 95), **kwargs)
        if channel == "pc-public" and response.status_code in (401, 403) and attempt == 0:
            response.close()
            pc_auth.clear()
            continue
        break
    if response.status_code in (301, 302, 303, 307, 308):
        response.close()
        raise UserError("原始对象返回重定向，程序未访问新地址或转发任何凭据。")
    return response


def _strong_tag(value):
    return value if isinstance(value, str) and re.fullmatch(r'"[^"\r\n]+"', value) else None


def _integer(value):
    return int(value) if isinstance(value, str) and re.fullmatch(r"[0-9]{1,21}", value) else None


def _blob_md5(response):
    value = response.headers.get("x-ms-blob-content-md5") or (response.headers.get("Content-MD5") if response.status_code == 200 else None)
    if not value:
        return None
    try:
        digest = base64.b64decode(value, validate=True)
        if len(digest) != 16:
            raise ValueError
    except (TypeError, ValueError, binascii.Error):
        raise UserError("Landsat 文件返回无效的源校验值，未保存。") from None
    return digest.hex()


def _s3_denied(response, cancel):
    """Only recognized error codes become messages; never expose the service's credential-bearing XML."""
    fallback = "CDSE S3 未授权访问此对象；请检查密钥、有效期、系统时间和数据权限。"
    body = bytearray()
    try:
        for block in response.iter_content(4096):
            check_cancel(cancel)
            body.extend(block)
            if len(body) > 16384:
                return fallback
        if b"<!DOCTYPE" in body.upper() or b"<!ENTITY" in body.upper():
            return fallback
        root = ET.fromstring(body)
        if root.tag.rsplit("}", 1)[-1] != "Error":
            return fallback
        code = next((node.text for node in root if node.tag.rsplit("}", 1)[-1] == "Code"), None)
    except (ET.ParseError, ValueError, *TRANSFER_ERRORS):
        return fallback
    messages = {
        "AccessDenied": "CDSE S3 拒绝访问所选对象（AccessDenied）；请检查该产品的数据访问权限和密钥有效期。若所选产品提供 OData，可切换该通道。",
        "SignatureDoesNotMatch": "CDSE S3 签名校验失败（SignatureDoesNotMatch）；请重新复制同一条密钥的 Access Key 与 Secret Key 并连接保存，同时核对系统时间。",
        "InvalidAccessKeyId": "CDSE S3 Access Key 不存在或已失效（InvalidAccessKeyId）；请从 S3 Keys Manager 复制有效的成对密钥并连接保存。",
        "RequestTimeTooSkewed": "CDSE S3 请求时间与服务器不一致（RequestTimeTooSkewed）；请同步系统日期、时间和时区后重试。",
        "RequestExpired": "CDSE S3 请求时间已过期（RequestExpired）；请同步系统时间后重试。",
        "ExpiredToken": "CDSE S3 凭据已过期（ExpiredToken）；请更新密钥并连接保存。",
        "InvalidToken": "CDSE S3 凭据无效（InvalidToken）；请更新有效的成对密钥并连接保存。",
    }
    return messages.get(code, fallback)


def _stream(http, auth, item, partial, cancel, progress, *, s3_auth=None, pc_auth=None, pinned_etag=None, pinned_md5=None):
    tag = total = blob_md5 = None
    channel = item.get("transport", "odata")
    kind = (item.get("identity") or {}).get("kind")
    pinned = pinned_etag or (item.get("identity") or {}).get("archive_etag")
    for attempt in range(4):
        check_cancel(cancel)
        position = partial.stat().st_size if partial.exists() and tag else 0
        headers = {"Accept-Encoding": "identity"}
        if pinned:
            headers["If-Match"] = pinned
        if position:
            conditional = "If-Range" if channel == "odata" else "If-Match"
            headers.update({"Range": f"bytes={position}-", conditional: tag})
        response = _transport_get(http, auth, s3_auth, item, headers, cancel, lambda msg: progress(msg, None, None), pc_auth=pc_auth)
        try:
            if response.status_code in (401, 403, 404, 410) and kind == WORLDCOVER:
                raise UserError("ESA WorldCover 已不提供所选对象；未换用其他年份、瓦片或图层，请重新检索。")
            if response.status_code in (401, 403, 404, 410) and kind == L3_MOSAIC:
                raise UserError("L3 镜像没有提供所选瓦片或波段，不会替换其他季度或产品。")
            if response.status_code in (401, 403, 404) and kind == "s5p-swath":
                raise UserError("公开档案未提供所选 Sentinel-5P 版本；没有改用别的处理流或产品。可检查 CDSE 官方原始产品路线。")
            if response.status_code in (401, 403, 404) and (item.get("identity") or {}).get("kind") == "sen3-member":
                raise UserError("公开档案不再提供所选 SEN3 原始成员。未换用其他版本，请重新检索。")
            if response.status_code in (401, 403):
                if channel == "cdse-s3":
                    raise UserError(_s3_denied(response, cancel))
                if channel == "pc-public":
                    raise UserError("公开 Landsat 服务未授权当前对象；程序未切换到其他产品。")
                if channel == "public":
                    raise UserError("公开源拒绝访问选中的对象，未替换其他产品。")
                raise UserError("CDSE 未授权下载此产品；请检查账号、令牌和数据访问权限。")
            if response.status_code == 412:
                if pinned:
                    raise UserError("公开档案对象版本在检索后已变化，请重新检索；未保存文件。")
                raise UserError("S3 对象版本在续传前已变化，没有拼接或发布此文件。")
            if response.status_code == 404:
                raise UserError("官方服务不再提供该产品，请重新检索。")
            if response.status_code not in (200, 206):
                raise UserError(f"原始产品下载未开始（HTTP {response.status_code}）。")
            content_type = response.headers.get("Content-Type", "").split(";")[0].strip().lower()
            if (content_type in ("text/html", "application/problem+json") or
                    content_type == "application/json" and not item["name"].lower().endswith(".json") or
                    response.headers.get("Content-Encoding", "identity").lower() != "identity"):
                raise UserError("服务返回的不是原始产品文件。")
            if kind == L3_MOSAIC and (content_type not in ("image/tiff", "application/octet-stream")
                                      or response.status_code == 200 and _integer(response.headers.get("Content-Length")) != item["bytes"]):
                raise UserError("L3 镜像返回的原始 COG 格式或字节长度不符，未保存。")
            if kind == WORLDCOVER and (not check_worldcover_headers(response.headers)
                                       or response.status_code == 200 and _integer(response.headers.get("Content-Length")) != item["bytes"]):
                raise UserError("ESA WorldCover 返回的 COG 格式、源校验类型或长度不符，未保存。")
            received_tag = _strong_tag(response.headers.get("ETag"))
            if pinned and received_tag != pinned:
                raise UserError("公开档案返回的对象版本与检索时不同，请重新检索；未保存文件。")
            if kind == MODIS:
                verify_modis_transfer(response, item["asset"], item["bytes"], pinned, pinned_md5)
                blob_md5 = pinned_md5
            elif channel == "pc-public":
                published = _blob_md5(response)
                if position and blob_md5 and published and blob_md5 != published:
                    raise UserError("Landsat 续传返回不同的源校验值，未拼接数据。")
                blob_md5 = (published or blob_md5) if position else published
            if response.status_code == 206:
                match = BYTE_RANGE.fullmatch(response.headers.get("Content-Range", ""))
                if not position or not match:
                    raise UserError("官方服务返回了无效的续传区间。")
                start, stop, size = map(int, match.groups())
                if (start != position or not start <= stop == size - 1 or received_tag != tag or (total is not None and total != size)
                        or ("Content-Length" in response.headers and _integer(response.headers["Content-Length"]) != size-start)):
                    raise UserError("续传文件的长度或版本已变化，未拼接不匹配的数据。")
                total = size
                append = True
            else:
                if channel != "odata" and position and (received_tag != tag or (total is not None and _integer(response.headers.get("Content-Length")) != total)):
                    raise UserError("续传服务器返回了不同的对象版本或长度，未保存。")
                tag, total, position, append = received_tag, _integer(response.headers.get("Content-Length")), 0, False
                if total == 0 or "Content-Length" in response.headers and total is None:
                    raise UserError("原始产品声明的字节长度无效。")
                if total is None and item["asset"]:
                    total = item["bytes"]
                if total is None and response.headers.get("Transfer-Encoding", "").lower().strip() != "chunked":
                    raise UserError("服务未提供可校验的文件传输长度或分块结束标记。")
                # STAC file:size is a second check for individual assets, never an assumption about ZIP size.
                if item["asset"] and item["bytes"] and total is not None and total != item["bytes"]:
                    raise UserError("服务返回长度与目录资产大小不一致，未保存该文件。")
            if total is not None and shutil.disk_usage(partial.parent).free < max(0, total-position):
                raise UserError("输出目录剩余磁盘空间不足，未开始写入。")
            written, last_update = position, 0.0
            with partial.open("ab" if append else "wb") as out:
                for block in response.iter_content(CHUNK):
                    check_cancel(cancel)
                    if not block:
                        continue
                    written += len(block)
                    if total is not None and written > total:
                        raise UserError("文件传输超过服务声明的长度，已拒绝保存。")
                    out.write(block)
                    if time.monotonic() - last_update >= .3:
                        progress("接收原始产品", written, total)
                        last_update = time.monotonic()
            if not written or (total is not None and written != total):
                raise requests.exceptions.ChunkedEncodingError("incomplete")
            if item["asset"] and item["bytes"] and item["bytes"] != written:
                raise UserError("下载字节数与所选目录资产不一致。")
            # AWS documents MD5 ETags for single-part plaintext/SSE-S3 objects, not multipart or KMS/SSE-C objects.
            aws_md5 = (tag.strip('"').lower() if channel == "public" and kind not in (L3_MOSAIC, WORLDCOVER) and re.fullmatch(r'"[a-fA-F0-9]{32}"', tag or "")
                       and response.headers.get("X-Amz-Server-Side-Encryption", "") in ("", "AES256")
                       and not response.headers.get("X-Amz-Server-Side-Encryption-Customer-Algorithm") else None)
            return {"size": written, "etag": tag, "content_type": content_type, "expected_bytes": total,
                    "transfer_attempts": attempt + 1, "aws_single_part_md5": aws_md5, "azure_blob_md5": blob_md5}
        except TRANSFER_ERRORS:
            if attempt == 3:
                raise UserError("原始产品传输多次中断，没有发布不完整文件。") from None
            progress("传输中断，正在验证并重试当前文件", None, None)
        finally:
            response.close()
    raise UserError("下载未完成。")


def _stem(value):
    value = value.replace("\\", "/").rsplit("/", 1)[-1].casefold()
    for ext in (".zip", ".safe", ".sen3", ".nc", ".tif", ".tiff", ".jp2", ".json", ".xml", ".tar", ".gz"):
        if value.endswith(ext):
            return value[:-len(ext)]
    return value


def _validate_archive(path, item, cancel):
    observed = set()
    nonempty = 0
    try:
        with zipfile.ZipFile(path) as bundle:
            contents = bundle.infolist()
            if not contents or len(contents) > 150_000:
                raise ValueError
            seen = set()
            for member in contents:
                check_cancel(cancel)
                if member.filename in seen:
                    raise ValueError
                seen.add(member.filename)
                name = member.filename
                parts = PurePosixPath(name).parts
                if not parts or name.startswith(("/", "\\")) or any(c in name for c in "\\:\x00") or ".." in parts:
                    raise ValueError
                if member.is_dir():
                    continue
                nonempty += member.file_size
                entry = _stem(parts[0])
                if len(parts) == 1:
                    # Some official archives are flat (e.g. Landsat and single-file S5P).
                    exact = _stem(item["key"])
                    if entry.startswith((exact + "_", exact + "-")):
                        entry = exact
                observed.add(entry)
                # Validate all members including CRC without extracting to the filesystem.
                with bundle.open(member) as handle:
                    while handle.read(CHUNK):
                        check_cancel(cancel)
    except (zipfile.BadZipFile, RuntimeError, NotImplementedError, ValueError, EOFError, OSError, zlib.error):
        raise UserError("产品 ZIP 结构或内容校验失败，未发布损坏的产品。") from None
    expected = {_stem(name) for name in item["names"]} | {_stem(item["name"])}
    signed_asset = item.get("provider_sha256") or item.get("provider_md5")
    if not nonempty or not observed or (not signed_asset and not observed <= expected):
        raise UserError("ZIP 内的产品标识与目录选择不符，未保存。")
    return ".zip"


def _validate(path, item, cancel):
    with path.open("rb") as f:
        prefix = f.read(4096)
    normalized = prefix.lstrip(b"\xef\xbb\xbf\xff\xfe\x00\t\r\n ")
    if not prefix or normalized.lower().startswith((b"<!doctype html", b"<html", b'{"error"', b'{"detail"')):
        raise UserError("下载内容不是完整的原始产品。")
    suffix = Path(item["name"]).suffix.lower()
    if prefix[:4] in (b"PK\x03\x04", b"PK\x05\x06"):
        if item["asset"] and suffix != ".zip":
            raise UserError("下载文件为 ZIP，但目录所选资产不是 ZIP，未保存。")
        return _validate_archive(path, item, cancel)
    if prefix.startswith(b"\x0e\x03\x13\x01") and suffix in (".hdf", ".h4") and path.stat().st_size > 64:
        return suffix
    if prefix.startswith((b"CDF\x01", b"CDF\x02", b"CDF\x05", b"\x89HDF\r\n\x1a\n")):
        if suffix not in ("", ".nc", ".h5", ".hdf", ".hdf5") or path.stat().st_size < 16:
            raise UserError("原始产品文件头与目录所列类型不符。")
        return suffix or ".nc"
    if prefix[:4] in (b"II*\0", b"MM\0*", b"II+\0", b"MM\0+"):
        if suffix not in ("", ".tif", ".tiff"):
            raise UserError("原始产品文件头与目录所列类型不符。")
        _check_raw_raster(path, cancel, item.get("raster"))
        return suffix or ".tif"
    if prefix.startswith(b"\x00\x00\x00\x0cjP  \r\n\x87\n") and suffix in ("", ".jp2"):
        _check_raw_raster(path, cancel, item.get("raster"))
        return ".jp2"
    if item["asset"] and suffix in (".xml", ".safe") and normalized.startswith(b"<") and path.stat().st_size <= 100*1024*1024:
        try:
            root = ET.parse(path).getroot()
            if root.tag.lower().rsplit("}",1)[-1] in ("error", "exception", "exceptionreport", "html"):
                raise ValueError
        except (ET.ParseError, ValueError):
            raise UserError("元数据 XML 校验失败。") from None
        if item["url"].startswith("https://" + S1_AWS_HOST + "/") and item["name"].lower() == "manifest.safe":
            _verify_radar_manifest(root, item["key"], path, cancel)
        if (item.get("identity") or {}).get("kind") == "l2a-index":
            check_cancel(cancel)
            verify_l2a_xml(root, item["identity"], item["name"])
        return suffix
    if item["asset"] and suffix == ".json" and path.stat().st_size <= 100*1024*1024:
        try:
            doc = json.loads(path.read_bytes())
            if isinstance(doc, dict) and ({"error", "exception"} & doc.keys()):
                raise ValueError
            identity = item.get("identity") or {}
            if identity.get("kind") == "landsat-mtl":
                if not _landsat_mtl_matches(doc, identity):
                    raise UserError("Landsat MTL 的产品编号、日期或轨道与所选文件不符。")
        except (ValueError, UnicodeError):
            raise UserError("元数据 JSON 校验失败。") from None
        return suffix
    if item["asset"] and suffix == ".txt" and path.stat().st_size <= 16*1024*1024:
        try:
            text = path.read_text(encoding="utf-8-sig")
            if not text.strip() or any(ord(c) < 32 and c not in "\r\n\t" for c in text):
                raise ValueError
        except (ValueError, UnicodeError):
            raise UserError("原始 TXT 元数据格式不符。") from None
        return suffix
    raise UserError("下载内容不是可校验的 ZIP、NetCDF、GeoTIFF、JPEG2000 或目录元数据；未保存。")


def _verify_radar_manifest(root, product, path, cancel):
    try:
        platform, mode, _, level, begin, end, orbit, take, suffix = product.split("_")
        pol = {"SV": {"VV"}, "SH": {"HH"}, "DV": {"VV", "VH"}, "DH": {"HH", "HV"}}[level[-2:]]
        start, stop = (dt.datetime.strptime(value, "%Y%m%dT%H%M%S") for value in (begin, end))
        named = {}
        for element in root.iter():
            tag = element.tag.rsplit("}", 1)[-1]
            if tag == "orbitNumber" and element.get("type") not in (None, "start"):
                continue
            named.setdefault(tag, []).append((element.text or "").strip())
        if (root.tag.rsplit("}", 1)[-1].lower() != "xfdu" or "SENTINEL-1" not in named.get("familyName", [])
                or platform[-1] not in named.get("number", []) or named.get("productType") != ["GRD"]
                or not ({mode, "SM"} if re.fullmatch(r"S[1-6]", mode) else {mode}).intersection(named.get("mode", []))
                or pol != set(named.get("transmitterReceiverPolarisation", []))
                or int(named["orbitNumber"][0]) != int(orbit) or int(named["missionDataTakeID"][0]) != int(take, 16)
                or dt.datetime.fromisoformat(named["startTime"][0][:19]) != start
                or dt.datetime.fromisoformat(named["stopTime"][0][:19]) != stop):
            raise ValueError
    except (IndexError, KeyError, ValueError):
        raise UserError("公开雷达 SAFE 清单的卫星、轨道、极化或采集时间与所选产品不符。") from None
    # The Sentinel-1 SAFE product's four-character unique identifier is the CRC-16 of its manifest.
    crc = 0xFFFF
    with path.open("rb") as source:
        while block := source.read(CHUNK):
            check_cancel(cancel)
            crc = binascii.crc_hqx(block, crc)
    if f"{crc:04X}" != suffix:
        raise UserError("公开雷达 SAFE 清单的 CRC-16 与所选产品标识不一致，未保存。")


def _landsat_mtl_matches(doc, identity):
    try:
        contents = doc["LANDSAT_METADATA_FILE"]["PRODUCT_CONTENTS"]
        attrs = doc["LANDSAT_METADATA_FILE"]["IMAGE_ATTRIBUTES"]
        return (contents["LANDSAT_PRODUCT_ID"] == identity["product"] and attrs["DATE_ACQUIRED"] == identity["date"]
                and int(attrs["WRS_PATH"]) == identity["path"] and int(attrs["WRS_ROW"]) == identity["row"])
    except (TypeError, ValueError, KeyError):
        return False


def _verify_l1c_index(http, item, cancel, expected_etag=None):
    proof = item["identity"]
    url = proof["url"]
    if (not url.startswith(f"https://{S2_L1C_AWS}/tiles/") or not is_public_url(url) or not url.endswith("/tileInfo.json")
            or url.rsplit("/", 1)[0] != item["url"].rsplit("/", 1)[0]):
        raise UserError("L1C 瓦片索引地址与原始文件不一致。")
    headers = {"Accept-Encoding": "identity"}
    if expected_etag:
        headers["If-Match"] = expected_etag
    response = http.request("HEAD" if expected_etag else "GET", url, headers=headers, stream=True, timeout=(10, 30), cancel=cancel)
    try:
        tag = _strong_tag(response.headers.get("ETag"))
        if response.status_code != 200 or not tag or expected_etag and expected_etag != tag:
            raise UserError("L1C 瓦片索引不可用或版本已变化，未发布原始文件。")
        if not expected_etag:
            data = bytearray()
            for block in response.iter_content(8192):
                check_cancel(cancel)
                data.extend(block)
                if len(data) > 65536:
                    raise ValueError
            index = json.loads(data)
            if (index["path"] != proof["path"] or index["productName"] != proof["product"]
                    or utc_time(index["timestamp"]) != utc_time(proof["datetime"])):
                raise ValueError
        return tag
    except (KeyError, TypeError, AttributeError, ValueError, *TRANSFER_ERRORS):
        raise UserError("L1C 索引的 SAFE 编号、路径或采集时间与所选产品不符，未保存。") from None
    finally:
        response.close()


def _check_raw_raster(path, cancel, expected=None):
    try:
        with rasterio.Env(GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR", GDAL_PAM_ENABLED="NO"):
            with rasterio.open(path) as dataset:
                if not dataset.count or dataset.width <= 0 or dataset.height <= 0:
                    raise ValueError
                if expected is not None:
                    trans = dataset.transform
                    cell = (abs(trans.a), abs(trans.e)) * 2
                    count = expected.get("count", 1)
                    mismatch = (dataset.crs is None or dataset.crs.to_epsg() != expected["epsg"] or dataset.count != count
                                or dataset.dtypes != (expected["dtype"],) * count or trans.a <= 0 or trans.e >= 0 or trans.b or trans.d
                                or ("nodata" in expected and dataset.nodatavals != (expected["nodata"],) * count)
                                or ("scale" in expected and (dataset.scales != (expected["scale"],) * count
                                                             or dataset.offsets != (expected["offset"],) * count))
                                or ("layout" in expected and dataset.tags(ns="IMAGE_STRUCTURE").get("LAYOUT") != expected["layout"])
                                or (expected.get("height") and (dataset.height != expected["height"] or abs(cell[1] * dataset.height - 1) > 1e-6))
                                or (expected.get("shape") and dataset.shape != tuple(expected["shape"]))
                                or (expected.get("transform") and any(abs(x-y) > 1e-6 for x, y in zip(tuple(trans)[:6], expected["transform"])))
                                or (expected.get("bounds") and any(abs(x-y) > res*1.1 for x, y, res in zip(dataset.bounds, expected["bounds"], cell))))
                    if mismatch:
                        raise UserError("原始栅格的位置、大小、波段或类型与所选瓦片/目录网格不一致。")
                    if "tags" in expected:
                        tags = dataset.tags()
                        if any(tags.get(key) != val for key, val in expected["tags"].items()):
                            raise UserError("原始栅格的产品标识、日期或波段顺序与所选瓦片不一致。")
                    if "colorinterp" in expected and [value.name for value in dataset.colorinterp] != expected["colorinterp"]:
                        raise UserError("原始栅格的调色板与土地覆盖产品不一致。")
                # Tolerate JP2 without georeferencing only when no expected catalogue grid was supplied.
                if expected and count > 1 and len(set(dataset.block_shapes)) == 1:
                    for _, window in dataset.block_windows(1):
                        check_cancel(cancel)
                        dataset.read(window=window)
                else:
                    for band in dataset.indexes:
                        for _, window in dataset.block_windows(band):
                            check_cancel(cancel)
                            dataset.read(band, window=window)
    except (ValueError, rasterio.errors.RasterioError):
        raise UserError("原始栅格无法完整解码，未保存。") from None


def _validated_digests(path, item, aws_md5, cancel, azure_md5=None):
    local_sha = hashlib.sha256()
    label = "ASF DAAC ZIP MD5" if is_slc(item) else "MD5"
    upstream = [(k, v) for k, v in ((label, item.get("provider_md5")), ("SEN3 XFDU MD5", (item.get("identity") or {}).get("xfdu_md5")),
                                   ("AWS single-part ETag (MD5)", aws_md5),
                                   ("Azure Blob Content-MD5", azure_md5)) if v]
    local_md5 = hashlib.md5(usedforsecurity=False) if upstream else None
    wanted_sha3 = item.get("provider_sha3_256")
    local_sha3 = hashlib.sha3_256() if wanted_sha3 else None
    with path.open("rb") as file:
        while block := file.read(CHUNK):
            check_cancel(cancel)
            local_sha.update(block)
            if local_md5 is not None:
                local_md5.update(block)
            if local_sha3 is not None:
                local_sha3.update(block)
    digest, verified = local_sha.hexdigest(), []
    if item.get("provider_sha256"):
        if item["provider_sha256"] != digest:
            raise UserError("下载文件与目录发布的 SHA-256 不符，未保存。")
        verified.append("SHA-256")
    if local_sha3 is not None:
        if wanted_sha3 != local_sha3.hexdigest():
            raise UserError("下载文件与目录发布的 SHA3-256 不符，未保存。")
        verified.append("SHA3-256")
    for label, wanted in upstream:
        if wanted != local_md5.hexdigest():
            raise UserError("下载文件与源服务的 MD5 不符，未保存。")
        verified.append(label)
    return digest, (" + ".join(verified) if verified else None)


def _cached_digests(path, item, meta, cancel):
    # The current catalogue remains authoritative even if both cached bytes and the local SHA-256 were replaced.
    recorded = meta.get("source_checksums", {})
    if not isinstance(recorded, dict):
        raise UserError("已有原始文件的源校验记录无效，请移走文件和同名 .starmap.json 后重试。")
    expected = item.copy()
    for label, field, digits in SOURCE_CHECKSUM_FIELDS:
        value = recorded.get(label)
        if value is not None:
            if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{" + str(digits) + "}", value):
                raise UserError("已有原始文件的源校验记录无效，请移走文件和同名 .starmap.json 后重试。")
            if not expected.get(field):
                expected[field] = value
    aws_md5 = meta.get("aws_single_part_md5") if item["transport"] == "public" else None
    azure_md5 = meta.get("azure_blob_md5") if item["transport"] == "pc-public" else None
    if any(value is not None and (not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{32}", value))
           for value in (aws_md5, azure_md5)):
        raise UserError("已有原始文件的源 MD5 记录无效，请移走文件和同名 .starmap.json 后重试。")
    try:
        return _validated_digests(path, expected, aws_md5, cancel, azure_md5)
    except UserError as error:
        raise UserError(f"已有原始文件的完整性校验失败：{error} 请移走文件和同名 .starmap.json 后重试。") from None


def _basename(item):
    if is_slc(item):
        return check_slc_target(item).product  # Preserve the original SAFE ZIP filename.
    if is_modis(item):
        check_modis_target(item)
        return Path(item["name"]).stem  # Keep original HDF and HDF.XML sibling filenames.
    title = re.sub(r"[^A-Za-z0-9_.-]", "_", _stem(item["name"]))[:88].strip(" ._") or "product"
    fields = (item["key"], item["url"], item.get("bytes"), item.get("provider_sha256"), item.get("provider_md5"))
    if item.get("provider_sha3_256"):
        fields += (item["provider_sha3_256"],)
    if item.get("identity"):
        fields += (item["identity"],)
    identity = hashlib.sha256(json.dumps(fields, separators=(",", ":")).encode()).hexdigest()[:12]
    return f"{title}_{identity}"


def download_original(geometry, plan, output_dir, auth, *, s3_auth=None, cancel=None, progress=None):
    base = io_path(output_dir)
    if not base.is_absolute():
        raise UserError("请选择本地绝对输出目录。")
    folder = base / plan["folder_name"]
    folder.mkdir(parents=True, exist_ok=True)
    uid = uuid4().hex[:10]
    record_path = folder / f"下载记录_{uid}.json"
    _save_json(folder / "研究区.geojson", {"type": "Feature", "properties": {}, "geometry": mapping(geometry)})
    manifest = {"software": VERSION, "source": plan["source"], "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                "collection": plan["options"]["collection"], "status": "running", "items": []}
    _save_json(record_path, manifest)
    http, result = HttpClient(), []
    pc_auth = PlanetaryAccess(http, cancel)
    modis_access = {scope: PlanetaryAccess(http, cancel, scope=scope) for scope in ("modis-cog", "modis-source")} if any(
        is_modis(file) for file in plan["targets"]) else {}
    l1c_tags, l2a_tags = {}, {}
    total = len(plan["targets"])
    try:
        for index, item in enumerate(plan["targets"]):
            check_cancel(cancel)
            name = _basename(item)
            receipt = folder / f"{name}.starmap.json"
            partial = folder / f".{name}.{uid}.part"
            row = {"product": item["key"], "uuid": item["uuid"], "asset": item["asset"], "url": item["url"],
                   "transport": item["transport"], "status": "running"}
            manifest["items"].append(row)
            proof = item.get("identity") or {}
            l2a = item["url"].startswith(f"https://{ES_HOST}/") or proof.get("kind") == "l2a-index"
            l3 = item["url"].startswith(f"https://{L3_MIRROR_HOST}/") or proof.get("kind") == L3_MOSAIC
            wc = _is_worldcover(item)
            modis = is_modis(item)
            slc = is_slc(item)

            def report(message, received, size):
                if progress:
                    extra = min(.94, received/size*.94) if received is not None and size else 0.0
                    progress(f"{index + 1}/{total} · {item['name']} · {message}", (index + extra)/total,
                             {"folder": str(folder), "received_bytes": received, "total_bytes": size, "completed_scenes": index, "total_scenes": total})

            try:
                if l2a:
                    check_l2a_target(item)
                    row.update(catalogue_identity=proof, catalogue_sha256=item["provider_sha256"], catalogue_raster=item.get("raster"))
                if l3:
                    check_l3_target(item)
                    row.update(catalogue_identity=proof, catalogue_raster=item["raster"], reflectance_scale=proof["reflectance_scale"])
                wc_part_bytes = None
                if wc:
                    check_worldcover_target(item)
                    row.update(catalogue_identity=proof, catalogue_raster=item["raster"])
                    report("核对 ESA 年度瓦片对象版本和源校验", None, None)
                    wc_part_bytes = pin_worldcover(http, item, cancel)
                if modis:
                    row.update(catalogue_identity=proof, catalogue_raster=item["raster"])
                if slc:
                    check_slc_target(item)
                    row.update(catalogue_identity=proof, asf_zip_md5=item["provider_md5"])
                if receipt.exists():
                    try:
                        meta = json.loads(receipt.read_text(encoding="utf-8"))
                        if not isinstance(meta, dict):
                            raise ValueError
                    except (ValueError, OSError):
                        raise UserError("已有原始文件的记录无法读取；请移动文件和同名 .starmap.json 后重试。") from None
                    filename = meta.get("file")
                    if not isinstance(filename, str) or not re.fullmatch(re.escape(name) + r"\.[a-zA-Z0-9]{1,7}", filename):
                        raise UserError("已存在的文件记录无效；请选择新的输出文件夹。")
                    target = folder / filename
                    report("校验已有文件", None, None)
                    if (meta.get("url") != item["url"] or not target.is_file() or target.stat().st_size != meta.get("size")
                            or item.get("bytes") and meta["size"] != item["bytes"]):
                        raise UserError("已有原始文件的完整性校验失败；请移动损坏文件和同名 .starmap.json 后重试。")
                    expected_identity = {"product": item["key"], "uuid": item["uuid"], "asset": item["asset"], "transport": item["transport"]}
                    if any(field in meta and meta[field] != value for field, value in expected_identity.items()):
                        raise UserError("已有原始文件的来源身份记录与所选产品不一致，请移走文件和同名 .starmap.json 后重试。")
                    if modis:
                        if filename != item["name"] or not isinstance(meta.get("etag"), str):
                            raise UserError("MODIS 已有文件缺少源对象记录；请将文件和同名 .starmap.json 移出后重试。")
                        pin = pin_modis(http, item, modis_access, cancel, tag=meta["etag"])
                        if pin["size"] != target.stat().st_size or pin["md5"] != meta.get("azure_blob_md5"):
                            raise UserError("MODIS 已有文件的源校验值或长度不符；请将文件和校验记录移出后重试。")
                        existing_digest, verified = _validated_digests(target, item, None, cancel, pin["md5"])
                        if existing_digest == meta.get("sha256"):
                            checked = verify_modis_file(target, item, pin, cancel)
                            row.update(etag=pin["etag"], size=pin["size"], azure_blob_md5=pin["md5"], provider_checksum_verified=verified,
                                       modis_validation=checked, format_validation=checked["format_validation"])
                    elif slc:
                        if (filename != item["name"] or meta.get("etag") != proof["archive_etag"]
                                or meta.get("asf_zip_md5") != item["provider_md5"]):
                            raise UserError("已有 SLC ZIP 的源版本记录与本次检索不符；请移出文件和校验记录后重试。")
                        existing_digest, verified = _validated_digests(target, item, None, cancel)
                        if existing_digest == meta.get("sha256"):
                            report("复核 SAFE 产品与全部成员", item["bytes"], item["bytes"])
                            row.update(etag=proof["archive_etag"], size=item["bytes"], provider_checksum_verified=verified,
                                       slc_validation=verify_slc_archive(target, item, cancel), format_validation=SLC_VALIDATION)
                    elif (item.get("identity") or {}).get("kind") == "s5p-swath":
                        existing_digest, verified = _validated_digests(target, item, None, cancel)
                        if not verified or not isinstance(meta.get("s5p_validation"), dict):
                            raise UserError("已有 NetCDF 缺少源校验或条带验证记录；请移动文件和同名 .starmap.json 后重试。")
                        row.update(format_validation=meta.get("format_validation"), s5p_validation=meta["s5p_validation"],
                                   provider_checksum_verified=verified)
                    elif wc:
                        if (filename != name + ".tif" or meta.get("etag") != proof["archive_etag"]
                                or meta.get("worldcover_grid_sha256") != proof["grid_sha256"]):
                            raise UserError("已有 ESA WorldCover 文件的源版本记录不符；请移出文件与同名校验记录后重试。")
                        existing_digest, verified = verify_worldcover_bytes(target, item, wc_part_bytes, cancel)
                        row.update(etag=proof["archive_etag"], size=item["bytes"], provider_checksum_verified=verified,
                                   worldcover_grid_sha256=proof["grid_sha256"], worldcover_upload_part_bytes=wc_part_bytes,
                                   format_validation=meta.get("format_validation"))
                    elif l3:
                        if (filename != name + ".tif" or not L3_ETAG.fullmatch(meta.get("etag") or "")
                                or meta.get("l3_index_sha256") != proof["index_sha256"]):
                            raise UserError("已有 L3 文件缺少已验证的镜像版本记录；请移出文件与校验记录后重试。")
                        existing_digest, verified = _validated_digests(target, item, None, cancel)
                        if existing_digest == meta.get("sha256"):
                            verify_l3_listing(http, item, cancel)
                            row.update(etag=pin_l3_object(http, item, cancel, meta["etag"]), size=item["bytes"],
                                       provider_checksum_verified=verified, format_validation=meta.get("format_validation"),
                                       l3_index_sha256=proof["index_sha256"], l3_index_etag=proof["index_etag"])
                    else:
                        existing_digest, verified = _cached_digests(target, item, meta, cancel)
                        row["provider_checksum_verified"] = verified or meta.get("provider_checksum_verified")
                    if existing_digest != meta.get("sha256") or l2a and (
                            existing_digest != item["provider_sha256"] or meta.get("l2a_index_sha256") != proof["index_sha256"]
                            or filename != name + Path(item["name"]).suffix):
                        raise UserError("已有原始文件的完整性校验失败；请移动损坏文件和同名 .starmap.json 后重试。")
                    if l2a:
                        row.update(provider_checksum_verified="SHA-256", l2a_index_sha256=proof["index_sha256"],
                                   safe_product=proof["product"], format_validation=meta.get("format_validation"))
                    for field in CACHED_EVIDENCE_FIELDS:
                        if field in meta:
                            row.setdefault(field, meta[field])
                    row.update(status="skipped", path=str(target), sha256=existing_digest, reused_receipt=receipt.name)
                else:
                    if slc and (folder / item["name"]).exists():
                        raise UserError("输出目录中已有同名 SLC ZIP 但没有校验记录，未覆盖。")
                    modis_pin = None
                    if modis:
                        if (folder / item["name"]).exists():
                            raise UserError("输出目录中已有同名 MODIS 文件但没有校验记录，未覆盖。")
                        report("核对源对象版本、完整 MD5 和配套元数据", None, None)
                        modis_pin = pin_modis(http, item, modis_access, cancel)
                        item = {**item, "bytes": modis_pin["size"]}
                    official = None
                    if item["transport"] == "odata":
                        report("核对所选 OData 产品", None, None)
                        official = _verify_product_identity(http, item, cancel)
                        row["odata_name"] = official["Name"]
                    tile_tag = None
                    if proof.get("kind") == "l1c-index":
                        report("核对 SAFE 和瓦片索引", None, None)
                        tile_tag = l1c_tags.get(proof["url"]) or _verify_l1c_index(http, item, cancel)
                        l1c_tags[proof["url"]] = tile_tag
                    if l2a:
                        report("核对 L2A 瓦片索引、采集时间与原网格", None, None)
                        cache_key = json.dumps(proof, sort_keys=True, separators=(",", ":"))
                        tile_tag = l2a_tags.get(cache_key) or verify_l2a_index(http, item, cancel)
                        l2a_tags[cache_key] = tile_tag
                    archive_tag = None
                    if l3:
                        report("核对季度瓦片索引和镜像文件版本", None, None)
                        verify_l3_listing(http, item, cancel)
                        archive_tag = pin_l3_object(http, item, cancel)
                    report("准备下载", None, None)
                    if slc:
                        transfer = transfer_slc(item, partial, cancel, report)
                    elif archive_tag:
                        transfer = _stream(http, auth, item, partial, cancel, report, s3_auth=s3_auth, pc_auth=pc_auth,
                                           pinned_etag=archive_tag)
                    elif modis:
                        transfer = _stream(http, auth, item, partial, cancel, report, pc_auth=modis_access[modis_scope(item["asset"])],
                                           pinned_etag=modis_pin["etag"], pinned_md5=modis_pin["md5"])
                    else:
                        transfer = _stream(http, auth, item, partial, cancel, report, s3_auth=s3_auth, pc_auth=pc_auth)
                    if tile_tag and not l2a:
                        transfer["l1c_index_etag"] = _verify_l1c_index(http, item, cancel, tile_tag)
                        transfer["safe_product"] = proof["product"]
                    report("校验产品格式与完整性", transfer["size"], transfer["size"])
                    validation_item = {**item, "name": official["Name"]} if official and not item["asset"] else item
                    digest = None
                    if wc:
                        digest, verified = verify_worldcover_bytes(partial, validation_item, wc_part_bytes, cancel)
                        transfer.update(worldcover_upload_part_bytes=wc_part_bytes, worldcover_grid_sha256=proof["grid_sha256"])
                    elif l2a or l3:  # Reject mismatched bytes before opening with a raster or XML decoder.
                        digest, verified = _validated_digests(partial, validation_item, transfer["aws_single_part_md5"], cancel)
                    if slc:
                        digest, verified = _validated_digests(partial, validation_item, None, cancel)
                        report("核对 SAFE 产品、全部成员与复数测量", transfer["size"], transfer["size"])
                        transfer.update(asf_zip_md5=item["provider_md5"], slc_validation=verify_slc_archive(partial, item, cancel))
                        suffix = ".zip"
                    elif modis:
                        digest, verified = _validated_digests(partial, validation_item, None, cancel, modis_pin["md5"])
                        transfer["modis_validation"] = verify_modis_file(partial, validation_item, modis_pin, cancel)
                        suffix = Path(item["name"]).suffix
                    else:
                        suffix = _validate(partial, validation_item, cancel)
                    if l2a:
                        transfer.update(l2a_index_etag=verify_l2a_index(http, item, cancel, tile_tag),
                                        l2a_index_sha256=proof["index_sha256"], safe_product=proof["product"])
                    if l3:
                        pin_l3_object(http, item, cancel, archive_tag)
                        transfer.update(l3_index_etag=verify_l3_listing(http, item, cancel), l3_index_sha256=proof["index_sha256"],
                                        reflectance_scale=proof["reflectance_scale"])
                    if official and not item["asset"] and suffix != ".zip":
                        if Path(official["Name"]).suffix.lower() != suffix:
                            raise UserError("原始产品格式与 OData 名称不符，未保存。")
                        length = official.get("ContentLength")
                        if type(length) is int and length > 0 and transfer["size"] != length:
                            raise UserError("原始文件字节数与 OData 元数据不一致。")
                        for entry in official.get("Checksum") or []:
                            if isinstance(entry, dict):
                                algorithm, value = entry.get("Algorithm"), entry.get("Value")
                                patterns = {"MD5": r"[0-9a-fA-F]{32}", "SHA256": r"[0-9a-fA-F]{64}", "SHA-256": r"[0-9a-fA-F]{64}"}
                                if isinstance(value, str) and isinstance(algorithm, str) and algorithm in patterns and re.fullmatch(patterns[algorithm], value):
                                    validation_item["provider_md5" if algorithm == "MD5" else "provider_sha256"] = value.lower()
                    if suffix in (".nc", ".h5", ".hdf5", ".hdf", ".h4") and transfer["expected_bytes"] is None and not any(validation_item.get(k) for k in ("provider_md5", "provider_sha256", "provider_sha3_256")):
                        raise UserError("科学数据文件缺少可验证的字节数或目录校验值，未保存。")
                    if digest is None:
                        digest, verified = _validated_digests(partial, validation_item, transfer["aws_single_part_md5"], cancel, transfer["azure_blob_md5"])
                    transfer["provider_checksum_verified"] = verified
                    transfer["source_checksums"] = {label: validation_item[field] for label, field, _ in SOURCE_CHECKSUM_FIELDS
                                                    if validation_item.get(field)}
                    if proof.get("kind") == "s5p-swath":
                        if suffix != ".nc" or not verified:
                            raise UserError("下载内容不是经源校验的原始 NetCDF，未保存。")
                        report("解码并校对大气条带与主要变量", transfer["size"], transfer["size"])
                        transfer["s5p_validation"] = verify_swath(partial, proof, cancel)
                    transfer["format_validation"] = (
                        SLC_VALIDATION if slc else
                        transfer["modis_validation"]["format_validation"] if modis else
                        "NetCDF swath identity and full core-array decode" if "s5p_validation" in transfer else
                        "ZIP entries and CRC" if suffix == ".zip" else
                        "full prior-version-pinned mirror COG decode, grid and length" if l3 else
                        "full COG pixel block decode, annual product/band tags and EPSG:4326 grid" if wc else
                        "full raster pixel block decode and expected tile grid" if item.get("raster") else
                        "full raster pixel block decode" if suffix in (".tif", ".tiff", ".jp2") else
                        "scientific container header only" if suffix in (".nc", ".h5", ".hdf", ".hdf5", ".h4") else
                        "SAFE XML fields and product CRC-16" if item["url"].startswith("https://" + S1_AWS_HOST + "/") and item["name"].lower() == "manifest.safe" else
                        "Landsat MTL product ID and WRS" if proof.get("kind") == "landsat-mtl" else
                        "L2A SAFE/datatake or granule/UTM metadata identity" if l2a and suffix == ".xml" else
                        "L2A index identity and catalogue SHA-256" if l2a else "metadata syntax"
                    )
                    target = folder / (name + suffix)
                    check_cancel(cancel)
                    if target.exists():
                        raise UserError("输出目录中已有同名文件，避免覆盖，未写入。")
                    publish_file(partial, target)
                    try:
                        _save_json(receipt, {"file": target.name, "url": item["url"], "product": item["key"], "uuid": item["uuid"],
                                             "asset": item["asset"], "transport": item["transport"], "sha256": digest,
                                             **({"odata_name": row["odata_name"]} if "odata_name" in row else {}), **transfer})
                    except Exception:
                        target.unlink(missing_ok=True)
                        raise
                    row.update(status="completed", path=str(target), sha256=digest, **transfer)
                result.append({"path": row["path"], "scene": item["key"], "status": row["status"]})
                _save_json(record_path, manifest)
            except Cancelled:
                row["status"] = "cancelled"
                raise
            except Exception:
                row["status"] = "failed"
                raise
            finally:
                partial.unlink(missing_ok=True)
        manifest["status"] = "completed"
        return {"files": result, "folder": str(folder), "manifest": str(record_path), "plan": plan, "empty_scenes": []}
    except Cancelled as error:
        manifest["status"] = "cancelled"
        keep_download_result(error, result, folder, record_path, plan)
        raise
    except Exception as error:
        manifest["status"] = "failed"
        keep_download_result(error, result, folder, record_path, plan)
        raise
    finally:
        try:
            _save_json(record_path, manifest)
        finally:
            pc_auth.clear()
            for access in modis_access.values():
                access.clear()
            http.close()
