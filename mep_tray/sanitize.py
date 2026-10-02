"""集中式脫敏：把會被交給下載者、存進版本資料或顯示在網頁的文字裡的本機路徑去識別化。

替換順序（由具體到一般，不可調換）：
1. 已知根目錄：輸出根目錄 → <OUT>；暫存目錄 → <TMP>；使用者家目錄 → <HOME>
   （不分大小寫，涵蓋 / 與 \\ 兩種分隔字元及 resolve 後的形式）
2. 引號內的絕對路徑（可含空格，含 Python repr 的雙反斜線，例如 'C:\\\\Program Files\\\\x.exe'）→ <PATH>
3. 以副檔名結尾、各段可含空格的絕對路徑 → <PATH>
4. 其餘到空白為止的絕對路徑（含 UNC、POSIX）→ <PATH>
所有可能含路徑的外洩點（RunResult 錯誤訊息、dwg_note、OSError 文字、manifest、報告、網頁）都應經由此函式。
"""
from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path

from .paths import output_root

_SEP = r"[\\/]"
_DRIVE = r"[A-Za-z]:" + _SEP
_UNC = r"\\\\[^\s\\/]+" + _SEP
_Q_PATH = re.compile(r"(['\"])(?:" + _DRIVE + "|" + _UNC + r").*?\1")
_EXT_PATH = re.compile(r"(?<![\w<])" + _DRIVE + r"(?:[^\\/:*?\"<>|\r\n]+" + _SEP + r")*"
                       r"[^\\/:*?\"<>|\r\n]+?\.[A-Za-z0-9]{1,5}\b")
_WIN_ABS = re.compile(r"(?<![\w<])(?:" + _DRIVE + "|" + _UNC + r")[^\s'\"<>|*?]*")
_POSIX_ABS = re.compile(r"(?<![\w<:/])/(?:home|Users|tmp|var|private|root|mnt)/[^\s'\"<>|*?]*")


def _variants(p: Path) -> list[str]:
    out = set()
    for q in (p, p.resolve()):
        s = str(q)
        out |= {s, s.replace("\\", "/"), s.replace("/", "\\")}
    return sorted((v for v in out if v), key=len, reverse=True)


def _replace_ci(text: str, needle: str, repl: str) -> str:
    return re.sub(re.escape(needle), lambda m: repl, text, flags=re.IGNORECASE)


def sanitize_text(text: str) -> str:
    if not isinstance(text, str) or not text:
        return text
    for base, label in ((output_root(), "<OUT>"), (Path(tempfile.gettempdir()), "<TMP>"), (Path.home(), "<HOME>")):
        for v in _variants(base):
            text = _replace_ci(text, v, label)
    text = _Q_PATH.sub("<PATH>", text)
    text = _EXT_PATH.sub("<PATH>", text)
    text = _WIN_ABS.sub("<PATH>", text)
    if os.name != "nt":
        text = _POSIX_ABS.sub("<PATH>", text)
    return text


def relative_to_output(path: Path | str | None) -> str | None:
    """檔案路徑 → 相對輸出根目錄的 posix 路徑（<run_id>/檔名）；不在輸出根目錄內時只留檔名。"""
    if path is None:
        return None
    p = Path(path)
    try:
        return p.resolve().relative_to(output_root()).as_posix()
    except (ValueError, OSError):
        return p.name


def scrub(obj):
    """遞迴脫敏：字串經 sanitize_text，dict/list/tuple 逐項處理（tuple 轉 list），其餘原樣。"""
    if isinstance(obj, str):
        return sanitize_text(obj)
    if isinstance(obj, dict):
        return {k: scrub(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [scrub(v) for v in obj]
    return obj
