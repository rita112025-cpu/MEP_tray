using System.Globalization;
using System.Text.Json.Nodes;
using Autodesk.Revit.Attributes;
using Autodesk.Revit.DB;
using Autodesk.Revit.DB.Electrical;
using Autodesk.Revit.UI;
using MepTray.Core;
using Units = MepTray.Core.Units;

namespace MepTray.Revit;

/// <summary>
/// 配管吊架（第一階段）：兩支並排 Conduit 共用一支吊架。配對/中線/外緣檢查在 MepTray.Core.ConduitHangers；
/// 族、校正、間距（固定間距與餘數待確認）、去重、單支 SubTransaction 回滾、整批一次 Undo 全部沿用橋架吊架流程（HangerPlacer）。
/// 不修改、不刪除、不接管非本工具建立的吊架；不建立、不移動配管。
/// </summary>
public static class ConduitHangerPlacer
{
    public const string ToolVersion = "conduit-hangers-v1";

    static double[] Mm(XYZ p) => new[] { Units.FeetToMm(p.X), Units.FeetToMm(p.Y), Units.FeetToMm(p.Z) };

    /// <summary>conduit_hanger_config.json → (配管設定, 橋架基礎設定, 基礎設定實際路徑)。基礎設定以既有 HangerConfigReader 驗證（行為不變）。</summary>
    public static (ConduitConfig Conduit, HangerConfig Base, string BasePath) LoadConfig(string path)
    {
        var cc = ConduitConfigReader.Read(JsonNode.Parse(File.ReadAllText(path)));
        var basePath = Path.IsPathRooted(cc.BaseConfig) ? cc.BaseConfig : Path.GetFullPath(Path.Combine(Path.GetDirectoryName(path) ?? "", cc.BaseConfig));
        if (!File.Exists(basePath)) throw new InvalidDataException($"base_config：找不到橋架吊架設定檔 {basePath}");
        return (cc, HangerPlacer.LoadConfig(basePath), basePath);
    }

    /// <summary>選取的 Conduit → 核心輸入。管徑取 BuiltInParameter.RBS_CONDUIT_DIAMETER_PARAM（"Diameter(Trade Size)"），
    /// 外徑取 RBS_CONDUIT_OUTER_DIAM_PARAM（"Outside Diameter"）；不依類型名稱推測用途。</summary>
    public static (List<ConduitInput> Inputs, Dictionary<string, Conduit> Conduits, List<string> Problems) ReadConduits(Document doc, IEnumerable<ElementId> ids)
    {
        var inputs = new List<ConduitInput>();
        var map = new Dictionary<string, Conduit>();
        var problems = new List<string>();
        foreach (var c in ids.Select(doc.GetElement).OfType<Conduit>().OrderBy(c => c.Id.Value))
        {
            var key = c.Id.Value.ToString(CultureInfo.InvariantCulture);
            if (c.Location is not LocationCurve lc || lc.Curve is not Line line) { problems.Add($"配管 {key} 不是直線段，未處理"); continue; }
            var dp = c.get_Parameter(BuiltInParameter.RBS_CONDUIT_DIAMETER_PARAM);
            var op = c.get_Parameter(BuiltInParameter.RBS_CONDUIT_OUTER_DIAM_PARAM);
            inputs.Add(new ConduitInput
            {
                Id = key, Start = Mm(line.GetEndPoint(0)), End = Mm(line.GetEndPoint(1)),
                TradeSizeMm = dp is { HasValue: true, StorageType: StorageType.Double } ? Units.FeetToMm(dp.AsDouble()) : null,
                OuterDiameterMm = op is { HasValue: true, StorageType: StorageType.Double } ? Units.FeetToMm(op.AsDouble()) : null,
            });
            map[key] = c;
        }
        return (inputs, map, problems);
    }

    /// <summary>配對的穩定識別（寫入 Extensible Storage 的 TrayUniqueId 欄位，供重跑去重）：兩支配管 UniqueId 排序後串接。</summary>
    public static string PairKey(Conduit a, Conduit b) => ConduitHangers.PairKey(a.UniqueId, b.UniqueId);

    // 計畫（不改模型）
    public static HangerRunReport Plan(Document doc, IEnumerable<ElementId> ids, HangerConfig cfg, ConduitConfig cc, out HangerPlacer.Resolved? resolved)
    {
        var rep = new HangerRunReport
        {
            BatchId = $"{DateTime.Now:yyyyMMdd-HHmmss}-{Guid.NewGuid():N}"[..24], Entry = "conduit_selection",
            Coordinates = "Revit 內部座標（mm）；TrayPoint=兩管中線，Bearing=管底（中心−外徑/2），Insertion=族插入點；TrayId/TrayUniqueId=配管配對",
        };
        var (inputs, conduits, problems) = ReadConduits(doc, ids);
        rep.Notes.AddRange(problems);
        resolved = HangerPlacer.Preflight(doc, cfg, rep.PreflightErrors);
        if (inputs.Count == 0) rep.PreflightErrors.Add("沒有可處理的 Conduit");
        var pr = ConduitHangers.Pair(inputs, cc);
        rep.LayoutIssues.AddRange(pr.Issues);
        var keys = pr.Pairs.ToDictionary(p => p.Id, p => PairKey(conduits[p.A], conduits[p.B]));
        foreach (var sg in pr.Singles) keys[sg.Id] = ConduitHangers.SingleKey(conduits[sg.Conduit].UniqueId);
        var singleIds = new HashSet<string>(pr.Singles.Select(x => x.Id));
        foreach (var p in pr.Pairs)
            rep.Notes.Add($"配對 {p.Id}：重疊 {p.OverlapMm:F0} mm，中心距 {p.CenterDistanceMinMm:F1}–{p.CenterDistanceMaxMm:F1} mm，外緣總寬 {p.OuterWidthMm:F1} mm ≤ 橫擔 {cc.CrossMemberValueMm:F0} mm");
        foreach (var sg in pr.Singles)
            rep.Notes.Add($"單管 {sg.Id}：長 {sg.LengthMm:F0} mm，外徑 {sg.OuterDiameterMm:F1} mm（吊架中心在管軸）");
        var lay = Supports.Layout(ConduitHangers.ToPieces(pr.Pairs, pr.Singles), new List<SupportLink>(), cfg);
        rep.LayoutIssues.AddRange(lay.Issues);
        var plan = lay.Supports.Select(s => new PlannedKey(keys[s.PieceId], s.StationMm, s.PointMm, s.DirectionDeg)).ToList();
        var existing = HangerPlacer.ReadExisting(doc, cfg.FamilyName);
        var cls = Supports.Classify(plan, existing, cfg.DedupeToleranceMm, cfg.DedupeAngleToleranceDeg);
        // 同一支配管已被其他組合（配對↔單管、或與另一支配對）的本工具吊架承托 → 待確認，不重複吊掛
        for (int i = 0; i < cls.Count; i++)
        {
            if (cls[i].Status != "new") continue;
            var cover = ConduitHangers.CoveringHangers(plan[i].TrayKey, existing);
            if (cover.Count > 0) cls[i] = new Classification("pending", cover, "該配管已有其他組合（配對／單管）的本工具吊架，不重複吊掛");
        }
        for (int i = 0; i < lay.Supports.Count; i++)
        {
            var s = lay.Supports[i];
            var pl = ConduitHangers.Place(s, cfg, cc, singleIds.Contains(s.PieceId));
            rep.Items.Add(new HangerItem
            {
                SupportId = s.Id, TrayId = s.PieceId, TrayUniqueId = keys[s.PieceId], StationMm = s.StationMm,
                TrayPointMm = s.PointMm, BearingPointMm = pl.BearingPointMm, InsertionPointMm = pl.InsertionPointMm, RotationDeg = pl.RotationDeg,
                TrayDirectionDeg = s.DirectionDeg, CrossMemberLengthMm = pl.CrossMemberLengthMm, RodLengthMm = pl.RodLengthMm, Reason = s.Reason,
                Status = cls[i].Status,
                Detail = cls[i].Status == "new" ? "" : cls[i].Reason + (cls[i].Match.Count > 0 ? "（元素 " + string.Join(",", cls[i].Match) + "）" : ""),
            });
        }
        var (len, param) = ConduitHangers.CrossMember(cfg, cc);
        rep.Notes.Add($"橫擔：族參數「{cfg.CrossMemberLength?.Name}」= {param:F1} mm、依校正實測長度 {len:F1} mm（cross_member.basis={cc.CrossMemberBasis}；不加寬，忽略橋架設定的 widen_left/right）");
        if (cc.SingleCrossMemberValueMm is not null)
        {
            var (sl, sp) = ConduitHangers.CrossMember(cfg, cc, single: true);
            rep.Notes.Add($"單管橫擔：族參數「{cfg.CrossMemberLength?.Name}」= {sp:F1} mm、依校正實測長度 {sl:F1} mm（cross_member_single.basis={cc.SingleCrossMemberBasis}；不加寬）");
        }
        else rep.Notes.Add("未設定 cross_member_single：單管列為待確認，不放置");
        HangerPlacer.ApplyTestSwitch(rep, cfg);   // 測試開關寫在 base_config（橋架設定）
        HangerPlacer.ApplyInstanceParams(rep, cfg);   // instance_params 同樣寫在 base_config（橋架設定）
        if (rep.PreflightErrors.Count > 0) rep.Outcome = "失敗";
        return rep;
    }
}

/// <summary>「選取配管補吊架」：對目前選取的 Conduit，兩支並排者共用一支吊架；設定 cross_member_single 時，未配對且不曖昧的單管單獨吊掛；
/// 其餘列為待確認，不放置。不建立、不移動配管。</summary>
[Transaction(TransactionMode.Manual)]
public class AddConduitHangersCommand : IExternalCommand
{
    public Result Execute(ExternalCommandData commandData, ref string message, ElementSet elements)
    {
        var uidoc = commandData.Application.ActiveUIDocument;
        var doc = uidoc?.Document;
        if (doc is null) { message = "請先開啟 Revit 專案"; return Result.Failed; }
        var ids = uidoc!.Selection.GetElementIds().Where(id => doc.GetElement(id) is Conduit).ToList();
        if (ids.Count == 0) { message = "請先選取要補吊架的 Conduit（兩支並排共用一支吊架）"; return Result.Failed; }
        var dlg = new Microsoft.Win32.OpenFileDialog { Filter = "MEP tray 配管吊架設定 (*.json)|*.json", Title = "選取配管吊架設定 JSON（conduit_hanger_config.json；族/校正/間距沿用其 base_config）" };
        if (dlg.ShowDialog() != true) return Result.Cancelled;
        var cfgPath = dlg.FileName;
        ConduitConfig cc; HangerConfig cfg; string basePath;
        try { (cc, cfg, basePath) = ConduitHangerPlacer.LoadConfig(cfgPath); }
        catch (Exception ex) when (ex is InvalidDataException or System.Text.Json.JsonException or IOException)
        { message = ex.Message; TaskDialog.Show("MEP Tray — 配管吊架設定", ex.Message); return Result.Failed; }

        var rep = ConduitHangerPlacer.Plan(doc, ids, cfg, cc, out var res);
        rep.Notes.Add($"基礎設定：{basePath}");
        if (!HangerRun.ConfirmPreview(rep) || res == null || rep.PreflightErrors.Count > 0)
        {
            rep.Outcome = rep.PreflightErrors.Count > 0 ? "失敗" : "已取消";
            HangerRun.WriteReport(rep, cfgPath);
            return rep.PreflightErrors.Count > 0 ? Result.Failed : Result.Cancelled;
        }
        using var tg = new TransactionGroup(doc, "MEP Tray 選取配管補吊架");
        tg.Start();
        try
        {
            HangerRun.Place(doc, rep, cfg, res, ConduitHangerPlacer.ToolVersion);
            if (rep.Count("created") == 0) { tg.RollBack(); rep.Notes.Add("沒有任何吊架建立成功，整批已回滾"); }
            else tg.Assimilate();
        }
        catch (Exception ex)
        {
            if (tg.GetStatus() == TransactionStatus.Started) tg.RollBack();
            rep.Outcome = "失敗";
            rep.Notes.Add($"整批回滾：{ex.GetType().Name}: {ex.Message}");
        }
        HangerRun.WriteReport(rep, cfgPath);
        TaskDialog.Show("MEP Tray 配管補吊架結果", rep.Summary());
        return rep.Outcome is "完成" or "部分完成" ? Result.Succeeded : Result.Failed;
    }
}
