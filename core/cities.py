"""Offline catalogue of full administrative boundaries on the imagery WGS84 CRS."""
from __future__ import annotations

import copy
import hashlib
import json
import re
import threading
import zipfile
from collections import OrderedDict
from pathlib import Path

from .errors import UserError
from .geometry import describe_aoi, normalize_aoi

DATA = Path(__file__).resolve().parent / "data" / "cities"


def region_scope(row):
    if row["id"] in ("810000", "820000"):
        return "完整特别行政区边界"
    if row["municipality"]:
        return "完整直辖市行政边界（含下辖县区）"
    if row["name"].endswith("省"):
        return "完整省级行政边界"
    return "完整地级行政边界（含下辖县区）"


class CityCatalogue:
    def __init__(self, root=DATA):
        self.root, self.lock, self.cache = Path(root), threading.RLock(), OrderedDict()
        self.error = None
        try:
            self.index = json.loads((self.root/"cities-index.json").read_text(encoding="utf-8"))
            if self.index.get("schema")!=1:
                raise ValueError("Unknown city catalogue schema")
            self.rows = {row["id"]: {**row, "scope": region_scope(row)} for row in self.index["cities"]}
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            self.index, self.rows = {}, {}
            self.error = "本地城市目录缺失或损坏；可修复安装，或导入完整城市 SHP。"

    def public(self):
        if self.error:
            raise UserError(self.error)
        return {"source": copy.deepcopy(self.index["source"]), "provinces": copy.deepcopy(self.index["provinces"]),
                "cities": copy.deepcopy([{key:value for key,value in row.items() if key not in ("member", "sha256")}
                                         for row in self.rows.values()])}

    def search(self, query, limit=20):
        term = str(query).strip().casefold()
        if not term:
            return []
        matches = [row for row in self.rows.values() if term in (row["province"]+row["name"]).casefold()]
        matches.sort(key=lambda row: (row["name"] not in (term, term+"市"), row["id"]))
        return copy.deepcopy(matches[:limit])

    def get(self, identifier):
        if self.error:
            raise UserError(self.error)
        if not isinstance(identifier, str) or not re.fullmatch(r"\d{6}", identifier) or identifier not in self.rows:
            raise UserError("请选择列表中的城市或州盟。")
        with self.lock:
            if identifier in self.cache:
                self.cache.move_to_end(identifier)
                return copy.deepcopy(self.cache[identifier])
            row = self.rows[identifier]
            try:
                with zipfile.ZipFile(self.root/"cities.zip") as archive:
                    member = archive.getinfo(row["member"])
                    if not 0 < member.file_size < 64*1024**2:
                        raise ValueError
                    data = archive.read(member)
                if hashlib.sha256(data).hexdigest()!=row["sha256"]:
                    raise ValueError
                geometry = normalize_aoi(json.loads(data))
                result = describe_aoi(geometry, row["name"])
                result["region"] = {"city_id": identifier, "city": row["name"], "province": row["province"],
                                    "scope": row["scope"], "municipality": row["municipality"],
                                    "geometry_sha256": row["sha256"], "source": copy.deepcopy(self.index["source"])}
                self.cache[identifier] = result
                while len(self.cache)>6:
                    self.cache.popitem(last=False)
                return copy.deepcopy(result)
            except (OSError, ValueError, KeyError, zipfile.BadZipFile):
                raise UserError("本地城市边界缺失或校验失败，请修复安装或导入自己的完整城市 SHP。") from None
