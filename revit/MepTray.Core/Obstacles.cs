using System.Globalization;
using System.Text;
using System.Text.Encodings.Web;
using System.Text.Json;

namespace MepTray.Core;

/// <summary>
/// 3×4 仿射變換（列優先：x' = M[0..2]·p + M[3]，依此類推）。用來表示「basis 座標（英呎）→ Revit 內部座標（英呎）」
/// 這類需要 Revit 才能取得的變換（INTERNAL_ORIGIN = 恆等、PROJECT_BASE_POINT = 平移、SHARED_COORDINATES =
/// ProjectLocation.GetTotalTransform()），讓幾何核心不依賴 Revit API 即可測試。
/// </summary>
public sealed class Affine
{
    public double[] M { get; }

    public Affine(double[] m)
    {
        if (m == null || m.Length != 12 || m.Any(v => !double.IsFinite(v))) throw new ArgumentException("Affine 需要 12 個有限數字");
        M = (double[])m.Clone();
    }

    public static Affine Identity => new(new double[] { 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0 });

    /// <summary>繞 Z 旋轉 deg（逆時針為正）後平移 t。</summary>
    public static Affine RotZThenTranslate(double deg, double tx, double ty, double tz)
    {
        var th = deg * Math.PI / 180.0; double c = Math.Cos(th), s = Math.Sin(th);
        return new(new[] { c, -s, 0, tx, s, c, 0, ty, 0, 0, 1, tz });
    }

    public double[] Apply(double[] p) => new[]
    {
        M[0] * p[0] + M[1] * p[1] + M[2] * p[2] + M[3],
        M[4] * p[0] + M[5] * p[1] + M[6] * p[2] + M[7],
        M[8] * p[0] + M[9] * p[1] + M[10] * p[2] + M[11],
    };

    public Affine Inverse()
    {
        double a = M[0], b = M[1], c = M[2], d = M[4], e = M[5], f = M[6], g = M[8], h = M[9], i = M[10];
        var det = a * (e * i - f * h) - b * (d * i - f * g) + c * (d * h - e * g);
        if (Math.Abs(det) < 1e-12) throw new InvalidOperationException("Affine 不可逆");
        var r = new[]
        {
            (e * i - f * h) / det, (c * h - b * i) / det, (b * f - c * e) / det,
            (f * g - d * i) / det, (a * i - c * g) / det, (c * d - a * f) / det,
            (d * h - e * g) / det, (b * g - a * h) / det, (a * e - b * d) / det,
        };
        double[] t = { M[3], M[7], M[11] };
        return new(new[]
        {
            r[0], r[1], r[2], -(r[0] * t[0] + r[1] * t[1] + r[2] * t[2]),
            r[3], r[4], r[5], -(r[3] * t[0] + r[4] * t[1] + r[5] * t[2]),
            r[6], r[7], r[8], -(r[6] * t[0] + r[7] * t[1] + r[8] * t[2]),
        });
    }
}

/// <summary>從 Revit 收集到的原始障礙物（Revit 內部座標，英呎）。二選一：Box（lo/hi）或 中心線（Start/End + RadiusFt）。</summary>
public sealed class RawObstacle
{
    public string Name { get; set; } = "";
    public string Kind { get; set; } = "other";
    public double[]? LoFt { get; set; }
    public double[]? HiFt { get; set; }
    public double[]? StartFt { get; set; }
    public double[]? EndFt { get; set; }
    /// <summary>中心線元件的半尺寸（圓管 = 半徑；矩形風管可給半對角線，保守）。</summary>
    public double RadiusFt { get; set; }
    /// <summary>保溫/內襯厚度（mm），各方向外擴。</summary>
    public double InsulationMm { get; set; }
}

public sealed class ObstacleOut
{
    public string Name { get; set; } = "";
    public string Kind { get; set; } = "other";
    public double[] Lo { get; set; } = new double[3];
    public double[] Hi { get; set; } = new double[3];
}

public sealed class DroppedObstacle
{
    public string Name { get; set; } = "";
    public string Kind { get; set; } = "";
    public string Reason { get; set; } = "";
}

public sealed class ExportOptions
{
    /// <summary>斜管分段長度上限（公尺）。軸向管不分段。</summary>
    public double MaxPieceM { get; set; } = 1.0;
    /// <summary>單一元件最多分幾段（避免一根長斜管吃光 200 個名額）。</summary>
    public int MaxPiecesPerElement { get; set; } = 20;
    /// <summary>方向與座標軸夾角小於此值（度）視為軸向，不分段。</summary>
    public double AxisAlignedTolDeg { get; set; } = 1.0;
    public int MaxCount { get; set; } = Obstacles.MaxCount;
    public int MaxBytes { get; set; } = Obstacles.MaxBytes;
}

public sealed class ExportResult
{
    public List<ObstacleOut> Obstacles { get; } = new();
    public List<DroppedObstacle> Dropped { get; } = new();
    public int Split { get; set; }
    public int Clipped { get; set; }
}

/// <summary>
/// 匯出障礙物的純幾何核心（不依賴 Revit API）：Revit 內部座標 → 房間本地公尺，
/// 與匯入器（RevitImporter：本地 mm → ToBasis → 英呎 → place）完全反向。輸出符合 MANUAL §2。
/// </summary>
public static class Obstacles
{
    public const int MaxCount = 200;
    public const int MaxBytes = 262_144;
    public const int MaxNameLen = 100;
    /// <summary>輸出座標的格點（公尺，0.1 mm）；lo 向下、hi 向上取整，只會更保守。</summary>
    public const double Quantum = 1e-4;

    public static readonly string[] Kinds = { "water", "duct", "heat", "heat_bare", "structure", "tray_power", "tray_signal", "other" };

    /// <summary>超過上限時的保留優先序（小者優先）：結構與熱源最不能漏。</summary>
    static readonly string[] Priority = { "structure", "heat_bare", "heat", "water", "duct", "tray_power", "tray_signal", "other" };

    /// <summary>ModelLoader.ToBasis 的反函數：basis 座標（mm）→ 模型本地座標（mm）。</summary>
    public static double[] FromBasis(CoordinateSystemDto cs, double[] basisMm)
    {
        var th = cs.RotationDeg * Math.PI / 180.0;
        double c = Math.Cos(th), s = Math.Sin(th);
        double dx = basisMm[0] - cs.Origin[0], dy = basisMm[1] - cs.Origin[1];
        return new[] { c * dx + s * dy, -s * dx + c * dy, basisMm[2] - cs.Origin[2] };
    }

    /// <summary>Revit 內部座標（英呎）→ 房間本地（公尺）。placeBasisFtToInternalFt 為匯入器使用的 place 變換。</summary>
    public static double[] InternalFtToLocalM(CoordinateSystemDto cs, Affine placeInverse, double[] internalFt)
    {
        var bFt = placeInverse.Apply(internalFt);
        var local = FromBasis(cs, new[] { Units.FeetToMm(bFt[0]), Units.FeetToMm(bFt[1]), Units.FeetToMm(bFt[2]) });
        return new[] { local[0] / 1000.0, local[1] / 1000.0, local[2] / 1000.0 };
    }

    /// <summary>以 8 個角點變換後取外接盒（旋轉時會變大，屬保守）。</summary>
    public static (double[] lo, double[] hi) TransformBox(Func<double[], double[]> f, double[] lo, double[] hi)
    {
        var rlo = new[] { double.PositiveInfinity, double.PositiveInfinity, double.PositiveInfinity };
        var rhi = new[] { double.NegativeInfinity, double.NegativeInfinity, double.NegativeInfinity };
        for (int k = 0; k < 8; k++)
        {
            var p = f(new[] { (k & 1) == 0 ? lo[0] : hi[0], (k & 2) == 0 ? lo[1] : hi[1], (k & 4) == 0 ? lo[2] : hi[2] });
            for (int a = 0; a < 3; a++) { rlo[a] = Math.Min(rlo[a], p[a]); rhi[a] = Math.Max(rhi[a], p[a]); }
        }
        return (rlo, rhi);
    }

    /// <summary>裁切到房間 [0, room]；完全在外或厚度為 0（僅接觸）回傳 null。</summary>
    public static (double[] lo, double[] hi)? Clip(double[] lo, double[] hi, double[] roomM)
    {
        var l = new double[3]; var h = new double[3];
        for (int a = 0; a < 3; a++)
        {
            l[a] = Math.Max(0, lo[a]); h[a] = Math.Min(roomM[a], hi[a]);
            if (!(h[a] > l[a])) return null;
        }
        return (l, h);
    }

    /// <summary>中心線 + 半徑 → 一或多個外接盒（本地公尺）。軸向（或很短）不分段；斜向依 maxPiece 均分。</summary>
    public static List<(double[] lo, double[] hi)> SplitSegment(double[] a, double[] b, double radiusM, ExportOptions opt)
    {
        var d = new[] { b[0] - a[0], b[1] - a[1], b[2] - a[2] };
        var len = Math.Sqrt(d[0] * d[0] + d[1] * d[1] + d[2] * d[2]);
        int n = 1;
        if (len > 0)
        {
            var maxComp = Math.Max(Math.Abs(d[0]), Math.Max(Math.Abs(d[1]), Math.Abs(d[2]))) / len;
            var axial = maxComp >= Math.Cos(opt.AxisAlignedTolDeg * Math.PI / 180.0);
            if (!axial) n = Math.Clamp((int)Math.Ceiling(len / opt.MaxPieceM), 1, Math.Max(1, opt.MaxPiecesPerElement));
        }
        var res = new List<(double[], double[])>();
        for (int k = 0; k < n; k++)
        {
            double t0 = (double)k / n, t1 = (double)(k + 1) / n;
            var lo = new double[3]; var hi = new double[3];
            for (int i = 0; i < 3; i++)
            {
                double p = a[i] + d[i] * t0, q = a[i] + d[i] * t1;
                lo[i] = Math.Min(p, q) - radiusM; hi[i] = Math.Max(p, q) + radiusM;
            }
            res.Add((lo, hi));
        }
        return res;
    }

    /// <summary>名稱清理：移除控制字元、去頭尾空白、以 Unicode 字元（code point）截到 max；空白則用 fallback。</summary>
    public static string SanitizeName(string? name, int max = MaxNameLen, string fallback = "obstacle")
    {
        var sb = new StringBuilder();
        foreach (var r in (name ?? "").EnumerateRunes())
            if (!Rune.IsControl(r) && r.Value != 0x2028 && r.Value != 0x2029) sb.Append(r.ToString());
        var s = sb.ToString().Trim();
        if (s.Length == 0) s = fallback;
        return TruncateRunes(s, max).TrimEnd() is { Length: > 0 } t ? t : fallback;
    }

    static string TruncateRunes(string s, int max)
    {
        var sb = new StringBuilder(); int n = 0;
        foreach (var r in s.EnumerateRunes()) { if (n++ >= max) break; sb.Append(r.ToString()); }
        return sb.ToString();
    }

    static string PieceName(string baseName, int k, int n)
    {
        if (n <= 1) return baseName;
        var suffix = $" #{k + 1}/{n}";
        return TruncateRunes(baseName, MaxNameLen - suffix.Length).TrimEnd() + suffix;
    }

    static double[] Quantize(double[] v, bool up) =>
        v.Select(x => Math.Round((up ? Math.Ceiling(x / Quantum - 1e-9) : Math.Floor(x / Quantum + 1e-9)) * Quantum, 4)).ToArray();

    /// <summary>
    /// 主流程：轉換 → 保溫外擴 → 斜管分段 → 裁切 → 量化 → 依優先序排序並套 200 個 / 256 KB 上限。結果完全決定性。
    /// placeBasisFtToInternalFt：匯入器的 place（basis 英呎 → 內部英呎）；roomM：房間尺寸（公尺）。
    /// </summary>
    public static ExportResult Build(IEnumerable<RawObstacle> raws, CoordinateSystemDto cs, Affine placeBasisFtToInternalFt,
                                     double[] roomM, ExportOptions? opt = null)
    {
        opt ??= new ExportOptions();
        var inv = placeBasisFtToInternalFt.Inverse();
        double[] ToLocal(double[] p) => InternalFtToLocalM(cs, inv, p);
        var res = new ExportResult();
        var cands = new List<(ObstacleOut o, int rank, double vol, int seq)>();
        int seq = 0;
        foreach (var r in raws)
        {
            var name = SanitizeName(r.Name);
            var kind = Array.IndexOf(Kinds, r.Kind) >= 0 ? r.Kind : "other";
            var padM = Math.Max(0, double.IsFinite(r.InsulationMm) ? r.InsulationMm : 0) / 1000.0;
            List<(double[] lo, double[] hi)> boxes;
            if (r.StartFt is { Length: 3 } s && r.EndFt is { Length: 3 } e && s.Concat(e).All(double.IsFinite) && double.IsFinite(r.RadiusFt))
            {
                var radM = Units.FeetToMm(Math.Max(0, r.RadiusFt)) / 1000.0 + padM;
                boxes = SplitSegment(ToLocal(s), ToLocal(e), radM, opt);
                padM = 0;
            }
            else if (r.LoFt is { Length: 3 } lo && r.HiFt is { Length: 3 } hi && lo.Concat(hi).All(double.IsFinite))
                boxes = new() { TransformBox(ToLocal, lo, hi) };
            else { res.Dropped.Add(new() { Name = name, Kind = kind, Reason = "invalid_geometry" }); continue; }

            if (boxes.Count > 1) res.Split++;
            int kept = 0;
            for (int k = 0; k < boxes.Count; k++)
            {
                var (blo, bhi) = boxes[k];
                blo = blo.Select(v => v - padM).ToArray(); bhi = bhi.Select(v => v + padM).ToArray();
                var c = Clip(blo, bhi, roomM);
                if (c == null) continue;
                var (clo, chi) = c.Value;
                if (Enumerable.Range(0, 3).Any(a => clo[a] > blo[a] || chi[a] < bhi[a])) res.Clipped++;
                var qlo = Quantize(clo, false); var qhi = Quantize(chi, true);
                for (int a = 0; a < 3; a++) { qlo[a] = Math.Max(0, qlo[a]); qhi[a] = Math.Min(roomM[a], qhi[a]); }
                if (Enumerable.Range(0, 3).Any(a => !(qhi[a] > qlo[a]))) continue;
                var vol = (qhi[0] - qlo[0]) * (qhi[1] - qlo[1]) * (qhi[2] - qlo[2]);
                cands.Add((new ObstacleOut { Name = PieceName(name, k, boxes.Count), Kind = kind, Lo = qlo, Hi = qhi },
                           Array.IndexOf(Priority, kind), vol, seq++));
                kept++;
            }
            if (kept == 0) res.Dropped.Add(new() { Name = name, Kind = kind, Reason = "outside_room" });
        }

        var ordered = cands.OrderBy(c => c.rank).ThenByDescending(c => c.vol).ThenBy(c => c.o.Name, StringComparer.Ordinal)
                           .ThenBy(c => c.seq).ToList();
        foreach (var c in ordered)
        {
            if (res.Obstacles.Count >= opt.MaxCount) { res.Dropped.Add(new() { Name = c.o.Name, Kind = c.o.Kind, Reason = "over_count_limit" }); continue; }
            res.Obstacles.Add(c.o);
            if (Utf8Bytes(res.Obstacles) > opt.MaxBytes)
            {
                res.Obstacles.RemoveAt(res.Obstacles.Count - 1);
                res.Dropped.Add(new() { Name = c.o.Name, Kind = c.o.Kind, Reason = "over_size_limit" });
            }
        }
        return res;
    }

    static readonly JsonSerializerOptions JsonOpt = new() { Encoder = JavaScriptEncoder.UnsafeRelaxedJsonEscaping, WriteIndented = false };

    /// <summary>MANUAL §2 格式的 JSON（清單），座標固定小數 4 位、不帶多餘欄位。</summary>
    public static string ToJson(IReadOnlyList<ObstacleOut> obs)
    {
        var sb = new StringBuilder("[");
        for (int i = 0; i < obs.Count; i++)
        {
            var o = obs[i];
            if (i > 0) sb.Append(",\n ");
            sb.Append("{\"name\": ").Append(JsonSerializer.Serialize(o.Name, JsonOpt))
              .Append(", \"kind\": ").Append(JsonSerializer.Serialize(o.Kind, JsonOpt))
              .Append(", \"lo\": ").Append(Vec(o.Lo)).Append(", \"hi\": ").Append(Vec(o.Hi)).Append('}');
        }
        return sb.Append(']').ToString();
    }

    static string Vec(double[] v) => "[" + string.Join(", ", v.Select(x => x.ToString("0.0###", CultureInfo.InvariantCulture))) + "]";

    public static int Utf8Bytes(IReadOnlyList<ObstacleOut> obs) => Encoding.UTF8.GetByteCount(ToJson(obs));

    /// <summary>輸出前的最終檢查（與 Python webui.validate_request 規則一致）；回傳錯誤清單（空 = 合格）。</summary>
    public static List<string> Validate(IReadOnlyList<ObstacleOut> obs, double[]? roomM = null)
    {
        var e = new List<string>();
        if (obs.Count > MaxCount) e.Add($"數量 {obs.Count} > {MaxCount}");
        if (Utf8Bytes(obs) > MaxBytes) e.Add("超過 256 KB");
        for (int i = 0; i < obs.Count; i++)
        {
            var o = obs[i];
            var n = o.Name.EnumerateRunes().Count();
            if (n < 1 || n > MaxNameLen || o.Name.Trim().Length == 0 || o.Name.EnumerateRunes().Any(Rune.IsControl)) e.Add($"[{i}] 名稱不合法");
            if (Array.IndexOf(Kinds, o.Kind) < 0) e.Add($"[{i}] kind 不合法");
            if (o.Lo?.Length != 3 || o.Hi?.Length != 3) { e.Add($"[{i}] lo/hi 需 3 個數字"); continue; }
            for (int a = 0; a < 3; a++)
            {
                if (!double.IsFinite(o.Lo[a]) || !double.IsFinite(o.Hi[a]) || !(o.Lo[a] < o.Hi[a])) e.Add($"[{i}] 軸 {a} 需 lo < hi");
                if (roomM != null && (o.Lo[a] < 0 || o.Hi[a] > roomM[a])) e.Add($"[{i}] 軸 {a} 超出房間");
            }
        }
        return e;
    }
}