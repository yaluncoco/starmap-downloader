from __future__ import annotations

import datetime as dt
import copy
import hashlib
import json
import os
import threading
import time
import uuid
import webbrowser
from collections import OrderedDict
from pathlib import Path
from shapely import normalize
from shapely.geometry import mapping

from . import VERSION
from .batch_sources import SOURCE_INFO, public_sources as batch_sources
from .batch_workflow import BatchWorkflow
from .catalogue import date_range
from .download import download_scenes
from .download_tasks import DownloadTasks, INCOMPLETE
from .errors import Cancelled, UserError, io_diagnostic
from .accounts import Accounts
from .cities import CityCatalogue
from .map_tiles import MapTiles
from .monthly_workflow import MonthlyWorkflow
from .earthdata_auth import EarthdataAuth
from .gedi_catalogue import GEDI, COLLECTIONS as GEDI_COLLECTIONS, DEFAULT_COLLECTION as GEDI_DEFAULT, search_gedi
from .gedi_catalogue import collection_id as gedi_collection, public_collections as gedi_collections
from .gedi_download import build_gedi_plan, download_gedi, normalize_options as gedi_options
from .geometry import describe_aoi, load_aoi, normalize_aoi, parse_coordinates, save_aoi
from .http import AuthManager, HttpClient, check_cancel
from .local_paths import display_path, io_path
from .planning import build_plan, normalize_options
from .mosaic import build_mosaic_plan
from .native_mosaic import PROFILE as S2_NATIVE, download_native_mosaic, search_native
from .spatial_selection import coverage_summary, priority_option, split_process_scenes
from .native_catalogue import collection_id, list_collections, search_original
from .open_l2a import ES_COLLECTION, S2_L2A_COG, search_l2a
from .open_l3 import COLLECTION as L3_COLLECTION, S2_L3_PUBLIC, search_l3
from .open_modis import COLLECTIONS as MODIS, MODIS_MIN_DATE, search_modis
from .open_native import LANDSAT_COLLECTION, LANDSAT_OPEN, L1C_COLLECTION, S2_L1C_OPEN, search_open_native
from .open_worldcover import PROFILE_LAYERS as WORLD_COVER, S2_ANNUAL, search_worldcover
from .public_catalogue import DEM, DEM_30, DEM_90, S2_PUBLIC, search_dem, search_earth
from .public_download import download_public, public_plan
from .public_radar import S1_CATALOGUE, S1_PUBLIC, search_public_radar
from .public_rtc import RTC_MODES, S1_RTC, rtc_options, search_rtc
from .raw_auth import RawAuth
from .raw_download import download_original, raw_plan
from .s3_source import CDSES3Auth
from .process_catalogue import cloud_limit, find_scenes
from .open_s5p import DEFAULT_COLLECTION as S5P_DEFAULT, S5P_PUBLIC, public_collections, s5p_collection, search_s5p
from .open_sen3 import DEFAULT_COLLECTION as SEN3_DEFAULT, SEN3_PUBLIC, sen3_collection, search_sen3
from .open_sen3 import public_collections as sen3_collections
from .open_slc import S1_SLC, SLC_COLLECTION, SLC_FIRST, search_slc
from .sen3_download import download_sen3
from .updates import InstallerLaunchError, UpdateClient, packaged_repository, version_key
from .safe_zip import PROFILE as S2_ZIP, build_zip_plan, download_options as zip_download_options, search_zip

S2_PROCESS = "s2-process"
ORIGINAL = "cdse-original"
NATIVE_OPEN = {S1_PUBLIC: (S1_CATALOGUE, "public"), S2_L1C_OPEN: (L1C_COLLECTION, "public"), S2_L2A_COG: (ES_COLLECTION, "public"),
               S2_L3_PUBLIC: (L3_COLLECTION, "public"), LANDSAT_OPEN: (LANDSAT_COLLECTION, "pc-public"),
               S5P_PUBLIC: (None, "public"), SEN3_PUBLIC: (None, "public"), S1_SLC: (SLC_COLLECTION, "public"),
               **{profile: (profile, "public") for profile in WORLD_COVER},
               **{profile: (profile, "pc-public") for profile in MODIS}}
PROFILE_IDS = (S2_ZIP, S2_PROCESS, S2_PUBLIC, S2_NATIVE, *NATIVE_OPEN, S1_RTC, DEM_30, DEM_90, GEDI, ORIGINAL)
RADAR_MODES = ("", "IW", "EW", "SM", "WV")


def _profile(value):
    profile = value.get("profile", S2_PROCESS)
    if profile not in PROFILE_IDS:
        raise UserError("请选择支持的影像来源和类别。")
    return profile


def _criteria(value, profile, *, shared_dates=False):
    if profile in DEM:
        if value.get("start") or value.get("end"):
            raise UserError("Copernicus DEM 是静态数据，请勿设置采集日期。")
        return {"start": None, "end": None}
    if profile == ORIGINAL:
        key = collection_id(value.get("collection"))
        mode = value.get("radar_mode") or ""
        if mode not in RADAR_MODES or (mode and key != S1_CATALOGUE):
            raise UserError("无效的 Sentinel-1 GRD 雷达模式。")
        filters = {"collection": key, "radar_mode": mode}
        if value.get("start") is None and value.get("end") is None:
            return {**filters, "start": None, "end": None}
        minimum = dt.date(1900, 1, 1)
    elif profile in (S1_PUBLIC, S1_RTC):
        mode = value.get("radar_mode") or ""
        if mode not in (RTC_MODES if profile == S1_RTC else RADAR_MODES):
            raise UserError("无效的 Sentinel-1 成像模式；RTC 仅提供 IW 和 EW。" if profile == S1_RTC else "无效的 Sentinel-1 GRD 雷达模式。")
        filters = {"radar_mode": mode}
        minimum = dt.date(2014, 4, 3)
    elif profile == GEDI:
        key = gedi_collection(value.get("collection"))
        filters = {"collection": key}
        minimum = dt.date(2019, 4, 4)
    elif profile == S1_SLC:
        filters = {}
        minimum = SLC_FIRST
    elif profile in MODIS:
        filters = {}
        minimum = MODIS_MIN_DATE
    elif profile == S2_L3_PUBLIC or profile in WORLD_COVER:
        filters = {}
        minimum = dt.date(1900 if profile in WORLD_COVER else 2017, 1, 1)
    elif profile == SEN3_PUBLIC:
        filters = {"collection": sen3_collection(value.get("collection"))}
        minimum = dt.date(2016, 2, 16)
    elif profile == S5P_PUBLIC:
        filters = {"collection": s5p_collection(value.get("collection"))}
        minimum = dt.date(2018, 1, 1)
    else:
        filters = {"max_cloud": cloud_limit(value.get("max_cloud", 20))}
        minimum = dt.date(1982, 1, 1) if profile == LANDSAT_OPEN else dt.date(2015, 6, 23)
    start, stop = date_range(value.get("start"), value.get("end"), dt.date(1900, 1, 1) if shared_dates else minimum)
    return {**filters, "start": start.isoformat(), "end": (stop - dt.timedelta(days=1)).isoformat()}


OFFICIAL_LINKS = {
    "dashboard": "https://shapps.dataspace.copernicus.eu/dashboard/#/account/settings",
    "register": "https://dataspace.copernicus.eu/",
    "auth": "https://documentation.dataspace.copernicus.eu/APIs/SentinelHub/Overview/Authentication.html",
    "docs": "https://documentation.dataspace.copernicus.eu/APIs/SentinelHub/Process/Examples/S2L2A.html",
    "quotas": "https://documentation.dataspace.copernicus.eu/Quotas.html",
    "browser": "https://browser.dataspace.copernicus.eu/",
    "raw-auth": "https://documentation.dataspace.copernicus.eu/APIs/Token.html",
    "raw-catalogue": "https://documentation.dataspace.copernicus.eu/APIs/STAC.html",
    "s3-keys": "https://eodata-s3keysmanager.dataspace.copernicus.eu/",
    "s3-docs": "https://documentation.dataspace.copernicus.eu/APIs/S3.html",
    "worldcover": "https://esa-worldcover.org/en/data-access",
    "earthdata-register": "https://urs.earthdata.nasa.gov/users/new",
    "earthdata-token": "https://urs.earthdata.nasa.gov/profile",
    "earthdata-auth": "https://urs.earthdata.nasa.gov/documentation/for_users/user_token",
    "gedi-docs": "https://gedi.umd.edu/data/products/",
    **{"gedi:" + key: "https://search.earthdata.nasa.gov/search/granules?p=" + spec["concept_id"] for key, spec in GEDI_COLLECTIONS.items()},
    **{profile: f"https://planetarycomputer.microsoft.com/dataset/{profile}" for profile in MODIS},
}


class Job:
    def __init__(self, kind, notify=None):
        self.id = uuid.uuid4().hex
        self.kind = kind
        self.cancel_event = threading.Event()
        self.lock = threading.RLock()
        self.notify = notify
        self.data = {"id": self.id, "kind": kind, "status": "running", "message": "准备中", "progress": 0.0, "logs": [], "result": None, "detail": {}, "error": None}

    def update(self, message, progress=0.0, detail=None):
        with self.lock:
            self.data["message"] = message
            self.data["progress"] = max(0.0, min(1.0, float(progress)))
            if detail:
                self.data["detail"].update(detail)
            if not self.data["logs"] or self.data["logs"][-1]["message"] != message:
                self.data["logs"].append({"time": time.time(), "message": message})
                self.data["logs"] = self.data["logs"][-80:]
        if self.notify:
            self.notify(self.snapshot())

    def is_active(self):
        with self.lock:
            return self.data["status"] in ("running", "cancelling")

    def snapshot(self):
        with self.lock:
            result = json.loads(json.dumps(self.data))
            if hasattr(self, "definition"):
                result["retry_available"] = result["status"] in INCOMPLETE and not result.get("replaced_by")
            return result


class Service:
    def __init__(self, config_dir=None):
        self.auth = AuthManager()
        self.raw_auth = RawAuth()
        self.browser_download = None
        self.s3_auth = CDSES3Auth()
        self.earthdata_auth = EarthdataAuth()
        self._collections_lock = threading.Lock()
        self._collections_cache = None
        self.jobs = {}
        self._jobs_lock = threading.RLock()
        self.lock = threading.RLock()
        self.scenes = {}
        self.last_aoi = None
        self.last_query = None
        self.cities = CityCatalogue()
        self._query_geometry = None
        self.batch = BatchWorkflow(self)
        self.monthly = MonthlyWorkflow(self)
        self._plans_lock = threading.RLock()
        self._source_plans = OrderedDict()
        self._connecting = False
        self._geocode_lock = threading.Lock()
        self._last_geocode = 0.0
        self._geocode_cache = {}
        root = Path(config_dir) if config_dir else Path(os.environ.get("LOCALAPPDATA", Path.home())) / "CopernicusDownloader"
        self.config_dir = root
        self.map_tiles = MapTiles(root / "map-cache")
        self.download_tasks = DownloadTasks(root)
        self.pref_file = root / "preferences.json"
        self.preferences = self._load_preferences()
        self.updates = UpdateClient(packaged_repository())
        self._update_check_lock = threading.Lock()
        self._update_release = None
        self._downloaded_update = None
        self.accounts = Accounts(root, self.auth, self.raw_auth, self.s3_auth, self.earthdata_auth)
        self.accounts.start()

    def _load_preferences(self):
        defaults = {"output_dir": str(Path.home() / "Downloads" / "星图影像"), "client_id": ""}
        try:
            value = json.loads(self.pref_file.read_text(encoding="utf-8"))
            if isinstance(value, dict):
                defaults.update({key: value[key] for key in defaults if isinstance(value.get(key), str)})
        except (OSError, ValueError, TypeError):
            pass
        return defaults

    def save_preferences(self, value):
        # Preferences remain allowlisted; reusable credentials use the separate DPAPI store.
        with self.lock:
            for key in ("output_dir", "client_id"):
                if isinstance(value.get(key), str):
                    self.preferences[key] = value[key][:2048]
            try:
                self.config_dir.mkdir(parents=True, exist_ok=True)
                self.pref_file.write_text(json.dumps(self.preferences, ensure_ascii=False, indent=2), encoding="utf-8")
            except OSError:
                pass
        return {"saved": True}

    def bootstrap(self):
        with self.lock:
            active = self.active_job()
            restoring = self.accounts.restoring
            return {"version": VERSION, "connected": not self._connecting and not restoring and self.auth.connected, "preferences": self.preferences.copy(),
                    "raw_connected": not self._connecting and not restoring and self.raw_auth.connected,
                    "s3_connected": not self._connecting and not restoring and self.s3_auth.connected,
                    "earthdata_connected": not self._connecting and not restoring and self.earthdata_auth.connected,
                    "accounts": self.account_status(),
                    "links": OFFICIAL_LINKS, "aoi": self.last_aoi, "query": self.last_query, "scenes": list(self.scenes.values()),
                    "s5p_collections": public_collections(), "s5p_default": S5P_DEFAULT,
                    "sen3_collections": sen3_collections(), "sen3_default": SEN3_DEFAULT,
                    "gedi_collections": gedi_collections(), "gedi_default": GEDI_DEFAULT,
                    "batch_sources": batch_sources(), "batch": self.batch.snapshot(), "monthly": self.monthly.snapshot(),
                    "active_job": active.snapshot() if active else None,
                    "resumable_downloads": self.download_tasks.summaries(),
                    "update_repository": self.updates.repository}

    def active_job(self):
        with self._jobs_lock:
            return next((job for job in self.jobs.values() if job.is_active()), None)

    def _assert_idle(self):
        if self.active_job() or self._connecting or self.accounts.restoring:
            raise UserError("已有任务或认证请求正在运行，请等待完成或先取消。")

    def _reset_query(self):
        self.scenes = {}
        self.last_query = None
        self._query_geometry = None
        self.batch.clear()
        self.monthly.clear()

    def _request_aoi(self, value):
        if value.get("city_id") is not None:
            aoi = self.cities.get(value["city_id"])
            return normalize_aoi(aoi["geometry"]), aoi
        geometry = normalize_aoi(value.get("geometry"))
        with self.lock:
            previous = self.last_aoi
        if previous and geometry.equals(normalize_aoi(previous["geometry"])):
            return geometry, copy.deepcopy(previous)
        return geometry, describe_aoi(geometry, previous["name"] if previous else "研究区")

    @staticmethod
    def _output_directory(value):
        output = str(value.get("output_dir", "")).strip()
        if not output or not Path(output).expanduser().is_absolute():
            raise UserError("请选择完整的输出文件夹路径。")
        return output

    def _check_access(self, requires):
        connected = {"earthdata": self.earthdata_auth.connected, "sentinel-hub": self.auth.connected,
                     "odata": self.raw_auth.connected, "cdse-s3": self.s3_auth.connected}
        labels = {"earthdata": "NASA Earthdata", "sentinel-hub": "Sentinel Hub OAuth", "odata": "CDSE OData", "cdse-s3": "CDSE S3"}
        missing = [labels.get(key, key) for key in set(requires) if not connected.get(key, False)]
        if missing:
            raise UserError("请先连接 " + "、".join(sorted(missing)) + "；原下载选择和缓存已保留。")

    def account_status(self):
        return self.accounts.status()

    def _account_connect(self, provider, value):
        with self.lock:
            self._assert_idle()
            self._connecting = True
        try:
            result = self.accounts.connect(provider, value)
            if provider == "oauth":
                self.save_preferences({"client_id": result["client_id"]})
                with self.lock:
                    if self.last_query and self.last_query["profile"] == S2_PROCESS:
                        self._reset_query()
            return result
        finally:
            with self.lock:
                self._connecting = False

    def connect(self, value):
        return self._account_connect("oauth", value)

    def disconnect(self):
        with self.lock:
            self._assert_idle()
            result = self.accounts.disconnect("oauth")
            self.save_preferences({"client_id": ""})
            if self.last_query and self.last_query["profile"] == S2_PROCESS:
                self._reset_query()
            return result

    def raw_connect(self, value):
        return self._account_connect("odata", value)

    def raw_disconnect(self):
        with self.lock:
            self._assert_idle()
            return self.accounts.disconnect("odata")

    def s3_connect(self, value):
        return self._account_connect("s3", value)

    def s3_disconnect(self):
        with self.lock:
            self._assert_idle()
            return self.accounts.disconnect("s3")

    def earthdata_connect(self, value):
        return self._account_connect("earthdata", value)

    def earthdata_disconnect(self):
        with self.lock:
            self._assert_idle()
            return self.accounts.disconnect("earthdata")

    def clear_accounts(self, _value):
        with self.lock:
            self._assert_idle()
            status = self.accounts.clear()
            self.save_preferences({"client_id": ""})
            if self.last_query and self.last_query["profile"] == S2_PROCESS:
                self._reset_query()
            return status

    def collections(self, value):
        with self._collections_lock:
            now = time.monotonic()
            cached = self._collections_cache
            if not value.get("refresh") and cached and now - cached[0] < 900:
                return {"collections": cached[1]}
            listing = list_collections()
            self._collections_cache = (now, listing)
            return {"collections": listing}

    def aoi(self, value):
        result = None
        if value.get("clear") is not True:
            if value.get("city_id") is not None:
                result = self.cities.get(value["city_id"])
                geometry = normalize_aoi(result["geometry"])
            elif value.get("coordinates") is not None:
                geometry = parse_coordinates(value["coordinates"])
            elif value.get("path"):
                geometry = load_aoi(value["path"], value.get("source_crs") or None)
            else:
                geometry = normalize_aoi(value.get("geometry"), value.get("source_crs") or None)
            if result is None:
                result = describe_aoi(geometry, value.get("name") or (Path(value["path"]).stem if value.get("path") else "研究区"))
        with self.lock:
            self._assert_idle()
            self.last_aoi = result
            self._reset_query()
        return result

    def export_aoi(self, value):
        geometry, aoi = self._request_aoi(value)
        return {"path": save_aoi(geometry, value.get("path", ""), value.get("name") or aoi["name"])}

    def _task_progress(self, snapshot, *, force=False):
        try:
            self.download_tasks.update(snapshot, force=force)
        except (OSError, ValueError):
            # Download receipts and source caches still protect the completed files.
            job = self.jobs.get(snapshot["id"])
            if job:
                with job.lock:
                    job.data["detail"]["resume_record_error"] = "续作记录暂时无法保存；保持软件打开可重试。"

    def _start_job(self, kind, operation, *, definition=None, previous=None):
        with self.lock:
            self._assert_idle()
            with self._jobs_lock:
                if len(self.jobs) > 20:
                    for key in list(self.jobs)[:-10]:
                        if not self.jobs[key].is_active():
                            del self.jobs[key]
            job = Job(kind, self._task_progress if definition else None)
            if definition:
                self.download_tasks.create(job.id, kind, definition, previous=previous)
                job.definition = copy.deepcopy(definition)
                job.data.update(title=definition["title"], previous_id=previous, retry_available=False)
            with self._jobs_lock:
                self.jobs[job.id] = job

        def run():
            try:
                result = operation(job)
                with job.lock:
                    job.data.update(status=result.get("status", "completed") if kind in ("batch-download", "monthly-download") else "completed", result=result, progress=1.0)
                    if kind == "plan":
                        job.data["message"] = "下载计划已生成，可确认月份、覆盖范围和保存位置"
                    elif kind == "monthly-search":
                        groups = result["monthly"]["groups"]
                        failed = sum(group["status"] == "failed" for group in groups)
                        job.data["message"] = f"按月检索完成 · {len(groups)} 个月 · {sum(len(g['scenes']) for g in groups)} 个本月产品 · {failed} 个月检索失败"
                    elif kind == "monthly-download":
                        groups = result["months"]
                        completed = sum(group["status"] == "completed" for group in groups)
                        empty = sum(group["status"] in ("empty", "not_selected", "no_valid_pixels") for group in groups)
                        failed = sum(group["status"] in ("failed", "search_failed") for group in groups)
                        job.data["message"] = f"按月下载 · {completed} 个月完成 · {empty} 个月无匹配、未选择或无有效像元 · {failed} 个月需重试"
                    elif kind == "batch-search":
                        groups = result["batch"]["groups"]
                        job.data["message"] = f"联合检索完成 · {len(groups)} 个来源 · {sum(len(g['scenes']) for g in groups)} 个结果"
                    elif kind == "search":
                        unit = " 个影像区块" if result["query"]["profile"] == S2_PROCESS else " 个匹配项"
                        job.data["message"] = f"找到 {len(result['scenes'])}{unit}"
                    elif kind == "batch-download":
                        groups = result["sources"]
                        completed = sum(g["status"] == "completed" for g in groups)
                        failed = sum(g["status"] in ("failed", "partial", "search_failed", "dependency_failed") for g in groups)
                        empty = sum(g["status"] in ("empty", "not_selected") for g in groups)
                        job.data["message"] = f"联合下载 · {completed} 个来源完成 · {empty} 个来源无选择或无匹配 · {failed} 个来源需重试"
                    elif kind == "update":
                        job.data["message"] = ("安装程序已打开，请按提示完成更新。账号与设置会保留。"
                                               if result.get("installer_launched") else result["install_error"])
                    else:
                        skipped = sum(file["status"] == "skipped" for file in result["files"])
                        saved = len(result["files"]) - skipped
                        if result["plan"].get("kind") == "gedi":
                            stats = result["statistics"]
                            if "accepted_shots" in stats:
                                job.data["message"] = f"GEDI 导出完成 · {stats['accepted_shots']} 个足迹 · 新保存 {saved} 个文件 · 已有 {skipped} 个"
                            else:
                                job.data["message"] = f"GEDI 下载完成 · 新保存 {saved} 个文件 · 已有 {skipped} 个"
                        elif result["plan"].get("kind") == "original":
                            job.data["message"] = f"新保存 {saved} 个原始文件 · 完整性验证后跳过 {skipped} 个"
                        elif result["plan"].get("kind") == "mosaic":
                            dates = " / ".join(result.get("used_dates", []))
                            job.data["message"] = (f"区域拼接 · 新保存 {saved} 份 · 已有 {skipped} 份 · 实际来源日期 {dates} UTC"
                                                   if dates else "所选区块在研究区没有有效输出像元")
                        else:
                            job.data["message"] = f"新保存 {saved} 景 · 已有 {skipped} 景 · 无有效像元 {len(result['empty_scenes'])} 景"
            except Cancelled as error:
                with job.lock:
                    message = "已取消安装包下载" if kind == "update" else "任务已取消；已完成的文件已保留"
                    job.data.update(status="cancelled", message=message)
                    job.data["result"] = getattr(error, "download_result", None)
            except UserError as error:
                with job.lock:
                    job.data.update(status="failed", error=str(error), message=str(error), result=getattr(error, "download_result", None))
            except OSError as error:
                with job.lock:
                    diagnostic = io_diagnostic(error)
                    code = diagnostic.get("winerror", diagnostic.get("errno"))
                    suffix = f"（系统代码 {code}）" if code is not None else ""
                    job.data.update(status="failed", error=f"文件读写失败{suffix}，请检查磁盘空间、文件夹权限和文件占用。",
                                    message="文件读写失败", io_diagnostic=diagnostic, result=getattr(error, "download_result", None))
            except Exception as error:
                # Exception bodies can include network payloads; expose only type.
                with job.lock:
                    job.data.update(status="failed", error=f"任务遇到异常（{type(error).__name__}）。请调整参数后重试。", message="任务失败", result=getattr(error, "download_result", None))
            finally:
                with job.lock:
                    job.data["retry_available"] = bool(definition and job.data["status"] in INCOMPLETE)
                if definition:
                    self._task_progress(job.snapshot(), force=True)
        threading.Thread(target=run, name=f"{kind}-{job.id[:6]}", daemon=True).start()
        return {"job_id": job.id}

    def _search_profile(self, profile, geometry, criteria, *, cancel=None, progress=None):
        start, end = criteria["start"], criteria["end"]
        kwargs = {"cancel": cancel, "progress": progress}
        if profile == ORIGINAL:
            scenes = search_original(geometry, criteria["collection"], start, end, radar_mode=criteria["radar_mode"], **kwargs)
        elif profile == S2_ZIP:
            scenes = search_zip(geometry, start, end, criteria["max_cloud"], **kwargs)
        elif profile == GEDI:
            scenes = search_gedi(geometry, criteria["collection"], start, end, **kwargs)
        elif profile in DEM:
            scenes = search_dem(geometry, profile, **kwargs)
        elif profile == S1_PUBLIC:
            scenes = search_public_radar(geometry, dt.date.fromisoformat(start), dt.date.fromisoformat(end) + dt.timedelta(days=1),
                                         criteria["radar_mode"], **kwargs)
        elif profile == S1_SLC:
            scenes = search_slc(geometry, start, end, **kwargs)
        elif profile == S1_RTC:
            scenes = search_rtc(geometry, dt.date.fromisoformat(start), dt.date.fromisoformat(end) + dt.timedelta(days=1),
                                criteria["radar_mode"], **kwargs)
        elif profile == SEN3_PUBLIC:
            scenes = search_sen3(geometry, criteria["collection"], start, end, **kwargs)
        elif profile == S5P_PUBLIC:
            scenes = search_s5p(geometry, criteria["collection"], start, end, **kwargs)
        elif profile in WORLD_COVER:
            scenes = search_worldcover(geometry, profile, start, end, **kwargs)
        elif profile in MODIS:
            scenes = search_modis(geometry, profile, start, end, **kwargs)
        elif profile in (S2_L1C_OPEN, LANDSAT_OPEN):
            scenes = search_open_native(profile, geometry, dt.date.fromisoformat(start), dt.date.fromisoformat(end) + dt.timedelta(days=1),
                                        criteria["max_cloud"], **kwargs)
        elif profile == S2_L3_PUBLIC:
            scenes = search_l3(geometry, start, end, **kwargs)
        elif profile == S2_L2A_COG:
            scenes = search_l2a(geometry, dt.date.fromisoformat(start), dt.date.fromisoformat(end) + dt.timedelta(days=1),
                                criteria["max_cloud"], **kwargs)
        elif profile == S2_PUBLIC:
            scenes = search_earth(geometry, dt.date.fromisoformat(start), dt.date.fromisoformat(end) + dt.timedelta(days=1),
                                  criteria["max_cloud"], **kwargs)
        elif profile == S2_NATIVE:
            scenes = search_native(geometry, start, end, criteria["max_cloud"], self.s3_auth,
                                   self.config_dir / "native-catalogue", **kwargs)
        else:
            scenes = find_scenes(geometry, start, end, criteria["max_cloud"], self.auth, **kwargs)
            scenes = split_process_scenes(geometry, scenes)
        return scenes

    def batch_search(self, value):
        return self.batch.search(value)

    def batch_plan(self, value):
        return self.batch.plan(value)

    def batch_download(self, value):
        return self.batch.download(value)

    def monthly_search(self, value):
        return self.monthly.search(value)

    def monthly_plan(self, value, *, cancel=None, progress=None):
        return self.monthly.prepare(value, cancel=cancel, progress=progress)

    def monthly_download(self, value):
        return self.monthly.download(value)

    def prepare_plan(self, value):
        # Register the job before any full-city grid/intersection work, so it is
        # observable and cancellable even while the download preview is prepared.
        frozen = copy.deepcopy(value)
        def operation(job):
            job.update("正在检查完整研究区和所选产品", 0)
            check_cancel(job.cancel_event)
            if frozen.get("monthly_id"):
                return self.monthly_plan(frozen, cancel=job.cancel_event, progress=job.update)
            if frozen.get("batch_id"):
                return self.batch.plan(frozen, cancel=job.cancel_event)
            return self.plan(frozen, cancel=job.cancel_event)
        return self._start_job("plan", operation)

    def search(self, value):
        geometry, aoi = self._request_aoi(value)
        profile = _profile(value)
        criteria = _criteria(value, profile)
        start, end = criteria["start"], criteria["end"]

        def operation(job):
            kwargs = {"cancel": job.cancel_event, "progress": job.update}
            job.update("连接影像目录", 0.0, {"search_profile": profile, "criteria": criteria})
            scenes = self._search_profile(profile, geometry, criteria, **kwargs)
            query = {"id": job.id, "profile": profile, **criteria}
            with self.lock:
                check_cancel(job.cancel_event)
                self.scenes = {scene["key"]: scene for scene in scenes}
                self.last_query = query
                self._query_geometry = geometry
            return {"scenes": scenes, "query": query}

        with self.lock:
            self._assert_idle()
            if profile == S2_PROCESS and not self.auth.connected:
                raise UserError("请先连接 Sentinel Hub OAuth 客户端凭据。")
            self.last_aoi = aoi
            self._reset_query()
            return self._start_job("search", operation)

    def _selection_options(self, profile, query, supplied, scenes):
        keys = [scene["key"] for scene in scenes]
        if profile in WORLD_COVER:
            unavailable = next((scene["raw"] for scene in scenes if not scene["raw"]["available"]), None)
            if unavailable:
                raise UserError("所选 ESA WorldCover 年度瓦片不可下载：" + unavailable["unavailable_reason"] + "。不会换用其他产品。")
        if profile == GEDI:
            options = gedi_options(supplied, query["collection"])
        elif profile == S2_ZIP and supplied.get("mode", "original") == "original":
            options = {"collection": "sentinel-2-l2a", "mode": "original", "transport": "odata", "archive_kind": "cdse-safe-zip",
                       **zip_download_options(supplied)}
        elif profile == ORIGINAL or profile in NATIVE_OPEN:
            modes = ("original", "metadata", "asset", "assets") if profile in (ORIGINAL, SEN3_PUBLIC) or profile in MODIS else (
                ("assets", "asset") if profile in (S2_L3_PUBLIC, S2_ANNUAL) else
                ("original",) if profile in (S5P_PUBLIC, S1_SLC) or profile in WORLD_COVER else ("metadata", "asset", "assets"))
            mode = supplied.get("mode", modes[0])
            if mode not in modes:
                raise UserError("该来源不支持所选下载内容；此公开来源只提供目录中列出的原始文件。")
            asset = supplied.get("asset")
            if mode == "asset" and (not isinstance(asset, str) or not asset or len(keys) != 1):
                raise UserError("下载指定原始文件时，请只选一个产品和一个文件。")
            if mode == "assets" and len(keys) != 1:
                raise UserError("同时下载多个原始文件时，请先单选一个产品。")
            transport = supplied.get("transport", "auto")
            if profile == ORIGINAL:
                collection, allowed = query["collection"], ("auto", "odata", "cdse-s3")
            else:
                collection, only = NATIVE_OPEN[profile]
                if profile in (S5P_PUBLIC, SEN3_PUBLIC):
                    collection = query["collection"]
                allowed = ("auto", only)
            if transport not in allowed:
                raise UserError("下载通道与所选目录不匹配。")
            options = {"collection": collection, "mode": mode, "asset": asset if mode == "asset" else None,
                       "transport": transport if profile == ORIGINAL else only}
            if mode == "assets":
                options["assets"] = supplied.get("assets")
        elif profile in DEM:
            mode = supplied.get("mode", "dem")
            if mode == "original":
                options = {"collection": f"cop-dem-glo-{DEM[profile][0]}", "mode": mode, "transport": "public"}
            elif mode == "dem":
                options = {"profile": profile, "mode": "dem", "label": "数字表面模型 DSM（米）", "resolution": int(DEM[profile][0]),
                           "bands": ["DSM"], "output_bands": ["DSM"], "dtype": "float32", "nodata": -9999.0, "count": 1, "mask_clouds": False}
            else:
                raise UserError("选择按研究区裁剪 DEM GeoTIFF，或下载完整的原始 DEM 瓦片。")
        elif profile == S1_RTC:
            options = rtc_options(supplied)
        else:
            options = normalize_options(supplied)
            if options["max_cloud"] != query["max_cloud"]:
                raise UserError("云量上限已变化，请重新检索。")
            if profile in (S2_PUBLIC, S2_NATIVE, S2_ZIP):
                options["profile"] = profile
            if profile == S2_ZIP:
                options.update(zip_download_options(supplied))
                if options["layout"] != "mosaic":
                    raise UserError("整景 ZIP 的本地处理输出一份拼接与裁剪影像；逐景保存请使用完整 ZIP 模式。")
                options["monthly_pipeline"] = "download-scenes-mosaic-clip"
                options.update(selection_start=query["start"], selection_end=query["end"])
                if any(not scene["raw"]["available"] for scene in scenes):
                    raise UserError("所选产品没有可用的官方整景 ZIP，无法开始本地处理。")
            if profile == S2_NATIVE:
                if options["layout"] != "mosaic":
                    raise UserError("官方 S3 区域读取采用单份拼接；逐产品时间序列请使用现有区域通道。")
                if any(not scene["native"]["available"] for scene in scenes):
                    raise UserError("所选产品缺少官方 S3 研究原件，无法完整拼接。")
        return options

    def _selection(self, value, *, coverage=False):
        geometry, _ = self._request_aoi(value)
        profile = _profile(value)
        supplied = value.get("options", {})
        if not isinstance(supplied, dict):
            raise UserError("导出选项无效。")
        keys = value.get("scene_keys")
        if not isinstance(keys, list) or not (0 if coverage else 1) <= len(keys) <= 500 or any(not isinstance(key, str) for key in keys):
            raise UserError("每批请选择 1～500 个结果。")
        if len(keys) != len(set(keys)):
            raise UserError("影像选择中有重复项。")
        with self.lock:
            query = self.last_query
            if not query or query["profile"] != profile or value.get("query_id") != query["id"] or any(key not in self.scenes for key in keys):
                raise UserError("影像来源、类别或检索结果已变化，请重新检索并选择。")
            if not geometry.equals(self._query_geometry):
                raise UserError("研究区已变化，请重新检索。")
            if profile != S2_PROCESS or "criteria" in value:
                criteria = value.get("criteria")
                if not isinstance(criteria, dict) or any(query.get(k) != v for k, v in _criteria(criteria, profile).items()):
                    raise UserError("类别、日期或筛选条件已变化，请重新检索。")
            scenes = [self.scenes[key] for key in keys]
            options = {"priority": priority_option(supplied)} if coverage else self._selection_options(profile, query, supplied, scenes)
            return profile, geometry, options, [self.scenes[key] for key in keys]

    def coverage(self, value):
        if value.get("monthly_id"):
            return self.monthly.coverage(value)
        if value.get("batch_id") and value.get("group_id", "primary") != "primary":
            return self.batch.coverage(value)
        _, geometry, options, scenes = self._selection(value, coverage=True)
        with self.lock:
            candidates = list(self.scenes.values())
        return coverage_summary(geometry, candidates, [s["key"] for s in scenes], options["priority"])

    @staticmethod
    def _source_plan_key(profile, geometry, options, scenes, criteria):
        return hashlib.sha256(json.dumps({"profile": profile, "geometry": mapping(normalize(geometry)), "options": options,
                                          "scenes": scenes, "criteria": criteria if profile == GEDI else None}, sort_keys=True, separators=(",", ":"),
                                         allow_nan=False).encode("utf-8")).hexdigest()

    def _planned_source(self, profile, geometry, options, scenes, criteria, *, cancel=None):
        check_cancel(cancel)
        key = self._source_plan_key(profile, geometry, options, scenes, criteria)
        with self._plans_lock:
            if key in self._source_plans and profile != GEDI:
                self._source_plans.move_to_end(key)
                return self._source_plans[key]
        if profile == GEDI:
            return build_gedi_plan(geometry, scenes, options, criteria)
        if options.get("monthly_pipeline") == "download-scenes-mosaic-clip":
            from .monthly_scenes import build_monthly_scene_plan
            planned = build_monthly_scene_plan(geometry, scenes, options, cancel=cancel)
        elif profile == S2_ZIP:
            planned = build_zip_plan(geometry, scenes, options)
        elif "transport" in options:
            planned = raw_plan(geometry, scenes, options)
        elif options.get("layout") == "mosaic":
            planned = build_mosaic_plan(geometry, scenes, options, cancel=cancel)
        elif profile != S2_PROCESS:
            planned = public_plan(geometry, scenes, options, cancel=cancel)
        else:
            planned = build_plan(geometry, options, len(scenes), cancel=cancel)
            planned[0].update(download_scope="all-selected-source-blocks", execution_order=["download_sources", "process_local"])
        check_cancel(cancel)
        with self._plans_lock:
            self._source_plans[key] = planned
            self._source_plans.move_to_end(key)
            while len(self._source_plans) > 6:
                self._source_plans.popitem(last=False)
        return planned

    def _plan_source(self, profile, geometry, options, scenes, criteria, *, cancel=None):
        planned = self._planned_source(profile, geometry, options, scenes, criteria, cancel=cancel)
        return planned[0] if isinstance(planned, tuple) else planned

    def _run_source(self, profile, geometry, options, scenes, plan, output_dir, *, cancel=None, progress=None,
                    download_only=False, staged=None, download_slots=None):
        planned = None
        if profile != GEDI and "transport" not in options:
            planned = self._planned_source(profile, geometry, options, scenes, None, cancel=cancel)
            compatible = {planned[0]["fingerprint"]}
            if profile == S2_ZIP and not any(key in options for key in ("download_engine", "download_workers")):
                compatible.add(planned[0].get("storage_fingerprint"))
            if plan.get("kind") != "pending" and plan["fingerprint"] not in compatible:
                raise UserError("原下载计划与当前软件的区域网格或产品身份不一致；请重新选择任务，已完成原件仍可复用。")
        if options.get("monthly_pipeline") == "download-scenes-mosaic-clip":
            from .monthly_scenes import download_monthly_scenes
            return download_monthly_scenes(geometry, scenes, options, output_dir, auth=self.auth,
                                           s3_auth=self.s3_auth, raw_auth=self.raw_auth,
                                           cancel=cancel, progress=progress, planned=planned, browser=self.browser_download,
                                           download_only=download_only, staged=staged, download_slots=download_slots)
        if profile == S2_NATIVE:
            return download_native_mosaic(geometry, scenes, options, output_dir, self.s3_auth, cancel=cancel, progress=progress, planned=planned)
        if profile == GEDI:
            return download_gedi(geometry, plan, output_dir, self.earthdata_auth, cancel=cancel, progress=progress)
        if "transport" in options:
            if profile == SEN3_PUBLIC:
                return download_sen3(geometry, plan, output_dir, cancel=cancel, progress=progress)
            return download_original(geometry, plan, output_dir, self.raw_auth, s3_auth=self.s3_auth,
                                     cancel=cancel, progress=progress, browser=self.browser_download,
                                     download_slots=download_slots)
        if profile == S2_PROCESS:
            return download_scenes(geometry, scenes, options, output_dir, self.auth, cancel=cancel, progress=progress, planned=planned)
        return download_public(geometry, scenes, options, output_dir, cancel=cancel, progress=progress, planned=planned)

    def plan(self, value, *, cancel=None):
        profile, geometry, options, scenes = self._selection(value)
        return self._plan_source(profile, geometry, options, scenes, self.last_query, cancel=cancel)

    def download(self, value):
        output_dir = self._output_directory(value)
        with self.lock:
            self._assert_idle()
            profile, geometry, options, scenes = self._selection(value)
            criteria = copy.deepcopy(self.last_query)
            scenes = copy.deepcopy(scenes)
            key = self._source_plan_key(profile, geometry, options, scenes, criteria)
            with self._plans_lock:
                cached = self._source_plans.get(key)
            if profile == GEDI or "transport" in options:
                plan = self._plan_source(profile, geometry, options, scenes, criteria)
            elif cached is not None:
                plan = cached[0]
            else:
                plan = {"kind": "pending", "fingerprint": None, "requires": []}
            if value.get("plan_fingerprint") is not None and value["plan_fingerprint"] != plan["fingerprint"]:
                raise UserError("参数、拼接区块或优先级已变化，请重新确认下载计划。")
            requires = list(plan.get("requires", [])) + (["sentinel-hub"] if profile == S2_PROCESS else ["cdse-s3"] if profile == S2_NATIVE else ["odata"] if profile == S2_ZIP else ["earthdata"] if profile == GEDI else [])
            self._check_access(requires)
            plan = {**plan, "requires": sorted(set(requires))}
            self.save_preferences({"output_dir": output_dir})
            definition = {"title": f"{SOURCE_INFO.get(profile, {}).get('title', '影像下载')} · {len(scenes)} 个产品", "profile": profile, "geometry": mapping(geometry),
                          "options": options, "scenes": scenes, "criteria": criteria, "region": (self.last_aoi or {}).get("region"),
                          "plan": plan, "output_dir": output_dir}

            def operation(job):
                actual_plan = plan
                if plan.get("kind") == "pending":
                    job.update("规划完整研究区；可随时取消", 0)
                    actual_plan = self._plan_source(profile, geometry, options, scenes, criteria, cancel=job.cancel_event)
                    actual_plan = {**actual_plan, "requires": sorted(set(requires))}
                    job.definition["plan"] = copy.deepcopy(actual_plan)
                    self.download_tasks.revise(job.id, job.definition)
                return self._run_source(profile, geometry, options, scenes, actual_plan, output_dir,
                                        cancel=job.cancel_event, progress=job.update)
            return self._start_job("download", operation, definition=definition)

    def retry_download(self, value):
        """Resume the frozen original selection, using current accounts and verified local caches."""
        identifier = value.get("job_id")
        with self.lock:
            self._assert_idle()
            old = self.jobs.get(identifier)
            if old is not None and hasattr(old, "definition") and old.snapshot()["status"] in INCOMPLETE:
                self._task_progress(old.snapshot(), force=True)
            kind, definition = self.download_tasks.load(identifier)
            geometry = normalize_aoi(definition["geometry"])
            plan = definition["plan"]
            requires = set(plan.get("requires", []))
            if kind == "download" and definition["profile"] == S2_PROCESS:
                requires.add("sentinel-hub")
            connected = {"earthdata": self.earthdata_auth.connected, "sentinel-hub": self.auth.connected,
                         "odata": self.raw_auth.connected, "cdse-s3": self.s3_auth.connected}
            labels = {"earthdata": "NASA Earthdata", "sentinel-hub": "Sentinel Hub OAuth", "odata": "CDSE OData", "cdse-s3": "CDSE S3"}
            missing = [labels.get(key, key) for key in requires if not connected.get(key, False)]
            if missing:
                raise UserError("请先连接 " + "、".join(missing) + "；原下载选择和缓存已保留。")
            if kind == "monthly-download":
                operation = lambda job: self.monthly.run(job, definition)
            elif kind == "batch-download":
                operation = lambda job: self.batch._download(job, geometry, definition["groups"], definition["reference"],
                                                            plan, Path(definition["output_dir"]))
            else:
                operation = lambda job: self._run_source(definition["profile"], geometry, definition["options"],
                                                         definition["scenes"], plan, definition["output_dir"],
                                                         cancel=job.cancel_event, progress=job.update)
            started = self._start_job(kind, operation, definition=definition, previous=identifier)
            if old is not None:
                with old.lock:
                    old.data.update(replaced_by=started["job_id"], retry_available=False)
            return started

    def job(self, job_id):
        with self._jobs_lock:
            job = self.jobs.get(job_id)
            if job is None:
                raise UserError("任务不存在，可能已过期。")
        return job.snapshot()

    def cancel(self, job_id):
        with self._jobs_lock:
            job = self.jobs.get(job_id)
            if job is None:
                raise UserError("任务不存在。")
        with job.lock:
            if job.data["status"] in ("running", "cancelling"):
                job.cancel_event.set()
                job.data.update(status="cancelling", message="正在取消，保留已完成文件和有效下载进度…")
        return job.snapshot()

    def abandon_download(self, value):
        identifier = value.get("job_id")
        with self._jobs_lock:
            old = self.jobs.get(identifier)
            if old is not None and old.is_active():
                raise UserError("该任务正在运行，请先点击取消任务，结束后再移除。")
            result = self.download_tasks.abandon(identifier)
            if old is not None:
                with old.lock:
                    old.data.update(status="abandoned", retry_available=False)
            return {**result, "tasks": self.download_tasks.summaries()}

    def geocode(self, value):
        query = str(value.get("query", "")).strip()
        if not query or len(query) > 200:
            raise UserError("请输入地点名称，最多 200 个字符。")
        local = self.cities.search(query, limit=5)
        if local:
            return {"places": [{"name": row["name"]+" · 完整行政边界", "city_id": row["id"],
                                "lat": row["center"][1], "lon": row["center"][0],
                                "bounds": [row["bounds"][1], row["bounds"][3], row["bounds"][0], row["bounds"][2]]}
                               for row in local]}
        with self._geocode_lock:
            if query in self._geocode_cache:
                return {"places": self._geocode_cache[query]}
            elapsed = time.monotonic() - self._last_geocode
            if elapsed < 1.1:
                time.sleep(1.1 - elapsed)
            self._last_geocode = time.monotonic()
            http = HttpClient()
            try:
                response = http.request("GET", "https://nominatim.openstreetmap.org/search", params={"q": query, "format": "jsonv2", "limit": 5}, headers={"Accept-Language": "zh-CN,zh;q=0.9,en;q=0.5"}, timeout=(8, 20))
                if response.status_code != 200:
                    raise UserError("地点搜索暂不可用，可直接缩放地图或输入经纬度。")
                data = response.json()
                places = [{"name": place["display_name"], "lat": float(place["lat"]), "lon": float(place["lon"]), "bounds": [float(v) for v in place["boundingbox"]]} for place in data]
                self._geocode_cache[query] = places
                return {"places": places}
            except (ValueError, KeyError, TypeError):
                raise UserError("地点搜索没有返回有效结果，可改用经纬度。") from None
            finally:
                http.close()

    def check_update(self, _value):
        status = {"current_version": VERSION}
        if not self.updates.repository:
            return {**status, "status": "unconfigured"}
        if not self._update_check_lock.acquire(blocking=False):
            raise UserError("正在检查更新，请稍等。")
        try:
            with self.lock:
                self._update_release = None
            release = self.updates.latest()
            if release is None:
                return {**status, "status": "not_published"}
            latest_key, current_key = version_key(release.version), version_key(VERSION)
            if latest_key <= current_key:
                return {**status, "status": "current", "latest_version": release.version, "ahead": latest_key < current_key}
            with self.lock:
                self._update_release = release
            return {**status, "status": "available", "latest_version": release.version, "tag": release.tag,
                    "installer_bytes": release.installer.size if release.installer else None}
        finally:
            self._update_check_lock.release()

    def _checked_release(self, value):
        with self.lock:
            release = self._update_release
            if release is None or value.get("tag") != release.tag:
                raise UserError("请重新检查更新，再选择要下载的版本。")
            return release

    def open_update_release(self, value):
        if not self.updates.repository:
            raise UserError("此构建未配置 GitHub 更新渠道。")
        release = self._checked_release(value) if value.get("tag") else None
        if not webbrowser.open(self.updates.release_page(release)):
            raise UserError("无法打开默认浏览器，请检查系统浏览器设置。")
        return {"opened": True}

    def download_update(self, value):
        release = self._checked_release(value)
        if release.installer is None:
            raise UserError("此版本没有可校验的 Windows x64 安装包。")

        def operation(job):
            path = self.updates.download(release.installer, self.config_dir / "updates",
                                         cancel=job.cancel_event, progress=job.update)
            with self.lock:
                self._downloaded_update = release
            return self._install_update_result(release, path, cancel=job.cancel_event)

        return self._start_job("update", operation)

    def _install_update_result(self, release, path, *, cancel=None):
        result = {"tag": release.tag, "folder": str(path.parent), "files": [{"path": str(path), "status": "saved"}],
                  "installer_ready": True, "installer_launched": False}
        try:
            self.updates.install(release.installer, path, cancel=cancel)
            result["installer_launched"] = True
        except InstallerLaunchError as error:
            result["install_error"] = str(error)
        return result

    def install_update(self, value):
        with self.lock:
            self._assert_idle()
            release = self._downloaded_update
            if release is None or value.get("tag") != release.tag:
                raise UserError("请先下载并校验此版本的安装包，再开始安装。")
            path = self.config_dir / "updates" / release.installer.name
            return self._install_update_result(release, path)

    def open_link(self, value):
        name = value.get("name")
        if name not in OFFICIAL_LINKS:
            raise UserError("未知链接。")
        webbrowser.open(OFFICIAL_LINKS[name])
        return {"opened": True}

    def open_folder(self, value):
        path = io_path(value.get("path") or self.preferences["output_dir"])
        if not path.is_absolute() or not path.is_dir():
            raise UserError("保存文件夹尚不存在。")
        if os.name == "nt":
            os.startfile(display_path(path))
        else:
            webbrowser.open(path.as_uri())
        return {"opened": True}

    def shutdown(self):
        self.accounts.stop()
        with self._jobs_lock:
            for job in self.jobs.values():
                job.cancel_event.set()
        self.auth.clear()
        self.raw_auth.clear()
        self.s3_auth.clear()
        self.earthdata_auth.clear()
