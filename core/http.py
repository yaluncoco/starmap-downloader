from __future__ import annotations

import datetime as dt
import math
import ssl
import threading
import time
from email.utils import parsedate_to_datetime
from urllib.parse import urlparse

import requests

from . import VERSION
from .errors import Cancelled, UserError, TransientDownloadError
from .cancellable_http import request_with_cancel

TOKEN_URL = "https://identity.dataspace.copernicus.eu/auth/realms/CDSE/protocol/openid-connect/token"
CATALOGUE_URL = "https://catalogue.dataspace.copernicus.eu/odata/v1/Products"
PROCESS_URL = "https://sh.dataspace.copernicus.eu/process/v1"
SH_CATALOGUE_URL = "https://sh.dataspace.copernicus.eu/catalog/v1/search"
USER_AGENT = f"CopernicusDownloader/{VERSION} (local desktop; documentation.dataspace.copernicus.eu)"


def tls_error_kind(error):
    """Recognize transport EOFs without retrying certificate or unknown TLS failures.

    Requests/urllib3 can wrap the original SSL exception several times. Inspect
    only exception links and their arguments; never expose these diagnostics,
    which can contain proxy credentials or signed request URLs.
    """
    pending, seen, interrupted, unknown = [error], set(), False, False
    while pending and len(seen) < 32:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(current, ssl.SSLCertVerificationError):
            return "certificate"
        if isinstance(current, ssl.SSLEOFError):
            interrupted = True
        reason = getattr(current, "reason", None)
        if isinstance(current, ssl.SSLError) and reason not in (None, "UNEXPECTED_EOF_WHILE_READING"):
            unknown = True
        for value in (*getattr(current, "args", ()), reason,
                      getattr(current, "__cause__", None), getattr(current, "__context__", None)):
            if isinstance(value, BaseException):
                pending.append(value)
            elif isinstance(value, str):
                if "[SSL: CERTIFICATE_VERIFY_FAILED]" in value or value == "CERTIFICATE_VERIFY_FAILED":
                    return "certificate"
                if "[SSL: UNEXPECTED_EOF_WHILE_READING]" in value or value == "UNEXPECTED_EOF_WHILE_READING":
                    interrupted = True
    return "interrupted" if interrupted and not unknown and not pending else "unknown"


def tls_error_message(error, subject="下载"):
    if tls_error_kind(error) == "certificate":
        return f"{subject}连接的证书校验失败，请检查网络或代理设置后重试。"
    return f"{subject}连接的 TLS 安全握手失败，请检查网络或代理设置后重试。"


def check_cancel(event):
    if event is not None and event.is_set():
        raise Cancelled()


def wait_retry(attempt, cancel=None, retry_after=None):
    delay = retry_after if retry_after is not None else min(2 ** (attempt + 1), 16)
    if delay > 120:
        raise TransientDownloadError(f"数据服务暂时限流，请至少等待 {math.ceil(delay)} 秒后继续下载。", delay)
    if cancel is not None:
        if cancel.wait(delay):
            raise Cancelled()
    else:
        time.sleep(delay)


def retry_seconds(response, attempt, *, sentinel_hub=False):
    value = response.headers.get("Retry-After")
    seconds = 0.0
    if value:
        try:
            seconds = float(value)
            # Unlike standard HTTP Retry-After, Sentinel Hub's 429 uses milliseconds.
            if sentinel_hub and response.status_code == 429:
                seconds /= 1000
        except ValueError:
            try:
                target = parsedate_to_datetime(value)
                if target.tzinfo is None:
                    target = target.replace(tzinfo=dt.timezone.utc)
                seconds = (target - dt.datetime.now(dt.timezone.utc)).total_seconds()
            except (TypeError, ValueError, OverflowError):
                pass
    elif response.headers.get("X-RateLimit-Retry-After"):
        try:
            seconds = float(response.headers["X-RateLimit-Retry-After"]) / 1000
        except ValueError:
            pass
    return max(1.0, seconds) if math.isfinite(seconds) and seconds > 0 else 2 ** (attempt + 1)


class HttpClient:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT})

    def request(self, method, url, *, cancel=None, status=None, timeout=(15, 120), attempts=5, **kwargs):
        if type(attempts) is not int or not 1 <= attempts <= 5:
            raise ValueError("Invalid HTTP attempt limit")
        sentinel_hub = urlparse(url).hostname == "sh.dataspace.copernicus.eu"
        # Do not forward credentials or request bodies to redirected hosts.
        kwargs.setdefault("allow_redirects", False)
        for attempt in range(attempts):
            check_cancel(cancel)
            try:
                response = request_with_cancel(self.session, method, url, cancel=cancel, timeout=timeout, **kwargs)
            except (requests.ConnectionError, requests.Timeout) as error:
                check_cancel(cancel)
                if isinstance(error, requests.exceptions.SSLError) and tls_error_kind(error) != "interrupted":
                    raise UserError(tls_error_message(error)) from None
                if attempt == attempts - 1:
                    raise TransientDownloadError("连接数据服务失败，已保留下载进度，可检查网络后继续。") from None
                delay = min(2 ** (attempt + 1), 16)
                if status:
                    status(f"网络连接中断，{delay} 秒后重试（{attempt + 1}/{attempts - 1}）")
                wait_retry(attempt, cancel)
                continue
            if response.status_code in (429, 500, 502, 503, 504):
                delay = retry_seconds(response, attempt, sentinel_hub=sentinel_hub)
                if attempt == attempts - 1:
                    response.close()
                    raise TransientDownloadError(f"数据服务限流或暂不可用，已尝试 {attempts} 次。可稍后继续下载。", delay)
                response.close()
                if delay > 120:
                    raise TransientDownloadError(f"官方服务暂时限流，请至少等待 {math.ceil(delay)} 秒后重试。", delay)
                if status:
                    status(f"官方服务暂忙，{delay:g} 秒后重试（{attempt + 1}/{attempts - 1}）")
                if cancel is not None:
                    if cancel.wait(delay):
                        raise Cancelled()
                else:
                    time.sleep(delay)
                continue
            return response
        raise UserError("请求未完成。")

    def close(self):
        self.session.close()


class AuthManager:
    """Runtime OAuth manager; reusable client inputs are stored separately with Windows DPAPI."""

    def __init__(self):
        self._lock = threading.RLock()
        self._state_lock = threading.RLock()
        self._client_id = ""
        self._secret = ""
        self._token = ""
        self._expires = 0.0

    @property
    def connected(self):
        with self._state_lock:
            return bool(self._client_id and self._secret and self._token)

    def clear(self):
        with self._lock, self._state_lock:
            self._client_id = self._secret = self._token = ""
            self._expires = 0.0

    def connect(self, client_id, client_secret):
        client_id, client_secret = str(client_id).strip(), str(client_secret).strip()
        if not client_id or not client_secret:
            raise UserError("请同时填写 Client ID 和 Client Secret。")
        if len(client_id) > 300 or len(client_secret) > 1000:
            raise UserError("凭据长度不正确，请重新复制。")
        with self._lock:
            token, expires = self._fetch(client_id, client_secret)
            with self._state_lock:
                self._client_id, self._secret = client_id, client_secret
                self._token, self._expires = token, expires
        return {"connected": True, "client_id": client_id, "expires_in": max(0, int(expires - time.monotonic()))}

    def _fetch(self, client_id, secret, cancel=None):
        http = HttpClient()
        try:
            response = http.request("POST", TOKEN_URL, data={"grant_type": "client_credentials", "client_id": client_id, "client_secret": secret}, timeout=(15, 40), cancel=cancel)
            try:
                data = response.json()
            except ValueError:
                raise UserError("认证服务没有返回有效结果，请稍后重试。") from None
            finally:
                response.close()
            if not isinstance(data, dict):
                raise UserError("认证服务没有返回有效结果，请稍后重试。")
            if response.status_code != 200:
                code = data.get("error", "")
                if code in ("invalid_client", "unauthorized_client", "invalid_grant"):
                    raise UserError("凭据验证失败。请检查 Client ID 和 Client Secret 是否来自同一客户端，以及客户端是否过期。")
                raise UserError(f"认证失败（HTTP {response.status_code}），请在官方 Dashboard 检查客户端状态。")
            if not data.get("access_token"):
                raise UserError("认证服务没有返回访问令牌。")
            try:
                lifetime = int(data.get("expires_in", 300))
            except (TypeError, ValueError, OverflowError):
                raise UserError("认证服务未返回有效的令牌有效期，请稍后重试。") from None
            if lifetime <= 0:
                raise UserError("访问令牌已过期，请重新连接。")
            return str(data["access_token"]), time.monotonic() + lifetime
        finally:
            http.close()

    def token(self, cancel=None, force=False):
        with self._lock:
            if not self._secret:
                raise UserError("请先连接 API 凭据。")
            if force or time.monotonic() >= self._expires - 45:
                token, expires = self._fetch(self._client_id, self._secret, cancel)
                with self._state_lock:
                    self._token, self._expires = token, expires
            return self._token

