using System.Globalization;
using System.Text.Encodings.Web;
using System.Text.Json;
using System.Text.Json.Nodes;
using Autodesk.Revit.Attributes;
using Autodesk.Revit.DB;
using Autodesk.Revit.DB.Electrical;
using Autodesk.Revit.DB.Mechanical;
using Autodesk.Revit.UI;
using Autodesk.Revit.UI.Selection;
using MepTray.Core;
using Units = MepTray.Core.Units;

namespace MepTray.Revit;

/// <summary>
/// 「彎頭／三通探查」：唯讀讀取選取的 Cable Tray 接頭（彎頭、三通、其他 fitting）的 connector、幾何曲線、PartType、族／類型與實例參數，
/// 寫成 JSON。不開任何 Transaction、不修改模型、不儲存。只回報 API 讀到的事實與純數學的衍生量（connector 夾角、距離），
/// 不判斷哪個是「中心線」、不推測半徑；讀取失敗的屬性以 {"error": …} 記錄，不中斷整份報告。
/// </summary>
[Transaction(TransactionMode.ReadOnly)]
public class FittingProbeCommand : IExternalCommand
{
    static readonly JsonSerializerOptions Json = new() { WriteIndented = true, Encoder = JavaScriptEncoder.UnsafeRelaxedJsonEscaping };
    const int MaxCurvesPerFitting = 200;
    const int MaxEdgesPerSolid = 400;

    sealed class FittingFilter : ISelectionFilter
    {
        public bool AllowElement(Element e) => IsFitting(e);
        public bool AllowReference(Reference reference, XYZ position) => false;
    }

    static bool IsFitting(Element e) =>
        e is FamilyInstance fi && (fi.Category?.BuiltInCategory == BuiltInCategory.OST_CableTrayFitting || fi.MEPModel is MechanicalFitting);

    public Result Execute(ExternalCommandData commandData, ref string message, ElementSet elements)
    {
        var uidoc = commandData.Application.ActiveUIDocument;
        var doc = uidoc?.Document;
        if (uidoc is null || doc is null) { message = "請先開啟 Revit 專案"; return Result.Failed; }
        var fittings = uidoc.Selection.GetElementIds().Select(doc.GetElement).Where(e => e != null && IsFitting(e)).Cast<FamilyInstance>().ToList();
        if (fittings.Count == 0)
        {
            try
            {
                fittings = uidoc.Selection.PickObjects(ObjectType.Element, new FittingFilter(), "選取要探查的橋架接頭（彎頭／三通等），完成後按 Finish")
                    .Select(r => doc.GetElement(r.ElementId)).OfType<FamilyInstance>().ToList();
            }
            catch (Autodesk.Revit.Exceptions.OperationCanceledException) { return Result.Cancelled; }
        }
        if (fittings.Count == 0) { message = "沒有選取任何橋架接頭"; return Result.Failed; }

        var root = new JsonObject
        {
            ["probed_at"] = DateTimeOffset.Now.ToString("yyyy-MM-dd HH:mm:ss zzz", CultureInfo.InvariantCulture),
            ["revit"] = $"{commandData.Application.Application.VersionName} {commandData.Application.Application.SubVersionNumber}",
            ["document"] = doc.Title,
            ["read_only"] = "未開 Transaction、未修改模型；只讀取 connector／幾何／參數",
            ["units"] = "長度同時列 ft（Revit 內部）與 mm（×304.8）",
        };
        var arr = new JsonArray();
        foreach (var fi in fittings.OrderBy(f => f.Id.Value)) arr.Add(Probe(fi));
        root["fittings"] = arr;

        var dir = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.MyDocuments), "MEP_tray");
        var path = Path.Combine(dir, $"fitting_probe_{DateTime.Now:yyyyMMdd-HHmmss}.json");
        try
        {
            Directory.CreateDirectory(dir);
            File.WriteAllText(path, root.ToJsonString(Json));
        }
        catch (Exception ex) when (ex is IOException or UnauthorizedAccessException)
        { message = $"無法寫入報告：{ex.Message}"; TaskDialog.Show("MEP Tray — 接頭探查", message); return Result.Failed; }

        TaskDialog.Show("MEP Tray — 接頭探查（唯讀）",
            $"已探查 {fittings.Count} 個接頭。\n報告：{path}\n\n" + string.Join("\n", arr.Select(a => a!["summary"]!.GetValue<string>())));
        return Result.Succeeded;
    }

    // ── 單一接頭 ──
    static JsonObject Probe(FamilyInstance fi)
    {
        var o = new JsonObject
        {
            ["element_id"] = fi.Id.Value, ["unique_id"] = fi.UniqueId,
            ["category"] = fi.Category?.Name, ["family"] = fi.Symbol?.Family?.Name, ["type"] = fi.Symbol?.Name,
            ["part_type"] = Safe(() => (fi.MEPModel as MechanicalFitting)?.PartType.ToString()),
            ["mep_model_class"] = fi.MEPModel?.GetType().Name,
            ["location"] = Safe(() => fi.Location switch
            {
                LocationPoint lp => new JsonObject { ["kind"] = "point", ["point"] = Pt(lp.Point), ["rotation_deg"] = Math.Round(lp.Rotation * 180.0 / Math.PI, 6) },
                LocationCurve lc => new JsonObject { ["kind"] = "curve", ["curve"] = CurveInfo(lc.Curve) },
                var l => new JsonObject { ["kind"] = l?.GetType().Name ?? "none" },
            }),
            ["hand_flipped"] = Safe(() => fi.HandFlipped), ["facing_flipped"] = Safe(() => fi.FacingFlipped),
            ["bounding_box"] = Safe(() => fi.get_BoundingBox(null) is BoundingBoxXYZ b ? new JsonObject { ["min"] = Pt(b.Min), ["max"] = Pt(b.Max) } : null),
        };
        var conns = new JsonArray();
        var connData = new List<(int Idx, XYZ Origin, XYZ? Z)>();
        int i = 0;
        var cmgr = fi.MEPModel?.ConnectorManager;
        if (cmgr is null) o["connector_error"] = "MEPModel／ConnectorManager 為 null";
        else
        {
            try
            {
                foreach (Connector c in cmgr.Connectors)
                {
                    conns.Add(ConnectorInfo(c, i));
                    XYZ? z = null;
                    try { z = c.CoordinateSystem.BasisZ; } catch (Exception) { z = null; }
                    connData.Add((i, c.Origin, z));
                    i++;
                }
            }
            catch (Exception ex) { o["connector_error"] = $"{ex.GetType().Name}: {ex.Message}"; }
        }
        o["connectors"] = conns;
        o["connector_math"] = ConnectorMath(connData);
        var geo = GeometryInfo(fi);
        o["geometry"] = geo;
        o["instance_parameters"] = Params(fi);
        o["type_parameters"] = fi.Symbol is null ? null : Params(fi.Symbol);
        o["summary"] = $"#{fi.Id.Value} {fi.Symbol?.Family?.Name}/{fi.Symbol?.Name} PartType={o["part_type"]?.ToJsonString()} connectors={conns.Count} curves={geo["curve_count"]}";
        return o;
    }

    // ── Connector ──
    static JsonObject ConnectorInfo(Connector c, int idx)
    {
        var o = new JsonObject { ["index"] = idx };
        o["id"] = Safe(() => c.Id);
        o["connector_type"] = Safe(() => c.ConnectorType.ToString());
        o["domain"] = Safe(() => c.Domain.ToString());
        o["shape"] = Safe(() => c.Shape.ToString());
        o["is_connected"] = Safe(() => c.IsConnected);
        o["origin"] = Safe(() => Pt(c.Origin));
        o["coordinate_system"] = Safe(() =>
        {
            var t = c.CoordinateSystem;
            return new JsonObject { ["origin"] = Pt(t.Origin), ["basis_x"] = Vec(t.BasisX), ["basis_y"] = Vec(t.BasisY), ["basis_z"] = Vec(t.BasisZ) };
        });
        o["direction"] = Safe(() => c.Direction.ToString());
        o["angle"] = Safe(() => c.Angle);
        o["width"] = Safe(() => Len(c.Width));
        o["height"] = Safe(() => Len(c.Height));
        o["radius"] = Safe(() => Len(c.Radius));
        var refs = new JsonArray();
        try
        {
            foreach (Connector r in c.AllRefs)
                refs.Add(new JsonObject
                {
                    ["owner_id"] = r.Owner?.Id.Value, ["owner_class"] = r.Owner?.GetType().Name, ["owner_category"] = r.Owner?.Category?.Name,
                    ["ref_connector_type"] = r.ConnectorType.ToString(), ["ref_origin"] = Safe(() => Pt(r.Origin)),
                    ["owner_tray"] = r.Owner is CableTray ct ? TrayInfo(ct) : null,
                });
        }
        catch (Exception ex) { refs.Add(new JsonObject { ["error"] = $"{ex.GetType().Name}: {ex.Message}" }); }
        o["all_refs"] = refs;
        return o;
    }

    static JsonObject TrayInfo(CableTray t) => new()
    {
        ["curve"] = Safe(() => t.Location is LocationCurve lc ? CurveInfo(lc.Curve) : null),
        ["width"] = Safe(() => Len(t.get_Parameter(BuiltInParameter.RBS_CABLETRAY_WIDTH_PARAM).AsDouble())),
        ["height"] = Safe(() => Len(t.get_Parameter(BuiltInParameter.RBS_CABLETRAY_HEIGHT_PARAM).AsDouble())),
    };

    /// <summary>純數學衍生量：任兩個 connector 的 BasisZ 夾角（度）與 Origin 距離。不判斷彎頭角度或半徑。</summary>
    static JsonArray ConnectorMath(List<(int Idx, XYZ Origin, XYZ? Z)> d)
    {
        var a = new JsonArray();
        for (int p = 0; p < d.Count; p++)
            for (int q = p + 1; q < d.Count; q++)
            {
                double? ang = d[p].Z is { } za && d[q].Z is { } zb ? Math.Round(za.AngleTo(zb) * 180.0 / Math.PI, 6) : null;
                a.Add(new JsonObject
                {
                    ["a"] = d[p].Idx, ["b"] = d[q].Idx,
                    ["origin_distance"] = Len(d[p].Origin.DistanceTo(d[q].Origin)),
                    ["basis_z_angle_deg"] = ang,
                });
            }
        return a;
    }

    // ── 幾何 ──
    static JsonObject GeometryInfo(FamilyInstance fi)
    {
        var o = new JsonObject();
        try
        {
            var opt = new Options { ComputeReferences = false, DetailLevel = ViewDetailLevel.Fine, IncludeNonVisibleObjects = true };
            var ge = fi.get_Geometry(opt);
            if (ge is null) { o["error"] = "Element.Geometry 回傳 null"; return o; }
            var doc = fi.Document;
            var curves = new JsonArray();
            var solids = new JsonArray();
            var counts = new SortedDictionary<string, int>(StringComparer.Ordinal);
            int total = 0;
            void Walk(GeometryElement g)
            {
                foreach (GeometryObject go in g)
                {
                    var kind = go.GetType().Name;
                    counts[kind] = counts.GetValueOrDefault(kind) + 1;
                    switch (go)
                    {
                        case GeometryInstance gi:
                            Walk(gi.GetInstanceGeometry());
                            break;
                        case Curve c:
                            total++;
                            if (curves.Count < MaxCurvesPerFitting)
                            {
                                var ci = CurveInfo(c);
                                ci["source"] = "geometry_curve";
                                ci["graphics_style"] = StyleName(doc, go);
                                curves.Add(ci);
                            }
                            break;
                        case Solid s when s.Faces.Size > 0:
                            solids.Add(SolidInfo(doc, s, curves, ref total));
                            break;
                    }
                }
            }
            Walk(ge);
            o["object_counts"] = Counts(counts);
            o["curve_count"] = total;
            o["curves_truncated"] = total > curves.Count;
            o["curves"] = curves;
            o["solids"] = solids;
        }
        catch (Exception ex) { o["error"] = $"{ex.GetType().Name}: {ex.Message}"; }
        return o;
    }

    static JsonObject SolidInfo(Document doc, Solid s, JsonArray curves, ref int total)
    {
        var faceKinds = new SortedDictionary<string, int>(StringComparer.Ordinal);
        foreach (Face f in s.Faces) faceKinds[f.GetType().Name] = faceKinds.GetValueOrDefault(f.GetType().Name) + 1;
        var arcEdges = new JsonArray();
        var edgeKinds = new SortedDictionary<string, int>(StringComparer.Ordinal);
        foreach (Edge e in s.Edges)
        {
            Curve c;
            try { c = e.AsCurve(); } catch (Exception) { continue; }
            edgeKinds[c.GetType().Name] = edgeKinds.GetValueOrDefault(c.GetType().Name) + 1;
            if (c is Arc && arcEdges.Count < MaxEdgesPerSolid)
            {
                var ci = CurveInfo(c);
                ci["source"] = "solid_edge";
                arcEdges.Add(ci);
            }
        }
        total += arcEdges.Count;
        return new JsonObject
        {
            ["graphics_style"] = StyleName(doc, s),
            ["volume"] = Safe(() => s.Volume), ["face_count"] = s.Faces.Size, ["edge_count"] = s.Edges.Size,
            ["face_kinds"] = Counts(faceKinds),
            ["edge_curve_kinds"] = Counts(edgeKinds),
            ["arc_edges"] = arcEdges,
        };
    }

    static JsonObject Counts(SortedDictionary<string, int> d)
    {
        var o = new JsonObject();
        foreach (var kv in d) o[kv.Key] = kv.Value;
        return o;
    }

    static string? StyleName(Document doc, GeometryObject go)
    {
        try { return doc.GetElement(go.GraphicsStyleId) is GraphicsStyle gs ? gs.GraphicsStyleCategory?.Name + "/" + gs.Name : null; }
        catch (Exception) { return null; }
    }

    static JsonObject CurveInfo(Curve c)
    {
        var o = new JsonObject { ["class"] = c.GetType().Name, ["is_bound"] = c.IsBound };
        if (!c.IsBound) return o;
        o["start"] = Pt(c.GetEndPoint(0));
        o["end"] = Pt(c.GetEndPoint(1));
        o["length"] = Safe(() => Len(c.Length));
        if (c is Arc a)
        {
            o["radius"] = Len(a.Radius);
            o["center"] = Pt(a.Center);
            o["normal"] = Vec(a.Normal);
            o["sweep_deg"] = Math.Round(a.Length / a.Radius * 180.0 / Math.PI, 6);
        }
        return o;
    }

    // ── 參數 ──
    static JsonArray Params(Element e)
    {
        var a = new JsonArray();
        foreach (Parameter p in e.Parameters.Cast<Parameter>().OrderBy(p => p.Definition?.Name, StringComparer.Ordinal))
        {
            var o = new JsonObject
            {
                ["name"] = p.Definition?.Name, ["storage"] = p.StorageType.ToString(), ["read_only"] = p.IsReadOnly,
                ["data_type"] = Safe(() => p.Definition?.GetDataType().TypeId),
                ["value_string"] = Safe(() => p.AsValueString()),
            };
            switch (p.StorageType)
            {
                case StorageType.Double: o["double_internal"] = p.AsDouble(); break;
                case StorageType.Integer: o["integer"] = p.AsInteger(); break;
                case StorageType.String: o["string"] = p.AsString(); break;
                case StorageType.ElementId: o["element_id"] = p.AsElementId().Value; break;
            }
            a.Add(o);
        }
        return a;
    }

    // ── 小工具 ──
    static JsonNode? Safe(Func<object?> f)
    {
        try
        {
            return f() switch
            {
                null => null,
                JsonNode n => n,
                bool b => JsonValue.Create(b),
                int i => JsonValue.Create(i),
                long l => JsonValue.Create(l),
                double d => JsonValue.Create(d),
                string s => JsonValue.Create(s),
                var other => JsonValue.Create(other.ToString()),
            };
        }
        catch (Exception ex) { return new JsonObject { ["error"] = $"{ex.GetType().Name}: {ex.Message}" }; }
    }

    static JsonObject Len(double ft) => new() { ["ft"] = ft, ["mm"] = Math.Round(Units.FeetToMm(ft), 3) };
    static JsonObject Pt(XYZ p) => new()
    {
        ["ft"] = new JsonArray(p.X, p.Y, p.Z),
        ["mm"] = new JsonArray(Math.Round(Units.FeetToMm(p.X), 3), Math.Round(Units.FeetToMm(p.Y), 3), Math.Round(Units.FeetToMm(p.Z), 3)),
    };
    static JsonArray Vec(XYZ v) => new(Math.Round(v.X, 9), Math.Round(v.Y, 9), Math.Round(v.Z, 9));
}
