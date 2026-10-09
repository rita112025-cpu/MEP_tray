# Git 開發工作流（GIT_WORKFLOW）

> 適用本 repo（origin：https://github.com/rita112025-cpu/MEP_tray.git）。2026-10-09 建立。

## 1. 分支結構

| 分支 | 角色 | 規則 |
|---|---|---|
| `master` | 穩定發布線 | 只接受 `develop` 或 `hotfix/*` 的合併；HEAD 必須通過完整驗證 gate（見 §3）；禁止 force push |
| `develop` | 整合線 | 功能分支的合併目標；合併前跑預設測試 gate；禁止 force push |
| `feature/<議題>` | 功能開發 | 自 `develop` 切出，完成後合併回 `develop`；例：`feature/web-ui-3d-preview` |
| `fix/<議題>` | 錯誤修正（非緊急） | 自 `develop` 切出；例：`fix/cable-tray-support-rules` |
| `hotfix/<議題>` | 緊急修正 | 自 `master` 切出，完成後同時合併回 `master` **與** `develop` |

現況：`fix/mrt-appx-c-provenance`、`fix/cable-tray-support-rules` 為首批 fix 分支；`develop` 自本文件生效時建立。

## 2. 日常操作（可複製）

```powershell
# 建立並同步 develop（首次）
git checkout master; git pull
git branch develop; git push -u origin develop

# 開新功能
git checkout develop; git pull
git checkout -b feature/<議題>

# 提交訊息格式：type: summary（沿用現有風格）
#   例：fix: align cable tray support rules with owner specification
#   type ∈ {feat, fix, docs, test, build, refactor}
git add -A; git commit -m "feat: <描述>"

# 合併回 develop（GitHub PR 或本機 --no-ff）
git checkout develop; git merge --no-ff feature/<議題>

# 釋出到 master
git checkout master; git merge --no-ff develop; git tag v<版本>
```

## 3. 合併 gate（合併前必跑）

| 目標分支 | 必要 gate |
|---|---|
| `develop` | `python -m pytest -q` 全綠；`git diff --check` 通過 |
| `master` | 完整驗證：`pytest -q` ＋ `-m slow` ＋（有 AutoCAD）`-m autocad` ＋（有 .NET SDK）`-m revit` ＋ `python -m compileall -q mep_tray tests`；任一類別無法執行須在合併說明註記「未執行」 |

額外約束（沿用專案既定準則）：

- Runtime gate 與 build 分開回報；編譯通過不視為執行驗證。
- 受到 OS 安全政策阻擋時蒐證並標 BLOCKED，**不解除 OS policy、不 Unblock-File 規避**。若環境政策已變更而可執行，記錄日期與證據（見 VERIFICATION.md）。
- Revit 實機項目屬 HUMAN TEST，不得以 build 或 `autorun.done` 單獨宣稱 GUI 人工驗收。

## 4. Release 檢查清單

- [ ] `master` 合併 gate 全綠
- [ ] `docs/VERIFICATION.md` 驗證狀態為最新
- [ ] Revit 增益集 DLL 若再分發：需程式碼簽章（WDAC 環境，見 INSTALL 常見問題）
- [ ] 打 tag：`v<major>.<minor>.<patch>`，並在 README 紀錄
