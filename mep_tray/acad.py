"""以本機 AutoCAD 的 accoreconsole.exe（無介面核心）稽核 DXF 並轉存 DWG。

- 只在偵測到 accoreconsole 時啟用；找不到或失敗一律優雅降級（回傳原因，不拋例外）。
- subprocess 用引數陣列、shell=False、有逾時；stdout 導向檔案（不用管線，避免子行程握住管線而卡死）。
- 工作目錄必須是純 ASCII 路徑（.scr 以 ANSI 讀取）；否則降級。
- 稽核結果由 AUDIT 的輸出解析（支援英文與繁中訊息）；解析不到回 None（未知，不當作 0）。
- 無任何網路呼叫。
"""
from __future__ import annotations

import glob
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

AUDIT_RE = [re.compile(r"Total errors found\s+(\d+)", re.I),
            re.compile(r"[發发]現?\s*(\d+)\s*[個个][錯错]誤?")]


@dataclass
class AcadResult:
    audit_errors: int | None      # None = 未執行或無法解析
    dwg: Path | None
    version: str                  # 例如 "AutoCAD 2027"；未知為空字串
    note: str


def find_accore() -> Path | None:
    env = os.environ.get("MEP_ACCORE_PATH")
    if env and Path(env).is_file():
        return Path(env)
    hits = sorted(glob.glob("C:/Program Files/Autodesk/AutoCAD */accoreconsole.exe"))
    return Path(hits[-1]) if hits else None


def _version(accore: Path) -> str:
    m = re.search(r"AutoCAD (\d{4})", str(accore))
    return f"AutoCAD {m.group(1)}" if m else "AutoCAD"


def _decodings(raw: bytes) -> list[str]:
    """AutoCAD 核心主控台的輸出是混合編碼（ASCII 段為 UTF-16LE、本地化文字段為 ANSI/Big5），
    故以多種方式解碼後逐一嘗試解析。"""
    nul_free = raw.replace(bytes(1), b"")
    out = [raw.decode("utf-16-le", errors="ignore")]
    for enc in ("utf-8", "cp950", "gbk"):
        out.append(nul_free.decode(enc, errors="ignore"))
    return out


def parse_audit(log_text: str) -> int | None:
    for rx in AUDIT_RE:
        m = rx.search(log_text)
        if m:
            return int(m.group(1))
    return None


def parse_audit_bytes(raw: bytes) -> int | None:
    for text in _decodings(raw):
        n = parse_audit(text)
        if n is not None:
            return n
    return None


def kill_tree(pid: int) -> None:
    """終止整棵行程樹（accoreconsole 可能衍生子行程；subprocess 逾時只會殺直接子行程）。"""
    if os.name == "nt":
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(int(pid))], shell=False,
                       capture_output=True, timeout=30, check=True)
    else:
        os.kill(int(pid), 9)


def audit_and_convert(dxf: Path, dwg_out: Path | None = None, accore: Path | None = None,
                      popen=subprocess.Popen, killer=kill_tree, timeout: int = 90) -> AcadResult:
    """popen / killer 為測試注入點；逾時依自建 PID 嘗試清理並回報未確認的清理步驟。"""
    accore = accore or find_accore()
    if accore is None:
        return AcadResult(None, None, "", "未偵測到 AutoCAD（accoreconsole.exe；可設環境變數 MEP_ACCORE_PATH）")
    ver = _version(accore)
    dxf = Path(dxf).resolve()
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        if not str(tmp).isascii():
            return AcadResult(None, None, ver, "暫存路徑含非 ASCII 字元，略過 AutoCAD 稽核/轉檔")
        src, scr, log, out = tmp / "in.dxf", tmp / "run.scr", tmp / "run.log", tmp / "out.dwg"
        shutil.copy2(dxf, src)
        lines = ["_.AUDIT", "_Y"]
        if dwg_out is not None:
            lines += ["_.SAVEAS", "2018", str(out)]
        lines += ["_.QUIT", "_Y", ""]
        scr.write_text("\n".join(lines), encoding="ascii")
        args = [str(accore), "/i", str(src), "/s", str(scr), "/l", "en-US"]      # 引數陣列
        rc = "?"
        proc = None
        try:
            with open(log, "wb") as fh:
                # cwd=暫存：AutoCAD 會在 cwd 寫 ErrorReports/；stdout 導向檔案（不用管線，避免子行程握住管線卡死）
                proc = popen(args, shell=False, stdout=fh, stderr=subprocess.DEVNULL,
                             stdin=subprocess.DEVNULL, cwd=str(tmp))
                try:
                    rc = proc.wait(timeout=timeout)
                except subprocess.TimeoutExpired:
                    cleanup = []
                    try:
                        killer(proc.pid)
                        if os.name != "nt":
                            cleanup.append("非 Windows 平台僅終止直接行程，子行程未確認")
                    except (OSError, subprocess.SubprocessError) as e:
                        cleanup.append(f"行程樹終止未確認: {e}")
                    try:
                        proc.wait(timeout=10)
                    except (OSError, subprocess.SubprocessError):
                        proc.kill()
                        proc.wait(timeout=10)
                    detail = "；".join(cleanup) if cleanup else "已依本次 PID 終止行程樹並確認直接行程結束"
                    return AcadResult(None, None, ver, f"{ver} 執行逾時（>{timeout}s）；{detail}")
        except (OSError, subprocess.SubprocessError) as e:
            cleanup = []
            if proc is not None and proc.poll() is None:
                try:
                    killer(proc.pid)
                except (OSError, subprocess.SubprocessError) as err:
                    cleanup.append(f"行程樹終止未確認: {err}")
                try:
                    proc.wait(timeout=10)
                except (OSError, subprocess.SubprocessError):
                    try:
                        proc.kill()
                        proc.wait(timeout=10)
                    except (OSError, subprocess.SubprocessError) as err:
                        cleanup.append(f"直接行程結束未確認: {err}")
            detail = ("；" + "；".join(cleanup)) if cleanup else ""
            return AcadResult(None, None, ver, f"{ver} 執行失敗: {e}{detail}")
        if rc != 0:
            return AcadResult(None, None, ver, f"{ver} 執行失敗（returncode={rc}），不發布未完成的 DWG")
        errors = parse_audit_bytes(log.read_bytes()) if log.is_file() else None
        dwg = None
        if dwg_out is not None and out.is_file() and out.stat().st_size > 0:
            shutil.copy2(out, dwg_out)
            dwg = Path(dwg_out)
        if errors is None:
            note = f"{ver} 已執行但無法解析稽核結果（returncode={rc}）"
        else:
            note = f"已由 {ver} 稽核：{errors} 個錯誤" + ("；並轉存為 DWG（2018）" if dwg else "")
        return AcadResult(errors, dwg, ver, note)
