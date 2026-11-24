"""Frozen, sequential Sentinel-2 mosaics, with an independent UTC month per output."""
from __future__ import annotations

import copy
import datetime as dt
import hashlib
import json
import re
import time
from pathlib import Path

from shapely import normalize
from shapely.geometry import mapping

from . import VERSION
from .batch_workflow import _safe_error, _write_json
from .errors import Cancelled, UserError
from .geometry import load_aoi, save_aoi
from .http import check_cancel
from .integrity import sha256_file
from .local_paths import io_path
from .scene_identity import utc_time
from .spatial_selection import available, coverage_summary, priority_option

PROFILES = frozenset({"s2-process", "s2-earth-search", "s2-cdse-s3"})
MAX_MONTHS = 120
SCHEMA = 2


def month_range(start, end):
    def parse(value):
        if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}", value):
            raise UserError("请选择开始和结束月份，格式为 YYYY-MM。")
        try:
            return dt.date.fromisoformat(value + "-01")
        except ValueError:
            raise UserError("月份无效，请重新选择。") from None

    first, last = parse(start), parse(end)
    current_date = dt.datetime.now(dt.timezone.utc).date()
    today = current_date.replace(day=1)
    count = (last.year - first.year) * 12 + last.month - first.month + 1
    if first < dt.date(2015, 6, 1) or last > today:
        raise UserError("Sentinel-2 月份范围为 2015-06 至当前 UTC 月份。")
    if not 1 <= count <= MAX_MONTHS:
        raise UserError(f"开始月份不能晚于结束月份；每次最多选择 {MAX_MONTHS} 个月，可分批下载。")
    result = []
    for offset in range(count):
        number = first.year * 12 + first.month - 1 + offset
        year, month = divmod(number, 12)
        day = dt.date(year, month + 1, 1)
        following = dt.date(year + (month == 11), (month + 1) % 12 + 1, 1)
        result.append({"month": day.strftime("%Y-%m"),
                       "start": max(day, dt.date(2015, 6, 23)).isoformat(),
                       "end": min(following - dt.timedelta(days=1), current_date).isoformat()})
    return result


def in_month(scene, month):
    """Check actual acquisition instants, including every whitelisted product."""
    try:
        instants = [scene["datetime"]] + [p["acquisition_utc"] for p in scene.get("products", [])]
        return bool(scene.get("products")) and all(utc_time(value).strftime("%Y-%m") == month for value in instants)
    except (ValueError, TypeError, KeyError, AttributeError):
        return False


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                                    allow_nan=False).encode("utf-8")).hexdigest()[:16]


def _request_digest(value):
    return _digest({key: item for key, item in value.items() if key not in ("output_dir", "plan_fingerprint")})


class MonthlyWorkflow:
    def __init__(self, service):
        self.service = service
        self.state = None
        self.geometry = None
        self.prepared = None

    def clear(self):
        self.state = self.geometry = self.prepared = None

    def snapshot(self):
        return copy.deepcopy(self.state)

    def search(self, value):
        from .service import _criteria, _profile

        profile = _profile(value)
        if profile not in PROFILES:
            raise UserError("按月拼接支持 Sentinel-2 的公开区域裁剪、Sentinel Hub 和官方 S3 区域路线。")
        geometry, aoi = self.service._request_aoi(value)
        dates = month_range(value.get("start_month"), value.get("end_month"))
        groups = [{**period, "criteria": _criteria({**period, "max_cloud": value.get("max_cloud", 20)}, profile),
                   "scenes": [], "recommended_keys": [], "status": "pending", "error": None} for period in dates]
        priority = priority_option(value.get("options", {}))

        def operation(job):
            for index, group in enumerate(groups):
                check_cancel(job.cancel_event)
                group["status"] = "searching"

                def progress(message, fraction=0, detail=None):
                    job.update(f"{index + 1}/{len(groups)} · {group['month']} · {message}",
                               (index + fraction) / len(groups), {"monthly_groups": self._progress(groups)})

                try:
                    found = self.service._search_profile(profile, geometry, group["criteria"],
                                                         cancel=job.cancel_event, progress=progress)
                    group["scenes"] = [scene for scene in found if in_month(scene, group["month"])]
                    group["excluded_outside_month"] = len(found) - len(group["scenes"])
                    group["status"] = "ready" if any(available(scene) for scene in group["scenes"]) else "empty"
                    if group["status"] == "ready":
                        summary = coverage_summary(geometry, group["scenes"], [], priority, cancel=job.cancel_event)
                        group["recommended_keys"] = summary["recommended_keys"]
                except Cancelled:
                    raise
                except Exception as error:
                    group.update(status="failed", error=_safe_error(error))
                group["query"] = {"id": job.id + ":" + group["month"], "profile": profile, **group["criteria"]}
                progress(group["error"] or f"{len(group['scenes'])} 个本月产品", 1)
            with self.service.lock:
                check_cancel(job.cancel_event)
                self.geometry = geometry
                self.state = {"id": job.id, "profile": profile, "start_month": dates[0]["month"],
                              "end_month": dates[-1]["month"], "geometry": mapping(geometry),
                              "aoi": aoi, "groups": groups}
                first = next((group for group in groups if group["status"] == "ready"), groups[0])
                self.service.scenes = {scene["key"]: scene for scene in first["scenes"]}
                self.service.last_query = first["query"]
                self.service._query_geometry = geometry
                return {"monthly": self.snapshot(), "scenes": first["scenes"], "query": first["query"]}

        with self.service.lock:
            self.service._assert_idle()
            self.service._check_access(["sentinel-hub"] if profile == "s2-process" else
                                       ["cdse-s3"] if profile == "s2-cdse-s3" else [])
            self.service.last_aoi = aoi
            self.service._reset_query()
            return self.service._start_job("monthly-search", operation)

    @staticmethod
    def _progress(groups):
        return [{"id": group["month"], "title": group["month"], "month": group["month"],
                 "status": group["status"], "matches": len(group.get("scenes", [])), "error": group.get("error")}
                for group in groups]

    def _state_for(self, value):
        geometry, _ = self.service._request_aoi(value)
        with self.service.lock:
            if (not self.state or value.get("monthly_id") != self.state["id"]
                    or value.get("profile") != self.state["profile"] or not geometry.equals(self.geometry)):
                raise UserError("城市、来源或按月检索已变化，请重新检索。")
            return geometry, copy.deepcopy(self.state)

    def _selection(self, group, value, profile, *, coverage=False):
        from .service import _criteria

        if (value.get("query_id") != group["query"]["id"] or not isinstance(value.get("criteria"), dict)
                or _criteria(value["criteria"], profile) != group["criteria"]):
            raise UserError("本月产品或筛选条件已变化，请重新检索。")
        keys = value.get("scene_keys", [])
        known = {scene["key"]: scene for scene in group["scenes"]}
        if (not isinstance(keys, list) or len(keys) > 500 or any(not isinstance(key, str) or key not in known for key in keys)
                or len(keys) != len(set(keys)) or any(not available(known[key]) or not in_month(known[key], group["month"]) for key in keys)):
            raise UserError("每个月只能选择本次检索中最多 500 个不同的可用产品，不能混入其他月份。")
        supplied = value.get("options", {})
        if not isinstance(supplied, dict):
            raise UserError("本月导出选项无效。")
        scenes = [known[key] for key in keys]
        if coverage or not scenes:
            return {"priority": priority_option(supplied)}, scenes
        options = self.service._selection_options(profile, group["criteria"], supplied, scenes)
        if options["layout"] != "mosaic":
            raise UserError("按月下载每月输出一份拼接影像，请使用区域拼接方式。")
        options["monthly_pipeline"] = "download-scenes-mosaic-clip"
        options["profile"] = profile
        return options, scenes

    def coverage(self, value, *, cancel=None):
        geometry, state = self._state_for(value)
        group = next((group for group in state["groups"] if group["month"] == value.get("month")), None)
        if group is None:
            raise UserError("请选择本次检索中的月份。")
        options, scenes = self._selection(group, value, state["profile"], coverage=True)
        return coverage_summary(geometry, group["scenes"], [scene["key"] for scene in scenes], options["priority"], cancel=cancel)

    def prepare(self, value, *, cancel=None, progress=None):
        geometry, state = self._state_for(value)
        if value.get("start_month") != state["start_month"] or value.get("end_month") != state["end_month"]:
            raise UserError("月份范围已变化，请重新检索。")
        requested = value.get("months")
        if not isinstance(requested, list) or len(requested) != len(state["groups"]) or any(not isinstance(row, dict) for row in requested):
            raise UserError("请检查各个月份的下载选择。")
        by_month = {row.get("month"): row for row in requested if isinstance(row.get("month"), str)}
        if len(by_month) != len(requested) or set(by_month) != {g["month"] for g in state["groups"]}:
            raise UserError("月份选择重复或不属于本次检索。")
        selected, previews, requirements = [], [], set()
        for index, group in enumerate(state["groups"]):
            check_cancel(cancel)
            row = by_month[group["month"]]
            include = row.get("include", True)
            if not isinstance(include, bool):
                raise UserError("月份勾选状态无效。")
            options, scenes = self._selection(group, row, state["profile"])
            if not include:
                scenes = []
            status = ("selected" if scenes else "not_selected" if not include or group["status"] == "ready" else
                      "search_failed" if group["status"] == "failed" else "empty")
            frozen = {"month": group["month"], "criteria": group["criteria"], "scenes": scenes,
                      "scene_keys": [scene["key"] for scene in scenes], "options": options, "status": status,
                      "error": group["error"], "matched_count": len(group["scenes"]), "plan": None}
            preview = {key: frozen[key] for key in ("month", "criteria", "scene_keys", "status", "error", "matched_count")}
            preview["selected_count"] = len(scenes)
            if scenes:
                if progress:
                    progress(f"规划 {group['month']} 的完整研究区", index / len(state["groups"]))
                plan = self.service._plan_source(state["profile"], geometry, options, scenes, group["criteria"], cancel=cancel)
                # The frozen plan needs its contract and grid, not duplicate city polygons.
                compact = {key: plan[key] for key in ("kind", "fingerprint", "width", "height", "epsg", "crs_label", "bounds",
                           "resolution", "bytes_per_scene", "dates", "source_count", "estimated_pu")}
                compact["requires"] = list(plan.get("requires", [])) + (["sentinel-hub"] if state["profile"] == "s2-process" else [])
                compact["coverage_percent"] = plan["coverage"]["coverage_percent"]
                for key in ("pipeline", "pipeline_schema", "whole_mosaic_bytes", "staging_required_bytes"):
                    if key in plan:
                        compact[key] = plan[key]
                frozen["plan"] = compact
                requirements.update(compact["requires"])
                preview.update(plan=compact, estimated_bytes=plan["bytes_per_scene"],
                               native_source_bytes=plan.get("native_source_bytes", 0),
                               whole_mosaic_bytes=plan.get("whole_mosaic_bytes", 0),
                               staging_required_bytes=plan.get("staging_required_bytes", 0), options=options)
            selected.append(frozen)
            previews.append(preview)
        if not any(row["scenes"] for row in selected):
            raise UserError("至少选择一个有可用产品的月份；无匹配月份不会生成影像。")
        fingerprint = _digest({"schema": SCHEMA, "geometry": mapping(normalize(geometry)), "profile": state["profile"],
                               "months": [{key: group[key] for key in ("month", "criteria", "scene_keys", "options", "status", "plan")}
                                          for group in selected]})
        region = state["aoi"].get("region", {})
        label = re.sub(r"[^\w\u3400-\u9fff.-]+", "_", state["aoi"]["name"])[:40]
        plan = {"kind": "monthly", "schema": SCHEMA, "profile": state["profile"], "fingerprint": fingerprint,
                "monthly_id": state["id"], "start_month": state["start_month"], "end_month": state["end_month"],
                "months": previews, "scene_count": sum(len(group["scenes"]) for group in selected),
                "output_count": sum(bool(group["scenes"]) for group in selected), "requires": sorted(requirements),
                "estimated_bytes": sum(row.get("estimated_bytes", 0) for row in previews),
                "native_source_bytes": sum(row.get("native_source_bytes", 0) for row in previews),
                "whole_mosaic_bytes": sum(row.get("whole_mosaic_bytes", 0) for row in previews),
                "staging_required_bytes": sum(row.get("staging_required_bytes", 0) for row in previews),
                "pipeline": "download-scenes-mosaic-clip",
                "estimated_pu": sum((row.get("plan") or {}).get("estimated_pu") or 0 for row in previews),
                "boundary_name": state["aoi"]["name"], "region": region,
                "folder_name": f"{label}_月度_{state['start_month']}_{state['end_month']}_{fingerprint}",
                "time_note": "UTC 逐月处理；先下载本月全部已选整景，再本地整月拼接，最后按完整边界裁剪；只使用该月产品。"}
        check_cancel(cancel)
        with self.service.lock:
            if not self.state or self.state["id"] != state["id"]:
                raise UserError("按月检索已变化，请重新确认计划。")
            self.prepared = {"digest": _request_digest(value), "created": time.monotonic(), "plan": plan,
                             "groups": selected, "aoi": state["aoi"]}
        return plan

    def download(self, value):
        output = self.service._output_directory(value)
        with self.service.lock:
            self.service._assert_idle()
            prepared = self.prepared
            if (not prepared or prepared["digest"] != _request_digest(value)
                    or value.get("plan_fingerprint") != prepared["plan"]["fingerprint"]):
                raise UserError("月份、产品或参数已变化，请先重新确认下载计划。")
            plan, groups, aoi = copy.deepcopy(prepared["plan"]), copy.deepcopy(prepared["groups"]), copy.deepcopy(prepared["aoi"])
            self.service._check_access(plan["requires"])
            self.service.save_preferences({"output_dir": output})
            definition = {"title": f"{aoi['name']} · {plan['output_count']} 个月的拼接影像", "profile": plan["profile"],
                          "geometry": aoi["geometry"], "region": aoi.get("region"), "boundary_name": aoi["name"],
                          "groups": groups, "plan": plan, "output_dir": output}
            return self.service._start_job("monthly-download", lambda job: self.run(job, definition), definition=definition)

    def run(self, job, definition):
        from .geometry import normalize_aoi

        geometry = normalize_aoi(definition["geometry"])
        plan, groups = definition["plan"], definition["groups"]
        for group in groups:
            if any(not in_month(scene, group["month"]) for scene in group["scenes"]):
                raise UserError("续作记录含有其他月份的产品，已停止；请重新选择月份。")
        folder = io_path(Path(definition["output_dir"]).expanduser()) / plan["folder_name"]
        folder.mkdir(parents=True, exist_ok=True)
        boundary = folder / "研究区.shp"
        manifest = folder / "月度下载记录.json"
        components = [boundary.with_suffix(ext) for ext in (".shp", ".shx", ".dbf", ".prj", ".cpg")]
        check_cancel(job.cancel_event)
        if any(path.exists() for path in components):
            if not all(path.is_file() for path in components) or not load_aoi(boundary).equals(geometry):
                raise UserError("已有月度研究区边界缺失或变化，请选择新的输出目录；原文件已保留。")
            if manifest.is_file():
                try:
                    if manifest.stat().st_size > 32 * 1024**2:
                        raise ValueError
                    old = json.loads(manifest.read_text(encoding="utf-8"))
                    hashes = {Path(item["path"]).suffix: item["sha256"] for item in old["boundary_files"]}
                    if len(hashes) != 5 or any(sha256_file(path, job.cancel_event) != hashes.get(path.suffix) for path in components):
                        raise ValueError
                except (OSError, ValueError, KeyError, TypeError):
                    raise UserError("已有完整研究区 SHP 的来源校验失败，请选择新的目录；原文件已保留。") from None
        else:
            save_aoi(geometry, boundary, definition["boundary_name"])
        records = [{"month": group["month"], "criteria": group["criteria"], "scene_keys": group["scene_keys"],
                    "status": "pending" if group["scenes"] else group["status"], "error": group["error"],
                    "result": None} for group in groups]
        document = {"software": VERSION, "schema": plan.get("schema", SCHEMA), "status": "running", "plan": plan,
                    "geometry": definition["geometry"], "region": definition.get("region"),
                    "boundary": str(boundary), "boundary_files": [{"path": str(path), "sha256": sha256_file(path, job.cancel_event)}
                                                                  for path in components], "months": records}

        def result(status):
            return {"status": status, "plan": plan, "months": copy.deepcopy(records), "folder": str(folder),
                    "manifest": str(manifest), "files": [item for row in records for item in (row["result"] or {}).get("files", [])],
                    "empty_scenes": []}

        def report(index, message, fraction=0, detail=None):
            if detail and detail.get("month_stage"):
                records[index]["stage"] = detail["month_stage"]
            job.update(f"{index + 1}/{len(groups)} · {groups[index]['month']} · {message}",
                       (index + fraction) / len(groups),
                       {"folder": str(folder), "monthly_groups": [{"id": row["month"], "month": row["month"],
                          "title": row["month"], "status": row["status"], "stage": row.get("stage"),
                          "error": row["error"]} for row in records]})

        _write_json(manifest, document)
        try:
            for index, group in enumerate(groups):
                check_cancel(job.cancel_event)
                row = records[index]
                if not group["scenes"]:
                    report(index, "本月没有匹配产品" if row["status"] == "empty" else "本月未选择产品" if row["status"] == "not_selected" else row["error"], 1)
                    continue
                row["status"] = "running"
                report(index, "核验该月计划与已有文件")
                _write_json(manifest, document)
                try:
                    row["result"] = self.service._run_source(definition["profile"], geometry, group["options"], group["scenes"],
                                                              group["plan"], str(folder / group["month"]),
                                                              cancel=job.cancel_event,
                                                              progress=lambda message, fraction=0, detail=None: report(index, message, fraction, detail))
                    if any(date[:7] != group["month"] for date in row["result"].get("used_dates", [])):
                        raise UserError("输出来源日期不属于本月，已停止本任务；请检查来源记录。")
                    row["status"] = "completed" if row["result"]["files"] else "no_valid_pixels"
                except Cancelled as error:
                    row.update(status="cancelled", result=getattr(error, "download_result", None))
                    raise
                except Exception as error:
                    row.update(status="failed", error=_safe_error(error), result=getattr(error, "download_result", row["result"]))
                finally:
                    _write_json(manifest, document)
                report(index, row["error"] or ("本月下载完成" if row["status"] == "completed" else "本月没有有效输出"), 1)
            check_cancel(job.cancel_event)
            document["status"] = "partial" if any(row["status"] in ("failed", "search_failed") for row in records) else "completed"
            return result(document["status"])
        except Cancelled as error:
            document["status"] = "cancelled"
            error.download_result = result("cancelled")
            raise
        except Exception as error:
            document["status"] = "failed"
            error.download_result = result("failed")
            raise
        finally:
            _write_json(manifest, document)
