using System.Text.Json;
using System.Text.Json.Serialization;

namespace MepTray.Core;

public sealed class CoordinateSystemDto
{
    [JsonPropertyName(Fields.Basis)] public string Basis { get; set; } = "UNSPECIFIED";
    [JsonPropertyName(Fields.Origin)] public double[] Origin { get; set; } = new double[3];
    [JsonPropertyName(Fields.Unit)] public string Unit { get; set; } = "";
    [JsonPropertyName(Fields.AxisX)] public double[] AxisX { get; set; } = new double[3];
    [JsonPropertyName(Fields.AxisY)] public double[] AxisY { get; set; } = new double[3];
    [JsonPropertyName(Fields.AxisZ)] public double[] AxisZ { get; set; } = new double[3];
    [JsonPropertyName(Fields.RotationDeg)] public double RotationDeg { get; set; }
}

public sealed class TrayDto
{
    [JsonPropertyName(Fields.TypeName)] public string? TypeName { get; set; }
    [JsonPropertyName(Fields.WidthMm)] public double WidthMm { get; set; }
    [JsonPropertyName(Fields.HeightMm)] public double HeightMm { get; set; }
    [JsonPropertyName(Fields.Kind)] public string Kind { get; set; } = "";
}

public sealed class SegmentDto
{
    [JsonPropertyName(Fields.Id)] public string Id { get; set; } = "";
    [JsonPropertyName(Fields.Start)] public double[] Start { get; set; } = new double[3];
    [JsonPropertyName(Fields.End)] public double[] End { get; set; } = new double[3];
}

public sealed class JointDto
{
    [JsonPropertyName(Fields.Id)] public string Id { get; set; } = "";
    [JsonPropertyName(Fields.Point)] public double[] Point { get; set; } = new double[3];
    [JsonPropertyName(Fields.Kind)] public string Kind { get; set; } = "";
    [JsonPropertyName(Fields.SegmentIds)] public List<string> Segments { get; set; } = new();
    [JsonPropertyName(Fields.Branch)] public string? Branch { get; set; }
}

public sealed class FindingDto
{
    [JsonPropertyName(Fields.SegmentId)] public string? SegmentId { get; set; }
    [JsonPropertyName(Fields.Status)] public string Status { get; set; } = "";
    [JsonPropertyName(Fields.Kind)] public string Kind { get; set; } = "";
    [JsonPropertyName(Fields.Code)] public string? Code { get; set; }
}

public sealed class TrayModel
{
    [JsonPropertyName(Fields.SchemaVersion)] public int SchemaVersion { get; set; }
    [JsonPropertyName(Fields.RunId)] public string RunId { get; set; } = "";
    [JsonPropertyName(Fields.Units)] public string Units { get; set; } = "";
    [JsonPropertyName(Fields.CoordinateSystem)] public CoordinateSystemDto CoordinateSystem { get; set; } = new();
    [JsonPropertyName(Fields.Tray)] public TrayDto Tray { get; set; } = new();
    [JsonPropertyName(Fields.Segments)] public List<SegmentDto> Segments { get; set; } = new();
    [JsonPropertyName(Fields.Joints)] public List<JointDto> Joints { get; set; } = new();
    [JsonPropertyName(Fields.Findings)] public List<FindingDto> Findings { get; set; } = new();
    [JsonPropertyName(Fields.Disclosures)] public List<string> Disclosures { get; set; } = new();
}

public static class ModelLoader
{
    public static readonly string[] Bases = { "INTERNAL_ORIGIN", "PROJECT_BASE_POINT", "SHARED_COORDINATES" };
    public static readonly string[] JointKinds = { "elbow", "tee", "cross", "union", "unsupported" };

    public static TrayModel Parse(string json)
    {
        var m = JsonSerializer.Deserialize<TrayModel>(json)
                ?? throw new InvalidDataException("JSON 內容為空");
        return m;
    }

    public static TrayModel Load(string path) => Parse(File.ReadAllText(path));

    /// <summary>結構/幾何驗證；回傳錯誤清單（空 = 合格）。不含 Revit 相關檢查，也不檢查 basis（見 BasisError）。</summary>
    public static List<string> Validate(TrayModel? m)
    {
        var e = new List<string>();
        if (m is null) return new List<string> { "模型為 null" };
        if (m.CoordinateSystem is null || m.Tray is null || m.Segments is null ||
            m.Joints is null || m.Findings is null || m.Disclosures is null)
            return new List<string> { "必要模型物件或集合為 null" };
        if (m.Findings.Any(f => f is null) || m.Disclosures.Any(d => d is null))
            e.Add("findings/disclosures 不得含 null 元素");
        if (m.SchemaVersion != Fields.SchemaVersion_Value)
            e.Add($"schema_version 應為 {Fields.SchemaVersion_Value}，實為 {m.SchemaVersion}");
        if (m.Units != "mm") e.Add($"units 應為 mm，實為 '{m.Units}'");
        if (m.CoordinateSystem.Unit != "mm") e.Add("coordinate_system.unit 應為 mm");
        var cs = m.CoordinateSystem;
        if (!Finite3(cs.Origin)) e.Add("coordinate_system.origin 需為 3 個有限數");
        else if (!double.IsFinite(cs.RotationDeg)) e.Add("coordinate_system.rotation_deg 需為有限數");
        else if (!Finite3(cs.AxisX) || !Finite3(cs.AxisY) || !Finite3(cs.AxisZ)) e.Add("coordinate_system.axis_x/y/z 需為 3 個有限數");
        else
        {
            // 只支援繞 Z 軸旋轉：axis_x=(cosθ,sinθ,0)、axis_y=(−sinθ,cosθ,0)、axis_z=(0,0,1)，必須與 rotation_deg 一致，不得忽略任何變換。
            var th = cs.RotationDeg * Math.PI / 180.0;
            double c = Math.Cos(th), s = Math.Sin(th);
            if (!Near3(cs.AxisX, c, s, 0) || !Near3(cs.AxisY, -s, c, 0) || !Near3(cs.AxisZ, 0, 0, 1))
                e.Add("coordinate_system 軸與 rotation_deg 不一致；只支援繞 Z 軸旋轉（axis_x=(cosθ,sinθ,0)、axis_y=(−sinθ,cosθ,0)、axis_z=(0,0,1)）");
        }
        if (!(m.Tray.WidthMm > 0 && double.IsFinite(m.Tray.WidthMm))) e.Add("tray.width_mm 需為正的有限數");
        if (!(m.Tray.HeightMm > 0 && double.IsFinite(m.Tray.HeightMm))) e.Add("tray.height_mm 需為正的有限數");
        if (m.Segments.Count == 0) e.Add("segments 為空");
        var ids = new HashSet<string>();
        foreach (var s in m.Segments)
        {
            if (s is null) { e.Add("segments 不得含 null 元素"); continue; }
            if (string.IsNullOrWhiteSpace(s.Id) || !ids.Add(s.Id)) e.Add($"segment id 空白或重複: '{s.Id}'");
            if (!Finite3(s.Start) || !Finite3(s.End)) e.Add($"segment {s.Id} 座標需為 3 個有限數");
            else if (Dist(s.Start, s.End) < 1e-6) e.Add($"segment {s.Id} 長度為 0");
        }
        var jointIds = new HashSet<string>();
        foreach (var j in m.Joints)
        {
            if (j is null) { e.Add("joints 不得含 null 元素"); continue; }
            if (string.IsNullOrWhiteSpace(j.Id) || !jointIds.Add(j.Id)) e.Add($"joint id 空白或重複: '{j.Id}'");
            if (j.Segments is null) { e.Add($"joint {j.Id} segments 為 null"); continue; }
            if (j.Segments.Distinct().Count() != j.Segments.Count) e.Add($"joint {j.Id} 重複引用 segment");
            if (Array.IndexOf(JointKinds, j.Kind) < 0) e.Add($"joint {j.Id} kind 未知: '{j.Kind}'");
            foreach (var sid in j.Segments) if (!ids.Contains(sid)) e.Add($"joint {j.Id} 參照不存在的 segment {sid}");
            if (j.Kind == "tee" && (j.Segments.Count != 3 || j.Branch != j.Segments.LastOrDefault()))
                e.Add($"joint {j.Id}: tee 需 3 段且 branch 為最後一段");
            if (j.Kind == "cross" && j.Segments.Count != 4) e.Add($"joint {j.Id}: cross 需 4 段");
            if (j.Kind == "elbow" && j.Segments.Count != 2) e.Add($"joint {j.Id}: elbow 需 2 段");
            if (j.Kind == "union" && j.Segments.Count != 2) e.Add($"joint {j.Id}: union 需 2 段");
            if (!Finite3(j.Point)) e.Add($"joint {j.Id} 座標需為 3 個有限數");
        }
        return e;
    }

    /// <summary>basis 必須由使用者明確指定；UNSPECIFIED 或未知值回傳錯誤文字，否則 null。</summary>
    public static string? BasisError(string basis) =>
        Array.IndexOf(Bases, basis) >= 0 ? null
        : basis == "UNSPECIFIED" ? "座標系基準為 UNSPECIFIED：需指定 INTERNAL_ORIGIN / PROJECT_BASE_POINT / SHARED_COORDINATES，匯入器不自動假定"
        : $"未知的座標系基準 '{basis}'";

    static bool Near3(double[] v, double x, double y, double z) =>
        Finite3(v) && Math.Abs(v[0] - x) <= AxisTol && Math.Abs(v[1] - y) <= AxisTol && Math.Abs(v[2] - z) <= AxisTol;
    const double AxisTol = 1e-6;

    /// <summary>模型區域座標（mm）→ basis 座標（mm）：origin + Rz(rotation_deg)·點。呼叫前須已通過 Validate。</summary>
    public static double[] ToBasis(CoordinateSystemDto cs, double[] p)
    {
        var th = cs.RotationDeg * Math.PI / 180.0;
        double c = Math.Cos(th), s = Math.Sin(th);
        return new[] { cs.Origin[0] + c * p[0] - s * p[1], cs.Origin[1] + s * p[0] + c * p[1], cs.Origin[2] + p[2] };
    }

    static bool Finite3(double[] v) => v is not null && v.Length == 3 && v.All(double.IsFinite);
    static bool Same3(double[] v, double x, double y, double z) =>
        Finite3(v) && v[0] == x && v[1] == y && v[2] == z;
    static double Dist(double[] a, double[] b) =>
        Math.Sqrt((a[0] - b[0]) * (a[0] - b[0]) + (a[1] - b[1]) * (a[1] - b[1]) + (a[2] - b[2]) * (a[2] - b[2]));
}
