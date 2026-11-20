"""NASA Earthdata authentication and credential-safe data redirects."""
from __future__ import annotations

import re
import threading
from urllib.parse import urljoin, urlparse

from requests.auth import AuthBase

from .errors import UserError
from .http import HttpClient, check_cancel

EDL_TOKEN_URL = "https://urs.earthdata.nasa.gov/api/users/find_or_create_token"
DATA_HOSTS = frozenset({"data.lpdaac.earthdatacloud.nasa.gov", "data.ornldaac.earthdata.nasa.gov"})
DATA_BUCKETS = frozenset({"lp-prod-protected", "lp-prod-public", "ornl-cumulus-prod-protected", "ornl-cumulus-prod-public"})
_TOKEN = re.compile(r"[A-Za-z0-9._-]{16,16000}\Z")


class _ExplicitHeaders(AuthBase):
    def __call__(self, request):
        # Suppress requests' implicit .netrc/session authentication. The token is
        # set explicitly for each NASA hop and is absent on the signed S3 hop.
        if urlparse(request.url).hostname not in DATA_HOSTS:
            request.headers.pop("Authorization", None)
        return request


def data_url(url, *, redirect=False):
    """Allow NASA data hosts, and exact NASA S3 buckets only after a redirect."""
    if not isinstance(url, str):
        raise UserError("NASA 数据目录中的下载地址无效。")
    try:
        parsed = urlparse(url)
        port = parsed.port
    except ValueError:
        raise UserError("NASA 数据下载地址无效。") from None
    if (parsed.scheme != "https" or parsed.username or parsed.password or parsed.fragment
            or port not in (None, 443)):
        raise UserError("NASA 数据下载地址必须是可信的 HTTPS 地址。")
    host = parsed.hostname or ""
    if host in DATA_HOSTS and (redirect or not parsed.query):
        return url
    if redirect:
        match = re.fullmatch(r"([a-z0-9-]+)\.s3(?:[.-][a-z0-9-]+)?\.amazonaws\.com", host)
        if match and match[1] in DATA_BUCKETS:
            return url
        if re.fullmatch(r"s3[.-][a-z0-9-]+\.amazonaws\.com", host) and parsed.path.lstrip("/").split("/", 1)[0] in DATA_BUCKETS:
            return url
    if host == "urs.earthdata.nasa.gov":
        raise UserError("Earthdata 尚未授权此数据中心或令牌已失效。请在官方 Earthdata Search 首次下载并授权 LP DAAC / ORNL DAAC，再重新连接。")
    raise UserError("NASA 返回了未支持的下载地址，已停止传递凭据。")


class EarthdataAuth:
    """Runtime Earthdata token manager; reusable login inputs use the separate encrypted account store."""

    def __init__(self):
        self._lock = threading.RLock()
        self._token = ""

    @property
    def connected(self):
        with self._lock:
            return bool(self._token)

    def clear(self):
        with self._lock:
            self._token = ""

    def connect(self, value):
        method = value.get("method", "token")
        if method == "token":
            token = value.get("token", "")
            if not isinstance(token, str) or not _TOKEN.fullmatch(token.strip()):
                raise UserError("请输入 Earthdata User Token；它与 Copernicus 的账号和 Client Secret 不同。")
            with self._lock:
                self._token = token.strip()
            return {"connected": True, "verified": False}
        if method != "account":
            raise UserError("请选择 Earthdata 用户名密码或 User Token。")
        username, password = value.get("username", ""), value.get("password", "")
        if (not isinstance(username, str) or not isinstance(password, str) or not username.strip() or not password
                or len(username) > 320 or len(password) > 2048 or ":" in username or any(c in username for c in "\r\n")):
            raise UserError("请输入 NASA Earthdata 用户名（通常不是邮箱）和密码。")
        http = HttpClient()
        try:
            response = http.request("POST", EDL_TOKEN_URL, auth=(username.strip(), password),
                                    timeout=(12, 35), allow_redirects=False)
            try:
                if response.status_code != 200:
                    raise UserError("Earthdata 登录失败。请核对用户名密码；使用联合登录或多因素认证的账号可在 Earthdata 官网生成 User Token 后连接。")
                data = response.json()
                token = data.get("access_token") if isinstance(data, dict) else None
                if not isinstance(token, str) or not _TOKEN.fullmatch(token):
                    raise UserError("Earthdata 未返回有效 User Token，请在官网生成令牌后连接。")
                with self._lock:
                    self._token = token
                return {"connected": True, "verified": True}
            except ValueError:
                raise UserError("Earthdata 认证服务返回格式异常，请稍后重试。") from None
            finally:
                response.close()
        finally:
            http.close()

    def token(self):
        with self._lock:
            if not self._token:
                raise UserError("请先连接 NASA Earthdata 账号或 User Token；GEDI 检索无需登录，下载需要认证。")
            return self._token


def request_data(http, auth, url, *, cancel=None, headers=None, **kwargs):
    """Follow signed S3 redirects without ever forwarding the Earthdata token to S3."""
    url = data_url(url)
    base_headers = {"Accept-Encoding": "identity", **(headers or {})}
    if any(key.lower() in ("authorization", "cookie") for key in base_headers):
        raise ValueError("Authentication headers are managed by the Earthdata adapter")
    for _ in range(6):
        check_cancel(cancel)
        current_headers = base_headers.copy()
        if urlparse(url).hostname in DATA_HOSTS:
            current_headers["Authorization"] = "Bearer " + auth.token()
        response = http.request("GET", url, headers=current_headers, auth=_ExplicitHeaders(), cancel=cancel,
                                allow_redirects=False, **kwargs)
        if response.status_code in (301, 302, 303, 307, 308):
            location = response.headers.get("Location")
            response.close()
            if not location:
                raise UserError("NASA 下载重定向缺少地址，请重新检索。")
            url = data_url(urljoin(url, location), redirect=True)
            continue
        if response.status_code in (401, 403):
            response.close()
            raise UserError("GEDI 下载未获授权或 Earthdata 令牌已过期。请重新连接；首次使用请在 Earthdata Search 授权 LP DAAC / ORNL DAAC 应用。")
        return response
    raise UserError("NASA 下载重定向次数过多，请稍后重试。")
