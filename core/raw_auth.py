"""Authentication for original CDSE products (separate from Sentinel Hub OAuth clients)."""

from __future__ import annotations

import threading
import time

from .errors import UserError
from .http import HttpClient, TOKEN_URL


class RawAuth:
    def __init__(self):
        self._lock = threading.RLock()
        self.clear()

    def clear(self):
        with self._lock:
            self._access, self._refresh, self._expires = "", "", 0.0

    @property
    def connected(self):
        with self._lock:
            return bool(self._access and (self._refresh or self._expires > time.monotonic()))

    def connect(self, request):
        method = request.get("method", "account")
        if method == "token":
            access = request.get("token", "")
            if not isinstance(access, str) or not 16 <= len(access.strip()) <= 12000 or any(c.isspace() for c in access.strip()):
                raise UserError("请输入正确的 CDSE 临时访问令牌；不要填写 Sentinel Hub Client Secret。")
            with self._lock:
                # A pasted access token is validated by the download endpoint, not by decoding it locally.
                self._access, self._refresh, self._expires = access.strip(), "", time.monotonic() + 3600
            return {"connected": True, "verified": False}
        if method != "account":
            raise UserError("未知的原始产品认证方式。")
        username, password, totp = (request.get(key, "") for key in ("username", "password", "totp"))
        if not all(isinstance(x, str) for x in (username, password, totp)) or not username.strip() or not password or len(username) > 320 or len(password) > 2048:
            raise UserError("请输入 CDSE 登录邮箱和密码。")
        if totp and (len(totp.strip()) != 6 or not totp.strip().isascii() or not totp.strip().isdigit()):
            raise UserError("两步验证动态码应为 6 位数字。")
        payload = {"grant_type": "password", "username": username.strip(), "password": password}
        if totp:
            payload["totp"] = totp.strip()
        with self._lock:
            try:
                self._exchange(payload)
            finally:
                payload.clear()
        return {"connected": True, "verified": True}

    def _exchange(self, payload, cancel=None):
        http = HttpClient()
        try:
            response = http.request("POST", TOKEN_URL, data={"client_id": "cdse-public", **payload}, timeout=(12, 30), cancel=cancel)
            try:
                status = response.status_code
                if status != 200:
                    raise UserError("CDSE 账号认证失败或已过期。请核对邮箱、密码和两步验证码，或粘贴有效访问令牌。")
                data = response.json()
            finally:
                response.close()
            access, refresh = data.get("access_token"), data.get("refresh_token", "")
            lifetime = int(data.get("expires_in", 0))
            if not isinstance(access, str) or not access or not isinstance(refresh, str) or not 0 < lifetime <= 86400:
                raise ValueError
            self._access, self._refresh, self._expires = access, refresh, time.monotonic() + lifetime
        except (ValueError, TypeError, AttributeError, OverflowError):
            raise UserError("CDSE 认证服务没有返回可用访问令牌。") from None
        finally:
            http.close()

    def token(self, cancel=None, force=False):
        with self._lock:
            if not self._access:
                raise UserError("请先连接 CDSE 账号或临时访问令牌。")
            remaining = self._expires - time.monotonic()
            if force or remaining <= (30 if self._refresh else 0):
                if not self._refresh:
                    self.clear()
                    raise UserError("临时访问令牌可能已失效，请重新连接 CDSE 账号或令牌。")
                try:
                    self._exchange({"grant_type": "refresh_token", "refresh_token": self._refresh}, cancel)
                except UserError:
                    self.clear()
                    raise
            return self._access
