"""Platform login persistence and startup restoration, without API secret disclosure."""
from __future__ import annotations

import threading
import time

from .account_store import AccountStore, PROVIDERS
from .errors import UserError


class Accounts:
    def __init__(self, config_dir, oauth, odata, s3, earthdata):
        self.managers = {"oauth": oauth, "odata": odata, "s3": s3, "earthdata": earthdata}
        self.store = AccountStore(config_dir)
        self.errors = {}
        self.verified = {}
        self.storage_error = self.store.load_error
        self._restoring = False
        self._restored = set()
        self._writing_odata = False
        self._stopping = threading.Event()
        self._worker = None
        self._state_lock = threading.RLock()
        odata.on_refresh = self._remember_refresh

    @property
    def restoring(self):
        with self._state_lock:
            return self._restoring

    def start(self):
        if self.store.snapshot():
            with self._state_lock:
                self._restoring = True
            self._worker = threading.Thread(target=self._restore, name="restore-platform-accounts", daemon=True)
            self._worker.start()

    def _remember_refresh(self, token):
        if self._stopping.is_set() or self._writing_odata:
            return
        try:
            self.store.remember_refresh(token)
        except UserError as error:
            with self._state_lock:
                self.storage_error = str(error)

    def _inputs(self, provider, value):
        if not isinstance(value, dict):
            raise UserError("请填写完整的账号信息。")
        old = self.store.get(provider)
        method = value.get("method", "token" if provider == "earthdata" else "account")
        if provider == "oauth":
            names, identity = ("client_id", "client_secret"), "client_id"
        elif provider == "s3":
            names, identity = ("access_key", "secret_key"), "access_key"
        elif method == "token":
            names, identity = ("token",), None
        else:
            names, identity = ("username", "password", "totp"), "username"
        if not all(isinstance(value.get(name, ""), str) for name in names):
            raise UserError("账号信息格式不正确，请重新填写。")
        reuse = value.get("use_saved") is True and old and (provider in ("oauth", "s3") or old.get("method") == method)
        if reuse and identity and value.get(identity) and value[identity].strip() != old.get(identity):
            reuse = False
        if provider == "odata" and method == "token" and reuse and not value.get("token") and old.get("expires_at", 0) <= time.time():
            raise UserError("保存的临时访问令牌已过期，请填写新令牌或改用账号登录。")
        inputs = {name: value.get(name) or (old.get(name, "") if reuse and name != "totp" else "") for name in names}
        if provider not in ("oauth", "s3"):
            inputs["method"] = method
        return inputs

    def _connect(self, provider, inputs):
        manager = self.managers[provider]
        if provider == "oauth":
            return manager.connect(inputs.get("client_id", ""), inputs.get("client_secret", ""))
        if provider == "s3":
            return manager.connect(inputs.get("access_key", ""), inputs.get("secret_key", ""))
        return manager.connect(inputs)

    def _reusable(self, provider, inputs):
        saved = inputs.copy()
        if provider in ("oauth", "s3"):
            saved = {key: item.strip() for key, item in saved.items()}
        elif saved.get("method") == "token":
            saved["token"] = saved["token"].strip()
            if provider == "odata":
                saved["expires_at"] = time.time() + 3600
        else:
            saved["username"] = saved["username"].strip()
            if provider == "odata":
                saved["requires_totp"] = bool(saved.get("totp"))
                refresh = self.managers[provider].refresh_for_storage()
                if refresh:
                    saved["refresh_token"] = refresh
        saved.pop("totp", None)
        return saved

    def connect(self, provider, value):
        inputs = self._inputs(provider, value)
        self._writing_odata = provider == "odata"
        try:
            result = self._connect(provider, inputs)
        finally:
            self._writing_odata = False
        with self._state_lock:
            self.verified[provider] = provider == "oauth" or result.get("verified", False)
            self.errors.pop(provider, None)
        try:
            self.store.put(provider, self._reusable(provider, inputs))
            with self._state_lock:
                self.storage_error = ""
            result["saved"] = True
        except UserError as error:
            with self._state_lock:
                self.storage_error = str(error)
            result.update(saved=False, save_error=str(error))
        return result

    def _restore(self):
        try:
            workers = [threading.Thread(target=self._restore_one, args=(provider, inputs),
                                        name=f"restore-{provider}-account", daemon=True)
                       for provider, inputs in self.store.snapshot().items()]
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join()
        finally:
            if self._stopping.is_set():
                for manager in self.managers.values():
                    manager.clear()
            with self._state_lock:
                self._restoring = False

    def _restore_one(self, provider, inputs):
        if self._stopping.is_set():
            return
        try:
            if provider == "odata" and inputs.get("method") == "account":
                if inputs.get("refresh_token"):
                    try:
                        result = self.managers[provider].restore_refresh(inputs["refresh_token"])
                    except UserError:
                        if inputs.get("requires_totp"):
                            raise UserError("请补充当前两步验证码，已保存的密码可留空沿用。") from None
                        result = self._connect(provider, inputs)
                elif inputs.get("requires_totp"):
                    raise UserError("请补充当前两步验证码，已保存的密码可留空沿用。")
                else:
                    result = self._connect(provider, inputs)
            else:
                if provider == "odata" and inputs.get("method") == "token" and inputs.get("expires_at", 0) <= time.time():
                    raise UserError("保存的临时访问令牌已过期，请重新登录。")
                result = self._connect(provider, inputs)
            with self._state_lock:
                self.verified[provider] = provider == "oauth" or result.get("verified", False)
                self._restored.add(provider)
        except UserError as error:
            self.managers[provider].clear()
            with self._state_lock:
                self.errors[provider] = str(error)
        except Exception:
            self.managers[provider].clear()
            with self._state_lock:
                self.errors[provider] = "保存的账号未能自动连接，请重新登录或稍后重试。"

    def status(self):
        saved = self.store.metadata()
        with self._state_lock:
            restoring, restored = self._restoring, self._restored.copy()
            verified, errors = self.verified.copy(), self.errors.copy()
            storage_error = self.storage_error
        providers = {}
        for provider in PROVIDERS:
            connected = provider in restored if restoring else self.managers[provider].connected
            providers[provider] = {**saved[provider], "connected": connected,
                                   "verified": verified.get(provider, False), "error": errors.get(provider, "")}
        connected = any(value["connected"] for value in providers.values())
        return {"restoring": restoring, "providers": providers, "has_saved_accounts": any(value["saved"] for value in saved.values()),
                "has_connected_accounts": connected, "login_required": not restoring and (not connected or bool(errors) or bool(storage_error)),
                "storage_error": storage_error}

    def disconnect(self, provider):
        self.store.remove(provider)
        self.managers[provider].clear()
        with self._state_lock:
            self.errors.pop(provider, None)
            self.verified.pop(provider, None)
        return {"connected": False, "saved": False}

    def clear(self):
        self.store.clear()
        for manager in self.managers.values():
            manager.clear()
        with self._state_lock:
            self.errors.clear()
            self.verified.clear()
            self.storage_error = ""
        return self.status()

    def stop(self):
        self._stopping.set()
