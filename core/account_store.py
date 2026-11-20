"""Current-Windows-user encrypted storage for reusable platform credentials."""
from __future__ import annotations

import copy
import ctypes
import json
import os
import threading
import uuid
from ctypes import wintypes

from .errors import UserError
from .local_paths import io_path

ACCOUNT_FILE = "accounts.v1.dpapi"
PROVIDERS = ("oauth", "odata", "s3", "earthdata")
_HEADER = b"STARMAP-ACCOUNTS-1\n"
_MAX_BYTES = 256 * 1024


class WindowsProtection:
    """DPAPI binds the encrypted data to the current Windows user."""

    def __init__(self):
        if os.name != "nt":
            raise UserError("账号自动保存需要 Windows；当前连接仍可在本次运行中使用。")
        self.crypt = ctypes.WinDLL("crypt32", use_last_error=True)
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)

        class Blob(ctypes.Structure):
            _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]

        self.Blob = Blob
        pointer = ctypes.POINTER(Blob)
        self.crypt.CryptProtectData.argtypes = [pointer, wintypes.LPCWSTR, pointer, ctypes.c_void_p,
                                               ctypes.c_void_p, wintypes.DWORD, pointer]
        self.crypt.CryptUnprotectData.argtypes = [pointer, ctypes.c_void_p, pointer, ctypes.c_void_p,
                                                 ctypes.c_void_p, wintypes.DWORD, pointer]
        self.crypt.CryptProtectData.restype = self.crypt.CryptUnprotectData.restype = wintypes.BOOL
        self.kernel.LocalFree.argtypes = [ctypes.c_void_p]
        self.kernel.LocalFree.restype = ctypes.c_void_p

    def _blob(self, data):
        buffer = ctypes.create_string_buffer(data)
        return self.Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte))), buffer

    def _transform(self, data, decrypt):
        source, source_buffer = self._blob(data)
        entropy, entropy_buffer = self._blob(b"StarmapDownloader:64D676BE-9FB7-4DC8-BA41-F498E21BC854:accounts:v1")
        result = self.Blob()
        operation = self.crypt.CryptUnprotectData if decrypt else self.crypt.CryptProtectData
        description = None if decrypt else "Starmap Downloader accounts"
        success = operation(ctypes.byref(source), description, ctypes.byref(entropy), None, None,
                            1, ctypes.byref(result))  # CRYPTPROTECT_UI_FORBIDDEN, current user scope.
        if not success:
            raise UserError("本机保存的账号无法解密，请在登录账号中重新登录。" if decrypt else "无法加密保存账号，请检查当前 Windows 用户环境。")
        try:
            return ctypes.string_at(result.data, result.size)
        finally:
            self.kernel.LocalFree(ctypes.cast(result.data, ctypes.c_void_p))

    def protect(self, data):
        return self._transform(data, False)

    def unprotect(self, data):
        return self._transform(data, True)


class AccountStore:
    def __init__(self, config_dir, *, protection=None):
        self.path = io_path(config_dir) / ACCOUNT_FILE
        self.lock = threading.RLock()
        self._protection = protection
        self._accounts = {}
        self.load_error = ""
        try:
            if self.path.is_file():
                with self.path.open("rb") as stream:
                    data = stream.read(_MAX_BYTES + 1)
                if not data.startswith(_HEADER) or len(data) > _MAX_BYTES:
                    raise ValueError
                envelope = json.loads(self.protection.unprotect(data[len(_HEADER):]).decode("utf-8"))
                if not isinstance(envelope, dict) or envelope.get("version") != 1 or not isinstance(envelope.get("accounts"), dict):
                    raise ValueError
                self._accounts = {name: self._sanitize(name, value) for name, value in envelope["accounts"].items()}
        except (OSError, ValueError, TypeError, RecursionError, UserError):
            self._accounts = {}
            self.load_error = "已保存的登录信息无法读取，请重新登录；也可清除本机保存后重新设置。"

    @property
    def protection(self):
        if self._protection is None:
            self._protection = WindowsProtection()
        return self._protection

    @staticmethod
    def _sanitize(provider, value):
        if provider not in PROVIDERS or not isinstance(value, dict):
            raise ValueError("Invalid account record")
        if provider == "oauth":
            keys = ("client_id", "client_secret")
        elif provider == "s3":
            keys = ("access_key", "secret_key")
        else:
            method = value.get("method")
            if method not in ("account", "token"):
                raise ValueError("Invalid account method")
            keys = ("method", "username", "password", "refresh_token") if method == "account" else ("method", "token")
        result = {key: value[key] for key in keys if key in value}
        if not all(isinstance(item, str) and len(item) <= 20000 for item in result.values()):
            raise ValueError("Invalid credential value")
        required = ("client_id", "client_secret") if provider == "oauth" else ("access_key", "secret_key") if provider == "s3" else ("token",) if value["method"] == "token" else ("username", "password")
        if any(not result.get(key) for key in required):
            raise ValueError("Incomplete credential record")
        if provider == "odata":
            if value.get("requires_totp") is True:
                result["requires_totp"] = True
            if isinstance(value.get("expires_at"), (int, float)) and 0 < value["expires_at"] < 1e12:
                result["expires_at"] = value["expires_at"]
        return result

    def snapshot(self):
        with self.lock:
            return copy.deepcopy(self._accounts)

    def get(self, provider):
        with self.lock:
            return self._accounts.get(provider, {}).copy()

    def metadata(self):
        """Read the allowlisted display state without copying reusable secrets."""
        with self.lock:
            result = {}
            for provider in PROVIDERS:
                value = self._accounts.get(provider, {})
                result[provider] = {"saved": bool(value),
                                    "method": value.get("method", "client" if provider == "oauth" else "keys"),
                                    "identifier": value.get("client_id", "") if provider == "oauth" else value.get("username", ""),
                                    "requires_totp": bool(value.get("requires_totp"))}
            return result

    def _write(self, accounts):
        temporary = self.path.with_name(self.path.name + "." + uuid.uuid4().hex + ".tmp")
        try:
            if accounts:
                payload = json.dumps({"version": 1, "accounts": accounts}, ensure_ascii=False,
                                     separators=(",", ":"), allow_nan=False).encode("utf-8")
                encrypted = _HEADER + self.protection.protect(payload)
                if len(encrypted) > _MAX_BYTES:
                    raise ValueError
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with temporary.open("xb") as stream:
                    stream.write(encrypted)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, self.path)
            else:
                self.path.unlink(missing_ok=True)
            self._accounts = accounts
            self.load_error = ""
        except (OSError, ValueError):
            raise UserError("无法更新本机保存的账号，请检查配置文件夹权限和磁盘空间。") from None
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass

    def put(self, provider, value):
        try:
            clean = self._sanitize(provider, value)
        except ValueError:
            raise UserError("账号信息未能完整保存，请重新填写账号信息。") from None
        with self.lock:
            accounts = self.snapshot()
            accounts[provider] = clean
            self._write(accounts)

    def remember_refresh(self, refresh_token):
        with self.lock:
            value = self.get("odata")
            if value.get("method") == "account":
                value["refresh_token"] = refresh_token
                self.put("odata", value)

    def remove(self, provider):
        with self.lock:
            accounts = self.snapshot()
            accounts.pop(provider, None)
            self._write(accounts)

    def clear(self):
        with self.lock:
            self._write({})
