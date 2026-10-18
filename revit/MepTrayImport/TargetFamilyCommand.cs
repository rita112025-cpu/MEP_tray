using System.Globalization;
using System.Text.Encodings.Web;
using System.Text.Json;
using System.Text.Json.Nodes;
using Autodesk.Revit.Attributes;
using Autodesk.Revit.DB;
using Autodesk.Revit.UI;
using MepTray.Core;

namespace MepTray.Revit;

/// <summary>
/// 「讀取目標吊架族」：唯讀讀取單一目標 .rfa（重用盤點的 ScanRfa：OpenDocumentFile → 讀 → Close(false) 不儲存）。
/// 以 Revit API 結果為準（PartAtom 只是提示）；把 API 讀到的事實（族/類型/放置方式/參數/檔案身分）寫入吊架設定，
/// 不填任何推測值（間距、內縮、插入點偏移、修正角、參數對應都不由本命令填）。
/// </summary>
[Transaction(TransactionMode.ReadOnly)]
public class TargetFamilyCommand : IExternalCommand
{
    static readonly JsonSerializerOptions Json = new() { WriteIndented = true, Encoder = JavaScriptEncoder.UnsafeRelaxedJsonEscaping };

    public static string? DerivedMode(string? placementType) => placementType switch
    {
        "OneLevelBased" => "level",
        "WorkPlaneBased" => "workplane",
        _ => null,
    };

    public Result Execute(ExternalCommandData commandData, ref string message, ElementSet elements)
    {
        var uiapp = commandData.Application;
        var doc = uiapp.ActiveUIDocument?.Document;
        var of = new Microsoft.Win32.OpenFileDialog { Title = "選擇目標吊架族 .rfa（只讀取，不修改、不儲存）", Filter = "Revit 族檔 (*.rfa)|*.rfa" };
        if (File.Exists(HangerCalibration.DefaultTargetRfa))
        {
            of.InitialDirectory = Path.GetDirectoryName(HangerCalibration.DefaultTargetRfa);
            of.FileName = Path.GetFileName(HangerCalibration.DefaultTargetRfa);
        }
        if (of.ShowDialog() != true) return Result.Cancelled;
        var path = of.FileName;
        var typeName = HangerCalibration.DefaultTargetType;

        JsonObject identity;
        try { identity = HangerCalibration.FileIdentity(path); }
        catch (Exception ex) when (ex is IOException or UnauthorizedAccessException)
        { message = $"無法讀取族檔：{ex.Message}"; TaskDialog.Show("讀取目標吊架族", message); return Result.Failed; }

        var app = uiapp.Application;
        var opened = new List<string>();
        var e = HangerInventoryCommand.ScanRfa(app, Path.GetDirectoryName(path)!, path, HangerInventory.ParseRevitYear(app.VersionNumber), opened);
        var stillOpen = app.Documents.Cast<Document>().Any(d => string.Equals(d.PathName, path, StringComparison.OrdinalIgnoreCase)) && opened.Count > 0;

        var info = new JsonObject
        {
            ["read_at"] = DateTimeOffset.Now.ToString("yyyy-MM-dd HH:mm:ss zzz", CultureInfo.InvariantCulture),
            ["read_via"] = "Revit API（OpenDocumentFile 唯讀讀取，Close(false) 不儲存）；PartAtom 只是提示，以此為準",
            ["revit"] = $"{app.VersionName} {app.SubVersionNumber}",
            ["status"] = e.Status, ["reason"] = e.Reason,
            ["family_file"] = identity, ["saved_in_version"] = e.SavedInVersion,
            ["family_name"] = e.FamilyName, ["category"] = e.Category, ["types"] = new JsonArray(e.Types.Select(t => (JsonNode?)t).ToArray()),
            ["type_name"] = typeName, ["type_exists"] = e.Types.Contains(typeName),
            ["placement_type"] = e.PlacementType, ["hosting"] = e.Hosting, ["work_plane_based"] = e.WorkPlaneBased, ["always_vertical"] = e.AlwaysVertical,
            ["placement_mode_derived"] = DerivedMode(e.PlacementType),
            ["closed_without_save"] = !stillOpen,
        };
        var pars = new JsonArray();
        foreach (var d in e.Params.OrderBy(p => p.Name, StringComparer.Ordinal))
        {
            d.ValuesByType.TryGetValue(typeName, out var v);
            var isLen = d.DataType != null && d.DataType.StartsWith("autodesk.spec.aec:length", StringComparison.Ordinal);
            pars.Add(new JsonObject
            {
                ["name"] = d.Name, ["scope"] = d.Scope, ["data_type"] = d.DataType, ["unit"] = d.Unit, ["is_length"] = isLen,
                ["formula"] = d.Formula, ["is_read_only"] = d.IsReadOnly, ["is_determined_by_formula"] = d.IsDeterminedByFormula,
                ["is_reporting"] = d.IsReporting, ["builtin"] = d.BuiltIn,
                ["adjustable"] = d.IsReadOnly != true && d.IsDeterminedByFormula != true && d.IsReporting != true && d.BuiltIn == null,
                ["value_mm"] = v?.Mm, ["value_display"] = v?.Display, ["value_kind"] = d.ValueKind,
            });
        }
        info["parameters"] = pars;
        info["parameter_set_sha256"] = HangerCalibration.ParamSetSha(e.Params);
        if (doc != null && !doc.IsFamilyDocument)
        {
            var sym = HangerCalibration.FindSymbol(doc, e.FamilyName, typeName);
            info["loaded_in_project"] = sym != null;
            if (sym != null) info["project_placement_type"] = sym.Family.FamilyPlacementType.ToString();
        }

        var problems = new List<string>();
        if (e.Status != "ok") problems.Add($"讀取失敗：{e.Reason}");
        if (e.Status == "ok" && !e.Types.Contains(typeName)) problems.Add($"族中沒有類型「{typeName}」；現有：{string.Join(", ", e.Types)}");
        if (e.Status == "ok" && DerivedMode(e.PlacementType) == null) problems.Add($"放置方式 {e.PlacementType ?? "無法讀取"}（hosting={e.Hosting}）需要主體或無法判定；v1 無法放置");
        if (stillOpen) problems.Add("族文件仍開啟（關閉失敗）；請手動關閉，不要儲存");
        info["problems"] = new JsonArray(problems.Select(p => (JsonNode?)p).ToArray());

        var outDir = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.MyDocuments), "MEP_tray");
        string reportPath;
        try
        {
            Directory.CreateDirectory(outDir);
            reportPath = Path.Combine(outDir, $"target_family_{DateTime.Now:yyyyMMdd_HHmmss}.json");
            File.WriteAllText(reportPath, info.ToJsonString(Json));
        }
        catch (Exception ex) { message = $"無法寫入報告：{ex.Message}"; TaskDialog.Show("讀取目標吊架族", message); return Result.Failed; }

        var lens = pars.Where(p => p!["is_length"]!.GetValue<bool>()).Select(p =>
            $"  {p!["name"]}（{p["scope"]}{(p["adjustable"]!.GetValue<bool>() ? "，可調" : "，不可調")}{(p["formula"] is JsonNode f ? $"，公式 {f}" : "")}）= {p["value_display"]}").ToList();
        var summary = $"族：{e.FamilyName}（{e.Category}）  狀態：{e.Status}\n類型「{typeName}」：{(e.Types.Contains(typeName) ? "存在" : "不存在")}\n" +
                      $"放置方式：{e.PlacementType}  hosting：{e.Hosting}  工作平面式：{e.WorkPlaneBased}  →  placement_mode：{DerivedMode(e.PlacementType) ?? "（不支援）"}\n" +
                      $"長度參數：\n{string.Join("\n", lens)}\n" +
                      (problems.Count > 0 ? "\n問題：\n  " + string.Join("\n  ", problems) + "\n" : "") + $"\n報告：{reportPath}";
        if (problems.Count > 0 && e.Status != "ok") { TaskDialog.Show("讀取目標吊架族（失敗）", summary); return Result.Failed; }

        var td = new TaskDialog("讀取目標吊架族") { MainInstruction = "讀取完成", MainContent = summary, CommonButtons = TaskDialogCommonButtons.Close };
        td.AddCommandLink(TaskDialogCommandLinkId.CommandLink1, "寫入既有吊架設定檔…", "只寫入 API 讀到的事實；其餘欄位不變");
        td.AddCommandLink(TaskDialogCommandLinkId.CommandLink2, "建立新吊架設定檔…", "API 事實以外的欄位全部為 null（需填寫或校正）");
        var r = td.Show();
        if (r is not (TaskDialogResult.CommandLink1 or TaskDialogResult.CommandLink2)) return Result.Succeeded;
        string cfgPath;
        JsonObject cfg;
        if (r == TaskDialogResult.CommandLink1)
        {
            var o = new Microsoft.Win32.OpenFileDialog { Title = "選擇吊架設定 JSON", Filter = "JSON (*.json)|*.json", InitialDirectory = outDir };
            if (o.ShowDialog() != true) return Result.Succeeded;
            cfgPath = o.FileName;
            try { cfg = JsonNode.Parse(File.ReadAllText(cfgPath))!.AsObject(); File.Copy(cfgPath, cfgPath + $".pre-read-{DateTime.Now:yyyyMMddHHmmss}.bak"); }
            catch (Exception ex) { TaskDialog.Show("讀取目標吊架族", $"設定檔無法讀取：{ex.Message}"); return Result.Failed; }
        }
        else
        {
            var s = new Microsoft.Win32.SaveFileDialog { Title = "新吊架設定檔", Filter = "JSON (*.json)|*.json", InitialDirectory = outDir, FileName = "tray_hanger_config.json" };
            if (s.ShowDialog() != true) return Result.Succeeded;
            cfgPath = s.FileName;
            cfg = NewConfig();
        }
        cfg["units"] = "mm";
        cfg["supported_object"] = "cable_tray";
        cfg["family"] = new JsonObject { ["family_name"] = e.FamilyName, ["type_name"] = typeName };
        cfg["placement_mode"] = DerivedMode(e.PlacementType);
        cfg["target_family"] = info.DeepClone();
        if (cfg["calibration"] is JsonObject cal && cal["family_file"]?["sha256"]?.GetValue<string>() != identity["sha256"]!.GetValue<string>())
            problems.Add("設定檔中的既有校正對應不同的族檔內容，放置時會被要求重新校正");
        try { File.WriteAllText(cfgPath, cfg.ToJsonString(Json)); }
        catch (Exception ex) { TaskDialog.Show("讀取目標吊架族", $"無法寫入設定檔：{ex.Message}"); return Result.Failed; }
        TaskDialog.Show("讀取目標吊架族", $"已寫入：{cfgPath}\n下一步：選取一段水平 Cable Tray，執行「校正目標吊架」。" +
                                         (problems.Count > 0 ? "\n\n注意：\n  " + string.Join("\n  ", problems) : ""));
        return Result.Succeeded;
    }

    static JsonObject NewConfig() => new()
    {
        ["units"] = "mm", ["supported_object"] = "cable_tray", ["family"] = null, ["placement_mode"] = null,
        ["max_spacing_mm"] = null, ["start_offset_mm"] = null, ["end_offset_mm"] = null, ["short_run_policy"] = null,
        ["near_fitting"] = new JsonObject { ["enabled"] = false },
        ["widen_left_mm"] = null, ["widen_right_mm"] = null, ["insertion_to_bearing_mm"] = null, ["orientation_correction_deg"] = null,
        ["parameter_map"] = new JsonObject { ["cross_member_length"] = null }, ["rod_length"] = new JsonObject { ["mode"] = "none" },
        ["dedupe"] = new JsonObject { ["tolerance_mm"] = null, ["angle_tolerance_deg"] = null },
    };
}
