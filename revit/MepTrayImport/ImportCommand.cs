using Autodesk.Revit.Attributes;
using Autodesk.Revit.DB;
using Autodesk.Revit.UI;
using MepTray.Core;

namespace MepTray.Revit;

/// <summary>Revit 外部命令：選取 tray_*.json → 以 CableTray 系統族建模。</summary>
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

        var report = RevitImporter.Import(doc, model, opt);
        try { File.WriteAllText(dlg.FileName + ".revit_report.json", report.ToJson()); }
        catch (Exception ex) { report.Warnings.Add($"無法寫入報告檔：{ex.Message}"); }

        var text = report.Summary();
        text += "\n\n座標限制：僅內部座標／專案基準點位置平移；不套用旋轉，不支援測量點、共用座標或連結模型變換。";
        if (model.Disclosures.Count > 0) text += "\n\n揭露：\n  " + string.Join("\n  ", model.Disclosures);
        TaskDialog.Show("MEP Tray 匯入結果", text);
        if (report.Abort != null) { message = report.Abort; return Result.Failed; }
        return Result.Succeeded;
    }
}
