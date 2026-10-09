> **歷史紀錄。** 現況以 [MANUAL 第 8 節](MANUAL.md) 與 [INSTALL](INSTALL.md) 為準；本檔日期之後的 A–E 輪新增了網頁介面、版本管理與報告，下文凡寫「沒有 Web UI／未 commit／未 push」者已過時。

# Revit / C# Verification Gate

本輪只驗證與修正既有路徑，不新增產品功能。所有下表 runtime 狀態與 build 分開；Python baseline 122 tests 與第一次 benchmark 留在 HANDOFF。

## Revit 2025.5 座標 oracle：PBP 非零位移（stage 4 slice 1，2026-10-09）

**更正**：下節 `i_pbp` 的 PASS 只證明 PROJECT_BASE_POINT 路徑可 commit；全新樣板的 Project Base Point 位於內部原點（本輪讀回 PositionMm=(0,0,0)），offset 為 0，因此該場景無法鑑別平移是否正確。

本輪新增：

- AutoRun 若見 `<模型名>.setup.json`，匯入前以獨立 Transaction 套用（`{"move_pbp_mm":[dx,dy,dz]}` → 必要時解除釘選後 `ElementTransformUtils.MoveElement` 平移 PBP），讀回位移與要求差 > 0.5 mm 即失敗並寫 `*.setup_error.txt`、不匯入該場景。
- Inspection 新增 `Coordinates`：PBP 與 Survey Point 的 Position／SharedPosition（mm）、Pinned，以及 ActiveProjectLocation 在內部原點的 ProjectPosition。
- 新場景 `i2_pbp_moved`：PROJECT_BASE_POINT 基準、PBP 移動 (5000, −3000, 0) mm（不動 Z）。
- `tests/test_revit_live.py`：實機測試（`revit` marker）斷言全部 12 場景的預期結果，以及「讀回端點 = 模型點 + 讀回 PBP 位置」（容差 0.5 mm；接頭端被 fitting 修剪不比對）；另有不需 Revit 的 setup 檔產生／驗證單元測試在預設 pytest 執行。

Revit 2025 實機結果（25.5.0.57，`python -m tests.revit_live output\revit2025_coord_oracle`，證據 `output/revit2025_coord_oracle/models/`）：

| 項目 | 讀回值 |
|---|---|
| 移動前 PBP Position／SharedPosition | (0, 0, 0)／(0, 0, 0)，Pinned=false |
| 移動後 PBP Position | (5000, −3000, 0) mm（moved_mm 與要求完全一致） |
| 移動後 PBP SharedPosition | (5000, −3000, 0) mm |
| Survey Point Position／SharedPosition | 移動前後皆 (0, 0, 0) |
| ActiveProjectPosition（Default Site，內部原點處） | EW=0、NS=0、Elev=0、Angle=0，移動前後不變 |
| i2_pbp_moved 橋架 S001 | 模型 (1000,1000,3000)→(10000,1000,3000)；讀回 (6000,−2000,3000)→(15000,−2000,3000)，誤差 < 1e-9 mm |
| 其餘 11 場景 | 結果與下節表相同（i_pbp offset 0） |

- 人工步驟：未簽署的建置會讓 Revit 跳出「Security - Unsigned Add-In」對話框，須由使用者按「Load Once」AutoRun 才會繼續；未以登錄檔或信任設定繞過。
- `pytest -m revit` 結果：`14 passed, 582 deselected in 90.46s`（13 項實機 AutoRun 斷言 + 1 項既有 C# SelfTest；Revit 實機輸出 `output/revit2025_coord_oracle_pytest/models/`；兩輪結束後皆無殘留 Revit.exe）
- 未驗證：PBP 旋轉（Angle≠0）、Survey Point 移動與 SHARED_COORDINATES 基準、Z 位移與 Level 互動、Revit 2027；Revit 2020.2 起 PBP 已無 clipped/unclipped 切換，本輪僅驗證 MoveElement 移動 PBP（不移動模型）的行為。

## Revit 2025.5 AutoRun acceptance update (2026-10-09)

本輪以 AutoRun 入口（`tests/revit_live.py`，Revit 2025 實機 25.5.0.57）執行全部 10 場景，`autorun.done` 產生、無 `revit_error.txt`、無殘留 Revit 程序。報告存於 `output/revit2025_autorun/`。本次新增驗證，取代先前的 Inspection=null UNVERIFIED 狀態：

| 場景 | 結果 | 證據 |
|---|---|---|
| a_tee | PASS — Committed、3 trays、tee OK、Inspection 回讀 | tray id 813370 起 |
| b_elbow | PASS — 2 trays、elbow OK、端點 mm 與 Comments 回讀正確 | S001(1000,1000,3000) 等 |
| c_unspecified | PASS（拒絕）— UNSPECIFIED 無覆寫正確 abort、未建元件 | Abort 訊息 |
| ov_d_unspecified | PASS — 以 INTERNAL_ORIGIN 覆寫後 committed | ov_ 前綴覆寫路徑 |
| e_missing_type | PASS（拒絕）— 列出可用 CableTrayType 後 abort、未建元件 | 可用型別清單 |
| f_rollback | PASS（回滾）— 過短線段（<1/10 in）transaction 回滾、Committed=false | ArgumentException 記錄 |
| g_cross | PASS — 4 trays、cross OK | tray id 4 筆 |
| h_union | PASS — 2 trays、union OK（先前 UNVERIFIED） | — |
| i_pbp | PASS — PROJECT_BASE_POINT 基準 committed、1 tray（先前 UNVERIFIED） | Basis=PROJECT_BASE_POINT |
| j_shared | PASS（拒絕）— SHARED_COORDINATES 明確拒絕、未建元件 | Abort 訊息 |
| k_unsupported | 依設計 — 非正交腿 joint 標 FAIL、trays 建立、Committed=true | topology matrix 定義行為 |

- 未驗證維持：Revit 2027（本機未安裝 `C:\Program Files\Autodesk\Revit 2027`）、數值精度僅以場景端點抽驗（誤差 < 0.05 mm）、存檔重開持久化仍依 2026-10-03 人工驗收。
- C# runtime 前次 BLOCKED（0x800711C7）已解除：SelfTest 44 項 ALL PASS、`pytest -m revit` 1 passed（2026-10-09，詳 HANDOFF 階段 1 紀錄；Code Integrity 政策未變更，無解除 OS policy 行為）。

## Revit 2025.5 manual acceptance update (2026-10-03)

- Installed / Add-in Load / Command Invocation: PASS on Revit 25.5.0.57.
- Straight / Elbow / Tee / Cross creation and transaction commit: PASS. The four local reports have Committed=true, Abort=null, Phase=done and Warnings=[]; fitting results are OK.
- Missing CableTrayType error path: PASS. The probe aborted during resolve with Committed=false and no created elements.
- Save, close and reopen: PASS (human confirmation and 3D screenshot). The user confirmed the trays and fittings remain after reopening, without reimporting. Local `output/revit2025_gui/acceptance.rvt` exists (8,495,104 bytes). No API comparison of element counts or values was performed.
- Inspection=null: numerical dimensions, endpoint coordinates and Comments readback remain UNVERIFIED. Project Base Point, union, other error/rollback paths and Revit 2027 remain UNVERIFIED.
- Installed Revit runtimeconfig specifies net10.0; the installed add-in targets net10.0-windows. The independent Core SelfTest Code Integrity BLOCKED evidence remains separate from successful Revit imports.
- Fixtures use the document's exact existing type name and the existing routing/exporter. `python -m tests.generate_revit_acceptance --type-name '<exact document type>' --run-id '<new output directory>'` preserves existing output files.

The individual cases and original gate below retain their historical status. This update supersedes their earlier pending statements for the acceptance scope listed above.

## VERIFIED

### 2026-10-03：Cross 實機匯入

使用者回報成功，且已讀取 `output/revit2025_gui/tray_cross.json.revit_report.json`：Committed=true、Abort=null、Phase=done、CreatedTrays=[S001=586386,S002=586387,S003=586388,S004=586389]、J001 Kind=cross Status=OK、Warnings=[]。Cross 的 4 段橋架及 1 個接頭建立／提交 PASS。Straight／Elbow／Tee／Cross 四種建立路徑均已有本機 Revit 2025.5 報告證據；Inspection=null，尺寸／端點讀回及儲存重開仍 UNVERIFIED。

### 2026-10-03：Tee 實機匯入

使用者回報成功，且已讀取 `output/revit2025_gui/tray_tee.json.revit_report.json`：Committed=true、Abort=null、Phase=done、CreatedTrays=[S001=586380,S002=586381,S003=586382]、J001 Kind=tee Status=OK、Warnings=[]。Tee 的 3 段橋架及 1 個接頭建立／提交 PASS。Inspection=null，尺寸／端點讀回及儲存重開仍 UNVERIFIED。Cross 尚待測。

### 2026-10-03：Elbow 實機匯入

使用者提供可見彎頭的平面圖，且已讀取 `output/revit2025_gui/tray_elbow.json.revit_report.json`：Committed=true、Abort=null、Phase=done、CreatedTrays=[S001=586357,S002=586358]、J001 Kind=elbow Status=OK、Warnings=[]。Elbow 的 2 段橋架及 1 個接頭建立／提交 PASS，平面圖可見；尺寸／端點讀回及儲存重開仍 UNVERIFIED。Tee／Cross 尚待測。

### 2026-10-03：Straight 實機匯入

使用者提供 Revit 2025.5 結果視窗，並已讀取 `output/revit2025_gui/tray_straight.json.revit_report.json`：Committed=true、Abort=null、Phase=done、Basis=INTERNAL_ORIGIN、CreatedTrays=[S001=586316]、Joints=[]、Warnings=[]。Straight 建立及 transaction commit PASS；0/0 接頭為該案例預期。Inspection=null，因此實體尺寸／端點讀回、視覺位置確認與存檔重開仍 UNVERIFIED。上方 IN PROGRESS 為整體功能驗收狀態。

最終 Python **134 passed**、AutoCAD marker **4 passed**、compileall PASS、C# Core net8/net10 / SelfTest net10 / Revit add-in net10-windows **BUILD PASS**（各 0 warnings、0 errors）。每條命令、exit code、stdout/stderr 與 benchmark 原始輸出存於 [regression.json](verification/regression.json)。

## BLOCKED — 0x800711C7

證據存於 [runtime.json](verification/runtime.json)，包含 Get-Item / Format-List *、Authenticode、SHA256、streams、原始 stdout/stderr bytes（base64）與篩選至本 repo 的 Code Integrity XML。

- .NET host：`C:\Users\user\AppData\Local\Microsoft\dotnet-sdk10\dotnet.exe`；Authenticode Valid；--info、--list-runtimes、--list-sdks 均 exit 0。
- 最終被擋 dependency：`D:\github\MEP_tray\revit\MepTray.Core.SelfTest\bin\Release\net10.0\MepTray.Core.dll`；NotSigned；SHA256 `088EF74CBE395D62F35D5444F69326F51804E63D551957AA853DBEFBF4DA3C4E`。
- SelfTest apphost 與 SelfTest DLL：NotSigned；各 SHA256 見 evidence。直接 host 執行 DLL 與原始 dotnet run 都 exit **3762504530**（0xE0434352，未處理 managed exception）；內部 FileLoadException HRESULT **0x800711C7**。兩個值不可混為同一 exit code。
- Code Integrity 3077／3033 明確指向 Core DLL；policy ID `{0283ac0f-fff1-49ae-ada1-8a933130cad6}`。前一輪政策也擋過 SelfTest DLL；依 binary 版本分開保存，不把前一輪訊息當本輪結果。
- Host、SelfTest EXE／DLL、Core DLL 均只有 `:$DATA` stream，**沒有 Zone.Identifier**；未執行 Unblock-File，也未改政策、簽章或降低測試。
- SelfTest deps 只有 SelfTest 與 Core project，**沒有 RevitAPI dependency**。本次無證據指向 Revit API 或 dotnet host 被擋；Core 被擋後無法進一步推斷其他 Revit runtime dependency。

結論：**C# BUILD VERIFIED / C# RUNTIME BLOCKED BY ENVIRONMENT**。診斷工具的 exit 0 僅代表收集成功；各 invocation 的 exit code 才是執行結果。

## Revit capability matrix

Static PASS 只表示已讀 interface/caller/source 並確認該範圍的防護與接線；不代表執行成功。GUI UNVERIFIED 均屬 HUMAN TEST PENDING。Revit GUI 欄的 PASS 僅限 Revit 2025.5（25.5.0.57）。SelfTest 欄為 2026-10-03 狀態；2026-10-09 SelfTest 44 項 ALL PASS 見上節。

| 功能（明確範圍） | Static | Build | SelfTest | Revit GUI |
|---|---|---|---|---|
| Coordinate transform：PROJECT_BASE_POINT 非零位置平移（PBP 移動 (5000, −3000, 0) mm，僅 XY、Angle=0） | PASS | PASS | BLOCKED | PASS — Revit 2025.5 AutoRun（2026-10-09，`i2_pbp_moved` 讀回端點 = 模型點 + PBP 位置，誤差 < 1e-9 mm；`pytest -m revit` 14 passed；未簽署建置須人工按「Load Once」） |
| Coordinate transform：SHARED_COORDINATES／UNSPECIFIED 基準拒絕 | PASS | PASS | BLOCKED | PASS（拒絕）— AutoRun 2026-10-09 `j_shared`、`c_unspecified` 未建元件 |
| Coordinate transform：PBP 旋轉（Angle≠0）、PBP Z 位移與 Level 互動 | NOT SUPPORTED（只平移、不套用旋轉） | — | — | UNVERIFIED |
| Coordinate transform：Survey Point 移動／SHARED_COORDINATES 基準匯入 | NOT SUPPORTED（依設計拒絕） | — | — | UNVERIFIED |
| Coordinate transform：模型 origin 非零、非標準 axis、rotation_deg 非零拒絕 | PASS（拒絕） | PASS | BLOCKED | UNVERIFIED |
| Revit 2027（任何功能） | — | — | — | UNVERIFIED（本機未安裝） |
| Duplicate fitting detection：重複 joint ID／重複線段引用；不宣稱幾何全面去重 | PASS | PASS | BLOCKED | UNVERIFIED |
| Segment reference validation：存在性、段數、null、唯一 ID | PASS | PASS | BLOCKED | UNVERIFIED |
| Routing export：Python 實際輸出／C# 模型與 Comments 契約 | PASS | PASS | BLOCKED | UNVERIFIED |
| Persistence：Revit 文件儲存及重新開啟 | NOT APPLICABLE | NOT APPLICABLE | NOT APPLICABLE | PASS (human observation; no API readback) |

Python export/roundtrip 已執行通過；C# Cross-language assertions 已 build、尚未執行。C# connector 是否吻合接頭座標於 MakeFitting 檢查，但完整 JSON 拓樸幾何一致性（線段端點／接頭位置）不是 Core Validate 已全面保證的能力。

## Coordinate scope / NOT SUPPORTED

- Revit internal：mm→ft，直接使用模型座標。
- Project Base Point：mm→ft 後加上 `BasePoint.Position`；只做位置平移，保持 internal axes，不套用旋轉。非零 XY 平移已於 Revit 2025.5 AutoRun 驗證 PASS（2026-10-09，見上方座標 oracle 節）；PBP 旋轉與 Z 位移 UNVERIFIED。
- Survey Point：NOT SUPPORTED，程式未取得 Survey Point。
- Shared Coordinates：NOT SUPPORTED；輸入此 basis 時 Import 回報尚未實作，未進 transaction。
- Link transform：NOT SUPPORTED，沒有 RevitLinkInstance / GetTransform caller。
- 模型 origin 非零、非標準 axis 或 rotation_deg 非零：拒絕，不能忽略變換。

前兩者已實作；Revit 2025.5 AutoRun 已驗證 PBP 非零 XY 平移（Angle=0、Z 不動），其餘座標變換與 Revit 2027 仍 UNVERIFIED。README、未指定 basis 的選擇對話框與匯入結果 UI 均明確揭露此限制。

## Nested null regression matrix

| 路徑 | 防護／測試 | Runtime |
|---|---|---|
| null model／coordinate_system／tray／collections | Core SelfTest 實際 JSON null mutations，Validate 回 errors | BLOCKED |
| null collection element／start／end／joint point／joint segments／axis/origin vector | Core SelfTest mutations；拒絕而非解參考 | BLOCKED |
| empty segments／empty tee segment refs | SelfTest 拒絕；空 optional joints/findings/disclosures 可接受 | BLOCKED |
| null document／ActiveUIDocument | Import／ImportCommand 提前回報沒有文件；需實際 Revit 測試 | UNVERIFIED |
| null element | 使用實際 collector/API 建立結果；未虛構 API 必然返回 null | UNVERIFIED |
| null connector manager／connector | ConnectorAt guards；空集合找不到 connector → fitting FAIL report | UNVERIFIED |
| null location | Inspect 要求 LocationCurve，否則明確 InvalidDataException | UNVERIFIED |
| null transform | 本實作沒有使用 Transform 物件（僅 XYZ offset） | NOT APPLICABLE |
| empty Level／CableTrayType collections | Import 明確 abort；Revit API 場景仍待測 | UNVERIFIED |

未建立假的 Revit API 替身來宣稱實機 PASS。C# `Validate` 回錯誤後 callers 不再解參考 malformed model。

## Topology matrix

`tests/test_revit_topology.py` 實際窮舉每個場景的線段排列及逐段 A→B / B→A，**10 passed**；tee 另查 branch 幾何與參數次序。

| 場景 | 預期 endpoint classification | Python |
|---|---|---|
| straight | 無接頭 | PASS |
| elbow | elbow | PASS |
| tee | tee、branch 最後 | PASS |
| cross | 每軸反向腿的 cross | PASS |
| touching endpoint | 共線接觸為 union | PASS |
| parallel nearby | 無共用端點、無接頭 | PASS |
| collinear overlapping | 同端點重疊腿為 unsupported | PASS |
| nearly-collinear | 非正交腿為 unsupported | PASS |
| duplicate segment | 兩端 unsupported | PASS |
| reversed duplicate segment | 與同向 duplicate 一致 | PASS |

這是 endpoint topology classifier，不是任意線段交集演算法；不同端點的 interior overlap/intersection 不會自動切段。Revit fittings 的建立／旋轉／實體碰撞仍 UNVERIFIED。

## AutoCAD timeout / crash / exception cleanup

`taskkill /F /T /PID <recorded PID>` 只針對本次啟動行程，沒有 `/IM acad.exe` 或全域終止。檢查 taskkill exit，fallback proc.kill 後 wait；清理失敗記入 note，不再吞例外宣稱完成。非零 exit 不解析為 audit PASS，也不發布 partial DWG。

四項實際 integration PASS：正常 AutoCAD 稽核／DWG；真實 AutoCAD 0.01s timeout 的自建 PID 結束及暫存目錄刪除；自建 Python helper 父子 process timeout 與 partial DWG/lock 清理；自建 helper exit 3 與 partial files 清理。最後兩项驗證 process-control 路徑，**不是宣稱真實 AutoCAD crash 測試**。wait exception / killer failure 為 unit regression。

真實 AutoCAD crash、未知 detached descendants 仍 UNVERIFIED。已退出父行程的未知子程序不能僅憑程式名稱任意 kill。Revit GUI 人工測試也不會清理使用者既有 AutoCAD/Revit。

## Benchmark

固定輸入與 baseline 保留，沒有做效能優化。每次輸出 case、room/cell、endpoints/obstacles、runtime、Python peak allocation、segments、length、geometry check count；計時結束後驗證端點、正交性、總長度、無幾何 clash 及三組期望長度 9/35/179m。結果見 regression.json；無 canvas/FPS，記憶體不是 RSS。

## 在允許執行的 Windows 主機重跑

以此 checkout 根目錄執行（本機實際根目錄為 `D:\github\MEP_tray`），需 .NET SDK 10；Revit add-in build 另需本機 Revit API。

```powershell
Set-Location D:\github\MEP_tray
$gateDotnet = python -c "from tests.dotnet_util import find_dotnet; print(find_dotnet())"
& $gateDotnet --info
& $gateDotnet --list-runtimes
& $gateDotnet --list-sdks
& $gateDotnet build revit/MepTray.Core -c Release
& $gateDotnet build revit/MepTray.Core.SelfTest -c Release
& $gateDotnet build revit/MepTrayImport -c Release

# 產生同一份真實 Python routing/export 模型及預期 Comments，沒有 mock PASS。
python -c "from pathlib import Path; from tests.test_revit_dotnet import _build_model; p=Path('.handoff-temp/runtime-inputs'); p.mkdir(parents=True,exist_ok=True); _build_model(p)"
& $gateDotnet .\revit\MepTray.Core.SelfTest\bin\Release\net10.0\MepTray.Core.SelfTest.dll .\.handoff-temp\runtime-inputs\m.json .\.handoff-temp\runtime-inputs\expected.json
& $gateDotnet run --project .\revit\MepTray.Core.SelfTest -c Release --no-build -- .\.handoff-temp\runtime-inputs\m.json .\.handoff-temp\runtime-inputs\expected.json

# 標準 gate assertions：不是只看 console 字樣。
python -m pytest -m revit
python -m pytest
python -m pytest -m autocad
python -m compileall -q mep_tray tests
python -m tests.benchmark
git diff --check
```

若執行仍遭政策阻擋，保存 command、exit/stdout/stderr 與 Code Integrity 證據，標 BLOCKED；不要改 assertion、skip runtime gate、Unblock-File 或解除 OS policy。

## HUMAN TEST PENDING?????????

在可載入此外掛的 Revit 專用測試專案，依序確認：無開啟文件錯誤；正常匯入；取消及未指定 basis；Internal Origin／PBP 位置對照；不存在 type/level；短線段 transaction rollback；tee/cross/elbow/union connector 結果；Comments；儲存專案、關閉、重開讀回；報告輸出失敗。保存 Revit version/build、模型與結果，不能以 build 或 autorun.done 替代 assertions。此輪未啟動 Revit GUI。
