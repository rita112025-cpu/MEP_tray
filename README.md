# MEP Tray

MEP 電纜橋架智慧設計與衝突偵測工具（Python）：3D 正交路由、避障、共幹與分支、吊架間距、五套規範取最嚴格值檢查、碰撞與淨距、DXF／DWG 與 Revit 系統族匯入檔輸出、版本管理與比對、HTML 報告，並提供本機網頁介面。`revit/` 是 Revit Cable Tray 系統族建模匯入器（不是 .rfa）。

> 規範值未驗證，不得視為合規；零問題不代表合規。
> 本報告僅涵蓋本工具已實作之檢查項，不等同於完整法規合規審查。

## 快速開始（PowerShell 5.1）

```powershell
python -m pip install -r requirements.txt
python -m mep_tray.webui
```

開啟終端機印出的網址即可操作。

## 版本

- 目前版本：v0.4.0（2026-10-09，首個 tag；發布說明：https://github.com/rita112025-cpu/MEP_tray/releases/tag/v0.4.0 ）。版本號格式與發布流程見 [RELEASE](docs/RELEASE.md)。
- 發布版本號不代表任何規範或 Revit／AutoCAD 實機驗收已完成；驗證狀態以 [VERIFICATION](docs/VERIFICATION.md) 為準。

## 文件

- [安裝（INSTALL）](docs/INSTALL.md)：需求、測試分級、常見問題。
- [使用手冊（MANUAL）](docs/MANUAL.md)：操作、障礙物格式、錯誤碼、報告讀法、規範表與限制。
- [架構（ARCHITECTURE）](docs/ARCHITECTURE.md)：資料流、決定性、安全邊界、擴充點。
- [驗證狀態（VERIFICATION）](docs/VERIFICATION.md)：Revit／AutoCAD 實機驗證紀錄（歷史紀錄）。
- [交接（HANDOFF）](docs/HANDOFF.md)：早期交接與 benchmark（歷史紀錄）。
- [Git 工作流（GIT_WORKFLOW）](docs/GIT_WORKFLOW.md)：分支與合併 gate。
- [發布流程（RELEASE）](docs/RELEASE.md)：release gate、CI 範圍、版本號、changelog、簽章與 WDAC。

Revit 只驗證到 2025.5：建立與儲存重開，以及 AutoRun 的端點／Comments 讀回與專案基準點非零 XY 平移；另已驗證 Shared Coordinates 基準（東西、南北、高程平移與 30° 真北旋轉，單一直線段）；模型自帶的 origin 平移與繞 Z 軸旋轉（含三通、彎頭）也已驗證；PBP 旋轉、PBP Z 位移、Survey Point、旋轉後的四通／union 接頭與 Revit 2027 為未驗證或不支援，詳見 VERIFICATION。
