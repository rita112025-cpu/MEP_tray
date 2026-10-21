using System.Text.Json.Nodes;
using MepTray.Core;

/// <summary>配管吊架核心（ConduitHangers.cs）的自我測試；contractPath 非 null 時比對 Python 產生的契約案例。</summary>
static class ConduitTests
{
    // 使用者決定 2026-10-17：E75 兩管並排共用一支吊架、橫擔 300 mm、不加寬；角度/高程容差沿用橋架去重容差（1°、1 mm）
    static JsonObject BaseCc() => new()
    {
        ["units"] = "mm", ["supported_object"] = "conduit_pair", ["base_config"] = "tray_hanger_config.json", ["conduits_per_hanger"] = 2,
        ["allowed_trade_sizes_mm"] = new JsonArray(75.0),
        ["cross_member"] = new JsonObject { ["value_mm"] = 300.0, ["basis"] = "parameter" },
        ["pairing"] = new JsonObject { ["angle_tolerance_deg"] = 1.0, ["elevation_tolerance_mm"] = 1.0 },
    };

    static JsonObject BaseCfg() => new()
    {
        ["units"] = "mm", ["supported_object"] = "cable_tray", ["family"] = new JsonObject { ["family_name"] = "F", ["type_name"] = "T" },
        ["placement_mode"] = "level", ["max_spacing_mm"] = 1500.0, ["start_offset_mm"] = 0.0, ["end_offset_mm"] = 0.0,
        ["short_run_policy"] = "skip", ["spacing_mode"] = "fixed", ["near_fitting"] = new JsonObject { ["enabled"] = false },
        ["widen_left_mm"] = 50.0, ["widen_right_mm"] = 50.0, ["insertion_to_bearing_mm"] = 0.0, ["orientation_correction_deg"] = 0.0,
        ["parameter_map"] = new JsonObject { ["cross_member_length"] = new JsonObject { ["name"] = "P", ["scope"] = "instance" } },
        ["rod_length"] = new JsonObject { ["mode"] = "none" },
        ["dedupe"] = new JsonObject { ["tolerance_mm"] = 1.0, ["angle_tolerance_deg"] = 1.0 },
        ["calibration"] = new JsonObject
        {
            ["family_file"] = new JsonObject { ["path"] = "X:/f.rfa", ["size"] = 1.0, ["sha256"] = "ab" }, ["family_name"] = "F", ["type_name"] = "T",
            ["project_signature"] = "cd", ["cross_member_model"] = new JsonObject { ["ratio"] = 1.0, ["offset_mm"] = 100.0 },
        },
    };

    static ConduitInput K(string id, double[] a, double[] b, double? ts = 75, double? od = 75) =>
        new() { Id = id, Start = a, End = b, TradeSizeMm = ts, OuterDiameterMm = od };

    static ConduitConfig Cc(Action<JsonObject>? f = null) { var c = BaseCc(); f?.Invoke(c); return ConduitConfigReader.Read(c); }

    public static void Run(Action<bool, string> Check, string? contractPath)
    {
        var cc = Cc();
        // 兩管並排、完全重疊：一對，中線在兩管正中，外緣 = 150 + 75 = 225 ≤ 300
        var r = ConduitHangers.Pair(new[] { K("1", new[] { 0.0, 0, 3000 }, new[] { 6000.0, 0, 3000 }), K("2", new[] { 0.0, 150, 3000 }, new[] { 6000.0, 150, 3000 }) }, cc);
        Check(r.Pairs.Count == 1 && r.Issues.Count == 0 && r.Pairs[0].Id == "1+2" && r.Pairs[0].Start.SequenceEqual(new[] { 0.0, 75, 3000 })
              && r.Pairs[0].End.SequenceEqual(new[] { 6000.0, 75, 3000 }) && r.Pairs[0].OuterWidthMm == 225 && r.Pairs[0].BearingZMm == 2962.5,
              "配管：兩管並排 → 一對，吊架線在中線，承托面 = 中心 − 外徑/2");
        // 部分重疊：吊架線 = 重疊段，尾段待確認
        r = ConduitHangers.Pair(new[] { K("1", new[] { 0.0, 0, 0 }, new[] { 6000.0, 0, 0 }), K("2", new[] { 6500.0, 150, 0 }, new[] { 1000.0, 150, 0 }) }, cc);
        Check(r.Pairs.Count == 1 && r.Pairs[0].OverlapMm == 5000 && r.Pairs[0].Start[0] == 1000 && r.Issues.Count(i => i.Code == "unsupported_tail" && i.Level == "pending") == 2,
              "配管：部分重疊 → 吊架線長 = 重疊段；重疊外尾段列待確認");
        // 單管
        r = ConduitHangers.Pair(new[] { K("1", new[] { 0.0, 0, 0 }, new[] { 6000.0, 0, 0 }) }, cc);
        Check(r.Pairs.Count == 0 && r.Issues.Single().Code == "unpaired" && r.Issues[0].Level == "pending", "配管：單管不放置，列待確認");
        // 不平行（2°）
        var th = 2.0 * Math.PI / 180;
        r = ConduitHangers.Pair(new[] { K("1", new[] { 0.0, 0, 0 }, new[] { 6000.0, 0, 0 }), K("2", new[] { 0.0, 150, 0 }, new[] { 6000 * Math.Cos(th), 150 + 6000 * Math.Sin(th), 0 }) }, cc);
        Check(r.Pairs.Count == 0 && r.Issues.Count == 2 && r.Issues.All(i => i.Code == "unpaired" && i.Detail.Contains("not_parallel")), "配管：不平行不配對");
        // 不同高程
        r = ConduitHangers.Pair(new[] { K("1", new[] { 0.0, 0, 0 }, new[] { 6000.0, 0, 0 }), K("2", new[] { 0.0, 150, 5 }, new[] { 6000.0, 150, 5 }) }, cc);
        Check(r.Pairs.Count == 0 && r.Issues.All(i => i.Detail.Contains("elevation_mismatch")), "配管：不同高程不配對");
        // 管徑不符
        r = ConduitHangers.Pair(new[] { K("1", new[] { 0.0, 0, 0 }, new[] { 6000.0, 0, 0 }), K("2", new[] { 0.0, 150, 0 }, new[] { 6000.0, 150, 0 }, 50, 50) }, cc);
        Check(r.Pairs.Count == 0 && r.Issues.Any(i => i.Code == "diameter_not_allowed" && i.PieceId == "2") && r.Issues.Any(i => i.Code == "unpaired" && i.PieceId == "1"),
              "配管：管徑不在允許清單不放置");
        // 外緣超出橫擔：中心距 230 + 75 = 305 > 300
        r = ConduitHangers.Pair(new[] { K("1", new[] { 0.0, 0, 0 }, new[] { 6000.0, 0, 0 }), K("2", new[] { 0.0, 230, 0 }, new[] { 6000.0, 230, 0 }) }, cc);
        Check(r.Pairs.Count == 0 && r.Issues.Any(i => i.Code == "exceeds_crossbar" && i.PieceId == "1+2"), "配管：外緣超出 300 橫擔 → 待確認");
        r = ConduitHangers.Pair(new[] { K("1", new[] { 0.0, 0, 0 }, new[] { 6000.0, 0, 0 }), K("2", new[] { 0.0, 225, 0 }, new[] { 6000.0, 225, 0 }) }, cc);
        Check(r.Pairs.Count == 1 && r.Pairs[0].OuterWidthMm == 300, "配管：外緣剛好 300 可配對");
        // 重疊（中心距 < 外徑）
        r = ConduitHangers.Pair(new[] { K("1", new[] { 0.0, 0, 0 }, new[] { 6000.0, 0, 0 }), K("2", new[] { 0.0, 50, 0 }, new[] { 6000.0, 50, 0 }) }, cc);
        Check(r.Pairs.Count == 0 && r.Issues.Any(i => i.Code == "too_close"), "配管：兩管重疊（非並排）不配對");
        // 三管並排：曖昧，不猜測
        r = ConduitHangers.Pair(new[] { K("1", new[] { 0.0, 0, 0 }, new[] { 6000.0, 0, 0 }), K("2", new[] { 0.0, 100, 0 }, new[] { 6000.0, 100, 0 }), K("3", new[] { 0.0, 200, 0 }, new[] { 6000.0, 200, 0 }) }, cc);
        Check(r.Pairs.Count == 0 && r.Issues.Count(i => i.Code == "ambiguous_pair") == 3, "配管：可多重配對時全部待確認，不猜測");
        // 斜管
        r = ConduitHangers.Pair(new[] { K("1", new[] { 0.0, 0, 0 }, new[] { 6000.0, 0, 100 }) }, cc);
        Check(r.Issues.Single().Code == "sloped" && r.Issues[0].Level == "unsupported", "配管：斜管不支援");
        // 外徑讀不到 → 用管徑並提示
        r = ConduitHangers.Pair(new[] { K("1", new[] { 0.0, 0, 0 }, new[] { 6000.0, 0, 0 }, 75, null), K("2", new[] { 0.0, 150, 0 }, new[] { 6000.0, 150, 0 }, 75, null) }, cc);
        Check(r.Pairs.Count == 1 && r.Issues.Count(i => i.Code == "outer_diameter_fallback") == 2, "配管：讀不到外徑時改用管徑並提示");

        // 佈置：沿用橋架設定（固定 1500、餘數待確認、無終點支撐）
        var bcfg = HangerConfigReader.Read(BaseCfg(), true);
        r = ConduitHangers.Pair(new[] { K("1", new[] { 0.0, 0, 3000 }, new[] { 6095.483, 0, 3000 }), K("2", new[] { 0.0, 150, 3000 }, new[] { 6095.483, 150, 3000 }) }, cc);
        var lay = Supports.Layout(ConduitHangers.ToPieces(r.Pairs), new List<SupportLink>(), bcfg);
        Check(lay.Supports.Select(s => s.StationMm).SequenceEqual(new[] { 0.0, 1500, 3000, 4500, 6000 }) && lay.Issues.Single().Code == "fixed_remainder"
              && lay.Supports.All(s => s.PieceId == "1+2" && s.PointMm[1] == 75 && s.BearingZMm == 2962.5),
              "配管：固定 1500 間距沿用橋架規則，餘數列待確認");
        var pl = ConduitHangers.Place(lay.Supports[1], bcfg, cc);
        Check(pl.CrossMemberLengthMm == 400 && pl.InsertionPointMm.SequenceEqual(new[] { 1500.0, 75, 2962.5 }) && pl.RotationDeg == 0,
              "配管：不加寬；basis=parameter → 參數 300、實測 = ratio×300+offset");
        Check(HangerPlacer_CrossParam(bcfg, pl.CrossMemberLengthMm) == 300, "配管：放置時寫入族參數值 = 300");
        var pl2 = ConduitHangers.Place(lay.Supports[1], bcfg, Cc(c => c["cross_member"]!["basis"] = "measured_length"));
        Check(pl2.CrossMemberLengthMm == 300 && ConduitHangers.CrossMember(bcfg, Cc(c => c["cross_member"]!["basis"] = "measured_length")).ParamMm == 200,
              "配管：basis=measured_length → 實測 300、參數依校正換算");
        // 設定驗證
        var bad = ConduitConfigReader.Validate(new JsonObject { ["units"] = "mm" });
        foreach (var f in new[] { "supported_object", "base_config", "conduits_per_hanger", "allowed_trade_sizes_mm", "cross_member", "pairing" })
            Check(HangerConfigReader.ErrorFields(bad).Contains(f), $"配管設定缺少欄位被點名：{f}");
        var three = BaseCc(); three["conduits_per_hanger"] = 3;
        Check(HangerConfigReader.ErrorFields(ConduitConfigReader.Validate(three)).Contains("conduits_per_hanger"), "配管設定：v1 只接受 2 支共用");
        bool threw = false;
        try { ConduitConfigReader.Read(new JsonObject()); } catch (InvalidDataException) { threw = true; }
        Check(threw, "配管設定不完整時拒絕執行");

        RunSingles(Check, bcfg);
        if (contractPath != null) RunContract(Check, contractPath);
    }

    // 使用者決定 2026-10-18：單管（未配成對）吊架寬度參數 200、basis parameter、不加寬；配對仍為 300
    static void SetSingle(JsonObject c) => c["cross_member_single"] = new JsonObject { ["value_mm"] = 200.0, ["basis"] = "parameter" };

    static void RunSingles(Action<bool, string> Check, HangerConfig bcfg)
    {
        var sc = Cc(SetSingle);
        Check(Cc().SingleCrossMemberValueMm is null, "單管：未設定 cross_member_single → 沿用舊行為");
        var r = ConduitHangers.Pair(new[] { K("1", new[] { 0.0, 0, 3000 }, new[] { 6095.483, 0, 3000 }) }, sc);
        Check(r.Pairs.Count == 0 && r.Issues.Count == 0 && r.Singles.Count == 1 && r.Singles[0].Id == "1" && r.Singles[0].BearingZMm == 2962.5
              && r.Singles[0].Start.SequenceEqual(new[] { 0.0, 0, 3000 }), "單管：設定後單支配管全長吊掛，承托面 = 管底");
        var lay = Supports.Layout(ConduitHangers.ToPieces(r.Pairs, r.Singles), new List<SupportLink>(), bcfg);
        Check(lay.Supports.Select(s => s.StationMm).SequenceEqual(new[] { 0.0, 1500, 3000, 4500, 6000 }) && lay.Issues.Single().Code == "fixed_remainder"
              && lay.Supports.All(s => s.PointMm[1] == 0), "單管：固定 1500、餘數待確認、吊架中心在管軸");
        var pl = ConduitHangers.Place(lay.Supports[1], bcfg, sc, single: true);
        Check(pl.CrossMemberLengthMm == 300 && ConduitHangers.CrossMember(bcfg, sc, true).ParamMm == 200 && ConduitHangers.CrossMember(bcfg, sc).ParamMm == 300,
              "單管：參數 200（實測 300），配對仍為 300");
        bool threw = false;
        try { ConduitHangers.CrossMember(bcfg, Cc(), true); } catch (InvalidOperationException) { threw = true; }
        Check(threw, "單管：未設定時拒絕計算單管橫擔");
        r = ConduitHangers.Pair(new[] { K("1", new[] { 0.0, 0, 0 }, new[] { 6000.0, 0, 0 }), K("2", new[] { 0.0, 100, 0 }, new[] { 6000.0, 100, 0 }), K("3", new[] { 0.0, 400, 0 }, new[] { 6000.0, 400, 0 }) }, sc);
        Check(r.Pairs.Single().Id == "1+2" && r.Singles.Single().Id == "3" && r.Issues.Count == 0, "單管：先配對，剩下有效配管成為單管");
        r = ConduitHangers.Pair(new[] { K("1", new[] { 0.0, 0, 0 }, new[] { 6000.0, 0, 0 }), K("2", new[] { 0.0, 100, 0 }, new[] { 6000.0, 100, 0 }), K("3", new[] { 0.0, 200, 0 }, new[] { 6000.0, 200, 0 }, 75, 80) }, sc);
        Check(r.Pairs.Single().Id == "1+2" && r.Singles.Count == 0 && r.Issues.Single().Code == "single_conflict" && r.Issues[0].PieceId == "3",
              "單管：橫擔可能與鄰近配對吊架重疊 → 待確認");
        r = ConduitHangers.Pair(new[] { K("1", new[] { 0.0, 0, 0 }, new[] { 6000.0, 0, 0 }), K("2", new[] { 0.0, 100, 0 }, new[] { 6000.0, 100, 0 }), K("3", new[] { 0.0, 200, 0 }, new[] { 6000.0, 200, 0 }) }, sc);
        Check(r.Pairs.Count == 0 && r.Singles.Count == 0 && r.Issues.Count(i => i.Code == "ambiguous_pair") == 3, "單管：三管曖昧不轉單管");
        r = ConduitHangers.Pair(new[] { K("1", new[] { 0.0, 0, 0 }, new[] { 6000.0, 0, 0 }), K("2", new[] { 0.0, 50, 0 }, new[] { 6000.0, 50, 0 }) }, sc);
        Check(r.Singles.Count == 0 && r.Issues.Count(i => i.Code == "single_conflict") == 2 && r.Issues.Any(i => i.Code == "too_close"), "單管：兩管重疊 → 都不吊、待確認");
        r = ConduitHangers.Pair(new[] { K("1", new[] { 0.0, 0, 0 }, new[] { 6000.0, 0, 0 }), K("2", new[] { 0.0, 230, 0 }, new[] { 6000.0, 230, 0 }) }, sc);
        Check(r.Singles.Count == 2 && r.Issues.Count == 0, "單管：中心距 230 ≥ 200 → 兩支各自單管");
        r = ConduitHangers.Pair(new[] { K("1", new[] { 0.0, 0, 0 }, new[] { 6000.0, 0, 0 }), K("2", new[] { 6500.0, 150, 0 }, new[] { 1000.0, 150, 0 }) }, sc);
        Check(r.Singles.Count == 0 && r.Issues.Count(i => i.Code == "unsupported_tail") == 2, "單管：配對尾段不轉單管，仍待確認");
        Check(HangerConfigReader.ErrorFields(ConduitConfigReader.Validate(BaseCc().Also(c => c["cross_member_single"] = new JsonObject { ["value_mm"] = -1.0 })))
              .SequenceEqual(new[] { "cross_member_single.value_mm", "cross_member_single.basis" }), "單管設定驗證");
        // 去重鍵與跨組合
        var pk = ConduitHangers.PairKey("B", "A"); var sk = ConduitHangers.SingleKey("A");
        Check(pk == "conduit-pair:A|B" && sk == "conduit-single:A" && ConduitHangers.KeyConduits(pk).SequenceEqual(new[] { "A", "B" })
              && ConduitHangers.KeyConduits("tray").Length == 0, "單管：鍵與配對鍵不同");
        var ex = new List<ExistingHanger>
        {
            new("h1", "tool", pk, 0, null, 0), new("h2", "tool", sk, 0, null, 0), new("h3", "other", null, null, null, null),
            new("h4", "tool", "conduit-single:C", 0, null, 0), new("h5", "tool", "tray-uid", 0, null, 0),
        };
        Check(ConduitHangers.CoveringHangers(sk, ex).SequenceEqual(new[] { "h1" }) && ConduitHangers.CoveringHangers(pk, ex).SequenceEqual(new[] { "h2" })
              && ConduitHangers.CoveringHangers(ConduitHangers.PairKey("A", "C"), ex).SequenceEqual(new[] { "h1", "h2", "h4" })
              && ConduitHangers.CoveringHangers("tray-uid", ex).Count == 0, "單管：同一配管已有其他組合吊架 → 被點名，不重複吊掛");
    }

    static JsonObject Also(this JsonObject o, Action<JsonObject> f) { f(o); return o; }

    // 與 Revit 端 HangerPlacer.CrossParamMm 相同的換算（核心不引用 Revit 專案）
    static double HangerPlacer_CrossParam(HangerConfig cfg, double lengthMm) => Math.Round((lengthMm - cfg.CrossOffsetMm) / cfg.CrossRatio, 6);

    static double[] Arr(JsonNode? n) => n!.AsArray().Select(x => x!.GetValue<double>()).ToArray();
    static double? NumOrNull(JsonNode? n) => n is null ? null : n.GetValue<double>();
    static bool Near(double a, double b) => Math.Abs(a - b) <= 2e-3;
    static bool Near3(double[] a, double[] b) => a.Length == b.Length && a.Zip(b).All(z => Near(z.First, z.Second));

    static void RunContract(Action<bool, string> Check, string path)
    {
        var root = JsonNode.Parse(File.ReadAllText(path))!;
        int n = 0;
        foreach (var c in root["conduit_cases"]!.AsArray())
        {
            n++;
            var name = c!["name"]!.GetValue<string>();
            var expFields = c["config_errors"]!.AsArray().Select(x => x!.GetValue<string>()).ToList();
            var gotFields = HangerConfigReader.ErrorFields(ConduitConfigReader.Validate(c["ccfg"]?.DeepClone()));
            Check(expFields.SequenceEqual(gotFields), $"配管契約[{name}] 設定驗證欄位一致: py={string.Join(",", expFields)} cs={string.Join(",", gotFields)}");
            var exp = c["expect"];
            if (exp is null) continue;
            var cc = ConduitConfigReader.Read(c["ccfg"]!.DeepClone());
            var conduits = c["conduits"]!.AsArray().Select(k => new ConduitInput
            {
                Id = k!["id"]!.GetValue<string>(), Start = Arr(k["start"]), End = Arr(k["end"]),
                TradeSizeMm = NumOrNull(k["trade_size_mm"]), OuterDiameterMm = NumOrNull(k["outer_diameter_mm"]),
            }).ToList();
            var got = ConduitHangers.Pair(conduits, cc);
            var ep = exp["pairs"]!.AsArray();
            bool same = ep.Count == got.Pairs.Count;
            for (int i = 0; same && i < ep.Count; i++)
            {
                var e = ep[i]!; var g = got.Pairs[i];
                same = e["id"]!.GetValue<string>() == g.Id && e["a"]!.GetValue<string>() == g.A && e["b"]!.GetValue<string>() == g.B
                       && Near3(Arr(e["start"]), g.Start) && Near3(Arr(e["end"]), g.End) && Near(e["overlap_mm"]!.GetValue<double>(), g.OverlapMm)
                       && Near(e["center_distance_min_mm"]!.GetValue<double>(), g.CenterDistanceMinMm) && Near(e["center_distance_max_mm"]!.GetValue<double>(), g.CenterDistanceMaxMm)
                       && Near(e["outer_width_mm"]!.GetValue<double>(), g.OuterWidthMm) && Near(e["outer_diameter_mm"]!.GetValue<double>(), g.OuterDiameterMm)
                       && Near(e["bearing_z_mm"]!.GetValue<double>(), g.BearingZMm);
            }
            Check(same, $"配管契約[{name}] 配對與 Python 一致（{ep.Count}/{got.Pairs.Count}）");
            var ei = exp["issues"]!.AsArray();
            bool si = ei.Count == got.Issues.Count;
            for (int i = 0; si && i < ei.Count; i++)
            {
                var e = ei[i]!; var g = got.Issues[i];
                si = e["level"]!.GetValue<string>() == g.Level && e["code"]!.GetValue<string>() == g.Code && e["piece_id"]!.GetValue<string>() == g.PieceId
                     && Near3(Arr(e["location_mm"]), g.LocationMm);
            }
            Check(si, $"配管契約[{name}] 問題清單與 Python 一致（{ei.Count}/{got.Issues.Count}）");
            var es1 = exp["singles"]!.AsArray();
            bool ss = es1.Count == got.Singles.Count;
            for (int i = 0; ss && i < es1.Count; i++)
            {
                var e = es1[i]!; var g = got.Singles[i];
                ss = e["id"]!.GetValue<string>() == g.Id && e["conduit"]!.GetValue<string>() == g.Conduit && Near3(Arr(e["start"]), g.Start) && Near3(Arr(e["end"]), g.End)
                     && Near(e["length_mm"]!.GetValue<double>(), g.LengthMm) && Near(e["outer_diameter_mm"]!.GetValue<double>(), g.OuterDiameterMm)
                     && Near(e["outer_width_mm"]!.GetValue<double>(), g.OuterWidthMm) && Near(e["bearing_z_mm"]!.GetValue<double>(), g.BearingZMm);
            }
            Check(ss, $"配管契約[{name}] 單管與 Python 一致（{es1.Count}/{got.Singles.Count}）");
            var sid = new HashSet<string>(got.Singles.Select(x => x.Id));
            if (c["base_cfg"] is JsonObject bc && c["layout"] is JsonObject lay)
            {
                var bcfg = HangerConfigReader.Read(bc.DeepClone(), true);
                var gl = Supports.Layout(ConduitHangers.ToPieces(got.Pairs, got.Singles), new List<SupportLink>(), bcfg);
                var es = lay["supports"]!.AsArray();
                bool sl = es.Count == gl.Supports.Count && lay["issues"]!.AsArray().Count == gl.Issues.Count;
                for (int i = 0; sl && i < es.Count; i++)
                {
                    var e = es[i]!; var g = gl.Supports[i];
                    sl = e["piece_id"]!.GetValue<string>() == g.PieceId && Near(e["station_mm"]!.GetValue<double>(), g.StationMm)
                         && Near3(Arr(e["point_mm"]), g.PointMm) && Near(e["direction_deg"]!.GetValue<double>(), g.DirectionDeg)
                         && Near(e["bearing_z_mm"]!.GetValue<double>(), g.BearingZMm);
                }
                Check(sl, $"配管契約[{name}] 支撐點與 Python 一致（{es.Count}/{gl.Supports.Count}）");
                var pls = c["placements"]!.AsArray();
                bool sp = pls.Count == gl.Supports.Count;
                for (int i = 0; sp && i < pls.Count; i++)
                {
                    var e = pls[i]!; bool one = sid.Contains(gl.Supports[i].PieceId);
                    var g = ConduitHangers.Place(gl.Supports[i], bcfg, cc, one);
                    sp = Near3(Arr(e["insertion_point_mm"]), g.InsertionPointMm) && Near3(Arr(e["bearing_point_mm"]), g.BearingPointMm)
                         && Near(e["rotation_deg"]!.GetValue<double>(), g.RotationDeg) && Near(e["cross_member_length_mm"]!.GetValue<double>(), g.CrossMemberLengthMm)
                         && Near(e["cross_member_param_mm"]!.GetValue<double>(), ConduitHangers.CrossMember(bcfg, cc, one).ParamMm);
                }
                Check(sp, $"配管契約[{name}] 族插入點/旋轉/橫擔與 Python 一致");
            }
        }
        Check(n > 0, $"配管契約案例非空（{n}）");
    }
}
