"""檢查結果的共用資料結構。to_dict() 為下游（報告/DXF 標註/Revit）的穩定介面。

狀態一律輸出 ASCII 代碼（status/indicative/kind）與中文顯示標籤（*_label）分開；
改文案不會破壞下游。座標與數值於輸出時四捨五入到固定位數，避免浮點雜訊。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .geometry import Vec
from .rules import LABELS, Check

SCHEMA_VERSION = 1


def _r(v: float, n: int = 3) -> float:
    """固定位數的 float；+0.0 正規化（避免 -0.0 與 int 造成下游比對雜訊）。"""
    return round(float(v), n) + 0.0


@dataclass
class Finding:
    kind: str                 # clash|clearance|structure|headroom|hanger_span|fill|bend
    status: str               # CLASH | FAIL | UNVERIFIED | PASS
    indicative: str           # 與規範值直接比較之結果（CLASH/PASS/FAIL）
    subject: str              # 對象（障礙物名稱/橋架段/房間面）
    location: Vec             # 問題位置 (m)
    actual: float
    required: float
    unit: str
    code: str                 # 決定要求的規範代號（無規範依據時為空字串）
    clause: str
    verified: bool
    suggestion: str = ""
    fix: dict | None = None   # 機器可讀修正：{"axis","sign","move_mm"}（橋架移動量）

    def to_dict(self) -> dict:
        return {
            "kind": self.kind, "status": self.status, "status_label": LABELS[self.status],
            "indicative": self.indicative, "indicative_label": LABELS[self.indicative],
            "subject": self.subject, "location": [_r(v) for v in self.location],
            "actual": _r(self.actual), "required": _r(self.required),
            "unit": self.unit, "code": self.code, "clause": self.clause,
            "verified": self.verified, "suggestion": self.suggestion,
            "fix": None if self.fix is None else dict(self.fix, move_mm=_r(self.fix["move_mm"], 1)),
        }


def from_check(kind: str, subject: str, loc: Vec, chk: Check, suggestion: str = "",
               fix: dict | None = None) -> Finding:
    return Finding(kind, chk.status, chk.indicative, subject, loc, chk.actual, chk.required,
                   chk.unit, chk.code, chk.clause, chk.verified, suggestion, fix)


@dataclass
class Report:
    findings: list = field(default_factory=list)   # 需處理：CLASH + indicative=FAIL
    checks: list = field(default_factory=list)     # 全部已驗算項（含 PASS/UNVERIFIED），供合規表
    design: dict = field(default_factory=dict)     # 採用的設計值與揭露事項

    def push(self, f: Finding) -> None:
        self.checks.append(f)
        if f.status == "CLASH" or f.indicative == "FAIL":
            self.findings.append(f)

    @property
    def has_clash(self) -> bool:
        return any(f.kind == "clash" for f in self.findings)

    def to_dict(self) -> dict:
        return {"schema_version": SCHEMA_VERSION,
                "findings": [f.to_dict() for f in self.findings],
                "checks": [f.to_dict() for f in self.checks], "design": self.design}
