using System.Globalization;
using System.Text.Encodings.Web;
using System.Text.Json;
using System.Text.Json.Nodes;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Interop;
using Autodesk.Revit.Attributes;
using Autodesk.Revit.DB;
using Autodesk.Revit.DB.Electrical;
using Autodesk.Revit.UI;
using Autodesk.Revit.UI.Selection;
using MepTray.Core;
using Units = MepTray.Core.Units;
using RvtTransaction = Autodesk.Revit.DB.Transaction;
using WpfGrid = System.Windows.Controls.Grid;
using ComboBox = System.Windows.Controls.ComboBox;
using TextBox = System.Windows.Controls.TextBox;
using Button = System.Windows.Controls.Button;
using TextBlock = System.Windows.Controls.TextBlock;

namespace MepTray.Revit;

/// <summary>
/// 「校正目標吊架」：在一段水平 Cable Tray 中點放 1 支測試吊架供目視檢查；量測候選參數 +Δ 的幾何效果；
/// 取消＝回滾校正建立的一切（含本命令載入的族）；確認＝回滾測試吊架與量測，只把校正值與族身分寫入設定檔。
/// 原類型不修改（測試用工具類型 "(MEPTRAY CAL)"，隨測試回滾）；原族檔不開啟、不儲存。
/// </summary>
[Transaction(TransactionMode.Manual)]
public class CalibrateHangerCommand : IExternalCommand
{
    static readonly JsonSerializerOptions Json = new() { WriteIndented = true, Encoder = JavaScriptEncoder.UnsafeRelaxedJsonEscaping };
    const double DeltaMm = 100.0;

    sealed class TrayFilter : ISelectionFilter
    {
        public bool AllowElement(Element e) => e is CableTray;
        public bool AllowReference(Reference r, XYZ p) => false;
    }

    static double? Num(JsonNode? n) => n is JsonValue v && v.TryGetValue<double>(out var d) && double.IsFinite(d) ? d : null;

    public Result Execute(ExternalCommandData commandData, ref string message, ElementSet elements)
    {
        var uiapp = commandData.Application;
        var uidoc = uiapp.ActiveUIDocument;
        var doc = uidoc?.Document;
        if (doc is null || doc.IsFamilyDocument) { message = "請在專案文件中執行"; return Result.Failed; }

        // 1. 測試用橋架（水平）
        var tray = uidoc!.Selection.GetElementIds().Select(doc.GetElement).OfType<CableTray>().FirstOrDefault();
        if (tray == null)
        {
            try { tray = doc.GetElement(uidoc.Selection.PickObject(ObjectType.Element, new TrayFilter(), "選取一段水平 Cable Tray（校正用）")) as CableTray; }
            catch (Autodesk.Revit.Exceptions.OperationCanceledException) { return Result.Cancelled; }
        }
        if (tray?.Location is not LocationCurve lc || lc.Curve is not Line line) { message = "請選取直線 Cable Tray"; return Result.Failed; }
        XYZ a = line.GetEndPoint(0), b = line.GetEndPoint(1);
        if (Math.Abs(Units.FeetToMm(b.Z - a.Z)) > Supports.GeomTolMm) { message = "校正需要水平橋架（此段有高差）"; TaskDialog.Show("校正目標吊架", message); return Result.Failed; }
        var widthMm = Units.FeetToMm(tray.get_Parameter(BuiltInParameter.RBS_CABLETRAY_WIDTH_PARAM).AsDouble());
        var heightMm = Units.FeetToMm(tray.get_Parameter(BuiltInParameter.RBS_CABLETRAY_HEIGHT_PARAM).AsDouble());
        var mid = (a + b) / 2;
        var dirDeg = Supports.Norm360(Math.Atan2(b.Y - a.Y, b.X - a.X) * 180 / Math.PI);
        var bottomMm = Units.FeetToMm(mid.Z) - heightMm / 2;

        // 2. 設定（需先由「讀取目標吊架族」寫入 target_family）
        var cfgPath = HangerRun.PickConfig();
        if (cfgPath == null) return Result.Cancelled;
        JsonObject cfg;
        try { cfg = JsonNode.Parse(File.ReadAllText(cfgPath))!.AsObject(); }
        catch (Exception ex) { message = $"設定檔無法讀取：{ex.Message}"; TaskDialog.Show("校正目標吊架", message); return Result.Failed; }
        if (cfg["target_family"] is not JsonObject tf || tf["status"]?.GetValue<string>() != "ok")
        { message = "設定檔沒有成功的 target_family；請先執行「讀取目標吊架族」並寫入此設定檔"; TaskDialog.Show("校正目標吊架", message); return Result.Failed; }
        var famName = tf["family_name"]!.GetValue<string>();
        var typeName = tf["type_name"]!.GetValue<string>();
        var rfa = tf["family_file"]!["path"]!.GetValue<string>();
        var mode = tf["placement_mode_derived"]?.GetValue<string>();
        if (tf["type_exists"]?.GetValue<bool>() != true || mode == null)
        { message = "目標族缺少類型或放置方式不支援（見 target_family.problems）"; TaskDialog.Show("校正目標吊架", message); return Result.Failed; }
        try
        {
            var now = HangerCalibration.FileIdentity(rfa);
            if (now["sha256"]!.GetValue<string>() != tf["family_file"]!["sha256"]!.GetValue<string>())
            { message = "族檔在讀取後已變更；請重新執行「讀取目標吊架族」"; TaskDialog.Show("校正目標吊架", message); return Result.Failed; }
        }
        catch (Exception ex) when (ex is IOException or UnauthorizedAccessException)
        { message = $"無法讀取族檔：{ex.Message}"; TaskDialog.Show("校正目標吊架", message); return Result.Failed; }
        var cands = tf["parameters"]!.AsArray()
            .Where(p => p!["is_length"]!.GetValue<bool>() && p["adjustable"]!.GetValue<bool>())
            .Select(p => (Name: p!["name"]!.GetValue<string>(), Scope: p["scope"]!.GetValue<string>())).ToList();
        if (cands.Count == 0) { message = "目標族沒有可調整的長度參數，無法對應橫擔長度"; TaskDialog.Show("校正目標吊架", message); return Result.Failed; }
        double widenL = Num(cfg["widen_left_mm"]) ?? 0, widenR = Num(cfg["widen_right_mm"]) ?? 0;
        var rodFixed = cfg["rod_length"]?["mode"]?.GetValue<string>() == "fixed" ? Num(cfg["rod_length"]?["value_mm"]) : null;

        var sym = HangerCalibration.FindSymbol(doc, famName, typeName);
        bool loadHere = false;
        if (sym == null)
        {
            var ask = new TaskDialog("校正目標吊架") { MainInstruction = $"專案中尚未載入 {famName}", MainContent = $"要從下列檔案載入嗎？（取消校正時一併回滾）\n{rfa}", CommonButtons = TaskDialogCommonButtons.Cancel };
            ask.AddCommandLink(TaskDialogCommandLinkId.CommandLink1, "載入並繼續");
            if (ask.Show() != TaskDialogResult.CommandLink1) return Result.Cancelled;
            loadHere = true;
        }

        using var group = new TransactionGroup(doc, "MEP Tray 校正目標吊架");
        group.Start();
        TransactionGroup? trial = null;
        var warnings = new List<string>();
        try
        {
            if (loadHere)
            {
                using var tx = HangerPlacer.StartTx(doc, "載入目標吊架族", warnings);
                if (!doc.LoadFamily(rfa, out _)) throw new InvalidOperationException("LoadFamily 回傳 false");
                tx.Commit();
                sym = HangerCalibration.FindSymbol(doc, famName, typeName) ?? throw new InvalidOperationException($"載入後找不到 {famName} / {typeName}（族名稱可能與檔名不同）");
            }
            var apiPlacement = sym!.Family.FamilyPlacementType.ToString();
            if (TargetFamilyCommand.DerivedMode(apiPlacement) != mode) throw new InvalidOperationException($"專案中族的放置方式 {apiPlacement} 與讀取結果不符；請重新讀取");

            FamilyInstance? test = null;
            HangerCalibration.Extents? baseEx = null;
            List<HangerCalibration.Effect>? effects = null;
            var win = new CalibrationWindow(
                $"族：{famName}  類型：{typeName}  放置方式：{apiPlacement}（{mode}）\n族檔：{rfa}\n" +
                $"測試橋架：{tray.Id}  寬 {widthMm:0.#} × 高 {heightMm:0.#} mm  方向 {dirDeg:0.##}°  底面 {bottomMm:0.#} mm\n" +
                $"測試橫擔長度（目標）：寬度 + 左 {widenL} + 右 {widenR} = {widthMm + widenL + widenR:0.#} mm",
                cands.Select(c => $"{c.Name}（{c.Scope}）").ToList(), Num(cfg["orientation_correction_deg"]), Num(cfg["insertion_to_bearing_mm"]));
            new WindowInteropHelper(win).Owner = uiapp.MainWindowHandle;

            (string Name, string Scope) Cross() => cands[Math.Max(0, win.Cross.SelectedIndex)];
            (string Name, string Scope)? Rod() => win.Rod.SelectedIndex <= 0 ? null : cands[win.Rod.SelectedIndex - 1];

            win.OnPlace = () =>
            {
                if (!win.TryValues(out var corr, out var off)) return;
                trial?.RollBack();
                trial = new TransactionGroup(doc, "校正測試");
                trial.Start();
                using var tx = HangerPlacer.StartTx(doc, "放置測試吊架", warnings);
                var cross = Cross();
                var target = widthMm + widenL + widenR;
                var model = win.CrossModel;
                var pval = model is { } m ? (target - m.Offset) / m.Ratio : target;
                var calName = sym.Name + HangerCalibration.CalTypeSuffix;
                var t = HangerCalibration.FindSymbol(doc, famName, calName) ?? (FamilySymbol)sym.Duplicate(calName);
                if (!t.IsActive) t.Activate();
                if (cross.Scope == "type") SetMm(t, cross.Name, pval);
                if (rodFixed is double rv && Rod() is { } rp && rp.Scope == "type") SetMm(t, rp.Name, rv);
                doc.Regenerate();
                var ins = new XYZ(mid.X, mid.Y, Units.MmToFeet(bottomMm - off));
                var rot = Supports.Norm360(dirDeg + corr);
                test = HangerPlacer.PlaceInstance(doc, t, mode, ins, rot, new Dictionary<long, SketchPlane>());
                if (cross.Scope == "instance") SetMm(test, cross.Name, pval);
                if (rodFixed is double rv2 && Rod() is { } rp2 && rp2.Scope == "instance") SetMm(test, rp2.Name, rv2);
                doc.Regenerate();
                tx.Commit();
                baseEx = HangerCalibration.Measure(test);
                var lp = (LocationPoint)test.Location;
                var back = lp.Point;
                var s = $"測試吊架 {test.Id}：插入點讀回 ({Units.FeetToMm(back.X):0.0}, {Units.FeetToMm(back.Y):0.0}, {Units.FeetToMm(back.Z):0.0}) mm，" +
                        $"旋轉讀回 {Supports.Norm360(lp.Rotation * 180 / Math.PI):0.##}°（目標 {rot:0.##}°）\n" +
                        $"{cross.Name} 設為 {pval:0.#} mm" + (model is null ? "（尚未量測參數效果，直接設成目標長度）" : $"（依量測：範圍 = {model.Value.Ratio:0.###}×參數 + {model.Value.Offset:0.#}）") + "\n";
                if (baseEx != null)
                {
                    s += $"局部範圍（原點＝插入點，mm）：X {baseEx.Min[0]:0.0}～{baseEx.Max[0]:0.0}（{baseEx.Size(0):0.0}）、Y {baseEx.Min[1]:0.0}～{baseEx.Max[1]:0.0}（{baseEx.Size(1):0.0}）、Z {baseEx.Min[2]:0.0}～{baseEx.Max[2]:0.0}\n" +
                         $"朝上水平面（局部 Z，承托面候選；最高者 {baseEx.Max[2]:0.0} 為幾何頂，通常不是承托面）：{string.Join("、", baseEx.UpFacesZ.Select(z => z.ToString("0.0", CultureInfo.InvariantCulture)))}\n" +
                         $"目前承托面設定 {off:0.0} → 世界高程 {bottomMm:0.0}（= 橋架底）；請目視確認橫擔頂面貼齊橋架底、橫擔與橋架垂直。";
                    win.SetBearingCandidates(baseEx.UpFacesZ.Where(z => z < baseEx.Max[2] - 0.05).ToList());
                }
                else s += "測試吊架沒有可量測的實體幾何（請目視檢查）";
                win.Show(s);
                uidoc.ShowElements(new List<ElementId> { test.Id, tray.Id });
                uidoc.RefreshActiveView();
            };
            win.OnMeasure = () =>
            {
                if (test == null) { win.Show("請先放置測試吊架"); return; }
                using var tx = new RvtTransaction(doc, "量測參數效果（將回滾）");
                tx.Start();
                try { effects = HangerCalibration.MeasureEffects(doc, test, cands, DeltaMm); }
                finally { if (tx.GetStatus() == TransactionStatus.Started) tx.RollBack(); }
                var cross = Cross();
                var ce = effects.FirstOrDefault(e => e.Param == cross.Name);
                var txt = "參數效果（+" + DeltaMm + " mm，量測後已回滾）：\n  " + string.Join("\n  ", effects.Select(e => e.Text()));
                if (ce?.DSize is { } ds)
                {
                    var axis = Math.Abs(ds[0]) >= Math.Abs(ds[1]) ? 0 : 1;
                    var ratio = ds[axis] / DeltaMm;
                    if (Math.Abs(ratio) < 0.01) txt += $"\n\n「{cross.Name}」不改變水平範圍 → 不是橫擔長度參數，請改選。";
                    else
                    {
                        var p0 = ReadMm(test, cross);
                        var offset = baseEx!.Size(axis) - ratio * p0;
                        win.CrossModel = (ratio, offset, axis);
                        var sug = axis == 0 ? 90.0 : 0.0;
                        txt += $"\n\n「{cross.Name}」：局部 {(axis == 0 ? "X" : "Y")} 範圍 = {ratio:0.###} × 參數 + {offset:0.#} mm（參數現值 {p0:0.#}）。" +
                               $"\n橫擔沿族局部 {(axis == 0 ? "X" : "Y")} → 修正角候選 {sug:0}°（請按「放置/更新」目視確認）。";
                        win.SuggestCorrection(sug);
                    }
                }
                else txt += $"\n\n「{cross.Name}」無法量測：{ce?.Error}";
                win.Show(txt);
            };
            win.CanConfirm = () => test != null && effects != null && win.CrossModel != null && win.TryValues(out _, out _);

            var ok = win.ShowDialog() == true && win.Confirmed;
            trial?.RollBack(); trial = null;
            if (!ok) { group.RollBack(); TaskDialog.Show("校正目標吊架", "已取消：校正建立的測試吊架、工具類型" + (loadHere ? "、載入的族" : "") + "全部回滾。"); return Result.Cancelled; }
            group.Assimilate();   // 只剩「載入族」（若有）；測試吊架與量測已回滾

            win.TryValues(out var fc, out var fo);
            var cm = win.CrossModel!.Value;
            var crossSel = Cross();
            var sig = HangerCalibration.ProjectSignature(doc, famName, typeName) ?? throw new InvalidOperationException("找不到目標類型，無法建立簽章");
            File.Copy(cfgPath, cfgPath + $".pre-calibration-{DateTime.Now:yyyyMMddHHmmss}.bak");
            cfg["family"] = new JsonObject { ["family_name"] = famName, ["type_name"] = typeName };
            cfg["placement_mode"] = mode;
            cfg["orientation_correction_deg"] = fc;
            cfg["insertion_to_bearing_mm"] = fo;
            cfg["parameter_map"] = new JsonObject { ["cross_member_length"] = new JsonObject { ["name"] = crossSel.Name, ["scope"] = crossSel.Scope } };
            if (Rod() is { } rsel && cfg["rod_length"] is JsonObject rl && rl["mode"]?.GetValue<string>() == "fixed")
                rl["param"] = new JsonObject { ["name"] = rsel.Name, ["scope"] = rsel.Scope };
            cfg["calibration"] = new JsonObject
            {
                ["version"] = 1, ["calibrated_at"] = DateTimeOffset.Now.ToString("yyyy-MM-dd HH:mm:ss zzz", CultureInfo.InvariantCulture),
                ["family_file"] = tf["family_file"]!.DeepClone(), ["family_name"] = famName, ["type_name"] = typeName,
                ["placement_type"] = apiPlacement, ["parameter_set_sha256"] = tf["parameter_set_sha256"]?.DeepClone(),
                ["project_signature"] = sig,
                ["cross_member_model"] = new JsonObject { ["param"] = crossSel.Name, ["axis"] = cm.Axis == 0 ? "local_x" : "local_y", ["ratio"] = cm.Ratio, ["offset_mm"] = cm.Offset },
                ["measured_effects"] = new JsonArray(effects!.Select(e => (JsonNode?)e.Text()).ToArray()),
                ["bearing_face_candidates_local_z_mm"] = new JsonArray(baseEx?.UpFacesZ.Select(z => (JsonNode?)z).ToArray() ?? Array.Empty<JsonNode?>()),
                ["test_tray"] = tray.Id.Value, ["note"] = "insertion_to_bearing_mm 與 orientation_correction_deg 由使用者在測試放置後確認",
            };
            File.WriteAllText(cfgPath, cfg.ToJsonString(Json));
            TaskDialog.Show("校正目標吊架", $"已儲存校正：{cfgPath}\n修正角 {fc}°、插入點→承托面 {fo} mm、橫擔參數 {crossSel.Name}（{cm.Ratio:0.###}×+{cm.Offset:0.#}）。\n測試吊架已移除；之後族檔或專案中的族變更時，放置命令會要求重新校正。");
            return Result.Succeeded;
        }
        catch (Exception ex)
        {
            if (trial != null && trial.GetStatus() == TransactionStatus.Started) trial.RollBack();
            if (group.GetStatus() == TransactionStatus.Started) group.RollBack();
            message = $"校正失敗，已全部回滾：{ex.GetType().Name}: {ex.Message}";
            TaskDialog.Show("校正目標吊架", message);
            return Result.Failed;
        }
    }

    static double ReadMm(FamilyInstance fi, (string Name, string Scope) p)
    {
        Element owner = p.Scope == "type" ? fi.Document.GetElement(fi.GetTypeId()) : fi;
        return Units.FeetToMm(owner.LookupParameter(p.Name)?.AsDouble() ?? double.NaN);
    }

    static void SetMm(Element e, string name, double mm)
    {
        var p = e.LookupParameter(name) ?? throw new InvalidOperationException($"找不到參數「{name}」");
        if (p.IsReadOnly || !p.Set(Units.MmToFeet(mm))) throw new InvalidOperationException($"參數「{name}」無法設定為 {mm:0.#} mm");
    }
}

/// <summary>校正視窗（程式碼建立的 WPF；在命令內以 ShowDialog 執行，按鈕事件中呼叫 Revit API）。</summary>
sealed class CalibrationWindow : Window
{
    public readonly ComboBox Cross = new(), Rod = new(), Bearing = new();
    readonly TextBox _corr = new(), _off = new(), _out = new() { IsReadOnly = true, TextWrapping = TextWrapping.Wrap, VerticalScrollBarVisibility = ScrollBarVisibility.Auto, AcceptsReturn = true };
    public Action? OnPlace, OnMeasure;
    public Func<bool>? CanConfirm;
    public (double Ratio, double Offset, int Axis)? CrossModel;
    public bool Confirmed;

    public CalibrationWindow(string header, List<string> cands, double? corr, double? off)
    {
        Title = "校正目標吊架"; Width = 760; Height = 640; WindowStartupLocation = WindowStartupLocation.CenterOwner;
        foreach (var c in cands) Cross.Items.Add(c);
        Cross.SelectedIndex = 0;
        Rod.Items.Add("（無／不對應吊桿長度）");
        foreach (var c in cands) Rod.Items.Add(c);
        Rod.SelectedIndex = 0;
        _corr.Text = corr?.ToString(CultureInfo.InvariantCulture) ?? "";
        _off.Text = off?.ToString(CultureInfo.InvariantCulture) ?? "";
        Bearing.SelectionChanged += (_, _) => { if (Bearing.SelectedItem is double z) _off.Text = z.ToString(CultureInfo.InvariantCulture); };
        Cross.SelectionChanged += (_, _) => CrossModel = null;

        var sp = new StackPanel { Margin = new Thickness(10) };
        sp.Children.Add(new TextBlock { Text = header, TextWrapping = TextWrapping.Wrap, Margin = new Thickness(0, 0, 0, 8) });
        void Row(string label, FrameworkElement c)
        {
            var g = new WpfGrid { Margin = new Thickness(0, 2, 0, 2) };
            g.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(230) });
            g.ColumnDefinitions.Add(new ColumnDefinition());
            var t = new TextBlock { Text = label, VerticalAlignment = VerticalAlignment.Center };
            WpfGrid.SetColumn(c, 1);
            g.Children.Add(t); g.Children.Add(c);
            sp.Children.Add(g);
        }
        Row("橫擔長度參數（候選：可調長度參數）", Cross);
        Row("吊桿長度參數（rod_length=fixed 時用）", Rod);
        Row("修正角 orientation_correction_deg", _corr);
        Row("插入點→承托面 insertion_to_bearing_mm", _off);
        Row("承托面候選（朝上水平面局部 Z）", Bearing);
        var btns = new StackPanel { Orientation = Orientation.Horizontal, Margin = new Thickness(0, 8, 0, 8) };
        Button B(string t, Action a) { var b = new Button { Content = t, Margin = new Thickness(0, 0, 8, 0), Padding = new Thickness(10, 4, 10, 4) }; b.Click += (_, _) => Guard(a); btns.Children.Add(b); return b; }
        B("1. 放置／更新測試吊架", () => OnPlace?.Invoke());
        B("2. 量測參數效果", () => OnMeasure?.Invoke());
        B("確認並儲存", () =>
        {
            if (CanConfirm?.Invoke() != true) { Show("尚不能儲存：需已放置測試吊架、已量測且所選橫擔參數會改變水平範圍、兩個數值有效。"); return; }
            Confirmed = true; DialogResult = true;
        });
        B("取消（全部回滾）", () => { Confirmed = false; DialogResult = false; });
        sp.Children.Add(btns);
        _out.Height = 330;
        sp.Children.Add(_out);
        Content = sp;
    }

    void Guard(Action a)
    {
        try { a(); }
        catch (Exception ex) { Show($"失敗：{ex.GetType().Name}: {ex.Message}"); }
    }

    public void Show(string s) => _out.Text = s;
    public void SuggestCorrection(double d) { if (string.IsNullOrWhiteSpace(_corr.Text)) _corr.Text = d.ToString(CultureInfo.InvariantCulture); }
    public void SetBearingCandidates(List<double> zs) { Bearing.Items.Clear(); foreach (var z in zs) Bearing.Items.Add(z); }

    public bool TryValues(out double corr, out double off)
    {
        var ok1 = double.TryParse(_corr.Text, NumberStyles.Float, CultureInfo.InvariantCulture, out corr) && double.IsFinite(corr);
        var ok2 = double.TryParse(_off.Text, NumberStyles.Float, CultureInfo.InvariantCulture, out off) && double.IsFinite(off);
        if (!ok1 || !ok2) Show("請輸入有效的修正角與插入點→承托面距離（數字）。");
        return ok1 && ok2;
    }
}
