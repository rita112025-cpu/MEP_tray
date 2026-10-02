using Autodesk.Revit.ApplicationServices;
using Autodesk.Revit.DB;
using Autodesk.Revit.DB.Electrical;
using Autodesk.Revit.UI;
using Autodesk.Revit.UI.Events;
using MepTray.Core;

namespace MepTray.Revit;

/// <summary>
/// 僅供自動化測試：Revit 啟動後，若設定環境變數 MEP_TRAY_AUTORUN_DIR，則對該資料夾內每個模型 JSON
/// 各開一個新專案（MEP_TRAY_TEMPLATE）執行匯入，寫出 *.revit_report.json 與 autorun.done 後結束 Revit。
/// 正式使用只需 MepTray.addin（外部命令），不需要本入口。
/// </summary>
public class AutoRunApp : IExternalApplication
{
    public Result OnStartup(UIControlledApplication application)
    {
        if (!string.IsNullOrEmpty(Environment.GetEnvironmentVariable("MEP_TRAY_AUTORUN_DIR")))
            application.Idling += OnIdling;
        return Result.Succeeded;
    }

    public Result OnShutdown(UIControlledApplication application) => Result.Succeeded;

    void OnIdling(object? sender, IdlingEventArgs e)
    {
        var uiapp = (UIApplication)sender!;
        uiapp.Idling -= OnIdling;
        var dir = Environment.GetEnvironmentVariable("MEP_TRAY_AUTORUN_DIR")!;
        var template = Environment.GetEnvironmentVariable("MEP_TRAY_TEMPLATE") ?? "";
        var basis = Environment.GetEnvironmentVariable("MEP_TRAY_BASIS");
        var app = uiapp.Application;
        try
        {
            File.WriteAllText(Path.Combine(dir, "revit_version.txt"), $"{app.VersionName} {app.VersionNumber} build {app.VersionBuild}");
            foreach (var path in Directory.GetFiles(dir, "*.json").Where(p => !p.EndsWith(".revit_report.json")).OrderBy(p => p))
            {
                try
                {
                    var model = ModelLoader.Load(path);
                    var doc = app.NewProjectDocument(template);
                    try
                    {
                        var types = new FilteredElementCollector(doc).OfClass(typeof(CableTrayType)).Cast<CableTrayType>()
                            .Select(t => t.Name).OrderBy(n => n).ToList();
                        File.WriteAllLines(Path.Combine(dir, "cabletray_types.txt"), types);
                        if (model.Tray.TypeName == "__FIRST__") model.Tray.TypeName = types.FirstOrDefault();
                        // 只有檔名以 ov_ 開頭的測試模型才套用基準覆寫；其餘依模型自身的 basis（UNSPECIFIED 應被拒絕）
                        var ov = Path.GetFileName(path).StartsWith("ov_") ? basis : null;
                        var rep = RevitImporter.Import(doc, model, new ImportOptions { BasisOverride = ov });
                        rep.Inspection = RevitImporter.Inspect(doc);
                        File.WriteAllText(path + ".revit_report.json", rep.ToJson());
                    }
                    finally { doc.Close(false); }
                }
                catch (Exception ex) { File.WriteAllText(path + ".revit_error.txt", ex.ToString()); }
            }
        }
        catch (Exception ex) { File.WriteAllText(Path.Combine(dir, "autorun.error.txt"), ex.ToString()); }
        finally
        {
            File.WriteAllText(Path.Combine(dir, "autorun.done"), DateTime.Now.ToString("O"));
            Environment.Exit(0);
        }
    }
}
