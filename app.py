from __future__ import annotations

import argparse
import json
import mimetypes
import re
import secrets
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from core.errors import UserError
from core.cities import DATA as CITY_DATA
from core.service import Service

ROOT = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
UI_ROOT = ROOT / "ui"


class NativeDialogs:
    def __init__(self):
        self.window = None
        self._lock = threading.Lock()

    def open(self, request):
        import webview

        kind = request.get("kind")
        if kind not in ("choose_boundary", "choose_folder", "save_boundary", "choose_reference"):
            raise UserError("未知的文件选择操作。")
        if self.window is None:
            raise UserError("窗口尚未就绪，请稍后重试。")
        if not self._lock.acquire(blocking=False):
            raise UserError("请先关闭当前文件选择窗口。")

        def pick():
            if kind == "choose_boundary":
                result = self.window.create_file_dialog(webview.FileDialog.OPEN, allow_multiple=False,
                                                        file_types=("边界文件 (*.shp;*.geojson;*.json)", "所有文件 (*.*)"))
            elif kind == "choose_reference":
                result = self.window.create_file_dialog(webview.FileDialog.OPEN, allow_multiple=False,
                                                        file_types=("GeoTIFF (*.tif;*.tiff)",))
            elif kind == "choose_folder":
                current = str(request.get("current") or "")[:2048]
                directory = str(Path(current).parent) if current and not Path(current).is_dir() else current
                result = self.window.create_file_dialog(webview.FileDialog.FOLDER, directory=directory or "")
            else:
                extension = "geojson" if request.get("extension") == "geojson" else "shp"
                label = "Shapefile (*.shp)" if extension == "shp" else "GeoJSON (*.geojson)"
                result = self.window.create_file_dialog(webview.FileDialog.SAVE, save_filename="研究区." + extension, file_types=(label,))
            return result if isinstance(result, str) else (result[0] if result else "")

        try:
            # COM file dialogs must run on the native WinForms STA, not the HTTP worker.
            native = self.window.native
            if sys.platform == "win32" and native is not None and native.InvokeRequired:
                from System import Func, Object
                path = native.Invoke(Func[Object](pick))
            else:
                path = pick()
            return {"path": str(path or "")}
        finally:
            self._lock.release()


class AppServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, service, dialogs=None):
        self.service = service
        self.dialogs = dialogs
        self.csrf_token = secrets.token_urlsafe(32)
        super().__init__(address, Handler)
        self.origin = f"http://127.0.0.1:{self.server_port}"


class Handler(BaseHTTPRequestHandler):
    server: AppServer

    def log_message(self, *_):
        pass

    def _respond(self, status, body, mime="application/json; charset=utf-8"):
        if not isinstance(body, bytes):
            body = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store" if "json" in mime or "html" in mime else "public, max-age=3600")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob: https://*.tile.openstreetmap.org https://tile.openstreetmap.org https://server.arcgisonline.com; connect-src 'self'; font-src 'self' data:; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(body)

    def _trusted_host(self):
        return self.headers.get("Host") == urlparse(self.server.origin).netloc

    def do_GET(self):
        if not self._trusted_host():
            return self._respond(403, {"error": "本地请求地址不正确。"})
        path = urlparse(self.path)
        if path.path.startswith("/api/") and self.headers.get("X-App-Token", "") != self.server.csrf_token:
            return self._respond(403, {"error": "请求校验失败，请重新打开软件。"})
        try:
            if path.path == "/api/bootstrap":
                return self._respond(200, {**self.server.service.bootstrap(), "native_dialogs": self.server.dialogs is not None,
                                          "edge_download": self.server.service.browser_download is not None,
                                          "edge_download_ready": bool(getattr(self.server.service.browser_download, "available", False))})
            if path.path == "/api/accounts":
                return self._respond(200, self.server.service.account_status())
            if path.path == "/api/cities":
                return self._respond(200, self.server.service.cities.public())
            if path.path == "/api/map-status":
                return self._respond(200, self.server.service.map_tiles.status())
            if path.path == "/map/admin-basemap.geojson":
                return self._respond(200, (CITY_DATA / "admin-basemap.geojson").read_bytes(), "application/geo+json; charset=utf-8")
            if path.path.startswith("/map/tiles/"):
                match = re.fullmatch(r"/map/tiles/(standard|satellite)/(\d{1,2})/(\d{1,6})/(\d{1,6})\.png", path.path)
                if match is None:
                    raise UserError("底图路径无效。")
                try:
                    body, mime = self.server.service.map_tiles.get(match[1], *map(int, match.group(2, 3, 4)))
                except UserError as error:
                    return self._respond(503, {"error": str(error)})
                return self._respond(200, body, mime)
            if path.path == "/api/resumable-downloads":
                return self._respond(200, {"tasks": self.server.service.download_tasks.summaries()})
            if path.path == "/api/job":
                job_id = parse_qs(path.query).get("id", [""])[0]
                return self._respond(200, self.server.service.job(job_id))
            if path.path == "/health":
                return self._respond(200, {"ok": True})
            relative = "index.html" if path.path == "/" else path.path.lstrip("/")
            target = (UI_ROOT / relative).resolve()
            if not target.is_relative_to(UI_ROOT.resolve()) or not target.is_file():
                return self._respond(404, {"error": "文件不存在。"})
            body = target.read_bytes()
            if target.name == "index.html":
                body = body.replace(b"__SESSION_TOKEN__", self.server.csrf_token.encode("ascii"))
            mime = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
            if target.suffix in (".js", ".css", ".html", ".svg"):
                mime += "; charset=utf-8"
            self._respond(200, body, mime)
        except UserError as error:
            self._respond(400, {"error": str(error)})
        except ConnectionError:
            pass
        except Exception:
            self._respond(500, {"error": "本地服务发生异常，请重新启动软件。"})

    def _reject_post(self, message):
        # Closing over unread request bytes can reset TCP on Windows and lose the 403.
        # Drain only the existing bounded body, with a short deadline for incomplete senders.
        previous_timeout = self.connection.gettimeout()
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if 0 < length <= 8 * 1024 * 1024:
                self.connection.settimeout(3)
                self.rfile.read(length)
        except (ValueError, OSError):
            pass
        finally:
            self.connection.settimeout(previous_timeout)
        try:
            self._respond(403, {"error": message})
        except ConnectionError:
            pass

    def do_POST(self):
        if not self._trusted_host() or self.headers.get("X-App-Token", "") != self.server.csrf_token:
            return self._reject_post("请求校验失败，请重新打开软件。")
        origin = self.headers.get("Origin")
        if origin and origin != self.server.origin:
            return self._reject_post("仅支持本地界面请求。")
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 8 * 1024 * 1024:
                raise UserError("请求体为空或边界过大。")
            value = json.loads(self.rfile.read(length))
            if not isinstance(value, dict):
                raise UserError("请求格式不正确。")
            route = urlparse(self.path).path
            if route == "/api/dialog" and self.server.dialogs is not None:
                return self._respond(200, self.server.dialogs.open(value))
            methods = {
                "/api/connect": self.server.service.connect,
                "/api/disconnect": lambda _: self.server.service.disconnect(),
                "/api/raw-connect": self.server.service.raw_connect,
                "/api/raw-disconnect": lambda _: self.server.service.raw_disconnect(),
                "/api/s3-connect": self.server.service.s3_connect,
                "/api/s3-disconnect": lambda _: self.server.service.s3_disconnect(),
                "/api/earthdata-connect": self.server.service.earthdata_connect,
                "/api/earthdata-disconnect": lambda _: self.server.service.earthdata_disconnect(),
                "/api/collections": self.server.service.collections,
                "/api/aoi": self.server.service.aoi,
                "/api/export-aoi": self.server.service.export_aoi,
                "/api/search": self.server.service.search,
                "/api/batch-search": self.server.service.batch_search,
                "/api/batch-plan": self.server.service.batch_plan,
                "/api/batch-download": self.server.service.batch_download,
                "/api/monthly-search": self.server.service.monthly_search,
                "/api/monthly-plan": self.server.service.monthly_plan,
                "/api/monthly-download": self.server.service.monthly_download,
                "/api/prepare-plan": self.server.service.prepare_plan,
                "/api/plan": self.server.service.plan,
                "/api/coverage": self.server.service.coverage,
                "/api/download": self.server.service.download,
                "/api/retry-download": self.server.service.retry_download,
                "/api/cancel": lambda v: self.server.service.cancel(v.get("job_id", "")),
                "/api/abandon-download": self.server.service.abandon_download,
                "/api/geocode": self.server.service.geocode,
                "/api/check-update": self.server.service.check_update,
                "/api/open-update-release": self.server.service.open_update_release,
                "/api/download-update": self.server.service.download_update,
                "/api/install-update": self.server.service.install_update,
                "/api/open-link": self.server.service.open_link,
                "/api/open-folder": self.server.service.open_folder,
                "/api/preferences": self.server.service.save_preferences,
                "/api/accounts-clear": self.server.service.clear_accounts,
            }
            if route not in methods:
                return self._respond(404, {"error": "未知操作。"})
            self._respond(200, methods[route](value))
        except UserError as error:
            self._respond(400, {"error": str(error)})
        except (ValueError, TypeError):
            self._respond(400, {"error": "输入格式不正确。"})
        except ConnectionError:
            pass
        except Exception:
            self._respond(500, {"error": "本地服务发生异常，请重新启动软件。"})


def make_server(port=0, config_dir=None, dialogs=None):
    service = Service(config_dir)
    return AppServer(("127.0.0.1", port), service, dialogs)


def main():
    parser = argparse.ArgumentParser(description="星图影像下载器")
    parser.add_argument("--server", action="store_true", help="只启动本地界面服务，用于开发")
    parser.add_argument("--port", type=int, default=0)
    args = parser.parse_args()
    dialogs = None if args.server else NativeDialogs()
    server = make_server(args.port, dialogs=dialogs)
    if args.server:
        print(server.origin, flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.service.shutdown()
            server.server_close()
        return
    import webview
    if sys.platform == "win32":
        from ctypes import windll
        windll.shell32.SetCurrentProcessExplicitAppUserModelID("StarmapDownloader")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    window = webview.create_window("星图影像下载器", server.origin, width=1440, height=960, min_size=(1100, 740), background_color="#f5f7f8", text_select=True)
    dialogs.window = window
    engine = None
    if sys.platform == "win32":
        from core.browser_download import EdgeDownload
        download_window = webview.create_window("星图下载引擎", html="<html><body></body></html>",
                                                hidden=True, width=300, height=200)
        engine = EdgeDownload(download_window)
        server.service.browser_download = engine
        window.events.closed += engine.close
    try:
        webview.start(gui="edgechromium", debug=False, private_mode=True, icon=str(UI_ROOT / "assets" / "globe-orbit.ico"))
    finally:
        server.service.shutdown()
        if engine:
            engine.close()
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    main()

