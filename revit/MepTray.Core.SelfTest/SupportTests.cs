using System.Text.Json.Nodes;
using MepTray.Core;

/// <summary>吊架支撐點核心（Supports.cs）的自我測試；contractPath 非 null 時比對 Python 產生的契約案例。</summary>
static class SupportTests
{
    static JsonObject BaseCfg() => new()
    {
        ["units"] = "mm", ["max_spacing_mm"] = 1500.0, ["start_offset_mm"] = 300.0, ["end_offset_mm"] = 300.0,
        ["short_run_policy"] = "midpoint", ["near_fitting"] = new JsonObject { ["enabled"] = false },
    };

    static SupportPiece P(string id, double[] a, double[] b, double w = 300, double h = 100, string sk = "free", string ek = "free") =>
        new() { Id = id, Start = a, End = b, WidthMm = w, HeightMm = h, StartKind = sk, EndKind = ek };

    static HangerConfig Cfg(Action<JsonObject>? f = null) { var c = BaseCfg(); f?.Invoke(c); return HangerConfigReader.Read(c, false); }

    public static void Run(Action<bool, string> Check, string? contractPath)
    {
        var none = new List<SupportLink>();
        // 可整除：6000 mm、內縮 300、上限 1350 → 4 等分剛好 1350
        var r = Supports.Layout(new[] { P("A", new[] { 0.0, 0, 3000 }, new[] { 6000.0, 0, 3000 }) }, none, Cfg(c => c["max_spacing_mm"] = 1350.0));
        Check(r.Supports.Select(s => s.StationMm).SequenceEqual(new[] { 300.0, 1650, 3000, 4350, 5700 }), "支撐點：可整除間距");
        // 不可整除：尾數不會超過上限
        r = Supports.Layout(new[] { P("A", new[] { 0.0, 0, 3000 }, new[] { 6000.0, 0, 3000 }) }, none, Cfg(c => c["max_spacing_mm"] = 1000.0));
        var st = r.Supports.Select(s => s.StationMm).ToList();
        Check(st.Count == 7 && st.Zip(st.Skip(1)).All(z => z.Second - z.First <= 1000 + 1e-6), "支撐點：不可整除時等分，所有間距 ≤ 上限");
        // 固定間距：6095.483 mm、內縮 0、1500 → 0..6000，剩餘 95.483 列待確認
        r = Supports.Layout(new[] { P("A", new[] { 0.0, 0, 0 }, new[] { 6095.483, 0, 0 }) }, none,
            Cfg(c => { c["spacing_mode"] = "fixed"; c["start_offset_mm"] = 0.0; c["end_offset_mm"] = 0.0; }));
        Check(r.Supports.Select(s => s.StationMm).SequenceEqual(new[] { 0.0, 1500, 3000, 4500, 6000 })
              && r.Issues.Count == 1 && r.Issues[0].Code == "fixed_remainder" && r.Issues[0].Level == "pending"
              && r.Issues[0].LocationMm[0] == 6000 && r.Issues[0].Detail.Contains("95.483"), "固定間距：剩餘段不放終點支撐，列待確認");
        // 固定間距：剛好整除 → 終點正常放置、無問題
        r = Supports.Layout(new[] { P("A", new[] { 0.0, 0, 0 }, new[] { 6600.0, 0, 0 }) }, none, Cfg(c => c["spacing_mode"] = "fixed"));
        Check(r.Supports.Select(s => s.StationMm).SequenceEqual(new[] { 300.0, 1800, 3300, 4800, 6300 }) && r.Issues.Count == 0, "固定間距：整除時終點正常放置");
        // even 明示 = 預設
        var ev0 = Supports.Layout(new[] { P("A", new[] { 0.0, 0, 0 }, new[] { 6095.483, 0, 0 }) }, none, Cfg());
        var ev1 = Supports.Layout(new[] { P("A", new[] { 0.0, 0, 0 }, new[] { 6095.483, 0, 0 }) }, none, Cfg(c => c["spacing_mode"] = "even"));
        Check(ev0.Supports.Select(s => s.StationMm).SequenceEqual(ev1.Supports.Select(s => s.StationMm)) && ev0.Issues.Count == ev1.Issues.Count
              && ev0.Supports.Count == 5, "spacing_mode=even 與未填一致");
        Check(HangerConfigReader.ValidateLayout(new JsonObject { ["units"] = "mm", ["max_spacing_mm"] = 1500.0, ["start_offset_mm"] = 0.0, ["end_offset_mm"] = 0.0,
              ["short_run_policy"] = "skip", ["near_fitting"] = new JsonObject { ["enabled"] = false }, ["spacing_mode"] = "Fixed" }).Count(x => x.StartsWith("spacing_mode：")) == 1, "spacing_mode 驗證");
        // 短段
        r = Supports.Layout(new[] { P("A", new[] { 0.0, 0, 3000 }, new[] { 400.0, 0, 3000 }) }, none, Cfg());
        Check(r.Supports.Count == 1 && r.Supports[0].StationMm == 200 && r.Issues.Any(i => i.Code == "short_run" && i.Level == "warning"), "短段 midpoint：置中 1 支並警告");
        r = Supports.Layout(new[] { P("A", new[] { 0.0, 0, 3000 }, new[] { 400.0, 0, 3000 }) }, none, Cfg(c => c["short_run_policy"] = "skip"));
        Check(r.Supports.Count == 0 && r.Issues.Any(i => i.Code == "short_run" && i.Level == "failed"), "短段 skip：不放並列 failed");
        // 反向段：鏡射
        var fw = Supports.Layout(new[] { P("A", new[] { 0.0, 0, 0 }, new[] { 5000.0, 0, 0 }) }, none, Cfg());
        var rv = Supports.Layout(new[] { P("A", new[] { 5000.0, 0, 0 }, new[] { 0.0, 0, 0 }) }, none, Cfg());
        Check(fw.Supports.Select(s => s.PointMm[0]).OrderBy(x => x).SequenceEqual(rv.Supports.Select(s => s.PointMm[0]).OrderBy(x => x))
              && rv.Supports[0].DirectionDeg == 180, "反向段：起終內縮相同時位置一致，方向 180°");
        // 共線切段＋不同寬度：合併計算，寬度取所在段
        r = Supports.Layout(new[] { P("A", new[] { 0.0, 0, 0 }, new[] { 3000.0, 0, 0 }), P("B", new[] { 3000.0, 0, 0 }, new[] { 6000.0, 0, 0 }, 400) },
                            new[] { new SupportLink("A", "B", "union") }, Cfg());
        Check(r.Supports.Count == 5 && r.Supports.All(s => s.ChainId == "C001") && r.Supports.Last().TrayWidthMm == 400 && r.Supports.First().TrayWidthMm == 300,
              "共線切段合併計算；不同寬度各自記錄");
        // 接頭本體空隙：支撐點不落在接頭內
        r = Supports.Layout(new[] { P("A", new[] { 0.0, 0, 0 }, new[] { 2950.0, 0, 0 }), P("B", new[] { 3250.0, 0, 0 }, new[] { 6000.0, 0, 0 }) },
                            new[] { new SupportLink("A", "B", "union") }, Cfg(c => c["max_spacing_mm"] = 1350.0));
        Check(r.Supports.All(s => s.PointMm[0] <= 2950 + 1e-6 || s.PointMm[0] >= 3250 - 1e-6) && r.Issues.Any(i => i.Code == "moved_out_of_fitting")
              && !r.Issues.Any(i => i.Code == "spacing_not_met"), "接頭本體內不放吊架（移到邊緣並補點維持間距）");
        // 斜段／垂直段
        r = Supports.Layout(new[] { P("V", new[] { 0.0, 0, 0 }, new[] { 0.0, 0, 3000 }), P("S", new[] { 0.0, 0, 0 }, new[] { 3000.0, 0, 100 }) }, none, Cfg());
        Check(r.Supports.Count == 0 && r.Issues.Any(i => i.Code == "vertical") && r.Issues.Any(i => i.Code == "sloped" && i.LocationMm[0] == 1500), "斜段/垂直段回報 unsupported 並附位置");
        // 近接頭加強：只有明確啟用才加
        var lp = new[] { P("A", new[] { 0.0, 0, 0 }, new[] { 3000.0, 0, 0 }, ek: "fitting") };
        var off = Supports.Layout(lp, none, Cfg(c => c["end_offset_mm"] = 600.0));
        var on = Supports.Layout(lp, none, Cfg(c => { c["end_offset_mm"] = 600.0; c["near_fitting"] = new JsonObject { ["enabled"] = true, ["max_distance_mm"] = 200.0 }; }));
        Check(!off.Supports.Any(s => s.Reason == "near_fitting") && on.Supports.Any(s => s.Reason == "near_fitting" && s.StationMm == 2800), "近接頭加強只在設定啟用時");
        // 承托面 ≠ 中心線
        Check(fw.Supports[0].BearingZMm == -50, "承托面高程 = 中心線 − 高度/2");
        // 設定驗證
        var bad = HangerConfigReader.ValidatePlacement(new JsonObject { ["units"] = "mm" });
        foreach (var f in new[] { "max_spacing_mm", "start_offset_mm", "end_offset_mm", "family", "insertion_to_bearing_mm", "orientation_correction_deg", "placement_mode", "parameter_map", "rod_length.mode", "dedupe" })
            Check(HangerConfigReader.ErrorFields(bad).Contains(f), $"缺少欄位被點名：{f}");
        var neg = BaseCfg(); neg["max_spacing_mm"] = -1.0;
        Check(HangerConfigReader.ErrorFields(HangerConfigReader.ValidateLayout(neg)).Contains("max_spacing_mm"), "負間距被拒絕");
        bool threw = false;
        try { HangerConfigReader.Read(new JsonObject(), false); } catch (InvalidDataException) { threw = true; }
        Check(threw, "設定不完整時拒絕執行");
        TestFailNth(Check);
        InstanceParams(Check);
        RodToStructureCases(Check);
        // 去重／重跑
        var plan = fw.Supports.Select(s => new PlannedKey("T1", s.StationMm, s.PointMm, s.DirectionDeg)).ToList();
        var ex = fw.Supports.Select(s => new ExistingHanger("e" + s.Id, "tool", "T1", s.StationMm, s.PointMm, s.DirectionDeg)).ToList();
        Check(Supports.Classify(plan, ex, 5, 2).All(c => c.Status == "exists"), "重跑冪等：本工具已放置者全部 exists");
        var other = new List<ExistingHanger> { new("m1", "other", null, null, new[] { 301.0, 0, 0 }, null), new("t2", "tool", "T2", 300, new[] { 300.0, 0, 0 }, 0) };
        var cl = Supports.Classify(plan, other, 5, 2);
        Check(cl[0].Status == "pending" && cl[0].Match.SequenceEqual(new[] { "m1" }) && cl.Skip(1).All(c => c.Status == "new"), "手動吊架附近 → 待確認；他橋架的本工具吊架不視為重複");
        var rot = new List<ExistingHanger> { new("x", "tool", "T1", 300, new[] { 300.0, 0, 0 }, 90) };
        Check(Supports.Classify(plan.Take(1).ToList(), rot, 5, 2)[0].Status == "pending", "方向不相容 → 待確認");
        Check(Supports.AngleCompatible(0, 180, 1) && !Supports.AngleCompatible(0, 90, 1), "方向相容以 180° 對稱判定");
        DedupeGeometry(Check);

        if (contractPath != null) RunContract(Check, contractPath);
    }

    // 合格的放置設定（以字串解析，與讀檔相同的 JsonElement 路徑）
    const string PlaceJson = """
        {"units": "mm", "max_spacing_mm": 1350.0, "start_offset_mm": 300.0, "end_offset_mm": 300.0, "short_run_policy": "midpoint",
         "near_fitting": {"enabled": false}, "supported_object": "cable_tray", "family": {"family_name": "F", "type_name": "T"},
         "placement_mode": "level", "widen_left_mm": 50.0, "widen_right_mm": 50.0, "insertion_to_bearing_mm": 40.0,
         "orientation_correction_deg": 90.0, "parameter_map": {"cross_member_length": {"name": "P", "scope": "type"}},
         "rod_length": {"mode": "none"}, "dedupe": {"tolerance_mm": 5.0, "angle_tolerance_deg": 2.0},
         "calibration": {"family_file": {"path": "X:/f.rfa", "size": 1, "sha256": "ab"}, "family_name": "F", "type_name": "T",
                         "project_signature": "cd", "cross_member_model": {"ratio": 1.0, "offset_mm": 0.0}}}
        """;

    static JsonObject PlaceCfg(string? testFailNthLiteral = null)
    {
        var o = JsonNode.Parse(PlaceJson)!.AsObject();
        if (testFailNthLiteral != null) o["test_fail_nth"] = JsonNode.Parse(testFailNthLiteral);
        return o;
    }

    static JsonObject RodCfg(Action<JsonObject>? f = null)
    {
        var o = PlaceCfg();
        var rod = new JsonObject { ["mode"] = "to_structure", ["param"] = new JsonObject { ["name"] = "R", ["scope"] = "instance" },
                                   ["offset_mm"] = -20.0, ["min_mm"] = 100.0, ["max_mm"] = 5000.0 };
        f?.Invoke(rod);
        o["rod_length"] = rod;
        return o;
    }

    /// <summary>去重：實際幾何（插入點／旋轉角／橫擔長度）與追蹤資料完整性。pending 一律不新增吊架。</summary>
    static void DedupeGeometry(Action<bool, string> Check)
    {
        var plan = new List<PlannedKey> { new("T1", 300, new[] { 300.0, 0, 0 }, 0, new[] { 300.0, 0, -90 }, 90, 400) };
        ExistingHanger Ex(double[]? pt = null, double? rot = 90, double? len = 400, string? key = "T1", double? st = 300, double? dir = 0, bool nullPt = false)
            => new("e1", "tool", key, st, new[] { 300.0, 0, 0 }, dir, nullPt ? null : pt ?? new[] { 300.0, 0, -90 }, rot, len);
        string S(ExistingHanger e) => Supports.Classify(plan, new List<ExistingHanger> { e }, 5, 2)[0].Status;
        Check(S(Ex()) == "exists", "幾何去重：模型與設定未變 → exists");
        Check(S(Ex(pt: new[] { 2300.0, 0, -90 })) == "pending", "幾何去重：吊架水平移動 → pending");
        Check(S(Ex(pt: new[] { 300.0, 0, -2090 })) == "pending", "幾何去重：吊架高程改變 → pending");
        Check(S(Ex(rot: 120)) == "pending", "幾何去重：旋轉超過容許值 → pending");
        Check(S(Ex(rot: 270)) == "pending", "幾何去重：旋轉 180° 反向 → pending（旋轉嚴格比較）");
        Check(S(Ex(len: 500)) == "pending", "幾何去重：橫擔尺寸改變 → pending");
        Check(S(Ex(nullPt: true)) == "pending", "幾何去重：實際插入點讀不到 → pending");
        Check(S(Ex(rot: null)) == "pending" && S(Ex(len: null)) == "pending", "幾何去重：實際旋轉角／橫擔長度讀不到 → pending");
        Check(S(Ex(st: null)) == "pending" && S(Ex(key: null)) == "pending" && S(Ex(dir: null)) == "pending", "追蹤資料缺欄位 → pending（不補一支）");
        Check(S(Ex(key: "  ")) == "pending" && S(Ex(key: "")) == "pending", "追蹤資料 tray_key 為空白字串 → pending");
        Check(S(Ex(st: double.NaN)) == "pending" && S(Ex(dir: double.PositiveInfinity)) == "pending" && S(Ex(rot: double.NaN)) == "pending" && S(Ex(len: double.PositiveInfinity)) == "pending",
              "NaN／Infinity 視為缺少 → pending");
        // 缺 tray_key、位置在預定插入點（與中心線點相差 50 mm，左右加寬不對稱）→ 以插入點判斷，必須 pending 而不是 new
        var offPlan = new List<PlannedKey> { new("T1", 300, new[] { 300.0, 0, 0 }, 0, new[] { 300.0, 50, -90 }, 90, 400) };
        var lost = new ExistingHanger("lost", "tool", null, null, new[] { 300.0, 50, -90 }, null, new[] { 300.0, 50, -90 }, 90, 400);
        Check(Supports.Classify(offPlan, new List<ExistingHanger> { lost }, 5, 2)[0].Status == "pending", "缺 tray_key 的鄰近判斷用預定插入點（含偏移）→ pending");
        var movedPlan = new List<PlannedKey> { new("T1", 300, new[] { 300.0, 0, 0 }, 0, new[] { 300.0, 800, -90 }, 90, 400) };
        Check(Supports.Classify(movedPlan, new List<ExistingHanger> { Ex() }, 5, 2)[0].Status == "pending", "幾何去重：橋架移動、吊架未跟著移動 → pending");
    }

    static void RodToStructureCases(Action<bool, string> Check)
    {
        // 設定驗證：to_structure 的 offset／min／max 必填且無預設；param 限 instance
        Check(HangerConfigReader.ValidatePlacement(RodCfg()).Count == 0, "to_structure：完整設定合格");
        foreach (var k in new[] { "offset_mm", "min_mm", "max_mm", "param" })
        {
            var errs = HangerConfigReader.ErrorFields(HangerConfigReader.ValidatePlacement(RodCfg(r => r.Remove(k))));
            Check(errs.SequenceEqual(new[] { "rod_length." + k }), $"to_structure：缺 {k} 被點名且只點名該欄位: {string.Join(",", errs)}");
        }
        Check(HangerConfigReader.ErrorFields(HangerConfigReader.ValidatePlacement(RodCfg(r => r["param"]!["scope"] = "type"))).SequenceEqual(new[] { "rod_length.param.scope" }),
              "to_structure：param.scope=type 被拒絕");
        Check(HangerConfigReader.ErrorFields(HangerConfigReader.ValidatePlacement(RodCfg(r => { r["min_mm"] = 800.0; r["max_mm"] = 700.0; }))).SequenceEqual(new[] { "rod_length.max_mm" }),
              "to_structure：max < min 被拒絕");
        Check(HangerConfigReader.ErrorFields(HangerConfigReader.ValidatePlacement(RodCfg(r => r["min_mm"] = -1.0))).SequenceEqual(new[] { "rod_length.min_mm" }), "to_structure：min < 0 被拒絕");
        Check(HangerConfigReader.ErrorFields(HangerConfigReader.ValidatePlacement(RodCfg(r => r["max_mm"] = 0.0))).Contains("rod_length.max_mm"), "to_structure：max = 0 被拒絕");
        Check(HangerConfigReader.ErrorFields(HangerConfigReader.ValidatePlacement(RodCfg(r => r["offset_mm"] = "10"))).SequenceEqual(new[] { "rod_length.offset_mm" }), "to_structure：offset 非數值被拒絕");
        foreach (var bad in new[] { 0.0, -5.0 })
            Check(HangerConfigReader.ErrorFields(HangerConfigReader.ValidatePlacement(RodCfg(r => r["round_to_mm"] = bad))).SequenceEqual(new[] { "rod_length.round_to_mm" }), $"to_structure：round_to_mm={bad} 被拒絕");
        Check(HangerConfigReader.ValidatePlacement(RodCfg(r => { r["round_to_mm"] = 5.0; r["offset_mm"] = 0.0; })).Count == 0, "to_structure：offset=0 與 round_to_mm=5 合格（0 需明確填寫，不是缺漏）");
        Check(HangerConfigReader.ErrorFields(HangerConfigReader.ValidatePlacement(RodCfg(r => r["mode"] = "structure"))).Contains("rod_length.mode"), "rod_length.mode 未知值被拒絕");
        Check(HangerConfigReader.ValidatePlacement(PlaceCfg()).Count == 0, "rod_length.mode=none 行為不變");
        // 佈置層不計算結構長度：Place 不給 RodLengthMm
        var tcfg = HangerConfigReader.Read(RodCfg(), true);
        var sp = Supports.Layout(new[] { P("A", new[] { 0.0, 0, 3000 }, new[] { 3000.0, 0, 3000 }) }, new List<SupportLink>(), tcfg).Supports[0];
        Check(Supports.Place(sp, tcfg).RodLengthMm is null && tcfg.RodMode == "to_structure" && tcfg.RodOffsetMm == -20 && tcfg.RodMinMm == 100 && tcfg.RodMaxMm == 5000 && tcfg.RodRoundToMm is null,
              "to_structure：Place 不計算長度，設定值原樣讀入（無預設）");
        // 長度計算：結構面 Z − 承托面 Z + offset；須高於橋架頂面且在 [min, max]
        var c = HangerConfigReader.Read(RodCfg(), true);
        var r0 = RodLength.Compute(2950, 5000, 3050, c);
        Check(r0.LengthMm == 2030 && r0.Code is null, $"長度 = 5000 − 2950 + (−20) = 2030: {r0.LengthMm}");
        Check(RodLength.Compute(2950, 3050, 3050, c).Code == RodLength.NotAboveTray && RodLength.Compute(2950, 3000, 3050, c).Code == RodLength.NotAboveTray, "結構面不高於橋架頂面 → 失敗");
        Check(RodLength.Compute(0, 120, 100, c).LengthMm == 100 && RodLength.Compute(0, 119.999, 100, c).Code == RodLength.BelowMin, "剛好 = min 通過、略小於 min 失敗");
        Check(RodLength.Compute(0, 5020, 100, c).LengthMm == 5000 && RodLength.Compute(0, 5020.001, 100, c).Code == RodLength.AboveMax, "剛好 = max 通過、略大於 max 失敗");
        Check(RodLength.Compute(0, 5020, 100, c).Message is null && RodLength.Compute(0, 6000, 100, c).Message!.Contains("max_mm"), "失敗訊息為中文並點名設定欄位");
        // 取整：最接近的倍數，恰在中間遠離 0 進位；未設定 = 只取到 0.001 mm
        var rc = HangerConfigReader.Read(RodCfg(r => { r["round_to_mm"] = 5.0; r["offset_mm"] = 0.0; }), true);
        Check(RodLength.Compute(0, 2987.5, 100, rc).LengthMm == 2990 && RodLength.Compute(0, 2987.4, 100, rc).LengthMm == 2985 && RodLength.Compute(0, 2982.4, 100, rc).LengthMm == 2980,
              "round_to_mm=5：最接近的倍數、恰在中間進位");
        Check(RodLength.Compute(0, 1234.56789, 100, HangerConfigReader.Read(RodCfg(r => r["offset_mm"] = 0.0), true)).LengthMm == 1234.568, "未設定 round_to_mm：取到 0.001 mm");
    }

    /// <summary>驗收用測試開關 test_fail_nth：未填 = 關閉；只接受 ≥ 1 的整數字面；不影響佈置與族座標換算。</summary>
    static void TestFailNth(Action<bool, string> Check)
    {
        Check(HangerConfigReader.ValidatePlacement(PlaceCfg()).Count == 0 && HangerConfigReader.Read(PlaceCfg(), true).TestFailNth is null,
              "test_fail_nth 未填：設定合格且開關關閉");
        foreach (var (lit, n) in new[] { ("1", 1L), ("2", 2L), ("9223372036854775807", long.MaxValue) })
            Check(HangerConfigReader.ValidatePlacement(PlaceCfg(lit)).Count == 0 && HangerConfigReader.Read(PlaceCfg(lit), true).TestFailNth == n,
                  $"test_fail_nth={lit} 被接受");
        foreach (var lit in new[] { "0", "-1", "2.0", "1.5", "1e3", "\"2\"", "true", "null", "[2]", "{}", "9223372036854775808" })
        {
            var errs = HangerConfigReader.ErrorFields(HangerConfigReader.ValidatePlacement(PlaceCfg(lit)));
            Check(errs.SequenceEqual(new[] { "test_fail_nth" }), $"test_fail_nth={lit} 被拒絕且只點名該欄位: {string.Join(",", errs)}");
        }
        bool threw = false;
        try { HangerConfigReader.Read(PlaceCfg("0"), true); } catch (InvalidDataException ex) { threw = ex.Message.Contains("test_fail_nth"); }
        Check(threw, "test_fail_nth 無效時拒絕執行並點名欄位");
        Check(HangerConfigReader.ValidateLayout(PlaceCfg("0")).Count == 0, "test_fail_nth 不屬於佈置欄位（layout-only 驗證不受影響）");
        // 開關不影響佈置與族座標換算
        var pieces = new[] { P("A", new[] { 0.0, 0, 3000 }, new[] { 6000.0, 0, 3000 }) };
        var off = HangerConfigReader.Read(PlaceCfg(), true);
        var on = HangerConfigReader.Read(PlaceCfg("2"), true);
        var lo = Supports.Layout(pieces, new List<SupportLink>(), off);
        var ln = Supports.Layout(pieces, new List<SupportLink>(), on);
        bool same = lo.Supports.Count == ln.Supports.Count && lo.Issues.Count == ln.Issues.Count && lo.Supports.Count == 5;
        for (int i = 0; same && i < lo.Supports.Count; i++)
        {
            var a = Supports.Place(lo.Supports[i], off); var b = Supports.Place(ln.Supports[i], on);
            same = lo.Supports[i].StationMm == ln.Supports[i].StationMm && a.InsertionPointMm.SequenceEqual(b.InsertionPointMm)
                   && a.RotationDeg == b.RotationDeg && a.CrossMemberLengthMm == b.CrossMemberLengthMm;
        }
        Check(same, "test_fail_nth 不改變支撐點與族插入點/旋轉/尺寸");
        // 判定與訊息
        Check(!TestSwitch.ShouldFail(null, 1) && TestSwitch.ShouldFail(2, 2) && !TestSwitch.ShouldFail(2, 1) && !TestSwitch.ShouldFail(2, 3),
              "TestSwitch.ShouldFail 只在第 N 支為 true；未設定永不觸發");
        Check(TestSwitch.FailMessage(2) == "測試開關 test_fail_nth=2：故意讓第 2 支失敗", "測試開關失敗訊息");
        Check(TestSwitch.PreviewNote(2).StartsWith("⚠ 測試開關 test_fail_nth=2 已啟用") && TestSwitch.PreviewNote(2).Contains("僅供驗收，正式設定勿使用"),
              "測試開關預覽警告");
        Check(TestSwitch.NotTriggeredNote(5, 3).Contains("不會觸發"), "測試開關 N 大於新增數的附註");
        // 放置後整批決策：全部失敗 → 整批回滾；至少一支成功 → 保留（部分完成）；計畫無新增 → 不因此回滾
        Check(HangerBatch.RollBackAllFailed(1, 0) && HangerBatch.RollBackAllFailed(3, 0), "HangerBatch：計畫有新增但全部失敗 → 整批回滾");
        Check(!HangerBatch.RollBackAllFailed(1, 1) && !HangerBatch.RollBackAllFailed(3, 1) && !HangerBatch.RollBackAllFailed(3, 3),
              "HangerBatch：至少一支建立成功 → 不回滾");
        Check(!HangerBatch.RollBackAllFailed(0, 0), "HangerBatch：計畫沒有新增吊架 → 不因此回滾");
        // 與測試開關合用（模擬 Execute 逐支迴圈：只有第 N 支失敗）
        foreach (var (planned, nth, expectRollBack) in new[] { (1, 1L, true), (3, 2L, false), (3, 1L, false), (1, 2L, false), (2, 5L, false) })
        {
            int created = Enumerable.Range(1, planned).Count(o => !TestSwitch.ShouldFail(nth, o));
            Check(HangerBatch.RollBackAllFailed(planned, created) == expectRollBack,
                  $"HangerBatch＋test_fail_nth={nth}、新增 {planned} 支 → {(expectRollBack ? "整批回滾" : "保留")}");
        }
    }

    /// <summary>instance_params：未填或 [] = 不寫入；只接受非空白文字值、不重複且不與長度參數同名；不影響佈置與族座標換算。</summary>
    static void InstanceParams(Action<bool, string> Check)
    {
        JsonObject Ip(string lit) { var o = PlaceCfg(); o["instance_params"] = JsonNode.Parse(lit); return o; }
        const string ok = """[{"name": "<參數A>", "value": "<值A>"}, {"name": "<參數B>", "value": "<值B>"}]""";
        Check(HangerConfigReader.Read(PlaceCfg(), true).InstanceParams.Count == 0, "instance_params 未填：不寫入");
        Check(HangerConfigReader.ValidatePlacement(Ip("[]")).Count == 0 && HangerConfigReader.Read(Ip("[]"), true).InstanceParams.Count == 0, "instance_params=[]：合格且不寫入");
        var r = HangerConfigReader.Read(Ip(ok), true).InstanceParams;
        Check(r.SequenceEqual(new[] { new InstanceParamValue("<參數A>", "<值A>"), new InstanceParamValue("<參數B>", "<值B>") }), "instance_params 依序讀入名稱與值（原樣，不修剪）");
        foreach (var (lit, fields) in new (string, string[])[] {
            ("{}", new[] { "instance_params" }), ("\"a\"", new[] { "instance_params" }), ("null", new[] { "instance_params" }),
            ("[\"a\"]", new[] { "instance_params[0]" }), ("[null]", new[] { "instance_params[0]" }),
            ("""[{"value": "v"}]""", new[] { "instance_params[0].name" }), ("""[{"name": " a", "value": "v"}]""", new[] { "instance_params[0].name" }),
            ("""[{"name": 1, "value": "v"}]""", new[] { "instance_params[0].name" }),
            ("""[{"name": "a"}]""", new[] { "instance_params[0].value" }), ("""[{"name": "a", "value": ""}]""", new[] { "instance_params[0].value" }),
            ("""[{"name": "a", "value": "  "}]""", new[] { "instance_params[0].value" }), ("""[{"name": "a", "value": 12}]""", new[] { "instance_params[0].value" }),
            ("""[{"name": "a", "value": true}]""", new[] { "instance_params[0].value" }),
            ("""[{"name": "a", "value": "v", "scope": "instance"}]""", new[] { "instance_params[0]" }),
            ("""[{"name": "a", "value": "1"}, {"name": "b", "value": "2"}, {"name": "a", "value": "3"}]""", new[] { "instance_params[2].name" }),
            ("""[{"name": "P", "value": "v"}]""", new[] { "instance_params[0].name" }),
        })
        {
            var errs = HangerConfigReader.ErrorFields(HangerConfigReader.ValidatePlacement(Ip(lit)));
            Check(errs.SequenceEqual(fields), $"instance_params={lit} 被拒絕且只點名 {string.Join(",", fields)}: {string.Join(",", errs)}");
        }
        Check(HangerConfigReader.ValidatePlacement(Ip("""[{"name": "a", "value": "1"}, {"name": "a", "value": "2"}]"""))
                  .SequenceEqual(new[] { "instance_params[1].name：「a」與 instance_params[0] 重複" }), "instance_params 重複名稱訊息");
        bool threw = false;
        try { HangerConfigReader.Read(Ip("""[{"name": "a", "value": ""}]"""), true); } catch (InvalidDataException ex) { threw = ex.Message.Contains("instance_params[0].value"); }
        Check(threw, "instance_params 空值時拒絕執行並點名欄位");
        Check(HangerConfigReader.ValidateLayout(Ip("5")).Count == 0, "instance_params 不屬於佈置欄位");
        var pieces = new[] { P("A", new[] { 0.0, 0, 3000 }, new[] { 6000.0, 0, 3000 }) };
        var off = HangerConfigReader.Read(PlaceCfg(), true); var on = HangerConfigReader.Read(Ip(ok), true);
        var lo = Supports.Layout(pieces, new List<SupportLink>(), off); var ln = Supports.Layout(pieces, new List<SupportLink>(), on);
        bool same = lo.Supports.Count == ln.Supports.Count && lo.Supports.Count == 5;
        for (int i = 0; same && i < lo.Supports.Count; i++)
        {
            var a = Supports.Place(lo.Supports[i], off); var b = Supports.Place(ln.Supports[i], on);
            same = a.InsertionPointMm.SequenceEqual(b.InsertionPointMm) && a.RotationDeg == b.RotationDeg && a.CrossMemberLengthMm == b.CrossMemberLengthMm;
        }
        Check(same, "instance_params 不改變支撐點與族插入點/旋轉/尺寸");
        // 逐支檢查與訊息（純邏輯；Revit 端以 LookupParameter/IsReadOnly/StorageType 取得狀態）
        Check(InstanceParamText.Problem("X", true, false, true, "String") is null, "可寫的文字參數 → 可寫入");
        Check(InstanceParamText.Problem("X", false, false, false, "")!.StartsWith("找不到實例參數「X」"), "參數不存在 → 該支失敗並點名");
        Check(InstanceParamText.Problem("X", true, true, true, "String")!.Contains("唯讀"), "唯讀 → 該支失敗");
        Check(InstanceParamText.Problem("X", true, false, false, "Double")!.Contains("不是文字參數（StorageType=Double"), "非文字 → 該支失敗");
        Check(InstanceParamText.ReadbackError("X", "v", "v") is null && InstanceParamText.ReadbackError("X", "v", "V") != null
              && InstanceParamText.ReadbackError("X", "v", null)!.Contains("(無值)"), "讀回逐字比對");
        var note = InstanceParamText.PreviewNote(on.InstanceParams);
        Check(note.Contains("2 項") && note.Contains("「<參數A>」=「<值A>」") && note.Contains("既有／手動吊架不修改"), "預覽附註列出名稱與值");
        Check(InstanceParamText.TypeParamError(1, "X").StartsWith("instance_params[1].name："), "預檢：類型參數訊息點名欄位");
    }

    static double[] Arr(JsonNode? n) => n!.AsArray().Select(x => x!.GetValue<double>()).ToArray();
    static bool Near(double a, double b) => Math.Abs(a - b) <= 2e-3;
    static bool Near3(double[] a, double[] b) => a.Length == b.Length && a.Zip(b).All(z => Near(z.First, z.Second));

    static double[]? OptArr(JsonNode? n) => n is JsonArray a && a.All(x => x is JsonValue v && v.GetValueKind() == System.Text.Json.JsonValueKind.Number)
        ? a.Select(x => x!.GetValue<double>()).ToArray() : null;
    static double? OptNum(JsonNode? n) => n is JsonValue v && v.GetValueKind() == System.Text.Json.JsonValueKind.Number ? v.GetValue<double>() : null;

    /// <summary>契約案例的接頭輸入（null／缺欄位 = 該欄位無效；數值型別以 JSON Number 為準，與 Python _num 一致）。</summary>
    static SupportFitting ToFitting(JsonNode? f) => new()
    {
        Id = f!["id"]!.GetValue<string>(), Kind = f["kind"]!.GetValue<string>(),
        Center = OptArr(f["center"]), Point = OptArr(f["point"]), LocationMm = OptArr(f["location_mm"]),
        RadiusMm = OptNum(f["radius_mm"]), StartAngleDeg = OptNum(f["start_angle_deg"]), SweepDeg = OptNum(f["sweep_deg"]),
        MainDirectionDeg = OptNum(f["main_direction_deg"]), WidthMm = OptNum(f["width_mm"]), HeightMm = OptNum(f["height_mm"]),
        FallbackReason = f["fallback_reason"]?.GetValue<string>(),
    };

    static void RunContract(Action<bool, string> Check, string path)
    {
        var root = JsonNode.Parse(File.ReadAllText(path))!;
        int n = 0;
        foreach (var c in root["layout_cases"]!.AsArray())
        {
            n++;
            var name = c!["name"]!.GetValue<string>();
            var cfgNode = c["cfg"];
            var expFields = c["config_errors"]!.AsArray().Select(x => x!.GetValue<string>()).ToList();
            var gotFields = HangerConfigReader.ErrorFields(HangerConfigReader.ValidatePlacement(cfgNode?.DeepClone()));
            Check(expFields.SequenceEqual(gotFields), $"契約[{name}] 設定驗證欄位一致: py={string.Join(",", expFields)} cs={string.Join(",", gotFields)}");
            var exp = c["expect"];
            if (exp is null) continue;
            var pieces = c["pieces"]!.AsArray().Select(p => new SupportPiece
            {
                Id = p!["id"]!.GetValue<string>(), Start = Arr(p["start"]), End = Arr(p["end"]),
                WidthMm = p["width_mm"]!.GetValue<double>(), HeightMm = p["height_mm"]!.GetValue<double>(),
                StartKind = p["start_kind"]!.GetValue<string>(), EndKind = p["end_kind"]!.GetValue<string>(),
            }).ToList();
            var links = c["links"]!.AsArray().Select(l => new SupportLink(l!["a"]!.GetValue<string>(), l["b"]!.GetValue<string>(), l["kind"]!.GetValue<string>())).ToList();
            var cfg = HangerConfigReader.Read(cfgNode!.DeepClone(), false);
            var fittings = c["fittings"] is JsonArray fa ? fa.Select(ToFitting).ToList() : null;
            var got = Supports.Layout(pieces, links, cfg, fittings);
            var es = exp["supports"]!.AsArray();
            bool same = es.Count == got.Supports.Count;
            for (int i = 0; same && i < es.Count; i++)
            {
                var e = es[i]!; var g = got.Supports[i];
                same = e["id"]!.GetValue<string>() == g.Id && e["piece_id"]!.GetValue<string>() == g.PieceId && e["chain_id"]!.GetValue<string>() == g.ChainId
                       && Near(e["station_mm"]!.GetValue<double>(), g.StationMm) && Near3(Arr(e["point_mm"]), g.PointMm)
                       && Near(e["direction_deg"]!.GetValue<double>(), g.DirectionDeg) && Near(e["tray_width_mm"]!.GetValue<double>(), g.TrayWidthMm)
                       && Near(e["bearing_z_mm"]!.GetValue<double>(), g.BearingZMm) && e["reason"]!.GetValue<string>() == g.Reason
                       && e["fitting_id"]?.GetValue<string>() == g.FittingId && Near(e["tray_height_mm"]!.GetValue<double>(), g.TrayHeightMm);
            }
            Check(same, $"契約[{name}] 支撐點與 Python 一致（{es.Count}/{got.Supports.Count}）");
            var ei = exp["issues"]!.AsArray();
            bool si = ei.Count == got.Issues.Count;
            for (int i = 0; si && i < ei.Count; i++)
            {
                var e = ei[i]!; var g = got.Issues[i];
                si = e["level"]!.GetValue<string>() == g.Level && e["code"]!.GetValue<string>() == g.Code && e["piece_id"]!.GetValue<string>() == g.PieceId
                     && Near3(Arr(e["location_mm"]), g.LocationMm);
            }
            Check(si, $"契約[{name}] 問題清單與 Python 一致（{ei.Count}/{got.Issues.Count}）");
            if (c["placements"] is JsonArray pls && expFields.Count == 0)
            {
                var pcfg = HangerConfigReader.Read(cfgNode.DeepClone(), true);
                bool sp = pls.Count == got.Supports.Count;
                for (int i = 0; sp && i < pls.Count; i++)
                {
                    var e = pls[i]!; var g = Supports.Place(got.Supports[i], pcfg);
                    var rod = e["rod_length_mm"];
                    sp = Near3(Arr(e["insertion_point_mm"]), g.InsertionPointMm) && Near3(Arr(e["bearing_point_mm"]), g.BearingPointMm)
                         && Near(e["rotation_deg"]!.GetValue<double>(), g.RotationDeg) && Near(e["cross_member_length_mm"]!.GetValue<double>(), g.CrossMemberLengthMm)
                         && (rod is null ? g.RodLengthMm is null : g.RodLengthMm is double rr && Near(rod.GetValue<double>(), rr));
                }
                Check(sp, $"契約[{name}] 族插入點/旋轉/尺寸與 Python 一致");
            }
        }
        foreach (var c in root["classify_cases"]!.AsArray())
        {
            n++;
            var name = c!["name"]!.GetValue<string>();
            var plan = c["planned"]!.AsArray().Select(p => new PlannedKey(p!["tray_key"]!.GetValue<string>(), p["station_mm"]!.GetValue<double>(), Arr(p["point_mm"]), p["direction_deg"]!.GetValue<double>(),
                p["insertion_point_mm"] is null ? null : Arr(p["insertion_point_mm"]), p["rotation_deg"]?.GetValue<double>(), p["cross_member_length_mm"]?.GetValue<double>())).ToList();
            var ex = c["existing"]!.AsArray().Select(e => new ExistingHanger(e!["id"]!.GetValue<string>(), e["source"]!.GetValue<string>(),
                e["tray_key"]?.GetValue<string>(), e["station_mm"]?.GetValue<double>(), e["point_mm"] is null ? null : Arr(e["point_mm"]), e["direction_deg"]?.GetValue<double>(),
                e["actual_point_mm"] is null ? null : Arr(e["actual_point_mm"]), e["actual_rotation_deg"]?.GetValue<double>(), e["actual_cross_member_length_mm"]?.GetValue<double>())).ToList();
            var got = Supports.Classify(plan, ex, c["tol_mm"]!.GetValue<double>(), c["tol_deg"]!.GetValue<double>());
            var exp = c["expect"]!.AsArray().Select(x => x!["status"]!.GetValue<string>() + ":" + string.Join(",", x["match"]!.AsArray().Select(m => m!.GetValue<string>()))).ToList();
            var g2 = got.Select(x => x.Status + ":" + string.Join(",", x.Match)).ToList();
            Check(exp.SequenceEqual(g2), $"契約[{name}] 去重分類與 Python 一致");
        }
        foreach (var rc in root["rod_cases"]!.AsArray())
        {
            n++;
            var name = rc!["name"]!.GetValue<string>();
            var cfg = HangerConfigReader.Read(rc["cfg"]!.DeepClone(), true);
            var got = RodLength.Compute(rc["bearing_z_mm"]!.GetValue<double>(), rc["hit_z_mm"]!.GetValue<double>(), rc["tray_top_z_mm"]!.GetValue<double>(), cfg);
            var expLen = rc["expect"]!["length_mm"];
            var expCode = rc["expect"]!["code"]?.GetValue<string>();
            Check(got.Code == expCode && (expLen is null ? got.LengthMm is null : got.LengthMm is double gl && Near(expLen.GetValue<double>(), gl)),
                  $"契約[{name}] 吊桿長度至結構與 Python 一致（py={expLen?.ToJsonString() ?? "null"}/{expCode ?? "-"} cs={got.LengthMm?.ToString() ?? "null"}/{got.Code ?? "-"}）");
        }
        Check(n > 0, $"契約案例非空（{n}）");
    }
}
