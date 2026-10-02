# Revit / C# Verification Gate

本輪只驗證與修正既有路徑，不新增產品功能。所有下表 runtime 狀態與 build 分開；Python baseline 122 tests 與第一次 benchmark 留在 HANDOFF。

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

Static PASS 只表示已讀 interface/caller/source 並確認該範圍的防護與接線；不代表執行成功。GUI UNVERIFIED 均屬 HUMAN TEST PENDING。

| 功能（明確範圍） | Static | Build | SelfTest | Revit GUI |
|---|---|---|---|---|
| Coordinate transform：內部座標／PBP 位置平移與未支援變換拒絕 | PASS | PASS | BLOCKED | UNVERIFIED |
| Duplicate fitting detection：重複 joint ID／重複線段引用；不宣稱幾何全面去重 | PASS | PASS | BLOCKED | UNVERIFIED |
| Segment reference validation：存在性、段數、null、唯一 ID | PASS | PASS | BLOCKED | UNVERIFIED |
| Routing export：Python 實際輸出／C# 模型與 Comments 契約 | PASS | PASS | BLOCKED | UNVERIFIED |
| Persistence：Revit 文件儲存及重新開啟 | NOT APPLICABLE | NOT APPLICABLE | NOT APPLICABLE | PASS (human observation; no API readback) |

Python export/roundtrip 已執行通過；C# Cross-language assertions 已 build、尚未執行。C# connector 是否吻合接頭座標於 MakeFitting 檢查，但完整 JSON 拓樸幾何一致性（線段端點／接頭位置）不是 Core Validate 已全面保證的能力。

## Coordinate scope / NOT SUPPORTED

- Revit internal：mm→ft，直接使用模型座標。
- Project Base Point：mm→ft 後加上 `BasePoint.Position`；只做位置平移，保持 internal axes，不套用旋轉。
- Survey Point：NOT SUPPORTED，程式未取得 Survey Point。
- Shared Coordinates：NOT SUPPORTED；輸入此 basis 時 Import 回報尚未實作，未進 transaction。
- Link transform：NOT SUPPORTED，沒有 RevitLinkInstance / GetTransform caller。
- 模型 origin 非零、非標準 axis 或 rotation_deg 非零：拒絕，不能忽略變換。

前兩者僅為「已實作，runtime 未驗證」。README、未指定 basis 的選擇對話框與匯入結果 UI 均明確揭露此限制。

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
