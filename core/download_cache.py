"""Checksummed caches for immutable source ranges and completed regional raster blocks."""
from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import tempfile
import time
import zipfile
from pathlib import Path

import numpy as np

from .errors import UserError
from .http import check_cancel
from .integrity import sha256_file


def save_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, suffix=".tmp", delete=False, mode="w", encoding="utf-8") as stream:
            temporary = Path(stream.name)
            json.dump(value, stream, ensure_ascii=False, sort_keys=True, allow_nan=False)
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


class PartialDownload:
    """Resume only an exact source version and a checksummed, durably recorded prefix."""
    def __init__(self, path, identity):
        self.path = Path(path)
        self.receipt = self.path.with_name(self.path.name + ".resume.json")
        self.identity = fingerprint(identity)
        self.state = None
        self.sha = hashlib.sha256()
        self.saved_at = 0.0
        self.saved_size = 0

    def load(self, cancel=None):
        check_cancel(cancel)
        try:
            if self.receipt.stat().st_size > 64 * 1024:
                raise ValueError
            state = json.loads(self.receipt.read_text(encoding="utf-8"))
            tag, count, total = state["etag"], state["received"], state["total"]
            if (state.get("schema") != 1 or state.get("identity") != self.identity
                    or not isinstance(tag, str) or len(tag) < 3 or tag[0] != '"' or tag[-1] != '"'
                    or any(c in tag[1:-1] for c in ('"', '\r', '\n'))
                    or type(count) is not int or type(total) is not int or not 0 < count <= total <= 512 * 1024**3
                    or self.path.stat().st_size < count):
                raise ValueError
            sha, remaining = hashlib.sha256(), count
            with self.path.open("rb") as stream:
                while remaining:
                    check_cancel(cancel)
                    block = stream.read(min(4 * 1024**2, remaining))
                    if not block:
                        raise ValueError
                    sha.update(block)
                    remaining -= len(block)
            if sha.hexdigest() != state["sha256"]:
                raise ValueError
            # A process can stop after writing bytes but before committing their receipt.
            # Only the verified prefix is reused; unrecorded tail bytes are re-fetched.
            if self.path.stat().st_size != count:
                with self.path.open("r+b") as stream:
                    stream.truncate(count)
            self.state, self.sha, self.saved_size = state, sha, count
            return state
        except (OSError, ValueError, TypeError, KeyError):
            self.discard()
            return None

    def reset(self):
        self.state, self.sha, self.saved_size = None, hashlib.sha256(), 0
        self.receipt.unlink(missing_ok=True)

    def checkpoint(self, received, total, etag, *, transfer=None, force=False):
        if (not etag or type(total) is not int or not 0 < received <= total
                or not force and received - self.saved_size < 16 * 1024**2 and time.monotonic() - self.saved_at < 2):
            return
        state = {"schema": 1, "identity": self.identity, "etag": etag, "received": received,
                 "total": total, "sha256": self.sha.hexdigest(), "transfer": transfer}
        save_json(self.receipt, state)
        self.state, self.saved_size, self.saved_at = state, received, time.monotonic()

    def discard(self):
        self.path.unlink(missing_ok=True)
        self.receipt.unlink(missing_ok=True)
        self.reset()

    def forget(self):
        self.receipt.unlink(missing_ok=True)


class RangeCache:
    def __init__(self, root, url, page_size):
        self.root = Path(root) / fingerprint(url)
        self.page_size = page_size
        self.object = None
        try:
            path = self.root / "object.json"
            if path.stat().st_size < 4096:
                value = json.loads(path.read_text(encoding="utf-8"))
                if (value.get("schema") == 1 and type(value.get("size")) is int and value["size"] > 0
                        and isinstance(value.get("etag"), str)):
                    self.object = value
        except (OSError, ValueError, TypeError):
            pass

    def bind(self, etag, size):
        if self.object and (self.object["etag"], self.object["size"]) != (etag, size):
            raise UserError("公开源对象版本与保存的下载进度不同，未混用数据；请重新检索并选择新任务。")
        self.object = {"schema": 1, "etag": etag, "size": size}
        save_json(self.root / "object.json", self.object)

    def _page(self, index, page_size=None):
        return self.root / fingerprint([self.object["etag"], self.object["size"], page_size or self.page_size]) / str(index)

    @staticmethod
    def _verified_page(path, expected):
        try:
            metadata = json.loads(path.with_suffix(".json").read_text(encoding="utf-8"))
            if expected <= 0 or path.stat().st_size != expected or metadata.get("size") != expected:
                return None
            data = path.read_bytes()
            if hashlib.sha256(data).hexdigest() == metadata.get("sha256"):
                return data
        except (OSError, ValueError, TypeError):
            pass
        return None

    def read(self, index):
        if self.object is None:
            return None
        expected = min(self.page_size, self.object["size"] - index * self.page_size)
        if expected <= 0:
            return None
        data = self._verified_page(self._page(index), expected)
        if data is not None:
            return data
        # Older regional downloads stored 1 MiB pages. Reuse a smaller slice
        # only after verifying the entire old page against the same version.
        legacy_size = 1024 ** 2
        if self.page_size < legacy_size and legacy_size % self.page_size == 0:
            start = index * self.page_size
            legacy_index, offset = divmod(start, legacy_size)
            legacy_expected = min(legacy_size, self.object["size"] - legacy_index * legacy_size)
            legacy = self._verified_page(self._page(legacy_index, legacy_size), legacy_expected)
            if legacy is not None:
                return legacy[offset:offset + expected]
        return None

    def write(self, index, data):
        path = self._page(index)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=path.parent, suffix=".tmp", delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(data)
            os.replace(temporary, path)
            temporary = None
            save_json(path.with_suffix(".json"), {"size": len(data), "sha256": hashlib.sha256(data).hexdigest()})
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)


class ArrayCache:
    """A complete block is reused only after every array and its provenance receipt validate."""
    def __init__(self, root, identity, specs):
        self.root = Path(root) / "blocks" / fingerprint(identity)
        self.identity, self.specs = identity, specs
        self.reused = 0

    def _paths(self, key):
        name = fingerprint(key)
        return self.root / (name + ".npz"), self.root / (name + ".json")

    def load(self, key, shape, cancel=None):
        check_cancel(cancel)
        path, receipt = self._paths(key)
        try:
            if receipt.stat().st_size > 16 * 1024**2:
                return None
            meta = json.loads(receipt.read_text(encoding="utf-8"))
            if (meta.get("schema") != 1 or meta.get("identity") != self.identity or meta.get("key") != key
                    or fingerprint(meta["detail"]) != meta.get("detail_sha256")):
                return None
            if path.stat().st_size != meta["size"] or sha256_file(path, cancel) != meta["sha256"]:
                return None
            with zipfile.ZipFile(path) as zipped:
                if set(zipped.namelist()) != {name + ".npy" for name in self.specs}:
                    return None
                for name, spec in self.specs.items():
                    if zipped.getinfo(name + ".npy").file_size > spec["count"] * shape[0] * shape[1] * np.dtype(spec["dtype"]).itemsize + 4096:
                        return None
            with np.load(path, allow_pickle=False) as archive:
                if set(archive.files) != set(self.specs):
                    return None
                arrays = {name: archive[name] for name in archive.files}
            for name, spec in self.specs.items():
                array = arrays[name]
                if array.shape != (spec["count"], *shape) or array.dtype != np.dtype(spec["dtype"]):
                    return None
            self.reused += 1
            return arrays, meta["detail"]
        except (OSError, ValueError, KeyError, TypeError, zipfile.BadZipFile, EOFError):
            return None

    def save(self, key, arrays, detail, cancel=None):
        check_cancel(cancel)
        path, receipt = self._paths(key)
        self.root.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=self.root, suffix=".tmp", delete=False) as stream:
                temporary = Path(stream.name)
                # Low compression avoids repeating expensive GeoTIFF encoding while saving disk space.
                with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=1) as archive:
                    for name, array in arrays.items():
                        check_cancel(cancel)
                        buffer = io.BytesIO()
                        np.save(buffer, array, allow_pickle=False)
                        archive.writestr(name + ".npy", buffer.getvalue())
            digest = sha256_file(temporary, cancel)
            size = temporary.stat().st_size
            os.replace(temporary, path)
            temporary = None
            save_json(receipt, {"schema": 1, "identity": self.identity, "key": key, "size": size,
                                "sha256": digest, "detail": detail, "detail_sha256": fingerprint(detail)})
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def clear(self):
        if self.root.is_dir():
            shutil.rmtree(self.root)
