"""Render the supplied globe-orbit SVG into transparent PNG and Windows ICO assets."""
from __future__ import annotations

import argparse
import os
import shutil
from pathlib import Path

from PIL import Image
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "ui" / "assets"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, help="Original SVG; copied without modifying its paths or colors")
    args = parser.parse_args()
    ASSETS.mkdir(parents=True, exist_ok=True)
    vector = ASSETS / "globe-orbit.svg"
    if args.source and args.source.resolve() != vector.resolve():
        shutil.copyfile(args.source.resolve(strict=True), vector)
    svg = vector.read_text(encoding="utf-8")
    png = ASSETS / "globe-orbit.png"
    with sync_playwright() as driver:
        browser = driver.chromium.launch(channel=os.environ.get("STARMAP_TEST_BROWSER", "msedge"), headless=True)
        page = browser.new_page(viewport={"width": 512, "height": 512}, device_scale_factor=1)
        page.set_content('<html><body style="margin:0;background:transparent">' + svg + "</body></html>")
        page.screenshot(path=png, omit_background=True)
        browser.close()
    with Image.open(png) as picture:
        assert picture.mode == "RGBA" and picture.size == (512, 512)
        picture.save(ASSETS / "globe-orbit.ico", format="ICO", sizes=[(n, n) for n in (16, 20, 24, 32, 40, 48, 64, 128, 256)])
    print("Created globe-orbit.svg, globe-orbit.png and a 9-size globe-orbit.ico")


if __name__ == "__main__":
    main()
