using System.Globalization;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json.Nodes;
using Autodesk.Revit.DB;
using MepTray.Core;
using Units = MepTray.Core.Units;

namespace MepTray.Revit;

/// <summary>
/// 目標吊架族的身分（檔案 path/size/mtime/sha256、專案中已載入族的內容簽章）與校正用幾何量測。
/// 量測只在呼叫端開啟、最後會 RollBack 的 Transaction/SubTransaction 內改參數；絕不修改或儲存原族檔。
/// </summary>
public static class HangerCalibration
{
    public const string DefaultTargetRfa = @"F:\學習revit&MEP_PLAN\Revit課程資料\鑫亞電通\Autodesk 機電元件\04_台灣專用元件族\13_固定工具\APP風管及電纜線架\吊架(角鐵).rfa";
    public const string DefaultTargetType = "矩形";
    public const string CalTypeSuffix = " [MEPTRAY CAL]";

    static string Hex(byte[] b) => Convert.ToHexString(b).ToLowerInvariant();

    public static JsonObject FileIdentity(string path)
    {
        var fi = new FileInfo(path);
        byte[] h;
        using (var fs = new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.ReadWrite)) h = SHA256.HashData(fs);
        return new JsonObject
        {
            ["path"] = fi.FullName, ["size"] = fi.Length,
            ["mtime_utc"] = fi.LastWriteTimeUtc.ToString("o", CultureInfo.InvariantCulture), ["sha256"] = Hex(h),
        };
    }

    public static string ParamSetSha(IEnumerable<ParamDef> defs) =>
        Hex(SHA256.HashData(Encoding.UTF8.GetBytes(string.Join("\n", defs
            .Select(d => $"{d.Name}|{d.Scope}|{d.DataType}|{d.Formula}|{d.IsReadOnly}|{d.IsDeterminedByFormula}|{d.IsReporting}")
            .OrderBy(s => s, StringComparer.Ordinal)))));

    public static FamilySymbol? FindSymbol(Document doc, string family, string type) =>
        new FilteredElementCollector(doc).OfClass(typeof(FamilySymbol)).Cast<FamilySymbol>()
            .FirstOrDefault(s => s.FamilyName == family && s.Name == type);

    /// <summary>專案中已載入的目標族／類型內容簽章（放置方式＋目標類型全部數值參數）。族重新載入或類型被改即不同。</summary>
    public static string? ProjectSignature(Document doc, string family, string type)
    {
        var sym = FindSymbol(doc, family, type);
        if (sym == null) return null;
        var lines = new List<string> { $"placement={sym.Family.FamilyPlacementType}" };
        foreach (Parameter p in sym.Parameters)
        {
            if (p?.Definition == null) continue;
            if (p.StorageType == StorageType.Double) lines.Add($"{p.Definition.Name}={Math.Round(p.AsDouble(), 9).ToString("R", CultureInfo.InvariantCulture)}");
            else if (p.StorageType == StorageType.Integer) lines.Add($"{p.Definition.Name}={p.AsInteger()}");
        }
        return Hex(SHA256.HashData(Encoding.UTF8.GetBytes(string.Join("\n", lines.OrderBy(s => s, StringComparer.Ordinal)))));
    }

    /// <summary>放置前強制檢查：族檔與專案中的族必須與校正時相同，否則要求重新校正。</summary>
    public static List<string> CheckCalibration(Document doc, HangerConfig cfg)
    {
        var e = new List<string>();
        const string redo = "請重新執行「讀取目標吊架族」與「校正目標吊架」";
        if (!File.Exists(cfg.CalibFilePath)) { e.Add($"校正記錄的族檔不存在：{cfg.CalibFilePath}；{redo}"); return e; }
        try
        {
            var id = FileIdentity(cfg.CalibFilePath);
            if (id["size"]!.GetValue<long>() != (long)cfg.CalibFileSize || id["sha256"]!.GetValue<string>() != cfg.CalibSha256)
                e.Add($"族檔內容已變更（size/sha256 與校正時不同）；{redo}");
        }
        catch (Exception ex) when (ex is IOException or UnauthorizedAccessException) { e.Add($"無法讀取族檔以核對校正：{ex.Message}"); }
        var sig = ProjectSignature(doc, cfg.FamilyName, cfg.TypeName);
        if (sig == null) e.Add($"專案中沒有 {cfg.FamilyName} / {cfg.TypeName}（請載入族）");
        else if (sig != cfg.CalibProjectSignature) e.Add($"專案中的族或類型「{cfg.TypeName}」內容與校正時不同（族被重新載入或類型參數被改）；{redo}");
        return e;
    }

    // ── 幾何量測（實例局部座標，mm；原點＝族插入點）──
    public sealed class Extents
    {
        public double[] Min = { double.MaxValue, double.MaxValue, double.MaxValue };
        public double[] Max = { double.MinValue, double.MinValue, double.MinValue };
        public SortedSet<double> UpFacesZ = new();
        public int Points;
        public double Size(int axis) => Max[axis] - Min[axis];
    }

    public static Extents? Measure(FamilyInstance fi)
    {
        var inv = fi.GetTransform().Inverse;
        var ex = new Extents();
        var opt = new Options { DetailLevel = ViewDetailLevel.Fine, ComputeReferences = false, IncludeNonVisibleObjects = false };
        void Add(XYZ world)
        {
            var p = inv.OfPoint(world);
            var mm = new[] { Units.FeetToMm(p.X), Units.FeetToMm(p.Y), Units.FeetToMm(p.Z) };
            for (int i = 0; i < 3; i++) { ex.Min[i] = Math.Min(ex.Min[i], mm[i]); ex.Max[i] = Math.Max(ex.Max[i], mm[i]); }
            ex.Points++;
        }
        void Walk(GeometryElement? ge)
        {
            if (ge == null) return;
            foreach (var o in ge)
            {
                switch (o)
                {
                    case GeometryInstance gi: Walk(gi.GetInstanceGeometry()); break;
                    case Solid s when s.Volume > 1e-9:
                        foreach (Edge ed in s.Edges) foreach (var p in ed.Tessellate()) Add(p);
                        foreach (Face f in s.Faces)
                            if (f is PlanarFace pf && inv.OfVector(pf.FaceNormal).Z > 0.999)
                                ex.UpFacesZ.Add(Math.Round(Units.FeetToMm(inv.OfPoint(pf.Origin).Z), 1));
                        break;
                    case Mesh m: for (int i = 0; i < m.Vertices.Count; i++) Add(m.Vertices[i]); break;
                }
            }
        }
        Walk(fi.get_Geometry(opt));
        return ex.Points == 0 ? null : ex;
    }

    public sealed record Effect(string Param, string Scope, double DeltaMm, double[]? DSize, double[]? DMin, double[]? DMax, string? Error)
    {
        public string Text() => Error != null ? $"Δ{Param}（{Scope}）+{DeltaMm:0} mm → 失敗：{Error}"
            : $"Δ{Param}（{Scope}）+{DeltaMm:0} mm → 局部 X 範圍 {DSize![0]:+0.0;-0.0;0.0}、Y {DSize[1]:+0.0;-0.0;0.0}、Z {DSize[2]:+0.0;-0.0;0.0} mm" +
              $"（Zmin {DMin![2]:+0.0;-0.0;0.0}、Zmax {DMax![2]:+0.0;-0.0;0.0}）";
    }

    /// <summary>在呼叫端已開啟的 Transaction 內逐一 SubTransaction：參數 +Δ → Regenerate → 量測 → RollBack。</summary>
    public static List<Effect> MeasureEffects(Document doc, FamilyInstance fi, IEnumerable<(string Name, string Scope)> candidates, double deltaMm)
    {
        var res = new List<Effect>();
        var baseEx = Measure(fi) ?? throw new InvalidOperationException("測試吊架沒有可量測的實體幾何");
        foreach (var (name, scope) in candidates)
        {
            using var sub = new SubTransaction(doc);
            sub.Start();
            try
            {
                Element owner = scope == "type" ? doc.GetElement(fi.GetTypeId()) : fi;
                var p = owner.LookupParameter(name);
                if (p == null) { res.Add(new Effect(name, scope, deltaMm, null, null, null, "找不到參數")); continue; }
                if (p.IsReadOnly) { res.Add(new Effect(name, scope, deltaMm, null, null, null, "唯讀（可能由公式驅動）")); continue; }
                if (p.StorageType != StorageType.Double) { res.Add(new Effect(name, scope, deltaMm, null, null, null, "不是數值參數")); continue; }
                if (!p.Set(p.AsDouble() + Units.MmToFeet(deltaMm))) { res.Add(new Effect(name, scope, deltaMm, null, null, null, "Set 回傳 false")); continue; }
                doc.Regenerate();
                var ex = Measure(fi);
                if (ex == null) { res.Add(new Effect(name, scope, deltaMm, null, null, null, "改值後沒有幾何")); continue; }
                res.Add(new Effect(name, scope, deltaMm,
                    new[] { ex.Size(0) - baseEx.Size(0), ex.Size(1) - baseEx.Size(1), ex.Size(2) - baseEx.Size(2) },
                    new[] { ex.Min[0] - baseEx.Min[0], ex.Min[1] - baseEx.Min[1], ex.Min[2] - baseEx.Min[2] },
                    new[] { ex.Max[0] - baseEx.Max[0], ex.Max[1] - baseEx.Max[1], ex.Max[2] - baseEx.Max[2] }, null));
            }
            catch (Exception ex) { res.Add(new Effect(name, scope, deltaMm, null, null, null, $"{ex.GetType().Name}: {ex.Message}")); }
            finally { if (sub.GetStatus() == TransactionStatus.Started) sub.RollBack(); }
        }
        return res;
    }
}
