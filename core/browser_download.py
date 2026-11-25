"""Receive canonical CDSE archives with the installed Edge WebView2 engine.

The desktop owns a private, hidden download view. Tokens exist only in request
headers; native events are handled on the WinForms STA. Archive validation and
atomic publication remain the responsibility of raw_download.
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from types import SimpleNamespace

from .download import _save_json
from .errors import Cancelled, DownloadAccessError, TransientDownloadError, UserError
from .http import check_cancel, retry_seconds, wait_retry
from .native_catalogue import download_asset
from .local_paths import display_path


class BrowserUnavailable(UserError):
    pass


class BrowserHTTPError(UserError):
    def __init__(self, status, retry_after=None):
        super().__init__(f"官方浏览器下载服务返回 HTTP {status}。")
        self.status, self.retry_after = status, retry_after


def matches_product(url, item):
    parsed = download_asset(url)
    return bool(parsed and parsed[0] == item["uuid"] and parsed[1:3] == (None, None)
                and parsed[3] == item["url"])


def completed_transfer(item, path):
    checkpoint = path.with_name(path.name + ".browser.json")
    if not checkpoint.is_file() or not path.is_file():
        return None
    try:
        if checkpoint.stat().st_size > 4096:
            raise ValueError
        saved = json.loads(checkpoint.read_text(encoding="utf-8"))
        transfer = saved["transfer"]
        size, total = transfer["size"], transfer.get("expected_bytes")
        if (saved["schema"] != 1 or saved["url"] != item["url"] or saved["uuid"] != item["uuid"]
                or type(size) is not int or size <= 0 or path.stat().st_size != size
                or total is not None and (type(total) is not int or total != size)):
            raise ValueError
        return {"size": size, "expected_bytes": total, "etag": None, "content_type": "application/zip",
                "aws_single_part_md5": None, "azure_blob_md5": None, "resumed_bytes": 0,
                "transport_engine": "edge-webview2", "reused_complete_transfer": True}
    except (ValueError, TypeError, KeyError, OSError):
        raise UserError("已接收 ZIP 的本地记录不一致，请检查下载缓存后重试。") from None


class _Transfer:
    def __init__(self, item, path):
        self.item, self.path = item, path
        self.lock = threading.RLock()
        self.started, self.done = threading.Event(), threading.Event()
        self.stopped = threading.Event()
        self.interrupted = threading.Event()
        self.operation, self.error = None, None
        self.received, self.total, self.last_byte = 0, None, time.monotonic()
        self.etag, self.content_type = None, "application/zip"
        self.status, self.redirects, self.retry_after = None, 0, None
        self.reason = None
        self.navigation_id = None
        self.token = ""
        self.resumes = 0
        self.handlers = []

    def snapshot(self):
        with self.lock:
            return self.received, self.total, self.last_byte, self.error

    def fail(self, error):
        with self.lock:
            self.error = self.error or error
        self.started.set()
        self.done.set()


class EdgeDownload:
    def __init__(self, window):
        self.window = window
        self.ready = threading.Event()
        self.closed = threading.Event()
        self.closing = threading.Event()
        self.lock = threading.RLock()
        self.start_lock = threading.Lock()
        self.jobs = {}
        self.navigation = None
        self.core = None
        self.failure = False
        window.events.loaded += self._bind
        window.events.closed += self._on_closed

    @property
    def available(self):
        return self.ready.is_set() and not self.failure and not self.closed.is_set() and not self.closing.is_set()

    def _invoke(self, callback):
        native = self.window.native
        if native is None or native.IsDisposed:
            raise BrowserUnavailable("Edge 下载窗口尚未就绪，请使用标准下载或重新启动软件。")
        if native.InvokeRequired:
            from System import Func, Object
            return native.Invoke(Func[Object](callback))
        return callback()

    def _bind(self):
        if self.ready.is_set():
            return
        def bind():
            if self.core is not None:
                return None
            from Microsoft.Web.WebView2.Core import CoreWebView2WebResourceContext
            host = self.window.native.browser
            self.core = host.webview.CoreWebView2
            # The library's default cancels or opens a save dialog. This view
            # accepts only downloads bound to a currently authorized product.
            self.core.DownloadStarting -= host.on_download_starting
            self.core.WebResourceRequested -= host.on_web_resource_request
            self.core.WebResourceResponseReceived -= host.on_web_resource_response
            self.core.AddWebResourceRequestedFilter("*", CoreWebView2WebResourceContext.All)
            self.core.DownloadStarting += self._download_starting
            self.core.NavigationStarting += self._navigation_starting
            self.core.NavigationCompleted += self._navigation_completed
            self.core.WebResourceRequested += self._requesting
            self.core.WebResourceResponseReceived += self._response
            self.core.ServerCertificateErrorDetected += self._certificate_error
            self.core.NewWindowRequested += self._new_window
            self.core.Settings.AreDevToolsEnabled = False
            self.core.Settings.AreDefaultContextMenusEnabled = False
            self.core.Settings.AreDefaultScriptDialogsEnabled = False
            self.core.Settings.IsScriptEnabled = False
            return None
        try:
            self._invoke(bind)
        except Exception:
            self.failure = True
        finally:
            self.ready.set()

    def _find(self, url):
        with self.lock:
            return next((job for job in self.jobs.values() if matches_product(str(url), job.item)), None)

    def _new_window(self, _, args):
        args.Handled = True

    def _navigation_starting(self, _, args):
        job = self._find(args.Uri)
        if job is None:
            args.Cancel = True
            with self.lock:
                pending = [j for j in self.jobs.values() if not j.started.is_set()]
            for current in pending:
                current.fail(UserError("官方产品返回了未验证的重定向，未继续浏览器请求。"))
            return
        job.navigation_id = int(args.NavigationId)
        if args.IsRedirected:
            job.redirects += 1
            if job.redirects > 3:
                args.Cancel = True
                job.fail(UserError("官方原始产品下载重定向无法完成。"))

    def _requesting(self, _, args):
        job = self._find(args.Request.Uri)
        if job is not None:
            # A native resume can outlive the original access token. This
            # header is updated only for the same verified product URL.
            with job.lock:
                token = job.token
            if token:
                args.Request.Headers.SetHeader("Authorization", "Bearer " + token)
            return
        # Also reject subrequests before transmission, even if a remote server
        # returns an HTML document. Never attach an account header to that host.
        args.Request.Headers.RemoveHeader("Authorization")
        args.Response = self.core.Environment.CreateWebResourceResponse(None, 403, "Forbidden", "")

    def _response(self, _, args):
        job = self._find(args.Request.Uri)
        if job is None:
            return
        response = args.Response
        status = int(response.StatusCode)
        with job.lock:
            job.status = status
            for name, field in (("ETag", "etag"), ("Content-Type", "content_type")):
                if response.Headers.Contains(name):
                    value = str(response.Headers.GetHeader(name))
                    if len(value) <= 200:
                        setattr(job, field, value)
            if response.Headers.Contains("Retry-After"):
                value = str(response.Headers.GetHeader("Retry-After"))
                job.retry_after = min(86400, retry_seconds(SimpleNamespace(headers={"Retry-After": value}), 0))
        if status >= 400:
            job.fail(BrowserHTTPError(status, job.retry_after))

    def _certificate_error(self, _, args):
        from Microsoft.Web.WebView2.Core import CoreWebView2ServerCertificateErrorAction
        args.Action = CoreWebView2ServerCertificateErrorAction.Cancel
        job = self._find(args.RequestUri)
        if job:
            job.fail(UserError("下载连接的证书校验失败，请检查网络或代理设置后重试。"))

    def _navigation_completed(self, _, args):
        with self.lock:
            pending = [j for j in self.jobs.values() if not j.started.is_set()
                       and j.navigation_id == int(args.NavigationId)]
        for job in pending:
            status = int(args.HttpStatusCode)
            if status >= 400:
                job.fail(BrowserHTTPError(status, job.retry_after))
            elif args.IsSuccess:
                job.fail(UserError("官方服务返回了网页而非完整产品文件，请检查账号后重试。"))
            elif str(args.WebErrorStatus) in ("ConnectionAborted", "OperationCanceled"):
                # Chromium reports an aborted page navigation when it hands
                # the response to its download manager. DownloadStarting may
                # be delivered after NavigationCompleted.
                continue
            else:
                # A file navigation normally completes unsuccessfully after
                # DownloadStarting; that event has already released this wait.
                job.fail(TransientDownloadError("浏览器连接数据服务中断，请检查网络后重试。"))

    def _download_starting(self, _, args):
        operation = args.DownloadOperation
        job = self._find(operation.Uri)
        if job is None or job.operation is not None or job.done.is_set():
            args.Cancel = True
            args.Handled = True
            return
        args.ResultFilePath = display_path(job.path)
        args.Handled = True
        args.Cancel = False
        job.operation = operation

        def progress(*_):
            with job.lock:
                received = int(operation.BytesReceived)
                if received != job.received:
                    job.last_byte = time.monotonic()
                job.received = received
                total = int(operation.TotalBytesToReceive)
                job.total = total if total > 0 else None

        def state(*_):
            progress()
            current = str(operation.State)
            if current == "Completed":
                job.stopped.set()
                job.done.set()
            elif current == "InProgress":
                job.interrupted.clear()
                job.stopped.clear()
            elif current == "Interrupted":
                job.stopped.set()
                reason = str(operation.InterruptReason)
                job.reason = reason
                if reason == "UserCanceled":
                    job.fail(Cancelled())
                elif reason in ("NetworkFailed", "NetworkTimeout", "NetworkDisconnected", "NetworkServerDown", "ServerFailed") and operation.CanResume:
                    job.interrupted.set()
                elif reason in ("ServerCertificateProblem", "ServerCrossOriginRedirect"):
                    job.fail(UserError("浏览器下载的证书或产品重定向校验失败，请检查网络后重试。"))
                elif reason.startswith("File"):
                    error = UserError("浏览器无法写入下载文件，请检查输出目录权限和磁盘空间。")
                    error.browser_interrupt = reason
                    job.fail(error)
                else:
                    job.fail(TransientDownloadError("浏览器下载连接中断，请检查网络后重试。"))

        job.handlers = [progress, state]
        operation.BytesReceivedChanged += progress
        operation.StateChanged += state
        progress()
        job.started.set()
        state()

    def _cancel(self, job):
        def stop():
            if job.operation is not None:
                job.operation.Cancel()
            elif self.core is not None and self.navigation is job:
                self.core.Stop()
            return None
        if not self.closed.is_set():
            self._invoke(stop)
            if job.operation is not None and not job.stopped.wait(5):
                # Dispose the control before allowing cleanup of its files.
                self._invoke(lambda: self.window.native.browser.webview.Dispose())
                self._on_closed()
        elif job.operation is not None and not job.stopped.is_set():
            # Closing a native view disposes the browser and its file writer.
            # The normal close path has already cancelled active operations.
            job.stopped.set()

    def _once(self, item, path, auth, cancel, report):
        job = _Transfer(item, path)
        job.token = auth.token(cancel)
        started = time.monotonic()
        registered = False
        try:
            while not self.start_lock.acquire(timeout=.1):
                check_cancel(cancel)
                if self.closed.is_set() or self.closing.is_set():
                    raise Cancelled()
            try:
                check_cancel(cancel)
                if self.closed.is_set() or self.closing.is_set():
                    raise Cancelled()
                with self.lock:
                    if item["uuid"] in self.jobs:
                        raise UserError("同一个完整产品已在下载队列中。")
                    self.jobs[item["uuid"]] = job
                    self.navigation = job
                    registered = True
                started = time.monotonic()
                def navigate():
                    if self.closed.is_set() or self.closing.is_set():
                        raise Cancelled()
                    headers = "Authorization: Bearer " + job.token + "\r\nAccept: application/octet-stream\r\nAccept-Encoding: identity\r\n"
                    request = self.core.Environment.CreateWebResourceRequest(item["url"], "GET", None, headers)
                    self.core.NavigateWithWebResourceRequest(request)
                    return None
                self._invoke(navigate)
                while not job.started.wait(.1):
                    check_cancel(cancel)
                    if self.closed.is_set() or self.closing.is_set():
                        raise Cancelled()
                    if time.monotonic() - started > 65:
                        raise TransientDownloadError("浏览器等待官方下载响应超时，请检查网络后重试。")
            finally:
                if registered and job.operation is None:
                    self._cancel(job)
                if self.navigation is job:
                    self.navigation = None
                self.start_lock.release()
            while True:
                check_cancel(cancel)
                received, total, last_byte, error = job.snapshot()
                if error:
                    raise error
                if job.interrupted.is_set():
                    if job.resumes >= 3:
                        raise TransientDownloadError("浏览器续传连接多次中断，请检查网络后继续。")
                    report("浏览器保留下载进度，等待恢复连接", received, total)
                    wait_retry(job.resumes, cancel)
                    token = auth.token(cancel)
                    with job.lock:
                        job.token = token
                        job.last_byte = time.monotonic()
                        job.resumes += 1
                        job.interrupted.clear()
                    def resume():
                        check_cancel(cancel)
                        if self.closed.is_set() or self.closing.is_set():
                            raise Cancelled()
                        job.operation.Resume()
                        return None
                    self._invoke(resume)
                    continue
                report("接收完整 ZIP", received, total)
                if job.done.wait(.25):
                    break
                if time.monotonic() - last_byte > 95:
                    raise TransientDownloadError("浏览器接收原始产品超时，请检查网络后重试。")
            _, total, _, error = job.snapshot()
            if error:
                raise error
            check_cancel(cancel)
            size = path.stat().st_size
            if not size or size != job.received or total and total != size:
                raise UserError("浏览器收到的完整产品字节数不一致，未保存。")
            return {"size": size, "expected_bytes": total, "etag": job.etag,
                    "content_type": job.content_type, "aws_single_part_md5": None, "azure_blob_md5": None,
                    "resumed_bytes": 0, "transport_engine": "edge-webview2",
                    "native_resume_attempts": job.resumes,
                    "network_seconds": time.monotonic() - started}
        finally:
            # Cancel on the UI thread before the caller removes any unfinished
            # output. No native operation may continue writing after this call.
            try:
                if registered and (not job.done.is_set() or job.error):
                    self._cancel(job)
                def detach():
                    if job.operation is not None and job.handlers:
                        job.operation.BytesReceivedChanged -= job.handlers[0]
                        job.operation.StateChanged -= job.handlers[1]
                    return None
                if not self.closed.is_set():
                    self._invoke(detach)
            finally:
                with self.lock:
                    if self.jobs.get(item["uuid"]) is job:
                        self.jobs.pop(item["uuid"], None)
                job.token = ""

    def download(self, item, path, auth, cancel, report):
        from .safe_zip import check_zip_target
        check_zip_target(item)
        reused = completed_transfer(item, path)
        if reused:
            report("复用已接收的完整 ZIP，准备重新校验", reused["size"], reused["size"])
            return reused
        if len(display_path(path)) >= 260:
            raise BrowserUnavailable("Edge 下载临时路径过长，请使用标准下载引擎。")
        initialized = time.monotonic()
        while not self.ready.wait(.1):
            check_cancel(cancel)
            if self.closed.is_set():
                raise BrowserUnavailable("Edge 下载引擎已关闭，请重新启动软件。")
            if time.monotonic() - initialized > 30:
                raise BrowserUnavailable("Edge 下载引擎启动超时，请选择标准下载。")
        if not self.available:
            raise BrowserUnavailable("Edge 下载引擎无法启动，请选择标准下载。")
        checkpoint = path.with_name(path.name + ".browser.json")
        path.unlink(missing_ok=True)
        refreshed = False
        for attempt in range(3):
            check_cancel(cancel)
            try:
                transfer = self._once(item, path, auth, cancel, report)
                _save_json(checkpoint, {"schema": 1, "url": item["url"], "uuid": item["uuid"], "transfer": transfer})
                return transfer
            except BrowserHTTPError as error:
                if error.status == 401 and not refreshed:
                    auth.token(cancel, force=True)
                    refreshed = True
                    continue
                if error.status in (401, 403):
                    raise DownloadAccessError("CDSE 账号或产品下载权限失效，请重新连接后继续。") from None
                if error.status not in (429, 500, 502, 503, 504):
                    raise UserError(f"官方完整产品下载返回 HTTP {error.status}，请换个产品或稍后重试。") from None
                if error.retry_after and error.retry_after > 120:
                    raise TransientDownloadError(f"官方服务暂时限流，请至少等待 {error.retry_after} 秒后继续。", error.retry_after) from None
                delay = error.retry_after
            except TransientDownloadError:
                if attempt == 2:
                    raise
                delay = None
            if attempt == 2:
                raise TransientDownloadError("官方浏览器下载暂不可用，已尝试 3 次，请稍后继续。")
            report("浏览器连接中断或官方服务暂忙，准备重试", None, None)
            wait_retry(attempt, cancel, delay)
        raise TransientDownloadError("浏览器下载未完成，请稍后继续。")

    def _on_closed(self):
        self.closing.set()
        self.closed.set()
        with self.lock:
            for job in self.jobs.values():
                job.fail(Cancelled())
        self.ready.set()

    def close(self):
        if self.closed.is_set():
            return
        self.closing.set()
        with self.lock:
            jobs = list(self.jobs.values())
        for job in jobs:
            try:
                self._cancel(job)
            except Exception:
                pass
        # Stop every native writer before waking callers that will clean up
        # staging files. The control is private to this download engine.
        try:
            self._invoke(lambda: self.window.native.browser.webview.Dispose())
        except Exception:
            pass
        self._on_closed()
        try:
            self.window.destroy()
        except Exception:
            pass
