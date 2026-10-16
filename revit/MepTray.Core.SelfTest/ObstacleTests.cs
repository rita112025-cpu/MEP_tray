using MepTray.Core;

/// <summary>Obstacles 幾何核心（不需 Revit）的自我測試；fixturePath 非 null 時寫出 Core 產生的障礙物 JSON 供 Python 驗證。</summary>
static class ObstacleTests
{
    static bool Near(double a, double b, double tol = 1e-6) => Math.Abs(a - b) <= tol;
    static bool Near3(double[] a, double[] b, double tol = 1e-6) => Enumerable.Range(0, 3).All(i => Near(a[i], b[i], tol));

    static CoordinateSystemDto Cs(double deg, double[] origin)
    {
        var th = deg * Math.PI / 180.0;
        return new CoordinateSystemDto
        {
            Basis = "INTERNAL_ORIGIN", Origin = origin, RotationDeg = deg,
            AxisX = new[] { Math.Cos(th), Math.Sin(th), 0.0 }, AxisY = new[] { -Math.Sin(th), Math.Cos(th), 0.0 },
        };
    }

    /// <summary>匯入器方向：本地公尺 → ToBasis（mm）→ 英呎 → place。</summary>
    static double[] LocalMToInternalFt(CoordinateSystemDto cs, Affine place, double[] m)
    {
        var b = ModelLoader.ToBasis(cs, new[] { m[0] * 1000, m[1] * 1000, m[2] * 1000 });
        return place.Apply(new[] { Units.MmToFeet(b[0]), Units.MmToFeet(b[1]), Units.MmToFeet(b[2]) });
    }

    public static void Run(Action<bool, string> Check, string? fixturePath)
    {
        var room = new[] { 10.0, 8.0, 4.0 };
        var places = new (string name, Affine a)[]
        {
            ("INTERNAL_ORIGIN(恆等)", Affine.Identity),
            ("PBP(平移)", Affine.RotZThenTranslate(0, 12.5, -40.0, 3.0)),
            ("SHARED(旋轉+平移)", Affine.RotZThenTranslate(-23.5, -150.0, 87.0, -10.0)),
        };

        // Affine 反函數
        foreach (var (pn, a) in places)
        {
            var p = new[] { 3.3, -7.1, 2.2 };
            Check(Near3(a.Inverse().Apply(a.Apply(p)), p, 1e-9), $"Affine.Inverse 往返: {pn}");
        }

        // FromBasis 是 ToBasis 的反函數；InternalFtToLocalM 與匯入器方向互逆
        foreach (var deg in new[] { 0.0, 30.0, 90.0, -45.0, 180.0 })
        {
            var cs = Cs(deg, new[] { 2000.0, -1000.0, 500.0 });
            var p = new[] { 1234.5, 678.9, 300.0 };
            Check(Near3(Obstacles.FromBasis(cs, ModelLoader.ToBasis(cs, p)), p, 1e-9), $"FromBasis(ToBasis(p)) = p, rotation {deg}°");
            foreach (var (pn, a) in places)
            {
                var m = new[] { 4.2, 1.7, 2.9 };
                var back = Obstacles.InternalFtToLocalM(cs, a.Inverse(), LocalMToInternalFt(cs, a, m));
                Check(Near3(back, m, 1e-9), $"內部英呎 → 本地公尺 往返: {pn}, rotation {deg}°");
            }
        }

        // 盒子：rotation 0/90 精確還原；30° 為外接盒（包含原盒、且更大）
        foreach (var deg in new[] { 0.0, 30.0, 90.0 })
        foreach (var (pn, a) in places)
        {
            var cs = Cs(deg, new[] { 1500.0, 2500.0, 0.0 });
            double[] lo = { 2.0, 3.0, 1.0 }, hi = { 2.5, 5.0, 1.3 };
            var (flo, fhi) = Obstacles.TransformBox(p => LocalMToInternalFt(cs, a, p), lo, hi);
            var r = Obstacles.Build(new[] { new RawObstacle { Name = "B", Kind = "duct", LoFt = flo, HiFt = fhi } }, cs, a, room);
            var o = r.Obstacles.Single();
            var axisAligned = (deg % 90 == 0) && pn != "SHARED(旋轉+平移)";
            if (axisAligned) Check(Near3(o.Lo, lo, 2e-4) && Near3(o.Hi, hi, 2e-4), $"8 角盒還原: rotation {deg}°, {pn}");
            else Check(Enumerable.Range(0, 3).All(i => o.Lo[i] <= lo[i] + 1e-9 && o.Hi[i] >= hi[i] - 1e-9)
                       && (o.Hi[0] - o.Lo[0]) > 0.5 + 1e-3, $"8 角盒為保守外接盒: rotation {deg}°, {pn}");
        }

        var cs0 = Cs(0, new[] { 0.0, 0.0, 0.0 });
        var id = Affine.Identity;
        double[] Ft(double x, double y, double z) => new[] { Units.MmToFeet(x * 1000), Units.MmToFeet(y * 1000), Units.MmToFeet(z * 1000) };

        // 裁切
        var clip = Obstacles.Build(new[]
        {
            new RawObstacle { Name = "部分在外", Kind = "structure", LoFt = Ft(-1, 2, 3.5), HiFt = Ft(3, 2.4, 5) },
            new RawObstacle { Name = "完全在外", Kind = "water", LoFt = Ft(11, 1, 1), HiFt = Ft(12, 2, 2) },
            new RawObstacle { Name = "只接觸牆面", Kind = "water", LoFt = Ft(10, 1, 1), HiFt = Ft(11, 2, 2) },
        }, cs0, id, room);
        var c0 = clip.Obstacles.Single();
        Check(Near3(c0.Lo, new[] { 0, 2, 3.5 }, 2e-4) && Near3(c0.Hi, new[] { 3, 2.4, 4 }, 2e-4), "部分在外的盒子被裁到房間內");
        Check(clip.Clipped == 1 && clip.Dropped.Count(d => d.Reason == "outside_room") == 2, "完全在外/只接觸者列入 dropped(outside_room)");

        // 斜管分段：10 m 長 45° 斜管 → 多段，每段體積遠小於整體 AABB
        var diag = new RawObstacle { Name = "斜管", Kind = "heat", StartFt = Ft(1, 1, 2), EndFt = Ft(8, 8, 2), RadiusFt = Units.MmToFeet(50), InsulationMm = 25 };
        var sp = Obstacles.Build(new[] { diag }, cs0, id, room);
        double Vol(ObstacleOut o) => (o.Hi[0] - o.Lo[0]) * (o.Hi[1] - o.Lo[1]) * (o.Hi[2] - o.Lo[2]);
        var whole = (7 + 0.15) * (7 + 0.15) * 0.15;
        Check(sp.Obstacles.Count == 10 && sp.Split == 1, $"斜管依 1 m 分為 10 段（實得 {sp.Obstacles.Count}）");
        Check(sp.Obstacles.Sum(Vol) < whole * 0.25, "分段總體積 < 整體 AABB 的 25%");
        Check(sp.Obstacles.All(o => Near(o.Hi[2] - o.Lo[2], 0.15, 2e-4)), "半徑 50 mm + 保溫 25 mm → 高度 150 mm");
        Check(sp.Obstacles.Any(o => o.Lo[0] <= 1 - 0.075 + 1e-4) && sp.Obstacles.Any(o => o.Hi[0] >= 8 + 0.075 - 1e-4), "分段覆蓋兩端點（含半徑）");
        Check(sp.Obstacles[0].Name.EndsWith("/10"), "分段名稱帶 #k/n");
        var axial = Obstacles.Build(new[] { new RawObstacle { Name = "直管", Kind = "water", StartFt = Ft(0.5, 1, 3), EndFt = Ft(9.5, 1, 3), RadiusFt = Units.MmToFeet(40) } }, cs0, id, room);
        Check(axial.Obstacles.Count == 1 && axial.Split == 0, "軸向管不分段");
        var many = Obstacles.Build(new[] { new RawObstacle { Name = "超長斜管", Kind = "water", StartFt = Ft(0, 0, 0.5), EndFt = Ft(10, 8, 3.5), RadiusFt = Units.MmToFeet(30) } },
                                   cs0, id, room, new ExportOptions { MaxPieceM = 0.1 });
        Check(many.Obstacles.Count == 20, "單一元件分段數受 MaxPiecesPerElement(20) 限制");

        // 200 上限、決定性優先序、dropped 報告
        var raws = new List<RawObstacle>();
        for (int i = 0; i < 150; i++) raws.Add(new RawObstacle { Name = $"other{i:D3}", Kind = "other", LoFt = Ft(0.01 * i, 0, 0), HiFt = Ft(0.01 * i + 0.005, 1, 1) });
        for (int i = 0; i < 100; i++) raws.Add(new RawObstacle { Name = $"beam{i:D3}", Kind = "structure", LoFt = Ft(0.01 * i, 2, 3), HiFt = Ft(0.01 * i + 0.005, 3, 3.5) });
        var cap = Obstacles.Build(raws, cs0, id, room);
        Check(cap.Obstacles.Count == 200 && cap.Dropped.Count(d => d.Reason == "over_count_limit") == 50, "超過 200 個時保留 200、其餘列入 dropped");
        Check(cap.Obstacles.Count(o => o.Kind == "structure") == 100, "結構優先保留");
        var cap2 = Obstacles.Build(Enumerable.Reverse(raws), cs0, id, room);
        Check(Obstacles.ToJson(cap.Obstacles) == Obstacles.ToJson(cap2.Obstacles), "輸入順序不同時輸出完全相同（決定性）");
        Check(Obstacles.Validate(cap.Obstacles, room).Count == 0, "上限結果通過 §2 驗證");
        var tiny = Obstacles.Build(raws, cs0, id, room, new ExportOptions { MaxBytes = 2000 });
        Check(Obstacles.Utf8Bytes(tiny.Obstacles) <= 2000 && tiny.Dropped.Any(d => d.Reason == "over_size_limit"), "位元組上限生效");

        // 名稱清理
        var longName = new string('管', 150) + "\u0007\n";
        var sn = Obstacles.SanitizeName("  " + longName);
        Check(sn.EnumerateRunes().Count() == 100 && !sn.Any(char.IsControl), "名稱截到 100 字元且無控制字元");
        Check(Obstacles.SanitizeName("\t\u0001 ") == "obstacle", "空白名稱改用預設");
        var emoji = Obstacles.SanitizeName(string.Concat(Enumerable.Repeat("😀", 120)));
        Check(emoji.EnumerateRunes().Count() == 100 && !char.IsHighSurrogate(emoji[^1]), "以 code point 截斷，不切斷代理對");
        var pn2 = Obstacles.Build(new[] { new RawObstacle { Name = longName, Kind = "bogus", StartFt = Ft(1, 1, 1), EndFt = Ft(4, 3, 2), RadiusFt = 0.1 } }, cs0, id, room);
        Check(pn2.Obstacles.All(o => o.Name.EnumerateRunes().Count() <= 100 && o.Kind == "other"), "分段名稱仍 ≤100 字元；未知 kind → other");
        Check(Obstacles.Build(new[] { new RawObstacle { Name = "x" } }, cs0, id, room).Dropped.Single().Reason == "invalid_geometry", "缺幾何 → dropped(invalid_geometry)");

        // 輸出 fixture：旋轉 30° + 共用座標變換的綜合案例，交給 Python 端既有驗證器
        if (fixturePath != null)
        {
            var cs = Cs(30, new[] { 1500.0, 2500.0, 0.0 });
            var a = places[2].a;
            double[] F(double x, double y, double z) => LocalMToInternalFt(cs, a, new[] { x, y, z });
            var fx = new List<RawObstacle>
            {
                new() { Name = "冷卻水主管", Kind = "water", StartFt = F(0.5, 1, 3), EndFt = F(9.5, 1, 3), RadiusFt = Units.MmToFeet(75), InsulationMm = 25 },
                new() { Name = "熱水斜管", Kind = "heat_bare", StartFt = F(1, 1, 2), EndFt = F(8, 7, 2.5), RadiusFt = Units.MmToFeet(40) },
                new() { Name = "梁 B1", Kind = "structure", LoFt = F(-1, 4, 3.4), HiFt = F(11, 4.4, 4.5) },
                new() { Name = "風管\u0007" + new string('x', 120), Kind = "duct", LoFt = F(6, 2, 2.5), HiFt = F(6.6, 6, 3) },
                new() { Name = "牆外", Kind = "other", LoFt = F(20, 20, 1), HiFt = F(21, 21, 2) },
            };
            for (int i = 0; i < 300; i++)
                fx.Add(new() { Name = $"小障礙 {i:D3}", Kind = i % 2 == 0 ? "tray_power" : "tray_signal", LoFt = F(0.02 * (i % 400), 7.5, 0.1), HiFt = F(0.02 * (i % 400) + 0.01, 7.6, 0.2) });
            var fr = Obstacles.Build(fx, cs, a, room);
            Check(fr.Obstacles.Count == 200 && Obstacles.Validate(fr.Obstacles, room).Count == 0, "fixture 綜合案例通過 C# 端 §2 驗證");
            File.WriteAllText(fixturePath, Obstacles.ToJson(fr.Obstacles), new System.Text.UTF8Encoding(false));
        }
    }
}