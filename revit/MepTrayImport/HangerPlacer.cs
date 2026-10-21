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
    /// <summary>彎頭／三通中點吊架（fitting_support）才有值：接頭的 ElementId；此時 TrayId／TrayUniqueId 指接頭，StationMm 為弧長（彎頭）或 0（三通）。</summary>
    public string? FittingId { get; set; }
    /// <summary>被支撐物件（橋架或接頭）的高度（mm），吊桿長度至結構用。</summary>
    public double TrayHeightMm { get; set; }
    public double StationMm { get; set; }
    public double[] TrayPointMm { get; set; } = new double[3];       // 橋架中心線參考點（Revit 內部座標，mm）
    public double[] BearingPointMm { get; set; } = new double[3];    // 承托面點
    public double[] InsertionPointMm { get; set; } = new double[3];  // 族插入點（依設定換算）
    public double RotationDeg { get; set; }
    public double TrayDirectionDeg { get; set; }
    public double CrossMemberLengthMm { get; set; }
    public double? RodLengthMm { get; set; }
    /// <summary>rod_length.mode=to_structure：命中的結構元件（連結模型時為連結內元素 Id，HitLinkInstanceId 為連結實例 Id）、類別、結構面 Z（mm）。</summary>
    public string? HitElementId { get; set; }
    public string? HitLinkInstanceId { get; set; }
    public string? HitCategory { get; set; }
    public double? HitZMm { get; set; }
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
    public string RodMode { get; set; } = "";      // none | fixed | to_structure
    public string Coordinates { get; set; } = "Revit 內部座標（mm）；TrayPoint=橋架中心線，Bearing=承托面，Insertion=族插入點";
    /// <summary>完成 | 部分完成 | 失敗 | 已取消 | 預覽</summary>
    public string Outcome { get; set; } = "預覽";
    public List<string> PreflightErrors { get; } = new();
    public List<SupportIssue> LayoutIssues { get; } = new();
    public List<HangerItem> Items { get; } = new();
    public List<string> RevitWarnings { get; } = new();
    public List<string> Notes { get; } = new();
    /// <summary>驗收用測試開關 test_fail_nth（null = 關閉）；啟用時預覽與結果最上方顯示警告。</summary>
    public long? TestFailNth { get; set; }
    /// <summary>instance_params：本批新建吊架寫入的實例文字參數（名稱、值；空 = 未設定）。</summary>
    public List<InstanceParamValue> InstanceParams { get; } = new();

    public int Count(string status) => Items.Count(i => i.Status == status);

    public string Summary()
    {
        var s = (TestFailNth is long tf ? TestSwitch.PreviewNote(tf) + "\n" : "") + $"批次 {BatchId}（{Outcome}）\n計畫吊架 {Items.Count}：新增 {Count("new") + Count("created")}、已存在 {Count("exists")}、待確認 {Count("pending") + LayoutIssues.Count(i => i.Level == "pending")}、失敗 {Count("failed")}";
        if (Count("created") > 0) s += $"\n已建立 {Count("created")}";
        if (PreflightErrors.Count > 0) s += "\n預檢失敗（未建立任何吊架）：\n  " + string.Join("\n  ", PreflightErrors);
        var bad = LayoutIssues.Where(i => i.Level is "failed" or "unsupported" or "warning" or "pending").ToList();
        if (bad.Count > 0)
            s += "\n佈置問題：\n  " + string.Join("\n  ", bad.Take(30).Select(i => $"[{i.Level}] {i.Code} {i.PieceId} @({i.LocationMm[0]:F0},{i.LocationMm[1]:F0},{i.LocationMm[2]:F0}) {i.Detail}"))
                 + (bad.Count > 30 ? $"\n  …其餘 {bad.Count - 30} 筆見報告檔" : "");
        var fb = LayoutIssues.Where(i => i.Code == "fitting_mid_fallback").ToList();
        if (fb.Count > 0)
            s += $"\n彎頭／三通中點退回只放兩側：{fb.Count} 個（接頭 ElementId {string.Join("、", fb.Take(30).Select(i => i.PieceId))}" + (fb.Count > 30 ? $"…其餘 {fb.Count - 30} 個見報告檔" : "") + "）";
        if (RodMode == "to_structure")
        {
            var rods =Items.Where(i => (i.Status is "new" or "created") && i.RodLengthMm is not null).ToList();
            if (rods.Count > 0)
                s += "\n吊桿長度（至結構）：\n  " + string.Join("\n  ", rods.Take(30).Select(i =>
                         $"{i.SupportId} 橋架 {i.TrayId} station {i.StationMm:F0}：{i.RodLengthMm:F1} mm，結構面 Z={i.HitZMm:F1}，{i.HitCategory} {i.HitElementId}" + (i.HitLinkInstanceId is null ? "" : $"（連結實例 {i.HitLinkInstanceId}）")))
                     + (rods.Count > 30 ? $"\n  …其餘 {rods.Count - 30} 筆見報告檔" : "");
        }
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
    public const string TrayToolVersion = "tray-hangers-v1";
    static readonly Guid SchemaGuid = new("4b6f2a1e-8d3c-4f57-9a0b-6e2d1c7f3a95");
    const double ReadbackTolMm = 0.5;
    const double ReadbackTolDeg = 0.05;
    const double LevelZTolFt = 1e-6;   // 約 0.0003 mm；只用來判斷是否需要校正，校正後仍以 ReadbackTolMm 讀回驗證

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

    // ── 1b. 彎頭／三通接頭（fitting_support）：只讀 connector 與幾何，交給 FittingGeometry 重建並交叉驗證 ──
    /// <summary>族的 FAMILY_CONTENT_PART_TYPE 轉成 PartType 列舉名稱（電纜架配件的 MEPModel 不是 MechanicalFitting，PartType 屬性讀不到）。</summary>
    public static string? FamilyPartTypeName(FamilyInstance fi)
    {
        var p = fi.Symbol?.Family?.get_Parameter(BuiltInParameter.FAMILY_CONTENT_PART_TYPE);
        if (p is null || p.StorageType != StorageType.Integer) return null;
        var v = p.AsInteger();
        return Enum.IsDefined(typeof(PartType), v) ? ((PartType)v).ToString() : null;
    }

    static List<FittingConnector> FittingConnectors(FamilyInstance fi)
    {
        var l = new List<FittingConnector>();
        var cm = fi.MEPModel?.ConnectorManager ?? throw new InvalidOperationException("接頭沒有 ConnectorManager");
        foreach (Connector c in cm.Connectors)
        {
            if (c.ConnectorType != ConnectorType.End) continue;
            var z = c.CoordinateSystem.BasisZ;
            l.Add(new FittingConnector(Mm(c.Origin), new[] { z.X, z.Y, z.Z }, Units.FeetToMm(c.Width), Units.FeetToMm(c.Height)));
        }
        return l;
    }

    /// <summary>接頭幾何中的圓弧邊（Solid 的邊；含 GeometryInstance 內）。彎頭中心線半徑由內外緣半徑推得（探查實測：中心線不是獨立曲線）。</summary>
    static List<FittingArc> FittingArcs(FamilyInstance fi)
    {
        var l = new List<FittingArc>();
        var opt = new Options { ComputeReferences = false, DetailLevel = ViewDetailLevel.Fine, IncludeNonVisibleObjects = true };
        var ge = fi.get_Geometry(opt) ?? throw new InvalidOperationException("Element.Geometry 回傳 null");
        void Walk(GeometryElement g)
        {
            foreach (GeometryObject go in g)
            {
                if (go is GeometryInstance gi) Walk(gi.GetInstanceGeometry());
                else if (go is Solid s && s.Faces.Size > 0)
                    foreach (Edge e in s.Edges)
                    {
                        Curve c;
                        try { c = e.AsCurve(); } catch (Exception) { continue; }
                        if (c is Arc a && a.IsBound)
                        {
                            var n = a.Normal;
                            l.Add(new FittingArc(Mm(a.Center), Units.FeetToMm(a.Radius), a.Length / a.Radius * 180.0 / Math.PI, new[] { n.X, n.Y, n.Z }));
                        }
                    }
            }
        }
        Walk(ge);
        return l;
    }

    /// <summary>fitting_support 啟用時，讀取選取橋架相鄰的彎頭／三通。接頭所有相鄰橋架都在選取內才處理（避免替沒選的那一段橋架補接頭吊架）；
    /// 讀不出或驗證不過者以 FallbackReason 回傳，由 Layout 標為 fitting_mid_fallback。不開交易、不改模型。</summary>
    public static (List<SupportFitting> Fittings, Dictionary<string, FamilyInstance> Instances, List<string> Notes) ReadFittings(Document doc, IEnumerable<ElementId> trayIds, HangerConfig cfg)
    {
        var fittings = new List<SupportFitting>();
        var instances = new Dictionary<string, FamilyInstance>();
        var notes = new List<string>();
        if (!cfg.FittingSupportEnabled) return (fittings, instances, notes);
        var sel = trayIds.Select(doc.GetElement).OfType<CableTray>().ToList();
        var selIds = new HashSet<long>(sel.Select(t => t.Id.Value));
        var found = new Dictionary<long, FamilyInstance>();
        foreach (var t in sel)
            foreach (Connector c in t.ConnectorManager.Connectors)
            {
                if (c.ConnectorType != ConnectorType.End) continue;
                foreach (Connector r in c.AllRefs)
                    if (r.Owner is FamilyInstance fi && r.ConnectorType != ConnectorType.Logical) found[fi.Id.Value] = fi;
            }
        int skippedKind = 0, skippedUnselected = 0;
        foreach (var fi in found.Values.OrderBy(f => f.Id.Value))
        {
            var kind = FittingGeometry.KindOf(FamilyPartTypeName(fi));
            if (kind is null) { skippedKind++; continue; }
            if (!cfg.FittingSupportKinds.Contains(kind)) continue;
            if (OtherTrays(fi, ElementId.InvalidElementId).Any(o => !selIds.Contains(o.Value))) { skippedUnselected++; continue; }
            var id = fi.Id.Value.ToString(CultureInfo.InvariantCulture);
            SupportFitting f;
            try
            {
                var conns = FittingConnectors(fi);
                f = kind == "elbow" ? FittingGeometry.Elbow(id, conns, FittingArcs(fi)) : FittingGeometry.Tee(id, conns);
            }
            catch (Exception ex) when (ex is not OperationCanceledException)
            {
                f = new SupportFitting { Id = id, Kind = kind, LocationMm = (fi.Location as LocationPoint) is { } lp ? Mm(lp.Point) : null, FallbackReason = $"讀取接頭失敗：{ex.GetType().Name}: {ex.Message}" };
            }
            fittings.Add(f);
            instances[id] = fi;
        }
        if (skippedKind > 0) notes.Add($"fitting_support：{skippedKind} 個接頭的種類不在彎頭／三通範圍（只處理 LadderCableTrayElbow、LadderCableTrayTee），未處理");
        if (skippedUnselected > 0) notes.Add($"fitting_support：{skippedUnselected} 個彎頭／三通有相鄰橋架不在選取內，未處理（請一併選取相鄰橋架）");
        return (fittings, instances, notes);
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
    /// <summary>cfg 非 null 時另讀「實際幾何」（插入點、旋轉角、橫擔長度），供 Classify 與預定位置比對；讀不到的欄位為 null（Classify 視為待確認）。
    /// 追蹤資料（StationMm／DirectionDeg）只當追蹤用，不當實際位置。</summary>
    public static List<ExistingHanger> ReadExisting(Document doc, string familyName, HangerConfig? cfg = null)
    {
        var schema = GetSchema();
        var list = new List<ExistingHanger>();
        var seen = new HashSet<long>();
        foreach (var fi in new FilteredElementCollector(doc).WherePasses(new ExtensibleStorageFilter(schema.GUID)).OfType<FamilyInstance>())
        {
            var e = fi.GetEntity(schema);
            if (!e.IsValid() || e.Get<string>("Source") != ToolSource) continue;
            seen.Add(fi.Id.Value);
            (double[]? actPt, double? actRot, double? actLen) = cfg is null ? default : ReadActual(fi, cfg);
            list.Add(new ExistingHanger(fi.Id.Value.ToString(CultureInfo.InvariantCulture), "tool", e.Get<string>("TrayUniqueId"),
                Parse(e.Get<string>("StationMm")), PointOf(fi), Parse(e.Get<string>("DirectionDeg")), actPt, actRot, actLen));
        }
        foreach (var fi in new FilteredElementCollector(doc).OfClass(typeof(FamilyInstance)).Cast<FamilyInstance>())
        {
            if (seen.Contains(fi.Id.Value) || fi.Symbol?.Family?.Name != familyName) continue;
            list.Add(new ExistingHanger(fi.Id.Value.ToString(CultureInfo.InvariantCulture), "other", null, null, PointOf(fi), null));
        }
        return list;
    }

    /// <summary>實際元素：插入點（LocationPoint）、旋轉角（0–360°）、橫擔長度（參數值經校正線性模型換算：範圍 = ratio × 參數 + offset）。</summary>
    static (double[]? Point, double? RotationDeg, double? CrossLengthMm) ReadActual(FamilyInstance fi, HangerConfig cfg)
    {
        double[]? pt = null; double? rot = null; double? len = null;
        if (fi.Location is LocationPoint lp) { pt = Mm(lp.Point); rot = Supports.Norm360(lp.Rotation * 180.0 / Math.PI); }
        var p = cfg.CrossMemberLength is { } cm ? fi.LookupParameter(cm.Name) : null;
        if (p is { StorageType: StorageType.Double }) len = cfg.CrossRatio * Units.FeetToMm(p.AsDouble()) + cfg.CrossOffsetMm;
        return (pt, rot, len);
    }

    static double? Parse(string? s) => double.TryParse(s, NumberStyles.Float, CultureInfo.InvariantCulture, out var v) ? v : null;
    static double[]? PointOf(FamilyInstance fi) => fi.Location is LocationPoint lp ? Mm(lp.Point) : null;

    // ── 3. 預檢（不改模型）──
    public sealed class Resolved
    {
        public FamilySymbol Symbol = null!;
        public FamilyPlacementType PlacementType;
    }

    /// <param name="checkCalibration">false 只限呼叫端在同一次命令、交易開始前已核對過校正（族文件簽章需 EditFamily，不能在交易／交易群組中途做）。</param>
    public static Resolved? Preflight(Document doc, HangerConfig cfg, List<string> errors, bool checkCalibration = true)
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
        if (checkCalibration) errors.AddRange(HangerCalibration.CheckCalibration(doc, cfg));   // 族檔或專案中的族變更 → 必須重新校正
        CheckParam(sym, cfg.CrossMemberLength!, "parameter_map.cross_member_length", errors);
        if (cfg.RodMode == "to_structure")
        {
            if (RodToStructure.PickView(doc) == null) errors.Add("rod_length.mode=to_structure：找不到可用的 3D 視圖（非範本、非透視）；請先建立 3D 視圖（工具不自動建立）");
            if (sym.LookupParameter(cfg.RodParam!.Name) != null) errors.Add($"rod_length.param：設定為 instance，但「{cfg.RodParam.Name}」是類型參數");
        }
        if (cfg.RodMode == "fixed") CheckParam(sym, cfg.RodParam!, "rod_length.param（固定吊桿長度）", errors, unsupportedIfMissing: true);
        // instance_params：族類型上同名者是類型參數 → 預檢失敗。實例參數（含綁定到類別的共用參數）要建立實例後才能確認；
        // 屆時不存在／唯讀／非文字 → 該支 failed 並回滾（與 instance 長度參數相同）
        for (int i = 0; i < cfg.InstanceParams.Count; i++)
            if (sym.LookupParameter(cfg.InstanceParams[i].Name) != null) errors.Add(InstanceParamText.TypeParamError(i, cfg.InstanceParams[i].Name));
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
    public static HangerRunReport Plan(Document doc, IEnumerable<ElementId> trayIds, HangerConfig cfg, string entry, out Dictionary<string, CableTray> trays, out Resolved? resolved, bool checkCalibration = true)
    {
        var rep = new HangerRunReport { BatchId = $"{DateTime.Now:yyyyMMdd-HHmmss}-{Guid.NewGuid():N}"[..24], Entry = entry, RodMode = cfg.RodMode };
        var (pieces, links, tr, problems) = ReadTrays(doc, trayIds);
        trays = tr;
        var trayMap = tr;
        rep.Notes.AddRange(problems);
        resolved = Preflight(doc, cfg, rep.PreflightErrors, checkCalibration);
        if (!checkCalibration) rep.Notes.Add("校正簽章已於本次命令開始、交易前核對（族文件內容簽章不能在交易群組中途讀取）");
        if (pieces.Count == 0) rep.PreflightErrors.Add("沒有可處理的 Cable Tray");
        var (fittings, fittingInstances, fittingNotes) = ReadFittings(doc, trayIds, cfg);
        rep.Notes.AddRange(fittingNotes);
        var lay = Supports.Layout(pieces, links, cfg, fittings);
        rep.LayoutIssues.AddRange(lay.Issues);
        // 被支撐物件的 UniqueId：直線段吊架指橋架，彎頭／三通中點吊架指接頭（去重鍵與追蹤資料都用它）
        string SupportedUniqueId(SupportPoint s) => s.FittingId is string fid ? fittingInstances[fid].UniqueId : trayMap[s.PieceId].UniqueId;
        // 預定幾何 = 套用族校正偏移與方向修正角後的插入點／旋轉角／橫擔長度（與實際放置用同一份 Place 結果）；去重以此與實際元素比對
        var places = lay.Supports.Select(s => Supports.Place(s, cfg)).ToList();
        var plan = lay.Supports.Select((s, i) => new PlannedKey(SupportedUniqueId(s), s.StationMm, s.PointMm, s.DirectionDeg,
            places[i].InsertionPointMm, places[i].RotationDeg, places[i].CrossMemberLengthMm)).ToList();
        var cls = Supports.Classify(plan, ReadExisting(doc, cfg.FamilyName, cfg), cfg.DedupeToleranceMm, cfg.DedupeAngleToleranceDeg);
        for (int i = 0; i < lay.Supports.Count; i++)
        {
            var s = lay.Supports[i];
            var pl = places[i];
            rep.Items.Add(new HangerItem
            {
                SupportId = s.Id, TrayId = s.PieceId, TrayUniqueId = SupportedUniqueId(s), FittingId = s.FittingId, TrayHeightMm = s.TrayHeightMm, StationMm = s.StationMm,
                TrayPointMm = s.PointMm, BearingPointMm = pl.BearingPointMm, InsertionPointMm = pl.InsertionPointMm, RotationDeg = pl.RotationDeg,
                TrayDirectionDeg = s.DirectionDeg, CrossMemberLengthMm = pl.CrossMemberLengthMm, RodLengthMm = pl.RodLengthMm, Reason = s.Reason,
                Status = cls[i].Status,
                Detail = cls[i].Status == "new" ? "" : cls[i].Reason + (cls[i].Match.Count > 0 ? "（元素 " + string.Join(",", cls[i].Match) + "）" : ""),
            });
        }
        if (cfg.RodMode == "to_structure" && rep.PreflightErrors.Count == 0) RodToStructure.Apply(doc, rep, cfg, trays);
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
        ApplyInstanceParams(rep, cfg);
        ApplyTestSwitch(rep, cfg);
        if (rep.PreflightErrors.Count > 0) rep.Outcome = "失敗";
        return rep;
    }

    /// <summary>驗收用測試開關：記到報告（預覽最上方顯示）；N 大於本次新增數時另加附註。未設定時不做任何事。</summary>
    public static void ApplyTestSwitch(HangerRunReport rep, HangerConfig cfg)
    {
        if (cfg.TestFailNth is not long n) return;
        rep.TestFailNth = n;
        var nNew = rep.Count("new");
        if (n > nNew) rep.Notes.Add(TestSwitch.NotTriggeredNote(n, nNew));
    }

    /// <summary>instance_params：記到報告（批次層級）並在預覽附註列出名稱與值。未設定時不做任何事。</summary>
    public static void ApplyInstanceParams(HangerRunReport rep, HangerConfig cfg)
    {
        if (cfg.InstanceParams.Count == 0) return;
        rep.InstanceParams.AddRange(cfg.InstanceParams);
        rep.Notes.Add(InstanceParamText.PreviewNote(cfg.InstanceParams));
    }

    // ── 5. 放置（呼叫端已開 Transaction，並在 TransactionGroup 內）──
    /// <param name="toolVersion">寫入 Extensible Storage 的 ToolVersion（橋架 = tray-hangers-v1；配管 = conduit-hangers-v1）。</param>
    public static void Execute(Document doc, HangerRunReport rep, HangerConfig cfg, Resolved res, string toolVersion = TrayToolVersion)
    {
        var schema = GetSchema();
        var levels = new FilteredElementCollector(doc).OfClass(typeof(Level)).Cast<Level>().OrderBy(l => l.Elevation).ToList();
        if (levels.Count == 0) throw new InvalidOperationException("文件中沒有任何 Level");
        var typeCache = new Dictionary<string, FamilySymbol>();
        var planes = new Dictionary<long, SketchPlane>();
        var zNotes = new List<string>();
        int ordinal = 0;   // 本次第幾支 new（只供測試開關 test_fail_nth；未設定時不影響任何行為）
        foreach (var it in rep.Items.Where(i => i.Status == "new"))
        {
            ordinal++;
            using var sub = new SubTransaction(doc);
            sub.Start();
            try
            {
                var sym = res.Symbol;
                if (cfg.CrossMemberLength!.Scope == "type" || (cfg.RodMode == "fixed" && cfg.RodParam!.Scope == "type"))
                    sym = ToolType(doc, res.Symbol, cfg, it, typeCache);
                if (!sym.IsActive) { sym.Activate(); doc.Regenerate(); }
                var ins = Ft(it.InsertionPointMm);
                var fi = PlaceInstance(doc, sym, cfg.PlacementMode, ins, it.RotationDeg, planes, levels, zNotes);
                if (cfg.CrossMemberLength.Scope == "instance") SetLen(fi, cfg.CrossMemberLength.Name, CrossParamMm(cfg, it.CrossMemberLengthMm), "橫擔長度");
                if (cfg.RodMode == "to_structure" && it.RodLengthMm is null) throw new InvalidOperationException("to_structure 吊桿長度尚未算出，不建立此支");
                if (cfg.RodMode != "none" && cfg.RodParam!.Scope == "instance") SetLen(fi, cfg.RodParam.Name, it.RodLengthMm!.Value, "吊桿長度");
                foreach (var ip in cfg.InstanceParams) SetText(fi, ip);   // instance_params（只寫本支新建實例）
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
                if (cfg.RodMode == "to_structure")
                {
                    var rp = fi.LookupParameter(cfg.RodParam!.Name) ?? throw new InvalidOperationException($"讀回時找不到吊桿長度參數「{cfg.RodParam.Name}」");
                    var rodBack = Units.FeetToMm(rp.AsDouble());
                    if (Math.Abs(rodBack - it.RodLengthMm!.Value) > ReadbackTolMm)
                        throw new InvalidOperationException($"讀回吊桿長度 {rodBack:F1} mm 與目標 {it.RodLengthMm.Value:F1} mm 不符（族參數可能被公式或約束覆蓋）");
                }
                foreach (var ip in cfg.InstanceParams)   // 讀回驗證：instance_params 逐字相同
                    if (InstanceParamText.ReadbackError(ip.Name, ip.Value, fi.LookupParameter(ip.Name)?.AsString()) is string ipErr) throw new InvalidOperationException(ipErr);
                var e = new Entity(schema);
                e.Set("Source", ToolSource);
                e.Set("TrayUniqueId", it.TrayUniqueId);
                e.Set("StationMm", F(it.StationMm));
                e.Set("DirectionDeg", F(it.TrayDirectionDeg));
                e.Set("BatchId", rep.BatchId);
                e.Set("SupportId", it.SupportId);
                e.Set("ToolVersion", toolVersion);
                fi.SetEntity(e);
                // 驗收用測試開關：實例已建立、參數已設、讀回已驗、追蹤資料已寫入 → 故意拋例外，驗證「只回滾此支」
                if (TestSwitch.ShouldFail(cfg.TestFailNth, ordinal)) throw new InvalidOperationException(TestSwitch.FailMessage(cfg.TestFailNth!.Value));
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
        foreach (var g in zNotes.GroupBy(x => x)) rep.Notes.Add(g.Count() > 1 ? $"{g.Key}（{g.Count()} 支）" : g.Key);
        var created = rep.Count("created");
        var failed = rep.Count("failed");
        rep.Outcome = failed == 0 ? "完成" : created > 0 ? "部分完成" : "失敗";
    }

    /// <summary>目標橫擔總長 → 參數值（依校正量測的線性關係：範圍 = ratio × 參數 + offset）。</summary>
    public static double CrossParamMm(HangerConfig cfg, double lengthMm) => (lengthMm - cfg.CrossOffsetMm) / cfg.CrossRatio;

    /// <summary>放置單一實例並旋轉（Execute 與「校正目標吊架」共用）。
    /// level 模式：NewFamilyInstance(XYZ, symbol, Level, …) 建立後的實際高程可能與目標 Z 相差所選 Level 的標高
    /// （實測 2026-10-18：Level 標高 −3600 mm 時落點恰低 3600 mm；標高 0 的 Level 不會出現）。因此建立後讀回 LocationPoint，
    /// 若 Z 與目標不同即垂直平移校正（只移動本工具剛建立的實例）；校正量記入 zNotes。標高 0 時不移動，行為與先前相同。</summary>
    public static FamilyInstance PlaceInstance(Document doc, FamilySymbol sym, string mode, XYZ ins, double rotationDeg,
                                               Dictionary<long, SketchPlane> planes, List<Level>? levels = null, List<string>? zNotes = null)
    {
        levels ??= new FilteredElementCollector(doc).OfClass(typeof(Level)).Cast<Level>().OrderBy(l => l.Elevation).ToList();
        if (levels.Count == 0) throw new InvalidOperationException("文件中沒有任何 Level");
        if (!sym.IsActive) { sym.Activate(); doc.Regenerate(); }
        FamilyInstance fi;
        if (mode == "level")
        {
            var lvl = levels.LastOrDefault(l => l.Elevation <= ins.Z + 1e-9) ?? levels[0];
            fi = doc.Create.NewFamilyInstance(ins, sym, lvl, StructuralType.NonStructural);
            doc.Regenerate();
            if (fi.Location is LocationPoint lp0)
            {
                var dz = ins.Z - lp0.Point.Z;
                if (Math.Abs(dz) > LevelZTolFt)
                {
                    ElementTransformUtils.MoveElement(doc, fi.Id, new XYZ(0, 0, dz));
                    zNotes?.Add($"level 放置落點 Z 與目標差 {Units.FeetToMm(-dz):F1} mm（Level「{lvl.Name}」標高 {Units.FeetToMm(lvl.Elevation):F1} mm），已垂直平移校正");
                }
            }
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

    /// <summary>instance_params：只接受存在、可寫、StorageType.String 的實例參數；否則拋例外（該支回滾）。</summary>
    static void SetText(Element e, InstanceParamValue ip)
    {
        var p = e.LookupParameter(ip.Name);
        if (InstanceParamText.Problem(ip.Name, p != null, p?.IsReadOnly == true, p?.StorageType == StorageType.String, p?.StorageType.ToString() ?? "") is string err)
            throw new InvalidOperationException(err);
        if (!p!.Set(ip.Value)) throw new InvalidOperationException(InstanceParamText.SetFailed(ip.Name));
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
