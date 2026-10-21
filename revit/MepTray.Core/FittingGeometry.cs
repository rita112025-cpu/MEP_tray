namespace MepTray.Core;

// 彎頭／三通中點吊架的接頭幾何（純邏輯，mm；不依賴 Revit）。
// Revit 端把 connector（Origin、對外軸向 CoordinateSystem.BasisZ、寬高）與 Solid 圓弧邊讀成下列資料，由本類別重建中心線並交叉驗證。
// 實機探查（Revit 2025.5，M_梯水平彎曲／M_梯水平 T 接頭）：connector 的 BasisZ 是對外方向；彎頭內外緣圓弧半徑的平均 = 中心線半徑；
// 轉角點（兩條對外軸向線的交點）= 彎頭 Location 點；三通主幹 = 兩個軸向相反的 connector，交點 = 其中點。
// 任何一項驗證不過 → 不猜，回傳 FallbackReason，該接頭退回只放兩側。

/// <summary>接頭的一個連接口：Origin（mm）、對外軸向（單位向量，由 Connector.CoordinateSystem.BasisZ 取得）、寬高（mm）。</summary>
public sealed record FittingConnector(double[] OriginMm, double[] OutwardAxis, double WidthMm, double HeightMm);

/// <summary>接頭幾何裡的一條圓弧邊：圓心（mm）、半徑（mm）、掃過角（度，取絕對值）、法向。</summary>
public sealed record FittingArc(double[] CenterMm, double RadiusMm, double SweepDeg, double[] Normal);

public static class FittingGeometry
{
    // 以下都是幾何判定容差，不是工程或規範數值。
    public const double CenterTolMm = 1.0;      // 由連接口推得的圓心與幾何圓弧圓心的容許差
    public const double SweepTolDeg = 0.1;      // 圓弧掃過角與連接口夾角的容許差
    public const double SizeTolMm = 0.5;        // 寬高、高度（Z）一致的容許差
    const double AxisTol = 1e-3;                // 軸向分量判定
    const double ParallelCos = 0.999;           // 三通主幹：兩軸向相反的判定（約 2.6°）

    /// <summary>族的 Part Type 列舉名稱 → 種類。只收實機探查過的 Ladder 系列；其他類型回傳 null（維持現狀，不處理）。</summary>
    public static string? KindOf(string? partTypeEnumName) => partTypeEnumName switch
    {
        "LadderCableTrayElbow" => "elbow",
        "LadderCableTrayTee" => "tee",
        _ => null,
    };

    static double Deg(double rad) => rad * 180.0 / Math.PI;
    static double NormDeg180(double d)
    {
        d %= 360.0;
        if (d > 180.0) d -= 360.0;
        if (d <= -180.0) d += 360.0;
        return d;
    }

    static SupportFitting Fallback(string id, string kind, IReadOnlyList<FittingConnector> conns, string reason)
    {
        var loc = conns.Count == 0 ? new[] { 0.0, 0.0, 0.0 }
            : new[] { conns.Average(c => c.OriginMm[0]), conns.Average(c => c.OriginMm[1]), conns.Average(c => c.OriginMm[2]) };
        return new SupportFitting { Id = id, Kind = kind, LocationMm = loc, FallbackReason = reason };
    }

    static double[]? Unit2(double[] a)
    {
        var n = Math.Sqrt(a[0] * a[0] + a[1] * a[1]);
        return n < 1e-9 ? null : new[] { a[0] / n, a[1] / n };
    }

    static string? CommonChecks(IReadOnlyList<FittingConnector> conns)
    {
        foreach (var c in conns)
            if (c.OriginMm.Length != 3 || c.OutwardAxis.Length != 3 || !c.OriginMm.All(double.IsFinite) || !c.OutwardAxis.All(double.IsFinite))
                return "連接口座標或軸向無效";
        if (conns.Any(c => Math.Abs(c.OutwardAxis[2]) > AxisTol)) return "接頭不是水平（連接口軸向有垂直分量），v1 不處理";
        if (conns.Any(c => Unit2(c.OutwardAxis) is null)) return "連接口軸向無法取得水平方向";
        if (conns.Max(c => c.OriginMm[2]) - conns.Min(c => c.OriginMm[2]) > SizeTolMm) return "連接口高度（Z）不一致";
        return null;
    }

    /// <summary>彎頭：由兩個連接口求轉角點與轉向角，由圓弧邊求中心線半徑，再以三項交叉驗證（掃過角、內外緣半徑差 = 橋架寬、圓心）。</summary>
    public static SupportFitting Elbow(string id, IReadOnlyList<FittingConnector> conns, IReadOnlyList<FittingArc> arcs)
    {
        if (conns.Count != 2) return Fallback(id, "elbow", conns, $"連接口數量為 {conns.Count}，彎頭需 2 個");
        if (CommonChecks(conns) is string common) return Fallback(id, "elbow", conns, common);
        var c0 = conns[0]; var c1 = conns[1];
        if (Math.Abs(c0.WidthMm - c1.WidthMm) > SizeTolMm || Math.Abs(c0.HeightMm - c1.HeightMm) > SizeTolMm)
            return Fallback(id, "elbow", conns, "兩個連接口的寬高不同");
        var u0 = Unit2(c0.OutwardAxis)!; var u1 = Unit2(c1.OutwardAxis)!;
        var between = Deg(Math.Acos(Math.Clamp(u0[0] * u1[0] + u0[1] * u1[1], -1.0, 1.0)));   // 兩條對外軸向的夾角
        if (between <= SweepTolDeg || between >= 180.0 - SweepTolDeg) return Fallback(id, "elbow", conns, "兩個連接口軸向共線，不是彎頭");
        double delta = 180.0 - between;                                                        // 轉向角
        // 轉角點：o0 − s0·u0 = o1 − s1·u1
        double ax = -u0[0], ay = -u0[1], bx = -u1[0], by = -u1[1];
        double dx = c1.OriginMm[0] - c0.OriginMm[0], dy = c1.OriginMm[1] - c0.OriginMm[1];
        double det = -ax * by + bx * ay;
        if (Math.Abs(det) < 1e-9) return Fallback(id, "elbow", conns, "無法求出轉角點");
        double s0 = (dx * -by + bx * dy) / det, s1 = (ax * dy - ay * dx) / det;
        if (s0 <= 0 || s1 <= 0) return Fallback(id, "elbow", conns, "轉角點不在兩個連接口的內側");
        var corner = new[] { c0.OriginMm[0] + s0 * ax, c0.OriginMm[1] + s0 * ay };
        var flat = arcs.Where(a => a.Normal.Length == 3 && Math.Abs(Math.Abs(a.Normal[2]) - 1.0) < 1e-3 && double.IsFinite(a.RadiusMm) && a.RadiusMm > 0
                                   && a.CenterMm.Length == 3 && a.CenterMm.All(double.IsFinite) && double.IsFinite(a.SweepDeg)).ToList();
        if (flat.Count < 2) return Fallback(id, "elbow", conns, $"幾何裡找不到足夠的水平圓弧邊（{flat.Count} 條）");
        var c = flat[0].CenterMm;
        var far = flat.Max(a => Math.Sqrt(Math.Pow(a.CenterMm[0] - c[0], 2) + Math.Pow(a.CenterMm[1] - c[1], 2)));
        if (far > CenterTolMm) return Fallback(id, "elbow", conns, $"圓弧邊的圓心不一致（最大相差 {far:F2} mm）");
        var badSweep = flat.FirstOrDefault(a => Math.Abs(a.SweepDeg - delta) > SweepTolDeg);
        if (badSweep is not null) return Fallback(id, "elbow", conns, $"圓弧掃過角 {badSweep.SweepDeg:F2}° 與連接口夾角推得的轉向角 {delta:F2}° 不符");
        double rmin = flat.Min(a => a.RadiusMm), rmax = flat.Max(a => a.RadiusMm);
        if (rmax - rmin < 1e-6) return Fallback(id, "elbow", conns, "圓弧邊只有一種半徑，無法求中心線");
        if (Math.Abs((rmax - rmin) - c0.WidthMm) > CenterTolMm)
            return Fallback(id, "elbow", conns, $"內外緣半徑差 {rmax - rmin:F2} mm 與橋架寬度 {c0.WidthMm:F2} mm 不符");
        double r = (rmin + rmax) / 2;
        var bis = Unit2(new[] { u0[0] + u1[0], u0[1] + u1[1] })!;
        double dist = r / Math.Cos(delta / 2 * Math.PI / 180.0);
        var expect = new[] { corner[0] + bis[0] * dist, corner[1] + bis[1] * dist };
        var gap = Math.Sqrt(Math.Pow(expect[0] - c[0], 2) + Math.Pow(expect[1] - c[1], 2));
        if (gap > CenterTolMm) return Fallback(id, "elbow", conns, $"由連接口推得的圓心與幾何圓弧圓心相差 {gap:F2} mm");
        double t = r * Math.Tan(delta / 2 * Math.PI / 180.0);
        double a0 = Deg(Math.Atan2(corner[1] + u0[1] * t - expect[1], corner[0] + u0[0] * t - expect[0]));
        double a1 = Deg(Math.Atan2(corner[1] + u1[1] * t - expect[1], corner[0] + u1[0] * t - expect[0]));
        double sweep = NormDeg180(a1 - a0), start = a0;
        if (sweep < 0) { sweep = -sweep; start = a1; }                                          // 一律以逆時針（正掃過角）表示
        return new SupportFitting
        {
            Id = id, Kind = "elbow", Center = new[] { expect[0], expect[1], c0.OriginMm[2] }, RadiusMm = r,
            StartAngleDeg = NormDeg180(start), SweepDeg = sweep, WidthMm = c0.WidthMm, HeightMm = c0.HeightMm,
        };
    }

    /// <summary>三通：主幹 = 唯一一對軸向相反的連接口，中點 = 兩者 Origin 的中點，方向 = 主幹方向。</summary>
    public static SupportFitting Tee(string id, IReadOnlyList<FittingConnector> conns)
    {
        if (conns.Count != 3) return Fallback(id, "tee", conns, $"連接口數量為 {conns.Count}，三通需 3 個");
        if (CommonChecks(conns) is string common) return Fallback(id, "tee", conns, common);
        var pairs = new List<(int I, int J)>();
        for (int i = 0; i < 3; i++)
            for (int j = i + 1; j < 3; j++)
            {
                var a = Unit2(conns[i].OutwardAxis)!; var b = Unit2(conns[j].OutwardAxis)!;
                if (a[0] * b[0] + a[1] * b[1] <= -ParallelCos) pairs.Add((i, j));
            }
        if (pairs.Count != 1) return Fallback(id, "tee", conns, $"找不到唯一的主幹（軸向相反的連接口有 {pairs.Count} 對）");
        var (mi, mj) = pairs[0];
        var m0 = conns[mi]; var m1 = conns[mj];
        if (Math.Abs(m0.WidthMm - m1.WidthMm) > SizeTolMm || Math.Abs(m0.HeightMm - m1.HeightMm) > SizeTolMm)
            return Fallback(id, "tee", conns, "主幹兩個連接口的寬高不同");
        var u = Unit2(m0.OutwardAxis)!;
        double lateral = Math.Abs((m1.OriginMm[0] - m0.OriginMm[0]) * u[1] - (m1.OriginMm[1] - m0.OriginMm[1]) * u[0]);
        if (lateral > SizeTolMm) return Fallback(id, "tee", conns, $"主幹兩個連接口不共線（偏 {lateral:F2} mm）");
        return new SupportFitting
        {
            Id = id, Kind = "tee",
            Point = new[] { (m0.OriginMm[0] + m1.OriginMm[0]) / 2, (m0.OriginMm[1] + m1.OriginMm[1]) / 2, m0.OriginMm[2] },
            MainDirectionDeg = Deg(Math.Atan2(u[1], u[0])), WidthMm = m0.WidthMm, HeightMm = m0.HeightMm,
        };
    }
}
