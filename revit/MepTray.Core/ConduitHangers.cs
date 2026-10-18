using System.Text.Json.Nodes;

namespace MepTray.Core;

// 配管（Conduit）吊架核心（純邏輯，mm）——第一階段：兩支並排配管共用一支吊架。
// 與 Python mep_tray/conduit_supports.py 行為一致；契約案例由 tests/test_conduit_supports.py 產生、SelfTest 比對。
// 間距/內縮/固定間距餘數/去重/族/校正全部沿用橋架吊架設定（base_config，HangerConfigReader 驗證，行為不變）；
// 本檔只負責：配管設定驗證、兩管配對、中線、外緣是否落在橫擔內、放置尺寸（不加寬、橫擔長度固定）。

public sealed class ConduitInput
{
    public string Id { get; set; } = "";
    public double[] Start { get; set; } = new double[3];
    public double[] End { get; set; } = new double[3];
    /// <summary>Revit BuiltInParameter.RBS_CONDUIT_DIAMETER_PARAM（"Diameter(Trade Size)"）；讀不到 = null。</summary>
    public double? TradeSizeMm { get; set; }
    /// <summary>Revit BuiltInParameter.RBS_CONDUIT_OUTER_DIAM_PARAM（"Outside Diameter"）；讀不到 = null。</summary>
    public double? OuterDiameterMm { get; set; }
}

public sealed class ConduitPair
{
    public string Id { get; set; } = "";
    public string A { get; set; } = "";
    public string B { get; set; } = "";
    public double[] Start { get; set; } = new double[3];
    public double[] End { get; set; } = new double[3];
    public double OverlapMm { get; set; }
    public double CenterDistanceMinMm { get; set; }
    public double CenterDistanceMaxMm { get; set; }
    public double OuterWidthMm { get; set; }
    public double OuterDiameterMm { get; set; }
    public double BearingZMm { get; set; }
}

/// <summary>單管吊架段（使用者決定 2026-10-18；設定 cross_member_single 時才產生）：吊架中心在管軸、全長。</summary>
public sealed class ConduitSingle
{
    public string Id { get; set; } = "";
    public string Conduit { get; set; } = "";
    public double[] Start { get; set; } = new double[3];
    public double[] End { get; set; } = new double[3];
    public double LengthMm { get; set; }
    public double OuterDiameterMm { get; set; }
    public double OuterWidthMm { get; set; }
    public double BearingZMm { get; set; }
}

public sealed class ConduitPairResult
{
    public List<ConduitPair> Pairs { get; } = new();
    public List<ConduitSingle> Singles { get; } = new();
    public List<SupportIssue> Issues { get; } = new();
}

/// <summary>已驗證的配管吊架設定（conduit_hanger_config.json）。</summary>
public sealed class ConduitConfig
{
    public string BaseConfig = "";
    public int ConduitsPerHanger;
    public double[] AllowedTradeSizesMm = Array.Empty<double>();
    public double CrossMemberValueMm;
    /// <summary>parameter：值直接寫入族的橫擔長度參數；measured_length：實測橫擔長度，經校正換算為參數。</summary>
    public string CrossMemberBasis = "";
    public double AngleToleranceDeg, ElevationToleranceMm;
    /// <summary>單管橫擔（cross_member_single）；null = 未設定 → 單管列待確認（與先前相同）。</summary>
    public double? SingleCrossMemberValueMm;
    public string SingleCrossMemberBasis = "";
}

public static class ConduitConfigReader
{
    public static readonly string[] SupportedObjects = { "conduit_pair" };
    public static readonly string[] CrossBases = { "parameter", "measured_length" };

    static double? Num(JsonNode? n)
    {
        if (n is not JsonValue v) return null;
        if (v.GetValueKind() == System.Text.Json.JsonValueKind.Number && v.TryGetValue<double>(out var d) && double.IsFinite(d)) return d;
        return null;
    }
    static string? Str(JsonNode? n) => n is JsonValue v && v.TryGetValue<string>(out var s) && !string.IsNullOrWhiteSpace(s) ? s : null;
    static bool Missing(JsonObject o, string k) => !o.TryGetPropertyValue(k, out var n) || n is null;

    public static List<string> Validate(JsonNode? root)
    {
        if (root is not JsonObject c) return new List<string> { "config：必須是 JSON 物件" };
        var e = new List<string>();
        if (Str(c["units"]) != "mm") e.Add("units：必須為 \"mm\"");
        if (Array.IndexOf(SupportedObjects, Str(c["supported_object"])) < 0) e.Add("supported_object：需為 conduit_pair");
        if (Str(c["base_config"]) is null) e.Add("base_config：缺少（橋架吊架設定檔路徑；族、校正、間距、去重沿用該檔）");
        if (Missing(c, "conduits_per_hanger")) e.Add("conduits_per_hanger：缺少（v1 只支援 2）");
        else if (!(c["conduits_per_hanger"] is JsonValue nv && nv.GetValueKind() == System.Text.Json.JsonValueKind.Number
                   && nv.TryGetValue<int>(out var n) && n == 2))
            e.Add("conduits_per_hanger：v1 只支援 2");
        if (Missing(c, "allowed_trade_sizes_mm")) e.Add("allowed_trade_sizes_mm：缺少（允許的配管管徑 Diameter(Trade Size)，工具不提供預設）");
        else if (c["allowed_trade_sizes_mm"] is not JsonArray ts || ts.Count == 0 || ts.Any(x => !(Num(x) is double d && d > 0)))
            e.Add("allowed_trade_sizes_mm：需為非空陣列，元素為 > 0 的有限數");
        if (c["cross_member"] is not JsonObject cm) e.Add("cross_member：缺少（{\"value_mm\": 橫擔長度, \"basis\": parameter|measured_length}）");
        else
        {
            var v = Num(cm["value_mm"]);
            if (Missing(cm, "value_mm")) e.Add("cross_member.value_mm：缺少");
            else if (v is null || v <= 0) e.Add("cross_member.value_mm：需為 > 0 的有限數");
            if (Array.IndexOf(CrossBases, Str(cm["basis"])) < 0)
                e.Add("cross_member.basis：需為 parameter（值直接寫入族的橫擔長度參數）或 measured_length（實測橫擔長度，經校正換算）");
        }
        if (c.ContainsKey("cross_member_single"))   // 選填；未填 = 單管不放置（列待確認），與先前相同
        {
            if (c["cross_member_single"] is not JsonObject cs)
                e.Add("cross_member_single：需為 {\"value_mm\": 單管橫擔長度, \"basis\": parameter|measured_length}（不放單管吊架時整個欄位省略）");
            else
            {
                var v = Num(cs["value_mm"]);
                if (Missing(cs, "value_mm")) e.Add("cross_member_single.value_mm：缺少");
                else if (v is null || v <= 0) e.Add("cross_member_single.value_mm：需為 > 0 的有限數");
                if (Array.IndexOf(CrossBases, Str(cs["basis"])) < 0) e.Add("cross_member_single.basis：需為 parameter 或 measured_length");
            }
        }
        if (c["pairing"] is not JsonObject pr) e.Add("pairing：缺少（angle_tolerance_deg、elevation_tolerance_mm）");
        else
        {
            var a = Num(pr["angle_tolerance_deg"]);
            if (a is null || a < 0 || a >= 90) e.Add("pairing.angle_tolerance_deg：需為 0 ≤ 值 < 90");
            var t = Num(pr["elevation_tolerance_mm"]);
            if (t is null || t <= 0) e.Add("pairing.elevation_tolerance_mm：需為 > 0 的有限數");
        }
        return e;
    }

    public static ConduitConfig Read(JsonNode? root)
    {
        var errs = Validate(root);
        if (errs.Count > 0) throw new InvalidDataException("配管吊架設定不完整：" + string.Join("; ", errs));
        var c = (JsonObject)root!;
        var single = c["cross_member_single"] as JsonObject;
        return new ConduitConfig
        {
            SingleCrossMemberValueMm = single is null ? null : Num(single["value_mm"]),
            SingleCrossMemberBasis = single is null ? "" : Str(single["basis"])!,
            BaseConfig = Str(c["base_config"])!,
            ConduitsPerHanger = c["conduits_per_hanger"]!.GetValue<int>(),
            AllowedTradeSizesMm = c["allowed_trade_sizes_mm"]!.AsArray().Select(x => Num(x)!.Value).ToArray(),
            CrossMemberValueMm = Num(c["cross_member"]!["value_mm"])!.Value,
            CrossMemberBasis = Str(c["cross_member"]!["basis"])!,
            AngleToleranceDeg = Num(c["pairing"]!["angle_tolerance_deg"])!.Value,
            ElevationToleranceMm = Num(c["pairing"]!["elevation_tolerance_mm"])!.Value,
        };
    }
}

public static class ConduitHangers
{
    public const double GeomTolMm = Supports.GeomTolMm;   // 幾何判定容差；非規範值

    static double R3(double v) => Math.Round(v, 3) + 0.0;
    static double[] P3(double[] p) => p.Select(R3).ToArray();
    static SupportIssue Issue(string level, string code, string id, double[] loc, string detail) => new(level, code, id, P3(loc), detail);

    sealed class Ok
    {
        public string Id = "";
        public double[] Start = new double[3], End = new double[3], Mid = new double[3];
        public double Len, Z, Od, Ts;
        public double[] U = new double[2];
    }

    sealed class Check
    {
        public bool IsOk;
        public string Code = "", Detail = "";
        public double Lo, Hi, Dlo, Dhi, Tb0, Tb1, Dmin, Dmax, Width;
    }

    sealed class Lat
    {
        public double Lo, Hi, Dlo, Dhi, Tb0, Tb1, Dmin;
    }

    /// <summary>線段 s0→e0 投影到 a 的軸上：重疊區與兩端側向距離（有號）；沒有重疊 → null。Dmin：跨越軸線時為 0。</summary>
    static Lat? Lateral(Ok a, double[] s0, double[] e0)
    {
        var u = a.U; var o = a.Start;
        double nx = -u[1], ny = u[0];
        double tb0 = (s0[0] - o[0]) * u[0] + (s0[1] - o[1]) * u[1];
        double tb1 = (e0[0] - o[0]) * u[0] + (e0[1] - o[1]) * u[1];
        double lo = Math.Max(0.0, Math.Min(tb0, tb1)), hi = Math.Min(a.Len, Math.Max(tb0, tb1));
        if (hi - lo <= GeomTolMm || Math.Abs(tb1 - tb0) <= 1e-12) return null;
        double L(double t)
        {
            var f = (t - tb0) / (tb1 - tb0);
            var px = s0[0] + (e0[0] - s0[0]) * f;
            var py = s0[1] + (e0[1] - s0[1]) * f;
            return (px - o[0]) * nx + (py - o[1]) * ny;
        }
        double dlo = L(lo), dhi = L(hi);
        return new Lat { Lo = lo, Hi = hi, Dlo = dlo, Dhi = dhi, Tb0 = tb0, Tb1 = tb1, Dmin = dlo * dhi < 0 ? 0.0 : Math.Min(Math.Abs(dlo), Math.Abs(dhi)) };
    }

    static double AngleDeg(double[] u, double[] v) => Math.Acos(Math.Min(1.0, Math.Abs(u[0] * v[0] + u[1] * v[1]))) * 180.0 / Math.PI;

    static Check CheckPair(Ok a, Ok b, ConduitConfig cc)
    {
        var ang = AngleDeg(a.U, b.U);
        if (ang > cc.AngleToleranceDeg + 1e-9) return new Check { Code = "not_parallel", Detail = $"夾角 {ang:F2}°" };
        var dz = Math.Abs(a.Z - b.Z);
        if (dz > cc.ElevationToleranceMm + 1e-9) return new Check { Code = "elevation_mismatch", Detail = $"中心高差 {dz:F1} mm" };
        if (Math.Abs(a.Od - b.Od) > GeomTolMm) return new Check { Code = "diameter_mismatch", Detail = $"外徑 {a.Od:F1} / {b.Od:F1} mm" };
        var u = a.U; var o = a.Start;
        double nx = -u[1], ny = u[0];
        double Proj(double[] p) => (p[0] - o[0]) * u[0] + (p[1] - o[1]) * u[1];
        double tb0 = Proj(b.Start), tb1 = Proj(b.End);
        double lo = Math.Max(0.0, Math.Min(tb0, tb1)), hi = Math.Min(a.Len, Math.Max(tb0, tb1));
        if (hi - lo <= GeomTolMm) return new Check { Code = "no_overlap", Detail = "沿管方向沒有重疊段" };
        double Lat(double t)
        {
            var f = (t - tb0) / (tb1 - tb0);
            var px = b.Start[0] + (b.End[0] - b.Start[0]) * f;
            var py = b.Start[1] + (b.End[1] - b.Start[1]) * f;
            return (px - o[0]) * nx + (py - o[1]) * ny;
        }
        double dlo = Lat(lo), dhi = Lat(hi);
        double dmin = Math.Min(Math.Abs(dlo), Math.Abs(dhi)), dmax = Math.Max(Math.Abs(dlo), Math.Abs(dhi));
        var od = a.Od;
        if (dlo * dhi < 0 || dmin < od - GeomTolMm)
            return new Check { Code = "too_close", Detail = $"中心距 {dmin:F1} mm 小於外徑 {od:F1} mm（重疊／非並排）" };
        var width = dmax + od;
        var cm = cc.CrossMemberValueMm;
        if (width > cm + GeomTolMm)
            return new Check { Code = "exceeds_crossbar", Detail = $"兩管外緣總寬 {width:F1} mm（最大中心距 {dmax:F1} + 外徑 {od:F1}）超過橫擔 {cm:F1} mm" };
        return new Check { IsOk = true, Lo = lo, Hi = hi, Dlo = dlo, Dhi = dhi, Tb0 = tb0, Tb1 = tb1, Dmin = dmin, Dmax = dmax, Width = width };
    }

    /// <summary>兩管配對（依輸入順序）。不配對者一律列為待確認／不支援，不放置。</summary>
    public static ConduitPairResult Pair(IReadOnlyList<ConduitInput> conduits, ConduitConfig cc)
    {
        var res = new ConduitPairResult();
        var issues = res.Issues;
        var ok = new List<Ok>();
        foreach (var c in conduits)
        {
            var st = c.Start; var en = c.End;
            var d = new[] { en[0] - st[0], en[1] - st[1], en[2] - st[2] };
            var h = Math.Sqrt(d[0] * d[0] + d[1] * d[1]);
            var L = Math.Sqrt(h * h + d[2] * d[2]);
            var mid = new[] { (st[0] + en[0]) / 2, (st[1] + en[1]) / 2, (st[2] + en[2]) / 2 };
            if (L <= GeomTolMm) { issues.Add(Issue("failed", "zero_length", c.Id, st, "長度為 0，未處理")); continue; }
            if (Math.Abs(d[2]) > GeomTolMm)
            {
                var code = h <= GeomTolMm ? "vertical" : "sloped";
                issues.Add(Issue("unsupported", code, c.Id, mid, code == "vertical" ? "垂直配管 v1 不支援" : $"斜配管 v1 不支援（高差 {Math.Abs(d[2]):F1} mm）"));
                continue;
            }
            if (c.TradeSizeMm is not double ts || !double.IsFinite(ts) || ts <= 0)
            { issues.Add(Issue("failed", "no_diameter", c.Id, mid, "讀不到管徑 Diameter(Trade Size)")); continue; }
            if (!cc.AllowedTradeSizesMm.Any(a => Math.Abs(ts - a) <= GeomTolMm))
            {
                issues.Add(Issue("pending", "diameter_not_allowed", c.Id, mid, $"管徑 {ts:F1} mm 不在允許清單 [{string.Join(", ", cc.AllowedTradeSizesMm)}]，未放置"));
                continue;
            }
            double od;
            if (c.OuterDiameterMm is double o0 && double.IsFinite(o0) && o0 > 0) od = o0;
            else
            {
                issues.Add(Issue("info", "outer_diameter_fallback", c.Id, mid, $"讀不到外徑 Outside Diameter，外緣檢查改用管徑 {ts:F1} mm"));
                od = ts;
            }
            ok.Add(new Ok { Id = c.Id, Start = (double[])st.Clone(), End = (double[])en.Clone(), Len = h, U = new[] { d[0] / h, d[1] / h }, Z = st[2], Od = od, Ts = ts, Mid = mid });
        }

        var cands = new List<(int I, int J, Check R)>();
        var fails = new List<(int I, int J, Check R)>();
        var reasons = ok.ToDictionary(x => x.Id, _ => new List<string>());
        for (int i = 0; i < ok.Count; i++)
            for (int j = i + 1; j < ok.Count; j++)
            {
                var a = ok[i]; var b = ok[j];
                var r = CheckPair(a, b, cc);
                if (r.IsOk) { cands.Add((i, j, r)); continue; }
                reasons[a.Id].Add($"{b.Id}:{r.Code}");
                reasons[b.Id].Add($"{a.Id}:{r.Code}");
                if (r.Code is "too_close" or "exceeds_crossbar") fails.Add((i, j, r));
            }
        var cnt = new int[ok.Count];
        foreach (var (i, j, _) in cands) { cnt[i]++; cnt[j]++; }
        var accepted = cands.Where(x => cnt[x.I] == 1 && cnt[x.J] == 1).ToList();
        var paired = new HashSet<int>(accepted.SelectMany(x => new[] { x.I, x.J }));

        var tails = new List<SupportIssue>();
        foreach (var (i, j, r) in accepted)
        {
            var a = ok[i]; var b = ok[j];
            var u = a.U; var o = a.Start;
            double nx = -u[1], ny = u[0];
            var z = (a.Z + b.Z) / 2;
            var s = new[] { o[0] + u[0] * r.Lo + nx * r.Dlo / 2, o[1] + u[1] * r.Lo + ny * r.Dlo / 2, z };
            var e = new[] { o[0] + u[0] * r.Hi + nx * r.Dhi / 2, o[1] + u[1] * r.Hi + ny * r.Dhi / 2, z };
            var pid = $"{a.Id}+{b.Id}";
            res.Pairs.Add(new ConduitPair
            {
                Id = pid, A = a.Id, B = b.Id, Start = P3(s), End = P3(e),
                OverlapMm = R3(Math.Sqrt(Math.Pow(e[0] - s[0], 2) + Math.Pow(e[1] - s[1], 2) + Math.Pow(e[2] - s[2], 2))),
                CenterDistanceMinMm = R3(r.Dmin), CenterDistanceMaxMm = R3(r.Dmax), OuterWidthMm = R3(r.Width),
                OuterDiameterMm = R3(a.Od), BearingZMm = R3(z - a.Od / 2),
            });
            double bmin = Math.Min(r.Tb0, r.Tb1), bmax = Math.Max(r.Tb0, r.Tb1);
            double[] At(Ok c, double t)
            {
                if (ReferenceEquals(c, a)) return new[] { o[0] + u[0] * t, o[1] + u[1] * t, a.Z };
                var f = (t - r.Tb0) / (r.Tb1 - r.Tb0);
                return new[] { b.Start[0] + (b.End[0] - b.Start[0]) * f, b.Start[1] + (b.End[1] - b.Start[1]) * f, b.Start[2] + (b.End[2] - b.Start[2]) * f };
            }
            foreach (var (c, t0, t1) in new[] { (a, 0.0, r.Lo), (a, r.Hi, a.Len), (b, bmin, r.Lo), (b, r.Hi, bmax) })
                if (t1 - t0 > GeomTolMm)
                    tails.Add(Issue("pending", "unsupported_tail", c.Id, At(c, (t0 + t1) / 2), $"{pid} 重疊段外的尾段 {t1 - t0:F1} mm 未放吊架（單管段，待確認）"));
        }

        // 每支未配成對的配管：ambiguous（有候選但不唯一，不猜測）／single 候選（設定 cross_member_single 時）／unpaired
        var status = new Dictionary<int, string>();
        for (int k = 0; k < ok.Count; k++)
        {
            if (paired.Contains(k)) continue;
            status[k] = cands.Any(x => x.I == k || x.J == k) ? "ambiguous" : cc.SingleCrossMemberValueMm is not null ? "single" : "unpaired";
        }
        var conflicts = new Dictionary<int, List<string>>();
        if (cc.SingleCrossMemberValueMm is double ws)
        {
            var wp = cc.CrossMemberValueMm;
            bool Near(Ok c, double[] ou, double oz, double[] s0, double[] e0, double limit)
            {
                if (AngleDeg(c.U, ou) > cc.AngleToleranceDeg + 1e-9) return false;
                if (Math.Abs(c.Z - oz) > cc.ElevationToleranceMm + 1e-9) return false;
                var lt = Lateral(c, s0, e0);
                return lt is not null && lt.Dmin + GeomTolMm < limit;
            }
            foreach (var k in status.Keys.OrderBy(x => x).ToList())
            {
                if (status[k] != "single") continue;
                var c = ok[k];
                var hit = new List<string>();
                for (int q = 0; q < accepted.Count; q++)      // 鄰近配對吊架：兩橫擔（各以中心線為中心）在平面上重疊
                {
                    var p = res.Pairs[q]; var m0 = ok[accepted[q].I];
                    if (Near(c, m0.U, m0.Z, p.Start, p.End, (ws + wp) / 2)) hit.Add(p.Id);
                }
                for (int m = 0; m < ok.Count; m++)            // 其他未配對配管：單管橫擔彼此重疊／管體重疊
                {
                    if (m == k || paired.Contains(m)) continue;
                    if (Near(c, ok[m].U, ok[m].Z, ok[m].Start, ok[m].End, ws)) hit.Add(ok[m].Id);
                }
                if (hit.Count > 0) { conflicts[k] = hit; status[k] = "single_conflict"; }
            }
        }
        var hung = new HashSet<int>(paired.Concat(status.Where(x => x.Value == "single").Select(x => x.Key)));
        foreach (var (i, j, r) in fails)   // 只對兩支都沒吊架者回報（已各自配對／單管吊架者不必提示）
        {
            if (hung.Contains(i) || hung.Contains(j)) continue;
            var a = ok[i]; var b = ok[j];
            var loc = new[] { (a.Mid[0] + b.Mid[0]) / 2, (a.Mid[1] + b.Mid[1]) / 2, (a.Mid[2] + b.Mid[2]) / 2 };
            issues.Add(Issue("pending", r.Code, $"{a.Id}+{b.Id}", loc, r.Detail + "，未放置"));
        }
        for (int k = 0; k < ok.Count; k++)
        {
            if (!status.TryGetValue(k, out var st)) continue;
            var c = ok[k];
            switch (st)
            {
                case "ambiguous":
                    var partners = cands.Where(x => x.I == k || x.J == k).Select(x => ok[x.I == k ? x.J : x.I].Id).ToList();
                    issues.Add(Issue("pending", "ambiguous_pair", c.Id, c.Mid, $"可與多支配管配對或對象已有其他配對（{string.Join(", ", partners)}），不猜測，未放置"));
                    break;
                case "unpaired":
                    if (ok.Count == 1) issues.Add(Issue("pending", "unpaired", c.Id, c.Mid, "單支配管（需兩支並排共用吊架），未放置"));
                    else issues.Add(Issue("pending", "unpaired", c.Id, c.Mid, "找不到可配對的配管（" + string.Join("; ", reasons[c.Id]) + "），未放置"));
                    break;
                case "single_conflict":
                    issues.Add(Issue("pending", "single_conflict", c.Id, c.Mid, $"單管吊架橫擔可能與鄰近吊架／配管重疊（{string.Join(", ", conflicts[k])}），未放置"));
                    break;
                default:
                    res.Singles.Add(new ConduitSingle
                    {
                        Id = c.Id, Conduit = c.Id, Start = P3(c.Start), End = P3(c.End), LengthMm = R3(c.Len),
                        OuterDiameterMm = R3(c.Od), OuterWidthMm = R3(c.Od), BearingZMm = R3(c.Z - c.Od / 2),
                    });
                    break;
            }
        }
        issues.AddRange(tails);
        return res;
    }

    /// <summary>配對（與單管）→ Supports.Layout 用的 pieces（寬 = 外緣總寬，高 = 外徑 → 承托面 = 中心 − 外徑/2）。
    /// 配對 id 含「+」，單管 id = 配管 id，兩者不會重複。</summary>
    public static List<SupportPiece> ToPieces(IEnumerable<ConduitPair> pairs, IEnumerable<ConduitSingle>? singles = null) =>
        pairs.Select(p => new SupportPiece
        {
            Id = p.Id, Start = (double[])p.Start.Clone(), End = (double[])p.End.Clone(), WidthMm = p.OuterWidthMm, HeightMm = p.OuterDiameterMm,
        }).Concat((singles ?? Enumerable.Empty<ConduitSingle>()).Select(p => new SupportPiece
        {
            Id = p.Id, Start = (double[])p.Start.Clone(), End = (double[])p.End.Clone(), WidthMm = p.OuterWidthMm, HeightMm = p.OuterDiameterMm,
        })).ToList();

    /// <summary>(實測橫擔長度, 族橫擔參數值)，mm。依校正：實測 = ratio × 參數 + offset。</summary>
    public static (double LengthMm, double ParamMm) CrossMember(HangerConfig baseCfg, ConduitConfig cc, bool single = false)
    {
        if (single && cc.SingleCrossMemberValueMm is null) throw new InvalidOperationException("cross_member_single 未設定，不能放單管吊架");
        var v = single ? cc.SingleCrossMemberValueMm!.Value : cc.CrossMemberValueMm;
        var basis = single ? cc.SingleCrossMemberBasis : cc.CrossMemberBasis;
        return basis == "parameter"
            ? (R3(baseCfg.CrossRatio * v + baseCfg.CrossOffsetMm), R3(v))
            : (R3(v), R3((v - baseCfg.CrossOffsetMm) / baseCfg.CrossRatio));
    }

    /// <summary>支撐點 → 族插入點／旋轉／尺寸：沿用橋架設定的族實測值，但不加寬，橫擔長度固定為設定值。</summary>
    public static Placement Place(SupportPoint sp, HangerConfig baseCfg, ConduitConfig cc, bool single = false)
    {
        double px = sp.PointMm[0], py = sp.PointMm[1], bz = sp.BearingZMm;
        return new Placement(
            P3(new[] { px, py, bz }),
            P3(new[] { px, py, bz - baseCfg.InsertionToBearingMm }),
            Supports.Norm360(sp.DirectionDeg + baseCfg.OrientationCorrectionDeg),
            CrossMember(baseCfg, cc, single).LengthMm,
            baseCfg.RodMode == "fixed" && baseCfg.RodLengthMm is double r ? R3(r) : null);
    }

    // ── 去重鍵（寫入 Extensible Storage 的 TrayUniqueId 欄位）──
    public const string PairKeyPrefix = "conduit-pair:";
    public const string SingleKeyPrefix = "conduit-single:";

    public static string PairKey(string uidA, string uidB)
    {
        var u = new[] { uidA, uidB }.OrderBy(x => x, StringComparer.Ordinal).ToArray();
        return $"{PairKeyPrefix}{u[0]}|{u[1]}";
    }

    public static string SingleKey(string uid) => SingleKeyPrefix + uid;

    /// <summary>配管吊架鍵 → 涵蓋的配管 UniqueId；非配管鍵（橋架）→ 空。</summary>
    public static string[] KeyConduits(string? key) =>
        key is null ? Array.Empty<string>()
        : key.StartsWith(PairKeyPrefix, StringComparison.Ordinal) ? key[PairKeyPrefix.Length..].Split('|')
        : key.StartsWith(SingleKeyPrefix, StringComparison.Ordinal) ? new[] { key[SingleKeyPrefix.Length..] }
        : Array.Empty<string>();

    /// <summary>同一支配管已被「不同組合」（配對↔單管、或與另一支配對）的本工具吊架承托 → 那些吊架 id（依輸入順序）。
    /// 呼叫端把該計畫標為待確認，不重複吊掛；不刪除、不修改既有吊架。</summary>
    public static List<string> CoveringHangers(string plannedKey, IEnumerable<ExistingHanger> existing)
    {
        var mine = new HashSet<string>(KeyConduits(plannedKey), StringComparer.Ordinal);
        if (mine.Count == 0) return new List<string>();
        return existing.Where(e => e.Source == "tool" && e.TrayKey != plannedKey && KeyConduits(e.TrayKey).Any(mine.Contains)).Select(e => e.Id).ToList();
    }
}
