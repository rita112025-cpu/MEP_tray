using System.Text.Encodings.Web;
using System.Text.Json;
using Autodesk.Revit.DB;
using Autodesk.Revit.DB.Electrical;
using MepTray.Core;
using Units = MepTray.Core.Units;   // 與 Autodesk.Revit.DB.Units 同名，明確指定本專案的換算類別

namespace MepTray.Revit;

public sealed class ImportOptions
{
    /// <summary>僅當模型的 basis 為 UNSPECIFIED 時才會採用；必須是使用者明確的選擇（對話框或測試參數）。</summary>
    public string? BasisOverride { get; set; }
}

public sealed record JointResult(string Id, string Kind, string Status, string Detail);

public sealed class TrayInfo
{
    public string Id { get; set; } = "";
    public double[] StartMm { get; set; } = new double[3];
    public double[] EndMm { get; set; } = new double[3];
    public double WidthMm { get; set; }
    public double HeightMm { get; set; }
    public string Comments { get; set; } = "";
}

public sealed class BasePointInfo
{
    /// <summary>BasePoint.Position（內部座標，mm）。</summary>
    public double[] PositionMm { get; set; } = new double[3];
    /// <summary>BasePoint.SharedPosition（共用座標，mm）。</summary>
    public double[] SharedPositionMm { get; set; } = new double[3];
    public bool Pinned { get; set; }
}

public sealed class ProjectPositionInfo
{
    public string Location { get; set; } = "";
    public double EastWestMm { get; set; }
    public double NorthSouthMm { get; set; }
    public double ElevationMm { get; set; }
    public double AngleRad { get; set; }
}

public sealed class CoordinateInfo
{
    public BasePointInfo? ProjectBasePoint { get; set; }
    public BasePointInfo? SurveyPoint { get; set; }
    /// <summary>ActiveProjectLocation 在內部原點處的 ProjectPosition。</summary>
    public ProjectPositionInfo? ActiveProjectPosition { get; set; }
}

public sealed class Inspection
{
    public List<TrayInfo> Trays { get; } = new();
    public int FittingCount { get; set; }
    public CoordinateInfo? Coordinates { get; set; }
}

public sealed class ImportReport
{
    public bool Committed { get; set; }
    /// <summary>整批中止原因（未進 Transaction，或已整批 Rollback）。null = 未中止。</summary>
    public string? Abort { get; set; }
    public string Phase { get; set; } = "validate";   // validate | resolve | transaction | done
    public string Basis { get; set; } = "";
    public List<string> CreatedTrays { get; } = new();
    public List<JointResult> Joints { get; } = new();
    public List<string> Warnings { get; } = new();
    public Inspection? Inspection { get; set; }

    static readonly JsonSerializerOptions Json = new()
    {
        WriteIndented = true,
        Encoder = JavaScriptEncoder.UnsafeRelaxedJsonEscaping,
    };

    public string ToJson() => JsonSerializer.Serialize(this, Json);

    public string Summary()
    {
        if (Abort != null) return $"匯入中止（階段 {Phase}）：{Abort}";
        var failed = Joints.Where(j => j.Status != "OK").ToList();
        var s = $"已建立 {CreatedTrays.Count} 段橋架；接頭 {Joints.Count - failed.Count}/{Joints.Count} 成功（基準 {Basis}）。";
        if (failed.Count > 0)
            s += "\n接頭失敗（橋架已保留）：\n" + string.Join("\n", failed.Select(j => $"  {j.Id} {j.Kind}: {j.Detail}"));
        if (Warnings.Count > 0) s += "\n警告：\n  " + string.Join("\n  ", Warnings);
        return s;
    }
}

public static class RevitImporter
{
    const double ConnectorTolFeet = 0.002;   // ≈0.6 mm

    public static ImportReport Import(Document? doc, TrayModel m, ImportOptions? opt = null)
    {
        opt ??= new ImportOptions();
        var rep = new ImportReport { Phase = "validate" };
        if (doc is null) { rep.Abort = "沒有可用的 Revit 文件"; return rep; }

        // ── 階段 1：不進 Transaction 的檢查（失敗 = 完全不動文件）──
        var errs = ModelLoader.Validate(m);
        if (errs.Count > 0) { rep.Abort = "模型驗證失敗: " + string.Join("; ", errs); return rep; }

        var basis = m.CoordinateSystem.Basis;
        if (basis == "UNSPECIFIED" && !string.IsNullOrEmpty(opt.BasisOverride)) basis = opt.BasisOverride!;
        var be = ModelLoader.BasisError(basis);
        if (be != null) { rep.Abort = be; return rep; }
        rep.Basis = basis;

        rep.Phase = "resolve";
        XYZ offset;
        switch (basis)
        {
            case "INTERNAL_ORIGIN":
                offset = XYZ.Zero;
                break;
            case "PROJECT_BASE_POINT":
                var pbp = BasePoint.GetProjectBasePoint(doc);
                if (pbp == null) { rep.Abort = "找不到 Project Base Point"; return rep; }
                offset = pbp.Position;
                break;
            default:
                rep.Abort = $"基準 {basis} 尚未實作（目前支援 INTERNAL_ORIGIN、PROJECT_BASE_POINT）";
                return rep;
        }

        XYZ ToXyz(double[] a) =>
            new XYZ(Units.MmToFeet(a[0]), Units.MmToFeet(a[1]), Units.MmToFeet(a[2])) + offset;

        var levels = new FilteredElementCollector(doc).OfClass(typeof(Level)).Cast<Level>().ToList();
        if (levels.Count == 0) { rep.Abort = "文件中沒有任何 Level"; return rep; }

        var types = new FilteredElementCollector(doc).OfClass(typeof(CableTrayType)).Cast<CableTrayType>().ToList();
        var type = types.FirstOrDefault(t => t.Name == m.Tray.TypeName);
        if (type == null)
        {
            rep.Abort = $"找不到 CableTrayType '{m.Tray.TypeName}'；可用: " + string.Join(", ", types.Select(t => $"'{t.Name}'"));
            return rep;
        }

        var coords = new List<(SegmentDto S, XYZ P1, XYZ P2)>();
        foreach (var s in m.Segments) coords.Add((s, ToXyz(s.Start), ToXyz(s.End)));   // 轉換失敗在此就會拋出，尚未進 Transaction
        var jointPts = m.Joints.ToDictionary(j => j.Id, j => ToXyz(j.Point));
        double widthFt = Units.MmToFeet(m.Tray.WidthMm), heightFt = Units.MmToFeet(m.Tray.HeightMm);

        // ── 階段 2：Transaction。CableTray 建立/參數設定任何失敗 → 整批 Rollback ──
        rep.Phase = "transaction";
        using var tx = new Transaction(doc, "MEP Tray import");
        tx.Start();
        try
        {
            var trays = new Dictionary<string, CableTray>();
            foreach (var (s, p1, p2) in coords)
            {
                var lvl = levels.OrderBy(l => Math.Abs(l.Elevation - p1.Z)).First();
                var ct = CableTray.Create(doc, type.Id, p1, p2, lvl.Id);
                SetFeet(ct, BuiltInParameter.RBS_CABLETRAY_WIDTH_PARAM, widthFt, s.Id, "寬度");
                SetFeet(ct, BuiltInParameter.RBS_CABLETRAY_HEIGHT_PARAM, heightFt, s.Id, "高度");
                trays[s.Id] = ct;
                rep.CreatedTrays.Add($"{s.Id}={ct.Id}");
            }
            doc.Regenerate();

            // 接頭：個別失敗不影響橋架本體（用 SubTransaction 隔離），但必須明確列入報告
            foreach (var j in m.Joints) rep.Joints.Add(MakeFitting(doc, j, trays, jointPts[j.Id]));

            // Comments：保留原文，只更新自己的 [MEP-TRAY] 區段
            foreach (var s in m.Segments)
            {
                var p = trays[s.Id].get_Parameter(BuiltInParameter.ALL_MODEL_INSTANCE_COMMENTS);
                if (p == null || p.IsReadOnly) { rep.Warnings.Add($"{s.Id} 的 Comments 參數不可寫"); continue; }
                if (!p.Set(Comments.Merge(p.AsString(), Comments.Body(m, s.Id))))
                    rep.Warnings.Add($"{s.Id} 的 Comments 寫入失敗");
            }

            var st = tx.Commit();
            if (st != TransactionStatus.Committed)
                throw new InvalidOperationException($"Transaction 未能 Commit（狀態 {st}）");
            rep.Committed = true;
            rep.Phase = "done";
        }
        catch (Exception ex)
        {
            if (tx.GetStatus() == TransactionStatus.Started) tx.RollBack();
            rep.Committed = false;
            rep.Abort = $"建立階段失敗，已整批 Rollback：{ex.GetType().Name}: {ex.Message}";
            rep.CreatedTrays.Clear();
            rep.Joints.Clear();
            rep.Warnings.Clear();
        }
        return rep;
    }

    static void SetFeet(Element e, BuiltInParameter bip, double feet, string segId, string what)
    {
        var p = e.get_Parameter(bip);
        if (p == null || p.IsReadOnly || !p.Set(feet))
            throw new InvalidOperationException($"{segId} 無法設定{what}參數");
    }

    static Connector? ConnectorAt(CableTray t, XYZ pt)
    {
        Connector? best = null;
        double bestD = double.MaxValue;
        var manager = t.ConnectorManager;
        if (manager is null) return null;
        foreach (Connector? c in manager.Connectors)
        {
            if (c is null) continue;
            var d = c.Origin.DistanceTo(pt);
            if (d < bestD) { best = c; bestD = d; }
        }
        return bestD <= ConnectorTolFeet ? best : null;
    }

    static JointResult MakeFitting(Document doc, JointDto j, Dictionary<string, CableTray> trays, XYZ pt)
    {
        JointResult Fail(string why) => new(j.Id, j.Kind, "FAIL", why);
        if (j.Kind == "unsupported") return Fail("拓樸無法支援（非共線的三向、或其他組合）");

        // Connector 幾何驗證：每段在接點處必須有一個未連接、位置吻合的 connector
        var conns = new List<Connector>();
        foreach (var sid in j.Segments)
        {
            var c = ConnectorAt(trays[sid], pt);
            if (c == null) return Fail($"{sid} 在接點處找不到 connector（距離超過 {ConnectorTolFeet} ft）");
            if (c.IsConnected) return Fail($"{sid} 的 connector 已被連接");
            conns.Add(c);
        }

        using var sub = new SubTransaction(doc);
        sub.Start();
        try
        {
            switch (j.Kind)
            {
                case "elbow": doc.Create.NewElbowFitting(conns[0], conns[1]); break;
                case "tee": doc.Create.NewTeeFitting(conns[0], conns[1], conns[2]); break;     // 前兩個為主幹，第三個為支線
                case "cross": doc.Create.NewCrossFitting(conns[0], conns[1], conns[2], conns[3]); break;
                case "union": doc.Create.NewUnionFitting(conns[0], conns[1]); break;
                default: sub.RollBack(); return Fail($"未知的接頭種類 {j.Kind}");
            }
            sub.Commit();
            return new JointResult(j.Id, j.Kind, "OK", "");
        }
        catch (Exception ex)
        {
            if (sub.GetStatus() == TransactionStatus.Started) sub.RollBack();
            return Fail($"{ex.GetType().Name}: {ex.Message}");
        }
    }

    /// <summary>讀回文件中的橋架與接頭（供測試驗證與報告使用）。</summary>
    public static Inspection Inspect(Document doc)
    {
        var ins = new Inspection();
        foreach (var ct in new FilteredElementCollector(doc).OfClass(typeof(CableTray)).Cast<CableTray>()
                     .OrderBy(c => c.Id.ToString()))
        {
            if (ct.Location is not LocationCurve location)
                throw new InvalidDataException($"橋架 {ct.Id} 沒有 LocationCurve，無法讀回幾何");
            var line = location.Curve;
            ins.Trays.Add(new TrayInfo
            {
                Id = ct.Id.ToString(),
                StartMm = Mm(line.GetEndPoint(0)),
                EndMm = Mm(line.GetEndPoint(1)),
                WidthMm = Units.FeetToMm(ct.get_Parameter(BuiltInParameter.RBS_CABLETRAY_WIDTH_PARAM).AsDouble()),
                HeightMm = Units.FeetToMm(ct.get_Parameter(BuiltInParameter.RBS_CABLETRAY_HEIGHT_PARAM).AsDouble()),
                Comments = ct.get_Parameter(BuiltInParameter.ALL_MODEL_INSTANCE_COMMENTS)?.AsString() ?? "",
            });
        }
        ins.FittingCount = new FilteredElementCollector(doc)
            .OfCategory(BuiltInCategory.OST_CableTrayFitting).WhereElementIsNotElementType().GetElementCount();
        ins.Coordinates = ReadCoordinates(doc);
        return ins;
    }

    static double[] Mm(XYZ p) => new[] { Units.FeetToMm(p.X), Units.FeetToMm(p.Y), Units.FeetToMm(p.Z) };

    static BasePointInfo? PointInfo(BasePoint? bp) =>
        bp == null ? null : new BasePointInfo { PositionMm = Mm(bp.Position), SharedPositionMm = Mm(bp.SharedPosition), Pinned = bp.Pinned };

    /// <summary>讀回 PBP、Survey Point 與 ActiveProjectLocation 的座標狀態（mm；角度為弧度）。</summary>
    public static CoordinateInfo ReadCoordinates(Document doc)
    {
        var c = new CoordinateInfo
        {
            ProjectBasePoint = PointInfo(BasePoint.GetProjectBasePoint(doc)),
            SurveyPoint = PointInfo(BasePoint.GetSurveyPoint(doc)),
        };
        var loc = doc.ActiveProjectLocation;
        if (loc != null)
        {
            var pp = loc.GetProjectPosition(XYZ.Zero);
            c.ActiveProjectPosition = new ProjectPositionInfo
            {
                Location = loc.Name,
                EastWestMm = Units.FeetToMm(pp.EastWest),
                NorthSouthMm = Units.FeetToMm(pp.NorthSouth),
                ElevationMm = Units.FeetToMm(pp.Elevation),
                AngleRad = pp.Angle,
            };
        }
        return c;
    }
}
