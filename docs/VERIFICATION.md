> **歷史紀錄。** 現況以 [MANUAL 第 8 節](MANUAL.md) 與 [INSTALL](INSTALL.md) 為準；本檔日期之後的 A–E 輪新增了網頁介面、版本管理與報告，下文凡寫「沒有 Web UI／未 commit／未 push」者已過時。

# Revit / C# Verification Gate

本輪只驗證與修正既有路徑，不新增產品功能。所有下表 runtime 狀態與 build 分開；Python baseline 122 tests 與第一次 benchmark 留在 HANDOFF。

## Revit 2025.5 模型自帶 origin 與繞 Z 旋轉（stage 4 slice 3，2026-10-10）

語意：`basis 座標 = origin + Rz(rotation_deg)·模型點`（mm，逆時針為正），basis 座標再依 INTERNAL_ORIGIN／PROJECT_BASE_POINT／SHARED_COORDINATES 換成 Revit 內部座標。`schema_version` 維持 1：origin 0、rotation 0 的舊檔意義不變，非零值以前就被匯入器拒絕，所以沒有舊檔語意被改變，也不需要遷移說明。

- C# `ModelLoader.Validate`：origin 與 rotation_deg 須為有限數；`axis_x=(cosθ,sinθ,0)`、`axis_y=(−sinθ,cosθ,0)`、`axis_z=(0,0,1)` 須在 1e-6 內與 rotation_deg 一致，否則拒絕（不能忽略任何變換）。`ModelLoader.ToBasis` 為共同換算；共用座標的自我檢查改比對換算後的 basis 座標。
- Python `export_revit.coordinate_system`／`build_model` 新增 `origin_mm`、`rotation_deg`（預設 0），軸向量由角度導出。**管線與 manifest 尚未傳入**，目前僅 Python API；網頁入口留給 slice 4。
- Core SelfTest 新增：軸與角度不一致、非繞 Z 軸、NaN／長度錯誤的 origin、無限大角度被拒絕；origin 平移加 30°／90°／−45°／180° 且軸一致時通過，`ToBasis` 與 origin + Rz·點相符。`pytest -m revit tests/test_revit_dotnet.py`：1 passed。
- 新實機場景（Revit 2025.5 AutoRun）：

| 場景 | 設定 | 結果 |
|---|---|---|
| `l1_local_rot90_tee` | INTERNAL_ORIGIN，origin (2000, −1000, 500)，90° | Committed、tee OK、fitting 1；S001 模型 (1000,1000,3000)→ 內部 (1000, 0, 3500)，XY 旋轉 90.0° |
| `l2_local_rot30_elbow` | INTERNAL_ORIGIN，origin (−3000, 4000, 0)，30° | Committed、elbow OK、fitting 1；兩段 XY 旋轉 30.0°（內部方向非軸向） |
| `l3_shared_local_rot45_tee` | SHARED_COORDINATES，共用座標位移 (12000, −7000, 1500) 加真北 30°，origin (500, 500, 0)，區域旋轉 45° | Committed、tee OK、fitting 1；`GetProjectPosition` 讀回 S001 起點共用座標 (500, 1914.214, 3000) = origin + R45·(1000, 1000)；內部 XY 旋轉 15.0°（45° − 30°） |

- 端點比對（非接頭端）：Revit 讀回內部座標（INTERNAL）或 `GetProjectPosition` 共用座標（SHARED）與 `origin + Rz·模型點` 差皆 ≤ 0.5 mm。
- `pytest -m revit`：18 passed（47.15 s）；結束後無殘留 `Revit.exe`，`%APPDATA%` 的 AutoRun 清單已移除。證據：`output/revit2025_local_frame/models/`（未納入 Git）。
- 環境事故（與程式無關）：前兩次重跑因 Revit 彈出「AddInId 重複」模態對話框（`TaskDialog_External_Tools_Duplicate_ClientId`，只有「Close」）而逾時；原因是使用者安裝目錄同時有 `MepTray.addin` 與舊的 `MepTray.Validation.addin`（同一 AddInId）。使用者把後者改名為 `.bak` 後通過。再有第二份同 ID 的清單，實機測試會再次卡住。
- **未驗證**：旋轉後的四通（cross）與 union 接頭、非 Z 軸旋轉（不支援，會被拒絕）、PBP 自身旋轉、Survey Point 移動、連結檔共用座標、Revit 2027。管線／網頁尚未提供 origin／rotation 入口。

## Revit 2025.5 共用座標（stage 4 slice 2，2026-10-10）

`basis = SHARED_COORDINATES` 不再被拒絕：匯入器以 `doc.ActiveProjectLocation.GetTotalTransform()` 把模型點（mm，視為共用座標 EastWest／NorthSouth／Elevation）換成 Revit 內部座標，Z 軸旋轉隨真北角度。進 Transaction 前有自我檢查：把每個端點與接頭換算後的內部點交給 `GetProjectPosition` 讀回共用座標，與模型點差超過 0.5 mm 即中止、不動文件。

- **方向由實機決定，不是推導**：第一次實測以 `.Inverse` 換算，自我檢查讀回 (25000, −13000, 6000) mm（模型點 (1000, 1000, 3000)），代表方向相反而被中止；去掉 `.Inverse` 後通過。`GetTotalTransform()` 本身即共用 → 內部。
- AutoRun 新增 setup 鍵 `set_project_position`（`ProjectLocation.SetProjectPosition(原點, …)`），匯入前設定並讀回。新場景：`j1_shared_translate`（EW 12000、NS −7000、Elev 1500 mm，Angle=0）與 `j2_shared_rotated`（同位移、Angle=30°）；原 `j_shared`（驗證拒絕）已移除。
- Inspection 新增每段橋架端點的 `SharedStartMm`／`SharedEndMm`（`GetProjectPosition` 讀回）。

| 檢查 | j1（Angle=0） | j2（Angle=30°） |
|---|---|---|
| 設定後 ActiveProjectPosition 讀回 | EW 12000、NS −7000、Elev 1500、Angle 0 | 同位移、Angle 30° |
| PBP Position／SharedPosition（Revit 讀回） | (0,0,0)／(12000, −7000, 1500) | 同左 |
| 模型 S001 | (1000,1000,3000)→(10000,1000,3000) | 同左 |
| 橋架內部座標（讀回） | (−11000, 8000, 1500)→(−2000, 8000, 1500) | (−5526.279, 12428.203, 1500)→(2267.949, 7928.203, 1500) |
| 端點共用座標（GetProjectPosition 讀回） | 等於模型點 | 等於模型點 |
| 模型方向 → 內部方向的 XY 旋轉 | 0° | −30.0°（內部座標 = R(−30°)·(模型點 − PBP 共用座標)，與獨立計算相符） |

- 獨立錨點：端點到 PBP 的距離與高差，等於模型點到 PBP 共用座標的距離與高差（PBP 內部／共用座標由 Revit 讀回，不經匯入器換算）。
- 結果：`pytest -m revit` 15 passed（117.91 s，含 2 個新場景測試）；Revit 結束後無殘留 `Revit.exe`。證據：`output/revit2025_shared/models/`（未納入 Git）。
- 事故紀錄：第一次實測 Revit 在 `Document.Close` 發生存取違規（`0xc0000005`，coreclr.dll），原因懷疑為未釋放的 `ProjectPosition`／`ProjectLocation` 物件；加上 `using` 後未再發生。**根因未做獨立驗證**，只能說加上釋放後的 2 次實機執行（含本次）未再崩潰。
- 測試收尾原本在 `autorun.done` 後立即檢查 Revit 是否結束，Revit 退出需數秒而偶發誤判；改為輪詢最多 30 秒（`wait_revit_exit`）。
- **未驗證**：旋轉後的四通／union 接頭（j1、j2 只有單一直線段；slice 3 已補驗三通與彎頭）、Project Base Point 本身有旋轉或 Survey Point 被移動、從連結檔取得共用座標、PBP 不在內部原點時的共用座標、Revit 2027。模型自帶的原點與旋轉仍被拒絕（第 3 刀）。

## develop 合併前完整 gate（2026-10-09，commit fbb5dde）

對象：`origin/develop` 於 PR #8 合併後（`fbb5dde`），在獨立 worktree 執行，準備併入 `master`（PR #5）。環境：Windows 11、Python 3.12、.NET SDK 10、本機 AutoCAD（accoreconsole）、Revit 2025。

| 項目 | 結果 |
|---|---|
| `git diff --check` | 通過 |
| `python -m compileall -q mep_tray tests` | 通過 |
| C# `MepTray.Core`／`MepTray.Core.SelfTest` Release 建置 | 0 警告、0 錯誤 |
| C# SelfTest 執行（net10.0，Python 實際匯出的模型） | ALL PASS，exit 0 |
| `python -m pytest -q` | 579 passed、1 failed、1 skipped、19 deselected（見下） |
| `python -m pytest -m slow` | 1 passed |
| `python -m pytest -m autocad` | 4 passed |
| `python -m pytest -m revit` | 14 passed（74.56 s）；Revit 結束後無殘留 `Revit.exe` |

- 預設 pytest 的 1 個失敗為 `tests/test_webui.py::test_content_type_must_be_json_but_charset_parameter_is_fine`，錯誤 `ConnectionAbortedError`（測試用 socket 連線被中止）。單獨重跑 6 次皆通過，`tests/test_webui.py` 整檔重跑 159 passed、1 skipped。判定為偶發；**根因未查明**，未修改測試，也不視為已解決。
- Revit 實機部分的 add-in 未簽署，依先前紀錄可能需人工按「Load Once」；本次是否出現該對話框未另行記錄。
- 未執行：Revit 2027（本機未安裝）。
- 本檔其餘章節（capability matrix、座標範圍、未驗證清單）未因本次 gate 改變；PBP 旋轉、Z 位移、Survey Point、Shared Coordinates 仍為未驗證／不支援。

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
| Coordinate transform：UNSPECIFIED 基準拒絕 | PASS | PASS | BLOCKED | PASS（拒絕）— AutoRun 2026-10-09 `c_unspecified` 未建元件 |
| Coordinate transform：SHARED_COORDINATES 平移（EW／NS／Elev）與 30° 真北旋轉，單一直線段 | PASS | PASS | — | PASS — Revit 2025.5 AutoRun（2026-10-10，`j1_shared_translate`、`j2_shared_rotated`；`GetProjectPosition` 讀回端點共用座標 = 模型點；`pytest -m revit` 15 passed） |
| Coordinate transform：旋轉後的三通、彎頭接頭（模型 origin + 繞 Z 旋轉；INTERNAL 30°／90°，SHARED 疊加 45° 後淨旋轉 15°） | PASS | PASS | — | PASS — Revit 2025.5 AutoRun（2026-10-10，`l1_local_rot90_tee`、`l2_local_rot30_elbow`、`l3_shared_local_rot45_tee`：tee／elbow Status=OK）；`pytest -m revit` 18 passed |
| Coordinate transform：旋轉後的四通（cross）與 union 接頭 | PASS | PASS | — | UNVERIFIED |
| Coordinate transform：PBP 旋轉（Angle≠0）、PBP Z 位移與 Level 互動 | NOT SUPPORTED（只平移、不套用旋轉） | — | — | UNVERIFIED |
| Coordinate transform：Survey Point 移動、從連結檔取得的共用座標 | NOT SUPPORTED | — | — | UNVERIFIED |
| Coordinate transform：模型自帶 origin 平移 + 繞 Z 軸旋轉（INTERNAL_ORIGIN／SHARED_COORDINATES） | PASS | PASS | PASS（2026-10-10 SelfTest，含 30°／90°／−45°／180° 的 ToBasis） | PASS — Revit 2025.5 AutoRun（2026-10-10，`l1`／`l2`／`l3`，端點誤差 ≤ 0.5 mm） |
| Coordinate transform：模型 axis 與 rotation_deg 不一致、非繞 Z 軸、非有限數值拒絕 | PASS（拒絕） | PASS | PASS（2026-10-10 SelfTest） | NOT APPLICABLE（匯入前 Validate 拒絕，以 SelfTest 驗證） |
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
- Shared Coordinates：已實作並於 Revit 2025.5 驗證（2026-10-10，見上方共用座標節）：`ActiveProjectLocation.GetTotalTransform()` 換算，進 Transaction 前以 `GetProjectPosition` 讀回自我檢查。旋轉後的接頭、Survey Point 移動、連結檔共用座標 UNVERIFIED。
- Link transform：NOT SUPPORTED，沒有 RevitLinkInstance / GetTransform caller。
- 模型 `coordinate_system`：origin 平移加繞 Z 軸旋轉已實作並驗證（2026-10-10，見上方 slice 3 節）；axis 與 rotation_deg 不一致、非繞 Z 軸、非有限數值：拒絕，不能忽略變換。

前兩者與 Shared Coordinates 已實作；Revit 2025.5 AutoRun 已驗證 PBP 非零 XY 平移（Angle=0、Z 不動）與 Shared Coordinates 平移＋30° 旋轉（單一直線段），其餘座標變換與 Revit 2027 仍 UNVERIFIED。README、未指定 basis 的選擇對話框與匯入結果 UI 均明確揭露此限制。

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
