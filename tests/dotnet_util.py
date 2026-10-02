import os
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOCAL = Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft"


def _major(c: str) -> int:
    r = subprocess.run([c, "--list-sdks"], capture_output=True, text=True, shell=False, timeout=60)
    if r.returncode != 0:
        return 0
    vs = [ln.split()[0] for ln in r.stdout.splitlines() if ln.strip()]
    return max((int(v.split(".")[0]) for v in vs), default=0)


def find_dotnet(min_major: int = 10) -> str | None:
    """找一個「確實有 SDK」且主版本 >= min_major 的 dotnet（只有 runtime 的不算）。
    核心專案含 net10.0 目標，因此預設需要 SDK 10；不代表特定 Revit 版本已驗證。"""
    cands = [os.environ.get("DOTNET_EXE"), str(LOCAL / "dotnet-sdk10" / "dotnet.exe"),
             str(LOCAL / "dotnet-sdk8" / "dotnet.exe"), shutil.which("dotnet")]
    for c in cands:
        if c and Path(c).is_file() and _major(c) >= min_major:
            return c
    return None


def run(args, **kw):
    env = dict(os.environ, DOTNET_CLI_TELEMETRY_OPTOUT="1", DOTNET_NOLOGO="1")
    return subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace",
                          shell=False, env=env, **kw)
