> **歷史紀錄。** 現況以 [MANUAL 第 8 節](MANUAL.md) 與 [INSTALL](INSTALL.md) 為準；本檔日期之後的 A–E 輪新增了網頁介面、版本管理與報告，下文凡寫「沒有 Web UI／未 commit／未 push」者已過時。

# Revit manual acceptance update - 2026-10-03 (Asia/Taipei)

Revit 2025.5 (25.5.0.57): installed add-in load, command invocation, Straight / Elbow / Tee / Cross creation and commit PASS. Four local reports have no Abort or warnings and fitting results are OK. Missing CableTrayType correctly aborts without creating elements.

The user saved, closed and reopened the model, confirmed trays and fittings remain, and supplied a 3D screenshot: persistence human acceptance PASS. Local `output/revit2025_gui/acceptance.rvt` is not included in Git. Inspection=null: numerical dimensions, endpoints, Comments and before/after element counts remain UNVERIFIED. Project Base Point, additional negative cases and Revit 2027 remain UNVERIFIED. Independent Core SelfTest retains its prior Code Integrity BLOCKED evidence.

See [VERIFICATION.md](VERIFICATION.md). The following is historical takeover and gate evidence.

# 接手紀錄 — 2026-10-02（Asia/Taipei）

## 本輪 Revit / C# Verification Gate（最新狀態）

- **VERIFIED**：Python 134 passed、5 deselected，10.62 秒；AutoCAD integration 4 passed、135 deselected，7.95 秒；compileall、Core net8/net10、SelfTest net10、Revit add-in net10-windows 全部 build PASS。
- **BLOCKED**：最終 C# runtime。`dotnet --info`／`--list-runtimes`／`--list-sdks` exit 0；直接 DLL 與原始 `dotnet run` exit 3762504530，FileLoadException 0x800711C7 明確指向 `MepTray.Core.dll`。Code Integrity 3077 事件確認相同 dependency 違反 policy。前一輪 SelfTest DLL 被擋為歷史證據，不混同最終 binary。
- **HUMAN TEST PENDING**：Revit GUI、實際 connector/element/location null 路徑、儲存／重開／持久化。
- **NOT SUPPORTED**：Shared Coordinates、Survey Point、Link transform、任意座標旋轉。Internal Origin／Project Base Point 位置平移已實作，但 runtime 未驗證。
- 修正：topology 的重疊／近共線／重複／反向重複線段錯誤分類；C# nested null 與命令前置驗證；AutoCAD 非零 exit 不發布半成品、timeout fallback kill 後 wait、例外清理自建 PID 並揭露未確認項目。
- Topology matrix 10 場景，所有排序與 A→B／B→A 變體通過。C# null regression 已 build，因政策阻擋尚未執行；未降低任何 assertion。
- AutoCAD cleanup 實測涵蓋真實 accoreconsole timeout、自建 helper 父子 process timeout、自建 helper 異常 exit 的 partial DWG/lock 清理。真實 AutoCAD crash 及崩潰後仍存活的 detached child 仍 UNVERIFIED；不任意終止使用者既有程序。
- Benchmark 保留以下第一次 baseline；本輪固定場景新增端點、正交性、長度、幾何衝突 correctness checks。新結果 small 0.004301s/95,200 bytes；12 obstacles 0.514950s/8,520,688 bytes；large straight 0.041783s/669,320 bytes；線段數 1/3/1，correctness 全 PASS。
- 本輪依使用者更新的 commit gate：source/tests build VERIFIED、runtime BLOCKED BY ENVIRONMENT、GUI HUMAN TEST PENDING 可提交與推送；前一輪「因 runtime blocking failure 不 commit」為歷史決策，已被本輪授權取代。

詳見 [VERIFICATION.md](VERIFICATION.md)、[runtime evidence](verification/runtime.json)、[regression commands/results](verification/regression.json)。

以下為第一次接手的歷史盤點與 baseline，非本輪最終結論。

## 已確認完成

- 接手 branch：master；HEAD：973004a93bd15c784eb30453bfcc132805858f78。
- 既有 commit 已包含 Python 路由、避障、共幹、吊架、規範/幾何檢查、DXF、AutoCAD 稽核與轉檔。
- 本輪 Python 全套：122 passed、2 deselected，10.38 秒。
- 本輪真實 AutoCAD 整合：1 passed、123 deselected，6.78 秒。測試確認稽核為 0、DWG AC1032 檔頭與圖面揭露。
- Revit 外掛 Release 建置成功，0 warnings、0 errors；輸出目標 net10.0-windows。此項僅證明編譯成功。

## 已實作但尚未驗證

- Revit Python 模型輸出與 C# 匯入器：Python export tests 已通過；C# 執行與 Revit GUI 仍 UNVERIFIED。
- 新增 C# regression checks：拒絕未支援的座標變換、重複 joint ID 與重複線段引用；已編譯，尚未執行成功。
- `tests/revit_live.py` 的實機場景存在，但並無 `tests/test_revit_live.py` caller；不是 pytest 已覆蓋項目。

## 未完成

- Web UI、API、使用者操作入口不存在；未從缺乏規格的情況擅自建立。
- Revit shared coordinates 明確未實作。
- Revit GUI 啟動、選檔、主要互動、錯誤路徑、儲存/重開與持久化仍待實機驗證。

## 潛在阻擋

- 原始 pytest：52 passed、69 errors；Windows sandbox 無法存取既有 pytest 暫存目錄。改用 workspace 暫存並經允許解除沙箱後，得到可用測試結果。
- C# SelfTest 建置成功但 DLL 執行被 Windows application control 拒絕，FileLoadException 0x800711C7。這不是 assertion failure，也不是 PASS；不變更 OS 安全政策以規避限制。
- 最終 `pytest -m revit`：1 failed、123 deselected，3.29 秒；失败发生在 DLL 載入階段，regression assertions 未執行。
- C# 模型驗證仍需額外處理 JSON 的巢狀 null、接頭端點與拓樸一致性；Revit 命令亦需驗證無開啟專案的錯誤路徑。
- AutoCAD timeout 分支仍有吞掉 kill-tree 錯誤、fallback kill 後未再次 wait 及過度宣稱行程樹已終止的風險；現有假行程測試不能證明真實逾時子行程已全部清理。
- 未找到 README、CLAUDE.md、docs/spec/plans 或 AGENTS.md 的既有專案規格；本輪 README 與此紀錄為新補文件。

## Working tree 未提交內容

接手時：修改 `.gitignore`、`mep_tray/acad.py`、`mep_tray/disclosure.py`、`tests/test_acad.py`；未追蹤 `mep_tray/export_revit.py`、`revit/`、`tests/dotnet_util.py`、`tests/revit_live.py`、`tests/test_export_revit.py`、`tests/test_revit_dotnet.py`。未 reset/clean/stash/restore；所有既有來源檔與產物保留。

本輪局部修改：修正十字接頭必須兩軸各有反向腿、揭露改為 UNVERIFIED、C# 模型驗證與 regression checks；新增 README、此紀錄及可重跑 benchmark。`.gitignore` 新增 bin/obj 與本輪暫存目錄排除，不刪除實際產物。

## 現有測試結果

| 類型 | 本輪證據 |
|---|---|
| Typecheck | 無 TypeScript；未設定 Python 靜態型別檢查器 |
| Python unit/export/regression/security | 122 passed；包含路徑拒絕、symlink escape、文字控制碼與覆寫防護 |
| Integration | 真實 AutoCAD 1 passed；C# 跨語言執行受 OS 政策阻擋 |
| Build | Python compileall 成功；C# Core/SelfTest 建置成功；Revit addin 建置成功 |
| Browser | 無 Web UI；BROWSER HUMAN TEST PENDING；Revit GUI HUMAN TEST PENDING |

ENGINE VERIFIED（限本輪 Python 測試範圍）；API NOT IMPLEMENTED。

## Benchmark

實際執行 `python -m tests.benchmark`，單次測量且 tracemalloc 開啟；Windows 11 10.0.26300、Python 3.12.10、AMD64、Intel64 Family 6 Model 198 Stepping 2。記憶體為 Python allocation peak，非 process RSS；無 canvas，FPS 不適用。

| Dataset | room m | cell m | ends / obstacles | runtime s | Python peak bytes |
|---|---|---|---|---|---|
| small | 12×6×4 | 0.25 | 1 / 0 | 0.004599 | 94,752 |
| obstacles | 20×12×6 | 0.25 | 2 / 12 | 0.521139 | 8,521,096 |
| large straight | 200×12×6 | 0.5 | 1 / 0 | 0.040249 | 668,168 |

這不是最壞情境效能保證；大型直線案例不代表大型複雜障礙場景。小場景 correctness 由現有路由/避障測試驗證。

## Git

Origin：https://github.com/rita112025-cpu/MEP_tray.git。只讀 `git ls-remote origin refs/heads/master` 本輪確認 remote SHA 與接手 HEAD 一致：973004a93bd15c784eb30453bfcc132805858f78。

因 C# execution verification 尚有 blocking failure，未 commit、未 push。沒有改寫 history 或操作其他 branch；`git diff --check` 已通過。
