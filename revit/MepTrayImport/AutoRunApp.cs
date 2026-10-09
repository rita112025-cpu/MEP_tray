using System.Text.Json;
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
/// 若同資料夾有 &lt;模型名&gt;.setup.json（例如 {"move_pbp_mm":[dx,dy,dz]}），先以獨立 Transaction
/// 設定文件座標狀態，結果寫入 *.setup_result.json；設定失敗寫 *.setup_error.txt 並略過該模型的匯入。
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

    static readonly string[] NonModelSuffixes = { ".revit_report.json", ".setup.json", ".setup_result.json" };

    static bool IsModelFile(string p) => !NonModelSuffixes.Any(s => p.EndsWith(s, StringComparison.OrdinalIgnoreCase));

    static readonly JsonSerializerOptions Indented = new() { WriteIndented = true };

    /// <summary>
    /// 依 setup 檔設定文件座標狀態（僅測試用）。目前支援 move_pbp_mm：以 ElementTransformUtils.MoveElement
    /// 平移 Project Base Point（必要時先解除釘選）。移動後讀回位置，與要求的位移差超過 0.5 mm 即拋出例外。
    /// </summary>
    static string ApplySetup(Document doc, string setupPath)
    {
        using var js = JsonDocument.Parse(File.ReadAllText(setupPath));
        var root = js.RootElement;
        foreach (var prop in root.EnumerateObject())
            if (prop.Name != "move_pbp_mm") throw new InvalidDataException($"未知的 setup 鍵 '{prop.Name}'");
        var log = new Dictionary<string, object?> { ["setup"] = setupPath, ["before"] = RevitImporter.ReadCoordinates(doc) };
        if (root.TryGetProperty("move_pbp_mm", out var mv))
        {
            var d = mv.EnumerateArray().Select(x => x.GetDouble()).ToArray();
            if (d.Length != 3 || d.Any(v => double.IsNaN(v) || double.IsInfinity(v)))
                throw new InvalidDataException("move_pbp_mm 必須是 3 個有限數值 [dx,dy,dz]（mm）");
            var pbp = BasePoint.GetProjectBasePoint(doc) ?? throw new InvalidOperationException("找不到 Project Base Point");
            var before = pbp.Position;
            log["pbp_pinned_before"] = pbp.Pinned;
            var delta = new XYZ(MepTray.Core.Units.MmToFeet(d[0]), MepTray.Core.Units.MmToFeet(d[1]), MepTray.Core.Units.MmToFeet(d[2]));
            using var tx = new Transaction(doc, "MEP Tray test setup: move Project Base Point");
            tx.Start();
            try
            {
                if (pbp.Pinned) pbp.Pinned = false;
                ElementTransformUtils.MoveElement(doc, pbp.Id, delta);
                doc.Regenerate();
                var st = tx.Commit();
                if (st != TransactionStatus.Committed)
                    throw new InvalidOperationException($"移動 PBP 的 Transaction 未能 Commit（狀態 {st}）");
            }
            catch
            {
                if (tx.GetStatus() == TransactionStatus.Started) tx.RollBack();
                throw;
            }
            var after = BasePoint.GetProjectBasePoint(doc).Position;
            var moved = after - before;
            log["requested_mm"] = d;
            log["moved_mm"] = new[] { MepTray.Core.Units.FeetToMm(moved.X), MepTray.Core.Units.FeetToMm(moved.Y), MepTray.Core.Units.FeetToMm(moved.Z) };
            log["after"] = RevitImporter.ReadCoordinates(doc);
            if (moved.DistanceTo(delta) > MepTray.Core.Units.MmToFeet(0.5))
                throw new InvalidOperationException("PBP 移動結果與要求不符：" + JsonSerializer.Serialize(log, Indented));
        }
        return JsonSerializer.Serialize(log, Indented);
    }

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
            foreach (var path in Directory.GetFiles(dir, "*.json").Where(IsModelFile).OrderBy(p => p))
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
                        var setupPath = Path.Combine(dir, Path.GetFileNameWithoutExtension(path) + ".setup.json");
                        if (File.Exists(setupPath))
                        {
                            try { File.WriteAllText(path + ".setup_result.json", ApplySetup(doc, setupPath)); }
                            catch (Exception ex)
                            {
                                File.WriteAllText(path + ".setup_error.txt", ex.ToString());
                                continue;   // 座標狀態未設定成功時不匯入，避免產生看似通過的空洞結果
                            }
                        }
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
