# -*- mode: python ; coding: utf-8 -*-
"""Windows x64 portable package. Build from this directory with PyInstaller."""
import json
import os
import ssl
import sys
from importlib.metadata import PackageNotFoundError
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_delvewheel_libs_directory, collect_submodules, copy_metadata

root = Path(SPECPATH).resolve()
datas = [(str(root / "ui"), "ui"), (str(root / "core" / "data"), "core/data")]
repository = os.environ.get("STARMAP_RELEASE_REPOSITORY")
if repository:
    sys.path.insert(0, str(root))
    try:
        from core.updates import validate_repository
        channel = Path(workpath) / "update-channel.json"
        channel.write_text(json.dumps({"repository": validate_repository(repository)}), encoding="utf-8")
        datas.append((str(channel), "core"))
    finally:
        sys.path.pop(0)
# Some Windows Python builds enforce an additional rejected-CA list in ssl.py.
# Preserve the runtime's rejection policy instead of patching or disabling verification.
ssl_py = Path(ssl.__file__)
rejected_cas = ssl_py.with_name("codex-rejected-cas.pem")
if rejected_cas.is_file():
    datas.append((str(rejected_cas), "."))
elif rejected_cas.name in ssl_py.read_text(encoding="utf-8"):
    raise RuntimeError("The build Python requires its rejected-CA bundle; refusing a broken TLS package.")
# Keep the license notices distributed with the actual Python runtime and wheels.
python_license = Path(sys.base_prefix) / "LICENSE.txt"
if python_license.is_file():
    datas.append((str(python_license), "licenses/python"))
for dependency in ("affine", "attrs", "bottle", "certifi", "cffi", "charset-normalizer", "click", "clr-loader",
                   "h5py", "idna", "numpy", "proxy-tools", "pycparser", "pyinstaller", "pyproj", "pyshp",
                   "pythonnet", "pywebview", "rasterio", "requests", "shapely", "urllib3"):
    try:
        datas += copy_metadata(dependency)
    except PackageNotFoundError:
        pass  # Optional, platform-dependent transitive packages may not be installed.
datas += collect_data_files("rasterio", includes=["gdal_data/**", "proj_data/**"])
datas += collect_data_files("webview", subdir="js")
datas, binaries = collect_delvewheel_libs_directory("rasterio", datas=datas)
a = Analysis([str(root / "app.py")], pathex=[str(root)], binaries=binaries, datas=datas,
             hiddenimports=["webview.platforms.winforms", "webview.platforms.edgechromium"] + collect_submodules("rasterio"),
             excludes=["tkinter", "PyQt5", "PyQt6", "PySide2", "PySide6"], noarchive=False)
pyz = PYZ(a.pure)
application = EXE(pyz, a.scripts, [], exclude_binaries=True, name="StarmapDownloader", icon=str(root / "ui" / "assets" / "globe-orbit.ico"), console=os.environ.get("STARMAP_CONSOLE") == "1", upx=False)
coll = COLLECT(application, a.binaries, a.datas, upx=False, name="StarmapDownloader")
