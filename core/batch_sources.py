"""User-facing source choices for a shared AOI/date download batch."""
from __future__ import annotations

import copy

from .gedi_catalogue import DEFAULT_COLLECTION as GEDI_DEFAULT, public_collections as gedi_collections
from .open_s5p import DEFAULT_COLLECTION as S5P_DEFAULT, public_collections as s5p_collections
from .open_sen3 import DEFAULT_COLLECTION as SEN3_DEFAULT, public_collections as sen3_collections


def _source(profile, title, group, kind, time_note, *, auth=None, modes=None, assets=None, options=None):
    return {"id": profile, "title": title, "group": group, "kind": kind, "time_note": time_note,
            "auth": auth, "modes": modes or [], "default_assets": assets or [], "default_options": options or {}}


_ACQUISITION = "按相同 UTC 日期范围检索采集数据；不保证各卫星同一时刻过境。"
_OPTICAL = {"mode": "research", "resolution": 10, "max_cloud": 20, "mask_clouds": True}
SOURCES = [
    _source("gedi-earthdata", "NASA GEDI · 高度 / 森林结构 / 生物量", "激光与森林参数", "gedi",
            "足迹按相同 UTC 日期范围筛选；L3/L4B 按整个汇总期重叠匹配。", auth="earthdata",
            options={"collection": GEDI_DEFAULT, "mode": "aligned", "quality": "recommended", "nondegraded": True,
                     "min_sensitivity": 0, "resolution": 10}),
    _source("s2-earth-search", "Sentinel-2 L2A · 公开区域裁剪", "光学影像", "optical", _ACQUISITION, options=_OPTICAL),
    _source("s2-process", "Sentinel-2 L2A · Sentinel Hub 区域裁剪", "光学影像", "optical", _ACQUISITION,
            auth="sentinel-hub", options=_OPTICAL),
    _source("s2-l2a-cog", "Sentinel-2 L2A · 原始整瓦片 COG", "光学影像", "native", _ACQUISITION,
            modes=["assets", "metadata"], assets=["B04", "B03", "B02", "B08"], options={"mode": "assets", "max_cloud": 20}),
    _source("s2-l1c-aws", "Sentinel-2 L1C · 原始 JP2", "光学影像", "native", _ACQUISITION,
            modes=["assets", "metadata"], assets=["B04", "B03", "B02", "B08"], options={"mode": "assets", "max_cloud": 20}),
    _source("landsat-pc-l2", "Landsat C2 L2 · 原始波段", "光学影像", "native", _ACQUISITION,
            modes=["assets", "metadata"], assets=["red", "green", "blue", "nir08"], options={"mode": "assets", "max_cloud": 20}),
    _source("s2-l3-quarterly", "Sentinel-2 L3 · 季度镶嵌", "光学影像", "native",
            "相同日期范围与季度有重叠即匹配；保留完整季度产品。", modes=["assets"],
            assets=["B04", "B03", "B02", "B08"], options={"mode": "assets"}),
    _source("s1-rtc-pc", "Sentinel-1 RTC · 雷达区域裁剪", "雷达影像", "rtc", _ACQUISITION,
            options={"mode": "rtc", "resolution": 10, "bands": ["VV"], "radar_mode": ""}),
    _source("s1-aws-original", "Sentinel-1 GRD · 公开原始文件", "雷达影像", "native", _ACQUISITION,
            modes=["assets", "metadata"], options={"mode": "assets", "radar_mode": ""}),
    _source("s1-slc-seasia", "Sentinel-1 SLC · 东南亚历史 SAFE ZIP", "雷达影像", "native",
            "按相同 UTC 采集日期筛选；历史存档截至 2024-04-04，单次最多检索 62 天。",
            modes=["original"], options={"mode": "original"}),
    _source("sen3-aws-original", "Sentinel-3 · SEN3 / NetCDF", "海洋与大气", "native", _ACQUISITION,
            modes=["original", "assets", "metadata"], options={"mode": "original", "collection": SEN3_DEFAULT}),
    _source("s5p-aws-l2", "Sentinel-5P · 大气 L2 NetCDF", "海洋与大气", "native", _ACQUISITION,
            modes=["original"], options={"mode": "original", "collection": S5P_DEFAULT}),
    _source("worldcover-s2", "ESA WorldCover · Sentinel-2 年度合成", "年度产品", "native",
            "相同日期范围与 2020/2021 年度重叠即匹配；保留完整年度产品。", modes=["assets"],
            assets=["S2RGBNIR", "NDVI", "SWIR"], options={"mode": "assets"}),
    _source("worldcover-s1", "ESA WorldCover · Sentinel-1 年度 Gamma0", "年度产品", "native",
            "相同日期范围与 2020/2021 年度重叠即匹配；保留完整年度产品。", modes=["original"], options={"mode": "original"}),
    _source("worldcover-map", "ESA WorldCover · 土地覆盖分类", "年度产品", "native",
            "相同日期范围与 2020/2021 年度重叠即匹配；保留完整年度分类。", modes=["original"], options={"mode": "original"}),
    *[_source(profile, title, "MODIS", "native", "按相同 UTC 标称日筛选合成产品；观测窗口另见下载记录。",
              modes=["assets", "original", "metadata"], assets=assets, options={"mode": "assets"})
      for profile, title, assets in [
          ("modis-13Q1-061", "MODIS · NDVI / EVI · 13Q1", ["250m_16_days_NDVI", "250m_16_days_EVI"]),
          ("modis-09Q1-061", "MODIS · 红光 / 近红外 · 09Q1", ["sur_refl_b01", "sur_refl_b02"]),
          ("modis-11A2-061", "MODIS · 昼夜地表温度 · 11A2", ["LST_Day_1km", "LST_Night_1km"]),
          ("modis-15A2H-061", "MODIS · LAI / FPAR · 15A2H", ["Lai_500m", "Fpar_500m"]),
          ("modis-43A4-061", "MODIS · NBAR · 43A4", ["Nadir_Reflectance_Band1", "Nadir_Reflectance_Band2"]),
      ]],
    _source("dem-30", "Copernicus DEM · 30 米 DSM", "高程", "dem", "静态 DSM；仅共用研究区，不表示所选日期的高程。",
            modes=["dem", "original"], options={"mode": "dem"}),
    _source("dem-90", "Copernicus DEM · 90 米 DSM", "高程", "dem", "静态 DSM；仅共用研究区，不表示所选日期的高程。",
            modes=["dem", "original"], options={"mode": "dem"}),
    _source("cdse-original", "Copernicus 官方目录 · 原始产品", "官方原始产品", "native", _ACQUISITION,
            auth="cdse", modes=["original", "assets", "metadata"],
            options={"mode": "original", "collection": "sentinel-2-l2a", "transport": "auto", "radar_mode": ""}),
]
SOURCE_INFO = {item["id"]: item for item in SOURCES}


def public_sources():
    sources = copy.deepcopy(SOURCES)
    for item in sources:
        if item["id"] == "gedi-earthdata":
            item["collections"] = gedi_collections()
        elif item["id"] == "s5p-aws-l2":
            item["collections"] = s5p_collections()
        elif item["id"] == "sen3-aws-original":
            item["collections"] = sen3_collections()
    return sources
