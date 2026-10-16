using System.Globalization;
using Autodesk.Revit.Attributes;
using Autodesk.Revit.DB;
using Autodesk.Revit.UI;
using MepTray.Core;
using RevitApp = Autodesk.Revit.ApplicationServices.Application;
using RevitUnits = Autodesk.Revit.DB.Units;

namespace MepTray.Revit;

/// <summary>
/// 「吊架族群盤點」：唯讀盤點 (A) 目前專案已載入的族與已放置實例、(B) 使用者選擇資料夾中的全部 .rfa。
/// 設計：未使用修改交易（命令宣告 TransactionMode.ReadOnly、程式不開 Transaction）、不呼叫放置/刪除/移動 API、不把族檔載入專案；
/// 自己開啟的族文件以 Close(false) 關閉（不儲存），使用者已開啟的文件不關閉。執行前後記錄元素數量與 IsModified 供比對。
/// 輸出：UTF-8 JSON ＋ UTF-8 BOM CSV（同主檔名）。
/// </summary>
[Transaction(TransactionMode.ReadOnly)]
public class HangerInventoryCommand : IExternalCommand
{
    public const string DefaultRfaFolder = @"F:\學習revit&MEP_PLAN\Revit課程資料\鑫亞電通\Autodesk 機電元件\04_台灣專用元件族\13_固定工具";
    const int MaxInstanceDetailsPerFamily = 200;

    public Result Execute(ExternalCommandData commandData, ref string message, ElementSet elements)
    {
        var uiapp = commandData.Application;
        var doc = uiapp.ActiveUIDocument?.Document;

        // 1. 來源
        var td = new TaskDialog("吊架族群盤點")
        {
            MainInstruction = "選擇盤點來源（未使用修改交易；不載入族；族檔關閉時不儲存）",
            MainContent = "兩個來源分開輸出：A＝目前專案已載入的族與已放置實例；B＝資料夾中的全部 .rfa（不依名稱或類別過濾）。",
            CommonButtons = TaskDialogCommonButtons.Cancel,
        };
        if (doc != null && !doc.IsFamilyDocument)
        {
            td.AddCommandLink(TaskDialogCommandLinkId.CommandLink1, "A ＋ B：目前專案（全部族）＋ .rfa 資料夾");
            td.AddCommandLink(TaskDialogCommandLinkId.CommandLink2, "只有 A：目前專案（全部族，吊架類會標示）");
            td.AddCommandLink(TaskDialogCommandLinkId.CommandLink3, "只有 A：目前專案（只列名稱像吊架者）");
        }
        td.AddCommandLink(TaskDialogCommandLinkId.CommandLink4, "只有 B：.rfa 資料夾");
        bool scanProject, scanRfa; string filter = "all";
        switch (td.Show())
        {
            case TaskDialogResult.CommandLink1: scanProject = true; scanRfa = true; break;
            case TaskDialogResult.CommandLink2: scanProject = true; scanRfa = false; break;
            case TaskDialogResult.CommandLink3: scanProject = true; scanRfa = false; filter = "hanger_like"; break;
            case TaskDialogResult.CommandLink4: scanProject = false; scanRfa = true; break;
            default: return Result.Cancelled;
        }

        // 2. 資料夾（不存在 → 顯示原因並可重選；不假裝成功）
        string? folder = null; bool includeSub = false; List<string> files = new(); var backups = new List<string>();
        if (scanRfa)
        {
            var candidate = DefaultRfaFolder;
            while (true)
            {
                var exists = Directory.Exists(candidate);
                var fd = new TaskDialog("吊架族群盤點 — .rfa 資料夾")
                {
                    MainInstruction = exists ? "使用這個資料夾？" : "資料夾不存在或無法存取",
                    MainContent = candidate,
                    CommonButtons = TaskDialogCommonButtons.Cancel,
                };
                if (exists)
                {
                    fd.AddCommandLink(TaskDialogCommandLinkId.CommandLink1, "使用此資料夾（含子資料夾）");
                    fd.AddCommandLink(TaskDialogCommandLinkId.CommandLink2, "使用此資料夾（不含子資料夾）");
                }
                fd.AddCommandLink(TaskDialogCommandLinkId.CommandLink3, "選擇其他資料夾…");
                fd.AddCommandLink(TaskDialogCommandLinkId.CommandLink4, "只盤點單一 .rfa 檔…");
                var r = fd.Show();
                if (r == TaskDialogResult.CommandLink4)
                {
                    var of = new Microsoft.Win32.OpenFileDialog { Title = "選擇單一 .rfa 族檔（只讀取，不會修改或儲存）", Filter = "Revit 族檔 (*.rfa)|*.rfa" };
                    if (Directory.Exists(candidate)) of.InitialDirectory = candidate;
                    if (of.ShowDialog() != true) continue;
                    if (!File.Exists(of.FileName)) { TaskDialog.Show("吊架族群盤點", $"檔案不存在或無法存取：\n{of.FileName}"); continue; }
                    files = new List<string> { of.FileName };
                    folder = Path.GetDirectoryName(of.FileName)!;
                    includeSub = false;
                    break;
                }
                if (r == TaskDialogResult.CommandLink3)
                {
                    var dlg = new Microsoft.Win32.OpenFolderDialog { Title = "選擇 .rfa 族檔資料夾（只讀取，不會修改或儲存）" };
                    if (Directory.Exists(candidate)) dlg.InitialDirectory = candidate;
                    if (dlg.ShowDialog() == true) candidate = dlg.FolderName;
                    continue;
                }
                if (r is not (TaskDialogResult.CommandLink1 or TaskDialogResult.CommandLink2)) return Result.Cancelled;
                includeSub = r == TaskDialogResult.CommandLink1;
                files = HangerInventory.EnumerateRfa(candidate, includeSub, out var err, backups);
                if (err != null) { TaskDialog.Show("吊架族群盤點", err + "\n請重新選擇。"); continue; }
                if (files.Count == 0)
                {
                    TaskDialog.Show("吊架族群盤點", $"資料夾中沒有 .rfa{(includeSub ? "（含子資料夾）" : "（未含子資料夾）")}：\n{candidate}\n請重新選擇。");
                    continue;
                }
                folder = candidate;
                break;
            }
        }

        // 3. 輸出位置
        var outDir = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.MyDocuments), "MEP_tray");
        try { Directory.CreateDirectory(outDir); } catch (Exception) { }
        var save = new Microsoft.Win32.SaveFileDialog
        {
            Title = "盤點報告存放位置（同時輸出同名 .csv）",
            Filter = "JSON (*.json)|*.json",
            FileName = $"吊架族群盤點_{DateTime.Now:yyyyMMdd_HHmmss}.json",
            InitialDirectory = Directory.Exists(outDir) ? outDir : null,
        };
        if (save.ShowDialog() != true) return Result.Cancelled;

        var rep = new InventoryReport
        {
            ToolVersion = typeof(HangerInventoryCommand).Assembly.GetName().Version?.ToString(),
            GeneratedAt = DateTimeOffset.Now.ToString("yyyy-MM-dd HH:mm:ss zzz", CultureInfo.InvariantCulture),
            RevitVersion = $"{uiapp.Application.VersionName} {uiapp.Application.SubVersionNumber}",
            DocumentTitle = doc?.Title, ProjectScanned = scanProject, ProjectFilter = filter, RfaFolder = folder, IncludeSubfolders = includeSub,
        };
        if (backups.Count > 0) rep.Notes.Add($"略過 Revit 備份檔 {backups.Count} 個（*.0001.rfa 之類）");

        var docsBefore = SnapshotDocs(uiapp.Application);
        var openedByTool = new List<string>();
        var progress = new ProgressWindow("吊架族群盤點", uiapp.MainWindowHandle);
        progress.Show();
        try
        {
            if (scanProject && doc != null)
            {
                rep.ReadOnlyCheck = new ReadOnlyCheck { ElementCountBefore = CountElements(doc), ModifiedBefore = doc.IsModified };
                rep.Families.AddRange(ScanProject(doc, filter, progress));
            }
            if (scanRfa && folder != null)
            {
                var app = uiapp.Application;
                var year = HangerInventory.ParseRevitYear(app.VersionNumber);
                rep.Families.AddRange(HangerInventory.RunBatch(files, f => ScanRfa(app, folder, f, year, openedByTool), () => { progress.Pump(); return progress.CancelRequested; },
                    (i, n, f) => progress.Report(".rfa 族檔", i, n, f.Length > 0 ? Path.GetRelativePath(folder, f) : "")));
            }
            if (rep.ReadOnlyCheck != null && doc != null)
            {
                rep.ReadOnlyCheck.ElementCountAfter = CountElements(doc);
                rep.ReadOnlyCheck.ModifiedAfter = doc.IsModified;
            }
            rep.Cancelled = progress.CancelRequested;
        }
        finally
        {
            progress.Finish();
            // 完成／取消／失敗都會到這裡：比對 Application.Documents 前後
            try { rep.DocCleanup = HangerInventory.CompareDocs(docsBefore, SnapshotDocs(uiapp.Application), openedByTool); }
            catch (Exception ex) { rep.Notes.Add($"文件清理檢查無法執行：{ex.Message}"); }
        }

        HangerInventory.Finish(rep);
        string jsonPath, csvPath;
        try { (jsonPath, csvPath) = HangerInventory.Write(rep, save.FileName); }
        catch (Exception ex) { message = $"無法寫入報告：{ex.Message}"; TaskDialog.Show("吊架族群盤點", message); return Result.Failed; }

        string S(SourceSummary s) => $"共 {s.Total}：成功 {s.Ok}、失敗 {s.Failed}、略過 {s.Skipped}、取消未處理 {s.CancelledUnprocessed}";
        var ro = rep.ReadOnlyCheck;
        TaskDialog.Show("吊架族群盤點完成",
            $"重點：{rep.Focus.Note}\n\n" +
            (scanProject ? $"A 專案族：{S(rep.ProjectSummary)}\n" : "") +
            (scanRfa ? $"B 族檔：{S(rep.RfaSummary)}\n" : "") +
            (rep.Cancelled ? "已取消：未處理項目已標示於報告。\n" : "") +
            (rep.DocCleanup is { } dc
                ? $"文件清理檢查：本工具開啟並已關閉 {dc.ToolOpenedAndClosed.Count}、開啟未關閉 {dc.LeftOpenByTool.Count}；原本開啟仍開啟 {dc.PreExistingStillOpen.Count}、原本開啟但不見 {dc.PreExistingMissing.Count}（{(dc.Passed ? "通過" : "不通過，請回報")}）\n"
                : "文件清理檢查：未能執行（見報告 notes）\n") +
            (ro != null ? $"未使用修改交易；元素數量及 IsModified 前後檢查結果為：元素數量 {ro.ElementCountBefore}→{ro.ElementCountAfter}、IsModified {ro.ModifiedBefore}→{ro.ModifiedAfter}（{(ro.Passed ? "前後一致" : "前後不一致，請回報")}）\n"
                       : "未使用修改交易；本次未盤點專案，未做元素數量及 IsModified 前後檢查\n") +
            $"\n報告：\n{jsonPath}\n{csvPath}");
        return Result.Succeeded;
    }

    static List<DocInfo> SnapshotDocs(RevitApp app) =>
        app.Documents.Cast<Document>().Select(d => new DocInfo { PathName = d.PathName ?? "", Title = d.Title ?? "", IsFamilyDocument = d.IsFamilyDocument }).ToList();

    static long CountElements(Document doc) => new FilteredElementCollector(doc).WhereElementIsNotElementType().GetElementCount()
                                               + new FilteredElementCollector(doc).WhereElementIsElementType().GetElementCount();

    // ───────── A. 專案 ─────────

    static List<FamilyEntry> ScanProject(Document doc, string filter, ProgressWindow progress)
    {
        var res = new List<FamilyEntry>();
        var families = new FilteredElementCollector(doc).OfClass(typeof(Family)).Cast<Family>().OrderBy(f => f.Name, StringComparer.Ordinal).ToList();
        var instances = new FilteredElementCollector(doc).OfClass(typeof(FamilyInstance)).Cast<FamilyInstance>()
            .Where(i => i.Symbol?.Family != null).GroupBy(i => i.Symbol.Family.Id.Value).ToDictionary(g => g.Key, g => g.ToList());
        var units = doc.GetUnits();
        for (int k = 0; k < families.Count; k++)
        {
            var fam = families[k];
            progress.Report("專案族", k, families.Count, fam.Name);
            if (progress.CancelRequested)
            {
                foreach (var rest in families.Skip(k))
                    res.Add(new FamilyEntry { Source = "project", FamilyName = rest.Name, Status = "cancelled_unprocessed", Reason = "使用者取消，未處理" });
                break;
            }
            var symbols = fam.GetFamilySymbolIds().Select(id => doc.GetElement(id)).OfType<FamilySymbol>().OrderBy(s => s.Name, StringComparer.Ordinal).ToList();
            var hanger = HangerInventory.IsHangerLike(new[] { fam.Name }.Concat(symbols.Select(s => s.Name)).ToArray());
            if (filter == "hanger_like" && !hanger) continue;
            // 來源 A 只讀專案中可得的資訊（FamilySymbol／實例參數、Family.FamilyPlacementType 等），不使用 EditFamily；
            // 族檔才有的公式/報告參數標為不可得（見 UnavailableFields）。
            var e = new FamilyEntry { Source = "project", FamilyName = fam.Name, HangerLike = hanger,
                                      UnavailableFields = HangerInventory.ProjectUnavailableFields.ToList() };
            try
            {
                e.Category = fam.FamilyCategory?.Name;
                ReadPlacement(fam, e);
                e.Types = symbols.Select(s => s.Name).ToList();
                var defs = new Dictionary<string, ParamDef>();
                foreach (var s in symbols)
                    foreach (Parameter p in s.Parameters)
                    {
                        if (p?.Definition == null) continue;
                        var key = Key(p);
                        if (!defs.TryGetValue(key, out var d)) defs[key] = d = DefFrom(p, units, "type");
                        d.ValuesByType[s.Name] = ReadValue(p);
                    }
                e.Params = defs.Values.ToList();
                if (instances.TryGetValue(fam.Id.Value, out var insts))
                {
                    e.InstanceCount = insts.Count;
                    e.InstancesTruncated = insts.Count > MaxInstanceDetailsPerFamily;
                    foreach (var i in insts.OrderBy(i => i.Id.Value).Take(MaxInstanceDetailsPerFamily)) e.Instances.Add(ReadInstance(doc, i, units));
                }
            }
            catch (Exception ex) { e.Status = "failed"; e.Reason = $"{ex.GetType().Name}: {ex.Message}"; }
            res.Add(e);
        }
        return res;
    }

    static string Key(Parameter p) =>
        p.Definition is InternalDefinition { BuiltInParameter: not BuiltInParameter.INVALID } idf ? "B:" + idf.BuiltInParameter
        : p.IsShared ? "S:" + p.GUID : "N:" + p.Definition.Name + "#" + p.Id.Value;

    static void ReadPlacement(Family fam, FamilyEntry e)
    {
        try { e.PlacementType = fam.FamilyPlacementType.ToString(); } catch (Exception) { e.PlacementType = null; }
        e.Hosting = fam.get_Parameter(BuiltInParameter.FAMILY_HOSTING_BEHAVIOR)?.AsValueString();
        var wp = fam.get_Parameter(BuiltInParameter.FAMILY_WORK_PLANE_BASED);
        if (wp != null && wp.HasValue) e.WorkPlaneBased = wp.AsInteger() != 0;
        var av = fam.get_Parameter(BuiltInParameter.FAMILY_ALWAYS_VERTICAL);
        if (av != null && av.HasValue) e.AlwaysVertical = av.AsInteger() != 0;
    }

    static string? UnitOf(RevitUnits units, ForgeTypeId? spec)
    {
        try { return spec != null && UnitUtils.IsMeasurableSpec(spec) ? units.GetFormatOptions(spec).GetUnitTypeId().TypeId : null; }
        catch (Exception) { return null; }
    }

    static ParamDef DefFrom(Parameter p, RevitUnits units, string scope)
    {
        var d = new ParamDef { Name = p.Definition.Name, Scope = scope, StorageType = p.StorageType.ToString(), IsReadOnly = p.IsReadOnly, ValueKind = "type_value" };
        if (p.Definition is InternalDefinition idf && idf.BuiltInParameter != BuiltInParameter.INVALID) d.BuiltIn = idf.BuiltInParameter.ToString();
        try { if (p.IsShared) d.SharedGuid = p.GUID.ToString(); } catch (Exception) { }
        d.ParamId = p.Id.Value;
        ForgeTypeId? spec = null;
        try { spec = p.Definition.GetDataType(); d.DataType = spec?.TypeId; } catch (Exception) { }
        try { d.Group = p.Definition.GetGroupTypeId()?.TypeId; } catch (Exception) { }
        d.Unit = UnitOf(units, spec);
        return d;
    }

    static bool IsLength(ForgeTypeId? spec) => spec != null && spec == SpecTypeId.Length;

    static ParamValue ReadValue(Parameter p)
    {
        try
        {
            if (!p.HasValue) return HangerInventory.NoValue();
            var display = p.AsValueString();
            switch (p.StorageType)
            {
                case StorageType.Double:
                    var raw = p.AsDouble();
                    ForgeTypeId? spec = null;
                    try { spec = p.Definition.GetDataType(); } catch (Exception) { }
                    if (IsLength(spec)) return HangerInventory.Length(raw, display);
                    return new ParamValue { Display = display, Raw = raw.ToString("R", CultureInfo.InvariantCulture),
                                            RawUnit = spec != null && UnitUtils.IsMeasurableSpec(spec) ? "Revit internal" : null };
                case StorageType.Integer: return new ParamValue { Display = display, Raw = p.AsInteger().ToString(CultureInfo.InvariantCulture) };
                case StorageType.String: return new ParamValue { Display = display ?? p.AsString(), Raw = p.AsString() };
                case StorageType.ElementId: return new ParamValue { Display = display, Raw = p.AsElementId()?.Value.ToString(CultureInfo.InvariantCulture) };
                default: return new ParamValue { Display = display };
            }
        }
        catch (Exception ex) { return HangerInventory.ReadError(ex); }
    }

    static InstanceInfo ReadInstance(Document doc, FamilyInstance i, RevitUnits units)
    {
        var info = new InstanceInfo { ElementId = i.Id.Value, TypeName = i.Symbol?.Name ?? "" };
        try
        {
            XYZ? pt = (i.Location as LocationPoint)?.Point;
            if (pt == null && i.Location is LocationCurve lc) { pt = lc.Curve.GetEndPoint(0); info.LocationNote = "線式族：記錄起點"; }
            if (pt != null)
            {
                info.LocationFt = new[] { pt.X, pt.Y, pt.Z };
                info.LocationMm = info.LocationFt.Select(v => Math.Round(v * HangerInventory.MmPerFoot, 3)).ToArray();
            }
            else info.LocationNote = "無點/線位置";
        }
        catch (Exception ex) { info.LocationNote = $"位置讀取失敗: {ex.Message}"; }
        try
        {
            var lid = i.LevelId;
            if (lid == null || lid == ElementId.InvalidElementId) lid = i.get_Parameter(BuiltInParameter.FAMILY_LEVEL_PARAM)?.AsElementId();
            if (lid != null && lid != ElementId.InvalidElementId) { info.LevelId = lid.Value; info.Level = doc.GetElement(lid)?.Name; }
        }
        catch (Exception) { }
        try { if (i.Host != null) { info.HostId = i.Host.Id.Value; info.Host = $"{i.Host.Category?.Name}: {i.Host.Name}"; } } catch (Exception) { }
        foreach (Parameter p in i.Parameters)
        {
            if (p?.Definition == null) continue;
            var d = DefFrom(p, units, "instance");
            info.Params.Add(new InstanceParam { Name = d.Name, BuiltIn = d.BuiltIn, SharedGuid = d.SharedGuid, ParamId = d.ParamId, DataType = d.DataType,
                                                Unit = d.Unit, StorageType = d.StorageType, IsReadOnly = d.IsReadOnly, Value = ReadValue(p) });
        }
        info.Params = info.Params.OrderBy(p => p.Name, StringComparer.Ordinal).ToList();
        return info;
    }

    // ───────── B. .rfa 族檔 ─────────

    /// <summary>亦供「讀取目標吊架族」重用（同樣唯讀、Close(false)）。</summary>
    internal static FamilyEntry ScanRfa(RevitApp app, string folder, string path, int? appYear, List<string> openedByTool)
    {
        var e = new FamilyEntry { Source = "rfa", FilePath = path, FamilyName = Path.GetFileNameWithoutExtension(path) };
        e.HangerLike = HangerInventory.IsHangerLike(e.FamilyName, Path.GetRelativePath(folder, path));
        try { using var fs = new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.ReadWrite); }
        catch (UnauthorizedAccessException ex) { e.Status = "failed"; e.Reason = $"權限不足：{ex.Message}"; return e; }
        catch (IOException ex) { e.Status = "failed"; e.Reason = $"無法讀取（被鎖定或 I/O 錯誤）：{ex.Message}"; return e; }
        try
        {
            var bfi = BasicFileInfo.Extract(path);
            e.SavedInVersion = bfi?.Format;
            var y = HangerInventory.ParseRevitYear(bfi?.Format);
            if (y != null && appYear != null && y > appYear)
            {
                e.Status = "skipped"; e.Reason = $"較新版本：族檔存於 {bfi!.Format}，目前 Revit {appYear} 無法開啟"; return e;
            }
        }
        catch (Exception ex) { e.SavedInVersion = null; e.Reason = $"BasicFileInfo 讀取失敗（{ex.GetType().Name}），仍嘗試開啟"; }

        Document? fdoc = null; bool openedHere = false;
        try
        {
            fdoc = app.Documents.Cast<Document>().FirstOrDefault(d => string.Equals(d.PathName, path, StringComparison.OrdinalIgnoreCase));
            if (fdoc == null) { openedByTool.Add(path); fdoc = app.OpenDocumentFile(path); openedHere = true; }
            if (fdoc == null || !fdoc.IsFamilyDocument) throw new InvalidOperationException("開啟結果不是族文件");
            ReadFamilyDoc(fdoc, e);
            e.Status = "ok";
            if (!openedHere) e.Reason = "此族檔已由使用者開啟：直接讀取，未關閉";
            else if (e.Reason != null && e.Reason.StartsWith("BasicFileInfo")) { /* 保留提示 */ }
            else e.Reason = null;
        }
        catch (Autodesk.Revit.Exceptions.CorruptModelException ex) { e.Status = "failed"; e.Reason = $"檔案損毀：{ex.Message}"; }
        catch (Autodesk.Revit.Exceptions.FileAccessException ex) { e.Status = "failed"; e.Reason = $"無法存取/版本不符：{ex.Message}"; }
        catch (Autodesk.Revit.Exceptions.FileNotFoundException ex) { e.Status = "failed"; e.Reason = $"找不到檔案：{ex.Message}"; }
        catch (Exception ex) { e.Status = "failed"; e.Reason = $"{ex.GetType().Name}: {ex.Message}"; }
        finally
        {
            if (fdoc != null && openedHere)
            {
                try { fdoc.Close(false); }   // false = 不儲存；只關閉本命令自己開啟的族文件
                catch (Exception ex) { e.Reason = (e.Reason != null ? e.Reason + " | " : "") + $"關閉失敗：{ex.Message}"; }
            }
        }
        return e;
    }

    static void ReadFamilyDoc(Document fdoc, FamilyEntry e)
    {
        var fam = fdoc.OwnerFamily;
        if (!string.IsNullOrEmpty(fam?.Name)) e.FamilyName = Path.GetFileNameWithoutExtension(e.FilePath!) ?? fam!.Name;
        e.Category = fam?.FamilyCategory?.Name;
        if (fam != null) ReadPlacement(fam, e);
        var units = fdoc.GetUnits();
        var fm = fdoc.FamilyManager;
        var types = fm.Types.Cast<FamilyType>().Where(t => t != null).ToList();
        e.Types = types.Select(t => t.Name).ToList();
        foreach (FamilyParameter fp in fm.Parameters)
        {
            if (fp?.Definition == null) continue;
            var d = new ParamDef
            {
                Name = fp.Definition.Name, Scope = fp.IsInstance ? "instance" : "type", StorageType = fp.StorageType.ToString(),
                IsReadOnly = fp.IsReadOnly, IsReporting = fp.IsReporting, IsDeterminedByFormula = fp.IsDeterminedByFormula,
                Formula = string.IsNullOrEmpty(fp.Formula) ? null : fp.Formula, ParamId = fp.Id.Value,
                ValueKind = fp.IsInstance ? "family_default" : "type_value",
            };
            if (fp.Definition is InternalDefinition idf && idf.BuiltInParameter != BuiltInParameter.INVALID) d.BuiltIn = idf.BuiltInParameter.ToString();
            try { if (fp.IsShared) d.SharedGuid = fp.GUID.ToString(); } catch (Exception) { }
            ForgeTypeId? spec = null;
            try { spec = fp.Definition.GetDataType(); d.DataType = spec?.TypeId; } catch (Exception) { }
            try { d.Group = fp.Definition.GetGroupTypeId()?.TypeId; } catch (Exception) { }
            d.Unit = UnitOf(units, spec);
            foreach (var t in types) d.ValuesByType[t.Name] = ReadFamilyValue(t, fp, spec);
            e.Params.Add(d);
        }
    }

    static ParamValue ReadFamilyValue(FamilyType t, FamilyParameter fp, ForgeTypeId? spec)
    {
        try
        {
            if (!t.HasValue(fp)) return HangerInventory.NoValue();
            var display = t.AsValueString(fp);
            switch (fp.StorageType)
            {
                case StorageType.Double:
                    var raw = t.AsDouble(fp);
                    if (raw == null) return HangerInventory.NoValue();
                    if (IsLength(spec)) return HangerInventory.Length(raw.Value, display);
                    return new ParamValue { Display = display, Raw = raw.Value.ToString("R", CultureInfo.InvariantCulture),
                                            RawUnit = spec != null && UnitUtils.IsMeasurableSpec(spec) ? "Revit internal" : null };
                case StorageType.Integer: return new ParamValue { Display = display, Raw = t.AsInteger(fp)?.ToString(CultureInfo.InvariantCulture) };
                case StorageType.String: return new ParamValue { Display = display ?? t.AsString(fp), Raw = t.AsString(fp) };
                case StorageType.ElementId: return new ParamValue { Display = display, Raw = t.AsElementId(fp)?.Value.ToString(CultureInfo.InvariantCulture) };
                default: return new ParamValue { Display = display };
            }
        }
        catch (Exception ex) { return HangerInventory.ReadError(ex); }
    }
}