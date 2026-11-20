"""Bind CDSE Sentinel-3 catalogue hits to exact, independently pinned MEEO SEN3 objects."""
from __future__ import annotations

import datetime as dt
import hashlib
import re
import xml.etree.ElementTree as ET

import requests

from .errors import UserError
from .http import HttpClient, check_cancel
from .native_catalogue import asset_details, raw_item, search_original
from .s3_source import (SEN3_ARCHIVE_HOST, SEN3_BUCKET, SEN3_MANIFEST, SEN3_NAME,
                        cdse_object, sen3_archive_parts, sen3_member_name)
from .scene_identity import utc_time

SEN3_PUBLIC = "sen3-aws-original"
ARCHIVE = f"https://{SEN3_ARCHIVE_HOST}/"
PROVIDER = "CDSE STAC / MEEO Sentinel-3 on AWS (original SEN3)"
S3_NS = "{http://s3.amazonaws.com/doc/2006-03-01/}"
SAFE = "{http://www.esa.int/safe/sentinel/1.1}"
SEN3 = "{http://www.esa.int/safe/sentinel/sentinel-3/1.0}"
XFDU_ROOT = "{urn:ccsds:schema:xfdu:1}XFDU"
SEN3_ETAG = re.compile(r'"[a-fA-F0-9]{32}(?:-[0-9]{1,5})?"', re.ASCII)
MD5_TEXT = re.compile(r"[a-fA-F0-9]{32}", re.ASCII)
LENGTH = re.compile(r"[1-9][0-9]{0,12}", re.ASCII)
SHA256_TEXT = re.compile(r"[a-f0-9]{64}", re.ASCII)
MAX_XML = 3 * 1024**2
MAX_MEMBERS = 256
MAX_SCENES = 48
DIGESTS = ("provider_md5", "provider_sha256", "provider_sha3_256")
WST_TYPE = "SL_2_WST___"
SRA_TYPE = "SR_1_SRA_A_"
SRA_MEMBER = "measurement_l1a.nc"
GHRSST_NAME = re.compile(r"(?P<start>[0-9]{14})-(?P<centre>[A-Z0-9]{3})-L2P_GHRSST-SSTskin-SLSTR(?P<sat>[AB])-"
                         r"(?P<created>[0-9]{14})-v(?P<gds>[0-9]{2}\.[0-9]{1,2})-fv(?P<version>[0-9]{2}\.[0-9]{1,2})\.nc", re.ASCII)
COLLECTIONS = {
    "sentinel-3-sl-2-lst-ntc": ("SL_2_LST___", "NTC", "SLSTR · L2 地表温度 LST", "SLSTR"),
    "sentinel-3-sl-2-lst-nrt": ("SL_2_LST___", "NRT", "SLSTR · L2 地表温度 LST", "SLSTR"),
    "sentinel-3-sl-1-rbt-ntc": ("SL_1_RBT___", "NTC", "SLSTR · L1 辐亮度与亮温 RBT", "SLSTR"),
    "sentinel-3-sl-2-aod-nrt": ("SL_2_AOD___", "NRT", "SLSTR · L2 气溶胶光学厚度 AOD", "SLSTR"),
    "sentinel-3-sl-2-wst-ntc": (WST_TYPE, "NTC", "SLSTR · L2 海表温度 WST（GHRSST）", "SLSTR"),
    "sentinel-3-sl-2-frp-ntc": ("SL_2_FRP___", "NTC", "SLSTR · L2 火点辐射功率 FRP", "SLSTR"),
    "sentinel-3-olci-1-efr-ntc": ("OL_1_EFR___", "NTC", "OLCI · L1 全分辨率辐亮度 EFR", "OLCI"),
    "sentinel-3-olci-2-lfr-ntc": ("OL_2_LFR___", "NTC", "OLCI · L2 陆地全分辨率 LFR", "OLCI"),
    "sentinel-3-olci-2-wfr-nrt": ("OL_2_WFR___", "NRT", "OLCI · L2 水色全分辨率 WFR", "OLCI"),
    "sentinel-3-syn-2-aod-ntc": ("SY_2_AOD___", "NTC", "SYNERGY · L2 气溶胶光学厚度 AOD", "SYNERGY"),
    "sentinel-3-sr-1-sra-a-ntc": (SRA_TYPE, "NTC", "SRAL · L1A 雷达高度计测量 SRA-A", "SRAL"),
}
DEFAULT_COLLECTION = "sentinel-3-sl-2-lst-ntc"


def public_collections():
    return [{"id": key, "title": f"{label} · {stream}", "group": sensor}
            for key, (_, stream, label, sensor) in COLLECTIONS.items()]


def sen3_collection(value):
    if not isinstance(value, str) or value not in COLLECTIONS:
        raise UserError("请选择受支持的 Sentinel-3 产品类型与处理流。")
    return value


def ghrsst_name(name, parsed):
    match = GHRSST_NAME.fullmatch(name)
    if (match and parsed and match["start"] == parsed["start"].replace("T", "")
            and match["created"] == parsed["created"].replace("T", "") and match["centre"] == parsed["centre"]
            and match["sat"] == parsed["sat"][-1]):
        return match
    return None


def valid_product_member(product, name, cdse_md5, xfdu_md5):
    """WST has no embedded SEN3 ID: do not plan it without two matching full-file source digests."""
    if not product:
        return False
    if name == SEN3_MANIFEST:
        return True
    if product["type"] == WST_TYPE:
        return bool(ghrsst_name(name, product) and cdse_md5 and cdse_md5 == xfdu_md5)
    if product["type"] == SRA_TYPE:
        return name == SRA_MEMBER
    return True


def _near(left, right):
    return abs((utc_time(left) - utc_time(right)).total_seconds()) <= 1


def sen3_row(feature, geometry, chosen, start, stop, *, radar_mode="", aoi_area=None):
    row = raw_item(feature, geometry, chosen, start, stop, aoi_area=aoi_area)
    if row is None:
        return None
    key = row["key"]
    product = key if key.endswith(".SEN3") else key + ".SEN3"
    parts = sen3_archive_parts(product)
    kind, stream, _, sensor = COLLECTIONS[chosen]
    if not parts or parts[0] != stream or parts[2] != kind:
        return None
    name = SEN3_NAME.fullmatch(product)
    props = feature["properties"]
    orbit = props.get("sat:absolute_orbit")
    if (props.get("platform") != "sentinel-3" + parts[1][-1].lower() or props.get("product:type") != kind
            or props.get("product:timeliness_category") not in (None, name["stream"])
            or props.get("sat:relative_orbit") not in (None, int(name["relative"]))
            or type(orbit) is not int or not 1 <= orbit <= 10**6 or not props.get("start_datetime") or not props.get("end_datetime")):
        return None
    first, last = row["start_utc"], row["end_utc"]
    named = [dt.datetime.strptime(name[k], "%Y%m%dT%H%M%S").replace(tzinfo=dt.timezone.utc) for k in ("start", "stop")]
    if (utc_time(first) >= utc_time(last) or abs((utc_time(first) - named[0]).total_seconds()) > 1
            or abs((utc_time(last) - named[1]).total_seconds()) > 1
            or (start is not None and utc_time(last) <= dt.datetime.combine(start, dt.time.min, dt.timezone.utc))):
        return None
    proof = {"kind": "sen3-member", "product": product, "orbit": orbit, "start_utc": first, "end_utc": last}
    official, conflicts = {}, set()
    assets = feature.get("assets")
    if isinstance(assets, dict):
        for asset in assets.values():
            if not isinstance(asset, dict):
                continue
            alternate = asset.get("alternate")
            alt_s3 = alternate.get("s3") if isinstance(alternate, dict) else None
            src = cdse_object(asset.get("href")) or (cdse_object(alt_s3.get("href")) if isinstance(alt_s3, dict) else None)
            if not src or len(src[0]) != 8 or src[0][:3] != ("Sentinel-3", sensor, kind) or src[0][-2] != product:
                continue
            filename = src[0][-1]
            if not sen3_member_name(filename):
                continue
            if asset.get("file:local_path", product + "/" + filename) != product + "/" + filename:
                conflicts.add(filename)
                continue
            detail = asset_details(filename, asset, filename)
            size = asset.get("file:size")
            if type(size) is float and size.is_integer() and 0 < size < 10**13:
                detail["bytes"] = int(size)
            published = {f: detail[f] for f in ("bytes", *DIGESTS) if detail[f] is not None}
            if asset.get("file:checksum") is not None and not any(k in published for k in DIGESTS):
                conflicts.add(filename)
            if asset.get("file:size") is not None and "bytes" not in published:
                conflicts.add(filename)
            previous = official.setdefault(filename, {})
            if any(k in previous and previous[k] != v for k, v in published.items()):
                conflicts.add(filename)
            previous.update(published)
    return {**row, "title": product, "platform": f"Sentinel-3{parts[1][-1]}", "tiles": [f"{stream} · 轨道 {orbit}"],
            "raw": {"uuid": None, "names": [key, product], "collection": chosen, "available": False, "assets": [],
                    "public_provider": PROVIDER, "sen3": {"complete": False},
                    "_lookup": {"proof": proof, "official": official, "conflicts": sorted(conflicts)}}}


def _xml(data):
    if not data or len(data) > MAX_XML:
        raise ValueError
    text = data.decode("utf-8-sig")
    if "\x00" in text or re.search(r"<!\s*(?:DOCTYPE|ENTITY)\b", text, re.IGNORECASE):
        raise ValueError
    return ET.fromstring(text)


def _one(node, path):
    selected = node.findall(path)
    if len(selected) != 1:
        raise ValueError
    return selected[0]


def _text(node, path):
    value = (_one(node, path).text or "").strip()
    if not value:
        raise ValueError
    return value


def parse_xfdu(data, proof):
    """Reject ambiguous paths, identity, links and file records as a unit."""
    try:
        root = _xml(data)
        if root.tag != XFDU_ROOT:
            raise ValueError
        meta = _one(root, "metadataSection")

        def block(ident, tag):
            obj = _one(meta, f"metadataObject[@ID='{ident}']")
            return _one(_one(_one(obj, "metadataWrap"), "xmlData"), tag)

        product = SEN3_NAME.fullmatch(proof["product"])
        if not product:
            raise ValueError
        information = block("generalProductInformation", SEN3 + "generalProductInformation")
        period = block("acquisitionPeriod", SAFE + "acquisitionPeriod")
        platform = block("platform", SAFE + "platform")
        orbit = block("measurementOrbitReference", SAFE + "orbitReference")
        first, last = _text(period, SAFE + "startTime"), _text(period, SAFE + "stopTime")
        if (_text(information, SEN3 + "productName") != proof["product"]
                or _text(information, SEN3 + "productType") != product["type"]
                or _text(information, SEN3 + "timeliness") != product["stream"]
                or _text(information, SEN3 + "baselineCollection") != product["baseline"]
                or _text(platform, SAFE + "familyName") != "Sentinel-3"
                or _text(platform, SAFE + "number") != product["sat"][-1]
                or int(_text(orbit, SAFE + "orbitNumber[@type='start']")) != proof["orbit"]
                or int(_text(orbit, SAFE + "relativeOrbitNumber[@type='start']")) != int(product["relative"])
                or utc_time(first) >= utc_time(last) or not _near(first, proof["start_utc"]) or not _near(last, proof["end_utc"])):
            raise ValueError
        section = _one(root, "dataObjectSection")
        if not 1 <= len(section) <= MAX_MEMBERS:
            raise ValueError
        members, identifiers, names = {}, set(), set()
        for obj in section:
            ident = obj.get("ID")
            if obj.tag != "dataObject" or not ident or ident in identifiers or len(obj) != 1:
                raise ValueError
            stream = _one(obj, "byteStream")
            loc, checksum = _one(stream, "fileLocation"), _one(stream, "checksum")
            name = loc.get("href", "").removeprefix("./")
            size, digest = stream.get("size", ""), (checksum.text or "").strip()
            if (not sen3_member_name(name) or name == SEN3_MANIFEST or name.casefold() in names
                    or not LENGTH.fullmatch(size) or checksum.get("checksumName") != "MD5" or not MD5_TEXT.fullmatch(digest)
                    or loc.get("locatorType") != "URL" or stream.get("mimeType") not in ("application/x-netcdf", "application/netcdf")):
                raise ValueError
            names.add(name.casefold())
            identifiers.add(ident)
            members[name] = (int(size), digest.lower())
        if {n.get("dataObjectID") for n in root.iter("dataObjectPointer")} != identifiers:
            raise ValueError
        return members
    except (TypeError, AttributeError, KeyError, ValueError, ET.ParseError, OverflowError):
        raise UserError("SEN3 清单的产品标识、轨道、时间或成员记录未通过验证。") from None


class _ArchiveGap(UserError):
    """Specific to this exact product, not evidence that the whole archive is unavailable."""


def _bounded(response, cancel, limit):
    if response.headers.get("Content-Encoding", "identity").lower() != "identity":
        raise _ArchiveGap("公开档案清单返回了非原始编码。")
    buffer = bytearray()
    for chunk in response.iter_content(65536):
        check_cancel(cancel)
        buffer.extend(chunk)
        if len(buffer) > limit:
            raise _ArchiveGap("公开档案清单超过安全大小上限。")
    return bytes(buffer)


def _request_xml(http, url, cancel, *, limit=MAX_XML, **kwargs):
    headers = {"Accept-Encoding": "identity", **kwargs.pop("headers", {})}
    response = http.request("GET", url, stream=True, headers=headers, cancel=cancel, timeout=(12, 26), **kwargs)
    try:
        if response.status_code == 404:
            raise _ArchiveGap("公开档案没有此精确 SEN3 版本或清单。")
        if response.status_code == 412:
            raise _ArchiveGap("公开档案对象在检索时已变化，请重试。")
        if response.status_code != 200:
            raise _ArchiveGap(f"公开档案未能确认此版本（HTTP {response.status_code}）。")
        try:
            return _bounded(response, cancel, limit), response.headers.get("ETag")
        except (requests.ConnectionError, requests.Timeout, requests.exceptions.ChunkedEncodingError, requests.exceptions.ContentDecodingError):
            raise _ArchiveGap("公开档案清单传输中断，请重新检索。") from None
    finally:
        response.close()


def _objects(http, prefix, cancel):
    data, _ = _request_xml(http, ARCHIVE, cancel, params={"list-type": "2", "prefix": prefix, "max-keys": 1000})
    try:
        page = _xml(data)
        if (page.tag != S3_NS + "ListBucketResult" or _text(page, S3_NS + "Name") != SEN3_BUCKET
                or _text(page, S3_NS + "Prefix") != prefix or _text(page, S3_NS + "IsTruncated") != "false"):
            raise ValueError
        result = {}
        for obj in page.findall(S3_NS + "Contents"):
            path = _text(obj, S3_NS + "Key")
            if not path.startswith(prefix):
                raise ValueError
            name = path[len(prefix):]
            if not sen3_member_name(name):  # Quicklooks and non-XFDU sidecars are not original members.
                continue
            tag, size = _text(obj, S3_NS + "ETag"), _text(obj, S3_NS + "Size")
            if name in result or not LENGTH.fullmatch(size) or not SEN3_ETAG.fullmatch(tag):
                raise ValueError
            result[name] = (int(size), tag)
        return result
    except (ValueError, ET.ParseError):
        raise _ArchiveGap("公开档案对象列表格式不符，不能确认该版本。") from None


def _resolve(row, http, cancel):
    src = row["raw"]
    lookup = src.pop("_lookup")
    proof = lookup["proof"]
    prefix = "/".join(sen3_archive_parts(proof["product"])) + "/"
    official, conflicts = lookup["official"], set(lookup["conflicts"])
    product = SEN3_NAME.fullmatch(proof["product"])
    try:
        objects = _objects(http, prefix, cancel)
        if SEN3_MANIFEST not in objects:
            raise _ArchiveGap("公开档案没有此精确 SEN3 版本的清单；没有用其他版本替代。")
        length, tag = objects[SEN3_MANIFEST]
        doc, received = _request_xml(http, ARCHIVE + prefix + SEN3_MANIFEST, cancel, headers={"If-Match": tag}, limit=length if length < MAX_XML else MAX_XML)
        if len(doc) != length or tag != received:
            raise _ArchiveGap("SEN3 清单的对象版本或字节数已变化。")
        try:
            members = parse_xfdu(doc, proof)
        except UserError as err:
            raise _ArchiveGap(str(err)) from None
        manifest_sha = hashlib.sha256(doc).hexdigest()
        hashes = {"provider_md5": lambda: hashlib.md5(doc, usedforsecurity=False).hexdigest(),
                  "provider_sha256": lambda: manifest_sha, "provider_sha3_256": lambda: hashlib.sha3_256(doc).hexdigest()}
        from_cdse = official.get(SEN3_MANIFEST, {})
        if (SEN3_MANIFEST in conflicts or from_cdse.get("bytes", length) != length
                or any(k in from_cdse and from_cdse[k] != make() for k, make in hashes.items())):
            raise _ArchiveGap("公开档案清单与 CDSE 发布的长度或校验值不符。")
    except _ArchiveGap as error:
        src.update(unavailable_reason=str(error))
        src["sen3"]["complete_reason"] = str(error)
        return row

    files, missing, mismatched = [], 0, 0

    def asset(name, size, etag, expected):
        cdse = official.get(name, {})
        return {**cdse, "id": name, "name": name, "bytes": size,
                "roles": ["metadata" if name == SEN3_MANIFEST else "data"],
                "type": "application/xml" if name == SEN3_MANIFEST else "application/netcdf",
                "public_url": ARCHIVE + prefix + name,
                "identity": {**proof, "archive_etag": etag, "manifest_sha256": manifest_sha, "xfdu_md5": expected}}

    files.append(asset(SEN3_MANIFEST, length, tag, None))
    for name, (size, digest) in sorted(members.items(), key=lambda pair: pair[0].casefold()):
        check_cancel(cancel)
        cdse = official.get(name, {})
        if name not in objects:
            missing += 1
        elif (name in conflicts or objects[name][0] != size or cdse.get("bytes", size) != size
              or cdse.get("provider_md5", digest) != digest
              or not valid_product_member(product, name, cdse.get("provider_md5"), digest)):
            mismatched += 1
        else:
            files.append(asset(name, size, objects[name][1], digest))
    undeclared = len(official.keys() - members.keys() - {SEN3_MANIFEST})
    unsupported_layout = product["type"] in (WST_TYPE, SRA_TYPE) and len(members) != 1
    complete = not (missing or mismatched or undeclared or unsupported_layout)
    state = "完整产品可用" if complete else f"仅部分可用 · 成员 {len(files) - 1}/{len(members)}"
    reason = None if complete else ("该类别的 SEN3 清单结构不同于预期，不能下载完整产品。" if unsupported_layout else
                                     f"不能下载完整 SEN3：缺少 {missing} 个成员，{mismatched} 个成员校验冲突或不可核实，{undeclared} 个 CDSE 文件未被清单声明。")
    src.update(available=True, assets=files, preferred_metadata=SEN3_MANIFEST,
               sen3={"complete": complete, "complete_reason": reason, "declared_members": len(members), "ready_members": len(files) - 1})
    row["tiles"].append(state)
    return row


def search_sen3(geometry, collection, start, end, *, cancel=None, progress=None):
    def catalogue_progress(message, ratio):
        if progress:
            progress(message, ratio * .25)

    rows = search_original(geometry, sen3_collection(collection), start, end, cancel=cancel,
                           progress=catalogue_progress, row_parser=sen3_row, limit=MAX_SCENES)
    if not rows:
        return []
    http = HttpClient()
    try:
        for index, row in enumerate(rows):
            check_cancel(cancel)
            if progress:
                progress(f"核对公开档案中的精确 SEN3 版本 · {index+1}/{len(rows)}", .25 + .74 * index / len(rows))
            _resolve(row, http, cancel)
    finally:
        http.close()
    return rows
