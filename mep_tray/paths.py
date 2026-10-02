"""輸出路徑與檔名消毒。UI 下載區只會經由本模組取得檔案，避免 ../、絕對路徑與怪異檔名。

輸出目錄：<專案根>/output/<run_id>/（可由環境變數 MEP_OUTPUT_ROOT 覆寫，供測試/部署）。
"""
from __future__ import annotations

import os
import re
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
RUN_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,80}$")
ALLOWED_EXT = {".dxf", ".dwg", ".json", ".html", ".txt", ".py", ".md", ".csv"}


def output_root() -> Path:
    return Path(os.environ.get("MEP_OUTPUT_ROOT") or PROJECT_ROOT / "output").resolve()


def check_run_id(run_id: str) -> str:
    if not isinstance(run_id, str) or not RUN_ID_RE.match(run_id):
        raise ValueError(f"不合法的 run_id: {run_id!r}（僅允許英數、底線、連字號，最長 40）")
    return run_id


def _inside(root: Path, p: Path) -> Path:
    p = p.resolve()
    if root != p and root not in p.parents:
        raise ValueError(f"路徑逸出輸出目錄: {p}")
    return p


def run_dir(run_id: str, create: bool = True) -> Path:
    root = output_root()
    d = _inside(root, root / check_run_id(run_id))
    if create:
        d.mkdir(parents=True, exist_ok=True)
    return d


def out_path(run_id: str, name: str) -> Path:
    """輸出檔完整路徑；檔名只允許 [A-Za-z0-9._-]、副檔名白名單、不得含目錄成分。"""
    if not isinstance(name, str) or not NAME_RE.match(name) or name.startswith(".") or ".." in name:
        raise ValueError(f"不合法的檔名: {name!r}")
    if Path(name).suffix.lower() not in ALLOWED_EXT:
        raise ValueError(f"不允許的副檔名: {name!r}")
    d = run_dir(run_id)
    return _inside(d, d / name)
