"""版本管理：manifest、完整性檢查、列出版本、三層差異比對。

一個「版本」就是 output/<run_id>/ 資料夾；其中的 manifest.json 由管線在暫存資料夾內最後寫入、
隨整個資料夾 rename 發佈，所以「有 manifest 的資料夾」必定是完整版本。本模組沒有任何修改/刪除既有版本的函式。

雜湊（皆為 sha256，取正規化 JSON）：
- input_sha256   = 標準化輸入 + codes（去重、保序，因為並列時「勝出規範」取決於順序）+ options
- rules_sha256   = 本次實際使用的 rules 內容（正規化 JSON，不是檔案位元組）；僅供快速判斷
- rules_snapshot_sha256 = 本次「生效的 gov 快照」；規則差異以快照逐項比對（rules 改了也能回答哪個值變了）
- result_sha256  = result 區段。明確排除：run_id、所有路徑、environment、created_at、disclosures、
                   DXF/DWG/JSON 檔案位元組（有 AutoCAD 時 DXF 多一行稽核結果，DWG 帶 AutoCAD 自身時間戳）。
manifest 內所有字串先經 sanitize.scrub（脫敏）再計算雜湊，確保載入時重算一致。

【完整性檢查 ≠ 防竄改】manifest 與雜湊可被同時重算，所以這只能偵測「損壞/誤改」，不防刻意竄改，
不是簽章，也不應被當成稽核證據。manifest 內的 run_id 與檔名一律視為不可信輸入（見 load_version）。

result 區段進雜湊的 finding 欄位精確定義為 (kind, subject, code) 為鍵，值為 (actual, required, status)
（固定位數）；不含 suggestion、location、clause、disclosures。subject 不得內嵌座標或序號等不穩定文字。
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from functools import lru_cache

from . import RESULT_ALGO, __version__
from .paths import ALLOWED_EXT, NAME_RE, RUN_ID_RE, output_root, run_dir
from .sanitize import scrub

SCHEMA_VERSION = 1
MANIFEST_NAME = "manifest.json"
MAX_MANIFEST_BYTES = 5_000_000
FILE_KEYS = ("dxf", "dwg", "revit_json", "report")
# 語意：「相同輸入 + 相同 run_id」下檔案位元組是否相同（檔案內嵌 run_id，不同 run_id 本來就不同）。
DETERMINISM = {"revit_json": "same-run-id", "dxf": "same-run-id-no-cad", "dwg": "never",
               "report": "same-run-id-no-cad"}
# engine.code_sha256 = 套件內「所有 .py 扣掉下列明確的非結果模組」的原始碼雜湊（fail-safe：日後新增的模組預設就被納入，
# 不必靠人記得；model.py 的 DEFAULT_CABLE、pipeline.py 的 DEFAULT_SPAN_M、findings.py 都會影響結果，所以必須在內）。
# 版本常數放在 __init__.py 並另記於 engine.version，故 __init__.py 排除。
ENGINE_EXCLUDED = frozenset({"__init__.py", "sanitize.py", "versioning.py", "acad.py", "export_dxf.py",
                             "paths.py", "disclosure.py", "report.py", "webui.py"})
REQUIRED = ("schema_version", "run_id", "created_at", "engine", "inputs", "codes", "options",
            "rules", "result", "hashes", "files")


# ───────────────────────── 正規化與雜湊 ─────────────────────────
def _norm(o):
    """正規化供雜湊：浮點取 6 位、整數值的浮點轉 int（300.0 與 300 等價）、-0.0 → 0；拒絕 NaN/inf。"""
    if isinstance(o, bool) or o is None or isinstance(o, (int, str)):
        return o
    if isinstance(o, float):
        if not math.isfinite(o):
            raise ValueError("正規化失敗：含非有限數")
        r = round(o, 6)
        return int(r) if r == int(r) else r
    if isinstance(o, dict):
        return {str(k): _norm(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_norm(v) for v in o]
    raise TypeError(f"無法正規化的型別: {type(o).__name__}")


def canonical_json(o) -> str:
    return json.dumps(_norm(o), sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def hash_obj(o) -> str:
    return hashlib.sha256(canonical_json(o).encode("utf-8")).hexdigest()


def engine_files(base: Path) -> list[str]:
    return sorted(p.name for p in Path(base).glob("*.py") if p.name not in ENGINE_EXCLUDED)


def _code_hash(base: Path, modules) -> str:
    """模組原始碼雜湊（換行正規化為 LF，避免 git autocrlf 造成跨機器的假差異；檔名也進雜湊）。"""
    h = hashlib.sha256()
    for name in sorted(modules):
        data = (Path(base) / name).read_bytes().replace(b"\r\n", b"\n")
        h.update(name.encode("utf-8") + b"\0" + hashlib.sha256(data).digest())
    return h.hexdigest()


@lru_cache(maxsize=1)
def code_sha256() -> str:
    """影響結果的模組原始碼的雜湊，進 manifest 的 engine.code_sha256。"""
    base = Path(__file__).resolve().parent
    return _code_hash(base, engine_files(base))


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def normalize_inputs(inp) -> dict:
    """標準化輸入：與輸入順序無關的排序（路由對 ends/obstacles 本來就排序，順序不影響結果）。"""
    d = inp.to_dict()
    d.pop("codes", None)                                  # codes 另存（順序有意義）
    d["room"] = [list(d["room"][0]), list(d["room"][1])]
    d["start"] = list(d["start"])
    d["ends"] = sorted([list(e) for e in d["ends"]])
    d["obstacles"] = sorted(({"name": o["name"], "kind": o["kind"], "lo": list(o["lo"]), "hi": list(o["hi"])}
                             for o in d["obstacles"]), key=canonical_json)
    d["cables"] = sorted(({"od_mm": c["od_mm"], "count": c.get("count", 1), "kind": c.get("kind")}
                          for c in d["cables"]), key=canonical_json)
    return d


def normalize_options(type_name=None, basis="UNSPECIFIED", notes=()) -> dict:
    return {"type_name": type_name, "basis": basis, "notes": list(notes)}


def input_material(inp, codes, *, type_name=None, basis="UNSPECIFIED", notes=()) -> dict:
    return scrub({"inputs": normalize_inputs(inp), "codes": list(codes),
                  "options": normalize_options(type_name, basis, notes)})


def input_hash(inp, codes, **opts) -> str:
    return hash_obj(input_material(inp, codes, **opts))


SNAPSHOT_SOURCE_FIELDS = ("rule_id", "pdf_page", "appendix_page", "document", "section", "document_page", "strength")


def gov_snapshot(gov: dict) -> dict:
    return {k: {"label": g.label, "unit": g.unit, "direction": g.direction, "value": g.value,
                "code": g.code, "clause": g.clause, "verified": g.verified,
                "sources": [{f: src[f] for f in SNAPSHOT_SOURCE_FIELDS if f in src} for src in g.sources]} for k, g in sorted(gov.items())}


def normalized_segments(route) -> list:
    """線段 → 與序號/方向無關的正規化座標（mm，3 位）：每段兩端點先排序，再整體排序。"""
    out = []
    for a, b in route.segments:
        pa = [round(v * 1000, 3) + 0.0 for v in a]
        pb = [round(v * 1000, 3) + 0.0 for v in b]
        out.append(sorted([pa, pb]))
    return sorted(out)


def result_section(route, reports: dict, joints: int, span_m: float, span_source: str) -> dict:
    agg: dict = {}
    for rep in reports.values():
        for f in rep.findings:
            agg.setdefault((f.kind, f.subject, f.code), []).append([round(f.actual, 3), round(f.required, 3), f.status])
    findings = [{"kind": k[0], "subject": k[1], "code": k[2], "count": len(v),
                 "values": sorted(v, key=canonical_json)} for k, v in sorted(agg.items())]
    return {
        "segments": normalized_segments(route), "segment_count": len(route.segments),
        "length_m": round(route.length_m, 3), "joint_count": joints,
        "hanger_count": len(route.hangers), "hanger_span_m": span_m, "hanger_span_source": span_source,
        "bend_count": len(route.bends),
        "unverified_checks": sum(1 for rep in reports.values() for c in rep.checks if c.status == "UNVERIFIED"),
        "findings": findings,
    }


# ───────────────────────── 建立 manifest ─────────────────────────
def build_manifest(*, run_id: str, created_at: str, inp, codes, type_name=None, basis="UNSPECIFIED",
                   notes=(), gov: dict, rules: dict, route, reports: dict, joints: int,
                   span_m: float, span_source: str, files: dict, dwg_note: str = "",
                   acad_audit=None, disclosures=(), environment: dict | None = None) -> dict:
    material = input_material(inp, codes, type_name=type_name, basis=basis, notes=notes)
    snapshot = scrub(gov_snapshot(gov))
    result = scrub(result_section(route, reports, joints, span_m, span_source))
    fmeta = {}
    for key in FILE_KEYS:
        p = files.get(key)
        if p is not None:
            p = Path(p)
            fmeta[key] = {"name": p.name, "sha256": file_sha256(p), "bytes": p.stat().st_size,
                          "determinism": DETERMINISM[key]}
    return {
        "schema_version": SCHEMA_VERSION, "run_id": run_id, "created_at": created_at,
        "engine": {"version": __version__, "result_algo": RESULT_ALGO, "code_sha256": code_sha256()},
        **material,
        "rules": {"sha256": hash_obj(rules), "snapshot": snapshot},
        "result": result,
        "hashes": {"input_sha256": hash_obj(material), "rules_sha256": hash_obj(rules),
                   "rules_snapshot_sha256": hash_obj(snapshot), "result_sha256": hash_obj(result)},
        "environment": scrub(environment or {}),
        "files": fmeta,
        "disclosures": scrub(list(disclosures)), "dwg_note": scrub(dwg_note), "acad_audit": acad_audit,
    }


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def write_manifest(directory: Path, manifest: dict) -> Path:
    """寫入 manifest.json；已存在則拒絕（版本不可變）。"""
    path = Path(directory) / MANIFEST_NAME
    text = json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False)
    with open(path, "x", encoding="utf-8") as f:
        f.write(text)
    return path


# ───────────────────────── 載入與完整性驗證 ─────────────────────────
@dataclass(frozen=True)
class VersionError:
    code: str      # invalid_run_id|missing_version|missing_manifest|corrupt_manifest|unsupported_schema|
                   # hash_mismatch|missing_file|file_tampered
    message: str
    run_id: str = ""

    def to_dict(self) -> dict:
        return {"code": self.code, "message": self.message, "run_id": self.run_id}


@dataclass
class LoadResult:
    ok: bool
    manifest: dict | None = None
    error: VersionError | None = None


def _err(code, message, run_id="") -> LoadResult:
    return LoadResult(False, None, VersionError(code, message, run_id))


def _is_real_dir(p: Path) -> bool:
    return p.is_dir() and not p.is_symlink() and not os.path.isjunction(p)


def _safe_file_name(name) -> bool:
    """manifest 內的檔名是不可信輸入：必須是單純檔名（不含目錄成分）、字元白名單、副檔名白名單。"""
    return (isinstance(name, str) and bool(NAME_RE.match(name)) and not name.startswith(".") and ".." not in name
            and Path(name).suffix.lower() in ALLOWED_EXT and Path(name).name == name)


def load_version(run_id: str, *, verify_files: bool = True) -> LoadResult:
    """載入並驗證版本。任何問題都回結構化錯誤（不拋、不當作空）。"""
    try:
        d = run_dir(run_id, create=False)
    except ValueError as e:
        return _err("invalid_run_id", str(e), str(run_id))
    if not _is_real_dir(d):
        return _err("missing_version", f"版本不存在: {run_id}", run_id)
    if d.resolve().parent != output_root():                 # 必須是輸出根目錄的直接子層（run_dir 已拒絕逸出者）
        return _err("invalid_run_id", "版本資料夾不在輸出根目錄的直接子層", run_id)
    mp = d / MANIFEST_NAME
    if not mp.is_file():
        return _err("missing_manifest", "找不到 manifest.json（不是完整版本）", run_id)
    if mp.stat().st_size > MAX_MANIFEST_BYTES:
        return _err("corrupt_manifest", "manifest 過大", run_id)
    try:
        m = json.loads(mp.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, RecursionError, MemoryError) as e:
        return _err("corrupt_manifest", f"manifest 無法解析: {type(e).__name__}", run_id)
    if not isinstance(m, dict):
        return _err("corrupt_manifest", "manifest 不是物件", run_id)
    if m.get("schema_version") != SCHEMA_VERSION:
        return _err("unsupported_schema", f"不認得的 schema_version: {m.get('schema_version')!r}", run_id)
    missing = [k for k in REQUIRED if k not in m]
    if missing:
        return _err("corrupt_manifest", f"manifest 缺少欄位: {missing}", run_id)
    if m["run_id"] != run_id:
        return _err("corrupt_manifest", "manifest 內的 run_id 與資料夾名稱不符", run_id)
    try:
        h = m["hashes"]
        material = {"inputs": m["inputs"], "codes": m["codes"], "options": m["options"]}
        checks = (("input_sha256", hash_obj(material)), ("result_sha256", hash_obj(m["result"])),
                  ("rules_snapshot_sha256", hash_obj(m["rules"]["snapshot"])))
        for name, recomputed in checks:
            if h.get(name) != recomputed:
                return _err("hash_mismatch", f"{name} 與 manifest 內容不符（內容遭修改或損毀）", run_id)
        if h.get("rules_sha256") != m["rules"].get("sha256"):
            return _err("hash_mismatch", "rules_sha256 前後不一致", run_id)
    except (KeyError, TypeError, ValueError, AttributeError, RecursionError) as e:
        return _err("corrupt_manifest", f"manifest 結構不完整: {type(e).__name__}", run_id)
    if not isinstance(m["files"], dict):
        return _err("corrupt_manifest", "files 欄位不是物件", run_id)
    if verify_files:
        for key, ent in m["files"].items():
            name = ent.get("name", "") if isinstance(ent, dict) else ""
            if key not in FILE_KEYS or not _safe_file_name(name):
                return _err("corrupt_manifest", f"files 項目不合法: {key!r}", run_id)
            fp = d / name
            if not fp.is_file() or fp.is_symlink():
                return _err("missing_file", f"缺少產出檔: {name}", run_id)
            if fp.stat().st_size != ent.get("bytes") or file_sha256(fp) != ent.get("sha256"):
                return _err("file_tampered", f"產出檔內容與 manifest 不符（完整性檢查失敗，偵測損壞，不防刻意竄改）: {name}", run_id)
    return LoadResult(True, m, None)


@dataclass
class VersionInfo:
    run_id: str
    created_at: str
    input_sha256: str = ""
    result_sha256: str = ""
    error: VersionError | None = None

    def to_dict(self) -> dict:
        return {"run_id": self.run_id, "created_at": self.created_at, "input_sha256": self.input_sha256,
                "result_sha256": self.result_sha256, "error": None if self.error is None else self.error.to_dict()}


@dataclass
class ScanResult:
    versions: list
    ignored: list            # 輸出根目錄下「不是版本」的項目名稱（沒有 manifest 的資料夾、非 run_id 命名、一般檔案…）
    stage_leftovers: int     # .stage- 暫存資料夾數（通常是被中斷的執行殘留，只佔磁碟）

    def to_dict(self) -> dict:
        return {"versions": [v.to_dict() for v in self.versions], "ignored": list(self.ignored),
                "stage_leftovers": self.stage_leftovers}


def scan_versions() -> ScanResult:
    """只掃輸出根目錄的直接子項；只讀 manifest，不開啟/雜湊產出檔（列表要快；完整性檢查留給 load_version/compare 按需執行）。
    - 有 manifest.json 的 run_id 資料夾 → 列出；manifest 存在但損毀/不符 → 仍列出並帶 error（不隱藏）。
    - 其餘（沒有 manifest 的資料夾、非 run_id 命名者、檔案）→ 不列出，但名稱記在 ignored，讓使用者知道有東西沒被列出。
    - .stage- 前綴者只計數。
    排序 = (created_at, run_id)；讀不到建立時間的損毀版本 created_at 為空字串，因此排在最前面。"""
    root = output_root()
    if not root.is_dir():
        return ScanResult([], [], 0)
    out, ignored, stages = [], [], 0
    for p in sorted(root.iterdir(), key=lambda q: q.name):
        if p.name.startswith(".stage-"):
            stages += 1
            continue
        if not RUN_ID_RE.match(p.name) or not _is_real_dir(p) or not (p / MANIFEST_NAME).is_file():
            ignored.append(p.name)
            continue
        r = load_version(p.name, verify_files=False)
        if r.ok:
            m = r.manifest
            out.append(VersionInfo(p.name, str(m.get("created_at", "")), m["hashes"]["input_sha256"],
                                   m["hashes"]["result_sha256"]))
        else:
            out.append(VersionInfo(p.name, "", error=r.error))
    return ScanResult(sorted(out, key=lambda v: (v.created_at, v.run_id)), ignored, stages)


def list_versions() -> list[VersionInfo]:
    return scan_versions().versions


# ───────────────────────── run_id ─────────────────────────
def new_run_id(inp, codes, *, now: datetime | None = None, taken=None, **opts) -> str:
    """YYYYMMDD-HHMMSS-<input_sha256 前 8 碼>（24 字，符合 RUN_ID_RE、<=40）。
    同秒同輸入碰撞時決定性地加序號後綴 -2、-3…（以 taken(run_id)->bool 找第一個空的）。
    pipeline 的 run_exists 仍是最終仲裁；呼叫端遇到 run_exists 可重取。"""
    now = now or datetime.now(timezone.utc)
    base = f"{now.strftime('%Y%m%d-%H%M%S')}-{input_hash(inp, codes, **opts)[:8]}"
    if taken is None:
        def taken(rid):
            return os.path.lexists(run_dir(rid, create=False))
    rid, n = base, 1
    while taken(rid):
        n += 1
        if n > 999:
            raise RuntimeError("run_id 碰撞過多")
        rid = f"{base}-{n}"
    assert RUN_ID_RE.match(rid)
    return rid


# ───────────────────────── 差異比對 ─────────────────────────
def _flatten(o, prefix="") -> dict:
    out: dict = {}
    if isinstance(o, dict) and o:
        for k in sorted(o):
            out.update(_flatten(o[k], f"{prefix}.{k}" if prefix else str(k)))
    elif isinstance(o, list) and o:
        for i, v in enumerate(o):
            out.update(_flatten(v, f"{prefix}[{i}]"))
    else:
        out[prefix] = o
    return out


def _diff_flat(a: dict, b: dict) -> dict:
    return {"changed": {k: [a[k], b[k]] for k in sorted(a.keys() & b.keys()) if a[k] != b[k]},
            "added": {k: b[k] for k in sorted(b.keys() - a.keys())},
            "removed": {k: a[k] for k in sorted(a.keys() - b.keys())}}


def _is_empty(d: dict) -> bool:
    return not (d["changed"] or d["added"] or d["removed"])


@dataclass
class DiffResult:
    ok: bool
    error: VersionError | None = None
    source: str = ""                 # inputs | rules | both | none | engine | unexplained（none 且結果不同時才細分）
    results_equal: bool = True
    engine_changed: bool = False      # 兩版引擎（version/result_algo/code_sha256 任一）不同；獨立於 source，永遠要揭露
    engine: dict = field(default_factory=dict)     # {欄位: [舊, 新]}，只含有差異者
    inputs: dict = field(default_factory=dict)
    rules: dict = field(default_factory=dict)
    result: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"ok": self.ok, "error": None if self.error is None else self.error.to_dict(),
                "source": self.source, "results_equal": self.results_equal,
                "engine_changed": self.engine_changed, "engine": self.engine,
                "inputs": self.inputs, "rules": self.rules, "result": self.result}


def _diff_rules(a: dict, b: dict) -> dict:
    sa, sb = a["rules"]["snapshot"], b["rules"]["snapshot"]
    changed = {}
    for k in sorted(sa.keys() & sb.keys()):
        fields = {f: [sa[k].get(f), sb[k].get(f)] for f in sorted(set(sa[k]) | set(sb[k]))
                  if sa[k].get(f) != sb[k].get(f)}
        if fields:
            changed[k] = fields
    added = {k: sb[k] for k in sorted(sb.keys() - sa.keys())}
    removed = {k: sa[k] for k in sorted(sa.keys() - sb.keys())}
    effective = bool(changed or added or removed)
    hash_changed = a["rules"]["sha256"] != b["rules"]["sha256"]
    return {"effective_changed": effective, "changed": changed, "added": added, "removed": removed,
            "hash_changed": hash_changed, "hash_only_change": hash_changed and not effective}


def _seg_key(s) -> tuple:
    return tuple(tuple(p) for p in s)


def _diff_result(a: dict, b: dict) -> dict:
    ra, rb = a["result"], b["result"]
    sa, sb = {_seg_key(s) for s in ra["segments"]}, {_seg_key(s) for s in rb["segments"]}
    segs = {"added": [[list(p) for p in s] for s in sorted(sb - sa)],
            "removed": [[list(p) for p in s] for s in sorted(sa - sb)]}
    counts = {}
    for k in ("segment_count", "length_m", "joint_count", "hanger_count", "hanger_span_m",
              "hanger_span_source", "bend_count", "unverified_checks"):
        if ra.get(k) != rb.get(k):
            ent = {"before": ra.get(k), "after": rb.get(k)}
            if isinstance(ra.get(k), (int, float)) and isinstance(rb.get(k), (int, float)):
                ent["delta"] = round(rb[k] - ra[k], 3)
            counts[k] = ent
    fa = {(f["kind"], f["subject"], f["code"]): f for f in ra["findings"]}
    fb = {(f["kind"], f["subject"], f["code"]): f for f in rb["findings"]}

    def brief(f):
        return {"kind": f["kind"], "subject": f["subject"], "code": f["code"],
                "count": f["count"], "values": f["values"]}
    return {
        "segments": segs, "counts": counts,
        "findings": {
            "added": [brief(fb[k]) for k in sorted(fb.keys() - fa.keys())],
            "resolved": [brief(fa[k]) for k in sorted(fa.keys() - fb.keys())],
            "changed": [{"kind": k[0], "subject": k[1], "code": k[2],
                         "before": {"count": fa[k]["count"], "values": fa[k]["values"]},
                         "after": {"count": fb[k]["count"], "values": fb[k]["values"]}}
                        for k in sorted(fa.keys() & fb.keys())
                        if (fa[k]["count"], fa[k]["values"]) != (fb[k]["count"], fb[k]["values"])],
        },
    }


def compare_manifests(a: dict, b: dict) -> DiffResult:
    """純函式：比對兩份已驗證的 manifest。"""
    ia = {"inputs": a["inputs"], "codes": a["codes"], "options": a["options"]}
    ib = {"inputs": b["inputs"], "codes": b["codes"], "options": b["options"]}
    inputs = _diff_flat(_flatten(ia), _flatten(ib))
    rules = _diff_rules(a, b)
    result = _diff_result(a, b)
    equal = a["hashes"]["result_sha256"] == b["hashes"]["result_sha256"]
    inputs_changed = a["hashes"]["input_sha256"] != b["hashes"]["input_sha256"]
    rules_changed = rules["effective_changed"]
    if inputs_changed and rules_changed:
        source = "both"
    elif inputs_changed:
        source = "inputs"
    elif rules_changed:
        source = "rules"
    elif equal:
        source = "none"
    else:                               # 輸入與生效規則都相同、結果卻不同
        source = "engine" if a["engine"] != b["engine"] else "unexplained"
    ea, eb = a["engine"], b["engine"]
    engine = {k: [ea.get(k), eb.get(k)] for k in sorted(set(ea) | set(eb)) if ea.get(k) != eb.get(k)}
    return DiffResult(True, None, source, equal, bool(engine), engine, inputs, rules, result)


def compare(run_a: str, run_b: str) -> DiffResult:
    """載入並完整驗證兩個版本後比對；任一邊有問題→ok=False 並說明是哪一邊、為何（拒絕比對）。"""
    loaded = {}
    for side, rid in (("A", run_a), ("B", run_b)):
        r = load_version(rid)
        if not r.ok:
            e = r.error
            return DiffResult(False, VersionError(e.code, f"版本 {side}（{rid}）無法比對：{e.message}", rid))
        loaded[side] = r.manifest
    return compare_manifests(loaded["A"], loaded["B"])
