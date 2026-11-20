"""Short-lived Planetary Computer Azure reads; sign only validated data paths in the requested container."""
from __future__ import annotations

import datetime as dt
from urllib.parse import parse_qsl

from .errors import UserError
from .native_catalogue import _json
from .modis_source import ACCOUNT as MODIS_ACCOUNT, COGS, SOURCE, modis_cog_object, modis_source_object
from .s3_source import PC_LANDSAT_ACCOUNT, PC_LANDSAT_CONTAINER, PC_RTC_ACCOUNT, PC_RTC_CONTAINER, landsat_blob_object, rtc_blob_object
from .scene_identity import utc_time

PC_TOKEN_ROOT = "https://planetarycomputer.microsoft.com/api/sas/v1/token"
PC_TOKEN = f"{PC_TOKEN_ROOT}/{PC_LANDSAT_ACCOUNT}/{PC_LANDSAT_CONTAINER}"
RTC_TOKEN = f"{PC_TOKEN_ROOT}/{PC_RTC_ACCOUNT}/{PC_RTC_CONTAINER}"
_SCOPES = {
    "landsat": (PC_TOKEN, landsat_blob_object, "Landsat", ("GET",)),
    "rtc": (RTC_TOKEN, rtc_blob_object, "Sentinel-1 RTC", ("GET",)),
    "modis-cog": (f"{PC_TOKEN_ROOT}/{MODIS_ACCOUNT}/{COGS}", modis_cog_object, "MODIS COG", ("HEAD", "GET")),
    "modis-source": (f"{PC_TOKEN_ROOT}/{MODIS_ACCOUNT}/{SOURCE}", modis_source_object, "MODIS HDF/XML", ("HEAD", "GET")),
}


class PlanetaryAccess:
    """A requests auth callable: creates a signed URL only on validated prepared reads; the original URL is safe to log."""
    def __init__(self, http, cancel=None, *, scope="landsat"):
        self.http = http
        self.cancel = cancel
        self.endpoint, self.valid_object, self.label, self.methods = _SCOPES[scope]
        self.clear()

    def clear(self):
        self._token = ""
        self._expiry = dt.datetime.min.replace(tzinfo=dt.timezone.utc)

    def __call__(self, request):
        if request.method not in self.methods or not self.valid_object(request.url):
            raise UserError(f"{self.label} 临时访问仅用于所选来源的有效数据路径。")
        if not self._token or self._expiry <= dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=60):
            self.clear()
            response = self.http.request("GET", self.endpoint, cancel=self.cancel, timeout=(10, 25), allow_redirects=False)
            if response.status_code in (401, 403):
                response.close()
                raise UserError(f"Planetary Computer 当前不向此会话提供 {self.label} 临时读取授权；没有切换其他数据源。")
            result = _json(response, f"{self.label} 临时读取授权")
            try:
                token = result["token"]
                expiry = utc_time(result["msft:expiry"])
                if not isinstance(token, str) or not 1 <= len(token) <= 4096 or any(ord(c) < 33 or ord(c) > 126 or c in "#?" for c in token):
                    raise ValueError
                pairs = parse_qsl(token, keep_blank_values=True, strict_parsing=True, max_num_fields=32)
                fields = dict(pairs)
                if (len(fields) != len(pairs) or not fields.get("sig") or fields.get("sp") not in ("r", "rl")
                        or fields.get("sr") not in ("b", "c") or expiry <= dt.datetime.now(dt.timezone.utc)):
                    raise ValueError
            except (AttributeError, KeyError, TypeError, ValueError, OverflowError):
                raise UserError(f"{self.label} 服务未提供有效的临时读取授权，请稍后重试。") from None
            self._token, self._expiry = token, expiry
        request.url += "?" + self._token
        return request
