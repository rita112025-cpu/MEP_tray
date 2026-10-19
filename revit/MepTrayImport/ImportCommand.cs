using Autodesk.Revit.Attributes;
using Autodesk.Revit.DB;
using Autodesk.Revit.UI;
using MepTray.Core;

namespace MepTray.Revit;

/// <summary>Revit 外部命令：選取 tray_*.json → 以 CableTray 系統族建模；可選「同時建立吊架」（匯入橋架＋吊架）。</summary>
[Transaction(TransactionMode.Manual)]
public class ImportCommand : IExternalCommand
{
    public Result Execute(ExternalCommandData commandData, ref string message, ElementSet elements)
    {
        var doc = commandData.Application.ActiveUIDocument?.Document;
        if (doc is null) { message = "請先開啟 Revit 專案"; return Result.Failed; }
        var dlg = new Microsoft.Win32.OpenFileDialog { Filter = "MEP tray model (*.json)|*.json", Title = "選取 tray_*.json" };
        if (dlg.ShowDialog() != true) return Result.Cancelled;

        TrayModel model;
        try { model = ModelLoader.Load(dlg.FileName); }
        catch (Exception ex) { message = $"無法讀取模型：{ex.Message}"; return Result.Failed; }

        var errors = ModelLoader.Validate(model);
        if (errors.Count > 0) { message = "模型驗證失敗：" + string.Join("; ", errors); return Result.Failed; }

        var opt = new ImportOptions();
        if (model.CoordinateSystem.Basis == "UNSPECIFIED")
        {
            // 不自動假定：必須由使用者明確選擇座標基準
            var td = new TaskDialog("MEP Tray — 座標系基準")
            {
                MainInstruction = "模型未指定座標系基準",
                MainContent = "模型座標單位為 mm。僅支援內部座標，或加上專案基準點位置的平移；座標軸保持內部軸向，不套用旋轉。測量點、共用座標及連結模型變換尚未支援。請選擇基準（不選則取消，不會建立元件）。",
                CommonButtons = TaskDialogCommonButtons.Cancel,
            };
            td.AddCommandLink(TaskDialogCommandLinkId.CommandLink1, "Internal Origin（內部原點）");
            td.AddCommandLink(TaskDialogCommandLinkId.CommandLink2, "Project Base Point（專案基準點）");
            switch (td.Show())
            {
                case TaskDialogResult.CommandLink1: opt.BasisOverride = "INTERNAL_ORIGIN"; break;
                case TaskDialogResult.CommandLink2: opt.BasisOverride = "PROJECT_BASE_POINT"; break;
                default: return Result.Cancelled;
            }
        }

        // 選項：同時建立吊架（設定與族預檢在任何模型修改之前完成）
        HangerConfig? hcfg = null;
        string? hcfgPath = null;
        var ask = new TaskDialog("MEP Tray — 吊架")
        {
            MainInstruction = "是否同時建立吊架？",
            MainContent = "吊架位置會在橋架與接頭建立後，以實際橋架端點重算（不在接頭本體內）。需要吊架設定 JSON（族實測資料、間距、內縮皆無預設）。",
            CommonButtons = TaskDialogCommonButtons.Cancel,
        };
        ask.AddCommandLink(TaskDialogCommandLinkId.CommandLink1, "只匯入橋架");
        ask.AddCommandLink(TaskDialogCommandLinkId.CommandLink2, "匯入橋架＋吊架");
        var choice = ask.Show();
        if (choice == TaskDialogResult.CommandLink2)
        {
            hcfgPath = HangerRun.PickConfig();
            if (hcfgPath == null) return Result.Cancelled;
            try { hcfg = HangerPlacer.LoadConfig(hcfgPath); }
            catch (Exception ex) when (ex is InvalidDataException or System.Text.Json.JsonException or IOException)
            { message = ex.Message; TaskDialog.Show("MEP Tray — 吊架設定", ex.Message); return Result.Failed; }
            var pre = new List<string>();
            if (HangerPlacer.Preflight(doc, hcfg, pre) == null)
            { message = "吊架預檢失敗：" + string.Join("; ", pre); TaskDialog.Show("MEP Tray — 吊架預檢（未修改模型）", string.Join("\n", pre)); return Result.Failed; }
        }
        else if (choice != TaskDialogResult.CommandLink1) return Result.Cancelled;

        if (hcfg == null)
        {
            var report = RevitImporter.Import(doc, model, opt);
            return Finish(dlg.FileName, model, report, null, ref message);
        }

        using var tg = new TransactionGroup(doc, "MEP Tray 匯入橋架＋吊架");
        tg.Start();
        var rep = RevitImporter.Import(doc, model, opt);
        HangerRunReport? hrep = null;
        try
        {
            if (rep.Abort != null || !rep.Committed)
            {
                tg.RollBack();
            }
            else
            {
                hrep = HangerPlacer.Plan(doc, rep.TrayElementIds, hcfg, "import", out var trays, out var res, checkCalibration: false);   // 已在 tg 開始前的 Preflight 核對
                var extra = CompareWithJson(model, hrep);
                hrep.Notes.Add(extra);
                if (!HangerRun.ConfirmPreview(hrep) || res == null || hrep.PreflightErrors.Count > 0)
                {
                    tg.RollBack();
                    hrep.Outcome = hrep.PreflightErrors.Count > 0 ? "失敗" : "已取消";
                    rep.Abort = "使用者取消或吊架預檢失敗：整批（含橋架）已回滾，模型未修改";
                    rep.Committed = false;
                }
                else
                {
                    HangerRun.Place(doc, hrep, hcfg, res);
                    tg.Assimilate();   // 橋架＋吊架一次 Undo
                }
            }
        }
        catch (Exception ex)
        {
            if (tg.GetStatus() == TransactionStatus.Started) tg.RollBack();
            rep.Committed = false;
            rep.Abort = $"吊架階段例外，整批（含橋架）已回滾：{ex.GetType().Name}: {ex.Message}";
            if (hrep != null) { hrep.Outcome = "失敗"; hrep.Notes.Add(rep.Abort); }
        }
        if (hrep != null) HangerRun.WriteReport(hrep, dlg.FileName);
        return Finish(dlg.FileName, model, rep, hrep, ref message);
    }

    /// <summary>JSON（未建接頭）預估 vs Revit（建立接頭後）重算的差異與原因。</summary>
    static string CompareWithJson(TrayModel m, HangerRunReport hrep)
    {
        if (m.HangerSupports is null) return "JSON 未含 hanger_supports（舊版檔案），僅以 Revit 實際幾何計算";
        var n = hrep.Items.Count;
        var j = m.HangerSupports.Count;
        return $"JSON 預估 {j} 支（未建接頭的路徑端點、JSON 參數）；Revit 依實際橋架與吊架設定重算 {n} 支。" +
               (j == n ? "" : "差異原因：接頭本體使橋架端點內縮，且 Revit 端使用吊架設定的間距/內縮（可能與 JSON 預覽參數不同）。");
    }

    static Result Finish(string path, TrayModel model, ImportReport report, HangerRunReport? hrep, ref string message)
    {
        try { File.WriteAllText(path + ".revit_report.json", report.ToJson()); }
        catch (Exception ex) { report.Warnings.Add($"無法寫入報告檔：{ex.Message}"); }

        var text = report.Summary();
        text += "\n\n座標限制：僅內部座標／專案基準點位置平移；不套用旋轉，不支援測量點、共用座標或連結模型變換。";
        if (hrep != null) text += "\n\n吊架：\n" + hrep.Summary();
        if (model.Disclosures.Count > 0) text += "\n\n揭露：\n  " + string.Join("\n  ", model.Disclosures);
        TaskDialog.Show("MEP Tray 匯入結果", text);
        if (report.Abort != null) { message = report.Abort; return Result.Failed; }
        return Result.Succeeded;
    }
}
