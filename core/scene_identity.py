"""Identity shared by the Sentinel Hub catalogue and Process requests."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import re

from .errors import UserError

UTC = dt.timezone.utc
SAFE_ID = re.compile(r"(S2[A-Z])_MSIL2A_(\d{8}T\d{6})_N\d+_R\d+_(T\d{2}[A-Z]{3})_\d{8}T\d{6}(?:\.SAFE)?", re.ASCII)


def utc_time(value):
    acquisition = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if acquisition.utcoffset() is None:
        raise ValueError("Missing acquisition time zone")
    return acquisition.astimezone(UTC)


def utc_text(value):
    return value.isoformat().replace("+00:00", "Z")


def expected_products(scene):
    """Return the selected SAFE identifiers, MGRS tiles and UTC granule times."""
    result = []
    for product in scene.get("products", []):
        identifier = product.get("id", "")
        parsed = SAFE_ID.fullmatch(identifier)
        if not parsed:
            raise UserError("影像目录未提供可匹配的产品标识，请重新检索。")
        platform, datatake, tile = parsed.groups()
        try:
            acquired = utc_time(product["acquisition_utc"])
        except (KeyError, TypeError, ValueError, AttributeError):
            raise UserError("影像目录未提供有效的瓦片采集时间，请重新检索。") from None
        if platform + "_" + datatake != scene.get("overpass_key", scene["key"]):
            raise UserError("所选过境与目录产品不一致，请重新检索。")
        result.append({"id": identifier.removesuffix(".SAFE"), "tile": tile, "utc": utc_text(acquired)})
    if not result or len({p["tile"] for p in result}) != len(result):
        raise UserError("所选过境缺少有效瓦片，或同一瓦片有多个版本，请重新检索。")
    return sorted(result, key=lambda p: p["id"])


def source_fingerprint(products):
    return hashlib.sha256(json.dumps(products, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:12]
