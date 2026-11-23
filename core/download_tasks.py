"""Persist exact download definitions so retry never depends on changed UI selections."""
from __future__ import annotations

import copy
import hashlib
import json
import re
import threading
import time
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

from .download import _save_json
from .errors import UserError

_ID = re.compile(r"[0-9a-f]{32}\Z")
_MAX_BYTES = 32 * 1024**2
_PRIVATE_KEYS = {"password", "secret", "client_secret", "access_token", "refresh_token", "id_token",
                 "secret_key", "access_key", "authorization", "token", "username"}
_SIGNED_QUERY = {"sig", "signature", "x-amz-signature", "x-amz-credential", "x-amz-security-token"}
INCOMPLETE = {"failed", "partial", "cancelled", "interrupted"}


def _definition(value):
    """Reject credentials rather than accidentally making a second account store."""
    def check(node):
        if isinstance(node, dict):
            if any(str(key).casefold() in _PRIVATE_KEYS for key in node):
                raise UserError("下载任务包含认证信息，未保存；请重新选择数据。")
            for child in node.values():
                check(child)
        elif isinstance(node, list):
            for child in node:
                check(child)
        elif isinstance(node, str) and re.match(r"(?i)^https?://", node):
            url = urlsplit(node)
            if url.username or url.password or any(key.casefold() in _SIGNED_QUERY | _PRIVATE_KEYS for key, _ in parse_qsl(url.query)):
                raise UserError("下载任务包含临时认证地址，未保存；请重新检索数据。")

    check(value)
    data = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    if len(data) > _MAX_BYTES:
        raise UserError("下载任务记录过大，请缩小区域或分批下载。")
    return hashlib.sha256(data).hexdigest()


class DownloadTasks:
    def __init__(self, root):
        self.root = Path(root) / "download-tasks"
        self.lock = threading.RLock()
        self.states = {}
        self.saved_at = {}
        if self.root.is_dir():
            for path in self.root.glob("*.state.json"):
                identifier = path.name.removesuffix(".state.json")
                if not _ID.fullmatch(identifier):
                    continue
                try:
                    if path.stat().st_size > _MAX_BYTES:
                        continue
                    state = json.loads(path.read_text(encoding="utf-8"))
                    if state.get("id") != identifier or state.get("schema") != 1:
                        continue
                    if state.get("status") in ("running", "cancelling"):
                        state.update(status="interrupted", message="软件关闭时下载尚未完成，可继续下载；已完成文件会核验后跳过。")
                    self.states[identifier] = state
                except (OSError, ValueError, TypeError, AttributeError):
                    continue

    def create(self, identifier, kind, definition, *, previous=None):
        if not isinstance(identifier, str) or not _ID.fullmatch(identifier) or kind not in ("download", "batch-download", "monthly-download"):
            raise UserError("下载任务标识无效，未保存任务。")
        digest = _definition(definition)
        title = definition["title"]
        with self.lock:
            if previous is not None and (previous not in self.states or self.states[previous].get("status") not in INCOMPLETE):
                raise UserError("该下载任务已继续或正在运行，请查看当前任务。")
            self.root.mkdir(parents=True, exist_ok=True)
            _save_json(self.root / (identifier + ".task.json"), {
                "schema": 1, "id": identifier, "kind": kind, "definition": definition, "sha256": digest})
            state = {"schema": 1, "id": identifier, "kind": kind, "status": "running", "title": title,
                     "output_dir": definition["output_dir"], "created_at": time.time(), "updated_at": time.time(),
                     "message": "准备继续下载" if previous else "准备下载", "progress": 0.0,
                     "previous_id": previous, "completed_files": 0,
                     "requires": sorted(set(definition["plan"].get("requires", [])) |
                                        ({"sentinel-hub"} if definition.get("profile") == "s2-process" else set()))}
            _save_json(self.root / (identifier + ".state.json"), state)
            self.states[identifier] = state
            if previous and previous in self.states:
                self.states[previous].update(status="replaced", retry_id=identifier)
                _save_json(self.root / (previous + ".state.json"), self.states[previous])

    def revise(self, identifier, definition):
        """Freeze a just-prepared grid before the first source bytes are written."""
        digest = _definition(definition)
        with self.lock:
            state = self.states.get(identifier)
            if state is None or state.get("status") not in ("running", "cancelling"):
                raise UserError("下载任务已结束，无法更新原计划。")
            _save_json(self.root / (identifier + ".task.json"), {"schema": 1, "id": identifier, "kind": state["kind"],
                                                                "definition": definition, "sha256": digest})

    def update(self, snapshot, *, force=False):
        identifier = snapshot["id"]
        with self.lock:
            state = self.states.get(identifier)
            if state is None or state.get("status") in ("replaced", "abandoned"):
                return
            state.update(status=snapshot["status"], message=snapshot["message"], progress=snapshot["progress"],
                         updated_at=time.time(), error=snapshot.get("error"),
                         folder=snapshot.get("detail", {}).get("folder") or state.get("folder"))
            result = snapshot.get("result") or {}
            state["completed_files"] = len(result.get("files", []))
            if force or time.monotonic() - self.saved_at.get(identifier, 0) >= 2:
                _save_json(self.root / (identifier + ".state.json"), state)
                self.saved_at[identifier] = time.monotonic()

    def load(self, identifier):
        if not isinstance(identifier, str) or not _ID.fullmatch(identifier):
            raise UserError("请选择一个尚未完成的下载任务。")
        with self.lock:
            state = self.states.get(identifier)
            if state is None or state.get("status") not in INCOMPLETE:
                raise UserError("该下载任务已完成、已继续或正在运行，请查看当前任务。")
            try:
                path = self.root / (identifier + ".task.json")
                if path.stat().st_size > _MAX_BYTES:
                    raise ValueError
                record = json.loads(path.read_text(encoding="utf-8"))
                if (record["schema"] != 1 or record["id"] != identifier
                        or record["kind"] not in ("download", "batch-download", "monthly-download")
                        or _definition(record["definition"]) != record["sha256"]):
                    raise ValueError
                return record["kind"], record["definition"]
            except (OSError, ValueError, KeyError, TypeError, RecursionError):
                raise UserError("此下载任务记录缺失或校验失败，请重新选择影像；已完成文件仍可复用。") from None

    def summaries(self):
        with self.lock:
            return copy.deepcopy(sorted((state for state in self.states.values() if state.get("status") in INCOMPLETE),
                                        key=lambda state: state.get("updated_at", 0), reverse=True))

    def abandon(self, identifier):
        """Remove an unfinished queue entry, preserving products and verified caches."""
        if not isinstance(identifier, str) or not _ID.fullmatch(identifier):
            raise UserError("请选择一个尚未完成的下载任务。")
        with self.lock:
            state = self.states.get(identifier)
            if state is None or state.get("status") not in INCOMPLETE:
                raise UserError("该任务已结束或正在运行；运行中的任务请先取消。")
            state.update(status="abandoned", updated_at=time.time(), message="任务已取消；已下载文件和有效缓存已保留。")
            _save_json(self.root / (identifier + ".state.json"), state)
            return {"cancelled": True, "job_id": identifier, "files_preserved": True}
