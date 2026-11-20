"""Manually check and verify installers from a build-pinned public GitHub Release."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import requests

from .errors import UserError
from .http import check_cancel

_API = "https://api.github.com"
_HEADERS = {"X-GitHub-Api-Version": "2026-03-10", "User-Agent": "StarmapDownloader-Update"}
_CDN_HOSTS = {"release-assets.githubusercontent.com", "objects.githubusercontent.com", "github-releases.githubusercontent.com"}
_MAX_METADATA = 1024 * 1024
_MAX_INSTALLER = 512 * 1024 * 1024
_TIMEOUT = (6, 15)
_REPOSITORY = re.compile(r"[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,37}[a-zA-Z0-9])?/[a-zA-Z0-9_.-]{1,100}")
_NUMBER = r"(0|[1-9][0-9]{0,8})"
_VERSION = re.compile(rf"v?{_NUMBER}\.{_NUMBER}(?:\.{_NUMBER})?")
_DIGEST = re.compile(r"sha256:([0-9a-fA-F]{64})")


def validate_repository(value):
    if not isinstance(value, str) or not _REPOSITORY.fullmatch(value) or value.split("/")[1] in (".", ".."):
        raise ValueError("Expected a GitHub repository in owner/repo format.")
    return value


def packaged_repository():
    try:
        channel = json.loads(Path(__file__).with_name("update-channel.json").read_text(encoding="utf-8"))
        return validate_repository(channel["repository"])
    except (OSError, ValueError, TypeError, KeyError):
        return None


def version_key(value):
    """Compare stable versions numerically, treating an omitted patch as zero."""
    match = _VERSION.fullmatch(value) if isinstance(value, str) else None
    return tuple(int(component or "0") for component in match.groups()) if match else None


@dataclass(frozen=True)
class Installer:
    asset_id: int
    name: str
    size: int
    sha256: str


@dataclass(frozen=True)
class Release:
    tag: str
    version: str
    installer: Installer | None


def _installer(assets, version):
    if not isinstance(assets, list):
        return None
    name = f"StarmapDownloader-Setup-{version}-win-x64.exe"
    matches = [asset for asset in assets if isinstance(asset, dict) and asset.get("name") == name]
    if len(matches) != 1:
        return None
    asset = matches[0]
    asset_id, size, digest = asset.get("id"), asset.get("size"), asset.get("digest")
    if (asset.get("state") != "uploaded" or type(asset_id) is not int or asset_id <= 0
            or type(size) is not int or not 0 < size <= _MAX_INSTALLER):
        return None
    match = _DIGEST.fullmatch(digest) if isinstance(digest, str) else None
    return Installer(asset_id, name, size, match[1].lower()) if match else None


def _anonymous_session():
    session = requests.Session()
    # Do not read .netrc, stale proxy auth, or credentials for any imagery service.
    session.trust_env = False
    return session


def _require_ok(response):
    status = response.status_code
    if status == 429 or (status == 403 and (response.headers.get("X-RateLimit-Remaining") == "0"
                                            or response.headers.get("Retry-After"))):
        raise UserError("GitHub 暂时限制了请求次数，请稍后再试。")
    if status != 200:
        raise UserError(f"GitHub 更新服务返回 HTTP {status}，请稍后检查更新。")


def _trusted_asset_redirect(location):
    if not isinstance(location, str) or any(ord(c) < 33 or ord(c) > 126 for c in location):
        return False
    try:
        url = urlsplit(location)
        return (url.scheme == "https" and url.netloc.lower() in _CDN_HOSTS
                and url.path.startswith("/") and not url.fragment and "\\" not in location)
    except ValueError:
        return False


class UpdateClient:
    def __init__(self, repository):
        self.repository = validate_repository(repository) if repository else None

    def latest(self):
        if self.repository is None:
            return None
        url = f"{_API}/repos/{self.repository}/releases/latest"
        try:
            with _anonymous_session() as session, session.get(
                url, headers={**_HEADERS, "Accept": "application/vnd.github+json"},
                timeout=_TIMEOUT, allow_redirects=False, stream=True,
            ) as response:
                if response.status_code == 404:
                    return None
                _require_ok(response)
                body = bytearray()
                for chunk in response.iter_content(16384):
                    if len(body) + len(chunk) > _MAX_METADATA:
                        raise UserError("GitHub 发布信息过大，无法安全检查更新。")
                    body.extend(chunk)
                data = json.loads(body)
        except (ValueError, RecursionError) as error:
            raise UserError("GitHub 返回的发布信息无法识别，请稍后再试。") from error
        except requests.RequestException as error:
            raise UserError("无法连接 GitHub 检查更新，请检查网络后重试。") from error
        if not isinstance(data, dict):
            raise UserError("GitHub 返回的发布信息无法识别，请稍后再试。")
        tag = data.get("tag_name")
        if data.get("draft") is not False or data.get("prerelease") is not False or version_key(tag) is None:
            return None
        version = tag.removeprefix("v")
        return Release(tag, version, _installer(data.get("assets"), version))

    def release_page(self, release=None):
        suffix = f"/tag/{release.tag}" if release else ""
        return f"https://github.com/{self.repository}/releases{suffix}"

    def download(self, installer, folder, *, cancel, progress):
        """Publish an .exe only after both byte count and SHA-256 match GitHub metadata."""
        check_cancel(cancel)
        folder.mkdir(parents=True, exist_ok=True)
        url = f"{_API}/repos/{self.repository}/releases/assets/{installer.asset_id}"
        target = folder / installer.name
        temporary = None
        try:
            with _anonymous_session() as session:
                for hop in range(4):
                    check_cancel(cancel)
                    headers = {"Accept": "application/octet-stream", "Accept-Encoding": "identity"}
                    if hop == 0:
                        headers.update(_HEADERS)
                    with session.get(url, headers=headers, timeout=_TIMEOUT, stream=True, allow_redirects=False) as response:
                        if response.status_code in (301, 302, 303, 307, 308):
                            url = response.headers.get("Location")
                            if not _trusted_asset_redirect(url):
                                raise UserError("安装包下载地址不在可信的 GitHub 下载域名内，已停止。")
                            continue
                        _require_ok(response)
                        size = response.headers.get("Content-Length")
                        if (response.headers.get("Content-Encoding", "identity").lower() != "identity"
                                or (size is not None and size != str(installer.size))):
                            raise UserError("安装包响应与发布信息不符，未保存安装包。请重新检查更新。")
                        digest = hashlib.sha256()
                        received = 0
                        progress("正在下载安装包…", 0)
                        with tempfile.NamedTemporaryFile(dir=folder, prefix=f".{installer.name}.", suffix=".part", delete=False) as stream:
                            temporary = Path(stream.name)
                            for chunk in response.iter_content(256 * 1024):
                                check_cancel(cancel)
                                if not chunk:
                                    continue
                                received += len(chunk)
                                if received > installer.size:
                                    raise UserError("安装包大小与发布信息不符，未保存安装包。")
                                stream.write(chunk)
                                digest.update(chunk)
                                progress("正在下载安装包…", 0.99 * received / installer.size)
                        break
                else:
                    raise UserError("GitHub 下载地址重定向次数过多，已停止。")
            check_cancel(cancel)
            if received != installer.size or not hmac.compare_digest(digest.hexdigest(), installer.sha256):
                raise UserError("安装包大小或 SHA-256 校验未通过，未保存安装包。请重新检查更新。")
            if os.name == "nt":
                # Keep Windows' normal downloaded-file warnings when the user runs it manually.
                try:
                    Path(f"{temporary}:Zone.Identifier").write_bytes(b"[ZoneTransfer]\r\nZoneId=3\r\n")
                except OSError as error:
                    raise UserError("无法添加 Windows 网络下载标记，请改用 GitHub 发布页获取安装包。") from error
            os.replace(temporary, target)
            temporary = None
            return target
        except requests.RequestException as error:
            raise UserError("安装包下载中断，请检查网络后重试。") from error
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
