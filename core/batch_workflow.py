"""One reviewable download batch with shared boundaries and UTC dates."""
from __future__ import annotations

import copy
import datetime as dt
import hashlib
import json
import threading
import uuid
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path

from rasterio.transform import from_origin
from shapely import normalize
from shapely.geometry import mapping

from . import VERSION
from .batch_sources import SOURCE_INFO
from .catalogue import date_range
from .errors import Cancelled, UserError
from .gedi_catalogue import GEDI
from .gedi_download import build_gedi_plan, normalize_options as gedi_options
from .geometry import describe_aoi, load_aoi, normalize_aoi, save_aoi
from .http import check_cancel
from .local_paths import io_path
from .spatial_selection import coverage_summary, priority_option

MAX_SCENES = 500
OPTION_KEYS = frozenset({"mode", "collection", "transport", "asset", "assets", "fields", "beams", "bands", "quality",
                         "nondegraded", "min_sensitivity", "resolution", "reference_path", "sample_reference",
                         "max_cloud", "radar_mode", "mask_clouds", "layout", "priority", "fill_gaps"})


def _fingerprint(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                                    allow_nan=False).encode("utf-8")).hexdigest()[:16]


def _available(scene):
    if "gedi" in scene:
        return scene["gedi"].get("available") is True
    if "raw" in scene:
        return scene["raw"].get("available") is True
    return True


def _static(profile, criteria):
    return profile.startswith("dem-") or (profile == "cdse-original" and
            str(criteria.get("collection", "")).startswith(("cop-dem-", "s2glc")))


def _safe_error(error):
    if isinstance(error, UserError):
        return str(error)
    if isinstance(error, OSError):
        return "文件读写失败，请检查磁盘空间、文件夹权限和文件占用。"
    return f"此来源遇到异常（{type(error).__name__}），请重试。"


def _write_json(path, value):
    temporary = path.with_name(path.name + ".tmp")
    try:
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


class BatchWorkflow:
    def __init__(self, service):
        self.service = service
        self.state = None
        self.geometry = None

    def clear(self):
        self.state = None
        self.geometry = None

    def snapshot(self):
        return copy.deepcopy(self.state)

    def coverage(self, value):
        from .service import _criteria

        geometry = normalize_aoi(value.get("geometry"))
        if not self.state or value.get("batch_id") != self.state["id"] or not geometry.equals(self.geometry):
            raise UserError("联合检索或研究区已变化，请重新检索。")
        group = next((g for g in self.state["groups"] if g["id"] == value.get("group_id")), None)
        if not isinstance(value.get("criteria"), dict):
            raise UserError("附带影像的检索条件无效。")
        if (not group or value.get("profile") != group["profile"] or value.get("query_id") != group["query"]["id"]
                or _criteria(value.get("criteria", {}), group["profile"], shared_dates=True) != group["criteria"]):
            raise UserError("附带影像的来源或日期已变化，请重新检索。")
        keys = value.get("scene_keys")
        known = {s["key"] for s in group["scenes"]}
        if (not isinstance(keys, list) or len(keys) > MAX_SCENES or any(not isinstance(k, str) or k not in known for k in keys)
                or len(keys) != len(set(keys))):
            raise UserError("附带影像的区块选择无效。")
        supplied = value.get("options", {})
        if not isinstance(supplied, dict):
            raise UserError("拼接选项无效。")
        return coverage_summary(geometry, group["scenes"], keys, priority_option(supplied))

    def search(self, value):
        # Imported lazily to keep the existing individual-source entry points reusable.
        from .service import DEM, S2_PROCESS, _criteria, _profile

        geometry = normalize_aoi(value.get("geometry"))
        shared = value.get("shared_dates")
        if not isinstance(shared, dict):
            raise UserError("附带下载需要一个共同的开始和结束日期。")
        start, stop = date_range(shared.get("start"), shared.get("end"), dt.date(1900, 1, 1))
        dates = {"start": start.isoformat(), "end": (stop - dt.timedelta(days=1)).isoformat()}
        companions = value.get("companions")
        if not isinstance(companions, list) or not 1 <= len(companions) < len(SOURCE_INFO):
            raise UserError("请勾选至少一个附带数据来源。")
        profile = _profile(value)
        primary_criteria = _criteria(value, profile)
        if not _static(profile, primary_criteria) and any(primary_criteria[k] != dates[k] for k in dates):
            raise UserError("主数据和附带数据必须使用同一个 UTC 日期范围。")
        if _static(profile, primary_criteria) and any(primary_criteria[k] is not None for k in dates):
            raise UserError("静态主数据仅共用研究区，请关闭它的采集日期筛选；共同日期用于附带观测数据。")
        groups = [{"id": "primary", "profile": profile, "criteria": primary_criteria, "options": value.get("options", {})}]
        seen = {(profile, primary_criteria.get("collection"))}
        ids = {"primary"}
        for item in companions:
            if not isinstance(item, dict):
                raise UserError("附带数据来源格式无效。")
            other = _profile(item)
            identifier = item.get("id", other)
            if identifier != other or identifier in ids:
                raise UserError("附带数据来源重复或标识无效。")
            filters = item.get("criteria", {})
            if not isinstance(filters, dict) or not _static(other, filters) and any(filters.get(k, v) != v for k, v in dates.items()):
                raise UserError("附带数据不能使用另一组日期，请重新勾选。")
            other_criteria = _criteria({**filters, **({"start": None, "end": None} if _static(other, filters) else dates)},
                                       other, shared_dates=True)
            identity = (other, other_criteria.get("collection"))
            if identity in seen:
                raise UserError("同一数据来源与产品已经选为主数据，请选择其他附带来源。")
            seen.add(identity)
            ids.add(identifier)
            groups.append({"id": identifier, "profile": other, "criteria": other_criteria, "options": item.get("options", {})})
        for group in groups:
            group.update(title=SOURCE_INFO[group["profile"]]["title"], scenes=[], status="pending", error=None)
            group["time_note"] = ("静态产品；仅共用研究区，不表示所选日期的观测。" if _static(group["profile"], group["criteria"])
                                  else SOURCE_INFO[group["profile"]]["time_note"])
            if not isinstance(group["options"], dict):
                raise UserError("附带导出选项无效。")
            group["options"] = {k: v for k, v in group["options"].items() if k in OPTION_KEYS}

        def operation(job):
            job.update("检索主数据及附带数据", 0, {"search_profile": profile, "criteria": primary_criteria,
                                                 "shared_dates": dates, "batch_groups": []})
            for index, group in enumerate(groups):
                check_cancel(job.cancel_event)
                group["status"] = "searching"

                def progress(message, fraction=0, detail=None):
                    job.update(f"{index + 1}/{len(groups)} · {group['title']} · {message}",
                               (index + fraction) / len(groups))

                try:
                    group["scenes"] = self.service._search_profile(group["profile"], geometry, group["criteria"],
                                                                   cancel=job.cancel_event, progress=progress)
                    group["status"] = "ready" if any(_available(s) for s in group["scenes"]) else "empty"
                except Cancelled:
                    raise
                except Exception as error:
                    group.update(status="failed", error=_safe_error(error))
                group["query"] = {"id": job.id if index == 0 else f"{job.id}:{group['id']}",
                                  "profile": group["profile"], **group["criteria"]}
                job.update(f"{group['title']} · {len(group['scenes'])} 个结果" if not group["error"] else group["error"],
                           (index + 1) / len(groups),
                           {"batch_groups": [{"id": g["id"], "title": g["title"], "status": g["status"],
                                              "matches": len(g["scenes"]), "error": g["error"]} for g in groups]})
            with self.service.lock:
                check_cancel(job.cancel_event)
                self.geometry = geometry
                self.state = {"id": job.id, "shared_dates": dates, "groups": groups,
                              "geometry": mapping(geometry)}
                self.service.scenes = {s["key"]: s for s in groups[0]["scenes"]}
                self.service.last_query = groups[0]["query"]
                self.service._query_geometry = geometry
                return {"scenes": groups[0]["scenes"], "query": groups[0]["query"], "batch": self.snapshot()}

        with self.service.lock:
            self.service._assert_idle()
            if any(g["profile"] == S2_PROCESS for g in groups) and not self.service.auth.connected:
                raise UserError("本次来源包含 Sentinel Hub，请先在登录入口连接 OAuth 客户端凭据。")
            self.service.last_aoi = describe_aoi(geometry, self.service.last_aoi["name"] if self.service.last_aoi else "研究区")
            self.service._reset_query()
            return self.service._start_job("batch-search", operation)

    def _selected_group(self, group, supplied, geometry, *, primary=False):
        from .service import DEM, _criteria

        profile = group["profile"]
        if supplied.get("profile", profile) != profile:
            raise UserError("附带数据来源已经变化，请重新检索。")
        criteria = supplied.get("criteria")
        if not isinstance(criteria, dict) or _criteria(criteria, profile, shared_dates=not primary) != group["criteria"]:
            raise UserError("主数据或附带数据的日期、产品、筛选条件已变化，请重新检索。")
        keys = supplied.get("scene_keys", [])
        if (not isinstance(keys, list) or len(keys) > MAX_SCENES or any(not isinstance(k, str) for k in keys)
                or len(keys) != len(set(keys))):
            raise UserError("每个来源最多选择 500 个不同结果。")
        known = {s["key"]: s for s in group["scenes"]}
        if any(k not in known or not _available(known[k]) for k in keys):
            raise UserError("附带文件不在此次检索结果中，或没有可用下载入口。")
        options = supplied.get("options", {})
        if not isinstance(options, dict):
            raise UserError("附带导出选项无效。")
        aligned = supplied.get("align_to_primary", False)
        sample = supplied.get("sample_primary", False)
        if not isinstance(aligned, bool) or not isinstance(sample, bool):
            raise UserError("附带网格与采样选项无效。")
        if aligned and (primary or profile != GEDI or options.get("mode", "aligned") == "original" or options.get("reference_path")):
            raise UserError("只有派生 GEDI 可跟随主数据网格；使用已有参考文件时请关闭跟随。")
        if sample and not aligned:
            raise UserError("主影像采样需要勾选 GEDI 跟随主数据网格。")
        result = {"id": group["id"], "profile": profile, "title": group["title"], "criteria": group["criteria"],
                  "selected_count": len(keys), "matched_count": len(group["scenes"]), "scene_keys": keys,
                  "align_to_primary": aligned, "sample_primary": sample, "units": [],
                  "status": "selected" if keys else "search_failed" if group["status"] == "failed" else "empty" if not group["scenes"] else "not_selected",
                  "error": group["error"], "time_note": group["time_note"]}
        if not keys:
            return result
        scene_assets = supplied.get("scene_assets", {})
        if not isinstance(scene_assets, dict) or any(k not in keys for k in scene_assets):
            raise UserError("原始文件选择与附带产品不一致。")
        if not primary and options.get("mode") == "assets" and set(scene_assets) != set(keys):
            raise UserError("请为每个选中的附带产品勾选原始文件。")
        selected = [known[k] for k in keys]
        chunks = [[scene] for scene in selected] if options.get("mode") in ("assets", "asset") else [selected]
        for chunk in chunks:
            requested = copy.deepcopy(options)
            if options.get("mode") == "assets" and scene_assets:
                if chunk[0]["key"] not in scene_assets:
                    raise UserError("请为每个选中的产品勾选原始文件。")
                requested["assets"] = scene_assets[chunk[0]["key"]]
            normalized = self.service._selection_options(profile, group["criteria"], requested, chunk)
            plan = self.service._plan_source(profile, geometry, normalized, chunk, group["criteria"])
            result["units"].append({"options": normalized, "scenes": chunk, "plan": plan})
        return result

    @staticmethod
    def _references(primary):
        from .raw_download import _basename

        candidates = []
        for index, unit in enumerate(primary["units"]):
            plan = unit["plan"]
            if plan.get("kind") == "original":
                for target in plan["targets"]:
                    if Path(target["name"]).suffix.lower() in (".tif", ".tiff"):
                        candidates.append({"id": _fingerprint({"key": target["key"], "asset": target.get("asset"), "url": target["url"]}),
                                           "title": target["name"] + " · " + target["key"], "unit": index,
                                           "name": _basename(target) + Path(target["name"]).suffix.lower(),
                                           "scene_key": target["key"], "grid": None})
            elif plan.get("kind") == "gedi":
                if plan["options"]["mode"] == "aligned":
                    candidates.append({"id": "primary-gedi", "title": "主 GEDI 的参数 GeoTIFF 网格", "unit": index,
                                       "name": "gedi_metrics.tif" if plan["data_kind"] == "footprint" else None,
                                       "scene_key": None, "grid": plan["grid"]})
            else:
                for scene in ([{"key": "mosaic", "title": "所选区块的跨日期拼接"}] if plan.get("kind") == "mosaic" else unit["scenes"]):
                    candidates.append({"id": _fingerprint({"scene": scene["key"], "plan": plan["fingerprint"]}),
                                   "title": (scene.get("title") or scene["key"]) + " · 主 GeoTIFF", "unit": index,
                                   "name": None, "scene_key": scene["key"],
                                   "grid": {"width": plan["width"], "height": plan["height"], "bounds": plan["bounds"],
                                            "transform": list(from_origin(plan["bounds"][0], plan["bounds"][3], plan["resolution"], plan["resolution"]))[:6],
                                            "crs": f"EPSG:{plan['epsg']}", "epsg": plan["epsg"], "crs_label": plan["crs_label"],
                                            "pixel_size": [plan["resolution"]] * 2, "pixel_unit": "metre", "reference": None,
                                            "grid_source": "batch_primary"}})
        return candidates

    def prepare(self, value):
        with self.service.lock:
            if not self.state or value.get("batch_id") != self.state["id"] or value.get("query_id") != self.state["groups"][0]["query"]["id"]:
                raise UserError("联合检索结果已变化，请重新检索主数据和附带数据。")
            geometry = normalize_aoi(value.get("geometry"))
            if not geometry.equals(self.geometry):
                raise UserError("研究区已变化，请重新检索。")
            if value.get("shared_dates") != self.state["shared_dates"]:
                raise UserError("共同日期范围已变化，请重新检索。")
            companions = value.get("companions")
            expected = {g["id"] for g in self.state["groups"][1:]}
            if (not isinstance(companions, list) or len(companions) != len(expected) or
                    any(not isinstance(item, dict) for item in companions) or {item.get("id") for item in companions} != expected):
                raise UserError("附带数据勾选已变化，请重新检索；不会静默漏掉来源。")
            supplied = {item["id"]: item for item in companions}
            selected = [self._selected_group(g, value if index == 0 else supplied[g["id"]], geometry, primary=index == 0)
                        for index, g in enumerate(self.state["groups"])]
            if not any(g["units"] for g in selected):
                raise UserError("请在主数据或附带数据中选择至少一个可下载结果。")
            candidates = self._references(selected[0])
            reference_id = value.get("primary_reference")
            if reference_id is not None and not isinstance(reference_id, str):
                raise UserError("主影像参考选择无效。")
            reference = next((c for c in candidates if c["id"] == reference_id), None) if reference_id else (candidates[0] if candidates else None)
            if reference_id and reference is None:
                raise UserError("所选主影像参考已不在下载计划中，请重新确认。")
            for group in selected[1:]:
                if not group["align_to_primary"] or not group["units"]:
                    continue
                if reference is None:
                    raise UserError("跟随主数据需要选中的主 GeoTIFF。请选择主影像文件，或改为研究区 UTM 网格 / 已有 GeoTIFF。")
                for unit in group["units"]:
                    if reference["grid"]:
                        unit["plan"] = build_gedi_plan(geometry, unit["scenes"], unit["options"], group["criteria"],
                                                       reference_grid=reference["grid"])
                    else:
                        unit["plan"] = {**unit["plan"], "grid": None, "reference_pending": True}
            requires = set()
            groups = []
            for group in selected:
                public = {k: v for k, v in group.items() if k != "units"}
                public["plans"] = [u["plan"] for u in group["units"]]
                public["estimated_bytes"] = sum((p.get("estimated_bytes") or p.get("bytes_per_scene", 0) * p["scene_count"])
                                                + p.get("native_source_bytes", 0) for p in public["plans"])
                public["unknown_size"] = any(p.get("native_size_unknown") or p.get("kind") in ("original", "gedi")
                                             and not p.get("estimated_bytes") for p in public["plans"])
                if group["align_to_primary"] and group["units"]:
                    public["reference_source"] = reference["title"]
                    public["reference_grid_pending"] = reference["grid"] is None
                for plan in public["plans"]:
                    requires.update(plan.get("requires", []))
                if group["profile"] == "s2-process" and group["units"]:
                    requires.add("sentinel-hub")
                groups.append(public)
            signature = {"batch_version": 1, "geometry": mapping(normalize(geometry)), "shared_dates": self.state["shared_dates"],
                         "groups": groups, "primary_reference": reference["id"] if reference else None}
            fingerprint = _fingerprint(signature)
            plan = {"kind": "batch", "fingerprint": fingerprint, "folder_name": "联合下载_" + fingerprint,
                    "shared_dates": self.state["shared_dates"], "groups": groups, "requires": sorted(requires),
                    "scene_count": sum(g["selected_count"] for g in selected),
                    "estimated_bytes": sum(g["estimated_bytes"] for g in groups), "unknown_size": any(g["unknown_size"] for g in groups),
                    "reference_candidates": [{k: v for k, v in c.items() if k != "grid"} for c in candidates],
                    "primary_reference": reference["id"] if reference else None}
            return geometry, selected, reference, plan

    def plan(self, value):
        return self.prepare(value)[-1]

    def download(self, value):
        output = str(value.get("output_dir", "")).strip()
        if not output or not Path(output).expanduser().is_absolute():
            raise UserError("请选择完整的输出文件夹路径。")
        with self.service.lock:
            self.service._assert_idle()
            geometry, selected, reference, plan = self.prepare(value)
            if value.get("plan_fingerprint") != plan["fingerprint"]:
                raise UserError("联合下载参数、所选文件或参考已变化，请重新确认完整计划。")
            connected = {"earthdata": self.service.earthdata_auth.connected, "sentinel-hub": self.service.auth.connected,
                         "odata": self.service.raw_auth.connected, "cdse-s3": self.service.s3_auth.connected}
            labels = {"earthdata": "NASA Earthdata", "sentinel-hub": "Sentinel Hub OAuth", "odata": "CDSE OData", "cdse-s3": "CDSE S3"}
            missing = [labels.get(key, key) for key in plan["requires"] if not connected.get(key, False)]
            if missing:
                raise UserError("请先在登录入口连接 " + "、".join(missing) + "，再开始联合下载。")
            self.service.save_preferences({"output_dir": output})
            definition = {"title": f"联合下载 · {len(selected)} 个来源", "geometry": mapping(geometry), "groups": selected,
                          "reference": reference, "plan": plan, "output_dir": output,
                          "boundary_name": (self.service.last_aoi or {}).get("name", "研究区")}
            return self.service._start_job("batch-download", lambda job: self._download(job, geometry, selected, reference, plan, Path(output).expanduser()),
                                           definition=definition)

    def _download(self, job, geometry, groups, reference, plan, base):
        base = io_path(base)
        folder = base / plan["folder_name"]
        folder.mkdir(parents=True, exist_ok=True)
        boundary = folder / "研究区.shp"
        check_cancel(job.cancel_event)
        if boundary.exists():
            if not all(boundary.with_suffix(s).is_file() for s in (".shp", ".shx", ".dbf", ".prj", ".cpg")) or not load_aoi(boundary).equals(geometry):
                raise UserError("已有联合研究区边界缺失或变化，请移动该输出目录后重试。")
        else:
            save_aoi(geometry, boundary, getattr(job, "definition", {}).get("boundary_name", "研究区"))
        manifest_path = folder / ("联合下载记录_" + uuid.uuid4().hex[:10] + ".json")
        records = [{"id": g["id"], "title": g["title"], "profile": g["profile"], "criteria": g["criteria"],
                    "scene_keys": g["scene_keys"], "status": "pending" if g["units"] else g["status"], "error": g["error"],
                    "align_to_primary": g["align_to_primary"], "results": [], "failures": []} for g in groups]
        document = {"software": VERSION, "status": "running", "plan": plan, "geometry": mapping(geometry),
                    "shared_dates": plan["shared_dates"], "sources": records, "boundary": str(boundary)}
        lock = threading.RLock()
        fractions = [0 if g["units"] else 1 for g in groups]
        resolved_reference = None

        def update(index, message, fraction=0):
            with lock:
                fractions[index] = fraction
                job.update(f"{groups[index]['title']} · {message}", sum(fractions) / len(groups),
                           {"folder": str(folder), "batch_groups": [{"id": r["id"], "title": r["title"], "status": r["status"],
                                                                     "error": r["error"]} for r in records]})

        def run(index):
            group, record = groups[index], records[index]
            record["status"] = "running"
            destination = folder / (f"{index + 1:02d}_" + ("主数据" if index == 0 else group["profile"].replace("-", "_")))
            try:
                check_cancel(job.cancel_event)
                for unit_index, unit in enumerate(group["units"]):
                    check_cancel(job.cancel_event)
                    options, actual_plan = unit["options"], unit["plan"]
                    if group["align_to_primary"]:
                        options = gedi_options({**options, "reference_path": str(resolved_reference),
                                                "sample_reference": group["sample_primary"]}, group["criteria"]["collection"])
                        actual_plan = build_gedi_plan(geometry, unit["scenes"], options, group["criteria"])

                    def progress(message, fraction=0, detail=None):
                        update(index, message, (unit_index + fraction) / len(group["units"]))

                    try:
                        result = self.service._run_source(group["profile"], geometry, options, unit["scenes"], actual_plan,
                                                          str(destination), cancel=job.cancel_event, progress=progress)
                        with lock:
                            record["results"].append({"unit": unit_index, **result})
                    except Cancelled as error:
                        partial = getattr(error, "download_result", None)
                        if partial is not None:
                            with lock:
                                record["results"].append({"unit": unit_index, "interrupted": True, **partial})
                        raise
                    except Exception as error:
                        with lock:
                            partial = getattr(error, "download_result", None)
                            if partial is not None:
                                record["results"].append({"unit": unit_index, "interrupted": True, **partial})
                            record["failures"].append({"scene_keys": [s["key"] for s in unit["scenes"]], "error": _safe_error(error)})
                if record["failures"]:
                    record["status"] = "partial" if any(r["files"] for r in record["results"]) else "failed"
                    record["error"] = record["failures"][0]["error"]
                else:
                    record["status"] = "completed" if any(r["files"] for r in record["results"]) else "empty"
            except Cancelled:
                record["status"] = "cancelled"
            except Exception as error:
                record.update(status="failed", error=_safe_error(error))
            finally:
                update(index, {"completed": "下载完成", "empty": "研究区无有效输出", "partial": "部分完成", "cancelled": "已取消", "failed": record["error"]}.get(record["status"], record["status"]), 1)
                with lock:
                    _write_json(manifest_path, document)

        def primary_reference():
            if reference is None:
                return None
            for result in records[0]["results"]:
                if result["unit"] != reference["unit"]:
                    continue
                for item in result["files"]:
                    path = Path(item["path"])
                    if (path.suffix.lower() in (".tif", ".tiff") and path.is_file()
                            and (reference["name"] is None or path.name == reference["name"])
                            and (not reference["scene_key"] or item.get("scene", reference["scene_key"]) == reference["scene_key"])):
                        return path
            return None

        pending = {i for i, group in enumerate(groups) if i and group["units"] and group["align_to_primary"]}
        _write_json(manifest_path, document)
        try:
            for index, group in enumerate(groups):
                check_cancel(job.cancel_event)
                if not group["units"]:
                    continue
                if index in pending and resolved_reference is None:
                    records[index].update(status="dependency_failed", error="所选主 GeoTIFF 未成功生成，未用另一影像替代；请重试或关闭跟随。")
                    update(index, records[index]["error"], 1)
                    continue
                run(index)
                if index == 0:
                    resolved_reference = primary_reference()
            check_cancel(job.cancel_event)
            files = [{**item, "source": record["id"]} for record in records for result in record["results"] for item in result["files"]]
            failures = [r for r in records if r["status"] in ("failed", "partial", "dependency_failed", "search_failed")]
            status = "partial" if failures and files else "failed" if failures else "completed"
            document["status"] = status
            document["primary_reference"] = str(resolved_reference) if resolved_reference else None
            _write_json(manifest_path, document)
            files.extend({"path": str(boundary.with_suffix(ext)), "status": "saved", "source": "boundary"}
                         for ext in (".shp", ".shx", ".dbf", ".prj", ".cpg"))
            files.append({"path": str(manifest_path), "status": "saved", "source": "manifest"})
            return {"status": status, "folder": str(folder), "files": files, "manifest": str(manifest_path),
                    "sources": records, "plan": plan, "empty_scenes": []}
        except Cancelled as error:
            document["status"] = "cancelled"
            for record in records:
                if record["status"] == "pending":
                    record["status"] = "cancelled"
            _write_json(manifest_path, document)
            from .errors import keep_download_result
            files = [{**item, "source": record["id"]} for record in records for result in record["results"] for item in result["files"]]
            keep_download_result(error, files, folder, manifest_path, plan)
            raise
