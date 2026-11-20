"""Strict, bounded, seekable HTTPS reader for immutable public COG objects."""
from __future__ import annotations

import base64
import binascii
import hashlib
import io
from collections import OrderedDict

import requests

from .errors import UserError
from .http import check_cancel
from .raw_download import BYTE_RANGE, _strong_tag


class RangeFile(io.RawIOBase):
    def __init__(self, http, url, cancel=None, *, page_size=512 * 1024, cache_pages=6, expected_size=None,
                 access=None, range_md5=False):
        super().__init__()
        if range_md5 and not 1 <= page_size <= 4 * 1024**2:
            raise ValueError("Azure range MD5 needs a positive page size of at most 4 MiB")
        self.http, self.url, self.cancel = http, url, cancel
        self.page_size, self.cache_pages = page_size, cache_pages
        self.expected_size, self.access, self.range_md5 = expected_size, access, range_md5
        self.position, self.size, self.etag = 0, None, None
        self.requests, self.bytes_received, self.failure = 0, 0, None
        self.verified_range_md5 = 0
        self.cache = OrderedDict()
        self.defer_errors = False
        self._page(0)

    def readable(self):
        return True

    def seekable(self):
        return True

    def tell(self):
        self._checkClosed()
        return self.position

    def seek(self, offset, whence=io.SEEK_SET):
        self._checkClosed()
        base = {io.SEEK_SET: 0, io.SEEK_CUR: self.position, io.SEEK_END: self.size}.get(whence)
        if base is None or base + offset < 0:
            raise ValueError("invalid seek")
        self.position = base + offset
        return self.position

    def _page(self, index):
        check_cancel(self.cancel)
        if index in self.cache:
            self.cache.move_to_end(index)
            return self.cache[index]
        begin = index * self.page_size
        last = begin + self.page_size - 1
        if self.size is not None:
            last = min(last, self.size - 1)
        headers = {"Range": f"bytes={begin}-{last}", "Accept-Encoding": "identity"}
        if self.range_md5:
            headers["x-ms-range-get-content-md5"] = "true"
        if self.etag:
            headers["If-Match"] = self.etag
        auth = {"auth": self.access} if self.access is not None else {}
        refreshed = False
        for attempt in range(3):
            response = self.http.request("GET", self.url, headers=headers, stream=True, timeout=(10, 25), cancel=self.cancel,
                                         allow_redirects=False, **auth)
            self.requests += 1
            try:
                if self.access is not None and response.status_code in (401, 403):
                    if refreshed or attempt == 2:
                        raise UserError("公开栅格源未授权读取此对象；未切换来源或发布数据。")
                    self.access.clear()
                    refreshed = True
                    continue
                if response.status_code != 206 or response.headers.get("Content-Encoding", "identity").lower() != "identity":
                    if response.status_code in (412, 416):
                        raise UserError("公开源文件版本或长度变化，未发布影像；请重新检索。")
                    raise UserError(f"公开源没有返回可校验的字节范围（HTTP {response.status_code}）。")
                tag = _strong_tag(response.headers.get("ETag"))
                value = BYTE_RANGE.fullmatch(response.headers.get("Content-Range", ""))
                if value is None or tag is None:
                    raise UserError("公开源缺少可核对的字节范围或版本标识。")
                start, stop, total = map(int, value.groups())
                if (not 1 <= total <= 64 * 1024**3 or start != begin or not start <= stop == min(last, total - 1)
                        or self.expected_size is not None and self.expected_size != total
                        or self.size is not None and (self.size != total or self.etag != tag)):
                    raise UserError("公开源返回的字节区间或文件版本不一致，未使用该文件。")
                if self.size is None:
                    self.size, self.etag = total, tag
                    # A body retry must not switch to a newly published revision.
                    headers["If-Match"] = tag
                expected = stop - start + 1
                length = response.headers.get("Content-Length")
                if length is not None and length != str(expected):
                    raise UserError("公开源区间长度与 HTTP 响应不一致。")
                data = bytearray()
                for block in response.iter_content(64 * 1024):
                    check_cancel(self.cancel)
                    self.bytes_received += len(block)
                    data.extend(block)
                    if len(data) > expected:
                        raise UserError("公开源返回了超出请求范围的内容。")
                if len(data) != expected:
                    raise requests.exceptions.ChunkedEncodingError()
                if self.range_md5:
                    try:
                        digest = base64.b64decode(response.headers.get("Content-MD5", ""), validate=True)
                    except (ValueError, TypeError, binascii.Error):
                        digest = b""
                    if len(digest) != 16 or hashlib.md5(data, usedforsecurity=False).digest() != digest:
                        raise UserError("Azure 字节区间的 MD5 缺失或不符，未使用该影像数据。")
                    self.verified_range_md5 += 1
            except (requests.ConnectionError, requests.Timeout, requests.exceptions.ChunkedEncodingError):
                if attempt == 2:
                    raise UserError("公开源字节传输多次中断，未发布影像。") from None
                continue
            finally:
                response.close()
            self.cache[index] = bytes(data)
            if len(self.cache) > self.cache_pages:
                self.cache.popitem(last=False)
            return self.cache[index]
        raise AssertionError("range retry limit")

    def read(self, size=-1):
        self._checkClosed()
        if self.defer_errors and self.failure:
            return b""
        try:
            remaining = max(0, self.size - self.position)
            size = remaining if size is None or size < 0 else min(size, remaining)
            if size > 64 * 1024**2:
                raise UserError("公开源单次读取超出安全缓冲上限，请缩小研究区。")
            result = bytearray()
            while size:
                index, offset = divmod(self.position, self.page_size)
                part = self._page(index)[offset:offset + size]
                result.extend(part)
                size -= len(part)
                self.position += len(part)
            return bytes(result)
        except Exception as error:
            # GDAL can turn callback failures into a generic I/O error or return a partial buffer.
            self.failure = error
            if self.defer_errors:
                # Let the C raster driver see a short read; the adapter re-raises the original error in Python.
                return b""
            raise

    def readinto(self, buffer):
        data = self.read(len(buffer))
        buffer[:len(data)] = data
        return len(data)

    def close(self):
        self.cache.clear()
        super().close()
