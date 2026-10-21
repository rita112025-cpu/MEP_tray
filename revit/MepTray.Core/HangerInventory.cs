using System.Globalization;
using System.Text;
using System.Text.Encodings.Web;
using System.Text.Json;
using System.Text.Json.Serialization;
using System.Text.RegularExpressions;

namespace MepTray.Core;

// ───────────────────────── 吊架族群盤點：資料模型（純資料，不依賴 Revit API）─────────────────────────

/// <summary>單一參數值。Status：ok／no_value（Revit 回報無值）／read_error（讀取時發生例外）— 三者明確區分。</summary>
public sealed class ParamValue
{
    public string Status { get; set; } = "ok";
    /// <summary>Revit 顯示字串（依專案/族檔單位設定，例如 "300 mm"）。</summary>
    public string? Display { get; set; }
    /// <summary>原始值（Double 為 Revit 內部單位；Integer/String/ElementId 原樣）。</summary>
    public string? Raw { get; set; }
    /// <summary>原始值的單位（例如 "ft (Revit internal)"）；非量測值為 null。</summary>
    public string? RawUnit { get; set; }
    /// <summary>長度類參數換算成 mm；非長度為 null。</summary>
    public double? Mm { get; set; }
    public string? Error { get; set; }
}

/// <summary>參數定義（每個族一份），values_by_type 依類型名存值。</summary>
public sealed class ParamDef
{
    public string Name { get; set; } = "";
    /// <summary>type／instance。</summary>
    public string Scope { get; set; } = "type";
    public string? BuiltIn { get; set; }
    public string? SharedGuid { get; set; }
    /// <summary>Revit ElementId（族參數 FamilyParameter.Id 或專案 Parameter.Id；內建參數為負值）。</summary>
    public long? ParamId { get; set; }
    /// <summary>ForgeTypeId spec（例如 autodesk.spec.aec:length-2.0.0）。</summary>
    public string? DataType { get; set; }
    /// <summary>文件單位設定中此 spec 的顯示單位 ForgeTypeId（非量測值為 null）。</summary>
    public string? Unit { get; set; }
    public string? Group { get; set; }
    public string StorageType { get; set; } = "";
    /// <summary>公式（僅族檔可得；專案端為 null）。</summary>
    public string? Formula { get; set; }
    public bool? IsReadOnly { get; set; }
    /// <summary>報告參數（僅族檔可得）。</summary>
    public bool? IsReporting { get; set; }
    public bool? IsDeterminedByFormula { get; set; }
    /// <summary>
    /// 值的性質：type_value（類型值）、family_default（族檔中實例參數的預設值，<b>不是</b>任何實例的實際值）。
    /// 專案中實例的實際值另存在 instances[].params。
    /// </summary>
    public string ValueKind { get; set; } = "type_value";
    public Dictionary<string, ParamValue> ValuesByType { get; set; } = new();
}

public sealed class InstanceParam
{
    public string Name { get; set; } = "";
    public string? BuiltIn { get; set; }
    public string? SharedGuid { get; set; }
    public long? ParamId { get; set; }
    public string? DataType { get; set; }
    public string? Unit { get; set; }
    public string StorageType { get; set; } = "";
    public bool? IsReadOnly { get; set; }
    public ParamValue Value { get; set; } = new();
}

/// <summary>專案中已放置的實例（實際值，與族預設值分開）。</summary>
public sealed class InstanceInfo
{
    public long ElementId { get; set; }
    public string TypeName { get; set; } = "";
    /// <summary>點位置：Revit 內部座標（ft）與 mm；線式族為起點，見 location_note。</summary>
    public double[]? LocationFt { get; set; }
    public double[]? LocationMm { get; set; }
    public string? LocationNote { get; set; }
    public string? Level { get; set; }
    public long? LevelId { get; set; }
    public string? Host { get; set; }
    public long? HostId { get; set; }
    public List<InstanceParam> Params { get; set; } = new();
}

public sealed class FamilyEntry
{
    /// <summary>project（目前專案已載入的族）／rfa（資料夾中的族檔）。兩來源分開，不合併。</summary>
    public string Source { get; set; } = "project";
    public string? FilePath { get; set; }
    public string FamilyName { get; set; } = "";
    public string? Category { get; set; }
    public List<string> Types { get; set; } = new();
    public string? PlacementType { get; set; }
    /// <summary>無法判定放置方式時為「待實機確認」。</summary>
    public string? PlacementNote { get; set; }
    public string? Hosting { get; set; }
    public bool? WorkPlaneBased { get; set; }
    public bool? AlwaysVertical { get; set; }
    public bool HangerLike { get; set; }
    public bool Focus { get; set; }
    /// <summary>ok／failed／skipped／cancelled_unprocessed。</summary>
    public string Status { get; set; } = "ok";
    public string? Reason { get; set; }
    public string? SavedInVersion { get; set; }
    public List<ParamDef> Params { get; set; } = new();
    /// <summary>此來源無法取得的欄位。來源 A（專案）不使用 EditFamily，故族檔才有的 formula／is_reporting／is_determined_by_formula 不可得。</summary>
    public List<string> UnavailableFields { get; set; } = new();
    public int InstanceCount { get; set; }
    public bool InstancesTruncated { get; set; }
    public List<InstanceInfo> Instances { get; set; } = new();
}

public sealed class SourceSummary
{
    public int Total { get; set; }
    public int Ok { get; set; }
    public int Failed { get; set; }
    public int Skipped { get; set; }
    public int CancelledUnprocessed { get; set; }
}

public sealed class FocusResult
{
    public string Family { get; set; } = "";
    public string Type { get; set; } = "";
    public bool FamilyInProject { get; set; }
    public bool TypeInProject { get; set; }
    public bool FamilyInRfa { get; set; }
    public bool TypeInRfa { get; set; }
    public string Note { get; set; } = "";
}

/// <summary>前後檢查：執行前後文件元素數量與 IsModified（只是比對結果，不是完整的唯讀證明）。</summary>
public sealed class ReadOnlyCheck
{
    public long ElementCountBefore { get; set; }
    public long ElementCountAfter { get; set; }
    public bool ModifiedBefore { get; set; }
    public bool ModifiedAfter { get; set; }
    public bool Passed => ElementCountBefore == ElementCountAfter && ModifiedBefore == ModifiedAfter;
}

/// <summary>Application.Documents 快照中的一份文件。</summary>
public sealed class DocInfo
{
    public string PathName { get; set; } = "";
    public string Title { get; set; } = "";
    public bool IsFamilyDocument { get; set; }
    /// <summary>顯示名稱：PathName 為空（未存檔）時改用 Title。</summary>
    public string Display => string.IsNullOrEmpty(PathName) ? Title : PathName;
    /// <summary>比對鍵：有路徑用路徑（不分大小寫），否則用標題。</summary>
    public string Key => string.IsNullOrEmpty(PathName) ? "title:" + Title : "path:" + PathName.ToUpperInvariant();
}

/// <summary>文件清理檢查：本工具開啟的族文件都已關閉、執行前已開啟的文件都仍開著。</summary>
public sealed class DocCleanupCheck
{
    public List<DocInfo> Before { get; set; } = new();
    public List<DocInfo> After { get; set; } = new();
    /// <summary>本工具以 OpenDocumentFile 開啟過的檔案路徑。</summary>
    public List<string> OpenedByTool { get; set; } = new();
    /// <summary>本工具開啟且已關閉。</summary>
    public List<string> ToolOpenedAndClosed { get; set; } = new();
    /// <summary>執行前已開啟、執行後仍開啟。</summary>
    public List<string> PreExistingStillOpen { get; set; } = new();
    public List<string> LeftOpenByTool { get; set; } = new();
    public List<string> PreExistingMissing { get; set; } = new();
    public List<string> UnexpectedNew { get; set; } = new();
    public bool Passed { get; set; }
}

public sealed class InventoryReport
{
    public int SchemaVersion { get; set; } = 1;
    public string Tool { get; set; } = "MepTrayImport.HangerInventory";
    public string? ToolVersion { get; set; }
    public string GeneratedAt { get; set; } = "";
    public string? RevitVersion { get; set; }
    public string? DocumentTitle { get; set; }
    public bool ProjectScanned { get; set; }
    /// <summary>all（全部族，吊架類標示 hanger_like）／hanger_like（只列名稱像吊架者）。</summary>
    public string ProjectFilter { get; set; } = "all";
    public string? RfaFolder { get; set; }
    public bool IncludeSubfolders { get; set; }
    public bool Cancelled { get; set; }
    public SourceSummary ProjectSummary { get; set; } = new();
    public SourceSummary RfaSummary { get; set; } = new();
    public FocusResult Focus { get; set; } = new();
    public ReadOnlyCheck? ReadOnlyCheck { get; set; }
    public DocCleanupCheck? DocCleanup { get; set; }
    public List<FamilyEntry> Families { get; set; } = new();
    public List<string> Notes { get; set; } = new();
}

// ───────────────────────── 純邏輯 ─────────────────────────

public static class HangerInventory
{
    public const string FocusFamily = "吊架(角鐵)";
    public const string FocusType = "矩形";
    public const string NeedsLiveCheck = "待實機確認";

    /// <summary>只用於「標示」吊架類（hanger_like），不作為過濾 .rfa 的條件。</summary>
    public static readonly string[] HangerKeywords = { "吊架", "支架", "吊桿", "管束", "固定架", "hanger", "support", "strut", "clamp" };

    public static bool IsHangerLike(params string?[] names) =>
        names.Any(n => !string.IsNullOrEmpty(n) && HangerKeywords.Any(k => n!.Contains(k, StringComparison.OrdinalIgnoreCase)));

    /// <summary>FamilyPlacementType 字串 → 可判定則 null，否則「待實機確認」。</summary>
    public static string? PlacementNote(string? placementType) =>
        string.IsNullOrWhiteSpace(placementType) || placementType is "Invalid" or "Unknown" ? NeedsLiveCheck : null;

    public const double MmPerFoot = 304.8;

    /// <summary>長度值（Revit 內部 ft）→ ParamValue（保留 raw、raw_unit 與 mm）。</summary>
    public static ParamValue Length(double feet, string? display) => new()
    {
        Status = "ok", Display = display, Raw = feet.ToString("R", CultureInfo.InvariantCulture),
        RawUnit = "ft (Revit internal)", Mm = Math.Round(feet * MmPerFoot, 6),
    };

    public static ParamValue NoValue() => new() { Status = "no_value" };
    public static ParamValue ReadError(Exception ex) => new() { Status = "read_error", Error = $"{ex.GetType().Name}: {ex.Message}" };

    /// <summary>"Autodesk Revit 2019"、"2019" 之類 → 2019；無法解析 → null。</summary>
    public static int? ParseRevitYear(string? format)
    {
        if (string.IsNullOrWhiteSpace(format)) return null;
        var m = Regex.Match(format, @"(20\d{2})");
        return m.Success ? int.Parse(m.Groups[1].Value, CultureInfo.InvariantCulture) : null;
    }

    static readonly Regex Backup = new(@"\.\d{4}\.rfa$", RegexOptions.IgnoreCase | RegexOptions.CultureInvariant);

    /// <summary>
    /// 列出資料夾內所有 .rfa（不依名稱或類別過濾；略過 Revit 備份檔 *.0001.rfa）。資料夾不存在或無法讀取時回傳空清單並給原因，不假裝成功。
    /// </summary>
    public static List<string> EnumerateRfa(string folder, bool includeSubfolders, out string? error, List<string>? skippedBackups = null)
    {
        error = null;
        if (string.IsNullOrWhiteSpace(folder)) { error = "未指定資料夾"; return new(); }
        if (!Directory.Exists(folder)) { error = $"資料夾不存在或無法存取：{folder}"; return new(); }
        try
        {
            var all = Directory.EnumerateFiles(folder, "*.rfa", includeSubfolders ? SearchOption.AllDirectories : SearchOption.TopDirectoryOnly).ToList();
            skippedBackups?.AddRange(all.Where(f => Backup.IsMatch(f)));
            return all.Where(f => !Backup.IsMatch(f)).OrderBy(f => f, StringComparer.Ordinal).ToList();
        }
        catch (Exception ex) when (ex is UnauthorizedAccessException or IOException or System.Security.SecurityException)
        {
            error = $"無法讀取資料夾（{ex.GetType().Name}）：{ex.Message}";
            return new();
        }
    }

    /// <summary>
    /// 逐檔批次處理：每檔一個 process 呼叫（必須在 Revit API 主執行緒、同一命令內呼叫）；單檔例外 → failed 並繼續；
    /// cancelled() 為 true 時停止，其餘標為 cancelled_unprocessed。progress(已完成, 總數, 目前檔案)。
    /// </summary>
    public static List<FamilyEntry> RunBatch(IReadOnlyList<string> files, Func<string, FamilyEntry> process, Func<bool> cancelled,
                                             Action<int, int, string>? progress = null, string source = "rfa")
    {
        var res = new List<FamilyEntry>();
        for (int i = 0; i < files.Count; i++)
        {
            var f = files[i];
            if (cancelled())
            {
                foreach (var rest in files.Skip(i))
                    res.Add(new FamilyEntry { Source = source, FilePath = rest, FamilyName = Path.GetFileNameWithoutExtension(rest),
                                              Status = "cancelled_unprocessed", Reason = "使用者取消，未處理" });
                break;
            }
            progress?.Invoke(i, files.Count, f);
            FamilyEntry e;
            try { e = process(f); }
            catch (Exception ex)
            {
                e = new FamilyEntry { Source = source, FilePath = f, FamilyName = Path.GetFileNameWithoutExtension(f), Status = "failed",
                                      Reason = $"{ex.GetType().Name}: {ex.Message}" };
            }
            res.Add(e);
        }
        progress?.Invoke(res.Count(r => r.Status != "cancelled_unprocessed"), files.Count, "");
        return res;
    }

    public static SourceSummary Summarize(IEnumerable<FamilyEntry> es)
    {
        var l = es.ToList();
        return new SourceSummary
        {
            Total = l.Count, Ok = l.Count(e => e.Status == "ok"), Failed = l.Count(e => e.Status == "failed"),
            Skipped = l.Count(e => e.Status == "skipped"), CancelledUnprocessed = l.Count(e => e.Status == "cancelled_unprocessed"),
        };
    }

    /// <summary>重點族 吊架(角鐵) → 類型 矩形 是否存在（完整名稱比對，不以相似名稱替代）。</summary>
    public static FocusResult FindFocus(IEnumerable<FamilyEntry> es)
    {
        var l = es.ToList();
        bool Fam(string src) => l.Any(e => e.Source == src && e.FamilyName == FocusFamily);
        bool Typ(string src) => l.Any(e => e.Source == src && e.FamilyName == FocusFamily && e.Types.Contains(FocusType));
        var r = new FocusResult
        {
            Family = FocusFamily, Type = FocusType, FamilyInProject = Fam("project"), TypeInProject = Typ("project"),
            FamilyInRfa = Fam("rfa"), TypeInRfa = Typ("rfa"),
        };
        var parts = new List<string>();
        parts.Add(r.TypeInProject ? "專案中有 吊架(角鐵) / 矩形" : r.FamilyInProject ? "專案中有 吊架(角鐵)，但沒有類型「矩形」" : "專案中未找到 吊架(角鐵)");
        parts.Add(r.TypeInRfa ? "族檔中有 吊架(角鐵) / 矩形" : r.FamilyInRfa ? "族檔中有 吊架(角鐵)，但沒有類型「矩形」" : "族檔中未找到 吊架(角鐵)");
        r.Note = string.Join("；", parts) + "（不以其他族替代）";
        return r;
    }

    public static readonly string[] ProjectUnavailableFields = { "formula", "is_reporting", "is_determined_by_formula" };

    /// <summary>
    /// 比對執行前後 Application.Documents：本工具開的必須全部關閉、原本開著的必須仍開著、不得多出其他文件。
    /// </summary>
    public static DocCleanupCheck CompareDocs(IEnumerable<DocInfo> before, IEnumerable<DocInfo> after, IEnumerable<string> openedByTool)
    {
        var b = before.ToList(); var a = after.ToList();
        var opened = openedByTool.Where(p => !string.IsNullOrEmpty(p)).Distinct(StringComparer.OrdinalIgnoreCase).ToList();
        var afterKeys = new HashSet<string>(a.Select(d => d.Key));
        var beforeKeys = new HashSet<string>(b.Select(d => d.Key));
        var openedKeys = new HashSet<string>(opened.Select(p => new DocInfo { PathName = p }.Key));
        var r = new DocCleanupCheck { Before = b, After = a, OpenedByTool = opened };
        r.LeftOpenByTool = opened.Where(p => afterKeys.Contains(new DocInfo { PathName = p }.Key)).ToList();
        r.ToolOpenedAndClosed = opened.Where(p => !afterKeys.Contains(new DocInfo { PathName = p }.Key)).ToList();
        r.PreExistingStillOpen = b.Where(d => afterKeys.Contains(d.Key)).Select(d => d.Display).ToList();
        r.PreExistingMissing = b.Where(d => !afterKeys.Contains(d.Key)).Select(d => d.Display).ToList();
        r.UnexpectedNew = a.Where(d => !beforeKeys.Contains(d.Key) && !openedKeys.Contains(d.Key)).Select(d => d.Display).ToList();
        r.Passed = r.LeftOpenByTool.Count == 0 && r.PreExistingMissing.Count == 0 && r.UnexpectedNew.Count == 0;
        return r;
    }

    /// <summary>排序（決定性）：重點族 → 來源 → 吊架類 → 族名 → 檔案路徑。</summary>
    public static void Finish(InventoryReport r)
    {
        foreach (var f in r.Families)
        {
            f.Focus = f.FamilyName == FocusFamily;
            f.PlacementNote ??= f.Status == "ok" ? PlacementNote(f.PlacementType) : null;
            f.Params = f.Params.OrderBy(p => p.Scope, StringComparer.Ordinal).ThenBy(p => p.Name, StringComparer.Ordinal).ToList();
        }
        r.Families = r.Families.OrderByDescending(f => f.Focus).ThenBy(f => f.Source, StringComparer.Ordinal)
            .ThenByDescending(f => f.HangerLike).ThenBy(f => f.FamilyName, StringComparer.Ordinal)
            .ThenBy(f => f.FilePath ?? "", StringComparer.Ordinal).ToList();
        r.ProjectSummary = Summarize(r.Families.Where(f => f.Source == "project"));
        r.RfaSummary = Summarize(r.Families.Where(f => f.Source == "rfa"));
        r.Focus = FindFocus(r.Families);
    }

    public static List<string> Validate(InventoryReport r)
    {
        var e = new List<string>();
        if (r.SchemaVersion != 1) e.Add("schema_version 應為 1");
        if (r.DocCleanup is { Passed: false }) e.Add("文件清理檢查失敗：有本工具開啟的文件未關閉，或原本開啟的文件不見");
        if (r.ReadOnlyCheck is { Passed: false }) e.Add("前後檢查不一致：元素數量或 IsModified 在執行前後不同");
        foreach (var f in r.Families)
        {
            var id = f.FilePath ?? f.FamilyName;
            if (f.Source is not ("project" or "rfa")) e.Add($"{id}: source 不合法");
            if (f.Status is not ("ok" or "failed" or "skipped" or "cancelled_unprocessed")) e.Add($"{id}: status 不合法");
            if (f.Status != "ok" && string.IsNullOrWhiteSpace(f.Reason)) e.Add($"{id}: {f.Status} 但無原因");
            if (f.Source == "rfa" && string.IsNullOrWhiteSpace(f.FilePath)) e.Add($"{id}: rfa 缺檔案路徑");
            if (f.Source == "rfa" && f.Instances.Count > 0) e.Add($"{id}: 族檔不應有實例");
            if (f.Source == "project" && f.Params.Any(p => p.Formula != null || p.IsReporting != null || p.IsDeterminedByFormula != null))
                e.Add($"{id}: 專案來源不應有 formula/reporting（未使用 EditFamily，無法取得）");
            foreach (var p in f.Params)
            {
                if (p.Scope is not ("type" or "instance")) e.Add($"{id}/{p.Name}: scope 不合法");
                if (p.ValueKind is not ("type_value" or "family_default")) e.Add($"{id}/{p.Name}: value_kind 不合法");
                if (p.Scope == "instance" && p.ValueKind != "family_default" && p.ValuesByType.Count > 0)
                    e.Add($"{id}/{p.Name}: 實例參數的族值必須標為 family_default");
                foreach (var (t, v) in p.ValuesByType) CheckValue(e, $"{id}/{p.Name}/{t}", v);
            }
            foreach (var i in f.Instances) foreach (var p in i.Params) CheckValue(e, $"{id}/#{i.ElementId}/{p.Name}", p.Value);
        }
        return e;
    }

    static void CheckValue(List<string> e, string where, ParamValue v)
    {
        if (v.Status is not ("ok" or "no_value" or "read_error")) e.Add($"{where}: value status 不合法");
        if (v.Status == "read_error" && string.IsNullOrWhiteSpace(v.Error)) e.Add($"{where}: read_error 但無錯誤訊息");
        if (v.Status == "no_value" && (v.Raw != null || v.Display != null)) e.Add($"{where}: no_value 卻有值");
    }

    static readonly JsonSerializerOptions Opt = new()
    {
        WriteIndented = true,
        PropertyNamingPolicy = JsonNamingPolicy.SnakeCaseLower,
        Encoder = JavaScriptEncoder.UnsafeRelaxedJsonEscaping,
    };

    public static string ToJson(InventoryReport r) => JsonSerializer.Serialize(r, Opt);
    public static InventoryReport FromJson(string s) => JsonSerializer.Deserialize<InventoryReport>(s, Opt) ?? throw new InvalidDataException("空報告");

    public static readonly string[] CsvColumns =
    {
        "source", "file_path", "family", "category", "family_status", "family_reason", "placement_type", "placement_note", "hosting",
        "work_plane_based", "row_kind", "type", "element_id", "level", "host", "location_mm", "param_name", "scope", "builtin",
        "shared_guid", "param_id", "data_type", "unit", "storage_type", "formula", "read_only", "reporting", "value_kind",
        "value_status", "display", "raw", "raw_unit", "mm", "error",
    };

    /// <summary>
    /// 可讀 CSV（每列一個值）：row_kind = family（族摘要列）／type_value／family_default／instance_actual。
    /// 呼叫端以 UTF-8 BOM 寫檔（Excel 正確顯示中文）。
    /// </summary>
    public static string ToCsv(InventoryReport r)
    {
        var sb = new StringBuilder();
        sb.Append(string.Join(",", CsvColumns)).Append("\r\n");
        string F(double? d) => d?.ToString("0.######", CultureInfo.InvariantCulture) ?? "";
        string Bo(bool? b) => b is null ? "" : b.Value ? "true" : "false";
        foreach (var f in r.Families)
        {
            string[] Fam() => new[] { f.Source, f.FilePath ?? "", f.FamilyName, f.Category ?? "", f.Status, f.Reason ?? "", f.PlacementType ?? "",
                                      f.PlacementNote ?? "", f.Hosting ?? "", Bo(f.WorkPlaneBased) };
            Row(sb, Fam().Concat(new[] { "family", string.Join(" | ", f.Types), "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "",
                                          $"instances={f.InstanceCount}{(f.InstancesTruncated ? " (truncated)" : "")}", "", "", "" }));
            foreach (var p in f.Params)
                foreach (var (t, v) in p.ValuesByType)
                    Row(sb, Fam().Concat(new[] { p.ValueKind, t, "", "", "", "", p.Name, p.Scope, p.BuiltIn ?? "", p.SharedGuid ?? "",
                        p.ParamId?.ToString(CultureInfo.InvariantCulture) ?? "", p.DataType ?? "", p.Unit ?? "", p.StorageType, p.Formula ?? "",
                        Bo(p.IsReadOnly), Bo(p.IsReporting), p.ValueKind, v.Status, v.Display ?? "", v.Raw ?? "", v.RawUnit ?? "", F(v.Mm), v.Error ?? "" }));
            foreach (var i in f.Instances)
            {
                var loc = i.LocationMm == null ? "" : string.Join(" ", i.LocationMm.Select(x => F(x)));
                foreach (var p in i.Params)
                    Row(sb, Fam().Concat(new[] { "instance_actual", i.TypeName, i.ElementId.ToString(CultureInfo.InvariantCulture), i.Level ?? "",
                        i.Host ?? "", loc, p.Name, "instance", p.BuiltIn ?? "", p.SharedGuid ?? "", p.ParamId?.ToString(CultureInfo.InvariantCulture) ?? "",
                        p.DataType ?? "", p.Unit ?? "", p.StorageType, "", Bo(p.IsReadOnly), "", "instance_actual", p.Value.Status,
                        p.Value.Display ?? "", p.Value.Raw ?? "", p.Value.RawUnit ?? "", F(p.Value.Mm), p.Value.Error ?? "" }));
            }
        }
        return sb.ToString();
    }

    static void Row(StringBuilder sb, IEnumerable<string> cells)
    {
        var l = cells.ToList();
        if (l.Count != CsvColumns.Length) throw new InvalidOperationException($"CSV 欄數 {l.Count} ≠ {CsvColumns.Length}");
        sb.Append(string.Join(",", l.Select(Esc))).Append("\r\n");
    }

    public static string Esc(string s)
    {
        if (s.Length > 0 && "=+-@".Contains(s[0]) && !double.TryParse(s, NumberStyles.Float, CultureInfo.InvariantCulture, out _))
            s = "'" + s;                                              // 防 Excel 公式注入（數字如 -1.5 不受影響）
        return s.IndexOfAny(new[] { ',', '"', '\r', '\n' }) >= 0 ? "\"" + s.Replace("\"", "\"\"") + "\"" : s;
    }

    /// <summary>寫出 JSON（UTF-8 無 BOM）與 CSV（UTF-8 BOM）。回傳 (jsonPath, csvPath)。</summary>
    public static (string json, string csv) Write(InventoryReport r, string jsonPath)
    {
        var dir = Path.GetDirectoryName(Path.GetFullPath(jsonPath));
        if (!string.IsNullOrEmpty(dir)) Directory.CreateDirectory(dir);
        var csvPath = Path.ChangeExtension(jsonPath, ".csv");
        File.WriteAllText(jsonPath, ToJson(r), new UTF8Encoding(false));
        File.WriteAllText(csvPath, ToCsv(r), new UTF8Encoding(true));
        return (jsonPath, csvPath);
    }
}