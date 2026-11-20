"""Decode bounded, representative science arrays from source-verified Sentinel-3 members."""
from __future__ import annotations

import datetime as dt
import math
import re

from .errors import UserError
from .http import check_cancel
from .open_sen3 import SRA_MEMBER, SRA_TYPE, WST_TYPE, ghrsst_name
from .s3_source import SEN3_NAME
from .scene_identity import utc_time

CORE = {
    "LST_in.nc": (2, ("LST", "exception")),
    "geodetic_in.nc": (2, ("latitude_in", "longitude_in")),
    "flags_in.nc": (2, ("confidence_in", "cloud_in")),
    "time_in.nc": (1, ("time_stamp_i",)),
    "geo_coordinates.nc": (2, ("latitude", "longitude")),
    "gifapar.nc": (2, ("GIFAPAR",)),
    "chl_nn.nc": (2, ("CHL_NN",)),
    "NRT_AOD.nc": (2, ("AOD_550_Merged_OceanLand", "latitude", "longitude", "aod_quality_flags")),
    "NTC_AOD.nc": (2, ("AOD_550", "latitude", "longitude", "aod_quality_flags")),
    "FRP_in.nc": (2, ("flags",)),  # A valid product can report zero fires.
    SRA_MEMBER: (1, ("time_l1a_echo_sar_ku", "lat_l1a_echo_sar_ku", "lon_l1a_echo_sar_ku", "alt_l1a_echo_sar_ku")),
}
MAX_DECODE_BYTES = 1024**3
STRIPE_BYTES = 8 * 1024**2
MAX_STRIPE_BYTES = 32 * 1024**2
GDS_EPOCH = dt.datetime(1981, 1, 1, tzinfo=dt.timezone.utc)


def verify_netcdf(path, name, proof, cancel=None):
    import h5py
    import numpy as np

    def scalar(value):
        array = np.asarray(value)
        if array.size != 1:
            raise ValueError
        atom = array.reshape(-1)[0]
        return atom.item() if isinstance(atom, np.generic) else atom

    def text(value):
        atom = scalar(value)
        if isinstance(atom, bytes):
            atom = atom.decode("ascii")
        if not isinstance(atom, str) or not 1 <= len(atom) <= 250:
            raise ValueError
        return atom

    def data_member(file, key, kinds="uif"):
        if not isinstance(file.get(key, getlink=True), h5py.HardLink):
            raise ValueError
        data = file[key]
        if not isinstance(data, h5py.Dataset) or data.dtype.kind not in kinds or data.shape is None:
            raise ValueError
        return data

    def allow_decode(data):
        if (not data.size or data.ndim > 8 or data.size * data.dtype.itemsize > MAX_DECODE_BYTES
                or data.is_virtual or data.external or (data.chunks and math.prod(data.chunks) * data.dtype.itemsize > 64 * 1024**2)):
            raise ValueError
        control = data.id.get_create_plist()
        if any(control.get_filter(i)[0] not in (1, 2, 3, 4, 5, 6) for i in range(control.get_nfilters())):
            raise ValueError

    def decode(data, axis=0):
        allow_decode(data)
        shape = data.shape
        if not shape:
            check_cancel(cancel)
            data[()]
        else:
            line = math.prod(shape) // shape[axis] * data.dtype.itemsize
            if not 1 <= line <= MAX_STRIPE_BYTES:
                raise ValueError
            n = max(1, STRIPE_BYTES // line)
            if data.chunks:
                span = data.chunks[axis]
                # Decode an integral number of scanline chunks when possible, to avoid repeat decompression.
                if span * line <= MAX_STRIPE_BYTES:
                    n = max(span, (n // span) * span)
            for first in range(0, shape[axis], n):
                check_cancel(cancel)
                part = [slice(None)] * len(shape)
                part[axis] = slice(first, first + n)
                data[tuple(part)]
        return {"variable": data.name[1:], "shape": list(shape), "dtype": str(data.dtype)}

    def period(start, end, tolerance=1):
        if (utc_time(start) >= utc_time(end)
                or abs((utc_time(start) - utc_time(proof["start_utc"])).total_seconds()) > tolerance
                or abs((utc_time(end) - utc_time(proof["end_utc"])).total_seconds()) > tolerance):
            raise UserError("SEN3 NetCDF 观测时段与选中的产品不一致。")

    def orbit(attrs):
        if "absolute_orbit_number" not in attrs:
            return False
        recorded = scalar(attrs["absolute_orbit_number"])
        if type(recorded) is not int or recorded != proof["orbit"]:
            raise UserError("SEN3 NetCDF 内的绝对轨道号不符。")
        return True

    def ghrsst(file, parsed):
        name_match = ghrsst_name(name, parsed)
        attrs = file.attrs
        # GHRSST L2P has no product_name. Source digests and exact manifest/file identity are required by planning/streaming;
        # here, independently verify the native format's own identity fields.
        version = text(attrs["product_version"])
        gds = re.fullmatch(r"([0-9]{1,2}\.[0-9]{1,2})(?:r[0-9]{1,3})?", text(attrs["gds_version_id"]))
        ver_parts = lambda value: tuple(int(part) for part in value.split("."))
        sat, centre = parsed["sat"][-1], parsed["centre"]
        if (not name_match or not gds or not re.fullmatch(r"[0-9]{1,2}\.[0-9]{1,2}", version)
                or ver_parts(gds[1]) != ver_parts(name_match["gds"]) or ver_parts(version) != ver_parts(name_match["version"])
                or text(attrs["id"]) != f"SLSTR{sat}-{centre}-L2P-v{version}"
                or text(attrs["platform"]).replace("-", "") != f"Sentinel3{sat}" or text(attrs["sensor"]) != "SLSTR"
                or text(attrs["processing_level"]) != "L2P" or text(attrs["naming_authority"]) != "org.ghrsst"
                or not re.fullmatch(r"SL__SST\." + parsed["baseline"] + r"\.[0-9]{2}\.[0-9]{2}", text(attrs["processing_baseline"]))
                or utc_time(text(attrs["date_created"])) != utc_time(parsed["created"] + "Z")):
            raise UserError("GHRSST 海表温度原件的卫星、处理基线或文件版本不符。")
        first, last = text(attrs["start_time"]), text(attrs["stop_time"])
        if (utc_time(first) != utc_time(text(attrs["time_coverage_start"]))
                or utc_time(last) != utc_time(text(attrs["time_coverage_end"]))):
            raise ValueError
        # WST compact whole-second field may describe the last sample rather than the XFDU interval endpoint.
        period(first, last, tolerance=2)
        fields = [(data_member(file, key, kinds), axis) for key, kinds, axis in (
            ("lat", "f", 0), ("lon", "f", 0), ("sea_surface_temperature", "i", 1),
            ("quality_level", "ui", 1), ("l2p_flags", "ui", 1), ("time", "i", 0),
        )]
        lat, lon, sst, quality, flags, time = [field for field, _ in fields]
        if (lat.ndim != 2 or lat.shape != lon.shape or not all(n > 0 for n in lat.shape)
                or any(field.shape != (1, *lat.shape) for field in (sst, quality, flags))
                or time.shape != (1,) or len({field.id for field, _ in fields}) != len(fields)
                or text(time.attrs["units"]) != "seconds since 1981-01-01T00:00:00Z"
                or text(sst.attrs["standard_name"]) != "sea_surface_skin_temperature"
                or text(lat.attrs["units"]) != "degrees_north" or text(lon.attrs["units"]) != "degrees_east"):
            raise ValueError
        allow_decode(time)
        check_cancel(cancel)
        if GDS_EPOCH + dt.timedelta(seconds=int(time[0])) != utc_time(first):
            raise UserError("GHRSST 时间数组与该文件的覆盖起点不符。")
        return {"product": proof["product"], "identity_schema": "GHRSST L2P SSTskin + exact CDSE/XFDU source hashes",
                "orbit_attribute_verified": orbit(attrs), "time_attributes_verified": True,
                "decoded_arrays": [decode(field, axis) for field, axis in fields]}

    check_cancel(cancel)
    try:
        parsed = SEN3_NAME.fullmatch(proof["product"])
        if not parsed:
            raise ValueError
        with h5py.File(path, "r", rdcc_nbytes=12 * 1024**2) as file:
            attrs = file.attrs
            if parsed["type"] == WST_TYPE:
                return ghrsst(file, parsed)
            product = text(attrs["product_name"])
            if product != proof["product"]:
                raise UserError("SEN3 NetCDF 内嵌产品号与选中的原始版本不一致。")
            time_keys = ("start_time", "stop_time")
            if parsed["type"] == SRA_TYPE:
                if name != SRA_MEMBER or text(attrs["altimeter_sensor_name"]) != "SRAL":
                    raise ValueError
                time_keys = ("first_meas_time", "last_meas_time")
            has_time = any(key in attrs for key in time_keys)
            if has_time:
                period(*(text(attrs[key]) for key in time_keys))
            elif name != "viscal.nc":
                raise ValueError
            matched_orbit = orbit(attrs)
            if name in CORE:
                rank, keys = CORE[name]
                fields = [data_member(file, key) for key in keys]
                if any(f.ndim != rank or f.shape != fields[0].shape for f in fields) or len({f.id for f in fields}) != len(fields):
                    raise ValueError
            elif isinstance(file.get(name[:-3], getlink=True), h5py.HardLink):
                fields = [data_member(file, name[:-3])]
            else:
                candidates = []
                for key in file:
                    check_cancel(cancel)
                    if not isinstance(file.get(key, getlink=True), h5py.HardLink):
                        continue
                    obj = file[key]
                    if (isinstance(obj, h5py.Dataset) and obj.dtype.kind in "uif" and obj.size
                            and obj.attrs.get("CLASS") != b"DIMENSION_SCALE"):
                        candidates.append(obj)
                fields = [max(candidates, key=lambda obj: (obj.size, obj.name))]
            return {"product": product, "orbit_attribute_verified": matched_orbit, "time_attributes_verified": has_time,
                    "decoded_arrays": [decode(d) for d in fields]}
    except (KeyError, TypeError, ValueError, RuntimeError, OSError, OverflowError):
        raise UserError(f"无法解码或核对 SEN3 NetCDF 的主要数组：{name}。未发布文件。") from None
