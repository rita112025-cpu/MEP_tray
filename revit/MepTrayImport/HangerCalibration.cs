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
    public const string CalTypeSuffix = " (MEPTRAY CAL)";

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

    /// <summary>簽章格式版本前綴。v1（無前綴）只含目標類型的數值參數，2026-10-18 起改為 v2：整個族的內容。</summary>
    public const string SignaturePrefix = "v2:";
    /// <summary>工具自建類型（名稱含此字串，例如「矩形 (MEPTRAY L400)」「矩形 (MEPTRAY CAL)」）不計入簽章，避免放置後自我失效。</summary>
    public const string ToolTypeMarker = "(MEPTRAY";

    static string D(double v) => Math.Round(v, 9).ToString("R", CultureInfo.InvariantCulture);
    static bool IsToolType(string name) => name.Contains(ToolTypeMarker, StringComparison.Ordinal);

    static string ParamValue(Document doc, Parameter p) => p.StorageType switch
    {
        StorageType.Double => D(p.AsDouble()),
        StorageType.Integer => p.AsInteger().ToString(CultureInfo.InvariantCulture),
        StorageType.String => p.AsString() ?? "",
        StorageType.ElementId => IdName(doc, p.AsElementId()),
        _ => "",
    };

    static string IdName(Document doc, ElementId? id) =>
        id == null || id == ElementId.InvalidElementId ? "-" : doc.GetElement(id) is Element e ? $"{e.GetType().Name}:{e.Name}" : $"id:{id.Value}";

    static string DefKey(Definition d) => $"{d.Name}|{d.GetGroupTypeId()?.TypeId}|{d.GetDataType()?.TypeId}";

    /// <summary>
    /// 專案中已載入的目標族內容簽章（v2）。任何族變更都會讓簽章不同 → 放置前要求重新校正：
    /// 專案端：族放置方式／品類／可否編輯；族內每個（非工具自建）類型的全部參數（名稱、儲存型別、群組、資料型別、值）。
    /// 族文件端（Document.EditFamily 取得獨立複本，只讀不改、不載回，最後 Close(false)）：
    /// 全部族參數定義（名稱、實例/類型、儲存型別、群組、資料型別、公式、報告/共用 GUID）、每個類型的全部參數值（含實例參數預設值），
    /// 以及族內所有元素在「目標類型」下的實體幾何摘要（體積、表面積、面/邊數、外包範圍；只在複本的交易內切換類型並回滾）。
    /// 專案文件有未完成的交易或唯讀時不能呼叫（EditFamily 限制）→ 拋 InvalidOperationException。
    /// </summary>
    public static string? ProjectSignature(Document doc, string family, string type)
    {
        var sym = FindSymbol(doc, family, type);
        if (sym == null) return null;
        var fam = sym.Family;
        var lines = new List<string>
        {
            $"family={fam.Name}", $"placement={fam.FamilyPlacementType}", $"category={fam.FamilyCategory?.Id.Value}",
            $"editable={fam.IsEditable}", $"inplace={fam.IsInPlace}", $"target_type={type}",
        };
        foreach (var id in fam.GetFamilySymbolIds())
        {
            if (doc.GetElement(id) is not FamilySymbol s || IsToolType(s.Name)) continue;
            foreach (Parameter p in s.Parameters)
                if (p?.Definition != null) lines.Add($"ptype:{s.Name}|{DefKey(p.Definition)}|{p.StorageType}={ParamValue(doc, p)}");
        }
        if (fam.IsEditable && !fam.IsInPlace) lines.AddRange(FamilyDocumentLines(doc, fam, type));
        else lines.Add("familydoc=unavailable");
        return SignaturePrefix + Hex(SHA256.HashData(Encoding.UTF8.GetBytes(string.Join("\n", lines.OrderBy(x => x, StringComparer.Ordinal)))));
    }

    static List<string> FamilyDocumentLines(Document doc, Family fam, string type)
    {
        if (doc.IsModifiable || doc.IsReadOnly)
            throw new InvalidOperationException("專案文件有未完成的交易或為唯讀，無法讀取族內容以建立／核對校正簽章");
        var lines = new List<string>();
        var fd = doc.EditFamily(fam);
        try
        {
            var fm = fd.FamilyManager;
            var pars = new List<FamilyParameter>();
            foreach (FamilyParameter fp in fm.Parameters)
            {
                if (fp?.Definition == null) continue;
                pars.Add(fp);
                lines.Add($"fparam:{DefKey(fp.Definition)}|{fp.StorageType}|inst={fp.IsInstance}|rep={fp.IsReporting}|shared={(fp.IsShared ? fp.GUID.ToString() : "-")}|f={fp.Formula}");
            }
            foreach (FamilyType ft in fm.Types)
            {
                if (ft == null || IsToolType(ft.Name)) continue;
                foreach (var fp in pars)
                {
                    if (!ft.HasValue(fp)) { lines.Add($"fvalue:{ft.Name}|{fp.Definition.Name}|{fp.IsInstance}=<none>"); continue; }
                    var v = fp.StorageType switch
                    {
                        StorageType.Double => ft.AsDouble(fp) is double d ? D(d) : "",
                        StorageType.Integer => ft.AsInteger(fp)?.ToString(CultureInfo.InvariantCulture) ?? "",
                        StorageType.String => ft.AsString(fp) ?? "",
                        StorageType.ElementId => IdName(fd, ft.AsElementId(fp)),
                        _ => "",
                    };
                    lines.Add($"fvalue:{ft.Name}|{fp.Definition.Name}|{fp.IsInstance}={v}");
                }
            }
            // 幾何以「目標類型」為目前類型量測（只在這份複本的交易內切換，最後 RollBack；複本不儲存、不載回專案）
            var target = fm.Types.Cast<FamilyType>().FirstOrDefault(t => t?.Name == type);
            lines.Add($"fcurrent={(target == null ? "missing" : type)}");
            using var tx = new Transaction(fd, "MEP Tray 族簽章（將回滾）");
            tx.Start();
            try
            {
                if (target != null) { fm.CurrentType = target; fd.Regenerate(); }
                var opt = new Options { DetailLevel = ViewDetailLevel.Fine, ComputeReferences = false, IncludeNonVisibleObjects = true };
                foreach (var e in new FilteredElementCollector(fd).WhereElementIsNotElementType())
                {
                    GeometryElement? ge;
                    try { ge = e.get_Geometry(opt); } catch (Autodesk.Revit.Exceptions.ApplicationException) { continue; }
                    if (ge == null) continue;
                    var sums = new List<string>();
                    GeometryLines(ge, sums);
                    foreach (var g in sums) lines.Add($"geom:{e.Category?.Name}|{e.GetType().Name}|{g}");
                }
            }
            finally { if (tx.GetStatus() == TransactionStatus.Started) tx.RollBack(); }
        }
        finally { fd.Close(false); }
        return lines;
    }

    static void GeometryLines(GeometryElement ge, List<string> sink)
    {
        foreach (var o in ge)
        {
            switch (o)
            {
                case GeometryInstance gi: GeometryLines(gi.GetInstanceGeometry(), sink); break;
                case Solid s when s.Faces.Size > 0:
                {
                    double[] lo = { double.MaxValue, double.MaxValue, double.MaxValue }, hi = { double.MinValue, double.MinValue, double.MinValue };
                    foreach (Edge ed in s.Edges)
                        foreach (var p in ed.Tessellate())
                        {
                            var c = new[] { p.X, p.Y, p.Z };
                            for (int i = 0; i < 3; i++) { lo[i] = Math.Min(lo[i], c[i]); hi[i] = Math.Max(hi[i], c[i]); }
                        }
                    sink.Add($"solid|v={R6(s.Volume)}|a={R6(s.SurfaceArea)}|f={s.Faces.Size}|e={s.Edges.Size}|lo={string.Join(",", lo.Select(R6))}|hi={string.Join(",", hi.Select(R6))}");
                    break;
                }
                case Mesh m: sink.Add($"mesh|n={m.Vertices.Count}|t={m.NumTriangles}"); break;
                case Curve cv when cv.IsBound: sink.Add($"curve|{cv.GetType().Name}|l={R6(cv.Length)}|p0={P6(cv.GetEndPoint(0))}|p1={P6(cv.GetEndPoint(1))}"); break;
            }
        }
    }

    // 幾何摘要以 1e-6（內部單位 ft / ft² / ft³）四捨五入，避免浮點雜訊；-0 正規化為 0
    static string R6(double v) { var r = Math.Round(v, 6); return (r == 0 ? 0 : r).ToString("R", CultureInfo.InvariantCulture); }
    static string P6(XYZ p) => $"{R6(p.X)},{R6(p.Y)},{R6(p.Z)}";

    public static bool IsCurrentSignatureFormat(string? sig) => sig != null && sig.StartsWith(SignaturePrefix, StringComparison.Ordinal);

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
        if (!IsCurrentSignatureFormat(cfg.CalibProjectSignature))
        {
            e.Add($"校正簽章為舊格式（只含目標類型的數值參數，無法偵測族幾何或參數定義變更）；本版改用完整族內容簽章 {SignaturePrefix}，{redo}");
            return e;
        }
        string? sig;
        try { sig = ProjectSignature(doc, cfg.FamilyName, cfg.TypeName); }
        catch (Exception ex) when (ex is InvalidOperationException or Autodesk.Revit.Exceptions.InvalidOperationException or Autodesk.Revit.Exceptions.ArgumentException)
        { e.Add($"無法讀取專案中的族內容以核對校正：{ex.Message}"); return e; }
        if (sig == null) e.Add($"專案中沒有 {cfg.FamilyName} / {cfg.TypeName}（請載入族）");
        else if (sig != cfg.CalibProjectSignature) e.Add($"專案中的族「{cfg.FamilyName}」內容與校正時不同（族被重新載入／編輯、幾何或參數定義／預設值改變、或類型參數被改）；{redo}");
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
