"""Verify installed Windows shortcuts and the icon actually returned by the shell."""
from __future__ import annotations

import argparse
import base64
import ctypes
from ctypes import wintypes
import hashlib
import json
import os
from pathlib import Path
import subprocess


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def shortcut_metadata(paths):
    def quoted(value):
        return "'" + str(value).replace("'", "''") + "'"

    command = """
[Console]::OutputEncoding=[System.Text.UTF8Encoding]::new($false)
$taskShell=New-Object -ComObject WScript.Shell
$taskShortcuts=@(
""" + ",\n".join(quoted(path) for path in paths) + """
)
@($taskShortcuts | ForEach-Object {
    $taskLink=$taskShell.CreateShortcut($_)
    [pscustomobject]@{Path=$_;TargetPath=$taskLink.TargetPath;IconLocation=$taskLink.IconLocation}
}) | ConvertTo-Json -Compress
"""
    encoded = base64.b64encode(command.encode("utf-16-le")).decode("ascii")
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-EncodedCommand", encoded],
        capture_output=True, timeout=20, check=True,
    )
    values = json.loads(result.stdout.decode("utf-8-sig"))
    return values if isinstance(values, list) else [values]


class SHFILEINFOW(ctypes.Structure):
    _fields_ = [("hIcon", wintypes.HICON), ("iIcon", ctypes.c_int),
                ("dwAttributes", wintypes.DWORD), ("szDisplayName", wintypes.WCHAR * 260),
                ("szTypeName", wintypes.WCHAR * 80)]


class ICONINFO(ctypes.Structure):
    _fields_ = [("fIcon", wintypes.BOOL), ("xHotspot", wintypes.DWORD),
                ("yHotspot", wintypes.DWORD), ("hbmMask", wintypes.HBITMAP),
                ("hbmColor", wintypes.HBITMAP)]


class BITMAP(ctypes.Structure):
    _fields_ = [("bmType", wintypes.LONG), ("bmWidth", wintypes.LONG),
                ("bmHeight", wintypes.LONG), ("bmWidthBytes", wintypes.LONG),
                ("bmPlanes", wintypes.WORD), ("bmBitsPixel", wintypes.WORD),
                ("bmBits", ctypes.c_void_p)]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG),
                ("biHeight", wintypes.LONG), ("biPlanes", wintypes.WORD),
                ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
                ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD),
                ("biClrImportant", wintypes.DWORD)]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 1)]


class WindowsIcons:
    def __init__(self):
        self.shell = ctypes.WinDLL("shell32", use_last_error=True)
        self.user = ctypes.WinDLL("user32", use_last_error=True)
        self.gdi = ctypes.WinDLL("gdi32", use_last_error=True)
        self.user.SetProcessDPIAware()
        self.shell.SHChangeNotify.argtypes = [ctypes.c_long, ctypes.c_uint, ctypes.c_void_p, ctypes.c_void_p]
        self.shell.SHGetFileInfoW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD,
                                             ctypes.POINTER(SHFILEINFOW), ctypes.c_uint, ctypes.c_uint]
        self.shell.SHGetFileInfoW.restype = ctypes.c_size_t
        self.user.GetIconInfo.argtypes = [wintypes.HICON, ctypes.POINTER(ICONINFO)]
        self.user.GetIconInfo.restype = wintypes.BOOL
        self.user.DestroyIcon.argtypes = [wintypes.HICON]
        self.user.DrawIconEx.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int, wintypes.HICON,
                                         ctypes.c_int, ctypes.c_int, ctypes.c_uint, wintypes.HBRUSH, ctypes.c_uint]
        self.user.DrawIconEx.restype = wintypes.BOOL
        self.gdi.GetObjectW.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p]
        self.gdi.GetObjectW.restype = ctypes.c_int
        self.gdi.CreateCompatibleDC.argtypes = [wintypes.HDC]
        self.gdi.CreateCompatibleDC.restype = wintypes.HDC
        self.gdi.CreateDIBSection.argtypes = [wintypes.HDC, ctypes.POINTER(BITMAPINFO), ctypes.c_uint,
                                             ctypes.POINTER(ctypes.c_void_p), wintypes.HANDLE, wintypes.DWORD]
        self.gdi.CreateDIBSection.restype = wintypes.HBITMAP
        self.gdi.SelectObject.argtypes = [wintypes.HDC, wintypes.HANDLE]
        self.gdi.SelectObject.restype = wintypes.HANDLE
        self.gdi.DeleteObject.argtypes = [wintypes.HANDLE]
        self.gdi.DeleteDC.argtypes = [wintypes.HDC]

    def shell_icon(self, path, *, refresh=False, shortcut_overlay=False):
        if refresh:
            changed = ctypes.create_unicode_buffer(str(path))
            # Refresh only this shortcut, then request its icon from the Windows shell.
            self.shell.SHChangeNotify(0x00002000, 0x0005 | 0x1000, ctypes.cast(changed, ctypes.c_void_p), None)
        info = SHFILEINFOW()
        # Windows adds a link arrow to .lnk icons automatically. Request the same
        # standard overlay on the supplied ICO reference, using identical shell scaling.
        flags = 0x100 | (0x8000 if shortcut_overlay else 0)
        if not self.shell.SHGetFileInfoW(str(path), 0, ctypes.byref(info), ctypes.sizeof(info), flags):
            raise ctypes.WinError(ctypes.get_last_error())
        return info.hIcon

    def dimensions(self, icon):
        info = ICONINFO()
        if not self.user.GetIconInfo(icon, ctypes.byref(info)):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            bitmap = BITMAP()
            if not self.gdi.GetObjectW(info.hbmColor or info.hbmMask, ctypes.sizeof(bitmap), ctypes.byref(bitmap)):
                raise ctypes.WinError(ctypes.get_last_error())
            return bitmap.bmWidth, bitmap.bmHeight if info.hbmColor else bitmap.bmHeight // 2
        finally:
            for handle in (info.hbmColor, info.hbmMask):
                if handle:
                    self.gdi.DeleteObject(handle)

    def render(self, icon, width, height):
        from PIL import Image
        dc = self.gdi.CreateCompatibleDC(None)
        info = BITMAPINFO()
        info.bmiHeader = BITMAPINFOHEADER(ctypes.sizeof(BITMAPINFOHEADER), width, -height, 1, 32, 0,
                                        width * height * 4, 0, 0, 0, 0)
        bits = ctypes.c_void_p()
        bitmap = self.gdi.CreateDIBSection(dc, ctypes.byref(info), 0, ctypes.byref(bits), None, 0)
        if not bitmap or not bits.value:
            self.gdi.DeleteDC(dc)
            raise ctypes.WinError(ctypes.get_last_error())
        previous = self.gdi.SelectObject(dc, bitmap)
        try:
            ctypes.memset(bits, 255, width * height * 4)
            if not self.user.DrawIconEx(dc, 0, 0, icon, width, height, 0, None, 3):
                raise ctypes.WinError(ctypes.get_last_error())
            data = ctypes.string_at(bits, width * height * 4)
            return Image.frombytes("RGBA", (width, height), data, "raw", "BGRA").convert("RGB")
        finally:
            self.gdi.SelectObject(dc, previous)
            self.gdi.DeleteObject(bitmap)
            self.gdi.DeleteDC(dc)


def verify(shortcuts, executable, source_icon, source_svg, output):
    from PIL import Image, ImageChops
    if os.name != "nt":
        raise RuntimeError("Run this check with Windows Python")
    output.mkdir(parents=True, exist_ok=True)
    installed_icon = executable.parent / "_internal/ui/assets/globe-orbit.ico"
    installed_svg = executable.parent / "_internal/ui/assets/globe-orbit.svg"
    assert sha256(installed_icon) == sha256(source_icon), "Installed ICO differs from source asset"
    assert sha256(installed_svg) == sha256(source_svg), "Installed SVG differs from the user-supplied SVG"
    with Image.open(source_icon) as ico:
        sizes = sorted(ico.ico.sizes())
    windows = WindowsIcons()
    checks = []
    for index, link in enumerate(shortcut_metadata(shortcuts)):
        assert Path(link["TargetPath"]) == executable, "Shortcut target differs from installed executable"
        location, icon_index = link["IconLocation"].rsplit(",", 1)
        assert Path(location.strip('"')) == installed_icon and int(icon_index) == 0, "Shortcut icon location mismatch"
        actual = windows.shell_icon(Path(link["Path"]), refresh=True)
        expected = None
        try:
            width, height = windows.dimensions(actual)
            assert (width, height) in sizes, "Shell icon has no corresponding source ICO frame"
            expected = windows.shell_icon(source_icon, shortcut_overlay=True)
            assert windows.dimensions(expected) == (width, height), "Shell reference icon size mismatch"
            actual_image = windows.render(actual, width, height)
            expected_image = windows.render(expected, width, height)
            actual_image.save(output / f"shortcut-{index + 1}-shell-icon.png")
            expected_image.save(output / f"source-icon-{width}x{height}.png")
            difference = ImageChops.difference(actual_image, expected_image)
            max_difference = max(value[1] for value in difference.getextrema())
            checks.append({**link, "native_shell_icon_size": [width, height],
                           "maximum_rgb_difference": max_difference, "matches_supplied_icon": max_difference == 0})
        finally:
            windows.user.DestroyIcon(actual)
            if expected:
                windows.user.DestroyIcon(expected)
    report = {"source_svg_sha256": sha256(source_svg), "installed_svg_matches_original": True,
              "source_ico_sha256": sha256(source_icon), "installed_ico_matches_source": True,
              "source_ico_sizes": sizes, "standard_windows_shortcut_overlay_included": True,
              "comparison_reference": "Windows shell rendering of the supplied ICO with its standard link arrow",
              "shortcuts": checks}
    (output / "desktop-icon-report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False), flush=True)
    assert all(value["matches_supplied_icon"] for value in checks), "Windows shell icon does not match the supplied ICO"


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--shortcut", type=Path, action="append", required=True)
    parser.add_argument("--exe", type=Path, required=True)
    parser.add_argument("--icon", type=Path, required=True)
    parser.add_argument("--svg", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    verify([path.resolve(strict=True) for path in args.shortcut], args.exe.resolve(strict=True),
           args.icon.resolve(strict=True), args.svg.resolve(strict=True), args.output.resolve())
