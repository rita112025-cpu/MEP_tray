"""各匯出格式共用的揭露文字（不含「該格式是否已實機驗證」那一行，由各匯出器自行附加）。"""
from __future__ import annotations

from .model import Inputs

COORD_NOTE = "座標 = 公尺×1000 (mm)，原點 = 輸入座標系原點（不平移）"


def base_notes(inp: Inputs, gov: dict, reports=(), extra=()) -> list[str]:
    notes = [COORD_NOTE]
    n_unv = sum(1 for rep in reports for f in rep.checks if f.status == "UNVERIFIED")
    if n_unv:     # 自 checks（不只 findings）統計：零 finding 的「乾淨」結果也必須揭露
        notes.append(f"本圖有 {n_unv} 項檢查所依規範值尚未核對條文（verified=false），不得視為合規")
    bad = sorted({g.code for g in gov.values() if not g.verified})
    if bad:
        notes.append("規範值未驗證 (verified=false)：" + ", ".join(bad) + "；結果不得視為合規依據")
    if inp.cables_defaulted:
        notes.append("未輸入電纜資料，填充率以預設電纜計算")
    return notes + list(extra)


# 報告/UI 共用的固定句（單一來源；report.py、webui 都從這裡取，避免措辭漂移）。
BANNER_FIXED = "規範值未驗證，不得視為合規；零問題不代表合規。"
ENGINE_CHANGED_NOTE = "引擎版本亦不同，差異可能包含引擎變動"
# 永遠印出（含全部規範值皆已驗證時）：零 finding 的報告不能被讀成「全部合規」。不得含「不得視為合規」字樣——
# 那句只在有未驗證規範值時才出現，已驗證的結果不該被亂報。
SCOPE_NOTE = "本報告僅涵蓋本工具已實作之檢查項，不等同於完整法規合規審查。"

# 不把過往 session 的敘述當作本版本實機驗證證據。
REVIT_STATUS = ("未於 Revit 驗證（UNVERIFIED，實機操作待確認）；Revit 2025／2027 相容性尚未驗證；"
                "未提供 .rfa（Cable Tray 為系統族）。")
