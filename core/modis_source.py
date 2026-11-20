"""Exact MODIS C6.1 archive identities, layer contracts and unsigned object addresses."""
from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass
from urllib.parse import urlsplit

ACCOUNT = "modiseuwest"
HOST = ACCOUNT + ".blob.core.windows.net"
COGS = "modis-061-cogs"
SOURCE = "modis-061"
FAMILIES = {"13Q1": (4800, 16), "09Q1": (4800, 8), "11A2": (1200, 8), "15A2H": (2400, 8), "43A4": (2400, 8)}
COLLECTIONS = {f"modis-{family}-061": family for family in FAMILIES}
PREFIXES = {"MOD": "Terra", "MYD": "Aqua", "MCD": "Terra+Aqua"}
GRANULE = re.compile(r"((MOD|MYD|MCD)(13Q1|09Q1|11A2|15A2H|43A4))\.A(20[0-9]{2})([0-9]{3})"
                     r"\.h([0-9]{2})v([0-9]{2})\.061\.(20[0-9]{11})", re.ASCII)


@dataclass(frozen=True)
class Layer:
    description: str
    dtype: str
    nodata: int
    gain: float = 1.0
    offset: float = 0.0
    tiff_gain: float | None = None
    units: str | None = None
    notice: str | None = None

    @property
    def stored_gain(self):
        return self.gain if self.tiff_gain is None else self.tiff_gain


VI = "250m_16_days_"
LAYERS = {
    "13Q1": {
        **{VI+k: Layer("250m 16 days "+k.replace("_", " "), "int16", -3000 if k in ("NDVI", "EVI") else -1000,
                       .0001, tiff_gain=10000, units="dimensionless")
           for k in ("NDVI", "EVI", "red_reflectance", "NIR_reflectance", "blue_reflectance", "MIR_reflectance")},
        "250m_16_days_VI_Quality": Layer("250m 16 days VI Quality", "uint16", 65535),
        **{VI+k: Layer("250m 16 days "+k.replace("_", " "), "int16", -4000 if k == "relative_azimuth_angle" else -10000,
                       .01, tiff_gain=100, units="degrees")
           for k in ("view_zenith_angle", "sun_zenith_angle", "relative_azimuth_angle")},
        VI+"composite_day_of_the_year": Layer("250m 16 days composite day of the year", "int16", -1, units="day_of_year"),
        VI+"pixel_reliability": Layer("250m 16 days pixel reliability", "uint8", 255,
                                      notice="COG 255 (uint8) represents HDF4 -1 (int8) fill; valid ranks are unchanged."),
    },
    "09Q1": {
        **{f"sur_refl_b0{i}": Layer(f"Surface_reflectance_for_band_{i}", "int16", -28672, .0001, units="reflectance") for i in (1, 2)},
        "sur_refl_qc_250m": Layer("Surface_reflectance_250m_quality_control_flags", "uint16", 65535),
        "sur_refl_state_250m": Layer("Surface_reflectance_250m_state_flags", "uint16", 65535),
    },
    "11A2": {
        **{f"LST_{k}_1km": Layer(f"8-day {k.lower()}time 1km grid Land-surface Temperature", "uint16", 0, .02, units="K") for k in ("Day", "Night")},
        **{f"QC_{k}": Layer(f"Quality control for {k.lower()}time LST and emissivity", "uint8", 0,
                            notice="Raw QA value 0 can be valid despite TIFF NoData=0. Read the unmasked data and consult corresponding LST.")
           for k in ("Day", "Night")},
        **{f"{k}_view_time": Layer(f"Average time of {k.lower()}time Land-surface Temperature observation", "uint8", 255, .1, units="hours")
           for k in ("Day", "Night")},
        **{f"{k}_view_angl": Layer(f"Average view zenith angle of {k.lower()}time Land-surface Temperature", "uint8", 255, offset=-65, units="degrees")
           for k in ("Day", "Night")},
        **{f"Emis_{i}": Layer(f"Band {i} emissivity", "uint8", 0, .002, .49, units="fraction") for i in (31, 32)},
        **{f"Clear_sky_{k}": Layer(f"the {k} in clear-sky conditions and with validate LSTs", "uint8", 0,
                                   notice="Read raw clear-sky bit flags; zero represents no clear observations.") for k in ("days", "nights")},
    },
    "15A2H": {
        "Lai_500m": Layer("Leaf Area Index LAI (8-day composite)", "uint8", 255, .1, units="m²/m²"),
        "Fpar_500m": Layer("FPAR (8-day composite)", "uint8", 255, .01, units="fraction"),
        "LaiStdDev_500m": Layer("Standard Deviation Leaf Area Index (8-day composite)", "uint8", 255, .1, units="m²/m²"),
        "FparStdDev_500m": Layer("Standard Deviation FPAR (8-day composite)", "uint8", 255, .01, units="fraction"),
        "FparLai_QC": Layer("QC for FPAR and LAI (8-day composite)", "uint8", 255),
        "FparExtra_QC": Layer("pass-through QC for FPAR and LAI (8-day composite)", "uint8", 255),
    },
    "43A4": {
        **{k: Layer(k, "int16", 32767, .0001, units="reflectance") for i in range(1, 8) for k in (f"Nadir_Reflectance_Band{i}",)},
        **{k: Layer(k, "uint8", 255) for i in range(1, 8) for k in (f"BRDF_Albedo_Band_Mandatory_Quality_Band{i}",)},
    },
}


@dataclass(frozen=True)
class Granule:
    key: str
    short: str
    prefix: str
    family: str
    nominal: dt.date
    h: int
    v: int

    @property
    def tile(self):
        return f"h{self.h:02d}v{self.v:02d}"

    @property
    def tile_id(self):
        return f"510{self.h:02d}0{self.v:02d}"

    @property
    def collection(self):
        return f"modis-{self.family}-061"

    @property
    def filename(self):
        return self.key + ".hdf"


def granule(key):
    match = GRANULE.fullmatch(key) if isinstance(key, str) else None
    if not match:
        raise ValueError("MODIS granule")
    short, prefix, family, year, doy, hh, vv, made = match.groups()
    h, v = int(hh), int(vv)
    if h > 35 or v > 17 or (family == "43A4" and prefix != "MCD") or (prefix == "MCD" and family not in ("43A4", "15A2H")):
        raise ValueError("MODIS tile or product")
    nominal = dt.date(int(year), 1, 1) + dt.timedelta(days=int(doy)-1)
    processed = dt.date(int(made[:4]), 1, 1) + dt.timedelta(days=int(made[4:7])-1)
    dt.time(int(made[7:9]), int(made[9:11]), int(made[11:13]))
    if nominal.year != int(year) or nominal < dt.date(2000, 1, 1) or processed.year != int(made[:4]) or processed < nominal:
        raise ValueError("MODIS day of year")
    return Granule(key, short, prefix, family, nominal, h, v)


def object_name(ref, layer):
    if layer == "hdf":
        return ref.filename
    if layer == "metadata":
        return ref.filename + ".xml"
    if layer not in LAYERS[ref.family]:
        raise ValueError("MODIS layer")
    return f"{ref.key}_{layer}.tif"


def object_url(ref, layer):
    container = SOURCE if layer in ("hdf", "metadata") else COGS
    return f"https://{HOST}/{container}/{ref.short}/{ref.h:02d}/{ref.v:02d}/{ref.key.split('.')[1][1:]}/{object_name(ref, layer)}"


def modis_object(url, container=None):
    """Return exact (identity, asset) only for a literal, unsigned, validated blob pathname."""
    if not isinstance(url, str) or not url.startswith(f"https://{HOST}/") or len(url) > 512:
        return None
    try:
        scope, _prod, _h, _v, _date, leaf = urlsplit(url).path[1:].split("/")
        if container not in (None, scope):
            return None
        if scope == COGS and leaf.endswith(".tif"):
            key, layer = leaf[:-4].split("_", 1)
            if layer in ("hdf", "metadata"):
                return None
        elif scope == SOURCE and leaf.endswith((".hdf", ".hdf.xml")):
            key = leaf.split(".hdf", 1)[0]
            layer = "metadata" if leaf.endswith(".xml") else "hdf"
        else:
            return None
        ref = granule(key)
        return (ref, layer) if url == object_url(ref, layer) else None
    except (TypeError, ValueError, OverflowError):
        return None


def modis_cog_object(url):
    return modis_object(url, COGS)


def modis_source_object(url):
    return modis_object(url, SOURCE)
