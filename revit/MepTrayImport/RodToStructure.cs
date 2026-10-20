using System.Globalization;
using Autodesk.Revit.DB;
using Autodesk.Revit.DB.Electrical;
using MepTray.Core;
using Units = MepTray.Core.Units;

namespace MepTray.Revit;

/// <summary>射線命中的結構元件（可能位於連結模型）。</summary>
public sealed record StructureHit(double ZMm, string ElementId, string? LinkInstanceId, string Category);

/// <summary>
/// rod_length.mode = to_structure：從每支新吊架的承托面點沿 +Z 打射線，只接受樓板（OST_Floors）與結構構架（OST_StructuralFraming），
/// 含連結模型內的元件。天花板（OST_Ceilings）等其他類別一律不接受；沒打到結構 → 該支失敗，不退而求其次。
/// 只讀取模型、不開交易；規劃（預覽）與實際放置共用同一份計算結果。
/// </summary>
public static class RodToStructure
{
    static readonly BuiltInCategory[] Targets = { BuiltInCategory.OST_Floors, BuiltInCategory.OST_StructuralFraming };

    static bool IsTarget(BuiltInCategory? c) => c is BuiltInCategory b && Array.IndexOf(Targets, b) >= 0;

    /// <summary>目前作用中的 3D 視圖（非範本、非透視）；否則 {3D}；否則任一非範本、非透視的 3D 視圖。找不到回傳 null（不建立視圖）。</summary>
    public static View3D? PickView(Document doc)
    {
        bool Ok(View3D v) => !v.IsTemplate && !v.IsPerspective;
        if (doc.ActiveView is View3D av && Ok(av)) return av;
        var all = new FilteredElementCollector(doc).OfClass(typeof(View3D)).Cast<View3D>().Where(Ok).ToList();
        return all.FirstOrDefault(v => v.Name == "{3D}") ?? all.FirstOrDefault();
    }

    /// <summary>沿 +Z 取第一個命中樓板／結構構架的底面。沒有符合者回傳 null。</summary>
    public static StructureHit? Probe(Document doc, View3D view, double[] originMm)
    {
        var filter = new ElementMulticategoryFilter(Targets);
        var ri = new ReferenceIntersector(filter, FindReferenceTarget.Face, view) { FindReferencesInRevitLinks = true };
        var origin = new XYZ(Units.MmToFeet(originMm[0]), Units.MmToFeet(originMm[1]), Units.MmToFeet(originMm[2]));
        foreach (var h in ri.Find(origin, XYZ.BasisZ).OrderBy(x => x.Proximity))
        {
            var r = h.GetReference();
            Element? e;
            string? linkId = null;
            if (r.LinkedElementId != ElementId.InvalidElementId)
            {
                var link = doc.GetElement(r.ElementId) as RevitLinkInstance;
                e = link?.GetLinkDocument()?.GetElement(r.LinkedElementId);
                linkId = r.ElementId.Value.ToString(CultureInfo.InvariantCulture);
            }
            else e = doc.GetElement(r.ElementId);
            // 篩選器已限定類別；這裡再核對一次（連結內元件、篩選行為差異時仍不會接到天花板等其他類別）
            if (e is null || !IsTarget(e.Category?.BuiltInCategory)) continue;
            var z = r.GlobalPoint?.Z ?? origin.Z + h.Proximity;
            return new StructureHit(Math.Round(Units.FeetToMm(z), 3) + 0.0,
                (linkId is null ? r.ElementId : r.LinkedElementId).Value.ToString(CultureInfo.InvariantCulture), linkId, e.Category!.Name);
        }
        return null;
    }

    /// <summary>為所有 status=new 的吊架計算吊桿長度；失敗者直接標為 failed 並寫明原因（不會被建立）。</summary>
    public static void Apply(Document doc, HangerRunReport rep, HangerConfig cfg, IReadOnlyDictionary<string, CableTray> trays)
    {
        var view = PickView(doc);
        if (view is null) return;   // 預檢已回報錯誤
        rep.Notes.Add($"吊桿長度至結構：以 3D 視圖「{view.Name}」從承托面向上打射線，僅接受樓板／結構構架（含連結模型）；視圖中這些類別或連結須為可見");
        foreach (var it in rep.Items.Where(i => i.Status == "new"))
        {
            var h = Units.FeetToMm(trays[it.TrayId].get_Parameter(BuiltInParameter.RBS_CABLETRAY_HEIGHT_PARAM).AsDouble());
            var bearingZ = it.BearingPointMm[2];
            StructureHit? hit;
            try { hit = Probe(doc, view, it.BearingPointMm); }
            catch (Exception ex) { Fail(it, $"射線檢測失敗：{ex.GetType().Name}: {ex.Message}"); continue; }
            if (hit is null) { Fail(it, $"承托面上方沒有樓板或結構構架（只接受 Floors／Structural Framing，不會接到天花板）；請確認結構模型與連結在視圖「{view.Name}」中可見"); continue; }
            it.HitElementId = hit.ElementId; it.HitLinkInstanceId = hit.LinkInstanceId; it.HitCategory = hit.Category; it.HitZMm = hit.ZMm;
            var res = RodLength.Compute(bearingZ, hit.ZMm, bearingZ + h, cfg);
            if (res.LengthMm is double len) it.RodLengthMm = len;
            else Fail(it, res.Message!);
        }
    }

    static void Fail(HangerItem it, string why)
    {
        it.Status = "failed";
        it.RodLengthMm = null;
        it.Detail = $"吊桿長度無法計算至結構：{why}";
    }
}
