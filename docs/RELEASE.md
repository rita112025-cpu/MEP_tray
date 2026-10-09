# 發布流程（RELEASE）

> 適用本 repo（origin：https://github.com/rita112025-cpu/MEP_tray.git）。分支規則見 [GIT_WORKFLOW.md](GIT_WORKFLOW.md)；驗證狀態見 [VERIFICATION.md](VERIFICATION.md)；增益集安裝見 [INSTALL.md](INSTALL.md)。
> 規範值未驗證，不得視為合規；零問題不代表合規。發布版本號不代表任何規範或 Revit／AutoCAD 實機驗收已完成。

## 1. 原則

- `master` 只能經由 **Pull Request** 從 `develop`（或 `hotfix/*`）合併；不直接 push、不 force push。
- 合併 PR 前必須通過下列 **release gate**，並把結果寫進 [VERIFICATION.md](VERIFICATION.md)。沒跑到的項目必須明確寫「**未執行**（not executed）」與原因，不得省略或以 build 代替 runtime。
- CI 綠燈只代表雲端可重現的子集合；Revit／AutoCAD 實機項目只能在本機 gate 驗證。

## 2. Release gate（develop → master PR 合併前）

| # | 項目 | 執行位置 | 通過條件 |
|---|---|---|---|
| 1 | CI（`.github/workflows/ci.yml`） | GitHub Actions `windows-latest` | PR 上 CI 綠燈 |
| 2 | 完整 pytest：`python -m pytest -q`、`-m slow`、（有 AutoCAD）`-m autocad`、（有 .NET SDK 10）`-m revit` | 本機 | 全綠；無法執行者記「未執行」 |
| 3 | `python -m compileall -q mep_tray tests`、`git diff --check` | 本機 | exit 0 |
| 4 | 本機 Revit gate：`dotnet build revit/MepTrayImport -c Release`（需本機 Revit API）＋ VERIFICATION 的 AutoRun／HUMAN TEST 項目 | 本機 Revit 2025.5 | 依 [VERIFICATION.md](VERIFICATION.md) 記錄；Revit 2027 未安裝時記「未執行」 |
| 5 | VERIFICATION.md 新增本次 release 小節 | PR 內 | 日期、commit、各項 PASS／FAIL／BLOCKED／未執行 |

CI 涵蓋範圍（細節見 workflow 檔頭註解）：

- 預設 pytest（`pytest.ini` 排除 `autocad`／`revit`／`slow` marker）、compileall、整棵樹的 whitespace 檢查。
- `dotnet build revit/MepTray.Core -c Release`（net8.0＋net10.0）與 `revit/MepTray.Core.SelfTest`，並以真實 Python exporter 產生的 `m.json`／`expected.json` 執行 SelfTest（net10.0）。
- **不建置 `revit/MepTrayImport`**：它參考本機 Revit 安裝的 `RevitAPI.dll`／`RevitAPIUI.dll`，這些 Autodesk 檔案不得再散布、不能放進 repo，hosted runner 也沒有 Revit。增益集建置與所有 Revit 驗收都屬本機 gate。

## 3. 版本號與 tag

- 採 SemVer：tag 格式 `v<major>.<minor>.<patch>`（例：`v0.4.0`）。
- 建議第一個正式 tag 為 **`v0.4.0`**（對應 stage 4；1.0 之前 API／輸出格式仍可能變動）。
- `major`：輸出 JSON schema（`schema_version`）或 Revit 匯入契約不相容變更；`minor`：新功能；`patch`：相容的修正與文件。
- tag 只打在已合併到 `master` 的 merge commit 上，使用 annotated tag：

```powershell
git checkout master; git pull --ff-only
git tag -a v0.4.0 -m "v0.4.0"
git push origin v0.4.0
```

## 4. Changelog

- 每個 release 在 GitHub Release 說明（或日後新增的 `CHANGELOG.md`）列出：版本、日期、合併的 PR、使用者可見變更、已知限制、VERIFICATION 小節連結。
- 分類沿用 commit type：feat／fix／docs／test／build／refactor。
- 必須重述限制：Revit 2027 與 AutoCAD 2027 未驗證（除非該 release 已在 VERIFICATION 記錄實機 PASS）、規範值未驗證。

## 5. Authenticode 簽章（再散布 DLL 時）

需要簽章的檔案：`MepTrayImport.dll`（Revit 增益集）與 `MepTray.Core.dll`。只在受控的本機簽章環境進行，**不在 CI 或 PR workflow 中簽章**。

檢查清單：

- [ ] 憑證（.pfx／硬體 token／雲端 HSM）**永遠不進 repo**、不進 PR、不放 GitHub Actions secrets 供 PR workflow 使用；fork 或 PR 觸發的 workflow 不可接觸簽章金鑰。
- [ ] 以 Release 組態從乾淨的 tag checkout 建置，記錄 SHA256（簽章前後各一次）。
- [ ] 簽章，必須加 RFC 3161 時間戳記（TSA URL 由憑證供應商提供）：

```powershell
signtool sign /fd SHA256 /tr <TSA URL> /td SHA256 /f <path-outside-repo>\cert.pfx MepTrayImport.dll
signtool sign /fd SHA256 /tr <TSA URL> /td SHA256 /f <path-outside-repo>\cert.pfx MepTray.Core.dll
```

- [ ] 驗證：

```powershell
signtool verify /pa /v MepTrayImport.dll
Get-AuthenticodeSignature .\MepTrayImport.dll | Format-List Status, SignerCertificate, TimeStamperCertificate
Get-AuthenticodeSignature .\MepTray.Core.dll | Format-List Status, SignerCertificate, TimeStamperCertificate
```

  `Status` 必須為 `Valid`，且有 `TimeStamperCertificate`。

- [ ] 把簽章者、憑證指紋、SHA256、時間戳記與驗證輸出記入 VERIFICATION.md。

## 6. WDAC／Code Integrity

- 受 WDAC（App Control for Business）管理的機器：由 **系統管理員**把發行者（publisher／簽章憑證）加入允許清單；不由開發者自行修改。
- **永遠不要**以 `Unblock-File`、停用或放寬 WDAC／Code Integrity、關閉 SmartScreen 等方式繞過；被擋時依 VERIFICATION 的方式蒐證並標 BLOCKED。
- **自簽憑證只用於測試**（隔離的測試機）；自簽憑證簽出的 DLL 不得當作正式 release 散布，也不得要求使用者信任自簽根憑證。

## 7. 發布步驟摘要

1. 在 `develop` 跑完 §2 gate，更新 VERIFICATION.md（含「未執行」項目）與 changelog。
2. 開 PR：`develop` → `master`，等 CI 綠燈並完成 review 後合併（不 force push）。
3. 在 `master` merge commit 打 `v<major>.<minor>.<patch>` annotated tag 並 push tag。
4. 若再散布 DLL：依 §5 簽章與驗證，依 §6 交由管理員處理 WDAC 允許清單。
5. 建立 GitHub Release，附 changelog 與 VERIFICATION 連結。
