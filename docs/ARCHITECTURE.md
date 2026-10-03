# 架構（ARCHITECTURE）

> 規範值未驗證，不得視為合規；零問題不代表合規。
> 本報告僅涵蓋本工具已實作之檢查項，不等同於完整法規合規審查。
> 安裝見 [INSTALL.md](INSTALL.md)；操作見 [MANUAL.md](MANUAL.md)；驗證狀態見 [VERIFICATION.md](VERIFICATION.md)；交接見 [HANDOFF.md](HANDOFF.md)。

## 1. 資料流

```
網頁表單 / 障礙物 JSON
   │ webui.validate_request（與管線共用上限常數與 router 的網格公式）
   ▼
model.Inputs ──► rules.merge_strictest（逐參數取最嚴，記錄 Governing 與方向）
   ▼
router.route_tray（3D 正交 A*、轉彎懲罰、共用主幹、分支）──► place_hangers
   ▼
clash（碰撞／淨距）＋ compliance（吊架間距、填充率、彎曲）──► findings.Report
   ▼
export_dxf（ezdxf，R2010，mm）──► acad（accoreconsole 稽核＋DWG；ODA 備援）
export_revit（junction 主幹切割、座標基準、Comments 規則）──► 匯入 JSON ──► revit/（C# 匯入器）
   ▼
versioning.build_manifest（輸入／規則快照／結果 sha256、引擎指紋）
   ▼
report.render_report（自含 HTML，無 JS）──► pipeline._publish（暫存 → 原子 rename → output/<run_id>/）
```

`pipeline.run` 把以上串起來並回傳 `RunResult`；失敗以 `PipelineError(stage, code, message, cleanup_failed)` 表示，不留下半成品。

## 2. 模組

| 模組 | 職責 |
|---|---|
| `rules.json` / `rules.py` | 5 套規範（CNS、IEC、NEC、TW_BUILDING、MRT_APPX_C）；三態評估 PASS／FAIL／UNVERIFIED（另有 CLASH）；`validate_rules` |
| `model.py` | `Inputs`，嚴格驗證，只丟 ValueError |
| `router.py` | 路徑搜尋與預算（`MAX_OBSTACLE_WORK`、`max_cells`、`max_expansions`）、`RoutingError.code` |
| `geometry.py` | `Box`、線段與盒、最近點 |
| `clash.py` / `compliance.py` | 幾何與規範檢查，輸出 `Finding` |
| `findings.py` | `Finding`、`Report`、`SCHEMA_VERSION` |
| `export_dxf.py` / `acad.py` | DXF 輸出（固定中繼資料）；AutoCAD 稽核與 DWG |
| `export_revit.py` | Revit 匯入 JSON 與 C# 常數產生 |
| `versioning.py` | manifest、版本掃描、比對、引擎指紋 |
| `report.py` | HTML 報告與差異報告 |
| `disclosure.py` | 固定揭露文字（橫幅、範圍說明、Revit 狀態、引擎變動說明） |
| `sanitize.py` / `paths.py` | 路徑／機敏字串清理；輸出路徑與 run_id 規則 |
| `webui.py` | 本機 HTTP 服務與前端 |
| `revit/MepTray.Core`、`MepTrayImport` | C# 模型、單位、Comments 與匯入器 |

## 3. 決定性

- 相同輸入與規則、相同引擎 → 相同 `result_sha256`。雜湊排除：run_id、路徑、環境、created_at、檔案位元組。
- DXF：固定中繼資料與 GUID（由輸入衍生）；時間戳記為固定常數，不是真實建立時間。
- **非決定性**：DWG（由 AutoCAD 產生）；HTML 報告與 DXF 受執行環境影響；`created_at`。
- 引擎指紋 `code_sha256`：套件內所有 `.py`（`ENGINE_EXCLUDED` 除外）。任何程式碼變動都會改變它，因此偏向多報「引擎變動」。

## 4. 輸出與原子發布

流程：`tempfile.mkdtemp('.stage-')` → 寫檔 → `os.rename` 到 `output/<run_id>`。已存在的 run_id 一律拒絕（`run_exists`）。短暫的 PermissionError（Windows 防毒／索引器）會有限次重試。失敗時只清除通過安全檢查的暫存（根目錄直接子項、前綴符合、非連結／接合點）；清除失敗以 `cleanup_failed` 回報而不阻擋重跑。

## 5. Web 安全邊界與對應測試

| 邊界 | 做法 | 測試 |
|---|---|---|
| 僅本機 | 綁 127.0.0.1；Host 白名單 | `tests/test_webui.py` |
| 認證 | 網址路徑 token（不用 cookie） | 同上 |
| 跨站 | Origin 與 Sec-Fetch-Site 檢查 | 同上 |
| 請求本文 | Transfer-Encoding 400、Expect 417、單一純數字 Content-Length、1 MB、嚴格 JSON（拒 NaN、重複鍵、深度 >20） | 同上（原始 socket 測試） |
| 資源 | 16 個連線上限（503）、15 秒逾時、單一工作者（409） | 同上 |
| 下載 | 只允許 manifest 清單內的檔案；CSP 含 sandbox、nosniff、Content-Disposition | 同上 |
| 前端 | 一律 `textContent`，不用 innerHTML | 同上 |
| 報告 | 所有動態字串經清理、控制／雙向字元轉義、截斷、HTML escape；無 JS | `tests/test_report.py` |

這些是本機工具的防護，不是對外服務的安全設計；不要綁到非迴路位址。

## 6. 擴充點

**新增規範**：在 `rules.json` 的 `codes` 加一套（所有 `params` 都要有值與出處，`verified` 預設 false），跑 `tests/test_core.py` 的規則驗證。
**新增檢查**：在 `clash.py` 或 `compliance.py` 產生 `Finding`（需有位置、規範依據、修正建議），由 `pipeline` 併入 `Report`；補測試並確認三態語意。
**新增輸出格式**：新增 `export_*.py`，在 `pipeline` 發布前產生檔案、登錄到 manifest 的 `files`（`versioning.FILE_KEYS`）、並更新下載白名單。

## 7. 升級流程

1. 改變輸出語意（欄位、演算法）時，遞增 `RESULT_ALGO` 或 `SCHEMA_VERSION`。
2. 舊 manifest 仍須可讀；比對時遇到不同 schema 要明確回報而不是猜測。
3. 引擎指紋會自動變；比對報告靠 `engine_changed` 提醒讀者。
4. 重新執行完整驗證（見 INSTALL 第 4 節），並更新 VERIFICATION.md 的狀態。

## 8. 已知限制

完整清單見 MANUAL 第 8 節。與架構直接相關的：發現以彙總比對、不配對位置；完整性檢查不防有意竄改；單一工作者且無取消；POSIX 上 rename 可能覆寫同名空資料夾。
