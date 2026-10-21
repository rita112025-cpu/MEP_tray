using System.Text.Json.Nodes;
using MepTray.Core;

// 彎頭／三通接頭幾何（FittingGeometry）的測試。樣本數字取自 2026-10-20 在 Revit 2025.5 實機探查的報告
// （M_梯水平彎曲／標準、M_梯水平 T 接頭／300mm 半徑；橋架 300x100，中心線 Z = −1050）。

public static class FittingTests
{
    static FittingConnector C(double x, double y, double ax, double ay, double w = 300, double h = 100, double z = -1050) =>
        new(new[] { x, y, z }, new[] { ax, ay, 0.0 }, w, h);

    /// <summary>實機圓弧邊：內緣 300／325.4、外緣 574.6／600，上下緣（Z −1100、−1000）各一組，法向 ±Z。</summary>
    static List<FittingArc> Arcs(double cx, double cy, double sweep, double[]? radii = null)
    {
        var l = new List<FittingArc>();
        foreach (var r in radii ?? new[] { 300.0, 325.4, 574.6, 600.0 })
        {
            l.Add(new FittingArc(new[] { cx, cy, -1000.0 }, r, sweep, new[] { 0.0, 0, 1 }));
            l.Add(new FittingArc(new[] { cx, cy, -1100.0 }, r, sweep, new[] { 0.0, 0, -1 }));
        }
        return l;
    }

    static bool Near(double? a, double b, double tol = 1e-3) => a is double x && Math.Abs(x - b) <= tol;
    static bool ElbowIs(SupportFitting f, double cx, double cy, double r, double start, double sweep) =>
        f.FallbackReason is null && f.Kind == "elbow" && f.Center is { } c && Near(c[0], cx) && Near(c[1], cy) && Near(c[2], -1050)
        && Near(f.RadiusMm, r) && Near(f.StartAngleDeg, start) && Near(f.SweepDeg, sweep) && Near(f.WidthMm, 300) && Near(f.HeightMm, 100);

    public static void Run(Action<bool, string> Check)
    {
        Check(FittingGeometry.KindOf("LadderCableTrayElbow") == "elbow" && FittingGeometry.KindOf("LadderCableTrayTee") == "tee"
              && FittingGeometry.KindOf("LadderCableTrayUnion") is null && FittingGeometry.KindOf("ChannelCableTrayElbow") is null
              && FittingGeometry.KindOf("Elbow") is null && FittingGeometry.KindOf(null) is null, "接頭辨識：只認實機探查過的 Ladder 彎頭／三通，其餘不處理");

        // 彎頭 #811194：90°（connector 0 對外 +Y、connector 1 對外 −X）
        var e90 = FittingGeometry.Elbow("811194", new[] { C(3992.471, 9605.553, 0, 1), C(3462.471, 9075.553, -1, 0) }, Arcs(3542.471, 9525.553, 90));
        Check(ElbowIs(e90, 3542.471, 9525.553, 450, -90, 90), $"彎頭 90°：中心線半徑 450、圓心與實機圓弧一致、逆時針表示（{e90.FallbackReason}）");
        // 彎頭 #811296：60°（connector 0 對外 −X、connector 1 對外 (0.5, 0.866)）
        var e60 = FittingGeometry.Elbow("811296", new[] { C(2018.637, 10984.74, -1, 0), C(2528.349, 11279.022, 0.5, 0.8660254037844386) }, Arcs(2098.637, 11434.74, 60));
        Check(ElbowIs(e60, 2098.637, 11434.74, 450, -90, 60), $"彎頭 60°：半徑 450、掃過 60°（{e60.FallbackReason}）");
        // 彎頭 #811252：另一個 90°（旋轉 270°）
        var e252 = FittingGeometry.Elbow("811252", new[] { C(2696.437, 5895.982, 0, 1), C(3226.437, 5365.982, 1, 0) }, Arcs(3146.437, 5815.982, 90));
        Check(ElbowIs(e252, 3146.437, 5815.982, 450, 180, 90), $"彎頭 90°（另一位置與旋轉）：圓心 (3146.437, 5815.982)（{e252.FallbackReason}）");
        var swapped = FittingGeometry.Elbow("811194s", new[] { C(3462.471, 9075.553, -1, 0), C(3992.471, 9605.553, 0, 1) }, Arcs(3542.471, 9525.553, 90));
        Check(ElbowIs(swapped, 3542.471, 9525.553, 450, -90, 90), "彎頭：connector 順序互換結果相同（不依賴順序）");

        // 與 Layout 串接：中點與方向（彎頭 90° → 45°、60° → 30°；三通 → 主幹方向）
        var cfg = new HangerConfig { MaxSpacingMm = 1500, StartOffsetMm = 0, EndOffsetMm = 0, ShortRunPolicy = "skip", FittingSupportEnabled = true, FittingSupportKinds = new() { "elbow", "tee" } };
        var lay = Supports.Layout(new List<SupportPiece>(), new List<SupportLink>(), cfg, new[] { e90, e60 });
        var m90 = lay.Supports[0]; var m60 = lay.Supports[1];
        Check(lay.Issues.Count == 0 && m90.Reason == "elbow_mid" && m90.FittingId == "811194" && Near(m90.PointMm[0], 3860.669) && Near(m90.PointMm[1], 9207.355)
              && Near(m90.DirectionDeg, 45) && Near(m90.BearingZMm, -1100) && Near(m90.StationMm, 353.429), "彎頭 90° 中點 (3860.669, 9207.355)、方向 45°、承托面 −1100、弧長一半 353.429");
        Check(Near(m60.PointMm[0], 2323.637) && Near(m60.PointMm[1], 11045.029) && Near(m60.DirectionDeg, 30) && Near(m60.StationMm, 235.619), "彎頭 60° 中點 (2323.637, 11045.029)、方向 30°");

        // 三通 #811279：兩種 connector 順序，主幹都是 (±X) 那一對
        var t1 = FittingGeometry.Tee("811279", new[] { C(1511.28, 9605.553, 0, 1), C(2041.28, 9075.553, 1, 0), C(981.28, 9075.553, -1, 0) });
        var t2 = FittingGeometry.Tee("811279", new[] { C(2041.28, 9075.553, 1, 0), C(1511.28, 9605.553, 0, 1), C(981.28, 9075.553, -1, 0) });
        bool teeOk(SupportFitting t) => t.FallbackReason is null && t.Kind == "tee" && t.Point is { } p && Near(p[0], 1511.28) && Near(p[1], 9075.553) && Near(p[2], -1050)
                                         && Near(t.MainDirectionDeg, 0) && Near(t.WidthMm, 300) && Near(t.HeightMm, 100);
        Check(teeOk(t1) && teeOk(t2), "三通：主幹 = 軸向相反的一對，中點 (1511.28, 9075.553)，順序不影響");
        var tl = Supports.Layout(new List<SupportPiece>(), new List<SupportLink>(), cfg, new[] { t1 });
        Check(tl.Supports.Single().Reason == "tee_mid" && Near(tl.Supports[0].DirectionDeg, 0) && Near(tl.Supports[0].BearingZMm, -1100), "三通中點吊架：方向取主幹、承托面 −1100");
        var tVertical = FittingGeometry.Tee("t", new[] { C(0, 0, 0, 1), C(530, -530, 1, 0), C(-530, -400, -1, 0) });
        Check(tVertical.FallbackReason is not null && tVertical.FallbackReason.Contains("不共線"), "三通：主幹兩連接口不共線 → 退回並說明");

        // 退回：不猜
        string? Why(SupportFitting f) => f.FallbackReason;
        var cs90 = new[] { C(3992.471, 9605.553, 0, 1), C(3462.471, 9075.553, -1, 0) };
        Check(Why(FittingGeometry.Elbow("x", cs90, new List<FittingArc>()))!.Contains("水平圓弧邊"), "彎頭：沒有圓弧邊 → 退回");
        Check(Why(FittingGeometry.Elbow("x", cs90, Arcs(3547.471, 9525.553, 90)))!.Contains("相差"), "彎頭：連接口推得的圓心與圓弧圓心差 5 mm → 退回並點出差距");
        Check(Why(FittingGeometry.Elbow("x", cs90, Arcs(3542.471, 9525.553, 45)))!.Contains("掃過角"), "彎頭：圓弧掃過角與連接口夾角不符 → 退回");
        Check(Why(FittingGeometry.Elbow("x", cs90, Arcs(3542.471, 9525.553, 90, new[] { 300.0, 700.0 })))!.Contains("內外緣半徑差"), "彎頭：內外緣半徑差不等於橋架寬 → 退回");
        Check(Why(FittingGeometry.Elbow("x", cs90, Arcs(3542.471, 9525.553, 90, new[] { 450.0 })))!.Contains("只有一種半徑"), "彎頭：只有一種半徑 → 退回");
        Check(Why(FittingGeometry.Elbow("x", cs90.Take(1).ToArray(), Arcs(3542.471, 9525.553, 90)))!.Contains("需 2 個"), "彎頭：連接口數量不對 → 退回");
        Check(Why(FittingGeometry.Elbow("x", new[] { C(0, 0, 1, 0), C(1000, 0, -1, 0) }, Arcs(0, 0, 90)))!.Contains("共線"), "彎頭：兩連接口軸向相反（直通）→ 退回");
        Check(Why(FittingGeometry.Elbow("x", new[] { cs90[0], new FittingConnector(new[] { 3462.471, 9075.553, -1050.0 }, new[] { 0.0, 0, 1 }, 300, 100) }, Arcs(3542.471, 9525.553, 90)))!.Contains("不是水平"), "彎頭：軸向有垂直分量 → 退回（v1 不處理垂直彎頭）");
        Check(Why(FittingGeometry.Elbow("x", new[] { cs90[0], C(3462.471, 9075.553, -1, 0, 400) }, Arcs(3542.471, 9525.553, 90)))!.Contains("寬高不同"), "彎頭：兩連接口寬度不同 → 退回");
        Check(Why(FittingGeometry.Elbow("x", new[] { cs90[0], C(3462.471, 9075.553, -1, 0, z: -1040) }, Arcs(3542.471, 9525.553, 90)))!.Contains("高度"), "彎頭：兩連接口高度不同 → 退回");
        Check(Why(FittingGeometry.Tee("x", new[] { C(0, 0, 0, 1), C(1, 0, 1, 0) }))!.Contains("需 3 個"), "三通：連接口數量不對 → 退回");
        Check(Why(FittingGeometry.Tee("x", new[] { C(0, 0, 0, 1), C(1000, 0, 1, 0), C(-1000, 500, 0.7071067811865476, 0.7071067811865476) }))!.Contains("唯一的主幹"), "三通：沒有軸向相反的一對 → 退回");
        var bad = FittingGeometry.Elbow("x", cs90, new List<FittingArc>());
        var fl = Supports.Layout(new List<SupportPiece>(), new List<SupportLink>(), cfg, new[] { bad });
        Check(fl.Supports.Count == 0 && fl.Issues.Single().Code == "fitting_mid_fallback" && fl.Issues[0].Detail.Contains("退回只放兩側") && fl.Issues[0].Detail.Contains("水平圓弧邊")
              && Near(fl.Issues[0].LocationMm[0], 3727.471, 0.01), "退回的接頭：不放中點，列出 fitting_mid_fallback 與原因，位置取連接口平均");
    }
}
