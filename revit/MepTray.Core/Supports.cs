using System.Text.Json.Nodes;

namespace MepTray.Core;

// 吊架支撐點核心（純邏輯，mm）。與 Python mep_tray/supports.py 行為一致；契約案例由 tests/test_supports.py 產生、SelfTest 比對。
// 不含任何規範數值預設：間距/內縮/族參數全部來自使用者設定，缺漏即拒絕並指出欄位。

public sealed class SupportPiece
{
    public string Id { get; set; } = "";
    public double[] Start { get; set; } = new double[3];
    public double[] End { get; set; } = new double[3];
    public double WidthMm { get; set; }
    public double HeightMm { get; set; }
    /// <summary>free | fitting（端點接到接頭本體）。</summary>
    public string StartKind { get; set; } = "free";
    public string EndKind { get; set; } = "free";
}

public sealed record SupportLink(string A, string B, string Kind);

public sealed class SupportPoint
{
    public string Id { get; set; } = "";
    public string PieceId { get; set; } = "";
    public string ChainId { get; set; } = "";
    public double StationMm { get; set; }
    public double[] PointMm { get; set; } = new double[3];
    public double DirectionDeg { get; set; }
    public double TrayWidthMm { get; set; }
    public double TrayHeightMm { get; set; }
    public double BearingZMm { get; set; }
    public string Reason { get; set; } = "";
}

public sealed record SupportIssue(string Level, string Code, string PieceId, double[] LocationMm, string Detail);

public sealed class LayoutResult
{
    public List<SupportPoint> Supports { get; } = new();
    public List<SupportIssue> Issues { get; } = new();
}

public sealed record Placement(double[] BearingPointMm, double[] InsertionPointMm, double RotationDeg, double CrossMemberLengthMm, double? RodLengthMm);

public sealed record ExistingHanger(string Id, string Source, string? TrayKey, double? StationMm, double[]? PointMm, double? DirectionDeg);
public sealed record PlannedKey(string TrayKey, double StationMm, double[] PointMm, double DirectionDeg);
public sealed record Classification(string Status, List<string> Match, string Reason);

public sealed record ParamRef(string Name, string Scope);

/// <summary>已驗證的設定（由 HangerConfigReader.Read 產生）。</summary>
public sealed class HangerConfig
{
    public double MaxSpacingMm, StartOffsetMm, EndOffsetMm;
    public string ShortRunPolicy = "";
    public string SpacingMode = "even";
    public bool NearFittingEnabled;
    public double? NearFittingMaxDistanceMm;
    // 以下僅放置用（layout-only 讀取時可能為預設值）
    public string SupportedObject = "", FamilyName = "", TypeName = "", PlacementMode = "";
    public double WidenLeftMm, WidenRightMm, InsertionToBearingMm, OrientationCorrectionDeg;
    public ParamRef? CrossMemberLength;
    public string RodMode = "";
    public double? RodLengthMm;
    public ParamRef? RodParam;
    public double DedupeToleranceMm, DedupeAngleToleranceDeg;
    // 校正（「校正目標吊架」寫入；放置前由 Revit 端核對族檔與專案中的族）
    public string CalibFilePath = "", CalibSha256 = "", CalibProjectSignature = "";
    public double CalibFileSize;
    public double CrossRatio = 1, CrossOffsetMm;
}

public static class HangerConfigReader
{
    public static readonly string[] ShortPolicies = { "midpoint", "skip" };
    public static readonly string[] SpacingModes = { "even", "fixed" };
    public static readonly string[] PlacementModes = { "level", "workplane" };
    public static readonly string[] Scopes = { "type", "instance" };
    public static readonly string[] RodModes = { "none", "fixed" };
    public static readonly string[] SupportedObjects = { "cable_tray" };

    static double? Num(JsonNode? n)
    {
        if (n is not JsonValue v) return null;
        if (v.TryGetValue<double>(out var d) && double.IsFinite(d) && v.GetValueKind() == System.Text.Json.JsonValueKind.Number) return d;
        return null;
    }
    static string? Str(JsonNode? n) => n is JsonValue v && v.TryGetValue<string>(out var s) && !string.IsNullOrWhiteSpace(s) ? s : null;
    static bool Missing(JsonObject o, string k) => !o.TryGetPropertyValue(k, out var n) || n is null;

    public static List<string> ValidateLayout(JsonNode? root)
    {
        if (root is not JsonObject c) return new List<string> { "config：必須是 JSON 物件" };
        var e = new List<string>();
        if (Str(c["units"]) != "mm") e.Add("units：必須為 \"mm\"");
        var s = Num(c["max_spacing_mm"]);
        if (Missing(c, "max_spacing_mm")) e.Add("max_spacing_mm：缺少（最大支撐間距，工具不提供預設）");
        else if (s is null || s <= 0) e.Add("max_spacing_mm：需為 > 0 的有限數");
        foreach (var k in new[] { "start_offset_mm", "end_offset_mm" })
        {
            var v = Num(c[k]);
            if (Missing(c, k)) e.Add($"{k}：缺少（工具不提供預設）");
            else if (v is null || v < 0) e.Add($"{k}：需為 ≥ 0 的有限數");
        }
        if (Missing(c, "short_run_policy")) e.Add("short_run_policy：缺少（midpoint 或 skip）");
        else if (Array.IndexOf(ShortPolicies, Str(c["short_run_policy"])) < 0) e.Add("short_run_policy：需為 midpoint 或 skip");
        if (c.ContainsKey("spacing_mode") && Array.IndexOf(SpacingModes, Str(c["spacing_mode"])) < 0) e.Add("spacing_mode：需為 even 或 fixed（未填 = even）");
        if (Missing(c, "near_fitting")) e.Add("near_fitting：缺少（不加強時填 {\"enabled\": false}）");
        else if (c["near_fitting"] is not JsonObject nf || nf["enabled"] is not JsonValue ev || !ev.TryGetValue<bool>(out var en))
            e.Add("near_fitting.enabled：需為 true/false");
        else if (en)
        {
            var d = Num(nf["max_distance_mm"]);
            if (Missing(nf, "max_distance_mm")) e.Add("near_fitting.max_distance_mm：啟用時必填");
            else if (d is null || d <= 0) e.Add("near_fitting.max_distance_mm：需為 > 0 的有限數");
            else if (s is > 0 && d > s) e.Add("near_fitting.max_distance_mm：不得大於 max_spacing_mm");
        }
        return e;
    }

    static List<string> ParamRefErrors(JsonNode? n, string field)
    {
        if (n is not JsonObject o) return new List<string> { $"{field}：缺少（需 {{\"name\": 族參數名稱, \"scope\": type|instance}}，取自族盤點）" };
        var e = new List<string>();
        if (Str(o["name"]) is null) e.Add($"{field}.name：缺少");
        if (Array.IndexOf(Scopes, Str(o["scope"])) < 0) e.Add($"{field}.scope：需為 type 或 instance");
        return e;
    }

    public static List<string> ValidatePlacement(JsonNode? root)
    {
        var e = ValidateLayout(root);
        if (root is not JsonObject c) return e;
        if (Array.IndexOf(SupportedObjects, Str(c["supported_object"])) < 0) e.Add("supported_object：v1 只支援 cable_tray");
        if (c["family"] is not JsonObject fam) e.Add("family：缺少（family_name、type_name）");
        else foreach (var k in new[] { "family_name", "type_name" }) if (Str(fam[k]) is null) e.Add($"family.{k}：缺少");
        foreach (var k in new[] { "widen_left_mm", "widen_right_mm" })
        {
            var v = Num(c[k]);
            if (Missing(c, k)) e.Add($"{k}：缺少（相對橋架寬度的加寬量，可為 0，但需明確填寫）");
            else if (v is null || v < 0) e.Add($"{k}：需為 ≥ 0 的有限數");
        }
        if (Missing(c, "insertion_to_bearing_mm")) e.Add("insertion_to_bearing_mm：缺少（族插入點到承托面的垂直距離，需由族實測）");
        else if (Num(c["insertion_to_bearing_mm"]) is null) e.Add("insertion_to_bearing_mm：需為有限數");
        if (Missing(c, "orientation_correction_deg")) e.Add("orientation_correction_deg：缺少（族橫擔軸向相對橋架方向的修正角，需由族實測）");
        else if (Num(c["orientation_correction_deg"]) is null) e.Add("orientation_correction_deg：需為有限數");
        if (Array.IndexOf(PlacementModes, Str(c["placement_mode"])) < 0) e.Add("placement_mode：需為 level 或 workplane（需與族的放置方式一致）");
        if (c["parameter_map"] is not JsonObject pm) e.Add("parameter_map：缺少");
        else e.AddRange(ParamRefErrors(pm["cross_member_length"], "parameter_map.cross_member_length"));
        if (c["rod_length"] is not JsonObject rod || Array.IndexOf(RodModes, Str(rod["mode"])) < 0) e.Add("rod_length.mode：需為 none 或 fixed");
        else if (Str(rod["mode"]) == "fixed")
        {
            var v = Num(rod["value_mm"]);
            if (Missing(rod, "value_mm")) e.Add("rod_length.value_mm：fixed 時必填");
            else if (v is null || v <= 0) e.Add("rod_length.value_mm：需為 > 0 的有限數");
            e.AddRange(ParamRefErrors(rod["param"], "rod_length.param"));
        }
        if (c["dedupe"] is not JsonObject dd) e.Add("dedupe：缺少（tolerance_mm、angle_tolerance_deg）");
        else
        {
            var t = Num(dd["tolerance_mm"]);
            if (t is null || t <= 0) e.Add("dedupe.tolerance_mm：需為 > 0 的有限數");
            var a = Num(dd["angle_tolerance_deg"]);
            if (a is null || a < 0 || a >= 90) e.Add("dedupe.angle_tolerance_deg：需為 0 ≤ 值 < 90");
        }
        e.AddRange(CalibrationErrors(c));
        return e;
    }

    static List<string> CalibrationErrors(JsonObject c)
    {
        var e = new List<string>();
        if (c["calibration"] is not JsonObject cal) { e.Add("calibration：缺少（請在 Revit 依序執行「讀取目標吊架族」「校正目標吊架」）"); return e; }
        if (cal["family_file"] is not JsonObject ff || Str(ff["path"]) is null || Str(ff["sha256"]) is null || Num(ff["size"]) is null)
            e.Add("calibration.family_file：缺少或不完整（path、size、sha256）");
        if (Str(cal["project_signature"]) is null) e.Add("calibration.project_signature：缺少");
        if (cal["cross_member_model"] is not JsonObject cm || !(Num(cm["ratio"]) is double r && Math.Abs(r) > 1e-6) || Num(cm["offset_mm"]) is null)
            e.Add("calibration.cross_member_model：缺少或無效（ratio≠0、offset_mm）");
        if (c["family"] is JsonObject fam && (Str(cal["family_name"]) != Str(fam["family_name"]) || Str(cal["type_name"]) != Str(fam["type_name"])))
            e.Add("calibration.family_name：與 family 不符，需重新校正");
        return e;
    }

    public static List<string> ErrorFields(IEnumerable<string> errors) => errors.Select(x => x.Split('：', 2)[0]).ToList();

    /// <summary>驗證並轉成型別化設定；placement=false 只檢查佈置欄位。驗證失敗拋 InvalidDataException（訊息含全部欄位）。</summary>
    public static HangerConfig Read(JsonNode? root, bool placement)
    {
        var errs = placement ? ValidatePlacement(root) : ValidateLayout(root);
        if (errs.Count > 0) throw new InvalidDataException("吊架設定不完整：" + string.Join("; ", errs));
        var c = (JsonObject)root!;
        var nf = (JsonObject)c["near_fitting"]!;
        var cfg = new HangerConfig
        {
            MaxSpacingMm = Num(c["max_spacing_mm"])!.Value,
            StartOffsetMm = Num(c["start_offset_mm"])!.Value,
            EndOffsetMm = Num(c["end_offset_mm"])!.Value,
            ShortRunPolicy = Str(c["short_run_policy"])!,
            SpacingMode = c.ContainsKey("spacing_mode") ? Str(c["spacing_mode"])! : "even",
            NearFittingEnabled = nf["enabled"]!.GetValue<bool>(),
        };
        if (cfg.NearFittingEnabled) cfg.NearFittingMaxDistanceMm = Num(nf["max_distance_mm"]);
        if (!placement) return cfg;
        cfg.SupportedObject = Str(c["supported_object"])!;
        cfg.FamilyName = Str(c["family"]!["family_name"])!;
        cfg.TypeName = Str(c["family"]!["type_name"])!;
        cfg.PlacementMode = Str(c["placement_mode"])!;
        cfg.WidenLeftMm = Num(c["widen_left_mm"])!.Value;
        cfg.WidenRightMm = Num(c["widen_right_mm"])!.Value;
        cfg.InsertionToBearingMm = Num(c["insertion_to_bearing_mm"])!.Value;
        cfg.OrientationCorrectionDeg = Num(c["orientation_correction_deg"])!.Value;
        var cm = c["parameter_map"]!["cross_member_length"]!;
        cfg.CrossMemberLength = new ParamRef(Str(cm["name"])!, Str(cm["scope"])!);
        var rod = c["rod_length"]!;
        cfg.RodMode = Str(rod["mode"])!;
        if (cfg.RodMode == "fixed")
        {
            cfg.RodLengthMm = Num(rod["value_mm"]);
            cfg.RodParam = new ParamRef(Str(rod["param"]!["name"])!, Str(rod["param"]!["scope"])!);
        }
        cfg.DedupeToleranceMm = Num(c["dedupe"]!["tolerance_mm"])!.Value;
        cfg.DedupeAngleToleranceDeg = Num(c["dedupe"]!["angle_tolerance_deg"])!.Value;
        var cal = c["calibration"]!;
        cfg.CalibFilePath = Str(cal["family_file"]!["path"])!;
        cfg.CalibSha256 = Str(cal["family_file"]!["sha256"])!;
        cfg.CalibFileSize = Num(cal["family_file"]!["size"])!.Value;
        cfg.CalibProjectSignature = Str(cal["project_signature"])!;
        cfg.CrossRatio = Num(cal["cross_member_model"]!["ratio"])!.Value;
        cfg.CrossOffsetMm = Num(cal["cross_member_model"]!["offset_mm"])!.Value;
        return cfg;
    }
}

public static class Supports
{
    public const double GeomTolMm = 0.5;   // 幾何判定容差；非規範值
    const double Eps = 1e-9;

    static double R3(double v) => Math.Round(v, 3) + 0.0;
    static double[] P3(double[] p) => p.Select(R3).ToArray();

    public static double Norm360(double d)
    {
        d %= 360.0;
        if (d < 0) d += 360.0;
        d = Math.Round(d, 6);
        return d >= 360.0 ? 0.0 : d + 0.0;
    }

    sealed class Ok
    {
        public string Id = "", StartKind = "free", EndKind = "free";
        public double[] Start = new double[3], End = new double[3];
        public double Len, W, H, Z;
        public double[] U = new double[2];
        public int Order;
    }

    sealed record Iv(double Lo, double Hi, string Id, bool Fwd, double T0);

    static SupportIssue Issue(string level, string code, string pid, double[] loc, string detail) => new(level, code, pid, P3(loc), detail);

    public static LayoutResult Layout(IReadOnlyList<SupportPiece> pieces, IReadOnlyList<SupportLink> links, HangerConfig cfg)
    {
        double s = cfg.MaxSpacingMm, aOff = cfg.StartOffsetMm, bOff = cfg.EndOffsetMm;
        if (!(s > 0) || !(aOff >= 0) || !(bOff >= 0)) throw new InvalidDataException("吊架設定未通過驗證");
        double? nfD = cfg.NearFittingEnabled ? cfg.NearFittingMaxDistanceMm : null;
        var res = new LayoutResult();
        var issues = res.Issues;
        var ok = new Dictionary<string, Ok>();
        for (int idx = 0; idx < pieces.Count; idx++)
        {
            var p = pieces[idx];
            var st = p.Start; var en = p.End;
            var d = new[] { en[0] - st[0], en[1] - st[1], en[2] - st[2] };
            var h = Math.Sqrt(d[0] * d[0] + d[1] * d[1]);
            var L = Math.Sqrt(h * h + d[2] * d[2]);
            var mid = new[] { (st[0] + en[0]) / 2, (st[1] + en[1]) / 2, (st[2] + en[2]) / 2 };
            if (L <= GeomTolMm) { issues.Add(Issue("failed", "zero_length", p.Id, st, "長度為 0，無法放置")); continue; }
            if (Math.Abs(d[2]) > GeomTolMm)
            {
                var code = h <= GeomTolMm ? "vertical" : "sloped";
                issues.Add(Issue("unsupported", code, p.Id, mid, code == "vertical" ? "垂直段 v1 不支援" : $"斜段 v1 不支援（高差 {Math.Abs(d[2]):F1} mm）"));
                continue;
            }
            if (!(p.WidthMm > 0 && double.IsFinite(p.WidthMm) && p.HeightMm > 0 && double.IsFinite(p.HeightMm)))
            { issues.Add(Issue("failed", "invalid_size", p.Id, mid, "橋架寬度/高度無效")); continue; }
            ok[p.Id] = new Ok
            {
                Id = p.Id, Start = (double[])st.Clone(), End = (double[])en.Clone(), Len = h, U = new[] { d[0] / h, d[1] / h },
                W = p.WidthMm, H = p.HeightMm, Z = st[2], StartKind = p.StartKind, EndKind = p.EndKind, Order = idx,
            };
        }

        var parent = ok.Keys.ToDictionary(k => k, k => k);
        string Find(string x) { while (parent[x] != x) { parent[x] = parent[parent[x]]; x = parent[x]; } return x; }
        foreach (var ln in links)
        {
            if (ln.Kind != "union" || !ok.ContainsKey(ln.A) || !ok.ContainsKey(ln.B)) continue;
            var pa = ok[ln.A]; var pb = ok[ln.B];
            var cross = pa.U[0] * pb.U[1] - pa.U[1] * pb.U[0];
            var lat = Math.Abs((pb.Start[0] - pa.Start[0]) * -pa.U[1] + (pb.Start[1] - pa.Start[1]) * pa.U[0]);
            if (Math.Abs(cross) > 1e-4 || lat > GeomTolMm || Math.Abs(pb.Z - pa.Z) > GeomTolMm)
            {
                issues.Add(Issue("warning", "union_not_collinear", pa.Id, pa.End, $"{pa.Id} 與 {pb.Id} 以 union 相接但不共線，分開計算"));
                continue;
            }
            string ra = Find(pa.Id), rb = Find(pb.Id);
            if (ra != rb) { if (ok[ra].Order < ok[rb].Order) parent[rb] = ra; else parent[ra] = rb; }
        }
        var groups = new Dictionary<string, List<string>>();
        foreach (var k in ok.Keys.OrderBy(x => ok[x].Order))
        {
            var r = Find(k);
            if (!groups.TryGetValue(r, out var g)) groups[r] = g = new List<string>();
            g.Add(k);
        }
        var chains = groups.Values.OrderBy(g => ok[g[0]].Order).ToList();

        var supports = res.Supports;
        int ci = 0;
        foreach (var members in chains)
        {
            ci++;
            var cid = $"C{ci:000}";
            var refp = ok[members[0]];
            var u = refp.U; var O = refp.Start;
            double Proj(double[] pt) => (pt[0] - O[0]) * u[0] + (pt[1] - O[1]) * u[1];
            var iv = members.Select(m =>
            {
                var pm = ok[m];
                double t0 = Proj(pm.Start), t1 = Proj(pm.End);
                return new Iv(Math.Min(t0, t1), Math.Max(t0, t1), m, t0 <= t1, t0);
            }).OrderBy(x => x.Lo).ThenBy(x => ok[x.Id].Order).ToList();
            bool bad = false;
            var gaps = new List<(double G0, double G1)>();
            for (int i = 0; i + 1 < iv.Count; i++)
            {
                var x = iv[i]; var y = iv[i + 1];
                if (y.Lo < x.Hi - GeomTolMm) { issues.Add(Issue("failed", "overlap", y.Id, ok[y.Id].Start, $"{x.Id} 與 {y.Id} 重疊，整條連續段不放置")); bad = true; }
                else if (y.Lo - x.Hi > GeomTolMm) gaps.Add((x.Hi, y.Lo));
            }
            if (bad) continue;
            double T0 = iv[0].Lo, T1 = iv.Max(x => x.Hi), L = T1 - T0;
            var first = iv[0];
            var last = iv.OrderByDescending(x => x.Hi).ThenBy(x => ok[x.Id].Order).First();
            var k0 = first.Fwd ? ok[first.Id].StartKind : ok[first.Id].EndKind;
            var k1 = last.Fwd ? ok[last.Id].EndKind : ok[last.Id].StartKind;
            var gapsRel = gaps.Select(g => (G0: g.G0 - T0, G1: g.G1 - T0)).ToList();
            double[] At(double t) => new[] { O[0] + u[0] * (T0 + t), O[1] + u[1] * (T0 + t), refp.Z };
            (double, bool) Snap(double t)
            {
                foreach (var (g0, g1) in gapsRel)
                    if (g0 + Eps < t && t < g1 - Eps) return (t - g0 <= g1 - t ? g0 : g1, true);
                return (t, false);
            }
            var pts = new SortedDictionary<double, string>();
            void Add(double t, string reason)
            {
                var (t2, moved) = Snap(t);
                var key = Math.Round(t2, 6);
                if (moved) issues.Add(Issue("info", "moved_out_of_fitting", first.Id, At(t2), $"{cid} 支撐點落在接頭本體內，已移到接頭邊緣（移動 {Math.Abs(t2 - t):F1} mm）"));
                if (!pts.ContainsKey(key)) pts[key] = reason;
            }
            if (aOff > s + Eps) issues.Add(Issue("warning", "start_offset_exceeds_spacing", first.Id, At(0), "起點內縮大於最大間距（懸臂段）"));
            if (bOff > s + Eps) issues.Add(Issue("warning", "end_offset_exceeds_spacing", last.Id, At(L), "終點內縮大於最大間距（懸臂段）"));
            foreach (var (g0, g1) in gapsRel)
                if (g1 - g0 > s + Eps) issues.Add(Issue("failed", "fitting_gap_exceeds_spacing", first.Id, At(g0), $"{cid} 接頭本體長 {g1 - g0:F1} mm 大於最大間距，無法滿足間距"));
            var usable = L - aOff - bOff;
            if (usable < -Eps)
            {
                if (cfg.ShortRunPolicy == "midpoint")
                {
                    Add(L / 2, "short_midpoint");
                    issues.Add(Issue("warning", "short_run", first.Id, At(L / 2), $"{cid} 長 {L:F1} mm 小於起終點內縮和 {aOff + bOff:F1} mm，置中 1 支"));
                }
                else
                {
                    issues.Add(Issue("failed", "short_run", first.Id, At(L / 2), $"{cid} 長 {L:F1} mm 小於起終點內縮和 {aOff + bOff:F1} mm，未放置"));
                    continue;
                }
            }
            else if (cfg.SpacingMode == "fixed" && usable > Eps)
            {
                int n = (int)Math.Floor(usable / s + 1e-9);
                for (int i = 0; i <= n; i++) Add(aOff + s * i, "spacing");
                var rem = usable - s * n;
                if (rem > 1e-6)
                {
                    double x0 = aOff + s * n, x1 = L - bOff;
                    issues.Add(Issue("pending", "fixed_remainder", last.Id, At(x0), $"{cid} 固定間距 {s:F1} mm 排至終點剩餘 {rem:F3} mm（位置 {x0:F1}–{x1:F1} mm），終點未放支撐，待確認（人工決定是否補點）"));
                }
            }
            else
            {
                int n = usable <= Eps ? 0 : Math.Max(1, (int)Math.Ceiling(usable / s - 1e-9));
                if (n == 0) Add(aOff, "spacing");
                else for (int i = 0; i <= n; i++) Add(aOff + usable * i / n, "spacing");
            }
            if (nfD is double nd)
            {
                var ts0 = pts.Keys.ToList();
                if (k0 == "fitting" && ts0.Count > 0 && ts0[0] > nd + Eps) Add(nd, "near_fitting");
                if (k1 == "fitting" && ts0.Count > 0 && L - ts0[^1] > nd + Eps) Add(L - nd, "near_fitting");
            }
            for (int it = 0; it < 50; it++)
            {
                var ts1 = pts.Keys.ToList();
                int before = ts1.Count;
                for (int i = 0; i + 1 < ts1.Count; i++)
                {
                    double x = ts1[i], y = ts1[i + 1];
                    if (y - x > s + 1e-6)
                    {
                        int k = (int)Math.Ceiling((y - x) / s - 1e-9);
                        for (int j = 1; j < k; j++) Add(x + (y - x) * j / k, "gap_fix");
                    }
                }
                if (pts.Count == before) break;
            }
            var ts = pts.Keys.ToList();
            for (int i = 0; i + 1 < ts.Count; i++)
                if (ts[i + 1] - ts[i] > s + 1e-6) issues.Add(Issue("failed", "spacing_not_met", first.Id, At(ts[i]), $"{cid} 有間距 {ts[i + 1] - ts[i]:F1} mm 超過最大間距"));
            foreach (var t in ts)
            {
                var T = T0 + t;
                var host = iv.FirstOrDefault(x => x.Lo - 1e-6 <= T && T <= x.Hi + 1e-6);
                if (host is null) { issues.Add(Issue("failed", "no_piece", first.Id, At(t), "支撐點不在任何橋架上")); continue; }
                var pm = ok[host.Id];
                var station = host.Fwd ? T - host.T0 : host.T0 - T;
                station = Math.Min(Math.Max(station, 0.0), pm.Len);
                var pt = new[] { pm.Start[0] + pm.U[0] * station, pm.Start[1] + pm.U[1] * station, pm.Z };
                supports.Add(new SupportPoint
                {
                    PieceId = pm.Id, ChainId = cid, StationMm = R3(station), PointMm = P3(pt),
                    DirectionDeg = Norm360(Math.Atan2(pm.U[1], pm.U[0]) * 180.0 / Math.PI),
                    TrayWidthMm = R3(pm.W), TrayHeightMm = R3(pm.H), BearingZMm = R3(pm.Z - pm.H / 2), Reason = pts[t],
                });
            }
        }
        for (int i = 0; i < supports.Count; i++) supports[i].Id = $"H{i + 1:000}";
        return res;
    }

    /// <summary>支撐點（橋架參考座標）→ 族插入點／旋轉／尺寸。insertion_to_bearing：插入點在承托面下方為正。</summary>
    public static Placement Place(SupportPoint sp, HangerConfig cfg)
    {
        var th = sp.DirectionDeg * Math.PI / 180.0;
        double nx = -Math.Sin(th), ny = Math.Cos(th);
        double shift = (cfg.WidenLeftMm - cfg.WidenRightMm) / 2;
        double px = sp.PointMm[0], py = sp.PointMm[1], bz = sp.BearingZMm;
        return new Placement(
            P3(new[] { px + nx * shift, py + ny * shift, bz }),
            P3(new[] { px + nx * shift, py + ny * shift, bz - cfg.InsertionToBearingMm }),
            Norm360(sp.DirectionDeg + cfg.OrientationCorrectionDeg),
            R3(sp.TrayWidthMm + cfg.WidenLeftMm + cfg.WidenRightMm),
            cfg.RodMode == "fixed" && cfg.RodLengthMm is double r ? R3(r) : null);
    }

    public static bool AngleCompatible(double a, double b, double tolDeg)
    {
        var d = Math.Abs((a - b) % 180.0);
        return Math.Min(d, 180.0 - d) <= tolDeg + 1e-9;
    }

    /// <summary>重跑去重：同橋架＋同 station＋方向相容的本工具吊架恰 1 支 → exists；不確定 → pending（待確認）；絕不刪除或修改。</summary>
    public static List<Classification> Classify(IReadOnlyList<PlannedKey> planned, IReadOnlyList<ExistingHanger> existing, double tolMm, double tolDeg)
    {
        var outp = new List<Classification>();
        foreach (var p in planned)
        {
            var tm = existing.Where(e => e.Source == "tool" && e.TrayKey == p.TrayKey && e.StationMm is double st && Math.Abs(st - p.StationMm) <= tolMm + 1e-9).ToList();
            if (tm.Count == 1 && tm[0].DirectionDeg is double dd && AngleCompatible(dd, p.DirectionDeg, tolDeg))
            { outp.Add(new Classification("exists", new List<string> { tm[0].Id }, "本工具已放置")); continue; }
            if (tm.Count > 0)
            { outp.Add(new Classification("pending", tm.Select(e => e.Id).ToList(), tm.Count > 1 ? "同位置有多支本工具吊架" : "同位置本工具吊架方向不相容")); continue; }
            var om = existing.Where(e => e.Source != "tool" && e.PointMm is double[] q &&
                                         Math.Sqrt(Math.Pow(q[0] - p.PointMm[0], 2) + Math.Pow(q[1] - p.PointMm[1], 2)) <= tolMm + 1e-9).ToList();
            if (om.Count > 0) { outp.Add(new Classification("pending", om.Select(e => e.Id).ToList(), "附近有非本工具（手動）吊架")); continue; }
            outp.Add(new Classification("new", new List<string>(), ""));
        }
        return outp;
    }

    /// <summary>中介 JSON（未建接頭）→ pieces/links；端點在 elbow/tee/cross/unsupported 接點者為 fitting 端。</summary>
    public static (List<SupportPiece> Pieces, List<SupportLink> Links) FromModel(TrayModel m)
    {
        var pieces = m.Segments.Select(s => new SupportPiece
        {
            Id = s.Id, Start = (double[])s.Start.Clone(), End = (double[])s.End.Clone(), WidthMm = m.Tray.WidthMm, HeightMm = m.Tray.HeightMm,
        }).ToList();
        var byId = pieces.ToDictionary(p => p.Id);
        var links = new List<SupportLink>();
        foreach (var j in m.Joints)
        {
            if (j.Kind == "union" && j.Segments.Count == 2) { links.Add(new SupportLink(j.Segments[0], j.Segments[1], "union")); continue; }
            foreach (var sid in j.Segments)
            {
                if (!byId.TryGetValue(sid, out var p)) continue;
                if (Dist(p.Start, j.Point) <= GeomTolMm) p.StartKind = "fitting";
                if (Dist(p.End, j.Point) <= GeomTolMm) p.EndKind = "fitting";
            }
        }
        return (pieces, links);
    }

    static double Dist(double[] a, double[] b) => Math.Sqrt(Math.Pow(a[0] - b[0], 2) + Math.Pow(a[1] - b[1], 2) + Math.Pow(a[2] - b[2], 2));
}
