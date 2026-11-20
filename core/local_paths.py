"""Windows disk paths that remain usable when generated filenames exceed MAX_PATH."""
from __future__ import annotations

import os
from pathlib import Path


def io_path(value):
    path = Path(value).expanduser()
    if os.name != "nt" or not path.is_absolute():
        return path
    text = os.path.abspath(str(path))
    if text.startswith("\\\\?\\"):
        return Path(text)
    if text.startswith("\\\\"):
        return Path("\\\\?\\UNC\\" + text[2:])
    return Path("\\\\?\\" + text)


def display_path(value):
    text = str(value)
    if text.startswith("\\\\?\\UNC\\"):
        return "\\\\" + text[8:]
    if text.startswith("\\\\?\\"):
        return text[4:]
    return text
