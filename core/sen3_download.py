"""Transactional publication for exact Sentinel-3 originals (whole SEN3 or selected members)."""
from __future__ import annotations

import datetime as dt
import json
import os
import shutil
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

from shapely.geometry import mapping

from . import VERSION
from .download import _save_json
from .errors import Cancelled, UserError
from .http import HttpClient, check_cancel
from .integrity import publish_file
from .open_sen3 import MAX_XML, parse_xfdu
from .raw_download import _sen3_target, _stream, _validate, _validated_digests
from .s3_source import SEN3_MANIFEST
from .sen3_netcdf import verify_netcdf

MAX_MEMBER_WORKERS = 4
PARALLEL_MIN_MEMBERS = 4


class _BatchStop:
    """A failure can stop siblings without turning the original job error into a user cancellation."""

    def __init__(self, parent):
        self.parent = parent
        self.event = threading.Event()

    def is_set(self):
        return self.event.is_set() or (self.parent is not None and self.parent.is_set())

    def wait(self, seconds):
        if self.parent is None:
            return self.event.wait(seconds)
        deadline = time.monotonic() + seconds
        while not self.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            self.event.wait(min(.1, remaining))
        return True


def _groups(plan):
    groups = {}
    for item in plan["targets"]:
        proof = _sen3_target(item)
        groups.setdefault(proof["product"], []).append(item)
    for product, items in groups.items():
        names = {item["name"].casefold() for item in items}
        proof = items[0]["identity"]
        fieldnames = ("manifest_sha256", "orbit", "start_utc", "end_utc")
        if (len(names) != len(items) or (plan["sen3_complete"] and SEN3_MANIFEST not in names)
                or any(any(item["identity"][key] != proof[key] for key in fieldnames) for item in items)):
            raise UserError("SEN3 下载清单缺失或包含不一致的文件版本，请重新检索。")
        yield product, sorted(items, key=lambda item: (item["name"] != SEN3_MANIFEST, item["name"].casefold()))


def _manifest_members(path, proof, digest, items, complete):
    if digest != proof["manifest_sha256"] or path.stat().st_size > MAX_XML:
        raise UserError("SEN3 清单与检索时的原始字节不一致，未发布产品。")
    members = parse_xfdu(path.read_bytes(), proof)
    if complete:
        planned = {i["name"]: (i["bytes"], i["identity"]["xfdu_md5"]) for i in items if i["name"] != SEN3_MANIFEST}
        if members != planned:
            raise UserError("待下载文件与 SEN3 清单的完整成员集不一致，未发布完整产品。")
    return len(members)


def _receive(path, item, items, complete, http, cancel, report):
    transfer = _stream(http, None, item, path, cancel, report)
    report("核对 SEN3 原始字节及主要数组", transfer["size"], transfer["size"])
    digest, checks = _validated_digests(path, item, transfer["aws_single_part_md5"], cancel)
    name, proof = item["name"], item["identity"]
    if name == SEN3_MANIFEST:
        count = _manifest_members(path, proof, digest, items, complete)
        validation = {"declared_members": count, "matches_retrieved_manifest_sha256": True}
        label = "XFDU identity and unchanged original manifest bytes"
    else:
        _validate(path, item, cancel)
        validation = verify_netcdf(path, name, proof, cancel)
        label = "whole-file XFDU MD5, NetCDF identity and full decode of listed arrays"
    return {"file": name, "url": item["url"], "transport": item["transport"], "identity": proof, "sha256": digest,
            "format_validation": label, "sen3_validation": validation, "provider_checksum_verified": checks, **transfer}


WINDOWS_PREFIX = "\\\\?\\"
WINDOWS_UNC_PREFIX = WINDOWS_PREFIX + "UNC\\"


def _on_disk(path):
    """Use explicit Win32 extended paths without changing the machine's registry."""
    if os.name != "nt":
        return path
    full = os.path.abspath(path)
    if full.startswith(WINDOWS_PREFIX):
        return Path(full)
    return Path(WINDOWS_UNC_PREFIX + full[2:] if full.startswith("\\\\") else WINDOWS_PREFIX + full)


def _report_path(path):
    value = str(path)
    if os.name != "nt":
        return value
    plain = ("\\\\" + value[len(WINDOWS_UNC_PREFIX):] if value.startswith(WINDOWS_UNC_PREFIX)
             else value.removeprefix(WINDOWS_PREFIX))
    return plain if len(plain.encode("utf-16-le")) // 2 < 260 else value


def _exists(path):
    return os.path.lexists(path)


def _read_existing(target, receipt, items, complete, cancel, report):
    try:
        if (not target.is_dir() or target.resolve() != target or not receipt.is_file()
                or receipt.resolve() != receipt or receipt.stat().st_size > MAX_XML):
            raise ValueError
        doc = json.loads(receipt.read_text(encoding="utf-8"))
        rows = doc["members"]
        if (doc["product"] != items[0]["identity"]["product"] or doc["scope"] != ("complete" if complete else "selected")
                or {x.name for x in target.iterdir()} != {i["name"] for i in items} or len(rows) != len(items)):
            raise ValueError
        for item, row in zip(items, rows, strict=True):
            check_cancel(cancel)
            path = target / item["name"]
            report(item, "复核已下载的 SEN3 原始字节", None, None)
            if (not path.is_file() or path.resolve() != path or row["file"] != item["name"]
                    or row["url"] != item["url"] or row["identity"] != item["identity"]
                    or row["size"] != item["bytes"] or path.stat().st_size != item["bytes"]
                    or not isinstance(row["sen3_validation"], dict)):
                raise ValueError
            digest, _ = _validated_digests(path, item, row.get("aws_single_part_md5"), cancel)
            if row["sha256"] != digest:
                raise ValueError
            if item["name"] == SEN3_MANIFEST:
                _manifest_members(path, item["identity"], digest, items, complete)
            report(item, "复核通过", row["size"], row["size"], verified=True)
        return rows
    except (KeyError, TypeError, ValueError, OSError, UserError):
        raise UserError("已有 SEN3 目录或校验记录不完整、版本不同或字节已变化；请将目录和对应 .starmap.json 移到其他位置再重试。") from None


def _receive_group(stage, items, complete, http, cancel, report):
    def receive(item, client, event):
        check_cancel(event)
        report(item, "下载精确版本", None, None)
        row = _receive(stage / item["name"], item, items, complete, client, event,
                       lambda message, received, size: report(item, message, received, size))
        report(item, "成员校验通过", row["size"], row["size"], verified=True)
        return row

    rows = []
    start = 0
    if items[0]["name"] == SEN3_MANIFEST:
        # An exact, validated declaration must precede any complete-product data transfer.
        rows.append(receive(items[0], http, cancel))
        start = 1
    remaining = items[start:]
    if len(remaining) < PARALLEL_MIN_MEMBERS:
        rows.extend(receive(item, http, cancel) for item in remaining)
        return rows

    stopping = _BatchStop(cancel)
    local = threading.local()
    clients = []
    client_lock = threading.Lock()
    ready = {}

    def worker(item):
        check_cancel(stopping)
        if not hasattr(local, "http"):
            local.http = HttpClient()
            with client_lock:
                clients.append(local.http)
        return receive(item, local.http, stopping)

    try:
        with ThreadPoolExecutor(max_workers=MAX_MEMBER_WORKERS, thread_name_prefix="sen3") as pool:
            pending = [pool.submit(worker, item) for item in remaining]
            try:
                for task in as_completed(pending):
                    row = task.result()
                    ready[row["file"]] = row
            except BaseException:
                stopping.event.set()
                for task in pending:
                    task.cancel()
                raise
        # Executor has joined every writer and all data checks have finished before the directory can be published.
        rows.extend(ready[item["name"]] for item in remaining)
        return rows
    finally:
        for client in clients:
            client.close()


def _stage_group(target, receipt, items, complete, http, cancel, report):
    if shutil.disk_usage(target.parent).free < sum(item["bytes"] for item in items):
        raise UserError("输出盘空间不足，未开始下载此 SEN3 产品。")
    with TemporaryDirectory(prefix=".sen3-", dir=target.parent) as temp:
        stage = Path(temp)
        if stage.resolve().parent != target.parent or target.parent != receipt.parent:
            raise UserError("SEN3 暂存目录不在选中的输出目录中。")
        rows = _receive_group(stage, items, complete, http, cancel, report)
        check_cancel(cancel)
        receipt_temp = receipt.parent / f".sen3-{uuid4().hex}.json"
        created = False
        try:
            _save_json(receipt_temp, {"product": items[0]["identity"]["product"], "scope": "complete" if complete else "selected", "members": rows})
            if _exists(target) or _exists(receipt):
                raise UserError("输出位置出现同名 SEN3 目录或校验记录，未覆盖。")
            publish_file(receipt_temp, receipt)
            created = True
            check_cancel(cancel)
            # On Windows rename is atomic here and refuses to replace an existing destination.
            stage.rename(target)
            created = False
        finally:
            receipt_temp.unlink(missing_ok=True)
            if created:
                receipt.unlink(missing_ok=True)
    return rows


def download_sen3(geometry, plan, output_dir, *, cancel=None, progress=None):
    base = Path(output_dir).expanduser()
    if not base.is_absolute():
        raise UserError("请选择本地绝对输出目录。")
    base = _on_disk(base)
    base.mkdir(parents=True, exist_ok=True)
    base = base.resolve()
    folder = base / plan["folder_name"]
    folder.mkdir(exist_ok=True)
    if folder.resolve() != folder or folder.parent != base:
        raise UserError("SEN3 输出目录不在所选择的位置内，请换一个目录。")
    complete = plan["sen3_complete"]
    record_path = folder / f"下载记录_{uuid4().hex[:10]}.json"
    _save_json(folder / "研究区.geojson", {"type": "Feature", "properties": {}, "geometry": mapping(geometry)})
    record = {"software": VERSION, "source": plan["source"], "collection": plan["options"]["collection"],
              "created_at": dt.datetime.now(dt.timezone.utc).isoformat(), "status": "running", "items": [], "products": []}
    _save_json(record_path, record)
    http, result = HttpClient(), []
    total, finished = len(plan["targets"]), 0
    total_bytes, finished_bytes = sum(item["bytes"] for item in plan["targets"]), 0
    try:
        for product, items in _groups(plan):
            check_cancel(cancel)
            name = product if complete else product.removesuffix(".SEN3") + "_selected_members"
            target, receipt = folder / name, folder / (name + ".starmap.json")
            scope = "complete" if complete else "selected"
            group = {"product": product, "scope": scope, "file_count": len(items), "status": "running"}
            record["products"].append(group)
            progress_lock = threading.Lock()
            file_progress, checked = {}, set()

            def report(item, message, size, expected, *, verified=False):
                if progress:
                    with progress_lock:
                        file = item["name"]
                        if verified:
                            checked.add(file)
                            file_progress[file] = item["bytes"]
                        elif size is not None and expected:
                            earned = min(.95, .95 * size / expected) * item["bytes"]
                            file_progress[file] = max(file_progress.get(file, 0), earned)
                        earned_bytes = finished_bytes + sum(file_progress.values())
                        verified_count = finished + len(checked)
                        progress(f"已核验 {verified_count}/{total} · {file} · {message}", min(.995, earned_bytes/total_bytes),
                                 {"folder": _report_path(folder), "received_bytes": size, "total_bytes": expected,
                                  "completed_scenes": verified_count, "total_scenes": total})

            try:
                reused = _exists(target) or _exists(receipt)
                rows = (_read_existing(target, receipt, items, complete, cancel, report) if reused else
                        _stage_group(target, receipt, items, complete, http, cancel, report))
                status = "skipped" if reused else "completed"
                group.update(status=status, path=_report_path(target))
                for row in rows:
                    path = _report_path(target / row["file"])
                    record["items"].append({**row, "path": path, "product": product, "status": status, "scope": scope})
                    result.append({"path": path, "scene": items[0]["key"], "status": status})
                finished += len(items)
                finished_bytes += sum(item["bytes"] for item in items)
                _save_json(record_path, record)
            except Cancelled:
                group["status"] = "cancelled"
                raise
            except Exception:
                group["status"] = "failed"
                raise
        record["status"] = "completed"
        return {"files": result, "folder": _report_path(folder), "manifest": _report_path(record_path), "plan": plan, "empty_scenes": []}
    except Cancelled:
        record["status"] = "cancelled"
        raise
    except Exception:
        record["status"] = "failed"
        raise
    finally:
        try:
            _save_json(record_path, record)
        finally:
            http.close()
