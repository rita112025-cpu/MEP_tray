# 安裝（INSTALL）

> 規範值未驗證，不得視為合規；零問題不代表合規。
> 本報告僅涵蓋本工具已實作之檢查項，不等同於完整法規合規審查。
> 驗證狀態見 [VERIFICATION.md](VERIFICATION.md)；操作見 [MANUAL.md](MANUAL.md)；架構見 [ARCHITECTURE.md](ARCHITECTURE.md)。

## 1. 需求

| 項目 | 必要性 | 說明 |
|---|---|---|
| Python 3.12 | 必要 | 開發與驗證版本；其他版本未驗證 |
| ezdxf（`requirements.txt`） | 必要 | 唯一第三方相依，版本 `>=1.3,<2` |
| 瀏覽器 | 必要（用網頁介面） | 只有內建瀏覽器驗證過，見 MANUAL「限制」 |
| AutoCAD（`accoreconsole.exe`） | 選用 | 產生並稽核 DWG；沒有時只輸出 DXF 並註明 |
| ODA File Converter | 選用 | DWG 的備援路徑，不是主要路徑 |
| .NET SDK | 僅建置 Revit 增益集時需要 | Python 與網頁介面不需要 |
| Revit | 僅匯入模型時需要 | 只驗證過 2025.5；2027 未驗證 |

## 2. 安裝步驟（PowerShell 5.1，逐行執行；5.1 沒有 `&&`）

```powershell
cd D:\github\MEP_tray
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

驗證安裝（預設不會啟動 AutoCAD 或 Revit）：

```powershell
python -m pytest -q
```

預期：全數通過；有一個測試會因本機不支援符號連結而 skip。

## 3. 啟動網頁介面

```powershell
python -m mep_tray.webui
```

終端機會印出含 token 的網址（`http://127.0.0.1:<埠>/t/<token>/`）。只綁定本機迴路位址。

- 指定埠：`python -m mep_tray.webui --port 9000`。埠被占用時自動改用空埠，以終端機印出的為準。
- 自動開瀏覽器：加 `--open`（預設不開，避免 token 進入非預期的瀏覽器）。

## 4. 測試分級

| 指令 | 內容 | 會啟動外部程式 |
|---|---|---|
| `python -m pytest -q` | 預設全套（排除下列三類） | 否 |
| `python -m pytest -m slow` | 完整端對端與細格距搜尋 | 否 |
| `python -m pytest -m autocad` | AutoCAD 稽核與 DWG 轉檔 | 是（需本機 AutoCAD） |
| `python -m pytest -m revit` | Revit 增益集 C# 編譯與 SelfTest | 需 .NET SDK；不啟動 Revit |

「完整驗證」＝ 預設全套 ＋ `-m slow` ＋（有 AutoCAD 時）`-m autocad` ＋（有 .NET SDK 時）`-m revit`。沒跑到的類別必須在回報中寫「未執行」，不能省略。

## 5. 建置 Revit 增益集（選用）

1. 安裝與 Revit 執行階段相符的 .NET SDK（Revit 2025.5 的 `RevitAPI.runtimeconfig.json` 指定 net10.0）。
2. 目標框架由 `revit/MepTrayImport` 的 csproj 從已安裝 Revit 自動偵測。
3. 在 Revit 內的使用步驟與驗證範圍見 [VERIFICATION.md](VERIFICATION.md)。本專案交付的是「Revit Cable Tray 系統族建模匯入器」，**不是 .rfa**。

## 6. 常見問題

**輸出放在哪？** `<專案根>\output\<run_id>\`。用環境變數 `MEP_OUTPUT_ROOT` 可改位置。工具不會寫到 `output` 以外（測試用暫存目錄除外）。

**舊版本會被刪嗎？** 不會。所有版本保留，要刪由你手動刪資料夾。

**啟動時埠被占用？** 看終端機印出的實際網址；或用 `--port` 指定別的埠。

**`import ezdxf` 失敗（出現 DLL load failed 等）？** 先 `pip install -r requirements.txt` 確認安裝在目前啟用的 venv；仍失敗時換 Python 3.12 的乾淨 venv。ezdxf 無法載入時 DXF 無法產生，這不是工具可以降級略過的步驟。

**輸出資料夾裡有 `.stage-` 開頭的資料夾？** 是中斷或清除失敗留下的暫存。確認沒有執行中的工作後可直接刪除；不影響已發布的版本，也不阻擋重跑同一個 run_id。

**PowerShell 腳本執行被擋（Activate.ps1）？** 對目前視窗執行 `Set-ExecutionPolicy -Scope Process RemoteSigned`；或不啟用 venv，直接用 `.\.venv\Scripts\python.exe -m ...`。
