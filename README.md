# MEP Tray

既有橋架路由與匯出專案：Python 核心負責 3D 正交路由、避障、共幹、吊架、幾何及規範檢查、DXF/DWG 匯出；`revit/` 提供 Cable Tray 匯入外掛。此 repo 目前沒有 Web UI、API server 或完整的使用者操作入口。

## 開發與驗證

Python 3.12，依賴列於 `requirements.txt`；測試另需 pytest。

```powershell
python -m pip install -r requirements.txt pytest
python -m pytest
python -m pytest -m autocad
python -m pytest -m revit
python -m tests.benchmark
```

預設 pytest 排除 AutoCAD/Revit 整合測試。AutoCAD 測試會實際啟動本機 accoreconsole，並確認稽核及 DWG 檔頭；Revit marker 目前只有 C# 核心建置與跨語言 SelfTest，**不代表 Revit GUI 已驗證**。Windows 沙箱若限制暫存目錄，需要可寫的測試暫存目錄及相應執行權限；不要把權限錯誤當作測試通過。

## 模組與單位

- `model.py`：輸入資料；場景座標用公尺，橋架尺寸用毫米。
- `rules.py` / `rules.json`：規範值與最嚴格合併；`verified=false` 只能回報 UNVERIFIED，不得視為合規。
- `router.py` / `geometry.py`：路由、避障、共幹及吊架。
- `clash.py` / `compliance.py`：幾何與規範檢查。
- `export_dxf.py` / `acad.py`：DXF 與本機 AutoCAD/ODA 轉檔。
- `export_revit.py`：毫米座標模型及接頭分類；`Fields.g.cs` 必須與 Python 產生器一致。
- `paths.py`：限制輸出路徑，預設為 `output/<run_id>/`，拒絕路徑逸出及預設覆寫。

## Revit 狀態

未於 Revit 驗證（UNVERIFIED，實機操作待確認）；Revit 2025／2027 相容性尚未驗證；未提供 .rfa（Cable Tray 為系統族）。

- **VERIFIED**：原始 Python 122 tests baseline 保留；本輪 134 tests 通過、AutoCAD integration 4 tests 通過、Python compileall 及三個 C# 專案 build 通過。
- **BLOCKED**：C# runtime，Code Integrity 3077 證實最終版本的 `MepTray.Core.dll` 被 OS policy 阻擋；BUILD PASS 不代表 runtime PASS。
- **HUMAN TEST PENDING**：Revit GUI、儲存／重開／持久化。
- **NOT SUPPORTED**：Shared Coordinates、Survey Point、Link transform、模型座標旋轉與任意模型原點平移。

外掛的正式入口為 `ImportCommand`，使用檔案選擇器匯入模型。需有開啟的 Revit 專案、Level 與模型指定的 CableTrayType；未指定座標基準時由使用者選擇。Shared coordinates 尚未支援；非零模型原點、非標準座標軸及旋轉會在驗證階段拒絕。

已實作的座標處理僅有：`INTERNAL_ORIGIN` 把毫米換成英呎並直接使用內部座標；`PROJECT_BASE_POINT` 在同樣換算後加上 `BasePoint.Position`，只做位置平移，保持內部軸向，不套用 Project North／True North 旋轉。兩條 Revit runtime 路徑均尚未實機驗證。此能力不是完整 Revit coordinate transform 支援。

建置需本機 .NET SDK 與 Revit API，依安裝目錄 runtimeconfig 選擇目標框架，不能用已存在的 DLL 證明新版可以執行。正式使用 `MepTray.addin`；`MepTray.AutoRun.addin` 與 `tests/revit_live.py` 僅供專用實機測試，會建立暫時外掛、啟動並關閉 Revit，不應直接安裝到日常工作環境。

本輪接手證據、測試限制與 benchmark 見 [docs/HANDOFF.md](docs/HANDOFF.md)。

本輪 verification matrix、null/topology 覆蓋範圍、blocked binary 證據及可直接重跑的 Windows 命令見 [docs/VERIFICATION.md](docs/VERIFICATION.md)。
