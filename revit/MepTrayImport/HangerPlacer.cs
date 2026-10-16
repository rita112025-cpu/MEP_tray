using System.Globalization;
using System.Text.Encodings.Web;
using System.Text.Json;
using Autodesk.Revit.DB;
using Autodesk.Revit.DB.Electrical;
using Autodesk.Revit.DB.ExtensibleStorage;
using Autodesk.Revit.DB.Mechanical;
using Autodesk.Revit.DB.Structure;
using MepTray.Core;
using Units = MepTray.Core.Units;

namespace MepTray.Revit;

/// <summary>單支吊架的計畫與結果。</summary>
public sealed class HangerItem
{
    public string SupportId { get; set; } = "";
    public string TrayId { get; set; } = "";
    public string TrayUniqueId { get; set; } = "";
    public double StationMm { get; set; }
    public double[] TrayPointMm { get; set; } = new double[3];       // 橋架中心線參考點（Revit 內部座標，mm）
    public double[] BearingPointMm { get; set; } = new double[3];    // 承托面點
    public double[] InsertionPointMm { get; set; } = new double[3];  // 族插入點（依設定換算）
    public double RotationDeg { get; set; }
    public double TrayDirectionDeg { get; set; }
    public double CrossMemberLengthMm { get; set; }
    public double? RodLengthMm { get; set; }
    public string Reason { get; set; } = "";
    /// <summary>new | exists | pending(待確認) | created | failed</summary>
    public string Status { get; set; } = "";
    public string Detail { get; set; } = "";
    public string? ElementId { get; set; }
    public string? TypeName { get; set; }
}

public sealed class HangerRunReport
{
    public string BatchId { get; set; } = "";
    public string Entry { get; set; } = "";        // import | selection
    public string Coordinates { get; set; } = "Revit 內部座標（mm）；TrayPoint=橋架中心線，Bearing=承托面，Insertion=族插入點";
    /// <summary>完成 | 部分完成 | 失敗 | 已取消 | 預覽</summary>
    public string Outcome { get; set; } = "預覽";
    public List<string> PreflightErrors { get; } = new();
    public List<SupportIssue> LayoutIssues { get; } = new();
    public List<HangerItem> Items { get; } = new();
    public List<string> RevitWarnings { get; } = new();
    public List<string> Notes { get; } = new();

    public int Count(string status) => Items.Count(i => i.Status == status);

    public string Summary()
    {
        var s = $"批次 {BatchId}（{Outcome}）\n計畫吊架 {Items.Count}：新增 {Count("new") + Count("created")}、已存在 {Count("exists")}、待確認 {Count("pending")}、失敗 {Count("failed")}";
        if (Count("created") > 0) s += $"\n已建立 {Count("created")}";
        if (PreflightErrors.Count > 0) s += "\n預檢失敗（未建立任何吊架）：\n  " + string.Join("\n  ", PreflightErrors);
        var bad = LayoutIssues.Where(i => i.Level is "failed" or "unsupported" or "warning").ToList();
        if (bad.Count > 0)
            s += "\n佈置問題：\n  " + string.Join("\n  ", bad.Take(30).Select(i => $"[{i.Level}] {i.Code} {i.PieceId} @({i.LocationMm[0]:F0},{i.LocationMm[1]:F0},{i.LocationMm[2]:F0}) {i.Detail}"))
                 + (bad.Count > 30 ? $"\n  …其餘 {bad.Count - 30} 筆見報告檔" : "");
        var fp = Items.Where(i => i.Status is "pending" or "failed").ToList();
        if (fp.Count > 0)
            s += "\n待確認／失敗：\n  " + string.Join("\n  ", fp.Take(30).Select(i => $"{i.Status} 橋架 {i.TrayId} station {i.StationMm:F0}：{i.Detail}"))
                 + (fp.Count > 30 ? $"\n  …其餘 {fp.Count - 30} 筆見報告檔" : "");
        if (Notes.Count > 0) s += "\n附註：\n  " + string.Join("\n  ", Notes);
        if (RevitWarnings.Count > 0) s += "\nRevit 警告：\n  " + string.Join("\n  ", RevitWarnings.Take(20));
        return s;
    }

    static readonly JsonSerializerOptions Json = new() { WriteIndented = true, Encoder = JavaScriptEncoder.UnsafeRelaxedJsonEscaping, IncludeFields = true };
    public string ToJson() => JsonSerializer.Serialize(this, Json);
}

/// <summary>
/// 吊架放置服務（兩個入口共用）：讀取最終橋架幾何（接頭建立後）→ Supports.Layout → 預檢 → 逐支 SubTransaction 放置。
/// 追蹤資料以 Extensible Storage 寫在吊架實例上（來源/橋架 UniqueId/station/方向/批次），重跑時據此去重；
/// 不修改、不刪除、不接管非本工具建立的吊架；不確定時標為待確認。
/// </summary>
public static class HangerPlacer
{
    public const string ToolSource = "MEP_tray";
    static readonly Guid SchemaGuid = new("4b6f2a1e-8d3c-4f57-9a0b-6e2d1c7f3a95");
    const double ReadbackTolMm = 0.5;
    const double ReadbackTolDeg = 0.05;

    static string F(double v) => v.ToString("R", CultureInfo.InvariantCulture);
    static double[] Mm(XYZ p) => new[] { Units.FeetToMm(p.X), Units.FeetToMm(p.Y), Units.FeetToMm(p.Z) };
    static XYZ Ft(double[] mm) => new(Units.MmToFeet(mm[0]), Units.MmToFeet(mm[1]), Units.MmToFeet(mm[2]));

    public static Schema GetSchema()
    {
        var s = Schema.Lookup(SchemaGuid);
        if (s != null) return s;
        var b = new SchemaBuilder(SchemaGuid);
        b.SetSchemaName("MepTrayHanger");
        b.SetDocumentation("MEP_tray 吊架追蹤（來源、橋架 UniqueId、支撐 station、方向、批次）");
        b.SetReadAccessLevel(AccessLevel.Public);
        b.SetWriteAccessLevel(AccessLevel.Public);
        foreach (var f in new[] { "Source", "TrayUniqueId", "StationMm", "DirectionDeg", "BatchId", "SupportId", "ToolVersion" })
            b.AddSimpleField(f, typeof(string));
        return b.Finish();
    }

    // ── 1. 最終橋架幾何 → pieces/links ──
    public static (List<SupportPiece> Pieces, List<SupportLink> Links, Dictionary<string, CableTray> Trays, List<string> Problems) ReadTrays(Document doc, IEnumerable<ElementId> ids)
    {
        var pieces = new List<SupportPiece>();
        var links = new List<SupportLink>();
        var trays = new Dictionary<string, CableTray>();
        var problems = new List<string>();
        var sel = ids.Select(doc.GetElement).OfType<CableTray>().OrderBy(t => t.Id.Value).ToList();
        var selIds = new HashSet<long>(sel.Select(t => t.Id.Value));
        var linkSeen = new HashSet<string>();
        foreach (var t in sel)
        {
            var key = t.Id.Value.ToString(CultureInfo.InvariantCulture);
            if (t.Location is not LocationCurve lc || lc.Curve is not Line line) { problems.Add($"橋架 {key} 不是直線段，未處理"); continue; }
            var wp = t.get_Parameter(BuiltInParameter.RBS_CABLETRAY_WIDTH_PARAM);
            var hp = t.get_Parameter(BuiltInParameter.RBS_CABLETRAY_HEIGHT_PARAM);
            var piece = new SupportPiece
            {
                Id = key, Start = Mm(line.GetEndPoint(0)), End = Mm(line.GetEndPoint(1)),
                WidthMm = wp == null ? double.NaN : Units.FeetToMm(wp.AsDouble()), HeightMm = hp == null ? double.NaN : Units.FeetToMm(hp.AsDouble()),
            };
            foreach (Connector c in t.ConnectorManager.Connectors)
            {
                if (c.ConnectorType != ConnectorType.End) continue;
                bool atStart = c.Origin.DistanceTo(line.GetEndPoint(0)) <= c.Origin.DistanceTo(line.GetEndPoint(1));
                string kind = "free";
                foreach (Connector r in c.AllRefs)
                {
                    if (r.Owner is null || r.Owner.Id == t.Id || r.ConnectorType == ConnectorType.Logical) continue;
                    if (r.Owner is CableTray other)
                    {
                        if (selIds.Contains(other.Id.Value) && linkSeen.Add(Pair(t.Id.Value, other.Id.Value)))
                            links.Add(new SupportLink(key, other.Id.Value.ToString(CultureInfo.InvariantCulture), "union"));
                        continue;
                    }
                    if (r.Owner is FamilyInstance fi)
                    {
                        kind = "fitting";
                        var pt = (fi.MEPModel as MechanicalFitting)?.PartType;
                        if (pt is PartType.Union or PartType.Transition)
                        {
                            foreach (var o in OtherTrays(fi, t.Id))
                                if (selIds.Contains(o.Value) && linkSeen.Add(Pair(t.Id.Value, o.Value)))
                                    links.Add(new SupportLink(key, o.Value.ToString(CultureInfo.InvariantCulture), "union"));
                        }
                    }
                }
                if (atStart) piece.StartKind = kind; else piece.EndKind = kind;
            }
            pieces.Add(piece);
            trays[key] = t;
        }
        return (pieces, links, trays, problems);
    }

    static string Pair(long a, long b) => a < b ? $"{a}-{b}" : $"{b}-{a}";

    static IEnumerable<ElementId> OtherTrays(FamilyInstance fitting, ElementId self)
    {
        var cm = fitting.MEPModel?.ConnectorManager;
        if (cm == null) yield break;
        foreach (Connector c in cm.Connectors)
            foreach (Connector r in c.AllRefs)
                if (r.Owner is CableTray ct && ct.Id != self) yield return ct.Id;
    }

    // ── 2. 既有吊架（本工具 / 其他）──
    public static List<ExistingHanger> ReadExisting(Document doc, string familyName)
    {
        var schema = GetSchema();
        var list = new List<ExistingHanger>();
        var seen = new HashSet<long>();
        foreach (var fi in new FilteredElementCollector(doc).WherePasses(new ExtensibleStorageFilter(schema.GUID)).OfType<FamilyInstance>())
        {
            var e = fi.GetEntity(schema);
            if (!e.IsValid() || e.Get<string>("Source") != ToolSource) continue;
            seen.Add(fi.Id.Value);
            list.Add(new ExistingHanger(fi.Id.Value.ToString(CultureInfo.InvariantCulture), "tool", e.Get<string>("TrayUniqueId"),
                Parse(e.Get<string>("StationMm")), PointOf(fi), Parse(e.Get<string>("DirectionDeg"))));
        }
        foreach (var fi in new FilteredElementCollector(doc).OfClass(typeof(FamilyInstance)).Cast<FamilyInstance>())
        {
            if (seen.Contains(fi.Id.Value) || fi.Symbol?.Family?.Name != familyName) continue;
            list.Add(new ExistingHanger(fi.Id.Value.ToString(CultureInfo.InvariantCulture), "other", null, null, PointOf(fi), null));
        }
        return list;
    }

    static double? Parse(string? s) => double.TryParse(s, NumberStyles.Float, CultureInfo.InvariantCulture, out var v) ? v : null;
    static double[]? PointOf(FamilyInstance fi) => fi.Location is LocationPoint lp ? Mm(lp.Point) : null;

    // ── 3. 預檢（不改模型）──
    public sealed class Resolved
    {
        public FamilySymbol Symbol = null!;
        public FamilyPlacementType PlacementType;
    }

    public static Resolved? Preflight(Document doc, HangerConfig cfg, List<string> errors)
    {
        var syms = new FilteredElementCollector(doc).OfClass(typeof(FamilySymbol)).Cast<FamilySymbol>()
            .Where(s => s.FamilyName == cfg.FamilyName).ToList();
        if (syms.Count == 0) { errors.Add($"專案中未載入族「{cfg.FamilyName}」（請先載入；工具不自動載入族）"); return null; }
        var sym = syms.FirstOrDefault(s => s.Name == cfg.TypeName);
        if (sym == null) { errors.Add($"族「{cfg.FamilyName}」沒有類型「{cfg.TypeName}」；現有：" + string.Join(", ", syms.Select(s => s.Name))); return null; }
        var pt = sym.Family.FamilyPlacementType;
        var need = pt switch { FamilyPlacementType.OneLevelBased => "level", FamilyPlacementType.WorkPlaneBased => "workplane", _ => null };
        if (need == null) errors.Add($"族放置方式 {pt} 需要主體/面（v1 不支援；設定固定吊桿長度也不能繞過主體需求）");
        else if (need != cfg.PlacementMode) errors.Add($"placement_mode={cfg.PlacementMode} 與族放置方式 {pt}（需 {need}）不符");
        errors.AddRange(HangerCalibration.CheckCalibration(doc, cfg));   // 族檔或專案中的族變更 → 必須重新校正
        CheckParam(sym, cfg.CrossMemberLength!, "parameter_map.cross_member_length", errors);
        if (cfg.RodMode == "fixed") CheckParam(sym, cfg.RodParam!, "rod_length.param（固定吊桿長度）", errors, unsupportedIfMissing: true);
        return errors.Count == 0 ? new Resolved { Symbol = sym, PlacementType = pt } : null;
    }

    static void CheckParam(FamilySymbol sym, ParamRef pr, string field, List<string> errors, bool unsupportedIfMissing = false)
    {
        if (pr.Scope == "type")
        {
            var p = sym.LookupParameter(pr.Name);
            if (p == null) { errors.Add(unsupportedIfMissing ? $"{field}：族類型沒有參數「{pr.Name}」→ 固定吊桿長度 unsupported" : $"{field}：族類型沒有參數「{pr.Name}」"); return; }
            if (p.StorageType != StorageType.Double || p.Definition.GetDataType() != SpecTypeId.Length) errors.Add($"{field}：參數「{pr.Name}」不是長度參數");
            if (p.IsReadOnly) errors.Add($"{field}：類型參數「{pr.Name}」唯讀（可能由公式驅動），無法依橋架寬度設定");
        }
        else if (sym.LookupParameter(pr.Name) != null)
            errors.Add($"{field}：設定為 instance，但「{pr.Name}」是類型參數");
        // instance 參數只有在建立實例後才能確認；屆時不存在/唯讀 → 該支 failed 並回滾
    }

    // ── 4. 計畫（不改模型）──
    public static HangerRunReport Plan(Document doc, IEnumerable<ElementId> trayIds, HangerConfig cfg, string entry, out Dictionary<string, CableTray> trays, out Resolved? resolved)
    {
        var rep = new HangerRunReport { BatchId = $"{DateTime.Now:yyyyMMdd-HHmmss}-{Guid.NewGuid():N}"[..24], Entry = entry };
        var (pieces, links, tr, problems) = ReadTrays(doc, trayIds);
        trays = tr;
        var trayMap = tr;
        rep.Notes.AddRange(problems);
        resolved = Preflight(doc, cfg, rep.PreflightErrors);
        if (pieces.Count == 0) rep.PreflightErrors.Add("沒有可處理的 Cable Tray");
        var lay = Supports.Layout(pieces, links, cfg);
        rep.LayoutIssues.AddRange(lay.Issues);
        var plan = lay.Supports.Select(s => new PlannedKey(trayMap[s.PieceId].UniqueId, s.StationMm, s.PointMm, s.DirectionDeg)).ToList();
        var cls = Supports.Classify(plan, ReadExisting(doc, cfg.FamilyName), cfg.DedupeToleranceMm, cfg.DedupeAngleToleranceDeg);
        for (int i = 0; i < lay.Supports.Count; i++)
        {
            var s = lay.Supports[i];
            var pl = Supports.Place(s, cfg);
            rep.Items.Add(new HangerItem
            {
                SupportId = s.Id, TrayId = s.PieceId, TrayUniqueId = trayMap[s.PieceId].UniqueId, StationMm = s.StationMm,
                TrayPointMm = s.PointMm, BearingPointMm = pl.BearingPointMm, InsertionPointMm = pl.InsertionPointMm, RotationDeg = pl.RotationDeg,
                TrayDirectionDeg = s.DirectionDeg, CrossMemberLengthMm = pl.CrossMemberLengthMm, RodLengthMm = pl.RodLengthMm, Reason = s.Reason,
                Status = cls[i].Status,
                Detail = cls[i].Status == "new" ? "" : cls[i].Reason + (cls[i].Match.Count > 0 ? "（元素 " + string.Join(",", cls[i].Match) + "）" : ""),
            });
        }
        // 承托面交叉檢查：以 Revit 自己的底部高程參數比對 中心線−高度/2
        foreach (var (id, t) in trays)
        {
            var bp = t.get_Parameter(BuiltInParameter.RBS_CTC_BOTTOM_ELEVATION);
            if (bp == null || t.Location is not LocationCurve lc) continue;
            var lvl = doc.GetElement(t.ReferenceLevel?.Id ?? ElementId.InvalidElementId) as Level;
            if (lvl == null) continue;
            var bottomAbs = Units.FeetToMm(lvl.Elevation + bp.AsDouble());
            var h = Units.FeetToMm(t.get_Parameter(BuiltInParameter.RBS_CABLETRAY_HEIGHT_PARAM).AsDouble());
            var calc = Units.FeetToMm(lc.Curve.GetEndPoint(0).Z) - h / 2;
            if (Math.Abs(bottomAbs - calc) > ReadbackTolMm)
                rep.Notes.Add($"橋架 {id}：Revit 底部高程 {bottomAbs:F1} mm 與 中心線−高度/2 = {calc:F1} mm 不一致（差 {bottomAbs - calc:F1}），請確認承托面基準");
        }
        if (rep.PreflightErrors.Count > 0) rep.Outcome = "失敗";
        return rep;
    }

    // ── 5. 放置（呼叫端已開 Transaction，並在 TransactionGroup 內）──
    public static void Execute(Document doc, HangerRunReport rep, HangerConfig cfg, Resolved res)
    {
        var schema = GetSchema();
        var levels = new FilteredElementCollector(doc).OfClass(typeof(Level)).Cast<Level>().OrderBy(l => l.Elevation).ToList();
        if (levels.Count == 0) throw new InvalidOperationException("文件中沒有任何 Level");
        var typeCache = new Dictionary<string, FamilySymbol>();
        var planes = new Dictionary<long, SketchPlane>();
        foreach (var it in rep.Items.Where(i => i.Status == "new"))
        {
            using var sub = new SubTransaction(doc);
            sub.Start();
            try
            {
                var sym = res.Symbol;
                if (cfg.CrossMemberLength!.Scope == "type" || (cfg.RodMode == "fixed" && cfg.RodParam!.Scope == "type"))
                    sym = ToolType(doc, res.Symbol, cfg, it, typeCache);
                if (!sym.IsActive) { sym.Activate(); doc.Regenerate(); }
                var ins = Ft(it.InsertionPointMm);
                var fi = PlaceInstance(doc, sym, cfg.PlacementMode, ins, it.RotationDeg, planes, levels);
                if (cfg.CrossMemberLength.Scope == "instance") SetLen(fi, cfg.CrossMemberLength.Name, CrossParamMm(cfg, it.CrossMemberLengthMm), "橫擔長度");
                if (cfg.RodMode == "fixed" && cfg.RodParam!.Scope == "instance") SetLen(fi, cfg.RodParam.Name, it.RodLengthMm!.Value, "吊桿長度");
                doc.Regenerate();
                // 讀回驗證：插入點與旋轉
                if (fi.Location is not LocationPoint lp) throw new InvalidOperationException("放置後沒有 LocationPoint，無法驗證");
                var back = Mm(lp.Point);
                var dmm = Math.Sqrt(Math.Pow(back[0] - it.InsertionPointMm[0], 2) + Math.Pow(back[1] - it.InsertionPointMm[1], 2) + Math.Pow(back[2] - it.InsertionPointMm[2], 2));
                if (dmm > ReadbackTolMm)
                    throw new InvalidOperationException($"讀回插入點 ({back[0]:F1},{back[1]:F1},{back[2]:F1}) 與目標差 {dmm:F1} mm（族原點/放置方式可能與設定不符）");
                var rotBack = Supports.Norm360(lp.Rotation * 180.0 / Math.PI);
                var dr = Math.Abs(rotBack - it.RotationDeg); dr = Math.Min(dr, 360 - dr);
                if (dr > ReadbackTolDeg) throw new InvalidOperationException($"讀回旋轉 {rotBack:F2}° 與目標 {it.RotationDeg:F2}° 不符");
                var e = new Entity(schema);
                e.Set("Source", ToolSource);
                e.Set("TrayUniqueId", it.TrayUniqueId);
                e.Set("StationMm", F(it.StationMm));
                e.Set("DirectionDeg", F(it.TrayDirectionDeg));
                e.Set("BatchId", rep.BatchId);
                e.Set("SupportId", it.SupportId);
                e.Set("ToolVersion", "tray-hangers-v1");
                fi.SetEntity(e);
                var st = sub.Commit();
                if (st != TransactionStatus.Committed) throw new InvalidOperationException($"SubTransaction 狀態 {st}");
                it.Status = "created";
                it.ElementId = fi.Id.Value.ToString(CultureInfo.InvariantCulture);
                it.TypeName = sym.Name;
            }
            catch (Exception ex) when (ex is not OperationCanceledException)
            {
                if (sub.GetStatus() == TransactionStatus.Started) sub.RollBack();
                typeCache.Clear();   // 回滾可能撤銷了本支剛建立的工具類型；之後重新查找
                it.Status = "failed";
                it.Detail = $"{ex.GetType().Name}: {ex.Message}（僅此支回滾）";
            }
        }
        var created = rep.Count("created");
        var failed = rep.Count("failed");
        rep.Outcome = failed == 0 ? "完成" : created > 0 ? "部分完成" : "失敗";
    }

    /// <summary>目標橫擔總長 → 參數值（依校正量測的線性關係：範圍 = ratio × 參數 + offset）。</summary>
    public static double CrossParamMm(HangerConfig cfg, double lengthMm) => (lengthMm - cfg.CrossOffsetMm) / cfg.CrossRatio;

    /// <summary>放置單一實例並旋轉（Execute 與「校正目標吊架」共用）。</summary>
    public static FamilyInstance PlaceInstance(Document doc, FamilySymbol sym, string mode, XYZ ins, double rotationDeg,
                                               Dictionary<long, SketchPlane> planes, List<Level>? levels = null)
    {
        levels ??= new FilteredElementCollector(doc).OfClass(typeof(Level)).Cast<Level>().OrderBy(l => l.Elevation).ToList();
        if (levels.Count == 0) throw new InvalidOperationException("文件中沒有任何 Level");
        if (!sym.IsActive) { sym.Activate(); doc.Regenerate(); }
        FamilyInstance fi;
        if (mode == "level")
        {
            var lvl = levels.LastOrDefault(l => l.Elevation <= ins.Z + 1e-9) ?? levels[0];
            fi = doc.Create.NewFamilyInstance(ins, sym, lvl, StructuralType.NonStructural);
        }
        else
        {
            var zKey = (long)Math.Round(ins.Z * 304800);
            if (!planes.TryGetValue(zKey, out var sp))
                planes[zKey] = sp = SketchPlane.Create(doc, Plane.CreateByNormalAndOrigin(XYZ.BasisZ, new XYZ(0, 0, ins.Z)));
            fi = doc.Create.NewFamilyInstance(sp.GetPlaneReference(), ins, XYZ.BasisX, sym);
        }
        doc.Regenerate();
        var angle = rotationDeg * Math.PI / 180.0;
        if (Math.Abs(angle) > 1e-12)
            ElementTransformUtils.RotateElement(doc, fi.Id, Line.CreateBound(ins, ins + XYZ.BasisZ), angle);
        return fi;
    }

    static void SetLen(Element e, string name, double mm, string what)
    {
        var p = e.LookupParameter(name) ?? throw new InvalidOperationException($"找不到{what}參數「{name}」");
        if (p.IsReadOnly) throw new InvalidOperationException($"{what}參數「{name}」唯讀");
        if (p.StorageType != StorageType.Double) throw new InvalidOperationException($"{what}參數「{name}」不是數值");
        if (!p.Set(Units.MmToFeet(mm))) throw new InvalidOperationException($"{what}參數「{name}」設定失敗");
    }

    /// <summary>類型參數需依橋架寬度變化時：建立或重用工具專用類型（名稱含 (MEPTRAY …)），絕不修改原類型。</summary>
    static FamilySymbol ToolType(Document doc, FamilySymbol orig, HangerConfig cfg, HangerItem it, Dictionary<string, FamilySymbol> cache)
    {
        var parts = new List<string>();
        if (cfg.CrossMemberLength!.Scope == "type") parts.Add("L" + it.CrossMemberLengthMm.ToString("0.###", CultureInfo.InvariantCulture));
        if (cfg.RodMode == "fixed" && cfg.RodParam!.Scope == "type") parts.Add("R" + it.RodLengthMm!.Value.ToString("0.###", CultureInfo.InvariantCulture));
        var name = $"{orig.Name} (MEPTRAY {string.Join(" ", parts)})";
        if (cache.TryGetValue(name, out var c)) return c;
        var sym = new FilteredElementCollector(doc).OfClass(typeof(FamilySymbol)).Cast<FamilySymbol>()
                      .FirstOrDefault(s => s.FamilyName == orig.FamilyName && s.Name == name)
                  ?? (FamilySymbol)orig.Duplicate(name);
        if (cfg.CrossMemberLength.Scope == "type") SetLen(sym, cfg.CrossMemberLength.Name, CrossParamMm(cfg, it.CrossMemberLengthMm), "橫擔長度");
        if (cfg.RodMode == "fixed" && cfg.RodParam!.Scope == "type") SetLen(sym, cfg.RodParam.Name, it.RodLengthMm!.Value, "吊桿長度");
        cache[name] = sym;
        return sym;
    }

    /// <summary>只記錄 Revit 警告（不刪除、不吞掉；錯誤照常交由 Revit 處理）。</summary>
    public sealed class WarningRecorder : IFailuresPreprocessor
    {
        readonly List<string> _sink;
        public WarningRecorder(List<string> sink) => _sink = sink;
        public FailureProcessingResult PreprocessFailures(FailuresAccessor a)
        {
            foreach (var f in a.GetFailureMessages())
                _sink.Add($"[{f.GetSeverity()}] {f.GetDescriptionText()}");
            return FailureProcessingResult.Continue;
        }
    }

    public static Transaction StartTx(Document doc, string name, List<string> warnings)
    {
        var tx = new Transaction(doc, name);
        var o = tx.GetFailureHandlingOptions();
        o.SetFailuresPreprocessor(new WarningRecorder(warnings));
        tx.SetFailureHandlingOptions(o);
        tx.Start();
        return tx;
    }

    public static HangerConfig LoadConfig(string path) =>
        HangerConfigReader.Read(System.Text.Json.Nodes.JsonNode.Parse(File.ReadAllText(path)), placement: true);
}
