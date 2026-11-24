"""Bounded, version-pinned range transfers and on-disk validation of the exact IW SLC SAFE ZIP."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import shutil
import stat
import struct
import threading
import time
import zipfile
import zlib
from concurrent.futures import ThreadPoolExecutor, as_completed
from xml.etree import ElementTree

import requests

from .errors import Cancelled, UserError, TransientDownloadError
from .download_cache import fingerprint, save_json
from .http import HttpClient, check_cancel, tls_error_kind, tls_error_message, wait_retry
from .open_slc import check_slc_target, zip_type
from .scene_identity import utc_text

RANGE_BYTES = 32 * 1024**2
BLOCK = 512 * 1024
LANES = 4
MAX_MEMBERS = 256
MAX_EXPANDED = 64 * 1024**3
MAX_MANIFEST = 1024**2
SAFE_SEGMENT = re.compile(r"[A-Za-z0-9_.-]+", re.ASCII)
MEASUREMENT = re.compile(r"(s1[ab])-(iw[123])-slc-(vv|vh|hh|hv)-(\d{8}t\d{6})-(\d{8}t\d{6})-(\d{6})-([0-9a-f]{6})-\d{3}\.tiff", re.ASCII)
BODY_ERRORS = (requests.ConnectionError, requests.Timeout, requests.exceptions.ChunkedEncodingError,
               requests.exceptions.ContentDecodingError)


def transfer_slc(item, partial, cancel, progress):
    ref = check_slc_target(item)
    size, tag = item["bytes"], item["identity"]["archive_etag"]
    windows = [(n, min(size, n + RANGE_BYTES) - 1) for n in range(0, size, RANGE_BYTES)]
    lanes = min(LANES, len(windows))
    receipt = partial.with_name(partial.name + ".ranges.json")
    identity = fingerprint(item)
    recorded, prefixes = {}, {}
    try:
        if receipt.stat().st_size > 4 * 1024**2:
            raise ValueError
        saved = json.loads(receipt.read_text(encoding="utf-8"))
        if saved.get("identity") != identity or saved.get("size") != size or saved.get("etag") != tag or partial.stat().st_size != size:
            raise ValueError
        with partial.open("rb") as stream:
            for begin, end in windows:
                row = saved.get("windows", {}).get(str(begin))
                if not isinstance(row, dict):
                    continue
                count = row.get("received")
                if type(count) is not int or not 0 < count <= end - begin + 1 or count == end - begin + 1 and not row.get("complete"):
                    continue
                sha, remaining = hashlib.sha256(), count
                stream.seek(begin)
                while remaining:
                    check_cancel(cancel)
                    block = stream.read(min(BLOCK, remaining))
                    if not block:
                        raise ValueError
                    sha.update(block)
                    remaining -= len(block)
                if sha.hexdigest() == row.get("sha256"):
                    recorded[str(begin)], prefixes[begin] = row, sha
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    if not partial.exists() or partial.stat().st_size != size:
        if shutil.disk_usage(partial.parent).free < size:
            raise UserError("磁盘剩余空间不足，无法接收完整 SLC ZIP。")
        with partial.open("wb") as out:
            out.truncate(size)
        recorded, prefixes = {}, {}
    lock, stop = threading.Lock(), threading.Event()
    received = sum(row["received"] for row in recorded.values())
    resumed, last_update = received, 0.0
    reused_windows = sum(bool(row.get("complete")) for row in recorded.values())

    def checkpoint(begin, position, sha, complete=False):
        if position <= begin:
            return
        with lock:
            recorded[str(begin)] = {"received": position - begin, "sha256": sha.hexdigest(), "complete": complete}
            save_json(receipt, {"schema": 1, "identity": identity, "size": size, "etag": tag, "windows": recorded})

    class TransferCancel:
        def is_set(self):
            return stop.is_set() or (cancel is not None and cancel.is_set())

        def wait(self, delay):
            deadline = time.monotonic() + delay
            while not self.is_set():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                stop.wait(min(remaining, .25))
            return True

    signal = TransferCancel()

    def receive(positions):
        nonlocal received, last_update
        http = HttpClient()
        requests_sent = 0
        try:
            with partial.open("r+b", buffering=0) as out:
                for begin, end in positions:
                    row = recorded.get(str(begin), {})
                    if row.get("complete"):
                        continue
                    position = begin + row.get("received", 0)
                    sha = prefixes.get(begin, hashlib.sha256())
                    saved_position, saved_time = position, time.monotonic()
                    for attempt in range(4):
                        check_cancel(signal)
                        if position == end + 1:
                            break
                        request_headers = {"If-Match": tag, "Accept-Encoding": "identity", "Range": f"bytes={position}-{end}"}
                        requests_sent += 1
                        response = None
                        try:
                            response = http.request("GET", ref.url, headers=request_headers, stream=True, cancel=signal, timeout=(15, 55), attempts=1)
                            if response.status_code == 412:
                                raise UserError("SLC 存档对象在检索后已更换，请重新检索；未保存或拼接其他版本。")
                            if (response.status_code != 206 or not zip_type(response.headers)
                                    or response.headers.get("ETag") != tag
                                    or response.headers.get("Content-Range") != f"bytes {position}-{end}/{size}"
                                    or response.headers.get("Content-Length") != str(end - position + 1)):
                                raise UserError(f"SLC 存档返回的字节范围、格式或版本与请求不一致（HTTP {response.status_code}）；未发布。")
                            out.seek(position)
                            for block in response.iter_content(BLOCK):
                                check_cancel(signal)
                                if not block:
                                    continue
                                if position + len(block) > end + 1:
                                    raise UserError("SLC 存档返回超出预期的字节，未发布。")
                                if out.write(block) != len(block):
                                    raise OSError("short SLC ZIP write")
                                position += len(block)
                                sha.update(block)
                                with lock:
                                    received += len(block)
                                    now = time.monotonic()
                                    if now - last_update >= .35:
                                        progress("分段接收 SLC 原始 ZIP", received, size)
                                        last_update = now
                                if position - saved_position >= 16 * 1024**2 or time.monotonic() - saved_time >= 2:
                                    checkpoint(begin, position, sha)
                                    saved_position, saved_time = position, time.monotonic()
                            if position == end + 1:
                                checkpoint(begin, position, sha, complete=True)
                                break
                        except (TransientDownloadError, *BODY_ERRORS) as error:
                            if isinstance(error, requests.exceptions.SSLError) and tls_error_kind(error) != "interrupted":
                                failure = UserError(tls_error_message(error, "SLC 下载"))
                                if recorded:
                                    failure.download_progress_preserved = True
                                raise failure from None
                            checkpoint(begin, position, sha)
                            # Preserve the prefix in this window; only retry the still-missing bytes, pinned to the same ETag.
                            if position == end + 1:
                                raise TransientDownloadError("SLC 区间传输未正常结束；已保留已校验区间，可继续下载。") from None
                            if attempt == 3:
                                raise TransientDownloadError("SLC 文件的一个区间多次中断；已保留校验通过的区间，可继续下载。") from None
                            if response is not None:
                                response.close()
                                response = None
                            wait_retry(attempt, signal, getattr(error, "retry_after", None))
                        except (Cancelled, OSError):
                            checkpoint(begin, position, sha)
                            raise
                        except UserError as error:
                            if recorded:
                                error.download_progress_preserved = True
                            raise
                        finally:
                            if response is not None:
                                response.close()
            return requests_sent
        finally:
            http.close()

    with ThreadPoolExecutor(max_workers=lanes, thread_name_prefix="slc-range") as pool:
        futures = [pool.submit(receive, windows[i::lanes]) for i in range(lanes)]
        try:
            total_requests = sum(f.result() for f in as_completed(futures))
        except BaseException:
            stop.set()  # Await other writers before the caller discards the temporary file (Windows).
            raise
    check_cancel(cancel)
    if received != size or partial.stat().st_size != size:
        raise UserError("SLC 区间未覆盖完整 ZIP，未发布。")
    return {"size": size, "etag": tag, "content_type": "application/zip", "expected_bytes": size,
            "transfer_attempts": total_requests, "range_windows": len(windows), "range_workers": lanes,
            "resumed_bytes": resumed, "reused_range_windows": reused_windows,
            "aws_single_part_md5": None, "azure_blob_md5": None}


def _path(value):
    if not value or any(part in (".", "..") or not SAFE_SEGMENT.fullmatch(part) or part.endswith(".") for part in value.split("/")):
        raise ValueError("unsafe SAFE member path")
    return value


def _children(parent, name):
    return [element for element in parent if element.tag.rsplit("}", 1)[-1] == name]


def _child(parent, name):
    only, = _children(parent, name)
    return only


def _value(parent, name):
    return _child(parent, name).text or ""


def _timestamp(text):
    value = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    return value.replace(tzinfo=dt.timezone.utc) if value.tzinfo is None else value.astimezone(dt.timezone.utc)


def _manifest(data, ref):
    text = data.decode("utf-8-sig")
    if "<!DOCTYPE" in text or "<!ENTITY" in text:
        raise ValueError("unexpected declaration")
    root = ElementTree.fromstring(text)
    if root.tag.rsplit("}", 1)[-1] != "XFDU":
        raise ValueError("not XFDU")
    wanted = {"acquisitionPeriod", "platform", "generalProductInformation", "measurementOrbitReference"}
    metadata = {}
    for obj in _children(_child(root, "metadataSection"), "metadataObject"):
        key = obj.get("ID")
        if key in wanted:
            if key in metadata:
                raise ValueError("duplicate XFDU metadata")
            [metadata[key]] = list(_child(_child(obj, "metadataWrap"), "xmlData"))
    acquisition = metadata["acquisitionPeriod"]
    start = _timestamp(_value(acquisition, "startTime"))
    end = _timestamp(_value(acquisition, "stopTime"))
    if (start >= end or start.replace(microsecond=0) != ref.start or end.replace(microsecond=0) != ref.end):
        raise ValueError("wrong acquisition period")
    platform = metadata["platform"]
    mode = _child(_child(_child(platform, "instrument"), "extension"), "instrumentMode")
    product = metadata["generalProductInformation"]
    orbits = [e.text for e in _children(metadata["measurementOrbitReference"], "orbitNumber") if e.get("type") == "start"]
    if (_value(platform, "familyName") != "SENTINEL-1" or _value(platform, "number") != ref.satellite
            or _value(mode, "mode") != "IW" or {e.text for e in _children(mode, "swath")} != {"IW1", "IW2", "IW3"}
            or _value(product, "productType") != "SLC" or _value(product, "productClass") != "S"
            or int(_value(product, "missionDataTakeID")) != int(ref.datatake, 16)
            or sorted(e.text for e in _children(product, "transmitterReceiverPolarisation")) != sorted(ref.polarisations)
            or len(orbits) != 1 or int(orbits[0]) != ref.orbit):
        raise ValueError("wrong SAFE product identity")
    members = {}
    for obj in _children(_child(root, "dataObjectSection"), "dataObject"):
        stream = _child(obj, "byteStream")
        location = _child(stream, "fileLocation").attrib["href"]
        if not location.startswith("./"):
            raise ValueError("non-relative XFDU member")
        name = _path(location[2:])
        checksum = _child(stream, "checksum")
        size, md5 = int(stream.attrib["size"]), (checksum.text or "").lower()
        if (name in members or size <= 0 or checksum.get("checksumName", "").upper() != "MD5"
                or not re.fullmatch(r"[0-9a-f]{32}", md5)):
            raise ValueError("invalid XFDU member digest")
        members[name] = (size, md5)
    return utc_text(start), utc_text(end), members


def _measurements(declared, files, ref):
    measurements = {}
    for name in files:
        if not name.startswith("measurement/"):
            continue
        m = MEASUREMENT.fullmatch(name.removeprefix("measurement/"))
        if not m or name not in declared:
            raise ValueError("unknown measurement")
        satellite, swath, pol, start, end, orbit, datatake = m.groups()
        times = [dt.datetime.strptime(t, "%Y%m%dT%H%M%S").replace(tzinfo=dt.timezone.utc) for t in (start.upper(), end.upper())]
        if ((swath, pol) in measurements or satellite != "s1" + ref.satellite.lower() or int(orbit) != ref.orbit
                or datatake != ref.datatake.lower() or not ref.start <= times[0] <= times[1] <= ref.end):
            raise ValueError("mismatched measurement identity")
        stem = name.removeprefix("measurement/").removesuffix(".tiff")
        if any(part not in declared for part in (f"annotation/{stem}.xml", f"annotation/calibration/calibration-{stem}.xml",
                                                f"annotation/calibration/noise-{stem}.xml")):
            raise ValueError("measurement metadata missing")
        measurements[swath, pol] = name
    if set(measurements) != {(f"iw{i}", pol.lower()) for i in range(1, 4) for pol in ref.polarisations}:
        raise ValueError("incomplete swath or polarisation inventory")
    return set(measurements.values())


def _tiff_layout(header, file_size):
    # Some SAFE producers put the first real IFD at the end of the TIFF. Do not seek backward or inflate twice.
    # Capture only the declared directory's bounded window while the CRC/MD5 stream passes that offset.
    order = {b"II": "<", b"MM": ">"}[header[:2]]
    kind, = struct.unpack_from(order + "H", header, 2)
    if kind == 42:
        offset, = struct.unpack_from(order + "I", header, 4)
        count_type, count_len, entry_len, entry_fmt = "H", 2, 12, "HHI"
    elif kind == 43:
        offsize, padding, offset = struct.unpack_from(order + "HHQ", header, 4)
        if offsize != 8 or padding:
            raise ValueError("unexpected BigTIFF header")
        count_type, count_len, entry_len, entry_fmt = "Q", 8, 20, "HHQ"
    else:
        raise ValueError("invalid TIFF version")
    if not (8 if kind == 42 else 16) <= offset <= file_size - count_len:
        raise ValueError("measurement TIFF directory outside file")
    return offset, kind, order, count_type, count_len, entry_len, entry_fmt


def _complex_tiff(table, layout):
    _, kind, order, count_type, count_len, entry_len, entry_fmt = layout
    count, = struct.unpack_from(order + count_type, table)
    wanted = {256, 257, 258, 277, 339}  # width, height, bits, samples, SampleFormat
    if not 5 <= count <= 1024 or count_len + count * entry_len > len(table):
        raise ValueError("invalid measurement TIFF directory")
    values = {}
    for i in range(count):
        start = count_len + entry_len * i
        code, dtype, elements = struct.unpack_from(order + entry_fmt, table, start)
        if code in wanted:
            if elements != 1 or code in values or dtype not in (3, 4, 16) or dtype == 16 and kind != 43:
                raise ValueError("invalid measurement TIFF field")
            values[code], = struct.unpack_from(order + {3: "H", 4: "I", 16: "Q"}[dtype], table, start + entry_len - (8 if kind == 43 else 4))
    width, height = values[256], values[257]
    if not (0 < width <= 100000 and 0 < height <= 100000 and (values[258], values.get(277, 1), values[339]) == (32, 1, 5)):
        raise ValueError("measurement is not CInt16 I/Q")
    return [width, height]


def verify_slc_archive(path, item, cancel=None):
    ref = check_slc_target(item)
    prefix = ref.product + ".SAFE/"
    try:
        with path.open("rb") as file:
            if file.read(4) != b"PK\x03\x04":
                raise ValueError("invalid ZIP signature")
        with zipfile.ZipFile(path) as archive:
            entries = archive.infolist()
            if not 1 <= len(entries) <= MAX_MEMBERS or sum(z.file_size for z in entries) > MAX_EXPANDED:
                raise ValueError("unexpected SAFE archive inventory")
            files, seen = {}, set()
            for member in entries:
                name = member.filename
                if (member.orig_filename != name or not name.startswith(prefix) or member.flag_bits & 1
                        or stat.S_IFMT(member.external_attr >> 16) not in (0, stat.S_IFREG, stat.S_IFDIR)
                        or member.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED)):
                    raise ValueError("unexpected ZIP member")
                directory = member.is_dir()
                if directory and member.file_size:
                    raise ValueError("nonempty ZIP directory")
                rel = name[len(prefix):].removesuffix("/") if directory else name[len(prefix):]
                if rel or not directory:
                    _path(rel)
                if rel.casefold() in seen:
                    raise ValueError("duplicate ZIP member")
                seen.add(rel.casefold())
                if not directory:
                    files[rel] = member
            manifest = files["manifest.safe"]
            if not 0 < manifest.file_size <= MAX_MANIFEST:
                raise ValueError("invalid manifest size")
            check_cancel(cancel)
            raw = archive.read(manifest)
            start, end, declared = _manifest(raw, ref)
            for name, (length, _) in declared.items():
                if name not in files or name == "manifest.safe" or length != files[name].file_size:
                    raise ValueError("manifest and ZIP lengths differ")
            expected = _measurements(declared, files, ref)
            measurements = []
            for name, member in files.items():
                if name == "manifest.safe":
                    continue
                check_cancel(cancel)
                digest = hashlib.md5(usedforsecurity=False) if name in declared else None
                with archive.open(member) as stream:
                    layout = None
                    position = 0
                    if name in expected:
                        header = stream.read(16)
                        layout = _tiff_layout(header, member.file_size)
                        offset, _, _, _, count_len, entry_len, _ = layout
                        limit = offset + count_len + 1024 * entry_len
                        table = bytearray(header[offset:])
                        digest.update(header)
                        position = len(header)
                    while block := stream.read(1024**2):
                        check_cancel(cancel)
                        if digest is not None:
                            digest.update(block)
                        if layout and position < limit and position + len(block) > offset:
                            table.extend(block[max(0, offset - position):limit - position])
                        position += len(block)
                    if layout:
                        measurements.append({"name": name, "shape": _complex_tiff(table, layout), "encoding": "CInt16",
                                             "md5": declared[name][1], "bytes": member.file_size, "crc32": f"{member.CRC:08x}"})
                if digest is not None and digest.hexdigest() != declared[name][1]:
                    raise ValueError("SAFE member MD5 mismatch")
            return {"product": ref.product, "acquisition_start": start, "acquisition_end": end, "beam_mode": "IW",
                    "absolute_orbit": ref.orbit, "data_take": ref.datatake, "polarizations": list(ref.polarisations),
                    "manifest_sha256": hashlib.sha256(raw).hexdigest(), "zip_files_crc_verified": len(files),
                    "declared_members_md5_verified": len(declared), "measurements": sorted(measurements, key=lambda x: x["name"])}
    except (ValueError, KeyError, TypeError, OverflowError, ElementTree.ParseError, zipfile.BadZipFile, zlib.error, EOFError,
            struct.error, RuntimeError, NotImplementedError):
        raise UserError("SLC SAFE 内部的产品身份、完整成员、复数测量格式或成员校验未通过；未发布。") from None
