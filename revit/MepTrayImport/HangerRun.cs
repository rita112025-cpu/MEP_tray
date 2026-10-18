using Autodesk.Revit.Attributes;
using Autodesk.Revit.DB;
using Autodesk.Revit.UI;
using MepTray.Core;

namespace MepTray.Revit;

/// <summary>兩個入口共用的流程：預覽對話框 → 放置 Transaction（在呼叫端的 TransactionGroup 內）→ 報告。</summary>
public static class HangerRun
{
    public static string? PickConfig()
    {
        var dlg = new Microsoft.Win32.OpenFileDialog { Filter = "MEP tray 吊架設定 (*.json)|*.json", Title = "選取吊架設定 JSON（族實測資料＋間距等，皆無預設）" };
        return dlg.ShowDialog() == true ? dlg.FileName : null;
    }

    /// <summary>預覽：不改模型。回傳 true＝使用者確認執行。</summary>
    public static bool ConfirmPreview(HangerRunReport rep, string? extra = null)
    {
        var td = new TaskDialog("MEP Tray — 吊架預覽（尚未修改模型）")
        {
            MainInstruction = rep.PreflightErrors.Count > 0 ? "預檢失敗，無法放置" : $"將新增 {rep.Count("new")} 支吊架",
            MainContent = rep.Summary() + (extra == null ? "" : "\n\n" + extra),
            CommonButtons = TaskDialogCommonButtons.Cancel,
        };
        if (rep.PreflightErrors.Count == 0 && rep.Count("new") > 0)
            td.AddCommandLink(TaskDialogCommandLinkId.CommandLink1, "執行放置", "單支失敗只回滾該支；完成後可一次 Undo");
        return td.Show() == TaskDialogResult.CommandLink1;
    }

    /// <summary>在已開始的 TransactionGroup 內放置；例外往上拋（由呼叫端整批回滾）。</summary>
    public static void Place(Document doc, HangerRunReport rep, HangerConfig cfg, HangerPlacer.Resolved res, string toolVersion = HangerPlacer.TrayToolVersion)
    {
        using var tx = HangerPlacer.StartTx(doc, "MEP Tray 放置吊架", rep.RevitWarnings);
        try
        {
            HangerPlacer.Execute(doc, rep, cfg, res, toolVersion);
            var st = tx.Commit();
            if (st != TransactionStatus.Committed) throw new InvalidOperationException($"吊架 Transaction 未能 Commit（狀態 {st}）");
        }
        catch
        {
            if (tx.GetStatus() == TransactionStatus.Started) tx.RollBack();
            throw;
        }
    }

    public static void WriteReport(HangerRunReport rep, string basePath)
    {
        var path = $"{basePath}.hangers_{rep.BatchId}.json";
        try { File.WriteAllText(path, rep.ToJson()); rep.Notes.Add($"報告：{path}"); }
        catch (Exception ex) { rep.Notes.Add($"無法寫入報告檔：{ex.Message}"); }
    }
}

/// <summary>「選取橋架補吊架」：對目前選取的 Cable Tray 批次補吊架；不重建、不移動橋架。</summary>
[Transaction(TransactionMode.Manual)]
public class AddHangersCommand : IExternalCommand
{
    public Result Execute(ExternalCommandData commandData, ref string message, ElementSet elements)
    {
        var uidoc = commandData.Application.ActiveUIDocument;
        var doc = uidoc?.Document;
        if (doc is null) { message = "請先開啟 Revit 專案"; return Result.Failed; }
        var ids = uidoc!.Selection.GetElementIds().Where(id => doc.GetElement(id) is Autodesk.Revit.DB.Electrical.CableTray).ToList();
        if (ids.Count == 0) { message = "請先選取要補吊架的 Cable Tray（v1 只支援 Cable Tray）"; return Result.Failed; }
        var cfgPath = HangerRun.PickConfig();
        if (cfgPath == null) return Result.Cancelled;
        HangerConfig cfg;
        try { cfg = HangerPlacer.LoadConfig(cfgPath); }
        catch (Exception ex) when (ex is InvalidDataException or System.Text.Json.JsonException or IOException)
        { message = ex.Message; TaskDialog.Show("MEP Tray — 吊架設定", ex.Message); return Result.Failed; }

        var rep = HangerPlacer.Plan(doc, ids, cfg, "selection", out _, out var res);
        if (!HangerRun.ConfirmPreview(rep) || res == null || rep.PreflightErrors.Count > 0)
        {
            rep.Outcome = rep.PreflightErrors.Count > 0 ? "失敗" : "已取消";
            HangerRun.WriteReport(rep, cfgPath);
            return rep.PreflightErrors.Count > 0 ? Result.Failed : Result.Cancelled;
        }
        using var tg = new TransactionGroup(doc, "MEP Tray 選取橋架補吊架");
        tg.Start();
        try
        {
            HangerRun.Place(doc, rep, cfg, res);
            if (rep.Count("created") == 0) { tg.RollBack(); rep.Notes.Add("沒有任何吊架建立成功，整批已回滾"); }
            else tg.Assimilate();
        }
        catch (Exception ex)
        {
            if (tg.GetStatus() == TransactionStatus.Started) tg.RollBack();
            rep.Outcome = "失敗";
            rep.Notes.Add($"整批回滾：{ex.GetType().Name}: {ex.Message}");
        }
        HangerRun.WriteReport(rep, cfgPath);
        TaskDialog.Show("MEP Tray 補吊架結果", rep.Summary());
        return rep.Outcome is "完成" or "部分完成" ? Result.Succeeded : Result.Failed;
    }
}
